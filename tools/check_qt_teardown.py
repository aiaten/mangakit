r"""护栏检查：每个会建 Qt 对象的测试脚本都必须按序收尾，否则退出时会弹窗。

背景（用户实际报的 bug）
------------------------
跑 `tools\t_fav_button.py`、`tools\t_gui_new_pages.py` 这类脚本，断言全过之后
Windows 会弹：

    python.exe - 应用程序错误
    0x00007FFC8E0AC91E 指令引用了 0x0000000000000208 内存。
    该内存不能为 read。

进程退出码 0xC0000005（访问冲突）。用户原话是「修复他不要再弹出来了」。
根因有两条，都在 `tools\qt_teardown.py` 的模块注释里写清楚了：

  1. `QThreadPool` 里的取封面线程没收工，QApplication 就先被销毁了；
  2. `deleteLater()` 投递的 DeferredDelete 事件**不会**被 `processEvents()`
     处理，窗口和 QWebEngineView 一直活到解释器退出，Qt profile 先释放、
     WebEnginePage 后析构。

本检查盯三件事（都是静态可判定的，所以能做成硬门禁）：

  A. 任何 import 了 `PySide6.QtWidgets`（QApplication 就在里面）的脚本，
     必须引用 `qt_teardown`。漏了收尾 = 随时可能弹窗。
  B. 不许再出现 `os._exit(`。历史上有人（包括我）拿它绕「解释器关闭阶段
     析构竞态」，实测崩溃率从偶发 1/10 升到 9/10 —— 它是**加重**问题的写法。
  C. 不许出现 `sendPostedEvents(None, 0)`。`0` 不是 DeferredDelete（那是 52），
     而是「所有事件」；这么写等于没删掉 WebEngineView，实测必崩。

A 为什么要按「import QtWidgets」判，而不是按「建了 QApplication」判：
建 QApplication 的写法有 `QApplication([])`、`QApplication.instance() or
QApplication(sys.argv)` 好几种，还可能在子进程字符串里（`gui_smoke.py` 就是
用 `-c` 起子进程，主进程一行 QApplication 都没有）。而「import 了 QtWidgets」
是必要前提 —— 没它连 QApplication 都拿不到。宁可多要求一个脚本写收尾
（多写一行 `teardown()` 无害），也不要漏掉一个。
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 这些文件本身不参与检查：
#   qt_teardown.py —— 它是收尾实现，里面当然会写 QApplication / sendPostedEvents；
#   check_qt_teardown.py —— 本文件，判据是字面量，扫自己必然自报。
_SELF = {"qt_teardown.py", "check_qt_teardown.py"}

# 已确认「import 了 QtWidgets 但故意不收尾」的例外。目前一个都没有 ——
# 真要加，必须在注释里写清楚为什么它不可能弹窗（例如全程在子进程里跑、
# 主进程根本不建 QApplication）。
_ALLOW_NO_TEARDOWN: dict[str, str] = {}

_QT_IMPORT = re.compile(r"^\s*(?:from|import)\s+PySide6\.QtWidgets\b", re.M)
_OS_EXIT = re.compile(r"\bos\._exit\s*\(")
# sendPostedEvents(x, 0) / sendPostedEvents(None, 0x0) —— 0 是「所有事件」
_SEND_ALL = re.compile(r"sendPostedEvents\s*\(\s*[^,)]*,\s*(?:0|0x0)\s*\)")


def _strip_comments(src: str) -> str:
    """把注释和文档字符串抹成空白，保留其余代码的原始排版。

    必须这么做：`t_gui_new_pages.py` 的注释里就写着「曾经这里用 os._exit(_code)
    绕过…那是错的」，`qt_teardown.py` 的文档也提到 os._exit。直接把注释算进来
    会把「教训记录」误判成「还在犯」。

    做法是**原地抹白**而不是重新拼 token：重新拼会把 `manager().stop()` 拆成
    带换行的碎片，子串检查就再也匹配不上了（第一版踩过）。
    """
    lines = src.splitlines(keepends=True)
    blank: list[tuple[int, int, int]] = []      # (行号, 起始列, 结束列)，0 起
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src          # 解析失败就别过滤，宁可多报也别漏报

    for i, tok in enumerate(toks):
        if tok.type == tokenize.COMMENT:
            blank.append((tok.start[0], tok.start[1], tok.end[1]))
            continue
        if tok.type != tokenize.STRING:
            continue
        # 只抹「独占一行」的字符串（文档字符串）；判据是前后都是行边界，
        # 这样 `"MangaKit.exe" in src` 这类判据字面量会被保留。
        prev = next((t for t in reversed(toks[:i])
                     if t.type not in (tokenize.NL, tokenize.NEWLINE,
                                       tokenize.INDENT, tokenize.DEDENT)), None)
        nxt = next((t for t in toks[i + 1:]
                    if t.type not in (tokenize.NL, tokenize.NEWLINE,
                                      tokenize.INDENT, tokenize.DEDENT)), None)
        starts_line = prev is None or prev.end[0] < tok.start[0]
        ends_line = nxt is None or nxt.start[0] > tok.end[0]
        if starts_line and ends_line:
            if tok.start[0] == tok.end[0]:
                blank.append((tok.start[0], tok.start[1], tok.end[1]))
            else:                      # 跨行 docstring：整段抹白
                for ln in range(tok.start[0], tok.end[0] + 1):
                    s = tok.start[1] if ln == tok.start[0] else 0
                    e = tok.end[1] if ln == tok.end[0] else len(lines[ln - 1].rstrip("\r\n"))
                    blank.append((ln, s, e))

    for ln, s, e in blank:
        if 1 <= ln <= len(lines):
            body = lines[ln - 1]
            lines[ln - 1] = body[:s] + " " * max(0, e - s) + body[e:]
    return "".join(lines)



def _line_of(src: str, pat: re.Pattern[str]) -> int | None:
    """在原文里找第一个非注释命中，返回行号（1 起）。"""
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split("#", 1)[0]
        if pat.search(code):
            return i
    return None



def _scan() -> tuple[list[tuple[Path, str]], list[tuple[Path, int, str]]]:
    """返回 (缺收尾的脚本, 危险写法)。"""
    missing: list[tuple[Path, str]] = []
    danger: list[tuple[Path, int, str]] = []

    for p in sorted(TOOLS.glob("*.py")):
        if p.name in _SELF:
            continue
        try:
            src = p.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        code = _strip_comments(src)

        if _QT_IMPORT.search(code) and "qt_teardown" not in code:
            if p.name not in _ALLOW_NO_TEARDOWN:
                missing.append((p, "import 了 PySide6.QtWidgets 却没引用 qt_teardown"))

        if _OS_EXIT.search(code):
            line = _line_of(src, _OS_EXIT) or 0
            danger.append((p, line, "os._exit() —— 跳过 Python 清理会让崩溃率更高"))

        if _SEND_ALL.search(code):
            line = _line_of(src, _SEND_ALL) or 0
            danger.append((p, line,
                           "sendPostedEvents(x, 0) —— 0 是「所有事件」，"
                           "DeferredDelete 是 52"))

    return missing, danger


def main() -> int:
    print(f"仓库根：{ROOT}")
    missing, danger = _scan()

    for p, why in missing:
        print(f"  [BAD] {p.relative_to(ROOT)}: {why}")
    for p, line, why in danger:
        print(f"  [BAD] {p.relative_to(ROOT)}:{line}: {why}")

    # 正向确认：收尾实现本身必须把两件事都做到（否则上面全过也没意义）
    impl = _strip_comments((TOOLS / "qt_teardown.py").read_text("utf-8"))
    checks = [
        ("qt_teardown.py 存在", (TOOLS / "qt_teardown.py").is_file()),
        ("qt_teardown 里 waitForDone 等线程收工", "waitForDone" in impl),
        ("qt_teardown 里显式发 DeferredDelete",
         "DeferredDelete" in impl and "sendPostedEvents" in impl),
        ("qt_teardown 里停下载管理器", "manager().stop()" in impl),
        ("qt_teardown 里没写 os._exit", not _OS_EXIT.search(impl)),
    ]
    for label, ok in checks:
        print(f"  [{'ok ' if ok else 'BAD'}] {label}")
    bad = len(missing) + len(danger) + sum(1 for _l, ok in checks if not ok)

    n_qt = 0
    for p in TOOLS.glob("*.py"):
        if p.name in _SELF:
            continue
        try:
            if _QT_IMPORT.search(_strip_comments(p.read_text("utf-8"))):
                n_qt += 1
        except (OSError, UnicodeDecodeError):
            continue
    if not missing and not danger:
        print(f"  [ok ] {n_qt} 个 Qt 脚本都已按序收尾")

    print("=== " + ("Qt 收尾护栏检查全部通过" if not bad
                    else f"Qt 收尾护栏有 {bad} 项不通过") + " ===")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
