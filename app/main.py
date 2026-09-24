"""FastAPI 入口：提交翻页规划、按 ID 查询结果、排练录音时间戳定位。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .planner import Plan, PlanningError, plan_pages
from .rehearsal import (
    Location,
    RehearsalError,
    prepare_rehearsal,
)
from .storage import PlanStore

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "plans.db")

store: Optional[PlanStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    os.makedirs(DATA_DIR, exist_ok=True)
    store = PlanStore(DB_PATH)  # 启动时完成建表初始化
    yield


app = FastAPI(
    title="演出乐谱翻页点规划 API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 数据模型


class PlanRequest(BaseModel):
    """提交规划的请求体。"""

    model_config = ConfigDict(extra="forbid")

    measure_heights: List[float] = Field(
        ..., description="依次排列的每小节高度（与页面可用高度同单位）"
    )
    silence_seconds: List[float] = Field(
        ..., description="每小节之后可供翻页的静默秒数"
    )
    page_height: float = Field(..., description="页面可用高度")
    min_turn_time: float = Field(..., description="最低翻页时间（秒）")

    @model_validator(mode="after")
    def _validate(self) -> "PlanRequest":
        if not self.measure_heights:
            raise ValueError("measure_heights 不能为空")
        if len(self.silence_seconds) != len(self.measure_heights):
            raise ValueError(
                "silence_seconds 长度必须与 measure_heights 一致（每小节一个静默值）"
            )
        if any(h <= 0 for h in self.measure_heights):
            raise ValueError("measure_heights 中每个小节高度必须为正数")
        if any(s < 0 for s in self.silence_seconds):
            raise ValueError("silence_seconds 中静默秒数不能为负数")
        if self.page_height <= 0:
            raise ValueError("page_height 必须为正数")
        if self.min_turn_time < 0:
            raise ValueError("min_turn_time 不能为负数")
        return self


class PageOut(BaseModel):
    page_index: int = Field(..., description="页码（从 1 开始）")
    start_measure: int = Field(..., description="起始小节序号（0 基）")
    end_measure: int = Field(..., description="结束小节序号（0 基，闭区间）")
    measure_count: int
    used_height: float
    turn_silence: Optional[float] = Field(
        None, description="本页之后翻页点的静默秒数；最后一页为 null"
    )


class PlanResult(BaseModel):
    page_count: int
    shortest_turn_silence: Optional[float] = Field(
        None, description="所有翻页点中最短的静默秒数；单页时为 null"
    )
    breaks: List[int] = Field(..., description="断页序号序列（新页起始小节，0 基）")
    pages: List[PageOut]


class PlanResponse(BaseModel):
    id: str
    created_at: str
    request: PlanRequest
    result: PlanResult


def _plan_to_result(plan: Plan) -> PlanResult:
    return PlanResult(
        page_count=plan.page_count,
        shortest_turn_silence=plan.shortest_turn_silence,
        breaks=plan.breaks,
        pages=[PageOut(**vars(p)) for p in plan.pages],
    )


# ---------------------------------------- 排练录音时间戳定位


class RepeatSectionIn(BaseModel):
    """一个重复段：起止小节均含（0 基），演奏 2~4 遍。"""

    model_config = ConfigDict(extra="forbid")

    start_measure: int = Field(..., description="重复段起始小节（0 基，含）")
    end_measure: int = Field(..., description="重复段结束小节（0 基，含）")
    play_count: int = Field(..., description="整段演奏次数（2~4，首尾两遍均完整演奏）")

    @model_validator(mode="after")
    def _validate(self) -> "RepeatSectionIn":
        if self.start_measure < 0 or self.end_measure < 0:
            raise ValueError("重复段起止小节不能为负数")
        if self.start_measure > self.end_measure:
            raise ValueError(
                f"重复段起始小节（{self.start_measure}）不能晚于结束小节"
                f"（{self.end_measure}）"
            )
        if not 2 <= self.play_count <= 4:
            raise ValueError(
                f"重复段（小节 {self.start_measure}..{self.end_measure}）演奏次数"
                f"必须在 2~4 之间（实际 {self.play_count}）"
            )
        return self


class RehearsalLocateRequest(BaseModel):
    """排练录音时间戳定位请求体。"""

    model_config = ConfigDict(extra="forbid")

    beats_per_measure: List[float] = Field(
        ...,
        description="与原小节等长的每小节拍数，长度必须与规划小节数一致、元素为正",
    )
    tempo_bpm: float = Field(..., description="每分钟拍数，必须为正")
    repeats: List[RepeatSectionIn] = Field(
        default_factory=list,
        description="若干不相交且不嵌套的重复段（起止小节均含、演奏次数 2~4）",
    )
    timestamps: List[float] = Field(
        ..., description="排练录音时间戳数组（秒，以演奏开始为 0）"
    )

    @model_validator(mode="after")
    def _validate(self) -> "RehearsalLocateRequest":
        if any(b != b or b in (float("inf"), float("-inf")) for b in self.beats_per_measure):
            raise ValueError("beats_per_measure 中每个拍数必须是有限数值")
        if any(b <= 0 for b in self.beats_per_measure):
            raise ValueError("beats_per_measure 中每个小节拍数必须为正数")
        if self.tempo_bpm != self.tempo_bpm or self.tempo_bpm in (
            float("inf"),
            float("-inf"),
        ):
            raise ValueError("tempo_bpm 必须是有限数值")
        if self.tempo_bpm <= 0:
            raise ValueError("tempo_bpm（每分钟拍数）必须为正数")
        if any(t != t or t in (float("inf"), float("-inf")) for t in self.timestamps):
            raise ValueError("时间戳必须是有限数值（不接受 NaN 或 Infinity）")
        if any(t < 0 for t in self.timestamps):
            raise ValueError("时间戳不能为负数（以演奏开始为 0）")
        return self


class LocationOut(BaseModel):
    measure: int = Field(..., description="原谱小节号（0 基）")
    playthrough: int = Field(
        ..., description="所属重复段内第几遍（1 基）；非重复段小节恒为 1"
    )
    beat_offset: float = Field(..., description="该小节内的拍数偏移（自小节起点起）")
    page_index: int = Field(..., description="已存方案的页码（1 基）")


class RehearsalLocateResponse(BaseModel):
    id: str
    request: RehearsalLocateRequest
    total_duration: float = Field(
        ..., description="按重复段展开后的演奏总时长（秒）"
    )
    locations: List[LocationOut] = Field(
        ..., description="与 timestamps 一一对应的定位结果"
    )


def _location_to_out(loc: Location) -> LocationOut:
    return LocationOut(
        measure=loc.measure,
        playthrough=loc.playthrough,
        beat_offset=loc.beat_offset,
        page_index=loc.page_index,
    )


# ---------------------------------------------------------------- 接口


@app.post("/api/v1/plans", response_model=PlanResponse, status_code=201)
def create_plan(payload: PlanRequest) -> PlanResponse:
    """提交一次翻页规划，返回最优方案并保存到本地。"""
    try:
        plan = plan_pages(
            heights=payload.measure_heights,
            silences=payload.silence_seconds,
            page_height=payload.page_height,
            min_turn_time=payload.min_turn_time,
        )
    except PlanningError as exc:
        # 单小节超高 / 无合法方案：语义错误，明确报错
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = _plan_to_result(plan)
    assert store is not None
    plan_id = store.save(payload.model_dump(), result.model_dump())
    record = store.get(plan_id)
    assert record is not None
    return PlanResponse(
        id=record["id"],
        created_at=record["created_at"],
        request=payload,
        result=result,
    )


@app.get("/api/v1/plans/{plan_id}", response_model=PlanResponse)
def get_plan(plan_id: str) -> PlanResponse:
    """按 ID 查询已保存的规划结果。"""
    assert store is not None
    record = store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划结果不存在：{plan_id}")
    return PlanResponse(
        id=record["id"],
        created_at=record["created_at"],
        request=PlanRequest(**record["request"]),
        result=PlanResult(**record["result"]),
    )


@app.post(
    "/api/v1/plans/{plan_id}/rehearsal/locate",
    response_model=RehearsalLocateResponse,
)
def locate_rehearsal_timestamps(
    plan_id: str, payload: RehearsalLocateRequest
) -> RehearsalLocateResponse:
    """按规划 ID 对排练录音时间戳做定位。

    依据提交的每小节拍数、速度与重复段，按谱面顺序展开实际演奏序列，
    以左闭右开区间把每个时间戳定位到原谱小节、重复遍号、拍数偏移和页码。
    """
    assert store is not None
    record = store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划结果不存在：{plan_id}")

    plan_request = PlanRequest(**record["request"])
    plan_result = PlanResult(**record["result"])
    page_starts = [p.start_measure for p in plan_result.pages]

    try:
        rehearsal = prepare_rehearsal(
            measure_count=len(plan_request.measure_heights),
            beats_per_measure=payload.beats_per_measure,
            tempo_bpm=payload.tempo_bpm,
            repeats=[
                (r.start_measure, r.end_measure, r.play_count)
                for r in payload.repeats
            ],
            page_starts=page_starts,
        )
        locations = rehearsal.locate_all(payload.timestamps)
    except RehearsalError as exc:
        # 数组长度不符 / 重复段重叠 / 时间戳越界等语义错误：明确报错
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return RehearsalLocateResponse(
        id=record["id"],
        request=payload,
        total_duration=rehearsal.total_duration,
        locations=[_location_to_out(loc) for loc in locations],
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
