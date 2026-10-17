"""PySide6 主窗口：搜索 → 漫画详情 → 章节勾选 → 下载 → 任务面板 → 设置/收藏。"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    Qt, QSize, QUrl, Signal, QTimer, QObject, QRunnable, QThreadPool,
)
from PySide6.QtGui import (
    QAction, QIcon, QKeySequence, QPixmap, QColor, QBrush, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHBoxLayout as _H, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget,
    QHeaderView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    QComboBox as _C,
)

from .. import __app_name__, __app_name_cn__, __version__
from ..config import config
from ..downloader import manager
from ..images import make_thumb
from ..logsetup import get
from ..models import Chapter, Comic, Task
from ..probe import (
    ProbeResult, SiteProber, add_user_rule, is_user_rule, probe_sync,
    remove_user_rule,
)
from ..service import ComicService
from ..sites import Site, registry, reload_registry
from .bridge import bridge

log = get("gui")

STATUS_TEXT = {
    "queued": "排队中", "running": "下载中", "done": "已完成",
    "error": "失败", "paused": "已暂停", "skipped": "已取消", "pending": "待处理",
}


# ============================================================== 封面异步加载器

class CoverLoader(QObject):
    """后台线程池取封面，取到一张就发一个 signal。

    以前 _cover_pixmap 在 GUI 线程里同步 httpx.get(每张 8s 超时)，
    bzmanga 一次 83 条结果 = 83 次串行阻塞下载，界面直接卡死。
    现在先摆占位图立刻显示，封面在后台并行取，取到谁就刷谁。
    """

    loaded = Signal(str, object)   # (url, QPixmap)

    CACHE_LIMIT = 600             # 缓存文件数上限，超了按 mtime 删最旧

    def __init__(self, parent=None, workers: int = 6):
        super().__init__(parent)
        self.cache = Path(os.environ.get("TEMP", ".")) / "mangakit_cover"
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, workers))
        self._done: set[str] = set()   # 已取过的 url（含失败），避免重复排队
        self._mem: dict[str, QPixmap] = {}
        self._ph: dict[tuple[int, int], QPixmap] = {}   # 占位图按尺寸缓存

    def placeholder(self, size: QSize) -> QPixmap:
        """满尺寸的空封面占位图（不能返回空 QPixmap，原因见 get()）。"""
        key = (size.width(), size.height())
        pm = self._ph.get(key)
        if pm is None or pm.isNull():
            pm = QPixmap(size)
            pm.fill(QColor("#e9e9e9"))
            self._ph[key] = pm
        return pm

    # ---- 缓存路径 ----
    def _path(self, url: str) -> Path:
        import hashlib
        key = hashlib.md5(url.encode()).hexdigest()[:20]
        return self.cache / f"{key}.jpg"

    def _prune(self) -> None:
        try:
            files = sorted(self.cache.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for p in files[:max(0, len(files) - self.CACHE_LIMIT)]:
            try:
                p.unlink()
            except OSError:
                pass

    # ---- 对外：立刻返回可用的图（占位或缓存），并安排后台取 ----
    def get(self, url: str, size: QSize) -> QPixmap:
        """立刻返回一张**满尺寸**的图：命中缓存给真图，否则给灰色占位图。

        这里绝不能返回空 QPixmap()：QListWidget 开了 setUniformItemSizes(True)
        后会用「第一条目」的 sizeHint 冻结所有条目的统一尺寸，而空图标的 sizeHint
        只有文本那么大（实测 80×25）。封面稍后异步到达、setIcon 能让它**画**出来，
        但命中测试仍用 80×25 那个旧矩形 —— 于是用户瞄准封面双击时 indexAt()
        返回 None，itemDoubleClicked 根本不发，表现为「封面看得见、点上去没反应」。
        """
        if not url:
            return self.placeholder(size)
        if url in self._mem:
            return self._mem[url]
        f = self._path(url)
        if f.exists():
            pm = QPixmap(str(f))
            if not pm.isNull():
                pm = pm.scaled(size, Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
                self._mem[url] = pm
                return pm
            try:
                f.unlink()          # 坏缓存，删掉重下
            except OSError:
                pass
        self.fetch(url, size)
        return self.placeholder(size)

    def fetch(self, url: str, size: QSize) -> None:
        if not url or url in self._done:
            return
        self._done.add(url)
        self._pool.start(_CoverTask(self, url, size))

    def _store(self, url: str, data: bytes) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)
        self._path(url).write_bytes(data)

    def _emit(self, url: str, size: QSize) -> None:
        f = self._path(url)
        if not f.exists():
            self.loaded.emit(url, QPixmap())
            return
        pm = QPixmap(str(f))
        if pm.isNull():
            self.loaded.emit(url, QPixmap())
            return
        pm = pm.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        self._mem[url] = pm
        if len(self._mem) > 400:
            self._mem.clear()
        self.loaded.emit(url, pm)


class _CoverTask(QRunnable):
    """线程池里的单个取封面任务。"""

    def __init__(self, loader: CoverLoader, url: str, size: QSize):
        super().__init__()
        self.loader = loader
        self.url = url
        self.size = size
        self.setAutoDelete(True)

    def run(self) -> None:
        f = self.loader._path(self.url)
        if not f.exists():
            try:
                import httpx
                with httpx.Client(
                    timeout=httpx.Timeout(6.0, connect=4.0),
                    follow_redirects=True,
                    headers={"User-Agent": str(config().get("user_agent"))},
                ) as c:
                    r = c.get(self.url)
                    if r.status_code == 200 and len(r.content) > 200:
                        self.loader._store(self.url, make_thumb(r.content, 320))
                        self.loader._prune()
            except Exception:
                return
        try:
            self.loader._emit(self.url, self.size)
        except RuntimeError:
            pass    # 窗口已销毁


# ==================================================================== 搜索页

class SearchPage(QWidget):
    """搜索框 + 结果网格。"""

    open_comic = Signal(object)   # Comic

    def __init__(self, svc: ComicService, parent=None):
        super().__init__(parent)
        self.svc = svc
        self._pending = 0
        self._rid = 0              # 本次搜索的 request_id，用来丢弃上一次的残留信号
        self._shown = 0
        self._items: dict[str, QListWidgetItem] = {}   # cover url -> item
        self._covers = CoverLoader(self)
        self._build()
        br = bridge()
        br.search_done.connect(self._on_done)
        br.search_partial.connect(self._on_partial)
        br.failed.connect(self._on_failed)
        self._covers.loaded.connect(self._on_cover)

    def _build(self):
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.ed = QLineEdit()
        self.ed.setPlaceholderText("输入漫画名，回车搜索（留空则同时向所有启用站点搜索）")
        self.ed.setClearButtonEnabled(True)
        self.ed.returnPressed.connect(self.start)
        self.cb_site = QComboBox()
        self.cb_site.addItem("全部站点", "")
        for s in registry().visible_sites():
            # 不支持站内搜索的站在下拉里直接标出来，避免选完只得到 0 条
            self.cb_site.addItem(s.name if s.can_search else s.name + "（不支持搜索）", s.id)
        self.btn = QPushButton("搜索")
        self.btn.clicked.connect(self.start)
        bar.addWidget(self.ed, 1)
        bar.addWidget(self.cb_site)
        bar.addWidget(self.btn)
        lay.addLayout(bar)

        self.hint = QLabel("提示：也可以直接粘贴漫画详情页网址，点「打开网址」")
        self.hint.setStyleSheet("color:#888")
        lay.addWidget(self.hint)

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setIconSize(QSize(140, 190))
        self.list.setGridSize(QSize(170, 226))
        self.list.setWordWrap(True)
        self.list.setUniformItemSizes(True)   # 条目同尺寸，滚动与布局大幅提速
        self.list.itemDoubleClicked.connect(self._open_item)
        self.list.setStyleSheet("QListWidget::item{padding:4px} QListWidget::item:selected{background:#3b6ea5}")
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_open_url = QPushButton("打开网址…")
        self.btn_open_url.clicked.connect(self.open_by_url)
        row.addWidget(self.btn_open_url)
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        lay.addLayout(row)

    def start(self):
        kw = self.ed.text().strip()
        site_id = self.cb_site.currentData() or ""
        if not kw:
            self.status.setText("请输入关键词")
            return
        reg = registry()
        site = next((s for s in reg.all_sites() if s.id == site_id), None)
        if site is not None and not site.can_search:
            self.status.setText("该站不支持站内搜索")
            QMessageBox.information(
                self, "不支持搜索",
                f"{site.name} 不支持站内搜索（站点搜索接口是 JS 异步加载或已失效）。\n\n"
                "请点「打开网址…」，直接粘贴该站的作品详情页地址。")
            return
        skipped = [s.name for s in reg.visible_sites() if not s.can_search]
        self._reset()
        self._pending = 1
        self.btn.setEnabled(False)
        self.status.setText("搜索中…")
        # 先置 0 再取新 id：取 id 之前到达的旧残留信号一律丢弃
        # （新 id 是同步返回的，而工作线程的信号是排队投递到 GUI 线程的，
        #  所以下面这一行执行完之后，任何到达的信号都已经是新搜索的了）
        if site_id:
            self._rid = self.svc.search_site(site_id, kw)
        else:
            self._rid = self.svc.search_all(kw)
        if skipped and not site_id:
            self.status.setText(
                f"搜索中…（已跳过 {len(skipped)} 个不支持搜索的站点）")

    def _reset(self):
        """清空结果视图与封面索引，并作废上一次搜索的 request_id。"""
        self._pending = 0
        self._rid = 0
        self.list.clear()
        self._items.clear()
        self._shown = 0
        self._covers._done.clear()

    def open_by_url(self):
        url, ok = QInputDialog_getText(self, "打开网址", "漫画详情页网址：")
        if not ok or not url or not url.strip():
            return
        # 走与「双击搜索结果」完全相同的链路（open_comic -> show_comic ->
        # MangaPage.load），别自己另发一次 load_comic：那样 comic_loaded 会被
        # 详情页的 rid 守卫拦掉，用户看到的是「输完网址什么都没发生」。
        self.status.setText("正在打开…")
        self.open_comic.emit(Comic(title="", url=url.strip()))

    def _add_items(self, results) -> int:
        """把一批结果加进列表，返回本批条数。

        每条都必须带上**满尺寸**图标：空图标会让 QListWidget 把统一尺寸
        （setUniformItemSizes(True)）冻成文本大小，导致封面可见但点不到。
        """
        size = self.list.iconSize()
        n = 0
        for c in results:
            pm = self._covers.get(c.cover, size)
            if pm.isNull():
                # 只在「完全没有图」时兜底：KeepAspectRatioByExpanding 会让真图
                # 的尺寸略大于 iconSize（如 140×210），不能拿尺寸不等当判据。
                pm = self._covers.placeholder(size)
            it = QListWidgetItem(QIcon(pm), c.title)
            it.setData(Qt.UserRole, c)
            it.setToolTip(f"{c.title}\n{c.site_name}\n{c.url}")
            self.list.addItem(it)
            if c.cover:
                self._items.setdefault(c.cover, it)
            n += 1
        self._shown += n
        return n

    def _on_partial(self, rid: int, results):
        """单站先到：立刻显示，不等其它站。"""
        if rid != self._rid:
            return                     # 上一次搜索的残留分片，丢弃
        if not results or isinstance(results, Comic):
            return
        n = self._add_items(results)
        if n and self._pending:
            self.status.setText(f"搜索中… 已到 {self._shown} 个结果")

    def _on_done(self, rid: int, results):
        # 全站搜索的 done 只回一个总数（结果已通过 search_partial 逐站推完）；
        # 单站搜索的 done 直接带 Comic 列表，得在这里补加。
        if rid != self._rid:
            # 上一次搜索的收尾信号：既不能把本次的 _pending 清零（那会让按钮
            # 提前可点和「完成，共 0 个结果」），也不能把它的结果混进本次列表。
            return
        if isinstance(results, int):
            # 全站搜索的收尾信号：只带总数，没有可加的条目。
            # 这里必须**先**判 int —— 流式模式下每个分片都会把 _pending 归零，
            # 最后到来的 search_done(int) 就会掉进下面 _add_items(results) 分支，
            # 报「TypeError: 'int' object is not iterable」把整次搜索打断。
            self._pending = 0
            self.btn.setEnabled(True)
            self.status.setText(f"完成，共 {self._shown} 个结果")
            return
        if self._pending:
            self._pending = 0
            self.btn.setEnabled(True)
            self._add_items(results)
            self.status.setText(f"找到 {len(results)} 个结果")
            return
        self._add_items(results)

    def _on_cover(self, url: str, pm: QPixmap):
        """后台取到封面：就地刷新对应条目，不动布局、不重排。"""
        it = self._items.get(url)
        if it is None or pm.isNull():
            return
        try:
            it.setIcon(QIcon(pm))
        except RuntimeError:
            pass

    def _on_failed(self, rid, msg):
        if rid != self._rid:
            return                     # 别的页面（详情页）的失败，别动搜索页状态
        self._pending = 0
        self.btn.setEnabled(True)
        self.status.setText(msg.replace("\n", " "))
        QMessageBox.warning(self, "失败", msg)

    def _open_item(self, item: QListWidgetItem):
        c = item.data(Qt.UserRole)
        if c:
            self.open_comic.emit(c)


def QInputDialog_getText(parent, title, label):
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getText(parent, title, label)


# ================================================================ 漫画详情页

class MangaPage(QWidget):
    """漫画详情 + 章节列表（支持全选/反选/范围选择 + 拖拽多选）。"""

    enqueue = Signal(object, object)   # (comic, list[Chapter])
    open_browser = Signal(str)         # 请求用内置浏览器打开当前漫画的网址

    def __init__(self, svc: ComicService, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.comic: Comic | None = None
        self.chapters: list[Chapter] = []
        self._rid_c = self._rid_ch = 0
        # 展开折叠章节时记住展开前勾了哪些，_render() 渲染新列表时按 (url, cid) 复原
        self._pending_keep: set[tuple[str, str]] = set()
        # 封面同样走后台线程池，别在 GUI 线程里同步下载
        self._covers = CoverLoader(self)
        self._covers.loaded.connect(self._on_cover)
        self._build()
        br = bridge()
        br.comic_loaded.connect(self._on_comic)
        br.chapters_loaded.connect(self._on_chapters)
        br.failed.connect(self._on_failed)

    def _on_cover(self, url: str, pm: QPixmap):
        if pm.isNull() or not self.comic or self.comic.cover != url:
            return
        try:
            self.lb_cover.setPixmap(pm)
        except RuntimeError:
            pass

    def _build(self):
        lay = QVBoxLayout(self)
        # 顶部信息
        top = QHBoxLayout()
        self.lb_cover = QLabel()
        self.lb_cover.setFixedSize(120, 160)
        self.lb_cover.setScaledContents(True)
        self.lb_cover.setFrameShape(QFrame.StyledPanel)
        info = QVBoxLayout()
        self.lb_title = QLabel("未选择漫画")
        self.lb_title.setStyleSheet("font-size:16px;font-weight:bold")
        self.lb_meta = QLabel("")
        self.lb_meta.setWordWrap(True)
        self.lb_meta.setStyleSheet("color:#777")
        info.addWidget(self.lb_title)
        info.addWidget(self.lb_meta)
        # 「在浏览器中打开」入口：站点要过验证码 / 图片防盗链、或者解析失败时，
        # 用户需要自己去站点页面看一眼。走应用内置浏览器，别把用户丢到系统浏览器里。
        row = QHBoxLayout()
        self.btn_browser = QPushButton("在浏览器中打开")
        self.btn_browser.setEnabled(False)
        self.btn_browser.setToolTip("用应用内置浏览器打开当前漫画的网址")
        self.btn_browser.clicked.connect(self._open_in_browser)
        row.addWidget(self.btn_browser)
        row.addStretch(1)
        info.addLayout(row)
        info.addStretch(1)
        top.addWidget(self.lb_cover)
        top.addLayout(info, 1)
        lay.addLayout(top)

        # 操作栏
        bar = QHBoxLayout()
        self.btn_reload = QPushButton("重新读取")
        self.btn_reload.clicked.connect(lambda: self.load(self._url))
        self.cb_sort = QComboBox()
        self.cb_sort.addItems(["正序", "倒序"])
        self.cb_sort.currentIndexChanged.connect(self._render)
        for text, fn in (
            ("全选", lambda: self._select(True)),
            ("反选", self._invert),
            ("取消选择", lambda: self._select(False)),
            ("仅选未下载", self._select_unread),
            ("选最新20话", lambda: self._select_latest(20)),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addWidget(self.cb_sort)
        bar.addStretch(1)
        # 不少站的详情页只渲染最近 10 话，剩下的藏在「加载更多章节」按钮后面。
        # 这个按钮重新走一遍章节解析，但 full=True —— 对配了 chapters.api 的站
        # 会去调站点自己的接口把折叠部分补齐（见 sites.py:_api_chapters）。
        self.btn_expand = QPushButton("展开全部章节")
        self.btn_expand.setEnabled(False)
        self.btn_expand.setToolTip("该站详情页只显示部分章节，点此展开站点折叠隐藏的其余章节")
        self.btn_expand.clicked.connect(self._expand_all)
        bar.addWidget(self.btn_expand)
        self.btn_dl = QPushButton("下载选中章节")
        self.btn_dl.setEnabled(False)
        self.btn_dl.clicked.connect(self._start_download)
        bar.addWidget(self.btn_dl)
        lay.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "章节", "页数", "状态"])
        # 章节列表是扁平的，没有父子层级，必须关掉缩进。主题默认缩进是 30px，
        # 而勾选列只有 28px 宽 —— 勾选框的内容区起点会越过列右边界，
        # visualRect 宽度变成 -2，勾选框被画到「章节」列标题文字上面，
        # 点它改的是章节列的勾选态，_checked() 永远读不到（表现为勾不上、点下载无反应）。
        self.tree.setIndentation(0)
        self.tree.setRootIsDecorated(False)
        # 勾选框指示器实际画在单元格 x=3..18，列宽给 26 让单元格正好包住它，
        # 点单元格中心（x=13）就能命中；给 40 的话中心会落在指示器右侧点不到。
        self.tree.setColumnWidth(0, 26)
        self.tree.setColumnWidth(1, 460)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        # 18px 的默认行高太扁，勾选框点不准；给到 26px。
        self.tree.setStyleSheet("QTreeWidget::item{min-height:26px}")
        # 表头第一列做成全选开关，省得每次都去点「全选」按钮。
        self.tree.header().setSectionsClickable(True)
        self.tree.header().sectionClicked.connect(self._on_header_clicked)
        lay.addWidget(self.tree, 1)

        # 详情页也必须有一行能看见的状态文字。这里以前只有一个裸属性
        # status_msg（写 5 次、读 0 次），用户的观感就是「点了没反应」：
        # 双击搜索结果后页面确实在解析，屏幕上却没有任何「正在做什么」的反馈。
        self.lb_status = QLabel("")
        self.lb_status.setStyleSheet("color:#666")
        lay.addWidget(self.lb_status)

        self._url = ""
        self._can_expand_site = False
        self._expand_base = None
        self._status_msg = ""

    @property
    def status_msg(self) -> str:
        return self._status_msg

    @status_msg.setter
    def status_msg(self, text: str) -> None:
        """status_msg 的赋值必须同时落到界面上，避免再出现「写了没人看」。"""
        self._status_msg = text or ""
        lb = getattr(self, "lb_status", None)
        if lb is not None:
            try:
                lb.setText(self._status_msg)
            except RuntimeError:
                pass

    def _on_header_clicked(self, section: int):
        """点表头第一列 = 全选/取消全选。"""
        if section != 0 or not self.tree.topLevelItemCount():
            return
        all_checked = all(
            self.tree.topLevelItem(i).checkState(0) == Qt.Checked
            for i in range(self.tree.topLevelItemCount())
        )
        self._select(not all_checked)

    # ---- 载入 ----
    def load(self, url: str):
        self._url = url
        self.status_msg = "解析中…"
        self.btn_browser.setEnabled(bool(url))
        self._refresh_expand_button(url)
        self._rid_c = self.svc.load_comic(url)

    def _apply_expand_state(self):
        """按 _can_expand_site 刷新「展开全部章节」的可用性与提示文案。

        单独抽出来是因为它同时被「载入漫画」和「重绘章节列表」两条路径调用，
        两处必须给出完全一致的按钮状态，否则又会出现「看起来能点、点完没反应」。
        """
        can = bool(self._can_expand_site)
        has = bool(self.chapters)
        self.btn_expand.setEnabled(can and has)
        name = getattr(self, "_expand_site_name", "") or "该站"
        if can:
            self.btn_expand.setToolTip(f"{name}的详情页只显示部分章节，点此展开折叠隐藏的其余章节")
        else:
            self.btn_expand.setToolTip("该站详情页已给出全部章节（或其规则未配置章节接口），无需展开")

    def _refresh_expand_button(self, url: str):
        """按站点规则判断「展开全部章节」值不值得点，别让用户点了没反应。

        规则里没配 chapters.api / chapters.embed 的站，full=True 也补不出任何
        章节（实测 22 个站里只有 3 个配了），按钮就该置灰并说明原因，而不是
        让用户点下去等一次完整重载、结果列表纹丝不动。
        """
        can = False
        name = ""
        try:
            from ..sites import registry
            site = registry().site_for_url(url)
            if site is not None:
                can = bool(site.can_expand)
                name = site.name
        except Exception:
            can = False
        self._can_expand_site = can
        self._expand_site_name = name
        self._apply_expand_state()

    def _on_comic(self, rid, c: Comic):
        if rid != self._rid_c:
            return
        self.comic = c
        self.lb_title.setText(c.title)
        self.btn_browser.setEnabled(bool(c.url))
        tags = "、".join(c.tags[:8])
        self.lb_meta.setText(f"来源：{c.site_name}\n作者：{c.author}\n状态：{c.status}\n标签：{tags}")
        self.status_msg = "已找到漫画，正在读取章节…"
        if c.cover:
            # 先摆缓存/占位，取不到就等后台回填（避免卡住 GUI 线程）
            pm = self._covers.get(c.cover, QSize(120, 160))
            self.lb_cover.setPixmap(pm)
        try:
            from ..store import store
            store().add_favorite({"site": c.site, "url": c.url, "title": c.title,
                                  "author": c.author, "cover": c.cover, "tags": c.tags[:10]})
        except Exception:
            pass
        self._rid_ch = self.svc.load_chapters(c.url)

    def _expand_all(self):
        """展开折叠章节：带 full=True 重新加载，并记住当前勾选状态。"""
        if not self._url:
            return
        # 先记下勾了哪些（用 url 当标识，重新拉回来后按 url 复原）。
        # 复原交给 _render() 统一做，这样中途任何一次重绘都不会把勾选弄丢。
        self._pending_keep = {(ch.url, ch.cid) for ch in self._checked()}
        self._expand_base = len(self.chapters)
        self._expand_rid = self._rid_ch
        self.btn_expand.setEnabled(False)
        self.btn_expand.setText("展开中…")
        self.status_msg = "正在展开折叠章节…"
        self._rid_ch = self.svc.reload_chapters_full(self._url)

    def _on_chapters(self, rid, chapters: list[Chapter]):
        if rid != self._rid_ch:
            return
        before = len(self.chapters)
        was_expand = getattr(self, "_expand_base", None) is not None
        self.chapters = chapters
        self._render()
        self.btn_expand.setText("展开全部章节")
        self.btn_expand.setEnabled(bool(chapters) and self._can_expand_site)
        if was_expand:
            # 展开是一个「用户主动点了按钮」的动作，必须有明确回执 —— 之前这里
            # 静默复原成原样，用户分不清「已经全是全部章节」和「不支持展开」。
            self._expand_base = None
            gained = len(chapters) - before
            if gained > 0:
                self.status_msg = f"展开完成：新增 {gained} 话，共 {len(chapters)} 话"
            elif self._can_expand_site:
                self.status_msg = f"展开完成：该站详情页已是全部 {len(chapters)} 话"
            else:
                self.status_msg = (
                    f"该站不支持展开（规则未配置章节接口），"
                    f"当前 {len(chapters)} 话即站点详情页全部内容"
                )
        elif not chapters:
            # 解析成功但一话都没有：必须说清楚，否则用户看到空列表会以为「点了没反应」。
            self.status_msg = "已读取详情页，但没有解析到任何章节（该站规则可能已失效）"
        else:
            self.status_msg = f"已载入 {len(chapters)} 话"

    def _on_failed(self, rid, msg):
        if rid in (self._rid_c, self._rid_ch):
            # 失败也要给文字回执：只弹框的话，框一关页面又恢复成一片空白，
            # 用户仍会觉得「点了没反应」。这里同时写状态栏并复原按钮。
            self.status_msg = f"解析失败：{msg}".replace("\n", " ")
            self.btn_expand.setText("展开全部章节")
            self.btn_expand.setEnabled(False)
            QMessageBox.warning(self, "解析失败", msg)

    # ---- 渲染 ----
    def _render(self):
        # 复原「展开前勾了哪些」。放在 _render 里而不是 _on_chapters 里：
        # 换排序、改筛选都会重画，只有渲染这一层才拿得到最新一棵树，
        # 也才不会在别的路径重绘时把 pending 状态提前清掉。
        keep = self._pending_keep
        self.tree.clear()
        chs = list(self.chapters)
        if self.cb_sort.currentIndex() == 1:
            chs.reverse()
        for ch in chs:
            item = QTreeWidgetItem(["", ch.title, str(ch.pages or ""), STATUS_TEXT.get(ch.status, ch.status)])
            item.setData(0, Qt.UserRole, ch)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            hit = (ch.url, ch.cid) in keep
            item.setCheckState(0, Qt.Checked if hit else Qt.Unchecked)
            self.tree.addTopLevelItem(item)
        self._pending_keep = set()
        self.btn_dl.setEnabled(bool(chs))
        # 展开按钮的可用性由「站点是否配了章节接口」决定，不能因为列表非空就点亮。
        self._apply_expand_state()

    def _checked(self) -> list[Chapter]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == Qt.Checked:
                out.append(it.data(0, Qt.UserRole))
        return out

    def _select(self, v: bool):
        st = Qt.Checked if v else Qt.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, st)

    def _invert(self):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setCheckState(0, Qt.Unchecked if it.checkState(0) == Qt.Checked else Qt.Checked)

    def _select_unread(self):
        self._select(False)
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            ch = it.data(0, Qt.UserRole)
            if ch and ch.status != "done":
                it.setCheckState(0, Qt.Checked)

    def _select_latest(self, n: int):
        self._select(False)
        chs = self.chapters[-n:]
        ids = {id(c) for c in chs}
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if id(it.data(0, Qt.UserRole)) in ids:
                it.setCheckState(0, Qt.Checked)

    def _start_download(self):
        chs = self._checked()
        if not chs:
            QMessageBox.information(self, "提示", "请先勾选章节")
            return
        if self.comic:
            self.enqueue.emit(self.comic, chs)

    def _open_in_browser(self):
        """把当前漫画（或正在解析的网址）交给内置浏览器页打开。

        只发信号、不自己找 BrowserPage：MangaPage 不该知道主窗口里页面的顺序，
        由 MainWindow 接线，测试里也能单独接一个桩来断言 URL。
        """
        url = (self.comic.url if self.comic else "") or self._url
        if not url:
            QMessageBox.information(self, "提示", "还没有可以打开的网址")
            return
        self.open_browser.emit(url)


# ==================================================================== 任务页

class TasksPage(QWidget):
    """下载任务列表 + 日志。"""

    MAX_LOG_LINES = 2000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mgr = manager()
        self._rows: dict[str, int] = {}
        self._build()
        br = bridge()
        br.task_changed.connect(self._on_task)
        br.log_line.connect(self._on_log)
        self.mgr.on_task_changed = br.task_changed.emit
        self.mgr.on_log = lambda msg, lvl: br.log_line.emit(lvl, msg)
        self.reload()

    def _build(self):
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        for text, fn in (("全部继续", lambda: self._bulk("resume")),
                         ("全部暂停", lambda: self._bulk("pause")),
                         ("清理已完成", self._clear_finished),
                         ("打开下载目录", lambda: os.startfile(config().download_dir))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        self.lb_sum = QLabel("")
        bar.addWidget(self.lb_sum)
        lay.addLayout(bar)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["漫画", "章节", "进度", "状态", "速度", "错误"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for i, w in enumerate((170, 190, 150, 70, 90, 220)):
            self.table.setColumnWidth(i, w)
        lay.addWidget(self.table, 2)

        row = QHBoxLayout()
        for text, fn in (("暂停", lambda: self._sel("pause")), ("继续", lambda: self._sel("resume")),
                         ("重试", lambda: self._sel("retry")), ("删除", lambda: self._sel("cancel"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.chk_auto_scroll = QCheckBox("自动滚动")
        self.chk_auto_scroll.setChecked(True)
        row.addWidget(self.chk_auto_scroll)
        lay.addLayout(row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(180)
        lay.addWidget(self.log)

    def reload(self):
        self.table.setRowCount(0)
        self._rows.clear()
        for t in self.mgr.tasks():
            self._append(t)

    def _append(self, t: Task) -> int:
        r = self.table.rowCount()
        self.table.insertRow(r)
        it0 = QTableWidgetItem(t.comic)
        # 把 tid 挂在第一列上：按钮要操作的是「这一行对应的任务」，
        # 不能靠漫画名反查（同一部漫画会有很多章节行，按名字只能命中第一条，
        # 于是「暂停第 3 话」实际暂停了第 1 话）。
        it0.setData(Qt.UserRole, t.tid)
        self.table.setItem(r, 0, it0)
        self.table.setItem(r, 1, QTableWidgetItem(t.chapter))
        self.table.setItem(r, 3, QTableWidgetItem(STATUS_TEXT.get(t.status, t.status)))
        self.table.setItem(r, 4, QTableWidgetItem(""))
        self.table.setItem(r, 5, QTableWidgetItem(t.error or ""))
        self._rows[t.tid] = r
        self._update(t)
        return r

    def _update(self, t: Task):
        r = self._rows.get(t.tid)
        if r is None:
            r = self._append(t)
        # 进度条复用：_update 每个进度回调都会跑（下载时每秒好几次），
        # 每次新建 QProgressBar 再 setCellWidget 会持续创建/销毁子控件，
        # 任务一多就明显卡。已存在就只改数值。
        bar = self.table.cellWidget(r, 2)
        if not isinstance(bar, QProgressBar):
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.setMinimumWidth(140)
            self.table.setCellWidget(r, 2, bar)
        bar.setValue(t.progress)
        bar.setFormat(f"{t.finished}/{t.total} 页")
        it3 = self.table.item(r, 3)
        if it3 is None:
            it3 = QTableWidgetItem("")
            self.table.setItem(r, 3, it3)
        it3.setText(STATUS_TEXT.get(t.status, t.status))
        sp = f"{t.speed:.1f} 页/s" if t.speed else ""
        if t.eta and t.status == "running":
            sp += f"  剩 {int(t.eta)}s"
        it = self.table.item(r, 4)
        if it is None:
            it = QTableWidgetItem("")
            self.table.setItem(r, 4, it)
        it.setText(sp)
        e = self.table.item(r, 5)
        if e is None:
            e = QTableWidgetItem("")
            self.table.setItem(r, 5, e)
        e.setText(t.error or "")
        self.lb_sum.setText(f"共 {self.table.rowCount()} 个任务")

    def _on_task(self, t: Task):
        self._update(t)
        if self.chk_auto_scroll.isChecked() and t.status in ("done", "error"):
            self.table.scrollToBottom()

    def _on_log(self, level: str, msg: str):
        # 日志框以前没有上限：一个几百页的任务能刷上千行，跑一晚上内存只涨不降。
        # 超过 MAX_LOG_LINES 就砍掉最早的 1/4（按块砍，避免每行都触发一次删除）。
        self.log.appendPlainText(msg)
        doc = self.log.document()
        if doc.blockCount() > self.MAX_LOG_LINES:
            cur = self.log.textCursor()
            cur.movePosition(QTextCursor.Start)
            cur.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor,
                             self.MAX_LOG_LINES // 4)
            cur.removeSelectedText()

    def _sel(self, action: str):
        """对选中的行执行 pause/resume/retry/cancel。

        必须按行里存的 tid 操作。旧写法从第 0 列取 Qt.UserRole 拿 tid，但
        _append 从来没 setData 过，拿到恒为 None，于是退化成「按漫画名匹配
        第一个任务」—— 同一部漫画下暂停第 3 话会暂停第 1 话，删错任务。
        """
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        for r in rows:
            it = self.table.item(r, 0)
            tid = it.data(Qt.UserRole) if it is not None else None
            if not tid:
                continue
            fn = getattr(self.mgr, action, None)
            if fn is not None:
                fn(tid)

    def _clear_finished(self):
        """清掉已完成/已取消的任务，并把表格一起刷新。

        DownloadManager.clear_finished() 只从内存里摘任务、不发任何信号，
        所以直接连它的话表格行会留在原地 —— 用户点了「清理已完成」，行还在，
        再点「暂停」就操作到一条已经不存在的任务上。
        """
        self.mgr.clear_finished()
        self.reload()

    def _bulk(self, action: str):
        for t in self.mgr.tasks():
            if action == "pause" and t.status in ("running", "queued"):
                self.mgr.pause(t.tid)
            elif action == "resume" and t.status in ("paused", "error"):
                self.mgr.resume(t.tid)


# ==================================================================== 收藏页

def _row_prev_total(row: Any) -> int:
    """从收藏行里取「上一次检查时的最大话号」（界面上「上次章节数」那一列）。

    优先读 base_no（每次检查时冻住的旧话号）。老库还没有这一列、或它还是
    -1 时，退回 last_no（那是"最近一次"的话号，勉强能用）。
    **故意不回退到 base_total / last_index**：那是章节数 / 列表位置，和话号
    是不同的量，拿来显示会凭空多报好多话。都没有就给 -1，界面上显示「—」。
    """
    for key in ("base_no", "last_no"):
        try:
            v = int(row[key])
            if v >= 0:
                return v
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    return -1


class FavoritesPage(QWidget):
    open_comic = Signal(object)

    # 表格列：0 漫画 1 作者 2 来源 3 最新章节 4 上次章节数 5 状态 6 上次检查 7 网址
    # 列名「上次章节数」沿用历史叫法（用户熟悉的说法），但内容口径是**最大话号**：
    # 显示 base_no（上次检查时源站的最大话号），不是章节总数。
    (COL_TITLE, COL_AUTHOR, COL_SITE, COL_LATEST, COL_PREV, COL_STATE,
     COL_CHECKED, COL_URL) = range(8)

    def __init__(self, svc: ComicService, parent=None):
        super().__init__(parent)
        # 检查更新要发网络请求，必须拿到 service。这里以前没有 svc 参数，
        # 而 _check_updates() 里直接写 self.svc.check_updates(...) —— 属性根本
        # 不存在。PySide6 会把槽里的异常吞掉，于是按钮永远停在「检查中…」，
        # 一个请求都没发出去，用户看到的就是「点了卡住、没反应」。
        self.svc = svc
        self._updates: dict[tuple[str, str], Any] = {}   # (site,url) -> FavoriteUpdate
        self._checking = False
        self._check_total = 0
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.ed = QLineEdit()
        self.ed.setPlaceholderText("搜索收藏…")
        self.ed.textChanged.connect(self.reload)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.reload)
        bar.addWidget(self.ed, 1)
        bar.addWidget(self.btn_refresh)
        lay.addLayout(bar)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["漫画", "作者", "来源", "最新章节", "上次章节数", "状态", "上次检查", "网址"])
        self.table.horizontalHeader().setStretchLastSection(True)
        # 「上次检查」才有意义的那一列给个提示，免得和「上次检查」时间列搞混
        self.table.horizontalHeaderItem(4).setToolTip(
            "上一次检查时源站的最大话号（例如「第 100 话」就记成 100 话）。\n"
            "和本次的最大话号对比，就能看出到底多了几话。\n"
            "按话号比而不是按章节总数比：源站跳话/缺话时，总数只多 1、"
            "实际却多出十几话可看。\n"
            "本次话号在「状态」列的悬停提示里。")
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 支持多选（Ctrl/Shift 点），才能一次删掉好几条、也能只检查选中的
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.doubleClicked.connect(self._open)
        # 右键菜单：删除 / 打开 / 检查更新（比先选中再找按钮顺手）
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_check = QPushButton("检查更新")
        self.btn_check.setToolTip(
            "去每个源站看收藏的漫画有没有新章节。\n"
            "第一次检查只记录当前进度，之后才有对比基准。\n"
            "选中若干条时只检查选中的。")
        self.btn_check.clicked.connect(self._check_updates)
        row.addWidget(self.btn_check)
        self.btn_del = QPushButton("删除选中")
        self.btn_del.setToolTip("删除选中的收藏（可按住 Ctrl / Shift 多选）")
        self.btn_del.clicked.connect(self._del)
        row.addWidget(self.btn_del)
        self.btn_open = QPushButton("打开")
        self.btn_open.clicked.connect(self._open)
        row.addWidget(self.btn_open)
        self.btn_clear = QPushButton("清空收藏")
        self.btn_clear.setToolTip("删除全部收藏（会先确认）")
        self.btn_clear.clicked.connect(self._clear)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        self.lb_count = QLabel("")
        row.addWidget(self.lb_count)
        lay.addLayout(row)
        self.reload()

    # ---- 渲染 ----
    @staticmethod
    def _fmt_latest(title: str, no: int, index: int) -> str:
        """「最新章节」单元格文本：优先原标题，没标题才回退拼一个。"""
        if title:
            return title
        if no >= 0:
            return f"第 {no} 话"
        if index >= 0:
            return f"第 {index + 1} 话"      # 连话号都抠不出来，只能按位置报
        return ""

    @staticmethod
    def _prev_cell(row: Any, up: Any) -> Any:
        """「上次章节数」单元格：上一次检查时源站的最大话号（如「100 话」）。

        两个取值来源，按调用场景分工：
        - `row` 有值（`reload()` 整表重画）：以**库里存的**为准 ——
          `base_no` 是上次检查后冻住的旧话号，`last_no` 是本次值。
          这样检查跑完、甚至重启软件之后，这一列仍然对得上。
        - `row` 为 None（`_paint_row()` 就地刷某一行）：用本轮内存里的
          `up.prev_no` / `up.latest_no`，避免依赖刚写库的时序。
        """
        if row is not None:
            prev = _row_prev_total(row)
            try:
                n = int(row["last_no"])
                now = n if n >= 0 else -1
            except (KeyError, IndexError, TypeError, ValueError):
                now = -1
        else:
            prev = int(getattr(up, "prev_no", -1) or -1) if up is not None else -1
            now = int(getattr(up, "latest_no", -1) or -1) if up is not None else -1

        cell = QTableWidgetItem("—" if prev < 0 else f"{prev} 话")
        if prev < 0:
            cell.setForeground(QBrush(QColor("#999")))
            cell.setToolTip("还没检查过，没有可对比的基准")
            return cell

        tip = [f"上次检查时最大话号：{prev} 话"]
        if now >= 0:
            delta = now - prev
            tip.append(f"本次最大话号：{now} 话"
                       + (f"（多 {delta} 话）" if delta > 0
                          else f"（少 {-delta} 话）" if delta < 0
                          else "（没变）"))
        else:
            # 源站标题里抠不出话号（这一话可能只写「番外」之类），
            # 这时界面上不显示对比，但 tooltip 要说清为什么
            tip.append("本次没能从章节标题里认出话号")
        cell.setToolTip("\n".join(tip))
        if now >= 0 and now != prev:
            cell.setForeground(QBrush(QColor("#c60")))
        return cell

    @staticmethod
    def _fmt_checked(ts: Any) -> str:
        try:
            t = float(ts)
        except (TypeError, ValueError):
            return "从未"
        if t <= 0:
            return "从未"
        import datetime
        d = datetime.datetime.fromtimestamp(t)
        now = datetime.datetime.now()
        if d.date() == now.date():
            return d.strftime("今天 %H:%M")
        if (now.date() - d.date()).days == 1:
            return d.strftime("昨天 %H:%M")
        return d.strftime("%m-%d %H:%M")

    def reload(self):
        from ..store import store
        kw = self.ed.text().strip().lower()
        rows = [r for r in store().favorites()
                if not kw or kw in r["title"].lower() or kw in (r["author"] or "").lower()]
        self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, self.COL_TITLE, QTableWidgetItem(r["title"]))
            self.table.setItem(i, self.COL_AUTHOR, QTableWidgetItem(r["author"] or ""))
            self.table.setItem(i, self.COL_SITE, QTableWidgetItem(r["site"]))

            up = self._updates.get((r["site"], r["url"]))
            latest = QTableWidgetItem("")
            state = QTableWidgetItem("")
            if up is not None:
                latest.setText(self._fmt_latest(up.latest_title, up.latest_no,
                                                up.latest_index))
                state.setText(up.display)
                if up.status == "new":
                    state.setForeground(QBrush(QColor("#c60")))   # 橙色 = 有更新
                    latest.setForeground(QBrush(QColor("#c60")))
                elif up.status == "error":
                    state.setForeground(QBrush(QColor("#c33")))
                elif up.status == "first":
                    state.setForeground(QBrush(QColor("#4a9")))
                # 悬停能看到「本轮拿什么比出来的、两边各是多少」，出问题时不用猜
                tip = [up.message or up.display]
                if up.latest_no >= 0:
                    tip.append(f"本次最大话号：{up.latest_no} 话")
                if up.total:
                    tip.append(f"本次章节数：{up.total}")
                state.setToolTip("\n".join(tip))
            else:
                # 还没检查过，但至少把「这站支持不支持检查」告诉用户
                st = self._support_state(r["site"])
                state.setText(st[0])
                state.setToolTip(st[1])
                state.setForeground(QBrush(QColor("#999")) if st[0] != "可检查"
                                    else QBrush(QColor("#666")))
            self.table.setItem(i, self.COL_LATEST, latest)
            self.table.setItem(i, self.COL_PREV, self._prev_cell(r, up))
            self.table.setItem(i, self.COL_STATE, state)
            self.table.setItem(i, self.COL_CHECKED,
                               QTableWidgetItem(self._fmt_checked(r["last_check"])))

            u = QTableWidgetItem(r["url"])
            u.setData(Qt.UserRole, r["url"])
            self.table.setItem(i, self.COL_URL, u)
            self.table.item(i, self.COL_TITLE).setData(Qt.UserRole, r["url"])

        total = len(store().favorites())
        # 检查进行中时优先显示进度。以前这里无条件写「共 N 条」，会把
        # _check_updates() 刚设好的「正在检查 N 本…」立刻盖掉，用户只看到
        # 「共 1 条」不动，跟没点一样 —— 这正是「卡住没反应」的观感来源。
        if self._checking:
            done = len(self._updates)
            self.lb_count.setText(f"正在检查 {done}/{self._check_total} 本…")
        elif kw:
            self.lb_count.setText(f"筛出 {len(rows)} / 共 {total} 条")
        else:
            self.lb_count.setText(f"共 {total} 条")
        # 没有收藏时禁用相关按钮，避免点了个寂寞
        self.btn_del.setEnabled(bool(rows))
        self.btn_open.setEnabled(bool(rows))
        self.btn_clear.setEnabled(total > 0)
        self.btn_check.setEnabled(bool(rows) and not self._checking)
        self.btn_check.setText("检查中…" if self._checking else "检查更新")

    @staticmethod
    def _support_state(site_id: str) -> tuple[str, str]:
        """这个站能不能做更新检查（站点被停用/删掉就不行）。"""
        s = registry().sites.get(site_id)
        if s is None:
            return "站点已移除", f"规则里已经没有 {site_id} 了"
        if not s.enabled:
            return "站点已停用", f"{s.name} 在设置里被停用了"
        return "可检查", f"{s.name}（{site_id}）"

    # ---- 选中项 ----
    def _selected(self) -> list[tuple[str, str]]:
        """返回选中行的 (site, url)。"""
        out = []
        for idx in self.table.selectionModel().selectedRows():
            i = idx.row()
            site = self.table.item(i, self.COL_SITE)
            url = self.table.item(i, self.COL_URL)
            if site is not None and url is not None:
                out.append((site.text(), url.text()))
        return out

    def _menu(self, pos):
        idx = self.table.indexAt(pos)
        if idx.isValid():
            # 右键点在没选中的行上，先把它选上，符合直觉
            if idx.row() not in {i.row() for i in self.table.selectionModel().selectedRows()}:
                self.table.selectRow(idx.row())
        sel = self._selected()
        m = QMenu(self)
        a_open = m.addAction("打开")
        a_open.setEnabled(len(sel) == 1)
        m.addSeparator()
        a_check = m.addAction(f"检查更新（{len(sel)} 条）" if sel else "检查更新")
        a_check.setEnabled(bool(sel) and not self._checking)
        m.addSeparator()
        a_del = m.addAction(f"删除选中（{len(sel)} 条）" if sel else "删除选中")
        a_del.setEnabled(bool(sel))
        m.addSeparator()
        a_clear = m.addAction("清空收藏")
        a_clear.setEnabled(self.table.rowCount() > 0)
        act = m.exec(self.table.viewport().mapToGlobal(pos))
        if act is a_open:
            self._open()
        elif act is a_check:
            self._check_updates()
        elif act is a_del:
            self._del()
        elif act is a_clear:
            self._clear()

    def _open(self):
        c = self._comic_at(self.table.currentRow())
        if c is not None:
            self.open_comic.emit(c)

    def _comic_at(self, idx: int):
        if idx < 0 or self.table.item(idx, self.COL_TITLE) is None:
            return None
        return Comic(title=self.table.item(idx, self.COL_TITLE).text(),
                     url=self.table.item(idx, self.COL_URL).text(),
                     site=self.table.item(idx, self.COL_SITE).text())

    # ---- 更新检查 ----
    def _check_updates(self):
        from ..store import store
        if self._checking:
            return
        sel = self._selected()
        if sel:
            targets = sel
        else:
            targets = [(r["site"], r["url"]) for r in store().favorites()]
        if not targets:
            return
        self._checking = True
        self._check_total = len(targets)
        # 先清掉旧结果，否则新结果没回来前会显示上一次的"有更新"
        for k in targets:
            self._updates.pop(k, None)
        self._rid = self.svc.check_updates(targets)
        self.reload()   # reload 里会按 _checking 显示进度，不在这里 hand-set 文案

    def on_update_partial(self, rid: int, up: Any):
        if rid != getattr(self, "_rid", None):
            return
        self._updates[(up.site, up.url)] = up
        # 就地刷新那一行，不整表重画（否则用户正在看的选中态会跳）
        for i in range(self.table.rowCount()):
            s = self.table.item(i, self.COL_SITE)
            u = self.table.item(i, self.COL_URL)
            if s is None or u is None:
                continue
            if s.text() == up.site and u.text() == up.url:
                self._paint_row(i, up)
                break
        if self._checking:
            self.lb_count.setText(f"正在检查 {len(self._updates)}/{self._check_total} 本…")

    def _paint_row(self, i: int, up: Any) -> None:
        latest = QTableWidgetItem(self._fmt_latest(up.latest_title, up.latest_no,
                                                   up.latest_index))
        state = QTableWidgetItem(up.display)
        tip = [up.message or up.display]
        if up.latest_no >= 0:
            tip.append(f"本次最大话号：{up.latest_no} 话")
        if up.total:
            tip.append(f"本次章节数：{up.total}")
        state.setToolTip("\n".join(tip))
        if up.status == "new":
            state.setForeground(QBrush(QColor("#c60")))
            latest.setForeground(QBrush(QColor("#c60")))
        elif up.status == "error":
            state.setForeground(QBrush(QColor("#c33")))
        elif up.status == "first":
            state.setForeground(QBrush(QColor("#4a9")))
        self.table.setItem(i, self.COL_LATEST, latest)
        self.table.setItem(i, self.COL_STATE, state)
        # 「上次章节数」保持本轮开始时快照的旧值（up.prev_total），别跟着刷成新的，
        # 否则刚查完这一列就变成了"本次"，跟没加这一列一样、没法对比。
        self.table.setItem(i, self.COL_PREV, self._prev_cell(None, up))

    def on_update_done(self, rid: int, stat: Any):
        if rid != getattr(self, "_rid", None):
            return
        self._checking = False
        self.btn_check.setText("检查更新")
        self.reload()
        stat = stat or {}
        n_new = int(stat.get("new", 0))
        n_err = int(stat.get("error", 0))
        checked = int(stat.get("checked", 0))
        parts = [f"检查完 {checked} 本"]
        if n_new:
            parts.append(f"{n_new} 本有更新")
        if n_err:
            parts.append(f"{n_err} 本失败")
        # 一本都没检查成时别说「暂无更新」——那会让人以为查过了、只是没更新，
        # 实际是根本没查成功。
        if not n_new and checked and not (n_err and checked == n_err):
            parts.append("暂无更新")
        # _guard 兜底的失败汇总：整体崩了时要让用户看到原因，
        # 不然按钮复原了、计数却是「检查完 0 本」，看着像什么都没发生。
        if stat.get("message"):
            parts.append(str(stat["message"]))
        self.lb_count.setText("，".join(parts))
        # 有更新的行挑出来高亮一下，方便一眼看到
        if n_new:
            for i in range(self.table.rowCount()):
                s = self.table.item(i, self.COL_SITE)
                u = self.table.item(i, self.COL_URL)
                if s is None or u is None:
                    continue
                up = self._updates.get((s.text(), u.text()))
                if up is not None and up.has_update:
                    self.table.selectRow(i)
                    self.table.scrollToItem(self.table.item(i, self.COL_TITLE))
                    break

    def _del(self):
        from ..store import store
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, "删除收藏", "请先选中要删除的收藏。")
            return
        if len(sel) == 1:
            title = self.table.item(self.table.currentRow(), self.COL_TITLE)
            name = title.text() if title is not None else sel[0][1]
            ask = f"确定删除收藏《{name}》吗？"
        else:
            ask = f"确定删除选中的 {len(sel)} 条收藏吗？"
        if QMessageBox.question(
            self, "删除收藏", ask,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        n = store().remove_favorites(sel)
        log.info("删除收藏 %d 条", n)
        for k in sel:
            self._updates.pop(k, None)
        self.reload()

    def _clear(self):
        from ..store import store
        total = len(store().favorites())
        if total <= 0:
            return
        if QMessageBox.question(
            self, "清空收藏",
            f"确定清空全部 {total} 条收藏吗？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        n = store().clear_favorites()
        log.info("清空收藏 %d 条", n)
        self._updates.clear()
        self.reload()


# ============================================== 添加网站（探测 + 确认）

class _ProbeTask(QRunnable):
    """线程池里跑一次站点探测（同步阻塞版）。"""

    def __init__(self, worker: "_ProbeWorker", url: str):
        super().__init__()
        self.worker = worker
        self.url = url
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            self.worker.finished.emit({"result": probe_sync(self.url, deep=True, timeout=90.0)})
        except Exception as e:  # noqa: BLE001
            self.worker.finished.emit({"error": f"{type(e).__name__}: {e}"})


class _ProbeWorker(QObject):
    """探测的后台跑腿，避免卡住界面（探测要开好几个页面，几秒到几十秒）。"""

    finished = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def start(self, url: str) -> None:
        self._pool.start(_ProbeTask(self, url))


class AddSiteDialog(QDialog):
    """「添加网站」对话框：贴网址 → 自动探测 → 用户决定是否添加。

    探测没通过的站（下不了图、图靠 JS 现拼、规则自检不过）一律不给加，
    只把原因说清楚。这是用户明确要求的：不能下载的不增加。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加网站")
        self.resize(680, 520)
        self.rule: dict[str, Any] | None = None
        self.result_info: ProbeResult | None = None
        self._busy = False
        self._worker = _ProbeWorker(self)
        self._worker.finished.connect(self._on_done)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        tip = QLabel(
            "贴一个漫画<strong>详情页</strong>网址（不是首页），点「开始探测」。<br>"
            "程序会自己试着打开这个站、找出章节和图片，能真正下到图才会让你添加。"
        )
        tip.setWordWrap(True)
        lay.addWidget(tip)

        h = QHBoxLayout()
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("https://www.example.com/comic/1234/")
        self.ed_url.returnPressed.connect(self._probe)
        self.btn_probe = QPushButton("开始探测")
        self.btn_probe.clicked.connect(self._probe)
        h.addWidget(self.ed_url, 1)
        h.addWidget(self.btn_probe)
        lay.addLayout(h)

        self.lb_state = QLabel("等待输入网址…")
        self.lb_state.setStyleSheet("color:#888")
        lay.addWidget(self.lb_state)

        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setPlaceholderText("探测结果会显示在这里")
        lay.addWidget(self.txt, 1)

        self.box = QDialogButtonBox()
        self.btn_add = self.box.addButton("添加到站点列表", QDialogButtonBox.AcceptRole)
        self.btn_cancel = self.box.addButton("取消", QDialogButtonBox.RejectRole)
        self.btn_add.setEnabled(False)
        self.box.accepted.connect(self.accept)
        self.box.rejected.connect(self.reject)
        lay.addWidget(self.box)

    # ---- 探测 ----

    def _probe(self) -> None:
        url = self.ed_url.text().strip()
        if not url:
            self.lb_state.setText("请先贴入网址。")
            return
        if "://" not in url:
            url = "https://" + url
            self.ed_url.setText(url)
        if self._busy:
            return
        self._busy = True
        self.btn_add.setEnabled(False)
        self.btn_probe.setEnabled(False)
        self.lb_state.setText("正在探测，请稍候……（要开好几个页面，慢的话可能要几十秒）")
        self.lb_state.setStyleSheet("color:#888")
        self.txt.setPlainText("")
        self._worker.start(url)

    def _on_done(self, payload: dict) -> None:
        self._busy = False
        self.btn_probe.setEnabled(True)
        if payload.get("error"):
            self.lb_state.setText("探测出错：" + payload["error"])
            self.lb_state.setStyleSheet("color:#c33")
            return
        r: ProbeResult = payload["result"]
        self.result_info = r
        self.txt.setPlainText(self._render(r))
        if r.addable:
            self.rule = r.rule
            self.btn_add.setEnabled(True)
            self.lb_state.setText("探测通过：这个站可以下载 ✅ 点下面的按钮添加。")
            self.lb_state.setStyleSheet("color:#2a7")
        else:
            self.rule = None
            self.btn_add.setEnabled(False)
            self.lb_state.setText("探测未通过：这个站暂时加不了 ❌ 原因见下方。")
            self.lb_state.setStyleSheet("color:#c33")

    @staticmethod
    def _render(r: ProbeResult) -> str:
        lines = [f"站点：{r.site_name}", f"域名：{r.host}"]
        if r.title:
            lines.append(f"漫画：{r.title}")
        lines.append(f"章节：{r.chapter_count} 话")
        lines.append(f"图片：{r.image_count} 张（实测 {len(r.ok_urls)}/{len(r.tried_urls)} 张可下载）")
        lines.append(f"搜索：{'支持' if r.can_search else '不支持'}")
        lines.append("")
        lines.append("—— 探测过程 ——")
        lines.extend("· " + x for x in r.log)
        lines.append("")
        if r.addable:
            lines.append("结论：可以添加 ✅")
        else:
            lines.append("结论：不予添加 ❌")
            lines.append(f"原因：{r.reason}")
        return "\n".join(lines)


