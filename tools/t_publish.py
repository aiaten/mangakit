#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键发布：add -> commit -> push -> tag -> gh release create（附 dist\\MangaKit.exe）。

用法
----
    python tools\\t_publish.py --dry-run        # 只打印将执行的命令，不改动任何东西
    python tools\\t_publish.py                  # 真跑一遍，提交信息自动生成（含时间）
    python tools\\t_publish.py -m "修好搜索翻页"  # 自定义提交信息
    python tools\\t_publish.py --tag v0.2.0     # 覆盖 tag（默认取 __version__ 加 v 前缀）
    python tools\\t_publish.py --no-release     # 只提交推送打 tag，不建 Release

设计要点
--------
1. ``--dry-run`` 绝不执行任何**改动**命令；只读命令（``git status`` / ``rev-parse`` /
   ``gh auth status``）仍然会跑，这样打印出来的计划基于真实仓库状态而不是猜的。
   仓库还没 ``git init`` 过时，dry-run 会打印一份「首次发布」引导计划而不是报错，
   正式执行则明确拒绝并给出初始化命令。
2. 没有改动时优雅退出（退出码 0），不会产出空提交。
3. ``git`` / ``gh`` 都用绝对路径探测：先 ``shutil.which``，再退到本机已知安装位置
   （本机 ``git`` 不在 PATH 上，实测只有 ``C:\\Program Files\\Git\\cmd\\git.exe``）。
4. tag 默认从 ``mangakit/__init__.py`` 的 ``__version__`` 读。
5. 任何一步失败都打印**真实 stderr** 并立刻停下，不静默继续。
6. 凡是 ``gh`` 相关的写法都带 ``--`` 风险规避：exe 路径整体作为位置参数传入。

体积提示：GitHub 对**单个 Git 对象**有 100 MB 硬上限，对 **Release 资产**单文件上限 2 GB。
exe 一律只作为 Release 附件上传，不进 Git 仓库（``.gitignore`` 已排除 ``dist/`` 与 ``*.exe``）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]          # 仓库根（本文件在 tools/ 下）
EXE = ROOT / "dist" / "MangaKit.exe"
LAUNCHER = ROOT / "启动漫卷.bat"                      # README 要求用户配合它启动 exe，必须一起发
PKG_INIT = ROOT / "mangakit" / "__init__.py"
GITIGNORE = ROOT / ".gitignore"

# GitHub 的两条硬限制（写进提示文案里，别改错）
GIT_OBJECT_LIMIT_MB = 100          # 单个 Git 对象上限
RELEASE_ASSET_LIMIT_GB = 2         # Release 单文件资产上限

GIT_FALLBACKS = [
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
]
GH_FALLBACKS = [
    r"C:\Program Files\GitHub CLI\gh.exe",
    r"C:\Program Files (x86)\GitHub CLI\gh.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\gh\gh.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\GitHub CLI\gh.exe"),
]


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文会炸；能改就改成 utf-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def step(n: int, total: int, title: str) -> None:
    print()
    print(f"[{n}/{total}] {title}")
    print("-" * 62)


def info(msg: str) -> None:
    print(f"    {msg}")


def warn(msg: str) -> None:
    print(f"    !  {msg}")


def ok(msg: str) -> None:
    print(f"    [OK] {msg}")


def die(msg: str, code: int = 1) -> NoReturn:
    print()
    print(f"[X] 失败：{msg}")
    sys.exit(code)


def _quote(s: str) -> str:
    return f'"{s}"' if " " in s else s


def show_cmd(cmd: list[str]) -> None:
    info("$ " + " ".join(_quote(c) for c in cmd))


# --------------------------------------------------------------------------
# 可执行文件探测
# --------------------------------------------------------------------------
def find_tool(name: str, fallbacks: list[str]) -> str:
    """先 PATH，再退到已知安装位置。找不到返回空串（由调用方决定怎么报）。"""
    hit = shutil.which(name)
    if hit:
        return hit
    for cand in fallbacks:
        if cand and Path(cand).is_file():
            return cand
    return ""


