"""CTP 多前置热备：拆地址、去重、登记顺序。"""

from src.trader.ctp_fronts import (
    collect_fronts,
    dump_front_field,
    format_fronts_ui,
    register_fronts,
    split_fronts,
)
from src.trader.ctp_gateway import CtpSettings
from src.trader.ctp_profile import resolve_ctp_profile


def test_split_fronts_accepts_semicolon_and_list() -> None:
    """分号、逗号、YAML 列表都能收成同一份去重列表。"""
    text = "tcp://220.196.208.129:41206；tcp://220.196.208.130:41206, tcp://220.196.208.129:41206"
    listed = ["tcp://220.196.208.129:41206", "tcp://220.196.208.130:41206"]
    assert split_fronts(text) == listed
    assert split_fronts(listed) == listed
    assert dump_front_field(listed) == listed
    assert "208.130" in format_fronts_ui(text)


def test_collect_fronts_keeps_primary_first() -> None:
    """主地址在前，备用接在后面，重复的丢掉。"""
    assert collect_fronts(
        "tcp://a.com:1",
        ["tcp://b.com:1", "tcp://a.com:1"],
    ) == ["tcp://a.com:1", "tcp://b.com:1"]


def test_register_fronts_calls_every_address() -> None:
    """Init 前要把每一台都 RegisterFront，柜台才能断线换台。"""

    class _Api:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def RegisterFront(self, front: str) -> None:
            self.seen.append(front)

    api = _Api()
    count = register_fronts(
        api,
        ["tcp://220.196.208.129:41206", "tcp://120.86.122.66:41206", ""],
    )
    assert count == 2
    assert api.seen[0].endswith("41206")
    assert "120.86.122.66" in api.seen[1]


def test_live_profile_yaml_list_becomes_settings_fronts() -> None:
    """真实档案写成列表后，通道设置里能看到多台备用。"""
    ctp = {
        "ctp_profile": "huatai_live",
        "ctp_profiles": {
            "huatai_live": {
                "kind": "live",
                "broker_id": "8080",
                "td_front": [
                    "tcp://220.196.208.129:41206",
                    "tcp://120.86.122.66:41206",
                ],
                "md_front": [
                    "tcp://220.196.208.129:41214",
                    "tcp://120.86.122.66:41214",
                ],
            }
        },
    }
    resolved = resolve_ctp_profile(ctp, account={"user_id": "20318052"})
    assert resolved["td_front"].endswith("41206")
    assert len(resolved["td_fronts"]) == 2
    settings = CtpSettings.from_cfg(
        {
            "trade": {"ctp": ctp},
            "account": {"user_id": "20318052"},
        }
    )
    assert len(settings.td_fronts) == 2
    assert settings.md_fronts[1].endswith("41214")
