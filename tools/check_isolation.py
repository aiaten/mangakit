"""护栏检查：确认「从 tools/ 下起的脚本绝不碰用户真库」这条防线真的成立。

背景（2026-10-14 事故）
----------------------
`tools\\gui_smoke.py` 没设 MANGAKIT_HOME，而它驱动的界面代码里
`MangaPage._on_comic` 会自动 `store().add_favorite()` → 落到
`%APPDATA%\\mangakit\\mangakit.db`，把用户真实收藏清空了（取证在 `tools\\_incident\\`）。

为什么要功能性检查，而不是静态检查
----------------------------------
我第一版写的是静态规则：「脚本自己调了写接口才算危险，要求它设 MANGAKIT_HOME」。
这版规则**放过了 t_fix_check.py** —— 它全文没有任何写接口调用，却照样往真库写了，
因为写操作藏在产品代码里（`MangaPage._on_comic` → `store().add_favorite()`）。
「这个脚本会不会写」根本无法静态判定，所以静态检查要么漏（放行 t_fix_check），
要么滥（要求 20 个只读脚本都加样板头）。

真正的防线只有一条，在 `mangakit/config.py:_dev_sandbox()`：
**只要进程是从 `tools/xxx.py` 起的，数据目录就改到临时目录。**
本脚本就直接验证这条防线 —— 起子进程看 `app_dir()` 到底落在哪里：

1. 探针放在仓库 `tools/` 下 → 必须落到临时目录（不能是真库）
2. 探针放在别处的 `tools/`（旁边没有 mangakit 包）→ 必须落到真库
   （证明护栏没过度扩张，不会误伤正常使用）
3. 设了 `MANGAKIT_ALLOW_REAL_HOME=1` → 必须落到真库（留的逃生门有效）

再加一条结构检查（会失败的那种）：`MANGAKIT_HOME` 被设在 mangakit import
**之后**的脚本 —— 那是主动写错，会让配置先按真目录读进来。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKIP_DIR_PARTS = {"_incident", "_smoke", "__pycache__"}
_MK_IMPORT = re.compile(r"^\s*(?:from|import)\s+mangakit\b")
_HOME = "MANGAKIT_HOME"

PROBE = (
    "import sys\n"
    "from mangakit.config import app_dir, _resolve_home\n"
    "print(str(app_dir()))\n"
)


def _real_home() -> Path:
    """在「没有护栏」的前提下，真库应该在哪。"""
    env = dict(os.environ)
    env.pop("MANGAKIT_HOME", None)
    env["MANGAKIT_ALLOW_REAL_HOME"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c",
         "from mangakit.config import app_dir; print(str(app_dir()))"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"取真库路径失败：{out.stderr.strip()[:400]}")
    return Path(out.stdout.strip().splitlines()[-1])


def _run_probe(script_dir: Path, *, extra_env: dict[str, str] | None = None) -> Path:
    """把探针写进 script_dir，用子进程跑，返回它算出的 app_dir()。"""
    script_dir.mkdir(parents=True, exist_ok=True)
    probe = script_dir / "_iso_probe_tmp.py"
    probe.write_text(PROBE, encoding="utf-8")
    env = dict(os.environ)
    env.pop("MANGAKIT_HOME", None)
    env.pop("MANGAKIT_ALLOW_REAL_HOME", None)
    env["PYTHONPATH"] = str(ROOT)          # 无论探针在哪，都能 import 真 mangakit
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    try:
        out = subprocess.run([sys.executable, str(probe)], capture_output=True,
                             text=True, encoding="utf-8", env=env, timeout=120)
    finally:
        probe.unlink(missing_ok=True)
    if out.returncode != 0:
        raise RuntimeError(f"探针失败（{script_dir}）：{out.stderr.strip()[:400]}")
    return Path(out.stdout.strip().splitlines()[-1])


def _order_violations() -> list[tuple[Path, int, int]]:
    """找出「先 import mangakit、后设 MANGAKIT_HOME」的脚本。"""
    bad: list[tuple[Path, int, int]] = []
    for d in (ROOT / "tools",):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if any(part in SKIP_DIR_PARTS for part in p.parts):
                continue
            try:
                lines = p.read_text("utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            first_imp = next((i for i, ln in enumerate(lines, 1)
                              if _MK_IMPORT.match(ln)), None)
            if first_imp is None:
                continue
            at = next((i for i, ln in enumerate(lines, 1)
                       if _HOME in ln and not ln.lstrip().startswith("#")), None)
            if at is not None and at > first_imp:
                bad.append((p, at, first_imp))
    return bad


def main() -> int:
    fails: list[str] = []
    print(f"仓库根：{ROOT}")

    try:
        real = _real_home()
    except RuntimeError as exc:
        print(f"  [BAD] {exc}")
        return 1
    print(f"真库路径：{real}")

    # 1) 仓库 tools/ 下的脚本必须被隔离
    in_repo = _run_probe(ROOT / "tools")
    if in_repo == real:
        fails.append(f"仓库 tools/ 下的脚本没有隔离，仍指向真库 {in_repo}")
        print(f"  [BAD] tools/ 探针 → {in_repo}（应当被隔离）")
    else:
        print(f"  [ok ] tools/ 探针 → {in_repo}（已隔离）")

    # 2) 别处的 tools/ 不能被误伤
    with tempfile.TemporaryDirectory(prefix="iso_ext_") as td:
        outside = _run_probe(Path(td) / "tools")
    if outside != real:
        fails.append(f"外部 tools/ 被误隔离：{outside} != {real}")
        print(f"  [BAD] 外部 tools/ 探针 → {outside}（不该被隔离）")
    else:
        print(f"  [ok ] 外部 tools/ 探针 → {outside}（正常，未误伤）")

    # 3) 逃生门有效
    with tempfile.TemporaryDirectory(prefix="iso_esc_") as td:
        forced = _run_probe(ROOT / "tools",
                            extra_env={"MANGAKIT_ALLOW_REAL_HOME": "1"})
    if forced != real:
        fails.append(f"MANGAKIT_ALLOW_REAL_HOME=1 未生效：{forced} != {real}")
        print(f"  [BAD] 逃生门 → {forced}（应当是真库）")
    else:
        print(f"  [ok ] 逃生门 → {forced}（确认可连真库调试）")

    # 4) 结构检查：MANGAKIT_HOME 设在 import 之后
    for p, at, imp in _order_violations():
        fails.append(f"{p.relative_to(ROOT)}: MANGAKIT_HOME 第 {at} 行设在 "
                     f"第 {imp} 行 import 之后，来不及")
        print(f"  [BAD] {p.relative_to(ROOT)}: MANGAKIT_HOME 排在 import 之后")

    for msg in fails:
        print(f"  [BAD] {msg}")
    print("=== " + ("数据隔离护栏检查全部通过" if not fails
                    else f"数据隔离护栏有 {len(fails)} 项不通过") + " ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
