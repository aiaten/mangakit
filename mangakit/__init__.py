"""mangakit — 漫画下载器（对标 MangaCon v3.0.6.28 的开源重构版）

设计目标
--------
1. 覆盖国内外主流漫画站，规则外置为可编辑的 JSON，新增站点无需改代码。
2. 下载队列支持并发、重试、断点续传、任务持久化，程序重启可继续。
3. 导出 CBZ / PDF / 长条图 / EPUB。
4. PySide6 原生 GUI。
"""

__version__ = "0.3.3"
__app_name__ = "MangaKit"
__app_name_cn__ = "漫卷"