"""数据模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any


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


@dataclass
class FavoriteUpdate:
    """收藏里一本漫画的「有没有更新」检查结果。"""

    site: str
    url: str
    title: str
    latest_index: int = -1         # 源站当前最新章节序号
    latest_title: str = ""         # 该章标题，给用户看
    total: int = 0                 # 源站当前总章数
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