@echo off
rem ---------------------------------------------------------------
rem  漫卷 MangaKit —— 备用启动器（兼容旧习惯用，不是必须的）
rem
rem  正常用法：直接双击 MangaKit.exe 就行，不需要本文件。
rem
rem  以前这个 bat 承担了三件事，现在都已经搬进 exe 内部，所以这里只剩
rem  一层兜底，防止有人还在按老习惯从这里启动：
rem    1) 把 TEMP/TMP 改向 exe 旁边的目录 —— 现在解包位置由 exe 自己决定
rem       （mangakit.spec 的 runtime_tmpdir = %LOCALAPPDATA%\MangaKit\run），
rem       bat 再改 TEMP 已经影响不到解包，只能影响 Python 层和子进程。
rem    2) 清理上次崩溃留下的 _MEI* 解包残留 —— 现在解包落在
rem       %LOCALAPPDATA%\MangaKit\run 下，这里顺手清一次（只有在
rem       MangaKit.exe 没在运行时才清，否则会把正在跑的实例删坏）。
rem    3) 探测下载目录能不能写 —— 现在由 exe 启动时自检并弹窗说明
rem       （mangakit/main.py 的 _startup_preflight），双击 exe 一样受保护，
rem       不再需要绕过 bat 才能看到提示。
rem
rem  保留本文件还有一点好处：可以填参数转发给 exe（如 --url / --selftest）。
rem ---------------------------------------------------------------
setlocal

rem 1) 找 exe：优先同目录，其次 dist\（本项目源码目录下的布局）
set "EXE=%~dp0MangaKit.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\MangaKit.exe"
if not exist "%EXE%" (
    echo [漫卷] 没找到 MangaKit.exe（不在本文件旁边，也不在 dist\ 下）。
    echo        请直接双击 MangaKit.exe 启动。
    pause
    exit /b 1
)

rem 2) 清理解包残留（上次崩溃/强杀留下的 _MEI*）。必须确认没有实例在跑：
rem    正在运行的实例还把 _MEI* 当自己的运行目录用，删了会当场崩。
set "MEIDIR=%LOCALAPPDATA%\MangaKit\run"
tasklist /FI "IMAGENAME eq MangaKit.exe" 2>nul | findstr /I "MangaKit.exe" >nul
if errorlevel 1 (
    if exist "%MEIDIR%" for /d %%i in ("%MEIDIR%\_MEI*") do rd /s /q "%%i" 2>nul
)

rem 3) 起 exe。用 start 是为了让 bat 自己立刻退出（不留黑框在任务栏）
start "" "%EXE%" %*
exit /b 0
