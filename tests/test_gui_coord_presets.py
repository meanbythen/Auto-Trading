"""下单坐标存档：只换点击位置，不改别的开关，删除也不动当前配置。"""

from datetime import datetime

from src.ui.gui_coord_presets import (
    add_coord_preset,
    apply_coords_to_config,
    delete_coord_preset,
    rename_coord_preset,
)


def _gui() -> dict:
    return {
        "assume_fill": True,
        "price_input": [10, 20],
        "volume_input": [30, 40],
        "buy_open_btn": [50, 60],
        "sell_open_btn": [70, 80],
        "close_btn": [90, 100],
    }


def test_save_rename_and_apply_only_the_points(tmp_path) -> None:
    entry = add_coord_preset(tmp_path, _gui(), name="家里显示器", when=datetime(2026, 10, 21, 21, 5, 0))
    assert entry["name"] == "家里显示器"
    renamed = rename_coord_preset(tmp_path, entry["id"], "笔记本")
    assert renamed == "笔记本"
    cfg = {
        "trade": {
            "executor": "gui",
            "gui": {
                "assume_fill": False,
                "confirm_mode": "both",
                "price_input": [1, 1],
                "symbol_input": [8, 8],
            },
        }
    }
    applied = apply_coords_to_config(cfg, entry["coords"])
    gui = applied["trade"]["gui"]
    assert gui["assume_fill"] is False
    assert gui["confirm_mode"] == "both"
    assert gui["price_input"] == [10, 20]
    assert gui["close_btn"] == [90, 100]
    assert "symbol_input" not in gui
    assert applied["trade"]["executor"] == "gui"


def test_delete_removes_only_the_backup(tmp_path) -> None:
    home = add_coord_preset(tmp_path, _gui(), name="家里")
    away = add_coord_preset(
        tmp_path,
        {**_gui(), "price_input": [200, 300]},
        name="笔记本",
        when=datetime(2026, 10, 21, 21, 6, 0),
    )
    assert delete_coord_preset(tmp_path, away["id"]) == "笔记本"
    from src.ui.gui_coord_presets import load_coord_presets

    left = load_coord_presets(tmp_path)
    assert [item["id"] for item in left] == [home["id"]]
    assert left[0]["coords"]["price_input"] == [10, 20]


def test_incomplete_coords_are_refused(tmp_path) -> None:
    try:
        add_coord_preset(tmp_path, {"price_input": [1, 2]})
    except ValueError as exc:
        assert "还不完整" in str(exc)
    else:
        raise AssertionError("缺按钮时不应该保存")
