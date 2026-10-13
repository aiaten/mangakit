"""写入权限回归：目录不可写时必须给出人话，而不是裸 PermissionError。

背景（真实事故）：用户报 GUI 下载「PermissionError: [WinError 5] 拒绝访问. : 'C:\\...\\龍之皇冠'」。
根因不是 MangaKit 的逻辑 —— 是那份 exe 被放在 DSH 工作区内运行，宿主禁止往
工作区外写文件（mkdir 常通、建文件被拒），下载器第一步 mkdir 就炸了。

这里把两件事钉死：
  1. probe_writable 必须真的「建目录 + 建文件」两步都试。只试 mkdir 会在
     「目录能建、文件建不出」的环境里误判为可写，错误一路漏到下载时。
  2. 下载队列抛的必须是 WriteDenied（带修复建议），不能是裸 PermissionError。
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mangakit.config import (  # noqa: E402
    WriteDenied, ensure_writable, probe_writable,
)
from mangakit.downloader import DownloadManager  # noqa: E402

ok = True


def report(name: str, good: bool, extra: str = "") -> None:
    global ok
    ok = ok and good
    print(f"{'OK ' if good else 'BAD'} {name}" + (f"  {extra}" if extra else ""))


BASE = Path(tempfile.mkdtemp(prefix="mk_write_"))

# --- 1. 正常目录必须判定为可写，且不留探针文件 -----------------------------
good_dir = BASE / "可写"
report("正常目录可写", probe_writable(good_dir)[0] is True)
leftovers = [p.name for p in good_dir.iterdir() if p.name.startswith(".mangakit_write_probe")]
report("探针文件已清理", not leftovers, f"残留={leftovers}" if leftovers else "")

# --- 2. 不可写目录必须判定为不可写并给出原因 -------------------------------
# 用「同名文件挡住目录」制造确定性的 mkdir 失败，不依赖权限/ACL（会随环境变）。
blocked_dir = BASE / "被文件挡住"
blocked_dir.write_text("i am a file, not a folder", encoding="utf-8")
writable, why = probe_writable(blocked_dir)
report("同名文件挡路 -> 判不可写", writable is False, f"原因={why!r}")
report("原因是人话而非 errno", why and why.isprintable() and not why.startswith("PermissionError"))

# --- 3. ensure_writable 抛 WriteDenied，且消息含修复步骤 ---------------------
raised = None
try:
    ensure_writable(blocked_dir)
except WriteDenied as e:
    raised = e
report("ensure_writable 抛 WriteDenied", isinstance(raised, WriteDenied))
if raised is not None:
    msg = str(raised)
    report("消息含下载目录路径", str(blocked_dir) in msg)
    report("消息给出修复建议", "下载目录" in msg and "移到" in msg)

# --- 4. 下载器落盘闸门：目录被挡住时应报 WriteDenied 而非 PermissionError ---
class _Cfg(dict):
    pass


async def check_downloader() -> None:
    from mangakit.downloader import TaskContext
    from mangakit.models import Task

    mgr = DownloadManager()
    task = Task(tid="t1", comic="测试漫画", chapter="第01話", folder="",
                status="queued", created=0)
    # 用一条真实规则的网址：闸门排在 parse_pages 之前，所以只要站点能匹配上，
    # 就会在「尝试联网抓页」之前先因为目录不可写退出 —— 这条测试全程不联网。
    ctx = TaskContext(
        task=task, comic_title="测试漫画", chapter_title="第01話",
        comic_url="https://www.comicwang.com/title/15412.html",
        chapter_url="https://www.comicwang.com/title/15412.html",
        folder=blocked_dir / "测试漫画" / "第01話",
    )
    try:
        await mgr._download_chapter(ctx)
        report("下载器拦截不可写目录", False, "竟然没抛异常")
    except WriteDenied as e:
        report("下载器抛 WriteDenied", True, f"{str(e).splitlines()[0][:60]}")
    except PermissionError as e:
        report("下载器抛 WriteDenied", False, f"仍是裸 {type(e).__name__}: {e}")
    except Exception as e:
        report("下载器抛 WriteDenied", False, f"意外 {type(e).__name__}: {e}")
    del mgr, task, ctx


try:
    asyncio.run(check_downloader())
except Exception as exc:  # noqa: BLE001
    report("下载器检查未崩", False, f"{type(exc).__name__}: {exc}")

# --- 5. 确认 WriteDenied 是 Exception 的子类，不会被 except Exception 误吞 ---
report("WriteDenied 可被 except Exception 捕获",
       issubclass(WriteDenied, Exception))
report("WriteDenied 不是 PermissionError 子类（需独立分支）",
       not issubclass(WriteDenied, PermissionError))

# 清理
shutil.rmtree(BASE, ignore_errors=True)

print("ALL WRITE TESTS PASSED" if ok else "WRITE TESTS FAILED")
sys.exit(0 if ok else 1)