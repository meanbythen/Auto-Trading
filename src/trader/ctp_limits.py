"""CTP 本机上限口径：公共值与券商档案值取小。

单独成模块的原因：account.session 也要用这个口径，但它不能静态依赖
trader.ctp_profile（会与 utils.config_loader 形成导入环）。这里只放纯函数。
"""

from __future__ import annotations

from typing import Any


def effective_max_order_volume(
    configured_max: Any,
    profile_max: Any,
    default: int = 20,
) -> int:
    """
    本机单笔最大手数的实际生效值。

    公共值（配置页填的）与券商档案值都 >0 时取小；只有一个 >0 时取那个；
    都为 0 时回退默认 20。界面回填和运行时拦单必须用同一口径。
    """
    def _int_or_zero(value: Any) -> int:
        try:
            return max(0, int(float(str(value))))
        except (TypeError, ValueError):
            return 0

    public = _int_or_zero(configured_max)
    profile = _int_or_zero(profile_max)
    if public > 0 and profile > 0:
        return min(public, profile)
    if public > 0:
        return public
    if profile > 0:
        return profile
    return default
