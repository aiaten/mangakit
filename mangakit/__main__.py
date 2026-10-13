from __future__ import annotations

import os
import sys

# PyInstaller 会把本文件当成顶层 __main__ 脚本运行，此时没有包上下文，
# 相对导入（from .main import main）会直接报
#   ImportError: attempted relative import with no known parent package
# 所以这里手工把包根目录塞进 sys.path，再走绝对导入。源码运行时 __package__
# 是 "mangakit"，走相对导入即可。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mangakit.main import main
else:
    from .main import main

sys.exit(main())
