"""仿真账号号码只写仿真档案，不改真实资金号。"""

from src.trader.ctp_profile import set_sim_profile_user_id
from src.trader.ctp_profile import sim_profile_user_id


def _box() -> dict:
    return {
        "account_kind": "live",
        "ctp_profile": "huatai_live",
        "ctp_profiles": {
            "huatai_sim": {"kind": "sim", "user_id": "28077"},
            "huatai_live": {"kind": "live", "user_id": ""},
        },
    }


def test_read_sim_user_even_when_current_profile_is_live() -> None:
    """配置页停在真实档案时，账号页仍能读到仿真 28077。"""
    assert sim_profile_user_id(_box()) == "28077"


def test_write_sim_user_does_not_touch_live_profile() -> None:
    """账号页改仿真号码，只动 huatai_sim，真实档案保持原样。"""
    ctp = _box()
    name = set_sim_profile_user_id(ctp, "28078")
    assert name == "huatai_sim"
    assert ctp["ctp_profiles"]["huatai_sim"]["user_id"] == "28078"
    assert ctp["ctp_profiles"]["huatai_live"]["user_id"] == ""
    assert sim_profile_user_id(ctp) == "28078"


def test_write_sim_user_empty_ctp_is_noop() -> None:
    """还没有券商档案时不报错、不瞎建一份。"""
    assert set_sim_profile_user_id({}, "28077") == ""
    assert sim_profile_user_id({}) == ""
