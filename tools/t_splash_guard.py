# -*- coding: utf-8 -*-
"""一次性验证：_splash_available() 的三种情形，以及不再往日志吐 pyi_splash 警告。

开发环境里 `pyi_splash` 并不存在（它由 PyInstaller 注入到 frozen 包里），
所以这里用一个**行为等价**的桩模块顶上：模块级就按环境变量决定
「警告 + _initialized=False」还是「吞掉变量 + _initialized=True」——
与 `PyInstaller\\fake-modules\\pyi_splash.py` 第 104-119 行一致。

情形：
  A 没有 _PYI_SPLASH_IPC（v0.3.1 现状）→ 必须压根不 import，日志无 warning
  B _PYI_SPLASH_IPC=0（splash 被优雅抑制）→ 可以 import，判定为真，无 warning
  C 连调两次 → 缓存生效（桩模块会自己 del 掉环境变量，第二次不能误判 False）
"""
import importlib
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\AI\工作区1\mangakit")

RECORDS = []


class _Cap(logging.Handler):
    def emit(self, r):
        RECORDS.append(r)


logging.getLogger("pyi_splash").addHandler(_Cap())
logging.getLogger("pyi_splash").setLevel(logging.DEBUG)

ok = True


def check(cond, label, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'ok' if cond else 'BAD'}] {label}" + (f" —— {detail}" if detail else ""))


# ---- 桩模块：与真实 pyi_splash 行为等价 --------------------------------
STUB_DIR = Path(tempfile.mkdtemp(prefix="mk_stub_"))
(STUB_DIR / "pyi_splash.py").write_text(
    '''import logging
import os

try:
    _ipc_port = int(os.environ["_PYI_SPLASH_IPC"])
    del os.environ["_PYI_SPLASH_IPC"]
    _initialized = True
    _ipc_socket_closed = False
except (KeyError, ValueError):
    _ipc_port = None
    _initialized = False
    _ipc_socket_closed = True
    logging.getLogger("pyi_splash").warning(
        "The environment does not allow connecting to the splash screen. "
        "Did bootloader fail to initialize it?",
        exc_info=True,
    )


def is_alive():
    return _initialized and not _ipc_socket_closed


def update_text(msg):
    pass


def close():
    global _ipc_socket_closed
    _ipc_socket_closed = True
''',
    encoding="utf-8",
)
sys.path.insert(0, str(STUB_DIR))


def fresh(env_val):
    """干净状态下重新加载 mangakit.main（清缓存 + 清已导入的桩模块）。"""
    os.environ.pop("_PYI_SPLASH_IPC", None)
    if env_val is not None:
        os.environ["_PYI_SPLASH_IPC"] = env_val
    sys.modules.pop("pyi_splash", None)
    for name in [n for n in sys.modules if n == "mangakit" or n.startswith("mangakit.")]:
        sys.modules.pop(name, None)
    return importlib.import_module("mangakit.main")


print("=== A：没有 _PYI_SPLASH_IPC（打包后未声明 splash 的现状）===")
RECORDS.clear()
m = fresh(None)
check(m._splash_available() is False, "判定为「没有启动画面」")
check("pyi_splash" not in sys.modules, "桩模块从未被 import（那次 import 才是警告来源）")
warn = [r for r in RECORDS if r.levelno >= logging.WARNING]
check(not warn, "日志里没有 warning", f"warning={len(warn)}")
m._splash_update("正在加载界面…")
m._splash_close()
check("pyi_splash" not in sys.modules, "调用 update/close 之后仍然没有被 import")

print()
print("=== B：_PYI_SPLASH_IPC=0（splash 被优雅抑制）===")
RECORDS.clear()
m = fresh("0")
check(m._splash_available() is True, "判定为「确实有启动画面」")
check("pyi_splash" in sys.modules, "桩模块已被 import")
warn = [r for r in RECORDS if r.levelno >= logging.WARNING]
check(not warn, "没有 warning（带变量时走正常分支）", f"warning={len(warn)}")

print()
print("=== C：缓存（桩模块会 del 掉环境变量，第二次不能误判）===")
check("_PYI_SPLASH_IPC" not in os.environ, "确认桩模块已自行删除该环境变量")
check(m._splash_available() is True, "第二次仍判定为真（缓存生效，未误判）")
m._splash_update("正在打开主窗口…")
m._splash_close()
check(True, "update/close 在有 splash 时正常执行不抛异常")

print()
print("=== D：真实 exe 的启动日志不再出现 pyi_splash 警告 ===")
print("     （由 tools\\t_no_splash.py 覆盖，见后续跑批结果）")

shutil.rmtree(STUB_DIR, ignore_errors=True)
print()
print("=== 全部通过 ===" if ok else "=== 有失败 ===")
raise SystemExit(0 if ok else 1)
