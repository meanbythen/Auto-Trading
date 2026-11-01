"""标定窗口的启动方式：源码走脚本，打包后的程序再开一个自己。"""

from __future__ import annotations

from src.ui.coord_picker import picker_launch_command, save_coords


def test_source_launch_runs_the_picker_script(monkeypatch) -> None:
    monkeypatch.setattr("src.ui.coord_picker.is_frozen", lambda: False)
    command = picker_launch_command()
    assert command[-1].replace("\\", "/").endswith("tools/pick_coords.py")


def test_frozen_launch_opens_the_picker_flag(monkeypatch) -> None:
    monkeypatch.setattr("src.ui.coord_picker.is_frozen", lambda: True)
    command = picker_launch_command()
    assert command[0]
    assert command[1:] == ["--pick-coords"]


def test_a_few_pixels_is_a_reminder_not_a_failure() -> None:
    from src.utils.preflight import describe_coord_mismatch

    current = {"price_input": [667, 981], "close_btn": [707, 1069]}
    saved = {"price_input": [665, 980], "close_btn": [708, 1066]}
    level, detail = describe_coord_mismatch(current, saved)
    assert level == "warn"
    assert "像素" in detail


def test_a_different_screen_fails_the_check() -> None:
    from src.utils.preflight import describe_coord_mismatch

    current = {"price_input": [667, 981]}
    saved = {"price_input": [400, 700]}
    level, _detail = describe_coord_mismatch(current, saved)
    assert level == "fail"


def test_save_coords_writes_beside_the_project(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ui.coord_picker.project_root", lambda: tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text("trade:\n  gui: {}\n", encoding="utf-8")
    out = save_coords({"buy_open_btn": [10, 20]})
    assert out == config / "gui_coords.yaml"
    text = (config / "config.yaml").read_text(encoding="utf-8")
    assert "10" in text and "20" in text
