"""排练录音时间戳定位核心的单元测试。运行：python -m pytest -q"""

from __future__ import annotations

import pytest

from app.rehearsal import (
    PlayedMeasure,
    RehearsalError,
    RepeatSection,
    build_timeline,
    locate_rehearsal_timestamps,
    page_for_measure,
)


def test_no_repeats_timeline_expansion():
    tl = build_timeline([4, 4, 4], tempo_bpm=60, repeat_sections=[])
    assert tl.sequence == [
        PlayedMeasure(0, 1),
        PlayedMeasure(1, 1),
        PlayedMeasure(2, 1),
    ]
    # 60 * 4 / 60 = 4 秒/小节
    assert tl.durations == [4.0, 4.0, 4.0]
    assert tl.total_duration == pytest.approx(12.0)


def test_repeat_section_expanded_in_place():
    # 4 小节，[1,2] 演奏 2 遍：序列应为 0,1,2,1,2,3
    tl = build_timeline(
        [4, 4, 4, 4], tempo_bpm=60,
        repeat_sections=[RepeatSection(1, 2, 2)],
    )
    assert [(p.measure, p.iteration) for p in tl.sequence] == [
        (0, 1), (1, 1), (2, 1), (1, 2), (2, 2), (3, 1)
    ]
    assert tl.total_duration == pytest.approx(24.0)


def test_multiple_disjoint_repeat_sections():
    # 6 小节，[0,1]x3、[4,5]x2：0,1,0,1,0,1,2,3,4,5,4,5
    tl = build_timeline(
        [1] * 6, tempo_bpm=60,
        repeat_sections=[RepeatSection(0, 1, 3), RepeatSection(4, 5, 2)],
    )
    assert [(p.measure, p.iteration) for p in tl.sequence] == [
        (0, 1), (1, 1), (0, 2), (1, 2), (0, 3), (1, 3),
        (2, 1), (3, 1),
        (4, 1), (5, 1), (4, 2), (5, 2),
    ]


def test_sections_given_out_of_order_are_sorted():
    tl = build_timeline(
        [1] * 6, tempo_bpm=60,
        repeat_sections=[RepeatSection(4, 5, 2), RepeatSection(0, 1, 3)],
    )
    measures = [p.measure for p in tl.sequence]
    assert measures == [0, 1, 0, 1, 0, 1, 2, 3, 4, 5, 4, 5]


def test_measure_duration_formula_and_offsets():
    # 3 拍/小节、120 bpm -> 每小节 1.5 秒
    total, locs = locate_rehearsal_timestamps(
        measure_count=2,
        beats_per_measure=[3, 3],
        tempo_bpm=120,
        repeat_sections=[],
        timestamps=[0.0, 0.5, 1.5, 2.0],
        breaks=[],
    )
    assert total == pytest.approx(3.0)
    assert [(l.measure, l.iteration, l.beat_offset, l.page_index) for l in locs] == [
        (0, 1, 0.0, 1),       # 起点
        (0, 1, 1.0, 1),       # 0.5s * 2 = 1 拍
        (1, 1, 0.0, 1),       # 恰在边界：归下一小节，偏移 0
        (1, 1, 1.0, 1),       # 小节内 1 拍
    ]


def test_boundary_belongs_to_next_measure_including_repeated():
    total, locs = locate_rehearsal_timestamps(
        measure_count=4,
        beats_per_measure=[4] * 4,
        tempo_bpm=60,
        repeat_sections=[RepeatSection(1, 2, 2)],
        timestamps=[4.0, 8.0, 12.0, 16.0, 20.0],
        breaks=[2],
    )
    assert total == pytest.approx(24.0)
    # 序列 0,1,2,1,2,3，每小节 4 秒；边界依次归入 1,2,1,2,3
    assert [(l.measure, l.iteration, l.beat_offset) for l in locs] == [
        (1, 1, 0.0),
        (2, 1, 0.0),
        (1, 2, 0.0),
        (2, 2, 0.0),
        (3, 1, 0.0),
    ]


def test_beat_offset_inside_repeated_iteration():
    # [0,0] 演奏 2 遍、60 bpm、4 拍/小节；第 6 秒是第二遍内的第 2 拍
    _, locs = locate_rehearsal_timestamps(
        measure_count=1,
        beats_per_measure=[4],
        tempo_bpm=60,
        repeat_sections=[RepeatSection(0, 0, 2)],
        timestamps=[6.0],
        breaks=[],
    )
    assert locs[0].measure == 0
    assert locs[0].iteration == 2
    assert locs[0].beat_offset == pytest.approx(2.0)
    assert locs[0].page_index == 1


def test_page_mapping_uses_saved_breaks():
    breaks = [2, 5]  # 页 1: 0..1，页 2: 2..4，页 3: 5..
    assert page_for_measure(breaks, 0) == 1
    assert page_for_measure(breaks, 1) == 1
    assert page_for_measure(breaks, 2) == 2
    assert page_for_measure(breaks, 4) == 2
    assert page_for_measure(breaks, 5) == 3
    assert page_for_measure([], 99) == 1


