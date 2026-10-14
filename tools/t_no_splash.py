# -*- coding: utf-8 -*-
r"""启动窗口回归：exe 启动时不得出现 Tk 启动画面（v0.3.1 起）。

背景
----
v0.3.0 及更早的 exe 里，`mangakit.spec` 配了 PyInstaller 的 `Splash(...)`。
PyInstaller 在 Windows 上用 Tcl/Tk 实现 splash，所以双击后屏幕中间会先冒出
一个类名 `TkTopLevel`、标题 `tk` 的小窗口。而且它是**坏的**：spec 里写的
`text_font="Microsoft YaHei UI"` 带空格、模板又原样拼进 Tcl 脚本没加引号，
脚本在 `font create myFont -family Microsoft YaHei UI -size 10` 处抛
`bad option "YaHei"`，而负责去边框/居中的 `wm overrideredirect` / `wm geometry`
在脚本末尾才执行 —— 于是 Tk 退回默认窗口：216x239、带标题栏、内容空白，
横在主窗口前面约 5 秒。

v0.3.1 移除了 splash（见 `mangakit.spec` 里 `EXE()` 之前的长注释）。
本脚本把这个修复固化成回归项，三层检查：

  1. **静态**：用 `CArchiveReader` 读 exe 归档，splash 专属载荷必须全部消失
     （`Splash-00.res`、`tcl86t.dll`、`tk86t.dll`、`_tcl_data`、`_tk_data`）。
     `assets\splash.png` 仍应保留（随 datas 分发，不算失败）。
  2. **动态**：经 WMI 启动 exe（等价双击，`Start-Process` 会污染判定），
     50ms 轮询 `EnumWindows`，全程不得出现类名含 `Tk` 的窗口，
     且必须在 20 秒内出现主窗口（类名含 `Qt`）。
     打包后的 exe 不受 `config._dev_sandbox()` 保护（`sys.frozen` 直接放行），
     所以这里自己套一个临时 `.bat` 把 `MANGAKIT_HOME` 指到临时目录再启动，
     绝不碰用户真实数据库与日志。
  3. **日志**：这一次启动产生的日志里不得有 `pyi_splash` 的警告/traceback
     —— v0.3.1 曾漏掉这点（移除 Splash() 后 bootloader 不再设
     `_PYI_SPLASH_IPC`，而 `mangakit\main.py` 仍在 import pyi_splash，
     该模块在模块级就抛 KeyError 并往用户日志吐 warning + traceback）。
     细节见 `check_splash_log()` 的 docstring。

用法
----
    python tools\t_no_splash.py            # 用 dist\MangaKit.exe
    python tools\t_no_splash.py <exe 路径>

退出码 0 = 全部通过；1 = 有失败项。跑完会清掉自己拉起的 exe 进程。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "MangaKit.exe"

# splash 专属载荷：这些必须在 v0.3.1+ 的归档里彻底消失
MUST_BE_ABSENT = ("Splash-00.res", "tcl86t", "tk86t", "_tcl_data", "_tk_data", "_tkinter")
# 允许保留：splash.png 仍随 datas 打包（将来恢复/做皮肤用）
ALLOWED_PRESENT = ("assets\\splash.png",)

_OK = "[ok]  "
_BAD = "[FAIL]"


class _Results:
    def __init__(self) -> None:
        self.ok = True

    def check(self, cond: bool, label: str, detail: str = "") -> None:
        print(f"{_OK if cond else _BAD} {label}" + (f" —— {detail}" if detail else ""))
        self.ok &= bool(cond)


# ── Win32 ────────────────────────────────────────────────────────────────
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pids_of(exe_lower: str) -> set[int]:
    """当前所有「可见顶层窗口属于 exe_lower」的进程号。"""
    found: set[int] = set()
    buf = ctypes.create_unicode_buffer(1024)

    def cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if h:
            n = wintypes.DWORD(1024)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                if buf.value.lower() == exe_lower:
                    found.add(pid.value)
            _kernel32.CloseHandle(h)
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return found


def _windows_of(pids: set[int]) -> list[tuple[str, str, int, int]]:
    """(类名, 标题, 宽, 高)。"""
    out: list[tuple[str, str, int, int]] = []

    def cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, title, 512)
            r = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append((cls.value, title.value, r.right - r.left, r.bottom - r.top))
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return out


def check_archive(exe: Path, res: _Results) -> None:
    print("=== ① 归档检查：splash 载荷应已移除 ===")
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(exe))
    names = [str(n) for n in reader.toc.keys()]
    print(f"     CArchive 条目数：{len(names)}")

    for kw in MUST_BE_ABSENT:
        hits = [n for n in names if kw.lower() in n.lower()]
        res.check(not hits, f"归档里不应再有 {kw}", f"命中 {hits[:3]}" if hits else "")

    for kw in ALLOWED_PRESENT:
        hit = any(kw.lower() in n.lower() for n in names)
        res.check(hit, f"应保留 {kw}（随 datas 分发，不影响启动）")

    # 顺手确认没把 WebEngine 误伤（v0.2.0 起内置浏览器页要用）
    nw = sum(1 for n in names if "WebEngine" in n)
    res.check(nw > 0, "QtWebEngine 仍在包里（内置浏览器页依赖它）", f"{nw} 条相关条目")


def check_windows(exe: Path, res: _Results, timeout: float = 20.0) -> None:
    print("=== ② 动态检查：启动全程不得出现 Tk 窗口 ===")
    exe_lower = str(exe).lower()

    subprocess.run(["taskkill", "/F", "/IM", exe.name], capture_output=True)

    # ★ 打包后的 exe 不受 `config._dev_sandbox()` 保护（`sys.frozen` 直接放行），
    #   所以必须在这里显式把 MANGAKIT_HOME 指到临时目录 —— 否则这一轮观察就会
    #   拿用户真实数据库/日志开刀（2026-10-14 收藏清空事故的同类风险）。
    #   WMI 的子进程继承的是 WMI 服务的环境，拿不到我们的 env，因此套一层 .bat。
    home = tempfile.mkdtemp(prefix="mk_nosplash_")
    launcher = Path(home) / "_launch.bat"
    launcher.write_text(
        "@echo off\r\n" f'set "MANGAKIT_HOME={home}"\r\n' f'"{exe}"\r\n',
        encoding="gbk",
    )

    # 必须用 WMI 启动（等价双击）。Start-Process / & 在受限环境下行为不可信。
    subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-Command",
            f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{ CommandLine = '{launcher}' }} | Out-Null",
        ],
        capture_output=True,
    )
    print(f"     已在隔离数据目录下启动（MANGAKIT_HOME={home}）")

    t0 = time.time()
    first_tk: tuple[str, str, int, int] | None = None
    first_qt: tuple[float, str, int, int] | None = None
    seen_before_qt: list[str] = []

    while time.time() - t0 < timeout:
        pids = _pids_of(exe_lower)
        if pids:
            for cls, title, w, h in _windows_of(pids):
                if first_qt is None and "Qt" not in cls:
                    # 主窗口之前的「别的窗口」——理想情况下一律为空
                    seen_before_qt.append(f"{cls}({title!r} {w}x{h})")
                    if "Tk" in cls and first_tk is None:
                        first_tk = (cls, title, w, h)
                if "Qt" in cls and first_qt is None:
                    first_qt = (time.time() - t0, title, w, h)
        if first_qt is not None and time.time() - t0 > first_qt[0] + 1.5:
            break
        time.sleep(0.05)

    elapsed = time.time() - t0

    res.check(first_tk is None, "启动全程没有任何 Tk 窗口（splash 已移除）",
              f"却发现 {first_tk}" if first_tk else f"观察 {elapsed:.1f}s")
    res.check(first_qt is not None, "主窗口（Qt）正常出现",
              f"{first_qt[0]:.2f}s 标题={first_qt[1]!r} {first_qt[2]}x{first_qt[3]}"
              if first_qt else f"{timeout:.0f}s 内没等到")
    if first_qt:
        res.check(first_qt[0] < 15.0, "主窗口出现时间在合理范围（< 15s，单文件 exe 要解包）",
                  f"{first_qt[0]:.2f}s")

    print(f"     主窗口之前出现过的窗口：{seen_before_qt or '（一个都没有，正是期望结果）'}")

    # 收尾：别把测试起的 exe 留在后台
    subprocess.run(["taskkill", "/F", "/IM", exe.name], capture_output=True)
    time.sleep(0.6)  # 让被杀的进程把日志刷完

    check_splash_log(home, res)


def check_splash_log(home: str, res: _Results) -> None:
    """③ 日志检查：不得出现 pyi_splash 的警告 / traceback。

    背景（v0.3.1 引入、v0.3.2 修掉）：spec 里移除 Splash() 之后，bootloader
    就不再设 `_PYI_SPLASH_IPC` 了。而 `mangakit\\main.py` 的启动流程仍会
    `import pyi_splash` 想更新「正在加载界面…」这几个字 ——
    那个模块**在 import 那一刻**就读 `os.environ['_PYI_SPLASH_IPC']`，
    读不到就往日志里写一条 warning **外加 traceback**：

        [WARNING] pyi_splash  The environment does not allow connecting to
                             the splash screen. Did bootloader fail to
                             initialize it?
        Traceback (most recent call last):
          File "pyi_splash.py", line 108, in <module>
          KeyError: '_PYI_SPLASH_IPC'

    副作用发生在模块级，`main.py` 里函数内的 try/except 拦不住，所以每次
    启动都会往用户真实日志里白刷一遍。修法是先看环境变量再决定 import
    （`mangakit\\main.py:_splash_available()`）——这条断言就是防它被改回去。
    """
    print("=== ③ 日志检查：不应有 pyi_splash 警告 ===")
    log = Path(home) / "logs" / "mangakit.log"
    if not log.is_file():
        res.check(True, "本轮未产生日志文件（跳过检查）", f"没有 {log}")
        return
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        res.check(False, "能读日志文件", f"{exc}")
        return

    hits = [
        ln for ln in text.splitlines()
        if "pyi_splash" in ln or "_PYI_SPLASH_IPC" in ln
    ]
    res.check(
        not hits,
        "启动日志里没有 pyi_splash 警告（_splash_available() 守卫生效）",
        f"命中 {len(hits)} 行，首行：{hits[0].strip()[:110]!r}" if hits
        else f"日志 {len(text.splitlines())} 行，无 pyi_splash 相关记录",
    )

    # 主窗口确实起来了 —— 顺便确认日志不是空的（否则上面的「没有命中」没意义）
    res.check(
        "main window shown" in text or "下载管理器已启动" in text,
        "日志里确有正常启动记录（证明日志真的被写入过）",
        "找到启动痕迹" if ("main window shown" in text or "下载管理器已启动" in text)
        else "日志里连启动记录都没有，上面的断言不算数",
    )


def main(argv: list[str]) -> int:
    exe = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_EXE
    print(f"目标 exe：{exe}")
    if not exe.is_file():
        print(f"{_BAD} 找不到 exe：{exe}")
        return 1

    vi = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         f"(Get-Item '{exe}').VersionInfo.FileVersion"],
        capture_output=True, text=True,
    ).stdout.strip()
    print(f"FileVersion：{vi}   大小：{exe.stat().st_size:,} 字节\n")

    res = _Results()
    check_archive(exe, res)
    print()
    check_windows(exe, res)

    print()
    if res.ok:
        print("=== 启动窗口回归全部通过（没有 tk 启动画面）===")
        return 0
    print("=== 有失败项，见上面的 [FAIL] ===")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
