# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller 打包配置：生成单文件 exe（漫卷 MangaKit）。

用法（必须在仓库根目录 mangakit/ 下执行）：
    python -m PyInstaller --noconfirm mangakit.spec
产物：
    dist/MangaKit.exe

双击 exe 即可运行，不需要任何外挂 bat —— 图标、版本资源、启动自检
（下载目录可写性）都已经在本配置和 mangakit/main.py 里做进 exe。
（启动画面在 v0.3.1 移除，原因见下方 EXE() 前的长注释。）

几个「不知道就会踩坑」的点，改本文件前先读：

1. QtWebEngine 不能排除。excludes 里曾经有 QtWebEngineCore /
   QtWebEngineWidgets / QtWebChannel，那是「内置浏览器页」还没做的年代留下的。
   浏览器页需要它们，已从 excludes 移除。QtWebEngineQuick / QtQml / QtQuick
   系列继续排除：它们只是 Quick(QML) 版实现，Widgets 版用不到，排掉省体积。
2. devtools_resources 必须过滤。PySide6 的 hook 会把
   qtwebengine_devtools_resources.debug.pak 收进 datas，实测 83.4MB，
   打包应用根本不加载它（只给 QtWebEngine 自带的 F12 远程调试用）。
3. runtime_tmpdir 用 `%LOCALAPPDATA%\MangaKit\run`（带环境变量的绝对路径）。
   环境变量 token 存进 exe 的是字面量，**在运行期由 bootloader 展开**，并且
   它会自己把缺失的父目录链建出来。选它而不是相对路径 `.mkrun`，是因为
   相对路径按「运行时的 cwd」解析而不是 exe 所在目录：从别处的快捷方式
   启动就会解包到那个目录，实测用 WMI 启动时 cwd=system32，解包目录直接
   落到了 C:\Windows\System32\.mkrun。绝对路径不依赖 cwd，且每用户独立、
   不挤 %TEMP%（绕开 %TEMP% 条目过多导致的 PYI-3216）。详见 EXE() 里的矩阵表。
4. upx=True 但 upx 不在 PATH，实际不生效（原样保留，免得以后装了 upx 反而漏改）。
5. QtWebEngine 在打包态必须关沙箱，旗标要在「任何 import PySide6.QtWebEngine*
   之前」设置。这段代码在 mangakit/main.py 的 _configure_webengine_sandbox()，
   不能在别处补，否则渲染进程起不来（表现为页面白屏 / loadFinished ok=False）。
