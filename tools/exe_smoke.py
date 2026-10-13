r"""打包后 exe 的端到端冒烟：对多个站点各下载第 1 章前 N 张，逐站报告结果。

用法（必须在 mangakit 项目根目录下跑）：
    $env:PYTHONIOENCODING='utf-8'
    & <dsh python> tools\exe_smoke.py <exe路径>

给每个站配独立的 MANGAKIT_HOME（落在工作区，避免本机 AppData 权限问题），
用 Python 的 json.dump 写 config.json（正确转义反斜杠），跑完统计落盘文件数。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 产物落在 tools\_smoke\ 下，且每次运行前先清空：onefile 解包出来的 _MEI*
# 会残留几百 MB dll，exe 被占用时还删不掉（须先 Stop-Process MangaKit）。
MEI = ROOT / "tools" / "_smoke"

# 特意覆盖两类「只在打包后才容易出问题」的解析路径：
#   dongmanla -> chapters.url_replace 改写成 /all.html 单页滚动版
#   dingmanhua -> pages.lazy_js 字典按「前缀 + 张数」展开
CASES = [
    ("cw", "https://www.comicwang.com/title/15412.html", 2),
    ("mhz", "https://www.manhuazhan.xyz/book/1206", 2),
    ("mhg", "https://manhuagui.cc/comic/douluodalu", 2),
    ("kanman", "https://www.kanman.com/107883/", 2),
    ("dongmanla", "https://www.dongman.la/manhua/detail/9367/", 2),
    ("dingmanhua", "https://www.dingmanhua.com/comic/2579.html", 2),
]

KEEP = ("漫画：", "章节数：", "图片数：", "✓", "✗", "完成 ", "警告：", "× ")


def _kill_group(exc: BaseException) -> None:
    """超时后把整个进程组（含 onefile 解包子进程）收干净。

    PyInstaller onefile 会起一个父进程解包、再 fork 子进程跑真正的程序；
    父进程退出后子进程仍持有 stdout 管道，只 kill 父进程会让调用方一直读不到 EOF。
    """
    import signal

    def walk(pids: set[int]) -> None:
        for pid in list(pids):
            try:
                kids = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=20).stdout
            except Exception:  # noqa: BLE001
                continue
            pids |= {int(part) for row in kids.splitlines()
                     for part in row.split(",")[1:2] if part.strip().isdigit()}
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    pids: set[int] = set()
    if getattr(exc, "pid", None):
        pids.add(int(exc.pid))
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MangaKit.exe", "/NH", "/FO", "CSV"],
                           capture_output=True, text=True, timeout=20).stdout
        pids |= {int(row.split(",")[1]) for row in r.splitlines()
                 if len(row.split(",")) > 1 and row.split(",")[1].strip().isdigit()}
    except Exception:  # noqa: BLE001
        pass
    walk(pids)


def main() -> int:
    exe = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist" / "MangaKit.exe")
    if not exe.is_file():
        print(f"找不到 exe：{exe}")
        return 2

    base = MEI / "exe_smoke"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    # 上一次运行若没退出干净，解包出来的 _MEI* 会占着文件；再不清就把
    # 磁盘塞满，而且下次跑失败的原因跟真实代码问题无关。
    for stale in MEI.glob("_MEI*"):
        shutil.rmtree(stale, ignore_errors=True)

    env = dict(os.environ)
    env["TEMP"] = str(base / "tmp")
    env["TMP"] = str(base / "tmp")
    (base / "tmp").mkdir(parents=True, exist_ok=True)

    passed = 0
    for tag, url, limit in CASES:
        home = base / f"home_{tag}"
        dl = base / f"dl_{tag}"
        home.mkdir(parents=True, exist_ok=True)
        env["MANGAKIT_HOME"] = str(home)
        (home / "config.json").write_text(
            json.dumps({"download_dir": str(dl), "pack_after": "off",
                        "first_run_done": True}, ensure_ascii=False, indent=2),
            "utf-8")

        # windowed exe 的 onefile 解包子进程会继承并长期持有 stdout 管道，
        # 只靠 timeout 杀不掉（父进程已退出、孙进程还在），所以自己开进程组整组收。
        try:
            proc = subprocess.run(
                [str(exe), "--url", url, "--get", "1", "--limit", str(limit)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=180,
                start_new_session=True)  # 独立进程组
        except subprocess.TimeoutExpired as e:
            _kill_group(e)
            raw = e.stdout or ""
            out = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            imgs = [p for p in dl.rglob("*") if p.is_file()] if dl.exists() else []
            print(f"[TIMEOUT] {tag:7s} 180s 超时，已清理进程组；落盘 {len(imgs)} 个文件")
            for ln in out.splitlines():
                if any(k in ln for k in KEEP):
                    print(f"         {ln.strip()}")
            print()
            continue
        except OSError as e:
            print(f"[ERROR] {tag:7s} 无法启动：{e}")
            print()
            continue

        out = (proc.stdout or "") + (proc.stderr or "")
        files = [p for p in dl.rglob("*") if p.is_file()] if dl.exists() else []
        imgs = [p for p in files if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif")]
        keep = [ln.strip() for ln in out.splitlines() if any(k in ln for k in KEEP)]
        ok = bool(imgs)
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {tag:7s} rc={proc.returncode} "
              f"图片={len(imgs)} 文件={len(files)}")
        for ln in keep:
            print(f"         {ln}")
        if not ok and out.strip():
            print("         (无匹配输出，原始尾部)")
            for ln in out.strip().splitlines()[-6:]:
                print(f"         | {ln}")
        print()

    print(f"=== exe 冒烟 {passed}/{len(CASES)} 站有真实图片落盘 ===")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())