# --------------------------------------------------------------------------
# 命令执行
# --------------------------------------------------------------------------
def run(
    cmd: list[str],
    *,
    dry_run: bool = False,
    mutating: bool = True,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    """跑一条命令。

    ``mutating=True`` 的命令在 dry-run 下只打印、不执行；
    ``mutating=False``（只读）的命令 dry-run 下照跑，用来拿真实的仓库状态。
    """
    if dry_run and mutating:
        show_cmd(cmd)
        info("(dry-run：跳过执行)")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    if not quiet:
        show_cmd(cmd)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        die(f"找不到可执行文件：{cmd[0]}\n       请确认它已安装，或用 --dry-run 先看计划。")
    except OSError as exc:                                    # pragma: no cover
        die(f"无法执行 {cmd[0]}：{exc}")

    if check and proc.returncode != 0:
        print()
        print(f"[X] 命令失败（退出码 {proc.returncode}）：")
        print("    " + " ".join(_quote(c) for c in cmd))
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            print("--- stdout " + "-" * 52)
            print(out)
        if err:
            print("--- stderr " + "-" * 52)
            print(err)
        print("-" * 62)
        print("    已停下，未继续执行后续步骤（不会留下半成品提交 / 推送）。")
        sys.exit(proc.returncode or 1)

    return proc


def out_of(proc: subprocess.CompletedProcess) -> str:
    if proc.returncode != 0:                      # 只读命令失败 → 当没结果
        return ""
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------
# 版本号 / 提交信息 / 体积
# --------------------------------------------------------------------------
def read_version() -> str:
    """从 mangakit/__init__.py 读 __version__。"""
    if not PKG_INIT.is_file():
        die(f"找不到 {PKG_INIT}，无法读取版本号。")
    text = PKG_INIT.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        die(f'{PKG_INIT} 里没有找到 __version__ = "…"。')
    return m.group(1)


def read_app_name() -> str:
    try:
        text = PKG_INIT.read_text(encoding="utf-8")
    except OSError:
        return "MangaKit"
    m = re.search(r'^__app_name__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    cn = re.search(r'^__app_name_cn__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    name = m.group(1) if m else "MangaKit"
    return f"{cn.group(1)} {name}" if cn else name


def default_message() -> str:
    return f"发布 {read_app_name()} v{read_version()}（{datetime.now():%Y-%m-%d %H:%M}）"


def human_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.3f} MB"


# --------------------------------------------------------------------------
# 首次发布引导（仓库还没 git init 时，dry-run 打这份计划）
# --------------------------------------------------------------------------
def print_first_publish_plan(git: str, gh: str, message: str, tag: str,
                             remote: str, branch: str, args: argparse.Namespace) -> None:
    total = 7
    print()
    warn(f"{ROOT} 还不是 git 仓库 —— 下面是为「首次发布」准备的完整计划。")
    warn("正式执行前请先初始化仓库（见步骤 1），再重跑本脚本（不加 --dry-run）。")

    step(1, total, "初始化仓库（本脚本不替你做，需人工确认远端地址）")
    show_cmd([git, "init", "-b", branch])
    info("$ " + f'{git} remote add {remote} https://github.com/<你的账号>/<仓库名>.git')
    info("        （远端地址由你决定；如仓库还没建，先执行 gh repo create <仓库名> --private --source . --push）")
    info(f"        .gitignore 已就位：{'是' if GITIGNORE.is_file() else '缺！（dist/ 与 *.exe 会被提交进去，先补上）'}")

    step(2, total, "验证 .gitignore 生效（关键：exe 不能进仓库）")
    show_cmd([git, "status", "--porcelain"])
    show_cmd([git, "check-ignore", "-v", "dist/MangaKit.exe"])
    info("        期望看到 dist/MangaKit.exe 被匹配（否则 63 MB 的 exe 会进仓库历史）")

    step(3, total, "暂存全部改动")
    show_cmd([git, "add", "-A"])
    show_cmd([git, "status", "--short"])
    info("        期望：dist/、build/、.tmp/、__pycache__/、tools/_smoke/ 不出现在列表里；")
    info("              mangakit/mangakit/rules/*.json、assets/、使用文档.md 必须在列表里。")

    step(4, total, "首次提交")
    show_cmd([git, "commit", "-m", message])
    info(f"        提交信息：{message}")

    step(5, total, "推送并设置上游")
    show_cmd([git, "push", "-u", remote, branch])

    step(6, total, f"打 tag {tag}")
    show_cmd([git, "tag", "-a", tag, "-m", f"{read_app_name()} {tag}"])
    show_cmd([git, "push", remote, tag])

    step(7, total, f"创建 GitHub Release {tag}")
    if args.no_release:
        info("已指定 --no-release，跳过。")
    else:
        notes_path = Path(tempfile.gettempdir()) / f"mangakit_release_{tag}.md"
        plan_assets = [str(EXE)] + ([str(LAUNCHER)] if LAUNCHER.is_file() else [])
        show_cmd([gh, "release", "create", tag, *plan_assets,
                  "--title", f"{read_app_name()} {tag}",
                  "--notes-file", str(notes_path)])
        info(f"        说明文件先写到：{notes_path}")

    print()
    print("=" * 62)
    print("  DRY-RUN 结束（首次发布计划），未改动任何东西。")
    print("=" * 62)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()

    ap = argparse.ArgumentParser(
        description="一键发布漫卷 MangaKit：add → commit → push → tag → gh release create",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令，不改动任何东西")
    ap.add_argument("-m", "--message", default="", help="提交信息（默认自动生成，含时间）")
    ap.add_argument("--tag", default="", help="tag 名（默认 v<__version__>）")
    ap.add_argument("--remote", default="origin", help="远端名（默认 origin）")
    ap.add_argument("--branch", default="main", help="分支名（默认 main）")
    ap.add_argument("--no-release", action="store_true", help="不创建 GitHub Release")
    ap.add_argument("--no-push", action="store_true", help="只本地 commit + tag，不推送")
    ap.add_argument("--notes", default="", help="Release 说明（默认自动生成）")
    ap.add_argument("--keep-notes-file", action="store_true", help="保留生成的 Release 说明文件")
    args = ap.parse_args(argv)

    version = read_version()
    tag = args.tag or f"v{version}"
    message = args.message or default_message()
    total = 7

    print("=" * 62)
    print(f"  {read_app_name()} 一键发布")
    print("=" * 62)
    info(f"仓库根目录 : {ROOT}")
    info(f"版本号     : {version}（来自 mangakit/__init__.py）")
    info(f"tag        : {tag}")
    info(f"提交信息   : {message}")
    info(f"模式       : {'DRY-RUN（不改动任何东西）' if args.dry_run else '正式执行'}")
    if args.dry_run:
        print()
        warn("dry-run：所有会改动仓库的命令都只打印、不执行。")

    # ---------------- 1. 环境检查 ----------------
    step(1, total, "环境检查（git / gh / 仓库状态）")
    git = find_tool("git", GIT_FALLBACKS)
    if not git:
        die(
            "找不到 git。\n"
            "       git 不在 PATH 上时，请确认已安装 Git for Windows，标准位置：\n"
            "         C:\\Program Files\\Git\\cmd\\git.exe\n"
            "       或者把 git 加进 PATH 后重试。"
        )
    info(f"git        : {git}")
    if not shutil.which("git"):
        info("             （git 不在 PATH 上，本脚本用绝对路径调用，不影响使用）")

    gh = find_tool("gh", GH_FALLBACKS)
    if gh:
        info(f"gh         : {gh}")
    else:
        info("gh         : 未找到")
        if args.no_release:
            warn("找不到 gh，但已指定 --no-release，继续。")
        else:
            die(
                "找不到 gh（GitHub CLI）。\n"
                "       装好后先跑一次 `gh auth login`；或加 --no-release 只做提交推送。"
            )

    # gh 登录态（只读，dry-run 下也真实检查）
    if gh and not args.no_release:
        auth = run([gh, "auth", "status"], mutating=False, check=False, quiet=True)
        if auth.returncode == 0:
            acct = ""
            blob = f"{auth.stdout}\n{auth.stderr}"
            m = re.search(r"account\s+(\S+)", blob)
            if m:
                acct = f"（账号 {m.group(1)}）"
            ok(f"gh 已登录 {acct}".rstrip())
        else:
            print("--- gh auth status " + "-" * 43)
            print((auth.stderr or auth.stdout or "").strip())
            print("-" * 62)
            die("gh 未登录。请先执行 `gh auth login`（需 repo 权限），再重跑本脚本。")

    is_repo = out_of(run([git, "rev-parse", "--is-inside-work-tree"],
                         mutating=False, check=False, quiet=True)) == "true"

    # ---------------- 2. 发布资产体积预检 ----------------
    # 放在「是否已 init」之前：不管仓库状态如何，体积提示都必须打出来。
    step(2, total, "发布资产体积预检")
    if not EXE.is_file():
        warn(f"没有找到 {EXE}")
        warn("先打包：python -m PyInstaller --noconfirm --clean mangakit.spec")
        if not args.no_release:
            die(f"没有 exe 就无法创建 Release（{EXE} 不存在）。")
        info("已指定 --no-release，跳过 Release 步骤。")
        size = 0
    else:
        size = EXE.stat().st_size
        exe_mb = size / 1024 / 1024
        info(f"exe        : {EXE}")
        info(f"体积       : {human_mb(size)}（{size:,} 字节）")
        info(f"构建时间   : {datetime.fromtimestamp(EXE.stat().st_mtime):%Y-%m-%d %H:%M:%S}")
        if exe_mb > GIT_OBJECT_LIMIT_MB:
            warn(f"exe {exe_mb:.1f} MB 超过 GitHub 单文件 {GIT_OBJECT_LIMIT_MB} MB 的 Git 对象上限！")
            warn("   绝对不能 git add 进仓库（.gitignore 已排除 dist/ 与 *.exe），只能作 Release 资产。")
        else:
            ok(f"未超过 GitHub {GIT_OBJECT_LIMIT_MB} MB 的 Git 对象上限，但按约定仍只走 Release。")
        print()
        warn(f"体积提醒：Release 附件让每个下载者都要拉 {exe_mb:.1f} MB。")
        warn(f"    GitHub Release 单文件上限是 {RELEASE_ASSET_LIMIT_GB} GB，本 exe 远低于该值；")
        warn("    但 QtWebEngine 打包版实测 138.9～210.4 MB，一旦启用内置浏览器，")
        warn(f"    exe 会直接越过 {GIT_OBJECT_LIMIT_MB} MB 的 Git 对象上限。")

    # ---------------- 仓库状态（决定走正常流程还是首次发布引导） ----------------
    if not is_repo:
        if args.dry_run:
            print_first_publish_plan(git, gh, message, tag, args.remote, args.branch, args)
            return 0
        die(
            f"{ROOT} 还不是一个 git 仓库。\n"
            "       请先初始化（本脚本不替你做这一步，避免建错远端）：\n"
            f'         cd "{ROOT}"\n'
            f'         "{git}" init -b {args.branch}\n'
            f'         "{git}" remote add {args.remote} https://github.com/<账号>/<仓库名>.git\n'
            "       不加 --dry-run 时本脚本会拒绝执行；加 --dry-run 可先看完整计划。"
        )

    ok("仓库已初始化")
    branch = args.branch or out_of(run([git, "rev-parse", "--abbrev-ref", "HEAD"],
                                       mutating=False, check=False, quiet=True)) or "main"
    info(f"分支       : {branch}")

    have_upstream = bool(out_of(run(
        [git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        mutating=False, check=False, quiet=True)))
    info(f"上游       : {'已设置' if have_upstream else '未设置（首次推送会自动 -u）'}")

    # ---------------- 3. 检查改动 ----------------
    step(3, total, "检查工作区改动")
    porcelain = out_of(run([git, "status", "--porcelain"],
                           mutating=False, check=False, quiet=True))
    changed = [ln for ln in porcelain.splitlines() if ln.strip()]
    if not changed:
        print()
        print("没有需要提交的改动，优雅退出（不会产生空提交）。")
        if EXE.is_file():
            info(f"提示：{EXE.name} 已在 dist/ 下；若只想重发 Release，")
            info("      先把 mangakit/__init__.py 的 __version__ 往上加，再重跑本脚本。")
        print("=" * 62)
        return 0

    info(f"共 {len(changed)} 项改动：")
    for ln in changed[:25]:
        print(f"      {ln}")
    if len(changed) > 25:
        info(f"      …… 其余 {len(changed) - 25} 项略")

    # ---------------- 4. add + commit ----------------
    step(4, total, "暂存并提交")
    run([git, "add", "-A"], dry_run=args.dry_run)
    run([git, "commit", "-m", message], dry_run=args.dry_run)
    info(f"提交信息：{message}" if args.dry_run else "已提交")
    if not args.dry_run:
        ok("提交完成")

    # ---------------- 5. push ----------------
    step(5, total, "推送到远端")
    if args.no_push:
        info("已指定 --no-push，跳过。")
    else:
        run([git, "push"] if have_upstream else [git, "push", "-u", args.remote, branch],
            dry_run=args.dry_run)
        if not args.dry_run:
            ok("推送完成")

    # ---------------- 6. tag ----------------
    step(6, total, f"打 tag {tag}")
    existing = out_of(run([git, "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                          mutating=False, check=False, quiet=True))
    if existing and not args.dry_run:
        warn(f"tag {tag} 已存在（{existing[:12]}），跳过创建。")
        warn("   要发新版本请先改 mangakit/__init__.py 的 __version__，或用 --tag 指定别的名字。")
    else:
        if args.dry_run and existing:
            warn(f"tag {tag} 已存在，正式执行时会跳过创建。")
        else:
            run([git, "tag", "-a", tag, "-m", f"{read_app_name()} {tag}"], dry_run=args.dry_run)
            if not args.dry_run:
                ok(f"已创建 tag {tag}")
        if not args.no_push and not (existing and not args.dry_run):
            run([git, "push", args.remote, tag], dry_run=args.dry_run)
            if not args.dry_run:
                ok("tag 已推送")

    # ---------------- 7. gh release create ----------------
    step(7, total, f"创建 GitHub Release {tag}")
    if args.no_release:
        info("已指定 --no-release，跳过。")
    else:
        rel = run([gh, "release", "view", tag], mutating=False, check=False, quiet=True)
        if rel.returncode == 0:
            die(
                f"Release {tag} 已存在。\n"
                "       要么先把 __version__ 往上加，要么手工处理：\n"
                f'         "{gh}" release delete {tag} --yes --cleanup-tag'
            )

        notes = args.notes or (
            f"{read_app_name()} {tag}\n\n"
            f"构建时间：{datetime.now():%Y-%m-%d %H:%M}\n"
            f"资产：MangaKit.exe（Windows 单文件，双击即可运行）\n\n"
            "本次改动：\n"
            "- （在这里补充更新说明；用 --notes 可覆盖本文件）\n\n"
            "完整使用说明见仓库根目录的 使用文档.md。\n"
            "仅供个人学习与离线备份使用，请遵守各站点版权与使用条款。\n"
        )
        notes_path = Path(tempfile.gettempdir()) / f"mangakit_release_{tag}.md"

        if args.dry_run:
            info(f"将写入 Release 说明：{notes_path}")
            for line in notes.strip().splitlines():
                print(f"      | {line}")
        else:
            notes_path.write_text(notes, encoding="utf-8")
            info(f"Release 说明：{notes_path}")

        assets = [str(EXE)]
        # exe 自己解决解包目录与写入预检（见 mangakit.spec 的 runtime_tmpdir /
        # main.py 的 _startup_preflight），bat 只是留给旧习惯的兜底入口，
        # 带上它没坏处，缺了也不影响使用。
        if LAUNCHER.is_file():
            assets.append(str(LAUNCHER))

        if EXE.is_file() or args.dry_run:
            run([gh, "release", "create", tag, *assets,
                 "--title", f"{read_app_name()} {tag}",
                 "--notes-file", str(notes_path)], dry_run=args.dry_run)
        else:
            warn(f"{EXE} 不存在，Release 将不带资产。")

        if not args.dry_run:
            url = out_of(run([gh, "release", "view", tag, "--json", "url", "-q", ".url"],
                             mutating=False, check=False, quiet=True))
            print()
            ok(f"Release {tag} 已创建" + (f"：{url}" if url else ""))
            if not args.keep_notes_file:
                try:
                    notes_path.unlink()
                except OSError:
                    pass

    # ---------------- 收尾 ----------------
    print()
    print("=" * 62)
    if args.dry_run:
        print("  DRY-RUN 结束：以上是完整计划，未改动任何东西。")
        info("去掉 --dry-run 即可正式执行。")
    else:
        print(f"  发布完成：{read_app_name()} {tag}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
