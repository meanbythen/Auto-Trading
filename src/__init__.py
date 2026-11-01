# 包标识：让 Python 可以把 src 当作一个包来导入
# 原理：有了 __init__.py，就能使用 python -m src.main 这种方式启动
"""Auto-Trading 期货自动交易机器人包。"""

from src.version import __version__  # noqa: F401