def test_page_index_reflects_original_measure_not_timeline_position():
    # 重复第 1 页中的小节时，页码仍应为 1
    _, locs = locate_rehearsal_timestamps(
        measure_count=4,
        beats_per_measure=[4] * 4,
        tempo_bpm=60,
        repeat_sections=[RepeatSection(0, 1, 2)],
        timestamps=[11.0],  # 第二遍小节 0 内（展开序列第 3 次演奏的最后一拍）
        breaks=[2],
    )
    assert locs[0].measure == 0
    assert locs[0].iteration == 2
    assert locs[0].page_index == 1


def test_empty_timestamps_allowed():
    total, locs = locate_rehearsal_timestamps(
        measure_count=2,
        beats_per_measure=[4, 4],
        tempo_bpm=60,
        repeat_sections=[],
        timestamps=[],
        breaks=[],
    )
    assert total == pytest.approx(8.0)
    assert locs == []


def test_length_mismatch_rejected():
    with pytest.raises(RehearsalError, match="长度 2 与原谱小节数 3 不符"):
        locate_rehearsal_timestamps(
            measure_count=3,
            beats_per_measure=[4, 4],
            tempo_bpm=60,
            repeat_sections=[],
            timestamps=[0.0],
            breaks=[],
        )


def test_timestamp_at_total_end_is_out_of_range():
    with pytest.raises(RehearsalError, match="超出演奏总时长"):
        locate_rehearsal_timestamps(
            measure_count=1,
            beats_per_measure=[4],
            tempo_bpm=60,
            repeat_sections=[],
            timestamps=[4.0],  # 总时长 4 秒，恰在结束边界 -> 归不存在的下一小节
            breaks=[],
        )


def test_negative_and_over_end_timestamps_reported_together():
    with pytest.raises(RehearsalError, match="超出演奏总时长"):
        locate_rehearsal_timestamps(
            measure_count=1,
            beats_per_measure=[4],
            tempo_bpm=60,
            repeat_sections=[],
            timestamps=[-1.0, 99.0],
            breaks=[],
        )
    # 异常信息中应同时列出两个非法时间戳
    try:
        locate_rehearsal_timestamps(
            measure_count=1,
            beats_per_measure=[4],
            tempo_bpm=60,
            repeat_sections=[],
            timestamps=[-1.0, 99.0],
            breaks=[],
        )
        raise AssertionError("应当抛出 RehearsalError")
    except RehearsalError as exc:
        assert "-1" in str(exc) and "99" in str(exc)


def test_floating_point_near_boundary_snaps_to_next_measure():
    # 1.5s/小节；1.5 与 3.0 边界应稳定归下一小节
    _, locs = locate_rehearsal_timestamps(
        measure_count=3,
        beats_per_measure=[3] * 3,
        tempo_bpm=120,
        repeat_sections=[],
        timestamps=[1.5 - 1e-12, 3.0 - 1e-12],
        breaks=[],
    )
    assert [(l.measure, l.beat_offset) for l in locs] == [(1, 0.0), (2, 0.0)]


def test_timestamp_just_inside_end_snaps_to_end_boundary_and_fails():
    # 距结束边界仅浮点误差：视为恰在结束边界，归不存在的下一小节，按超出处理
    with pytest.raises(RehearsalError, match="超出演奏总时长"):
        locate_rehearsal_timestamps(
            measure_count=3,
            beats_per_measure=[3] * 3,
            tempo_bpm=120,
            repeat_sections=[],
            timestamps=[4.5 - 1e-12],
            breaks=[],
        )


def test_overlapping_sections_rejected():
    with pytest.raises(RehearsalError, match="重叠或嵌套"):
        build_timeline(
            [1] * 5, tempo_bpm=60,
            repeat_sections=[RepeatSection(0, 2, 2), RepeatSection(2, 4, 2)],
        )


def test_nested_sections_rejected():
    with pytest.raises(RehearsalError, match="重叠或嵌套"):
        build_timeline(
            [1] * 5, tempo_bpm=60,
            repeat_sections=[RepeatSection(0, 4, 2), RepeatSection(1, 2, 3)],
        )


def test_adjacent_sections_are_disjoint_and_allowed():
    # 端点均含：[0,1] 与 [2,3] 不相交，允许
    tl = build_timeline(
        [1] * 4, tempo_bpm=60,
        repeat_sections=[RepeatSection(0, 1, 2), RepeatSection(2, 3, 2)],
    )
    assert [p.measure for p in tl.sequence] == [0, 1, 0, 1, 2, 3, 2, 3]


def test_section_out_of_bounds_rejected():
    with pytest.raises(RehearsalError, match="越界"):
        build_timeline(
            [1] * 3, tempo_bpm=60,
            repeat_sections=[RepeatSection(1, 3, 2)],
        )


def test_section_bad_play_count_rejected():
    with pytest.raises(RehearsalError, match="2～4"):
        build_timeline([1] * 3, 60, [RepeatSection(0, 1, 5)])


def test_non_positive_beats_and_tempo_rejected():
    with pytest.raises(RehearsalError, match="拍数"):
        build_timeline([4, 0], 60, [])
    with pytest.raises(RehearsalError, match="每分钟拍数"):
        build_timeline([4, 4], 0, [])
