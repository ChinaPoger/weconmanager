"""界面坐标的三层优先级与自动校准的记忆。

这里不碰客户端 —— 只验「用哪个点、什么时候试探、什么时候记住」这套决策。
真实的点击与成功信号在 test_local_group.py 里。
"""
import json

import pytest

from wecom_svr import ui_offsets
from wecom_svr.settings import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path)


def test_parse_point_tolerates_spaces_and_rejects_junk():
    assert ui_offsets.parse_point("295,29") == (295, 29)
    assert ui_offsets.parse_point(" 295 , 29 ") == (295, 29)
    for junk in ("", "295", "a,b", "295;29"):
        assert ui_offsets.parse_point(junk) is None


def test_defaults_are_used_when_nothing_was_learned(settings):
    assert ui_offsets.primary("plus", settings) == ui_offsets.DEFAULTS["plus"]
    assert ui_offsets.candidates("plus", settings)[0] == ui_offsets.DEFAULTS["plus"]


def test_learned_offset_wins_over_the_default(settings):
    ui_offsets.remember("plus", (275, 29), settings)
    assert ui_offsets.primary("plus", settings) == (275, 29)
    # 候选也要跟着挪 —— 试探是围绕「现在用的那个点」展开的
    assert ui_offsets.candidates("plus", settings)[0] == (275, 29)


def test_env_override_wins_over_everything_and_stops_probing(tmp_path):
    s = Settings(data_dir=tmp_path, ui_plus_offset="10,20")
    ui_offsets.remember("plus", (275, 29), s)          # 配死了就不该被记忆干扰
    assert ui_offsets.primary("plus", s) == (10, 20)
    assert ui_offsets.candidates("plus", s) == [(10, 20)]
    assert not (tmp_path / "ui_offsets.json").exists()


def test_a_broken_override_falls_back_instead_of_breaking(tmp_path):
    s = Settings(data_dir=tmp_path, ui_title_offset="不是坐标")
    assert ui_offsets.primary("title", s) == ui_offsets.DEFAULTS["title"]


def test_calibration_can_be_turned_off(tmp_path):
    s = Settings(data_dir=tmp_path, ui_calibrate=False)
    assert ui_offsets.candidates("plus", s) == [ui_offsets.DEFAULTS["plus"]]


def test_candidates_start_at_the_current_point_and_do_not_repeat(settings):
    cands = ui_offsets.candidates("title", settings)
    assert cands[0] == ui_offsets.DEFAULTS["title"]
    assert len(cands) == len(set(cands))


def test_first_calibration_persists_even_when_default_point_works(settings, tmp_path):
    ui_offsets.remember("plus", ui_offsets.DEFAULTS["plus"], settings)
    saved = json.loads((tmp_path / "ui_offsets.json").read_text(encoding="utf-8"))
    assert saved == {"plus": "295,29"}


def test_remembering_an_already_saved_point_does_not_rewrite(settings, tmp_path):
    path = tmp_path / "ui_offsets.json"
    ui_offsets.remember("plus", (275, 29), settings)
    first_mtime = path.stat().st_mtime_ns

    ui_offsets.remember("plus", (275, 29), settings)

    assert path.stat().st_mtime_ns == first_mtime


def test_two_positions_are_remembered_independently(settings, tmp_path):
    ui_offsets.remember("plus", (275, 29), settings)
    ui_offsets.remember("title", (395, 33), settings)
    saved = json.loads((tmp_path / "ui_offsets.json").read_text(encoding="utf-8"))
    assert saved == {"plus": "275,29", "title": "395,33"}


def test_a_corrupt_state_file_is_ignored_not_fatal(settings, tmp_path):
    """这个文件只是加速缓存 —— 坏了就重新校准一遍,不能让整条路挂掉。"""
    (tmp_path / "ui_offsets.json").write_text("{半截", encoding="utf-8")
    assert ui_offsets.learned("plus", settings) is None
    assert ui_offsets.primary("plus", settings) == ui_offsets.DEFAULTS["plus"]

    ui_offsets.remember("plus", (275, 29), settings)      # 覆盖掉坏文件
    assert ui_offsets.learned("plus", settings) == (275, 29)


def test_unknown_position_is_a_programming_error(settings):
    with pytest.raises(ui_offsets.UnknownOffset):
        ui_offsets.primary("nope", settings)


def test_more_offset_is_from_the_right_edge(settings):
    assert ui_offsets.DEFAULTS["more"] == (28, 33)
    assert ui_offsets.candidates("more", settings)[0] == (28, 33)


def test_describe_says_where_the_number_came_from(settings, tmp_path):
    assert "默认值" in ui_offsets.describe("plus", settings)
    ui_offsets.remember("plus", (275, 29), settings)
    assert "上次校准" in ui_offsets.describe("plus", settings)

    pinned = Settings(data_dir=tmp_path, ui_plus_offset="10,20")
    assert "WECOM_SVR_UI_PLUS_OFFSET" in ui_offsets.describe("plus", pinned)
