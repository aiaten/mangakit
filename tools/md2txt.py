"""把 使用文档.md 转成纯文本 txt，去掉 Markdown 语法噪音，保持可读性。

处理：
  - 标题：去掉 #，用 === / --- 下划线或全角编号
  - 表格：保留对齐（按列宽补空格），去掉分隔行
  - 代码块：去掉 ``` 围栏，保留内容
  - 行内：**粗体**、`code`、[文字](链接) → 纯文字
  - 引用 > → 缩进
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "使用文档.md"
DST = Path(__file__).resolve().parents[1] / "使用文档.txt"
WIDTH = 100  # 正文折行宽度（东亚洲字符算 2）；表格可略宽


def dwidth(s: str) -> int:
    """显示宽度：CJK 算 2，其余算 1。"""
    return sum(2 if ord(c) > 0x1100 and is_wide(c) else 1 for c in s)


def is_wide(c: str) -> bool:
    o = ord(c)
    return (
        0x1100 <= o <= 0x115F
        or 0x2E80 <= o <= 0xA4CF
        or 0xAC00 <= o <= 0xD7A3
        or 0xF900 <= o <= 0xFAFF
        or 0xFE30 <= o <= 0xFE6F
        or 0xFF00 <= o <= 0xFF60
        or 0xFFE0 <= o <= 0xFFE6
        or 0x20000 <= o <= 0x3FFFD
    )


def pad(s: str, w: int) -> str:
    return s + " " * max(0, w - dwidth(s))


def strip_inline(s: str) -> str:
    # 粗体/斜体可能跨行（源文件里一处 **…** 被换行劈开了），
    # 但行内代码与链接仍按行处理更安全（避免把代码块里的引号吃掉）。
    s = re.sub(r"\*\*\s*(.+?)\s*\*\*", r"\1", s, flags=re.S)   # **bold**（可跨行）
    s = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", s)          # [text](url) -> text
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)        # *italic*
    s = re.sub(r"`([^`\n]+)`", r"\1", s)                       # `code`
    s = re.sub(r"~~([^~\n]+)~~", r"\1", s)                     # ~~strike~~
    s = s.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    # 跨行合并后可能留下多余空格
    return re.sub(r"[ \t]{2,}", " ", s).rstrip()


def wrap_cell(s: str, w: int) -> list[str]:
    """把一格内容按显示宽度折成多行；优先在空格/标点处断开，避免把数字和单位劈开。"""
    if dwidth(s) <= w:
        return [s]
    # 断点候选：空格之后，或全角标点/斜杠/逗号之后
    breakable = " \t/、，,；;：:）)（(】]}-"
    rows: list[str] = []
    cur = ""
    last_break = -1  # cur 中最后一个可断点的下标（断在它之后）
    for ch in s:
        if dwidth(cur + ch) > w:
            if last_break > 0:
                rows.append(cur[:last_break].rstrip())
                cur = cur[last_break:] + ch
            else:
                rows.append(cur)
                cur = ch
            last_break = -1
            # 重算当前行的断点
            for idx in range(len(cur) - 1, 0, -1):
                if cur[idx - 1] in breakable:
                    last_break = idx
                    break
        else:
            cur += ch
            if ch in breakable:
                last_break = len(cur)
    if cur:
        rows.append(cur)
    return rows


def emit_table(buf: list[str], rows: list[list[str]]) -> None:
    rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c or "---") for c in r)]
    if not rows:
        return
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    rows = [[strip_inline(c) for c in r] for r in rows]
    widths = [max(dwidth(r[i]) for r in rows) for i in range(ncol)]

    # 总宽超了就压最宽的列；压到下限还超，就允许横向溢出（宁可宽也不要丢字）
    sep = 2 * (ncol - 1)
    avail = WIDTH + 20 - 2 - sep
    while sum(widths) > avail:
        j = max(range(ncol), key=lambda k: widths[k])
        if widths[j] <= 16:
            break
        widths[j] -= 1

    for i, r in enumerate(rows):
        # 每格折行，按最长的那格决定这一行占几行
        wrapped = [wrap_cell(r[j], widths[j]) for j in range(ncol)]
        nlines = max(len(c) for c in wrapped)
        for k in range(nlines):
            cells = []
            for j in range(ncol):
                part = wrapped[j][k] if k < len(wrapped[j]) else ""
                cells.append(pad(part, widths[j]))
            buf.append("  " + "  ".join(cells).rstrip())
        if i == 0:
            total = sum(widths) + sep
            buf.append("  " + "-" * min(total, WIDTH + 20))
    buf.append("")


def join_bold_lines(text: str) -> str:
    """源文件里个别 **粗体** 被换行劈成两行，先粘回去再转换。

    只粘「普通段落行」，绝不碰表格行（以 | 开头）、代码块内、标题行 ——
    否则表格会被并入上一段，整张表塌成一行。
    """
    out: list[str] = []
    in_code = False
    for ln in text.splitlines():
        if ln.startswith("```"):
            in_code = not in_code
            out.append(ln)
            continue
        # 上一行有落单的 ** 且本行以 ** 收尾，且两行都不是表格/标题 → 合并
        if (
            out
            and not in_code
            and out[-1].count("**") % 2 == 1
            and "**" in ln
            and not out[-1].lstrip().startswith(("|", "#"))
            and not ln.lstrip().startswith("|")
        ):
            out[-1] = out[-1].rstrip() + ln.strip()
            continue
        out.append(ln)
    return "\n".join(out)


def convert(text: str) -> str:
    text = join_bold_lines(text)
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    in_code = False
    code_lang = ""
    pending_table: list[list[str]] = []

    def flush_table() -> None:
        nonlocal pending_table
        if pending_table:
            emit_table(out, pending_table)
            pending_table = []

    while i < len(lines):
        ln = lines[i]

        # 代码块
        if ln.startswith("```"):
            flush_table()
            if not in_code:
                in_code = True
                code_lang = ln[3:].strip()
                out.append("  " + "┄" * 60)
            else:
                in_code = False
                out.append("  " + "┄" * 60)
                out.append("")
            i += 1
            continue
        if in_code:
            out.append("    " + ln if ln.strip() else "")
            i += 1
            continue

        s = ln.rstrip()

        # 表格
        if s.lstrip().startswith("|"):
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            pending_table.append(cells)
            i += 1
            continue
        flush_table()

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl, title = len(m.group(1)), strip_inline(m.group(2))
            if lvl == 1:
                out.append("=" * WIDTH)
                out.append(title.center(WIDTH - 2).rstrip())
                out.append("=" * WIDTH)
            elif lvl == 2:
                out.append("")
                out.append(title)
                out.append("=" * dwidth(title))
            else:
                out.append("")
                out.append(title)
                out.append("-" * dwidth(title))
            out.append("")
            i += 1
            continue

        # 水平线
        if re.fullmatch(r"\s*[-*_]{3,}\s*", s):
            out.append("-" * WIDTH)
            i += 1
            continue

        # 引用
        m = re.match(r"^(\s*)>\s?(.*)$", s)
        if m:
            body = strip_inline(m.group(2))
            if body:
                out.append("    | " + body)
            i += 1
            continue

        # 列表
        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", s)
        if m:
            indent, mark, body = m.group(1), m.group(2), strip_inline(m.group(3))
            bullet = "· " if mark in "-*+" else mark + " "
            out.append(indent + "  " + bullet + body)
            i += 1
            continue

        out.append(strip_inline(s) if s else "")
        i += 1

    flush_table()

    # 压掉连续空行
    res: list[str] = []
    for ln in out:
        if ln == "" and res and res[-1] == "":
            continue
        res.append(ln)
    while res and res[0] == "":
        res.pop(0)
    while res and res[-1] == "":
        res.pop()
    return "\n".join(res) + "\n"


def main() -> int:
    raw = SRC.read_text(encoding="utf-8")
    txt = convert(raw)
    # 加一行来源说明
    head = (
        "漫卷 MangaKit 使用文档（纯文本版）\n"
        f"本文由 {SRC.name} 自动转换生成，内容以该文件为准。\n"
        "\n"
    )
    DST.write_text(head + txt, encoding="utf-8-sig")  # BOM 便于 Windows 记事本识别 UTF-8
    n = len(txt.splitlines())
    print(f"已生成 {DST}")
    print(f"  源: {SRC.stat().st_size} bytes / {len(raw.splitlines())} 行")
    print(f"  出: {DST.stat().st_size} bytes / {n} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