# ==================================================================== 设置页

class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()
        self.reload()

    def _build(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.ed_dir = QLineEdit()
        b = QPushButton("选择…")
        b.clicked.connect(self._pick_dir)
        h = QHBoxLayout()
        h.addWidget(self.ed_dir, 1)
        h.addWidget(b)
        w = QWidget()
        w.setLayout(h)
        form.addRow("下载文件夹", w)

        self.sp_conc = QSpinBox()
        self.sp_conc.setRange(1, 32)
        form.addRow("单章并发数", self.sp_conc)

        self.sp_retry = QSpinBox()
        self.sp_retry.setRange(0, 10)
        form.addRow("失败重试次数", self.sp_retry)

        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(5, 120)
        form.addRow("超时（秒）", self.sp_timeout)

        self.sp_rl = QSpinBox()
        self.sp_rl.setRange(0, 30)
        self.sp_rl.setSuffix(" 秒/请求")
        form.addRow("同域限速（0=不限）", self.sp_rl)

        self.sp_mb = QSpinBox()
        self.sp_mb.setRange(1, 200)
        self.sp_mb.setSuffix(" MB")
        form.addRow("单图体积上限", self.sp_mb)

        self.cb_pack = QComboBox()
        self.cb_pack.addItem("不打包", "off")
        self.cb_pack.addItem("CBZ", "cbz")
        self.cb_pack.addItem("ZIP（压缩）", "zip")
        self.cb_pack.addItem("PDF", "pdf")
        self.cb_pack.addItem("EPUB", "epub")
        form.addRow("下载后自动打包", self.cb_pack)

        self.ck_webp = QCheckBox("WebP 转 PNG（兼容性更好）")
        form.addRow("", self.ck_webp)
        self.ck_verify = QCheckBox("校验图片有效性")
        form.addRow("", self.ck_verify)
        self.ck_split = QCheckBox("超长图自动切割")
        form.addRow("", self.ck_split)
        self.sp_split = QSpinBox()
        self.sp_split.setRange(500, 8000)
        self.sp_split.setSingleStep(200)
        form.addRow("切割高度阈值", self.sp_split)
        self.cb_referer = QComboBox()
        self.cb_referer.addItem("章节页（最稳）", "page")
        self.cb_referer.addItem("站点首页", "site")
        self.cb_referer.addItem("不发送", "none")
        form.addRow("Referer 策略", self.cb_referer)
        lay.addLayout(form)

        gb = QGroupBox("站点管理")
        gl = QVBoxLayout(gb)
        self.tbl_sites = QTableWidget(0, 4)
        self.tbl_sites.setHorizontalHeaderLabels(["启用", "站点", "域名", "说明"])
        self.tbl_sites.horizontalHeader().setStretchLastSection(True)
        self.tbl_sites.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_sites.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_sites.setSelectionMode(QAbstractItemView.SingleSelection)
        # 双击自定义站点也能删，符合直觉。
        self.tbl_sites.itemDoubleClicked.connect(lambda *_: self._del_site())
        gl.addWidget(self.tbl_sites)

        sh = QHBoxLayout()
        self.btn_add_site = QPushButton("添加网站")
        self.btn_add_site.setToolTip("贴入漫画详情页网址，自动探测这个站能否下载；不能下载的不予添加")
        self.btn_add_site.clicked.connect(self._add_site)
        self.btn_del_site = QPushButton("删除站点")
        self.btn_del_site.setToolTip("删除选中的自定义站点（内置站点不能删，只能取消左边的勾来停用）")
        self.btn_del_site.clicked.connect(self._del_site)
        sh.addWidget(self.btn_add_site)
        sh.addWidget(self.btn_del_site)
        sh.addStretch(1)
        self.lb_sites = QLabel("")
        self.lb_sites.setStyleSheet("color:#888")
        sh.addWidget(self.lb_sites)
        gl.addLayout(sh)
        lay.addWidget(gb)

        lay.addStretch(1)
        btns = QHBoxLayout()
        self.btn_apply = QPushButton("保存")
        self.btn_apply.clicked.connect(self.save)
        self.btn_reset = QPushButton("恢复默认")
        self.btn_reset.clicked.connect(self.reset)
        btns.addStretch(1)
        btns.addWidget(self.btn_reset)
        btns.addWidget(self.btn_apply)
        lay.addLayout(btns)
        self.lb_tip = QLabel("")
        self.lb_tip.setStyleSheet("color:#4a9")
        lay.addWidget(self.lb_tip)

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载文件夹", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    # ---- 添加 / 删除自定义网站 ----

    def _add_site(self):
        """弹「添加网站」框：贴一个漫画详情页网址，自动探测后由用户决定加不加。"""
        dlg = AddSiteDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.rule:
            ok = add_user_rule(dlg.rule)
            if not ok:
                QMessageBox.warning(self, "添加失败", "规则写入配置失败，请检查配置文件权限。")
                return
            reload_registry()
            self.reload()
            name = dlg.rule.get("name") or dlg.rule.get("id")
            self.lb_tip.setText(f"已添加「{name}」✓")
            QTimer.singleShot(3000, lambda: self.lb_tip.setText(""))
            log.info("用户添加自定义站点 %s", dlg.rule.get("id"))

    def _del_site(self):
        """删掉选中的自定义站点（内置规则不允许删，只能停用）。"""
        row = self.tbl_sites.currentRow()
        if row < 0:
            QMessageBox.information(self, "删除站点", "请先在列表里选中要删除的站点。")
            return
        it = self.tbl_sites.item(row, 0)
        sid = it.data(Qt.UserRole) if it else None
        if not sid:
            return
        name = self.tbl_sites.item(row, 1).text() if self.tbl_sites.item(row, 1) else sid
        if not is_user_rule(sid):
            QMessageBox.information(
                self, "删除站点",
                f"「{name}」是内置站点，不能删除。\n"
                "如果不想用它，把左边的「启用」勾去掉即可。",
            )
            return
        # 删除是不可逆的：默认按钮设成 No，别让回车变成误删。
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("删除站点")
        box.setText(f"确定删除自定义站点「{name}」吗？")
        box.setInformativeText("删除后需要重新添加才能恢复。")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        if remove_user_rule(sid):
            reload_registry()
            self.reload()
            self.lb_tip.setText(f"已删除「{name}」✓")
            QTimer.singleShot(3000, lambda: self.lb_tip.setText(""))
            log.info("用户删除自定义站点 %s", sid)
        else:
            QMessageBox.warning(self, "删除失败", "没找到这条自定义规则，可能已被删除。")

    def reload(self):
        c = config()
        self.ed_dir.setText(str(c.get("download_dir")))
        self.sp_conc.setValue(int(c.get("concurrency")))
        self.sp_retry.setValue(int(c.get("retries")))
        self.sp_timeout.setValue(int(c.get("timeout")))
        self.sp_rl.setValue(int(c.get("rate_limit")))
        self.sp_mb.setValue(int(c.get("max_image_mb")))
        i = self.cb_pack.findData(str(c.get("pack_after")))
        self.cb_pack.setCurrentIndex(max(0, i))
        self.ck_webp.setChecked(bool(c.get("convert_webp")))
        self.ck_verify.setChecked(bool(c.get("verify_image")))
        self.ck_split.setChecked(bool(c.get("auto_split_long")))
        self.sp_split.setValue(int(c.get("split_min_height")))
        i = self.cb_referer.findData(str(c.get("referer_policy")))
        self.cb_referer.setCurrentIndex(max(0, i))
        self.tbl_sites.setRowCount(0)
        n_custom = 0
        for s in registry().all_sites():
            r = self.tbl_sites.rowCount()
            self.tbl_sites.insertRow(r)
            ck = QTableWidgetItem("")
            ck.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            ck.setCheckState(Qt.Checked if s.enabled else Qt.Unchecked)
            ck.setData(Qt.UserRole, s.id)
            self.tbl_sites.setItem(r, 0, ck)
            custom = is_user_rule(s.id)
            if custom:
                n_custom += 1
            name_it = QTableWidgetItem(("[自建] " if custom else "") + s.name)
            if custom:
                name_it.setForeground(QBrush(QColor("#2a7")))
            self.tbl_sites.setItem(r, 1, name_it)
            self.tbl_sites.setItem(r, 2, QTableWidgetItem("、".join(s.domains[:4])))
            note = s.note or s.lang
            if custom:
                note = "自定义添加的站点，可删除"
            self.tbl_sites.setItem(r, 3, QTableWidgetItem(note))
        self.lb_sites.setText(f"共 {self.tbl_sites.rowCount()} 个站点，其中自建 {n_custom} 个")
        self.btn_del_site.setEnabled(n_custom > 0)

    def save(self):
        c = config()
        c.update({
            "download_dir": self.ed_dir.text().strip(),
            "concurrency": self.sp_conc.value(),
            "retries": self.sp_retry.value(),
            "timeout": self.sp_timeout.value(),
            "rate_limit": float(self.sp_rl.value()),
            "max_image_mb": self.sp_mb.value(),
            "pack_after": self.cb_pack.currentData(),
            "convert_webp": self.ck_webp.isChecked(),
            "verify_image": self.ck_verify.isChecked(),
            "auto_split_long": self.ck_split.isChecked(),
            "split_min_height": self.sp_split.value(),
            "referer_policy": self.cb_referer.currentData(),
        })
        # 站点启用状态要落盘：以前只调 registry().set_enabled()，改的是内存里
        # 的 Site.enabled，config 一个字都没写，重启后用户取消勾选的站点又全
        # 回来了。set_enabled_many 会一次性写进 config 的 sites 表。
        states = {}
        for r in range(self.tbl_sites.rowCount()):
            it = self.tbl_sites.item(r, 0)
            if it:
                sid = it.data(Qt.UserRole)
                if sid:
                    states[sid] = it.checkState() == Qt.Checked
        registry().set_enabled_many(states)
        # 并发数要热生效：DownloadManager 的信号量是 start() 时按当时的配置建的，
        # 之后改设置只写进 config，正在跑的队列还用旧并发，必须重启软件才变。
        # apply_concurrency 会在下载线程里换掉信号量（缩容时用占位协程收走多余
        # 许可，避免正在下载的任务被半途卡死）。管理器没起来时返回 0，无副作用。
        try:
            manager().apply_concurrency(self.sp_conc.value())
        except Exception as e:      # 槽里抛异常会被 Qt 吞掉，这里留个日志
            log.warning("应用并发数失败：%s", e)
        self.lb_tip.setText("已保存 ✓")
        QTimer.singleShot(3000, lambda: self.lb_tip.setText(""))

    def reset(self):
        from ..config import DEFAULTS
        config().update(DEFAULTS)
        self.reload()
        self.lb_tip.setText("已恢复默认")


# ================================================================== 图源仓库页

# yck2026（异次元）图源索引。整页的网络活儿 —— 列表、校验、写库 —— 全在
# QThreadPool 里跑：校验要真下一张图，秒级到十几秒级，同步跑必然假死。

_yck_mod = None      # 缓存已成功 import 的 ycksource 模块
_yck_err = ""        # 非空 = import 失败过一次，不再重试
_web_mod = None      # 缓存已成功 import 的 QWebEngineView 类
_web_err = ""        # 非空 = WebEngine 不可用


def _import_ycksource():
    """真正去 import 图源模块的那一步。单独抽成函数是为了测试能打桩成失败。"""
    from .. import ycksource
    return ycksource


def get_ycksource():
    """拿到 ycksource 模块；不可用时返回 None，绝不抛。

    图源模块有自己的一堆可选依赖（httpx 等），打包时可能被裁掉，它自己也在
    演进。它一旦 import 失败，必须只有「图源仓库」这一页降级，不能连主窗口
    都构造不出来 —— 所以这里把 import 期的所有异常都咽掉并记进 _yck_err。
    """
    global _yck_mod, _yck_err
    if _yck_mod is not None:
        return _yck_mod
    if _yck_err:
        return None
    try:
        _yck_mod = _import_ycksource()
    except Exception as exc:  # noqa: BLE001 - import 期什么都可能炸
        _yck_err = f"{type(exc).__name__}: {exc}"
        log.warning("图源模块不可用：%s", _yck_err)
        return None
    return _yck_mod


def ycksource_error() -> str:
    """返回图源模块不可用的原因（空串 = 可用）。"""
    return _yck_err


def _import_webengine():
    """真正去 import QtWebEngineWidgets 的那一步。单独抽出来是为了测试能打桩。"""
    from PySide6.QtWebEngineWidgets import QWebEngineView
    return QWebEngineView


def get_webengine():
    """拿到 QWebEngineView 类；不可用时返回 None，绝不抛。

    **必须懒加载**：onefile 打包后，import QtWebEngine 要把上百兆 Chromium
    运行库解包出来，主窗口首屏会从 ~0.8s 变成 ~3.4s，用户看到的就是「卡死了」。
    所以这个函数只在用户第一次进浏览器页（或第一次 open_url）时才被调到。
    """
    global _web_mod, _web_err
    if _web_mod is not None:
        return _web_mod
    if _web_err:
        return None
    try:
        _web_mod = _import_webengine()
    except Exception as exc:  # noqa: BLE001
        _web_err = f"{type(exc).__name__}: {exc}"
        log.warning("内置浏览器不可用：%s", _web_err)
        return None
    return _web_mod


class _SourceTask(QRunnable):
    """线程池里执行一次取数（列表 / 校验 / 写库），异常也吞成 payload 回主线程。"""

    def __init__(self, worker: "_SourceWorker", kind: str, fn):
        super().__init__()
        self.worker = worker
        self.kind = kind
        self.fn = fn
        self.setAutoDelete(True)

    def run(self):
        try:
            payload: dict[str, Any] = {"kind": self.kind, "result": self.fn()}
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成界面文案
            payload = {"kind": self.kind,
                       "error": f"{type(exc).__name__}: {exc}"}
        try:
            self.worker.finished.emit(payload)
        except RuntimeError:
            pass   # 页面已经销毁，丢弃结果即可


class _SourceWorker(QObject):
    """单线程池：列表/校验/写库排队串行，用户连点也不会造成并发风暴。"""

    finished = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def start(self, kind: str, fn):
        self._pool.start(_SourceTask(self, kind, fn))


class SourcePage(QWidget):
    """图源仓库：搜索 yck2026 的图源索引，校验后一键加成自定义站点规则。

    校验通过写入的规则就是 `config.user_rules` 里的一条，会出现在「设置」页，
    和用户手工「添加网站」得到的规则完全一样。
    """

    open_browser = Signal(str)   # 请求用内置浏览器打开某个网址
    added = Signal(int)          # 成功写入一条规则（携带图源 sid）

    # 与 _build() 里的表头顺序一一对应
    COL_NAME, COL_SITE, COL_UPDATED, COL_UPLOADER, COL_DOWNLOADS, COL_SEARCH = range(6)

    HEADERS = ("名称", "网址", "更新时间", "分享者", "下载量", "支持搜索")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []          # list[ycksource.SourceEntry]
        self._pending_sid = 0
        self._pending_name = ""
        self._busy = False             # 有活儿在池子里跑，按钮全锁
        self._status = ""
        self._worker = _SourceWorker(self)
        self._worker.finished.connect(self._on_work)
        self._build()
        # 构造期只查「模块在不在」，不联网：主窗口启动时这页就已经建好了，
        # 在这里发请求等于每次开程序都白等一次图源站。
        self._apply_module_state()

    # ---- 构建 ----
    def _build(self):
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("关键词："))
        self.ed = QLineEdit()
        self.ed.setPlaceholderText("留空看全部；输入站名或标题关键词后回车")
        self.ed.returnPressed.connect(self.refresh)
        top.addWidget(self.ed, 1)
        top.addWidget(QLabel("排序："))
        self.cb_order = QComboBox()
        self.cb_order.addItem("按更新时间", "time")
        self.cb_order.addItem("按下载量", "down")
        # 必须在 addItem 之后再连：插第一项时 currentIndex 会从 -1 变 0 并触发
        # 一次信号，构造期就发网络请求了。
        self.cb_order.currentIndexChanged.connect(lambda _i: self.refresh())
        top.addWidget(self.cb_order)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新读取图源索引（走后台线程，不会卡界面）")
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)

        self.tbl = QTableWidget(0, len(self.HEADERS))
        self.tbl.setHorizontalHeaderLabels(list(self.HEADERS))
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.itemSelectionChanged.connect(self._on_selection)
        hh = self.tbl.horizontalHeader()
        for col in (self.COL_NAME, self.COL_SITE):
            hh.setSectionResizeMode(col, QHeaderView.Stretch)
        for col in (self.COL_UPDATED, self.COL_UPLOADER,
                    self.COL_DOWNLOADS, self.COL_SEARCH):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        lay.addWidget(self.tbl, 1)

        bottom = QHBoxLayout()
        self.btn_add = QPushButton("验证并添加")
        self.btn_add.setEnabled(False)
        self.btn_add.setToolTip("校验该图源能否被解析（会真下一张图），通过后写入自定义站点规则")
        self.btn_add.clicked.connect(self._verify)
        bottom.addWidget(self.btn_add)
        self.btn_browser = QPushButton("在浏览器中打开")
        self.btn_browser.setEnabled(False)
        self.btn_browser.setToolTip("用应用内置浏览器打开该图源的站点页面")
        self.btn_browser.clicked.connect(self._open_in_browser)
        bottom.addWidget(self.btn_browser)
        bottom.addStretch(1)
        lay.addLayout(bottom)

        self.lb_status = QLabel("")
        self.lb_status.setWordWrap(True)
        self.lb_status.setStyleSheet("color:#666")
        lay.addWidget(self.lb_status)

    # ---- 状态 ----
    @property
    def status_msg(self) -> str:
        return self._status

    @status_msg.setter
    def status_msg(self, text: str) -> None:
        """赋值必须同时落到界面上：这个项目吃过「只写属性没人看」的亏。"""
        self._status = text or ""
        lb = getattr(self, "lb_status", None)
        if lb is not None:
            try:
                lb.setText(self._status)
            except RuntimeError:
                pass

    def _apply_module_state(self) -> bool:
        """模块可用返回 True；不可用则把整页降级成人话提示。"""
        if get_ycksource() is not None:
            return True
        reason = ycksource_error() or "未知原因"
        self.status_msg = f"图源模块不可用：{reason}（本页功能已禁用，其余页面不受影响）"
        for w in (self.ed, self.cb_order, self.btn_refresh, self.btn_add):
            w.setEnabled(False)
        self.btn_browser.setEnabled(False)
        self.tbl.setRowCount(0)
        self._rows = []
        return False

    def _refresh_buttons(self):
        ok_mod = get_ycksource() is not None
        has_row = self._selected() is not None
        self.btn_refresh.setEnabled(ok_mod and not self._busy)
        self.btn_add.setEnabled(ok_mod and not self._busy and has_row)
        self.btn_browser.setEnabled(has_row)

    # ---- 取数 ----
    def ensure_loaded(self):
        """首次进入本页时自动拉一次列表；已有数据或正在跑就不重复请求。"""
        if self._rows or self._busy:
            return
        if get_ycksource() is None:
            self._apply_module_state()
            return
        self.refresh()

    def refresh(self):
        """按当前关键词/排序重读图源索引（后台线程）。"""
        if self._busy:
            return
        if not self._apply_module_state():
            return
        self.btn_browser.setEnabled(False)
        mod = get_ycksource()
        keys = self.ed.text().strip()
        order = self.cb_order.currentData() or "time"
        self._busy = True
        self.btn_refresh.setEnabled(False)
        self.btn_add.setEnabled(False)
        self.status_msg = "正在读取图源列表…" + (f"（关键词：{keys}）" if keys else "（全部）")
        self._worker.start(
            "list", lambda: mod.list_sources(page=1, keys=keys, order=order))

    def _selected(self):
        """取当前选中行挂的 SourceEntry（挂在第 0 列的 UserRole 上）。

        必须同时看「有没有选中」和 currentRow：QTableWidget 清空选中后
        currentRow 仍停在原处，只看 currentRow 的话，界面上什么都没选、
        「验证并添加」却还亮着，点了会去校验上一次那行 —— 用户理解的
        「没选中就该点不动」会被打破。
        """
        row = self.tbl.currentRow()
        if row < 0:
            return None
        sm = self.tbl.selectionModel()
        if sm is not None and not sm.hasSelection():
            return None
        it = self.tbl.item(row, self.COL_NAME)
        return it.data(Qt.UserRole) if it else None

    def _on_selection(self):
        self._refresh_buttons()

    # ---- 列表回来 ----
    def _on_work(self, payload: dict):
        kind = (payload or {}).get("kind")
        if kind == "list":
            self._on_list(payload)
        elif kind == "check":
            self._on_check(payload)
        elif kind == "add":
            self._on_add(payload)

    def _on_list(self, payload: dict):
        self._busy = False
        if payload.get("error"):
            self._refresh_buttons()
            self.status_msg = (f"读取图源列表失败：{payload['error']}"
                               f"（检查网络后点「刷新」重试）")
            return
        rows = list(payload.get("result") or [])
        self._rows = rows
        self._fill(rows)
        self._refresh_buttons()
        if not rows:
            self.status_msg = "没有匹配的图源，换个关键词再试。"
            return
        n_search = sum(1 for e in rows if getattr(e, "can_search", False))
        self.status_msg = (f"共 {len(rows)} 个图源，其中 {n_search} 个支持站内搜索。"
                           f"选中一行后点「验证并添加」。")

    def _fill(self, rows: list):
        self.tbl.setRowCount(0)
        self.tbl.setRowCount(len(rows))
        for r, e in enumerate(rows):
            cells = (
                (self.COL_NAME, getattr(e, "name", "") or f"#{getattr(e, 'sid', '')}"),
                (self.COL_SITE, getattr(e, "site", "") or ""),
                (self.COL_UPDATED, getattr(e, "updated", "") or ""),
                (self.COL_UPLOADER, getattr(e, "uploader", "") or ""),
                (self.COL_DOWNLOADS, str(getattr(e, "downloads", 0) or 0)),
                (self.COL_SEARCH, "是" if getattr(e, "can_search", False) else "否"),
            )
            for col, text in cells:
                it = QTableWidgetItem(str(text))
                if col == self.COL_DOWNLOADS:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == self.COL_NAME:
                    it.setData(Qt.UserRole, e)
                self.tbl.setItem(r, col, it)

    # ---- 校验并添加 ----
    def _verify(self):
        e = self._selected()
        if e is None:
            QMessageBox.information(self, "提示", "请先选中一行图源")
            return
        if self._busy:
            return
        mod = get_ycksource()
        if mod is None:
            self._apply_module_state()
            return
        sid = int(getattr(e, "sid", 0) or 0)
        self._pending_sid = sid
        self._pending_name = getattr(e, "name", "") or f"#{sid}"
        self._busy = True
        self.btn_add.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.btn_browser.setEnabled(False)
        self.status_msg = (f"正在校验「{self._pending_name}」…"
                           f"（会真下一张图，可能要十几秒；界面不会卡住）")
        self._worker.start("check", lambda: mod.check_source(sid, deep=True))

    def _on_check(self, payload: dict):
        if payload.get("error"):
            self._busy = False
            self._refresh_buttons()
            self.status_msg = f"校验失败：{payload['error']}"
            return
        res = payload.get("result")
        if res is None:
            self._busy = False
            self._refresh_buttons()
            self.status_msg = "校验失败：没有拿到结果"
            return
        if not getattr(res, "ok", False):
            self._busy = False
            self._refresh_buttons()
            # 失败原因直接透传 ycksource 的原文（它已经分好「加密源 / 语法不支持 /
            # 无法转换」几类）。GUI 再造一套文案只会和引擎说法对不上。
            self.status_msg = ("不可添加：" + (getattr(res, "message", "") or "校验未通过")
                               + f"（{getattr(res, 'status', '') or 'fail'}）")
            return
        ch = int(getattr(res, "chapters", 0) or 0)
        im = int(getattr(res, "images", 0) or 0)
        self.status_msg = f"可转换：{ch} 话 / {im} 张图 —— 正在写入自定义站点规则…"
        # 只有 ok=True 才写库；add_checked_source 内部会再校验一次并调
        # probe.add_user_rule，写库失败时它会把 ok 改回 False 并给出 message。
        mod = get_ycksource()
        sid = self._pending_sid
        self._worker.start("add", lambda: mod.add_checked_source(sid, deep=True))

    def _on_add(self, payload: dict):
        self._busy = False
        self._refresh_buttons()
        res = payload.get("result")
        if payload.get("error") or res is None or not getattr(res, "ok", False):
            why = (payload.get("error") or getattr(res, "message", "")
                   or "写入配置失败")
            self.status_msg = f"校验通过，但添加失败：{why}"
            return
        name = getattr(res, "site_name", "") or self._pending_name
        ch = int(getattr(res, "chapters", 0) or 0)
        im = int(getattr(res, "images", 0) or 0)
        self.status_msg = (f"已添加「{name}」（可转换：{ch} 话 / {im} 张图），"
                           f"可在设置页查看")
        log.info("图源仓库添加自定义站点 %s（sid=%s）", name, self._pending_sid)
        self.added.emit(self._pending_sid)

    def _open_in_browser(self):
        e = self._selected()
        if e is None:
            return
        # 想给用户看的是站点页面；没有站点网址才退回图源 JSON 地址。
        url = getattr(e, "site", "") or getattr(e, "json_url", "")
        if not url:
            QMessageBox.information(self, "提示", "这一行没有可打开的网址")
            return
        self.open_browser.emit(url)


