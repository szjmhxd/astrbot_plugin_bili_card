"""单元测试包。

运行方式（在插件目录的父目录执行）：

    python -m unittest discover -s astrbot_plugin_bili_card/tests -v
"""

import sys
from pathlib import Path

# 让测试可以直接 import core.*，无需安装插件
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
