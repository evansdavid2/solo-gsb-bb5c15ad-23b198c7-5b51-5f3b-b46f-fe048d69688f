"""翻页点规划动态规划核心。

问题建模
========
小节按顺序编号 0..n-1，每小节高度 ``heights[i]``，第 i 小节之后的静默时间为
``silences[i]``（单位：秒）。一页可容纳连续若干小节，且总高度不超过
``page_height``。除最后一页外，每个断页位置 b（表示第 b-1 小节之后翻页，
即下一页从第 b 小节开始）必须满足 ``silences[b-1] >= min_turn_time``。

目标（按优先级）：
1. 最小化总页数；
2. 在页数最少的方案中，最大化所有翻页点静默时间的最小值；
3. 仍并列时，选择断页序号序列字典序最小的方案。

求解分三步（均为动态规划/基于 DP 的贪心重构）：
1. 前向最短路 DP 求最小页数 K；不可达即无合法方案；
2. 对"所有翻页点静默 >= t"这一可行性判定做二分（判定仍是同一条最短路 DP，
   只是允许的断页位置减少），求最大可行 t；
3. 在阈值 t 下做后缀 min/max 页数 DP，贪心选择最小的合法断页序号，
   得到字典序最小的断页序列。

单小节高度超过页面可用高度时无法排版，直接报错。
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

INF = 10**15


def _eps(magnitude: float) -> float:
    """随高度量级缩放的浮点容差，避免长序列前缀和的累加误差导致误判。"""
    return 1e-9 * max(1.0, abs(magnitude))


class PlanningError(ValueError):
    """规划失败：输入语义不合法（单小节超高、无合法方案等）。"""


@dataclass(frozen=True)
class Page:
    """一页的排版结果。小节序号均为 0 基、端点闭区间。"""

    page_index: int
    start_measure: int  # 起始小节（0 基）
    end_measure: int  # 结束小节（0 基，闭区间）
    measure_count: int
    used_height: float
    # 本页之后翻页点的静默秒数；最后一页无翻页点，为 None
    turn_silence: Optional[float]


@dataclass(frozen=True)
class Plan:
    pages: List[Page]
    page_count: int
    # 所有翻页点中最短的静默秒数；单页（无翻页点）时为 None
    shortest_turn_silence: Optional[float]
    # 断页序号序列：每个元素是"新一页起始小节"的 0 基序号
    breaks: List[int]


def _page_end_range(prefix: Sequence[float], r: int, page_height: float) -> tuple[int, int]:
    """以第 r 个小节（0 基）为页尾时，页首 l（0 基）的取值范围。

    页内容为小节 l..r，高度为 prefix[r+1] - prefix[l] <= page_height，
    即 prefix[l] >= prefix[r+1] - page_height。
    """
    threshold = prefix[r + 1] - page_height
    lo = bisect_left(prefix, threshold - _eps(threshold), 0, r + 1)
    return lo, r


def _min_pages_forward(
    n: int,
    prefix: Sequence[float],
    page_height: float,
    allowed: Callable[[int], bool],
) -> List[int]:
    """前向 DP：f[r] = 覆盖小节 0..r 所需的最少页数。

    一页小节 l..r 合法当且仅当高度不超限，且 l == 0 或断页位置 l 允许翻页。
    滑动窗口（l 的可行区间随 r 单调）用单调队列维护 f[l-1] 的最小值，
    整体 O(n)。
    """
    # 状态以"已覆盖小节数 m"表示：g[m] = f[m-1]，g[0] = 0（空前缀）。
    g: List[int] = [INF] * (n + 1)
    g[0] = 0
    dq: deque[int] = deque()
    for r in range(n):
        m = r  # 新候选：页 [r..r]，对应前缀状态 g[r]
        # 在 r 处断页意味着下一页从第 r 小节开始，r>0 时必须允许翻页。
        if g[m] < INF and (m == 0 or allowed(m)):
            while dq and g[dq[-1]] >= g[m]:
                dq.pop()
            dq.append(m)
        lo, _ = _page_end_range(prefix, r, page_height)
        while dq and dq[0] < lo:
            dq.popleft()
        g[r + 1] = g[dq[0]] + 1 if dq else INF
    return [g[r + 1] for r in range(n)]


def _suffix_min_max_pages(
    n: int,
    prefix: Sequence[float],
    page_height: float,
    allowed: Callable[[int], bool],
) -> tuple[List[int], List[int]]:
    """后缀 DP：从第 i 小节开始排完剩余内容所需的最少/最多页数。

    允许页数在 [smin[i], smax[i]] 之间连续可取（任意合法方案都能在允许翻页的
    位置把一页继续拆成两页，从而逐页 +1，故中间页数均可达）。
    窗口随 i 反向单调，用两个单调队列分别维护最小、最大值，整体 O(n)。
    """
    smin: List[int] = [INF] * (n + 1)
    smax: List[int] = [-INF] * (n + 1)
    smin[n] = 0
    smax[n] = 0

    min_dq: deque[int] = deque()  # 按 smin 值升序
    max_dq: deque[int] = deque()  # 按 smax 值降序

    for i in range(n - 1, -1, -1):
        # 上一轮窗口里 e == i 的候选已过期（页必须至少包含第 i 小节，e > i）。
        # 索引从右（最新、最小）加入，故从右端剔除 e <= i。
        while min_dq and min_dq[-1] <= i:
            min_dq.pop()
        while max_dq and max_dq[-1] <= i:
            max_dq.pop()
        # 新候选 e = i+1：页 [i..i] 后接后缀 i+1。断页位置 i+1 必须允许翻页，
        # 除非 i+1 == n（本页即最后一页）。
        e = i + 1
        if e == n or allowed(e):
            if smin[e] < INF:
                while min_dq and smin[min_dq[-1]] >= smin[e]:
                    min_dq.pop()
                min_dq.append(e)
            if smax[e] > -INF:
                while max_dq and smax[max_dq[-1]] <= smax[e]:
                    max_dq.pop()
                max_dq.append(e)
        # 页 [i..e-1] 高度 = prefix[e] - prefix[i] <= page_height，
        # 即 prefix[e] <= prefix[i] + page_height；bisect_left 给出第一个
        # 超限的索引，减 1 得最大合法 e。
        max_height = prefix[i] + page_height
        max_e = (
            bisect_left(prefix, max_height + _eps(max_height), i + 1, n + 1) - 1
        )
        while min_dq and min_dq[0] > max_e:
            min_dq.popleft()
        while max_dq and max_dq[0] > max_e:
            max_dq.popleft()
        if min_dq:
            smin[i] = smin[min_dq[0]] + 1
        if max_dq:
            smax[i] = smax[max_dq[0]] + 1
    return smin, smax


def plan_pages(
    heights: Sequence[float],
    silences: Sequence[float],
    page_height: float,
    min_turn_time: float,
) -> Plan:
    """计算最优翻页方案。

    :raises PlanningError: 单小节超高，或不存在任何合法断页方案。
    """
    n = len(heights)

    for i, h in enumerate(heights):
        if h > page_height + _eps(page_height):
            raise PlanningError(
                f"第 {i + 1} 小节高度 {h:g} 超过页面可用高度 {page_height:g}，"
                "小节不可拆开，无法排版。"
            )

    prefix: List[float] = [0.0]
    for h in heights:
        prefix.append(prefix[-1] + h)

    # 断页位置 b（下一页从第 b 小节开始，0 基）：
    # b == 0 是起点占位；内部翻页看第 b-1 小节后的静默；b == n 为文档结束，
    # 不作为翻页点。
    def baseline_allowed(b: int) -> bool:
        if b <= 0 or b >= n:
            return False
        return silences[b - 1] + _eps(min_turn_time) >= min_turn_time

    f = _min_pages_forward(n, prefix, page_height, baseline_allowed)
    total = f[n - 1]
    if total >= INF:
        raise PlanningError(
            "无合法翻页方案：在满足最低翻页时间的静默位置无法完成分页"
            "（存在小节无法与相邻小节排入同一页且其后静默不足）。"
        )

    if total == 1:
        page = Page(
            page_index=1,
            start_measure=0,
            end_measure=n - 1,
            measure_count=n,
            used_height=prefix[n],
            turn_silence=None,
        )
        return Plan(pages=[page], page_count=1, shortest_turn_silence=None, breaks=[])

    # ---- 第二步：最大化最短翻页静默 ----
    candidates = sorted(
        {silences[b - 1] for b in range(1, n) if baseline_allowed(b)}
    )

    def feasible(threshold: float) -> bool:
        allowed_t = (
            lambda b: b != 0 and b != n and silences[b - 1] + _eps(threshold) >= threshold
        )
        ft = _min_pages_forward(n, prefix, page_height, allowed_t)
        return ft[n - 1] <= total

    lo, hi = 0, len(candidates) - 1  # candidates[lo] 必然可行（等于基线）
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if feasible(candidates[mid]):
            lo = mid
        else:
            hi = mid - 1
    best_threshold = candidates[lo]

    # ---- 第三步：阈值固定，贪心取字典序最小断页序列 ----
    def best_allowed(b: int) -> bool:
        if b <= 0 or b >= n:
            return False
        return silences[b - 1] + _eps(best_threshold) >= best_threshold

    smin, smax = _suffix_min_max_pages(n, prefix, page_height, best_allowed)

    breaks: List[int] = []
    start = 0
    remaining = total
    while remaining > 1:
        # 本页结束后下一断页 b（页 [start..b-1]），之后后缀用 remaining-1 页排完。
        max_height = prefix[start] + page_height
        max_b = (
            bisect_left(prefix, max_height + _eps(max_height), start + 1, n + 1) - 1
        )
        chosen: Optional[int] = None
        need = remaining - 1
        for b in range(start + 1, max_b + 1):
            if best_allowed(b) and smin[b] <= need <= smax[b]:
                chosen = b
                break
        if chosen is None:  # 理论上不可达：阈值方案已被证明可行
            raise PlanningError("内部错误：重构翻页方案失败。")
        breaks.append(chosen)
        start = chosen
        remaining -= 1

    # ---- 组装页信息 ----
    boundaries = [0] + breaks + [n]
    pages: List[Page] = []
    turn_values: List[float] = []
    for idx in range(len(boundaries) - 1):
        a, e = boundaries[idx], boundaries[idx + 1]
        is_last = idx == len(boundaries) - 2
        silence = None if is_last else float(silences[e - 1])
        if silence is not None:
            turn_values.append(silence)
        pages.append(
            Page(
                page_index=idx + 1,
                start_measure=a,
                end_measure=e - 1,
                measure_count=e - a,
                used_height=prefix[e] - prefix[a],
                turn_silence=silence,
            )
        )

    shortest = min(turn_values) if turn_values else None
    return Plan(
        pages=pages,
        page_count=total,
        shortest_turn_silence=shortest,
        breaks=breaks,
    )
