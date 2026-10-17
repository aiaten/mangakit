"""safe_filename / safe_path 的离线回归：Windows 保留设备名 + MAX_PATH 截断。

背景（为什么要单独立一个套件）：
    这两个问题**在本机永远复现不了**，只有在别人机器上才炸：
      - 保留设备名（CON/NUL/COM1…）：`mkdir` 看起来成功，往里写文件却
        抛 FileNotFoundError（实测 winerror=206 或 3）。漫画标题里出现
        "NUL" 这种短英文词是完全可能的。
      - MAX_PATH：Windows 默认 260 字符上限，长标题漫画拼出
        `下载目录/漫画名/章节名/.part/0001.jpg.tmp` 就会超。本机
        下载目录短、漫画名也短时一切正常，分发给别人（比如下载目录在
        很深的 OneDrive 路径下）才会失败。

用法：
    python tools\\t_filenames.py
全部通过时末行打印 === 文件名回归全部通过 ===，退出码 0。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 本套件只读文件系统自身能力，但统一走隔离目录，保持 tools/ 下的约定
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_fname_"))

from mangakit.images import (  # noqa: E402
    MAX_PATH_BUDGET,
    safe_filename,
    safe_path,
)

_ok = 0
_bad = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"[ok] {label}" + (f" —— {detail}" if detail else ""))
    else:
        _bad += 1
        print(f"[BAD] {label}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    print("=== 1. Windows 保留设备名必须加前缀躲开 ===")
    reserved = ["CON", "con", "Con", "NUL", "PRN", "AUX", "COM1", "COM9",
                "LPT1", "LPT9", "CON.txt", "NUL ", "nul."]
    for n in reserved:
        got = safe_filename(n)
        check(got.startswith("_"), f"保留名 {n!r} -> {got!r}", got)
    # 这些不能误伤：a.CON 主名是 a，CONa 主名是 CONa，都不是设备名
    for n in ["a.CON", "CONa", "我的NUL", "nt", "CONTENT", "COM10"]:
        got = safe_filename(n)
        check(not got.startswith("_"), f"非保留名 {n!r} 不该被改 -> {got!r}", got)

    print()
    print("=== 2. 常规清理仍然正确（别为了新规则弄坏老行为） ===")
    cases = [
        ('第1话 a/b\\c:d*e?f"g<h>i|j', "第1话 a_b_c_d_e_f_g_h_i_j"),
        ("  前后空格  ", "前后空格"),
        ("结尾点...", "结尾点"),
        ("", "untitled"),
        ("   ", "untitled"),
        ("...", "untitled"),
        ("正常标题 第12话", "正常标题 第12话"),
    ]
    for src, want in cases:
        got = safe_filename(src)
        check(got == want, f"{src!r} -> {want!r}", got)
    long_name = safe_filename("x" * 300)
    check(len(long_name) <= 120, "超长名截到 120", f"len={len(long_name)}")

    print()
    print("=== 3. safe_path 保证不超 MAX_PATH 预算 ===")
    base = Path("C:/Users/someone/OneDrive/文档/漫画下载")
    for comic, chap in [("短", "第1话"),
                        ("漫" * 120, "第1话 " + "x" * 120),
                        ("漫" * 400, "第2话 " + "y" * 400)]:
        p = safe_path(base, comic, chap, tail=40)
        check(len(str(p)) <= MAX_PATH_BUDGET,
              f"comic={len(comic)} chap={len(chap)} -> {len(str(p))} 字符",
              f"预算 {MAX_PATH_BUDGET}")
        check(p.parent.parent == base, "层级结构没变（还是 base/漫画/章节）")
        check(p.name != "" and p.parent.name != "", "截断后没有空名字")

    print()
    print("=== 4. 真写盘：保留名与超长名都要能落下 0001.jpg ===")
    root = Path(tempfile.mkdtemp(prefix="mk_fname_disk_"))
    try:
        for comic, chap in [("NUL", "CON"), ("漫" * 400, "第1话 " + "y" * 400),
                            ("普通漫画", "第5话 终章。")]:
            d = safe_path(root, comic, chap, tail=40)
            try:
                d.mkdir(parents=True, exist_ok=True)
                (d / "0001.jpg").write_bytes(b"x")
                check(True, f"落盘成功 {len(str(d))} 字符", d.name[:36])
            except OSError as e:
                check(False, f"落盘失败 {d}",
                      f"{type(e).__name__} winerror={getattr(e, 'winerror', None)}")
        # 模拟下载器的真实布局：章节目录下还有 .part 子目录 + 页码 + .tmp
        d = safe_path(root, "漫" * 400, "第1话 " + "z" * 400, tail=40)
        part = d / (safe_filename("第1话 " + "z" * 400, maxlen=24) + ".part")
        try:
            part.mkdir(parents=True, exist_ok=True)
            (part / "0001.jpg.tmp").write_bytes(b"x")
            check(True, ".part 子目录 + 页码 + .tmp 也不超长", f"{len(str(part))} 字符")
        except OSError as e:
            check(False, ".part 布局落盘失败",
                  f"{type(e).__name__} winerror={getattr(e, 'winerror', None)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    print(f"通过 {_ok} 项，失败 {_bad} 项")
    if _bad:
        print("=== 文件名回归有失败项 ===")
        return 1
    print("=== 文件名回归全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
