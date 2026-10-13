"""在旧 exe 上真机验证：搜索后双击是否有反应。

用 Win32 UI Automation 太脆弱，改成：启动 exe，用键盘/鼠标脚本操作不可行。
改为直接看 exe 启动后日志 + 让用户复测。

本脚本只做一件事：把 dist/MangaKit.exe 与源码的构建时序核对清楚，
并输出结论，避免再靠猜。
用法：python tools/t_exe_ver.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WATCH = [
    ROOT / "dist" / "MangaKit.exe",
    ROOT / "mangakit" / "gui" / "main_window.py",
    ROOT / "mangakit" / "gui" / "bridge.py",
    ROOT / "mangakit" / "service.py",
    ROOT / "mangakit" / "models.py",
]


def main() -> int:
    print("=== 构建时序核对 ===")
    exe = WATCH[0]
    if not exe.exists():
        print("exe 不存在")
        return 1
    et = datetime.fromtimestamp(exe.stat().st_mtime)
    print(f"exe            {et:%Y-%m-%d %H:%M:%S}  {exe.stat().st_size:,} bytes")
    newest = et
    for p in WATCH[1:]:
        if not p.exists():
            continue
        t = datetime.fromtimestamp(p.stat().st_mtime)
        flag = "  <<< 比 exe 新，exe 里没有这次改动！" if t > et else ""
        print(f"{p.name:22s} {t:%Y-%m-%d %H:%M:%S}{flag}")
        newest = max(newest, t)

    print()
    if newest > et:
        print("结论：exe 落后于源码，必须重新打包才能复现/验证最新修复。")
    else:
        print("结论：exe 不比源码旧。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
