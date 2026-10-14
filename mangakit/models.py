"""数据模型。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

# 从章节标题里抠「话号」。**只认明确的写法**，不用「标题里任意数字」这种
# 宽松规则 —— 否则「2026新年贺图」会被当成第 2026 话，最新章节直接跳到 2026。
# 顺序有意义：先匹配最具体的，逐个退让。
_CH_NO_RX = (
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[话話回]"),          # 第12话 / 第 12 話
    re.compile(r"[Cc]hapter\s*(\d+(?:\.\d+)?)"),             # Chapter 12
    re.compile(r"(?:^|\D)(\d+(?:\.\d+)?)\s*[话話回]\s*$"),   # 12话（结尾处）
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[章集卷篇]"),         # 第12章 / 第3卷
)


@dataclass
class Chapter:
    """一个可下载的章节（对应 MangaCon 的 volume）。"""

    cid: str
    title: str
    url: str
    index: int = 0                 # 排序用序号
    pages: int = 0                 # 图片页数（解析后回填）
    volume: str = ""               # 所属卷/话号文本
    status: str = "pending"        # pending | running | done | error | skipped
    pinned: bool = False

    @property
    def display(self) -> str:
        return self.title or self.cid

    @property
    def no(self) -> int:
        """这一话的「话号」；从标题/卷名里抠不到就返回 -1。"""
        for text in (self.title or "", self.volume or ""):
            for rx in _CH_NO_RX:
                m = rx.search(text)
                if m:
                    try:
                        return int(float(m.group(1)))
                    except ValueError:
                        continue
        return -1


def latest_chapter(chs: list["Chapter"]) -> "Chapter":
    """挑出「最新章节」= 话号最大那一话。

    为什么不能用 `max(chs, key=lambda c: c.index)`：index 是**列表位置**，
    而不少站点的话目是倒序排的（列表第一条就是最新话）。按位置取最大就取到了
    列表末尾，也就是**第 1 话** —— 检查完「最新章节」变成第一章就是这么来的。

    话号抠不出来时（例如标题里只有「序章」「番外」）退回到位置最大的那个，
    但这种退化只在**过半数章节都抠不出话号**时才整体采用，避免一个站的章节
    里只有少数几话带话号时，拿残缺的话号去比较。
    """
    if not chs:
        raise ValueError("latest_chapter: 章节列表为空")
    best_by_pos = max(chs, key=lambda c: c.index)
    got = [c for c in chs if c.no >= 0]
    # 过半能认出话号才信话号，否则整站退回按位置
    if len(got) * 2 <= len(chs):
        return best_by_pos
    return max(got, key=lambda c: c.no)


@dataclass
class FavoriteUpdate:
    """收藏里一本漫画的「有没有更新」检查结果。"""

    site: str
    url: str
    title: str
    latest_index: int = -1         # 当前位置序号（列表位置，0 起）
    latest_no: int = -1            # 最新话的「话号」（从标题解析，解析不到为 -1）
    latest_title: str = ""         # 该章标题，给用户看
    total: int = 0                 # 源站当前总章数
    prev_total: int = -1           # 上次检查时的总章数（-1 = 第一次，没有基准）
    prev_no: int = -1              # 上次检查时的话号（-1 = 没有基准）
    by: str = ""                   # 这次怎么比出来的：count | no | pos
    new_count: int = 0             # 比上次多出来几话
    status: str = "ok"             # ok | new | first | error | unsupported
    message: str = ""              # 失败原因 / 备注

    @property
    def has_update(self) -> bool:
        return self.status == "new" and self.new_count > 0

    @property
    def display(self) -> str:
        if self.status == "new":
            return f"有 {self.new_count} 话更新"
        if self.status == "first":
            return "已记录当前进度"
        if self.status == "unsupported":
            return "不支持检查"
        if self.status == "error":
            return "检查失败"
        return "无更新"


@dataclass
class Comic:
    """一本漫画。"""

    title: str
    url: str
    site: str = ""
    site_name: str = ""
    cid: str = ""
    author: str = ""
    cover: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = "unknown"        # ongoing | finished | unknown
    description: str = ""
    chapters: list[Chapter] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Page:
    """章节内的一页。"""

    index: int
    url: str
    data: bytes | None = None
    path: str = ""                 # 落盘路径
    status: str = "pending"
    error: str = ""


@dataclass
class Task:
    """一个下载任务 = 一章。

    字段与 SQLite 的 tasks 表一一对应，`to_dict()` 可直接落库。
    """

    tid: str
    comic: str                     # 漫画标题（用于路径）
    chapter: str                   # 章节标题文本
    folder: str = ""               # 目标目录
    status: str = "pending"        # queued | running | done | error | paused | skipped
    total: int = 0
    finished: int = 0
    error: str = ""
    url: str = ""                  # 章节页 URL（断点续传靠它）
    site: str = ""                 # 站点 id
    chapter_id: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    speed: float = 0.0             # 页/秒
    eta: float = 0.0               # 预计剩余秒数

    @property
    def progress(self) -> int:
        return int(self.finished * 100 / self.total) if self.total else 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("progress", None)
        d["progress"] = self.progress
        return d