# ================================================================== 内置浏览器页

DEFAULT_HOME_URL = "https://www.yck2026.fun/yiciyuan/tuyuan/index.html"


def normalize_url(text: str) -> str:
    """给用户的输入补上 https://（地址栏里没人愿意每次打协议头）。

    但别把本来就带协议头的东西弄坏：`about:blank`、`mailto:`、`data:`、`file:///`
    这些原样交给 WebEngine。判断依据是冒号后面跟的不是端口号——`localhost:8080`
    要补成 `https://localhost:8080`，`about:blank` 要原样保留。
    """
    t = (text or "").strip()
    if not t:
        return ""
    if "://" in t:
        return t
    head, sep, rest = t.partition(":")
    looks_like_scheme = (
        bool(sep) and bool(rest)
        and not rest[:1].isdigit()      # localhost:8080 / 1.2.3.4:80 是主机:端口
        and "/" not in head
        and "." not in head             # example.com:xx 当主机看，不当 scheme
        and head.lower() not in ("http", "https")
    )
    if looks_like_scheme:
        return t
    return "https://" + t.lstrip("/")


class BrowserPage(QWidget):
    """应用内置浏览器（QtWebEngine），用来在应用里看站点页面。

    两件必须记住的事：

    1. **WebEngine 懒加载**。构造本页时一行 WebEngine 的代码都不许碰，真正
       import + 建视图推迟到第一次显示本页（showEvent）或第一次 open_url()。
       打包成 onefile 后 import QtWebEngine 要解包上百兆运行库，提前做会把
       主窗口首屏从 ~0.8s 拖到 ~3.4s。
    2. **模块可能被裁掉**。import 失败（或建视图失败）时给一句人话提示，
       其余页面照常用，绝不能崩。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = None            # 真 QWebEngineView，懒加载后才有
        self._pending_url = ""      # 视图还没建好时先记住要打开的地址
        self._build()

    # ---- 构建（只摆控件，不碰 WebEngine）----
    def _build(self):
        lay = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.btn_back = QPushButton("←")
        self.btn_back.setToolTip("后退")
        self.btn_back.clicked.connect(self.back)
        self.btn_fwd = QPushButton("→")
        self.btn_fwd.setToolTip("前进")
        self.btn_fwd.clicked.connect(self.forward)
        self.btn_reload = QPushButton("刷新")
        self.btn_reload.clicked.connect(self.reload)
        self.btn_home = QPushButton("回首页")
        self.btn_home.setToolTip(DEFAULT_HOME_URL)
        self.btn_home.clicked.connect(self.go_home)
        for b in (self.btn_back, self.btn_fwd, self.btn_reload, self.btn_home):
            b.setEnabled(False)
            bar.addWidget(b)

        self.ed_url = QLineEdit(DEFAULT_HOME_URL)
        self.ed_url.setPlaceholderText("输入网址后回车")
        self.ed_url.returnPressed.connect(self._on_go)
        bar.addWidget(self.ed_url, 1)

        self.pg = QProgressBar()
        self.pg.setRange(0, 100)
        self.pg.setValue(0)
        self.pg.setFixedWidth(140)
        self.pg.setTextVisible(False)
        bar.addWidget(self.pg)
        lay.addLayout(bar)

        self.placeholder = QLabel("正在准备内置浏览器…")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet("color:#888")
        # 建视图前先占着位置，建好后整体换成视图（QStackedWidget 换页最干净，
        # 不用动布局里的 item 顺序）。
        self._host = QStackedWidget()
        self._host.addWidget(self.placeholder)
        lay.addWidget(self._host, 1)

        self.lb_status = QLabel("首次进入本页才会加载浏览器内核（避免拖慢启动）")
        self.lb_status.setWordWrap(True)
        self.lb_status.setStyleSheet("color:#666")
        lay.addWidget(self.lb_status)

    # ---- 懒加载 ----
    def showEvent(self, ev):
        super().showEvent(ev)
        # 懒加载的落点：只有这一页真的显示出来，才去碰 WebEngine。
        self.ensure_view()

    def ensure_view(self) -> bool:
        """建好 QWebEngineView（只建一次）。失败返回 False 并显示人话提示。"""
        if self.view is not None:
            return True
        cls = get_webengine()
        if cls is None:
            self._show_unavailable()
            return False
        try:
            self.view = cls(self._host)
        except Exception as exc:  # noqa: BLE001
            global _web_err
            _web_err = f"{type(exc).__name__}: {exc}"
            log.warning("创建内置浏览器失败：%s", _web_err)
            self.view = None
            self._show_unavailable()
            return False

        v = self.view
        v.loadStarted.connect(self._on_load_started)
        v.loadProgress.connect(self._on_progress)
        v.loadFinished.connect(self._on_load_finished)
        v.urlChanged.connect(self._on_url_changed)
        self._host.addWidget(v)
        self._host.setCurrentWidget(v)
        self.pg.setValue(0)
        self.lb_status.setText("正在加载首页…")

        target = self._pending_url or normalize_url(self.ed_url.text()) or DEFAULT_HOME_URL
        self._pending_url = ""
        self._navigate(target)
        self._sync_buttons()
        return True

    def _show_unavailable(self):
        reason = _web_err or "未知原因"
        self.placeholder.setText(
            "内置浏览器不可用。\n\n"
            f"原因：{reason}\n\n"
            "其余功能不受影响；需要看网页时可以用系统浏览器打开这个网址。\n"
            "（打包版出现这个提示＝PyInstaller 没有把 PySide6.QtWebEngineWidgets "
            "和 QtWebEngineProcess 打进去。）"
        )
        self.lb_status.setText(f"内置浏览器不可用：{reason}")
        for b in (self.btn_back, self.btn_fwd, self.btn_reload, self.btn_home):
            b.setEnabled(False)

    # ---- 对外入口 ----
    def open_url(self, url: str) -> None:
        """公开方法：详情页/图源页把网址丢进来，在应用内打开。"""
        url = (url or "").strip()
        if not url:
            return
        self.ed_url.setText(url)
        if self.view is None:
            self._pending_url = url
            self.ensure_view()   # 失败时内部已经给了提示，网址留在地址栏可复制
            return
        self._navigate(url)

    # ---- 导航 ----
    def _navigate(self, url: str):
        target = normalize_url(url)
        if not target:
            return
        self.ed_url.setText(target)
        if self.view is None:
            self._pending_url = target
            self.ensure_view()
            return
        try:
            self.view.setUrl(QUrl(target))
        except Exception as exc:  # noqa: BLE001
            self.lb_status.setText(f"打开失败：{type(exc).__name__}: {exc}")

    def _on_go(self):
        self._navigate(self.ed_url.text())

    def back(self):
        self._history("back")

    def forward(self):
        self._history("forward")

    def reload(self):
        if self.view is None:
            self.ensure_view()
            return
        try:
            self.view.reload()
        except Exception as exc:  # noqa: BLE001
            self.lb_status.setText(f"刷新失败：{type(exc).__name__}: {exc}")

    def go_home(self):
        self._navigate(DEFAULT_HOME_URL)

    def _history(self, which: str):
        if self.view is None:
            return
        try:
            h = self.view.history()
            if which == "back":
                h.back()
            else:
                h.forward()
        except Exception as exc:  # noqa: BLE001
            self.lb_status.setText(f"导航失败：{type(exc).__name__}: {exc}")
        self._sync_buttons()

    def _sync_buttons(self):
        can = self.view is not None
        back = fwd = False
        if can:
            try:
                h = self.view.history()
                back, fwd = bool(h.canGoBack()), bool(h.canGoForward())
            except Exception:  # noqa: BLE001 - 桩视图没有 history() 也要能用
                back = fwd = False
        self.btn_back.setEnabled(can and back)
        self.btn_fwd.setEnabled(can and fwd)
        self.btn_reload.setEnabled(can)
        self.btn_home.setEnabled(can)

    # ---- 视图回调（签名必须和 WebEngine 的信号一致）----
    def _on_load_started(self):
        self.pg.setValue(0)
        self.lb_status.setText("正在加载…")

    def _on_progress(self, p: int):
        p = int(p)
        self.pg.setValue(p)
        if p < 100:
            self.lb_status.setText(f"正在加载… {p}%")

    def _on_load_finished(self, ok: bool):
        self.pg.setValue(100 if ok else 0)
        self.lb_status.setText(
            "加载完成" if ok else "加载失败：网址不可达或当前没有网络")
        self._sync_buttons()

    def _on_url_changed(self, qurl):
        u = qurl.toString() if hasattr(qurl, "toString") else str(qurl)
        # 加载失败/跳转过程中 WebEngine 会发一个空 urlChanged；照单全收会把用户
        # 刚输入的地址清空，人就再也复制不出来了。空值直接忽略。
        if u.strip():
            self.ed_url.setText(u)
        self._sync_buttons()


# ==================================================================== 主窗口

# 左侧导航行号 = 右侧 QStackedWidget 索引，二者共用下面这一份定义。
# 以前 _nav() / _goto() 里写的是裸数字（i == 3、_goto(6)…），插页或调序会
# **静默**错位（点了收藏却刷新图源仓库），所以改成常量。
PG_SEARCH, PG_MANGA, PG_TASKS, PG_FAVS, PG_SETTINGS, PG_SOURCES, PG_BROWSER = range(7)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{__app_name_cn__} {__app_name__} v{__version__}")
        self.resize(1120, 760)
        self.mgr = manager()
        self.mgr.start()
        self.svc = ComicService(self.mgr)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.search = SearchPage(self.svc)
        self.manga = MangaPage(self.svc)
        self.tasks = TasksPage()
        self.favs = FavoritesPage(self.svc)
        self.settings = SettingsPage()
        # 这两个页面必须加在最后：导航行号与栈索引一一对应，中间插页会让
        # _nav() 里 `if i == PG_FAVS:` 这类判定错位（原来是硬编码数字，
        # 现在换成下面的 PAGES 元组 + 常量，插页也只改一处）。
        # SourcePage 构造期不联网；BrowserPage 构造期不碰 WebEngine（懒加载）。
        self.sources = SourcePage()
        self.browser = BrowserPage()

        self.search.open_comic.connect(self.show_comic)
        self.favs.open_comic.connect(self.show_comic)
        self.manga.enqueue.connect(self.add_tasks)
        # 详情页 / 图源页 → 内置浏览器
        self.manga.open_browser.connect(self.open_in_browser)
        self.sources.open_browser.connect(self.open_in_browser)
        # 图源校验通过并写库后，设置页要立刻能看见这条自定义站点
        self.sources.added.connect(self._on_source_added)
        # 收藏页的更新检查结果（后台线程 -> 主线程）
        br = bridge()
        br.update_partial.connect(self.favs.on_update_partial)
        br.update_done.connect(self.favs.on_update_done)

        nav = QListWidget()
        nav.setFixedWidth(132)
        nav.setFrameShape(QFrame.NoFrame)
        self.nav = nav
        # 顺序 = 栈索引，**只在这里定义一次**。(导航文字, 页面) 元组同时喂给
        # 导航栏和堆栈，顺序天然一致；插页/调序只改这一处，下面的 PG_* 常量自动跟着变。
        pages = (("搜索 / 打开", self.search), ("漫画详情", self.manga),
                 ("下载任务", self.tasks), ("收藏", self.favs),
                 ("设置", self.settings), ("图源仓库", self.sources),
                 ("内置浏览器", self.browser))
        for name, page in pages:
            nav.addItem(name)
            page._nav_label = name
        nav.currentRowChanged.connect(self._nav)
        for _name, page in pages:
            self.stack.addWidget(page)

        wrap = QWidget()
        hl = QHBoxLayout(wrap)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(nav)
        hl.addWidget(self.stack, 1)
        self.setCentralWidget(wrap)

        self._build_menu()
        # 首屏落在搜索页。同步设一次就够了：栈里五个页面此时都已 addWidget，
        # currentRowChanged -> _nav 立刻生效。早期版本还挂了个 50ms 延时兜底，
        # 结果把用户/测试刚切走的页面又抢回第 0 页（点「下载」跳任务页会被弹回搜索页）。
        nav.setCurrentRow(0)

    def _build_menu(self):
        m = self.menuBar()
        f = m.addMenu("文件")
        f.addAction("打开下载目录", lambda: os.startfile(config().download_dir))
        f.addAction("退出", self.close)
        h = m.addMenu("帮助")
        h.addAction("使用文档", self._open_doc)
        h.addAction("关于", lambda: QMessageBox.about(
            self, "关于",
            f"{__app_name_cn__}（{__app_name__}）v{__version__}\n"
            "通用漫画批量下载器\n支持本地规则解析，站点改版时可自行编辑 rules/*.json。"))

    def _open_doc(self):
        p = Path(__file__).resolve().parents[2] / "使用文档.md"
        if p.exists():
            os.startfile(p)
        else:
            webbrowser.open("https://github.com/topics/manga-downloader")

    def _nav(self, i):
        self.stack.setCurrentIndex(i)
        if i == PG_FAVS:
            self.favs.reload()
        elif i == PG_SOURCES:
            # 图源仓库第一次进来才联网；已经拉过（或正忙）时 ensure_loaded 自己是空操作。
            # 放在这里而不是构造函数里，是为了不给启动路径加一次网络请求。
            self.sources.ensure_loaded()

    def open_in_browser(self, url: str) -> None:
        """把网址丢给内置浏览器页打开（详情页/图源页的「在浏览器中打开」）。"""
        # 先 open_url 再切页：这样懒加载建视图时 _pending_url 已经是目标地址，
        # 只会加载一次（反过来会先加载首页再跳一次，白跑一趟网络）。
        self.browser.open_url(url)
        self._goto(PG_BROWSER)

    def _on_source_added(self, sid: int) -> None:
        """图源页写库成功后刷新设置页的站点表。"""
        try:
            self.settings.reload()
        except Exception as exc:  # noqa: BLE001 - 刷新失败不该把添加结果吃掉
            log.warning("设置页刷新失败：%s", exc)

    def show_comic(self, c: Comic):
        self._goto(PG_MANGA)
        if c.url:
            self.manga.load(c.url)

    def _goto(self, i: int) -> None:
        """切页时左侧导航高亮要跟着走。

        只 setCurrentIndex 会出现「内容页是任务页、高亮却还停在搜索页」的错位，
        而下次点导航又会把内容切回高亮那一页，看着像程序自己跳回去了。
        这里把两边一起对齐，并且避开导航自己的信号回环。
        """
        if 0 <= i < self.stack.count() and self.nav.currentRow() != i:
            self.nav.blockSignals(True)
            try:
                self.nav.setCurrentRow(i)
            finally:
                self.nav.blockSignals(False)
        self.stack.setCurrentIndex(i)

    def add_tasks(self, comic: Comic, chapters: list[Chapter]):
        self.mgr.enqueue_comic(comic, chapters)
        self._goto(PG_TASKS)
        self.tasks.reload()
        QMessageBox.information(self, "已加入队列",
                                f"已加入 {len(chapters)} 个章节，正在下载。\n可在「下载任务」查看进度。")


def _wrap(name: str, parent) -> QWidget:
    w = QWidget(parent)
    QLabel(name, w)
    return w