"""排练录音时间戳定位核心。

已保存的翻页方案只描述**原谱**分页；实际演奏存在反复段，录音时间轴对应的
是"展开后"的小节序列。本模块负责：

1. 校验每小节拍数数组（长度与原谱小节数一致、各项为正）、速度（正数）与
   重复段（起止为 0 基闭区间、演奏次数 2～4、两两不相交且不嵌套）；
2. 按谱面顺序展开重复段：走到重复段 [a, b] 时把该段完整连续演奏指定遍数，
   再进入第 b+1 小节；非重复小节只演奏一遍；
3. 每小节时长 = 60 × 拍数 / 每分钟拍数，时间戳以演奏开始为 0，小节区间
   左闭右开，**恰在边界归入下一小节**；
4. 把每个时间戳定位为（原谱 0 基小节号、重复段内第几遍、小节内拍数偏移），
   并依据已存方案的断页序列给出 1 基页码。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import List, Sequence, Tuple


def _eps(magnitude: float) -> float:
    """随数值量级缩放的浮点容差，避免前缀和累加误差导致边界误判。"""
    return 1e-9 * max(1.0, abs(magnitude))


class RehearsalError(ValueError):
    """定位请求语义不合法（数组长度不符、重复段重叠/越界、时间戳超出总时长等）。"""


@dataclass(frozen=True)
class RepeatSection:
    """重复段：原谱 0 基闭区间 [start_measure, end_measure]，演奏 times 遍。"""

    start_measure: int
    end_measure: int
    times: int


@dataclass(frozen=True)
class PlayedMeasure:
    """展开后序列中的一次小节演奏。"""

    measure: int  # 原谱 0 基小节号
    iteration: int  # 重复段内第几遍（1 基）；非重复小节为 1


@dataclass(frozen=True)
class Location:
    """单个时间戳的定位结果。"""

    timestamp: float
    measure: int  # 原谱 0 基小节号
    iteration: int  # 重复段内第几遍；非重复段为 1
    beat_offset: float  # 该小节内的拍数偏移（可为小数）
    page_index: int  # 已存方案的 1 基页码


@dataclass(frozen=True)
class Timeline:
    """展开后的演奏时间轴。prefix[j] 为第 j 次演奏的开始时刻，长度为小节数 +1。"""

    sequence: List[PlayedMeasure]
    durations: List[float]
    prefix: List[float]
    tempo_bpm: float

    @property
    def total_duration(self) -> float:
        return self.prefix[-1]


def page_for_measure(breaks: Sequence[int], measure: int) -> int:
    """按已存方案的断页序列（每页起始小节，0 基、升序）求小节所在 1 基页码。"""
    return bisect_right(breaks, measure) + 1


def _normalize_sections(
    n: int, repeat_sections: Sequence[RepeatSection]
) -> List[RepeatSection]:
    """校验重复段并按起始小节排序：端点合法、次数 2～4、两两不相交且不嵌套。"""
    ordered = sorted(repeat_sections, key=lambda s: (s.start_measure, s.end_measure))
    prev_end = -1
    for sec in ordered:
        if sec.times < 2 or sec.times > 4:
            raise RehearsalError(
                f"重复段 [{sec.start_measure}, {sec.end_measure}] 的演奏次数 "
                f"{sec.times} 不在 2～4 之间。"
            )
        if (
            sec.start_measure < 0
            or sec.start_measure > sec.end_measure
            or sec.end_measure >= n
        ):
            raise RehearsalError(
                f"重复段 [{sec.start_measure}, {sec.end_measure}] 越界：规划共 "
                f"{n} 个小节，起止必须是 0～{n - 1} 内的闭区间（均含）。"
            )
        # 端点均含，故后一段起点 <= 前一段终点即为重叠（嵌套也属此情形）。
        if sec.start_measure <= prev_end:
            raise RehearsalError(
                f"重复段 [{sec.start_measure}, {sec.end_measure}] 与前一个重复段"
                f"重叠或嵌套：各重复段必须两两不相交（起止小节均含）。"
            )
        prev_end = sec.end_measure
    return ordered


def build_timeline(
    beats_per_measure: Sequence[float],
    tempo_bpm: float,
    repeat_sections: Sequence[RepeatSection],
) -> Timeline:
    """校验参数并按谱面顺序展开重复段，构造演奏时间轴。"""
    n = len(beats_per_measure)
    if n == 0:
        raise RehearsalError("beats_per_measure 不能为空。")
    for i, beats in enumerate(beats_per_measure):
        if beats <= 0:
            raise RehearsalError(f"第 {i} 小节拍数 {beats:g} 不是正数。")
    if tempo_bpm <= 0:
        raise RehearsalError(f"每分钟拍数 {tempo_bpm:g} 必须为正数。")

    sections = _normalize_sections(n, repeat_sections)

    sequence: List[PlayedMeasure] = []
    i = 0
    k = 0
    while i < n:
        if k < len(sections) and sections[k].start_measure == i:
            sec = sections[k]
            for iteration in range(1, sec.times + 1):
                # 每次完整演奏该段后才进入后续小节
                sequence.extend(
                    PlayedMeasure(measure=m, iteration=iteration)
                    for m in range(sec.start_measure, sec.end_measure + 1)
                )
            i = sec.end_measure + 1
            k += 1
        else:
            sequence.append(PlayedMeasure(measure=i, iteration=1))
            i += 1

    durations = [
        60.0 * beats_per_measure[played.measure] / tempo_bpm
        for played in sequence
    ]
    prefix: List[float] = [0.0]
    for duration in durations:
        prefix.append(prefix[-1] + duration)
    return Timeline(
        sequence=sequence,
        durations=durations,
        prefix=prefix,
        tempo_bpm=tempo_bpm,
    )


def _segment_index(timestamp: float, timeline: Timeline) -> int:
    """返回时间戳落入的展开序列下标（左闭右开，恰在边界归下一小节）。"""
    prefix = timeline.prefix
    j = bisect_right(prefix, timestamp) - 1
    # 时间戳略小于下一边界（浮点误差内）视为恰在边界，归入下一小节。
    nxt = j + 1
    if nxt < len(prefix) and prefix[nxt] - timestamp <= _eps(prefix[nxt]):
        return nxt
    return j


def locate_rehearsal_timestamps(
    measure_count: int,
    beats_per_measure: Sequence[float],
    tempo_bpm: float,
    repeat_sections: Sequence[RepeatSection],
    timestamps: Sequence[float],
    breaks: Sequence[int],
) -> Tuple[float, List[Location]]:
    """把一组排练录音时间戳定位回原谱。

    :param measure_count: 已存规划的原谱小节数（beats_per_measure 必须等长）
    :param breaks: 已存方案的断页序列（每页起始小节，0 基、升序）
    :returns: (演奏总时长, 与 timestamps 一一对应的定位结果)
    :raises RehearsalError: 长度不符、重复段非法或存在超出总时长的时间戳。
    """
    if len(beats_per_measure) != measure_count:
        raise RehearsalError(
            f"beats_per_measure 长度 {len(beats_per_measure)} 与原谱小节数 "
            f"{measure_count} 不符（每个原谱小节须恰好给一个拍数）。"
        )

    timeline = build_timeline(beats_per_measure, tempo_bpm, repeat_sections)
    total = timeline.total_duration
    tolerance = _eps(total)

    # 合法区间 [0, total)：负数、超过总时长、恰在结束边界（归不存在的"下一小节"）
    # 均按超出总时长处理。
    invalid = [t for t in timestamps if t < 0.0 or t >= total - tolerance]
    if invalid:
        rendered = "、".join(f"{t:g}" for t in invalid)
        raise RehearsalError(
            f"时间戳 {rendered} 超出演奏总时长 {total:g} 秒：合法范围为左闭右开"
            f"区间 [0, {total:g})，恰在结束边界归入下一小节（不存在），亦按超出处理。"
        )

    locations: List[Location] = []
    for timestamp in timestamps:
        j = _segment_index(timestamp, timeline)
        played = timeline.sequence[j]
        into = timestamp - timeline.prefix[j]
        if into <= _eps(timeline.prefix[j + 1]):
            into = 0.0  # 吸附到小节边界
        beat_offset = into * timeline.tempo_bpm / 60.0
        if abs(beat_offset) < 1e-9:
            beat_offset = 0.0
        locations.append(
            Location(
                timestamp=timestamp,
                measure=played.measure,
                iteration=played.iteration,
                beat_offset=round(beat_offset, 9),
                page_index=page_for_measure(breaks, played.measure),
            )
        )
    return round(total, 9), locations
