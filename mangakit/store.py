"""持久化：SQLite 存任务、章节进度、收藏、站点状态。

任务必须跨进程存活 —— 软件被强杀后重启能接着下。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .config import app_dir

# 从 SQL 文本里粗提取 `:name` 形式的命名参数
_SQL_COLUMNS = re.compile(r":(\w+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    tid       TEXT PRIMARY KEY,
    comic     TEXT NOT NULL,
    chapter   TEXT NOT NULL,
    folder    TEXT DEFAULT '',
    status    TEXT DEFAULT 'pending',
    total     INTEGER DEFAULT 0,
    finished  INTEGER DEFAULT 0,
    error     TEXT DEFAULT '',
    url       TEXT DEFAULT '',
    site      TEXT DEFAULT '',
    chapter_id TEXT DEFAULT '',
    created   REAL,
    started   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS chapters (
    site       TEXT NOT NULL,
    cid        TEXT NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT,
    idx        INTEGER DEFAULT 0,
    pages      INTEGER DEFAULT 0,
    volume     TEXT DEFAULT '',
    status     TEXT DEFAULT 'pending',
    PRIMARY KEY (site, cid)
);

CREATE TABLE IF NOT EXISTS favorites (
    site       TEXT NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL,
    author     TEXT DEFAULT '',
    cover      TEXT DEFAULT '',
    tags       TEXT DEFAULT '[]',
    last_check REAL DEFAULT 0,
    last_index INTEGER DEFAULT -1,
    PRIMARY KEY (site, url)
);

CREATE TABLE IF NOT EXISTS site_state (
    site      TEXT PRIMARY KEY,
    checked   REAL,
    enabled   INTEGER DEFAULT 1,
    note      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Store:
    """线程安全的 SQLite 封装。每个线程一个连接。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "mangakit.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 显式指定路径时父目录可能还不存在
        self._local = threading.local()
        self._wlock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    # ---- 连接管理 ----
    def _connect(self) -> sqlite3.Connection:
        try:
            con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            return con
        except sqlite3.OperationalError as e:
            # 典型于「数据目录所在分区不支持 WAL」（某些网络盘/受控环境会报
            # unable to open database file），退回默认 rollback 日志再试一次。
            if "unable to open database file" not in str(e) and "disk I/O" not in str(e):
                raise
            con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA synchronous=NORMAL")
            return con

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "con", None)
        if c is None:
            c = self._connect()
            self._local.con = c
        return c

    # ---- 通用 ----
    def execute(self, sql: str, args: Iterable | dict[str, Any] = ()) -> sqlite3.Cursor:
        """执行一条语句。args 支持 dict（命名参数，多余键无害）或位置参数序列。"""
        with self._wlock:
            con = self.conn()
            if isinstance(args, dict):
                # 命名参数：只保留 SQL 里出现的键，避免 dataclass 多字段报错
                used = set(_SQL_COLUMNS.findall(sql))
                params = {k: v for k, v in args.items() if not used or k in used}
                cur = con.execute(sql, params)
            else:
                cur = con.execute(sql, tuple(args))
            con.commit()
            return cur

    def query(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        return list(self.conn().execute(sql, tuple(args)).fetchall())

    def one(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, tuple(args)).fetchone()

    # ---- kv ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT v FROM kv WHERE k=?", (key,))
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except Exception:
            return r["v"]

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---- 任务 ----
    @staticmethod
    def _as_task_dict(t: Any) -> dict[str, Any]:
        if isinstance(t, dict):
            return t
        if hasattr(t, "to_dict"):
            return t.to_dict()
        return dict(getattr(t, "__dict__", {}))

    def upsert_task(self, t: Any) -> None:
        d = self._as_task_dict(t)
        self.execute(
            """INSERT INTO tasks(tid,comic,chapter,folder,status,total,finished,error,url,site,chapter_id,created,started)
               VALUES(:tid,:comic,:chapter,:folder,:status,:total,:finished,:error,:url,:site,:chapter_id,:created,:started)
               ON CONFLICT(tid) DO UPDATE SET
                 comic=excluded.comic, chapter=excluded.chapter, folder=excluded.folder,
                 status=excluded.status, total=excluded.total, finished=excluded.finished,
                 error=excluded.error, started=excluded.started""",
            d,
        )

    def update_task(self, task: Any, **fields: Any) -> None:
        """既支持 update_task(Task 对象)，也支持 update_task(tid, status=..., ...)。"""
        if isinstance(task, str):
            tid = task
        else:
            d = self._as_task_dict(task)
            tid = d.get("tid", "")
            for k in ("total", "finished", "status", "error", "folder", "started"):
                if k in d:
                    fields.setdefault(k, d[k])
        if not tid:
            return
        if not fields:
            return
        allowed = {"comic", "chapter", "folder", "status", "total", "finished",
                   "error", "url", "site", "chapter_id", "created", "started"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE tasks SET {sets} WHERE tid=?", (*fields.values(), tid))

    def active_tasks(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM tasks WHERE status IN ('pending','running','paused') ORDER BY created"
        )

    def all_tasks(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,))

    def delete_task(self, tid: str) -> None:
        self.execute("DELETE FROM tasks WHERE tid=?", (tid,))

    def clear_finished(self) -> None:
        self.execute("DELETE FROM tasks WHERE status IN ('done','skipped')")

    # ---- 章节 ----
    def save_chapters(self, site: str, chapters: list[dict[str, Any]]) -> None:
        if not chapters:
            return
        with self._wlock:
            con = self.conn()
            con.executemany(
                """INSERT INTO chapters(site,cid,url,title,idx,pages,volume,status)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(site,cid) DO UPDATE SET
                     url=excluded.url, title=excluded.title, idx=excluded.idx,
                     pages=excluded.pages, volume=excluded.volume""",
                [
                    (site, c["cid"], c["url"], c.get("title", ""), c.get("index", 0),
                     c.get("pages", 0), c.get("volume", ""), c.get("status", "pending"))
                    for c in chapters
                ],
            )
            con.commit()

    def get_chapters(self, site: str, cid: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM chapters WHERE site=? AND cid=? ORDER BY idx", (site, cid)
        )

    def set_chapter_status(self, site: str, cid: str, status: str, pages: int = 0) -> None:
        if pages:
            self.execute(
                "UPDATE chapters SET status=?, pages=? WHERE site=? AND cid=?",
                (status, pages, site, cid),
            )
        else:
            self.execute(
                "UPDATE chapters SET status=? WHERE site=? AND cid=?", (status, site, cid)
            )

    # ---- 收藏 ----
    def add_favorite(self, f: Any, url: str = "", site: str = "", title: str = "",
                     tags: list[str] | None = None, **extra: Any) -> None:
        """既支持 add_favorite(dict)，也支持 add_favorite(title, url, site, tags=[...])。"""
        if not isinstance(f, dict):
            f = {"title": str(f), "url": url, "site": site, "tags": tags or [], **extra}
        self.execute(
            """INSERT INTO favorites(site,url,title,author,cover,tags,last_check)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(site,url) DO UPDATE SET
                 title=excluded.title, author=excluded.author, cover=excluded.cover, tags=excluded.tags""",
            (f["site"], f["url"], f["title"], f.get("author", ""), f.get("cover", ""),
             json.dumps(f.get("tags", []), ensure_ascii=False), time.time()),
        )

    def remove_favorite(self, site: str, url: str) -> None:
        self.execute("DELETE FROM favorites WHERE site=? AND url=?", (site, url))

    def remove_favorites(self, items: list[tuple[str, str]]) -> int:
        """批量删除收藏，返回实际删除条数。"""
        n = 0
        for site, url in items:
            cur = self.execute("DELETE FROM favorites WHERE site=? AND url=?", (site, url))
            n += cur.rowcount if cur.rowcount > 0 else 0
        return n

    def clear_favorites(self) -> int:
        """清空全部收藏，返回删除条数。"""
        cur = self.execute("DELETE FROM favorites")
        return cur.rowcount if cur.rowcount > 0 else 0

    def favorites(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM favorites ORDER BY title")

    def favorite_exists(self, site: str, url: str) -> bool:
        return self.one("SELECT 1 FROM favorites WHERE site=? AND url=?", (site, url)) is not None

    # ---- 收藏：更新检查 ----
    def mark_favorite_checked(self, site: str, url: str, latest_index: int | None = None) -> None:
        """记录一次更新检查。latest_index 给出时同时更新「上次见到的章节序号」。

        latest_index 传 None 表示只是记个检查时间（例如检查失败、或该站拿不到话号）。
        """
        if latest_index is None:
            self.execute(
                "UPDATE favorites SET last_check=? WHERE site=? AND url=?",
                (time.time(), site, url),
            )
        else:
            self.execute(
                "UPDATE favorites SET last_check=?, last_index=? WHERE site=? AND url=?",
                (time.time(), int(latest_index), site, url),
            )

    def set_favorite_baseline(self, site: str, url: str, latest_index: int) -> None:
        """把当前最新章节记为基线（用户点「从现在开始」时用）。"""
        self.execute(
            "UPDATE favorites SET last_check=?, last_index=? WHERE site=? AND url=?",
            (time.time(), int(latest_index), site, url),
        )


_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store