"""

import os

block_cipher = None

# 仓库根目录（本 spec 所在处）。用 SPECPATH 而不是 cwd，保证从任意目录
# 调用 pyinstaller 时资源路径都正确。
_HERE = os.path.abspath(SPECPATH or os.path.dirname(os.path.abspath(__file__)))
_ASSETS = os.path.join(_HERE, "assets")

# 站点规则以数据文件形式打包（用户可在运行时目录里覆盖/新增）
datas = [
    (os.path.join("mangakit", "rules"), os.path.join("mangakit", "rules")),
    # 图标与 splash 图一并打进 datas：图标已作为 exe 资源，这里留一份便于
    # 「关于」页读取；splash.png 自 v0.3.1 起不再被 bootloader 使用（启动画面
    # 已移除），仍随包分发以便将来恢复或做皮肤。
    (_ASSETS, "assets"),
]

hiddenimports = [
    "mangakit.gui.main_window",
    "mangakit.gui.bridge",
    "mangakit.adapters",
    "mangakit.crypto",
    "mangakit.exporter",
    "img2pdf",
    "lxml.etree",
    "lxml._elementpath",
    "lxml.cssselect",
    "httpx",
    # HTTP/2 走 h2 包。httpx>=0.28 里已经没有 httpx._http2 这个模块了
    # （0.27 及更早才有），写错会让 PyInstaller 直接报
    # "Hidden import 'httpx._http2' not found" 而中断打包。
    "h2",
    # 内置浏览器页要用的 QtWebEngine。这两个是「动态 import」的
    # （只有用户切到浏览器页才加载，见 gui/main_window.py），
    # 静态分析扫不到，必须显式声明，否则打包后一点就 ImportError。
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    # 图源仓库页用的异次元源解析模块。它在 gui/main_window.py 里是
    # 「函数内 import」（见 get_ycksource()），静态分析同样扫不到；
    # 漏了它，打包后「图源仓库」页会显示「图源模块不可用」。
    "mangakit.ycksource",
]

# PySide6 里用不到的重型 Qt 模块，排除可让 exe 小一大截。
# 注意：不要往这里加 QtWebEngineCore / QtWebEngineWidgets / QtWebChannel ——
# 内置浏览器页需要它们，加了打包能过、点浏览器页必崩。
excludes = [
    "tkinter",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DPhysics", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    # Quick/QML 版 WebEngine：Widgets 版不需要，排除后不影响 QWebEngineView
    "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineQuickDelegatesQml",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtQuickControls2",
    "PySide6.QtWebSockets",
    "PySide6.QtWebSocketsLegacy", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtHttpServer",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "matplotlib", "numpy", "pandas", "scipy", "pytest", "setuptools",
    "pip", "wheel", "IPython", "notebook", "sqlite3.__main__",
]

a = Analysis(
    ["mangakit/__main__.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# 砍掉 devtools 资源（83.4MB）。这些 .pak 只给 QtWebEngine 的远程调试用，
# 打包应用永远不加载；不砍会让 exe 白胖 80MB+。
a.datas = [d for d in a.datas if "devtools_resources" not in d[0]]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ────────────────────────────────────────────────────────────────────────
# 启动画面：**已移除**（v0.3.1 起）。
#
# 原来这里有 `splash = Splash(assets/splash.png, ...)`，并在 EXE() 里传
# `splash, splash.binaries`。移除的两个原因：
#
# 1) 用户反馈「打开程序时弹出的那个 tk 小窗口能否不显示」。PyInstaller 的
#    splash 在 Windows 上用 Tcl/Tk 实现（`excludes` 里的 "tkinter" 只排 Python
#    层绑定，挡不住 bootloader 自带的 splash），所以它必然以 Tk 窗口形式出现。
#
# 2) 它其实一直是**坏的**：`text_font="Microsoft YaHei UI"` 带空格，
#    PyInstaller 的模板 `splash_templates.py:154` 直接写成
#        font create myFont -family Microsoft YaHei UI -size 10
#    没有加引号，Tcl 在脚本第 30 行左右就抛
#        bad option "YaHei": must be -family, -size, -weight, ...
#    而 `wm overrideredirect` / `wm geometry` 在脚本末尾才执行，于是 Tk 退回
#    默认窗口：**216x239、带标题栏、标题 `tk`、内容全空白**，横在主窗口前面
#    约 5 秒。实测复跑内嵌脚本确认（`_mk_res/Splash-00.res` → 真 Tk 解释器）：
#    给字体名加引号后整段即通过（600x340、overrideredirect=1、图正常加载）。
#
# 若将来要恢复启动画面：把下面这段 Splash(...) 和 EXE() 里的两个参数加回来，
# 并把 text_font 改成带引号的写法 —— `text_font='"Microsoft YaHei UI"'`
# （模板是原样拼接，需要自己带上引号）。代价：exe 增大约 1.7MB（tcl86t.dll
# 905KB + tk86t.dll 707KB + Splash-00.res 62KB + splash.png 60KB）。
#
# 移除后：onefile 解包 + import 那 5 秒左右完全没有窗口，主窗口直接出现。
# assets/splash.png 仍随 datas 打包（供「关于」页等后续使用），不影响启动。
# ────────────────────────────────────────────────────────────────────────

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="MangaKit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    # ────────────────────────────────────────────────────────────────────
    # runtime_tmpdir：为什么用 `%LOCALAPPDATA%\MangaKit\run`
    #
    # 先说结论：环境变量 token 会被**运行期的 bootloader** 展开，而且它会自己
    # 把缺失的父目录链建出来。所以带 %VAR% 的绝对路径是可用且最稳的写法。
    #   * makespec.py:34 `def portable_filepath(path)` 只做分隔符转换、不碰
    #     %VAR%，token 原样进 exe TOC；
    #   * bootloader 二进制里有 `ExpandEnvironmentStringsW` 符号。
    # 实测（探针 envtmp2.exe / envtmp4.exe，WMI 启动）：
    #   r'%LOCALAPPDATA%\MangaKit'   → MEIPASS=C:\Users\Administrator\AppData\Local\MangaKit\_MEI000034a42
    #   r'%TEMP%\MangaKitRun'        → MEIPASS=C:\Users\Administrator\AppData\Local\Temp\MangaKitRun\_MEI00000c802
    # 反证：exe 里搜 `%LOCALAPPDATA%` 命中 1 次、展开后的 `AppData\Local\MangaKit`
    # 命中 0 次 —— 存的确实是字面 token，展开发生在运行期。
    #
    # 为什么**不用**相对路径 `.mkrun`：它按「运行时的 cwd」解析，不是 exe 目录。
    # 实测用 WMI 启动时子进程 cwd=C:\Windows\System32，解包目录直接落到
    # `C:\Windows\System32\.mkrun\_MEI000022202`（已清理）。从别处快捷方式、
    # 计划任务、或任何 cwd 不同的方式启动都会漂到意外位置，甚至无权写入。
    # 绝对路径不依赖 cwd，且每用户独立、不挤 %TEMP%（绕开 %TEMP% 条目过多
    # 导致的 PYI-3216 —— 用户最初那个 "Could not create temporary directory!"
    # 的成因）。
    #
    # ⚠️ 历史上有一整张「%TEMP% / %LOCALAPPDATA% 绝对路径全部失败」的矩阵，
    # 那些结果**全部是 DSH pwsh 沙箱造成的假象**，不是 PyInstaller 行为：同一个
    # envtmp2.exe 在沙箱内 Start-Process 失败退出，改用
    # `Invoke-CimMethod -ClassName Win32_Process -MethodName Create`（进程由 WMI
    # 服务创建、不继承沙箱，等价于用户双击）启动即成功。已排除「权限」假设：
    # 原生 C 程序直接调 CreateDirectoryW 在 %TEMP%、%LOCALAPPDATA%、工作区
    # 三处都成功。**别再照那张旧矩阵"改成 .mkrun"，也不要退回 None。**
    #
    # 教训（写下来免得后人重踩）：在 DSH 沙箱里用 Start-Process / & 启动 exe
    # 验证行为不可信，必须用上面那个 WMI 方式，并看 exe 同目录的日志文件。
    # ────────────────────────────────────────────────────────────────────
    runtime_tmpdir=r'%LOCALAPPDATA%\MangaKit\run',
    console=False,          # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(_ASSETS, "mangakit.ico"),
    version=os.path.join(_ASSETS, "version_info.txt"),
)
