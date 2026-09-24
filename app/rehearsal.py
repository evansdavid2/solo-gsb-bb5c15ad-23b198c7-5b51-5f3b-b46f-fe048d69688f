"""排练录音时间戳定位核心。

已保存的翻页规划描述的是原谱排版；排练录音则按实际演奏顺序进行：重复段会被
完整演奏若干遍。本模块负责：

1. 校验每小节拍数、速度与重复段（不相交、不嵌套、起止含端点、演奏 2~4 遍）；
2. 按谱面顺序展开重复段，得到实际演奏序列——每次完整演奏该段后再进入后续小节；
3. 以演奏开始为 0，按 ``每小节时长 = 60 × 拍数 / 速度`` 求前缀时间；
4. 用左闭右开区间（恰在边界归下一小节）把时间戳定位到：
   原谱 0 基小节号、重复段内第几遍（非重复段为 1）、小节内拍数偏移，
   并借助已存方案的分页结果给出 1 基页码。

本模块仅依赖标准库，便于脱离 Web 层直接单测。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import List, Sequence, Tuple


def _eps(magnitude: float) -> float:
    """随数量级缩放的浮点容差，抹平前缀和累加与 JSON 浮点解析误差。"""
    return 1e-9 * max(1.0, abs(magnitude))


class RehearsalError(ValueError):
    """排练定位输入不合法：数组长度不符、重复段重叠、时间戳越界等。"""


@dataclass(frozen=True)
class PlayedMeasure:
    """演奏序列中的一次小节发声。

    :param measure: 原谱小节号（0 基）
    :param playthrough: 所属重复段内的第几遍（1 基）；非重复段固定为 1
    """

    measure: int
    playthrough: int


@dataclass(frozen=True)
class Location:
    """单个时间戳的定位结果。"""

    measure: int  # 原谱 0 基小节号
    playthrough: int  # 重复段内第几遍；非重复段为 1
    beat_offset: float  # 该小节内的拍数偏移（自小节起点起）
    page_index: int  # 已存方案中的 1 基页码


# (起始小节, 结束小节, 演奏次数)，小节均为 0 基、两端皆含
Repeat = Tuple[int, int, int]


def _normalize_repeats(
    n: int, repeats: Sequence[Repeat]
) -> List[Tuple[int, int, int]]:
    """校验重复段并按起始小节排序。"""
    checked: List[Tuple[int, int, int]] = []
    for idx, (start, end, times) in enumerate(repeats):
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(times, int)
            or isinstance(start, bool)
            or isinstance(end, bool)
            or isinstance(times, bool)
        ):
            raise RehearsalError(f"第 {idx + 1} 个重复段的字段必须是整数")
        if start < 0 or end < 0 or start > n - 1 or end > n - 1:
            raise RehearsalError(
                f"第 {idx + 1} 个重复段不合法：起止小节必须落在 0..{n - 1}"
                f" 内（实际 {start}..{end}）"
            )
        if start > end:
            raise RehearsalError(
                f"第 {idx + 1} 个重复段起始小节（{start}）不能晚于"
                f"结束小节（{end}）"
            )
        if not 2 <= times <= 4:
            raise RehearsalError(
                f"第 {idx + 1} 个重复段（小节 {start}..{end}）的演奏次数"
                f"必须在 2~4 之间（实际 {times}）"
            )
        checked.append((start, end, times))

    checked.sort(key=lambda r: r[0])
    for prev, nxt in zip(checked, checked[1:]):
        # 端点均含：下一段起点落入上一段区间即为相交（含嵌套/首尾相接重叠）
        if nxt[0] <= prev[1]:
            raise RehearsalError(
                f"重复段 [{prev[0]}..{prev[1]}] 与 [{nxt[0]}..{nxt[1]}] "
                "重叠或嵌套：重复段必须互不相交且不嵌套（首尾相邻允许）"
            )
    return checked


def expand_repeats(
    n: int, repeats: Sequence[Repeat]
) -> List[PlayedMeasure]:
    """按谱面顺序展开重复段，返回实际演奏的小节序列。

    遇到重复段时完整演奏该段 ``times`` 遍（每遍都覆盖起止间的全部小节，
    遍号 1..times），随后再进入该段之后的小节；非重复段小节遍号为 1。
    """
    ordered = _normalize_repeats(n, repeats)
    by_start = {start: (end, times) for start, end, times in ordered}

    events: List[PlayedMeasure] = []
    i = 0
    while i < n:
        if i in by_start:
            end, times = by_start[i]
            for playthrough in range(1, times + 1):
                for measure in range(i, end + 1):
                    events.append(PlayedMeasure(measure, playthrough))
            i = end + 1
        else:
            events.append(PlayedMeasure(i, 1))
            i += 1
    return events


@dataclass(frozen=True)
class Rehearsal:
    """一次排练定位的已校验上下文：演奏序列、前缀时间与分页映射。"""

    measure_count: int
    tempo_bpm: float
    events: List[PlayedMeasure]
    start_times: List[float]  # start_times[k]：第 k 个演奏事件的开始时间
    total_duration: float
    page_starts: Tuple[int, ...]  # 每页起始原谱小节（0 基，升序，首个为 0）

    def page_of(self, measure: int) -> int:
        """原谱小节号 -> 已存方案的 1 基页码。"""
        return bisect_right(self.page_starts, measure)

    def locate(self, timestamp: float) -> Location:
        """定位单个时间戳；越界/为负时抛 RehearsalError。"""
        total = self.total_duration
        if timestamp < -_eps(total):
            raise RehearsalError(
                f"时间戳 {timestamp:g} 秒不能为负数（时间戳以演奏开始为 0）"
            )
        # 左闭右开：总时长处是最后一小节的右端点，不属于任何小节
        if timestamp >= total - _eps(total):
            raise RehearsalError(
                f"时间戳 {timestamp:g} 秒超出演奏总时长 {total:g} 秒"
                "（定位采用左闭右开区间，端点总时长不可定位）"
            )

        t = max(0.0, timestamp)  # 0 附近的负向舍入误差视为起点
        # 恰在边界归下一小节：先按严格小于取段，再用容差吸收浮点累加误差
        idx = bisect_right(self.start_times, t) - 1
        while (
            idx + 1 < len(self.events)
            and self.start_times[idx + 1] <= t + _eps(self.start_times[idx + 1])
        ):
            idx += 1

        event = self.events[idx]
        seconds_per_beat = 60.0 / self.tempo_bpm
        beat_offset = (t - self.start_times[idx]) / seconds_per_beat
        # 消除浮点噪声与可能的 -0.0，拍数偏移保留 9 位小数
        beat_offset = round(beat_offset, 9)
        if beat_offset == 0:
            beat_offset = 0.0
        return Location(
            measure=event.measure,
            playthrough=event.playthrough,
            beat_offset=beat_offset,
            page_index=self.page_of(event.measure),
        )

    def locate_all(self, timestamps: Sequence[float]) -> List[Location]:
        """批量定位；错误信息附带序号，便于定位具体时间戳。"""
        results: List[Location] = []
        for idx, ts in enumerate(timestamps):
            try:
                results.append(self.locate(ts))
            except RehearsalError as exc:
                raise RehearsalError(f"第 {idx + 1} 个时间戳：{exc}") from exc
        return results


def prepare_rehearsal(
    measure_count: int,
    beats_per_measure: Sequence[float],
    tempo_bpm: float,
    repeats: Sequence[Repeat],
    page_starts: Sequence[int],
) -> Rehearsal:
    """校验输入并构建排练定位上下文。

    :param measure_count: 已存规划的原谱小节数
    :param beats_per_measure: 每小节拍数，长度必须等于 ``measure_count``、元素为正
    :param tempo_bpm: 每分钟拍数，必须为正
    :param repeats: 重复段序列，彼此不相交、不嵌套，起止含、演奏 2~4 遍
    :param page_starts: 已存方案每页起始小节（0 基升序，首个为 0）
    :raises RehearsalError: 任一语义约束不满足
    """
    if measure_count <= 0:
        raise RehearsalError("规划小节数必须为正数")
    if len(beats_per_measure) != measure_count:
        raise RehearsalError(
            f"beats_per_measure 数组长度（{len(beats_per_measure)}）"
            f"与规划小节数（{measure_count}）不符"
        )
    for i, beats in enumerate(beats_per_measure):
        if not isinstance(beats, (int, float)) or isinstance(beats, bool):
            raise RehearsalError(f"第 {i + 1} 小节拍数必须是数值")
        beats_f = float(beats)
        if beats_f != beats_f or beats_f in (float("inf"), float("-inf")):
            raise RehearsalError(f"第 {i + 1} 小节拍数必须是有限数值")
        if beats_f <= 0:
            raise RehearsalError(
                f"第 {i + 1} 小节拍数必须为正数（实际 {beats_f:g}）"
            )
    if not isinstance(tempo_bpm, (int, float)) or isinstance(tempo_bpm, bool):
        raise RehearsalError("每分钟拍数必须是数值")
    tempo = float(tempo_bpm)
    if tempo != tempo or tempo in (float("inf"), float("-inf")):
        raise RehearsalError("每分钟拍数必须是有限数值")
    if tempo <= 0:
        raise RehearsalError(f"每分钟拍数必须为正数（实际 {tempo:g}）")

    page_tuple = tuple(sorted(page_starts))
    if not page_tuple or page_tuple[0] != 0:
        raise RehearsalError("已存方案分页信息不合法：首页必须从第 0 小节开始")

    # _normalize_repeats 在 expand_repeats 内完成；此处提前调用以先报错
    events = expand_repeats(measure_count, repeats)

    seconds_per_beat = 60.0 / tempo
    start_times: List[float] = []
    elapsed = 0.0
    for event in events:
        start_times.append(elapsed)
        elapsed += float(beats_per_measure[event.measure]) * seconds_per_beat

    return Rehearsal(
        measure_count=measure_count,
        tempo_bpm=tempo,
        events=events,
        start_times=start_times,
        total_duration=elapsed,
        page_starts=page_tuple,
    )
