"""应用配置：读写 %APPDATA%/mangakit/config.json"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# _dev_sandbox() 的进程级缓存：同一进程内 app_dir() 必须始终返回同一目录
_dev_home: Path | None = None


def _warn(msg: str) -> None:
    """配置层告警：GUI 下进日志，CLI 下也必须让人看见。"""
    _log.warning(msg)
    try:
        print(f"[mangakit] 警告：{msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 —— stderr 都坏了就别再抛
        pass


def _dev_sandbox() -> Path | None:
    """从仓库 tools/ 目录下起的脚本，自动改到进程级临时目录。

    返回 None 表示「这是正常使用，用真目录」。判定依据是**启动脚本的位置**
    （`sys.argv[0]`），不是 cwd，也不会因为 os.chdir 而误判：
    只有在 `tools/xxx.py` 且仓库里确实有 `mangakit/` 包时才算开发态。

    这样做的理由是 t_fix_check.py 的教训：它自己不调任何写接口，但驱动
    `MangaPage._on_comic` 时产品代码内部 `store().add_favorite()` 就落库了，
    「会不会写」根本没法静态判断。隔离必须发生在数据目录这一层。
    """
    if os.environ.get("MANGAKIT_ALLOW_REAL_HOME"):
        return None
    if getattr(sys, "frozen", False):
        return None                     # 打包后的 exe 不是开发态
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0 or not argv0.endswith(".py"):
        return None                     # 交互式解释器 / -c：不猜，照旧
    script = Path(argv0)
    if script.parent.name != "tools":
        return None                     # 便宜的字符串判断放前面，避免每次都 resolve
    try:
        script = script.resolve()
    except OSError:
        return None
    if not (script.parent.parent / "mangakit" / "__init__.py").is_file():
        return None                     # 只是恰好叫 tools/ 的别的项目
    # 同一进程复用同一个沙箱目录（多次调用 app_dir() 必须一致）
    global _dev_home
    if _dev_home is None:
        _dev_home = Path(tempfile.mkdtemp(prefix="mk_dev_"))
        _warn(f"检测到从 tools/ 下运行，数据目录已隔离到 {_dev_home}；"
              f"确需连真库请设 MANGAKIT_ALLOW_REAL_HOME=1")
    return _dev_home


def app_dir() -> Path:
    """用户数据根目录。"""
    p = _resolve_home()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_home() -> Path:
    """算出数据目录（不建目录），打包环境下要考虑不可写的回退路径。"""
    override = os.environ.get("MANGAKIT_HOME")
    if override:
        return Path(override)
    # 开发态护栏：从仓库 tools/ 目录下起的脚本一律改到临时目录。
    # 起因是 2026-10-14 的真实事故：tools\gui_smoke.py 没有设 MANGAKIT_HOME，
    # 而它驱动的界面代码里 MangaPage._on_comic 会自动 add_favorite()，
    # 于是测试把用户真实的收藏清空了。问题在于「这个脚本会不会写库」无法靠
    # 静态检查预测——写操作藏在产品代码里——所以这里做单点兜底：
    # 只要进程是从 tools\ 里起的，就绝不碰 %APPDATA%\mangakit。
    # 真要连真库调试，显式设 MANGAKIT_ALLOW_REAL_HOME=1。
    dev = _dev_sandbox()
    if dev is not None:
        return dev
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path.home() / ".mangakit"
    p = base / "mangakit"
    if not _writable_dir(p.parent):
        # 便携模式：用户数据根目录不可写时，退到可执行文件旁边（移动版常用）
        alt = _portable_dir()
        if alt is not None:
            return alt
    return p


def _portable_dir() -> Path | None:
    """exe 所在目录下的 MangаKitData，找不到可写位置时返回 None。"""
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = Path(sys.executable).resolve().parent
    for cand in (exe_dir / "MangaKitData", exe_dir / "data"):
        if _writable_dir(cand):
            return cand
    return None


class WriteDenied(RuntimeError):
    """目标目录不可写。

    单独立一个类型，是为了让下载队列能把「目录被占/权限不足/路径非法」翻译成
    人话，而不是把裸的 `PermissionError: [WinError 5] 拒绝访问` 甩到界面上。
    """


def _why_unwritable(e: OSError) -> str:
    """把 OSError 翻成一句能指导下一步的中文。"""
    win = getattr(e, "winerror", None)
    err = getattr(e, "errno", None)
    if win == 5 or err == 13:
        return "系统拒绝访问（权限不足，或该目录被其它程序占用）"
    if win == 3 or err == 2:
        return "路径不存在，且上级目录无法创建"
    if err in (20, 21) or win == 267:
        return "路径中有同名文件挡住了目录"
    if err == 28 or win == 112:
        return "磁盘空间不足"
    if win == 32:
        return "文件正被其它程序占用"
    return f"写入失败（{type(e).__name__}: {e}）"


def probe_writable(p: Path) -> tuple[bool, str]:
    """p 是否真能落文件。

    注意必须「建目录 + 真正建一个文件」两步都试。只 mkdir 会漏掉一种很常见的
    受限环境：目录能建、空文件却建不出来（此时表现是 Errno 13/EPERM 而不是
    WinError 5，只试 mkdir 会误判成可写，错误一路漏到下载时才炸）。

    返回 (是否可写, 不可写的原因)。
    """
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, _why_unwritable(e)
    probe = p / f".mangakit_write_probe_{os.getpid()}"
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("1")
        probe.unlink()
        return True, ""
    except OSError as e:
        try:
            probe.unlink()
        except OSError:
            pass
        return False, _why_unwritable(e)


def ensure_writable(p: Path) -> None:
    """落盘前的可写性闸门：不可写就抛 WriteDenied（带修复建议）。"""
    ok, why = probe_writable(p)
    if ok:
        return
    raise WriteDenied(
        f"无法写入目录：{p}\n"
        f"原因：{why}\n"
        f"怎么办：\n"
        f"  1) 把 MangaKit.exe 移到桌面或「文档」目录再运行 —— 不要放在开发工具、\n"
        f"     沙箱或隔离环境的工作区里，那类位置会禁止往工作区外写文件；\n"
        f"  2) 或在「设置 → 下载目录」改一个能写的路径；\n"
        f"  3) 关掉正在占用该目录的其它程序（图片查看器、同步盘）后重试。"
    )


def _writable_dir(p: Path) -> bool:
    """p（或它还没建时能建出来）是否可写。不能用 W_OK 判断父目录。"""
    return probe_writable(p)[0]


def rules_dir() -> Path:
    """站点规则目录。打包后指向解包出来的资源目录。"""
    env = os.environ.get("MANGAKIT_RULES_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    p = Path(__file__).parent / "rules"
    if p.is_dir():
        return p
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "mangakit" / "rules"
        if p.is_dir():
            return p
    return p


DEFAULTS: dict[str, Any] = {
    "download_dir": str(Path.home() / "Downloads" / "MangaKit"),
    "concurrency": 6,          # 单个任务内图片并发数
    "max_tasks": 3,            # 同时进行的章节任务数
    "timeout": 20,
    "retries": 3,
    "rate_limit": 0.0,         # 每个域名最小请求间隔（秒），0=不限
    "max_image_mb": 40,        # 单张图片体积上限
    "max_chapter_pages": 3000,
    "verify_image": True,      # 下载后校验是否为有效图片
    "convert_webp": True,      # webp 转 png
    "auto_split_long": True,   # 长图自动切割
    "split_min_height": 1600,  # 超过该高度视为长图并切割
    "split_overlap": 0,
    "naming": "{comic}/{volume}",
    "image_ext": "auto",       # auto | keep
    "pack_after": "off",       # off | cbz | pdf
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "referer_policy": "page",  # page | site | none
    "sites": {},               # site_id -> {enabled, cookies, headers, overrides}
    "recent_searches": [],
    "user_rules": [],          # 用户在设置里新增的站点规则（整段 JSON 文本）
    "first_run_done": False,
}


class Config:
    """线程安全的 JSON 配置容器。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "config.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load_error: str = ""
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                # utf-8-sig：兼容带 BOM 的 config.json（记事本、部分编辑器会加 BOM）
                raw = json.loads(self.path.read_text("utf-8-sig"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        self._data[k] = v
            except FileNotFoundError:
                pass
            except Exception as e:
                # 配置损坏时退回默认值，不阻断启动，但要说一声，否则用户会以为设置生效了
                self.load_error = f"{type(e).__name__}: {e}"
                _warn(f"config.json 读取失败，已退回默认设置（{self.path}）：{self.load_error}")

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, *, persist: bool = True) -> None:
        with self._lock:
            self._data[key] = value
            if persist:
                self.save()

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(values)
            self.save()

    def site(self, site_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.setdefault("sites", {}).setdefault(site_id, {}))

    def set_site(self, site_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            self._data.setdefault("sites", {}).setdefault(site_id, {}).update(values)
            self.save()

    @property
    def download_dir(self) -> Path:
        p = Path(self.get("download_dir"))
        p.mkdir(parents=True, exist_ok=True)
        return p


_config: Config | None = None


def config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config