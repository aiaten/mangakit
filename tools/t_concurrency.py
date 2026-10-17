# -*- coding: utf-8 -*-
"""离线回归：下载并发数的热调整（不用重启程序就能改并发）。

背景：DownloadManager 的 asyncio.Semaphore 原先只在 start() 里建一次，
用户在设置页把「同时下载数」从 6 改成 2，保存后毫无反应，必须退出重开。
现在 apply_concurrency() 能在线调整。

这里不真下载，只验证信号量的**实际容量**确实跟着变：
  1. 建成 N 个许可，同时只跑 N 个；
  2. 调大 → 立刻能多跑几个；
  3. 调小 → 多出来的许可被收走，但**正在跑的任务不会被掐断**（关键）；
  4. 调小后再调大 → 回到原来的容量，不残留占位的协程；
  5. 事件循环没起来时调用返回 0，不做任何事。

跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'; $env:PYTHONIOENCODING='utf-8'
    <py> tools\\t_concurrency.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 必须在 import mangakit 之前隔离数据目录（见开发接手.md 第 5 节）
os.environ.setdefault("MANGAKIT_HOME",
                      tempfile.mkdtemp(prefix="mk_conc_"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mangakit.config import config        # noqa: E402
from mangakit.downloader import DownloadManager   # noqa: E402

FAILED: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def measure_capacity(mgr, timeout=3.0) -> int:
    """实测当前信号量容量：从外部不停塞「占住许可」的任务，数能进去几个。"""
    grabbed = []

    async def grab():
        await mgr._sem.acquire()
        grabbed.append(1)

    futs = []
    deadline = time.time() + timeout
    # 多塞一些候选，多出来的会一直等着（拿不到许可就不算数）
    for _ in range(40):
        futs.append(mgr.submit(grab()))
    # 给足够时间让能进去的都进去
    time.sleep(min(timeout, 0.6))
    n = len(grabbed)
    # 收尾：把还挂着的取消掉，并把已抓住的许可还回去
    for f in futs:
        try:
            f.cancel()
        except Exception:
            pass

    async def release_all():
        for _ in range(len(grabbed)):
            mgr._sem.release()

    mgr.submit(release_all()).result(timeout=5)
    time.sleep(0.05)
    return n


def main() -> int:
    print("== 1. 事件循环没起来时调用 ==")
    mgr = DownloadManager()
    check("未启动时 apply_concurrency 返回 0", mgr.apply_concurrency(4), 0)

    print("\n== 2. 按配置启动，容量 = 配置值 ==")
    config().set("concurrency", 3)
    mgr.start()
    time.sleep(0.2)
    check("启动后 _sem_n", mgr._sem_n, 3)
    check("实测容量", measure_capacity(mgr), 3)

    print("\n== 3. 调大 → 立刻生效 ==")
    ret = mgr.apply_concurrency(6)
    check("apply_concurrency(6) 返回", ret, 6)
    time.sleep(0.15)
    check("调大后 _sem_n", mgr._sem_n, 6)
    check("调大后实测容量", measure_capacity(mgr), 6)

    print("\n== 4. 调小 → 多出的许可被收走 ==")
    # 先让循环空转，确保没有在跑的任务占着许可
    time.sleep(0.1)
    mgr.apply_concurrency(2)
    time.sleep(0.2)
    check("调小后 _sem_n", mgr._sem_n, 2)
    # 占位协程已经吞掉 4 个许可，实测容量应回落到 2
    check("调小后实测容量", measure_capacity(mgr), 2)

    print("\n== 5. 调小再调大，容量要能回到原位（占位协程不残留）==")
    mgr.apply_concurrency(6)
    time.sleep(0.2)
    check("再调大后 _sem_n", mgr._sem_n, 6)
    check("再调大后实测容量", measure_capacity(mgr), 6)
    check("占位协程已清空", len(mgr._holders), 0)

    print("\n== 6. 调小时不打断正在跑的任务（关键）==")
    # 模拟 3 个「正在跑的下载」：各自抓住许可不放，直到外部放行
    async def long_task(gate):
        async with mgr._sem:
            await gate.wait()

    import asyncio

    gates = []
    futs = []
    for _ in range(3):
        gate = asyncio.Event()
        gates.append(gate)
        futs.append(mgr.submit(long_task(gate)))
    time.sleep(0.3)

    mgr.apply_concurrency(1)          # 从 6 缩到 1，但已有 3 个在跑
    time.sleep(0.3)
    still_running = sum(1 for f in futs if not f.done())
    check("缩容不掐断在跑的任务", still_running, 3)

    # 放行，让它们正常结束
    async def open_all():
        for g in gates:
            g.set()

    mgr.submit(open_all())
    for f in futs:
        try:
            f.result(timeout=5)
        except Exception:
            pass
    check("放行后全部结束", all(f.done() for f in futs), True)

    print("\n== 7. 下限保护：并发数至少 1 ==")
    check("apply_concurrency(0) 夹到 1", mgr.apply_concurrency(0), 1)
    check("apply_concurrency(-5) 夹到 1", mgr.apply_concurrency(-5), 1)
    time.sleep(0.2)
    check("_sem_n 最终", mgr._sem_n, 1)

    mgr.stop()

    print("\n" + ("=" * 46))
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
