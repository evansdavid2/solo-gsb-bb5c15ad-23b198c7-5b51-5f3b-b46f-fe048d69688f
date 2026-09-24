# 演出乐谱翻页点规划 API

根据依次排列的小节高度、每小节后的静默秒数、页面可用高度与最低翻页时间，自动规划演出用电子乐谱的翻页点。小节不可拆开，只能在静默时间达标的位置**提前翻页**或**满页翻页**。此外，可为已保存的规划提交排练录音的拍数、速度、重复段与时间戳，定位每个时间戳对应原谱的哪一小节、重复段内的第几遍、小节内拍数偏移及所在页码。

- 技术栈：Python 3.11 + FastAPI + SQLite（结果保存在本地）
- 算法：动态规划，整体 O(n)（第二步为对候选静默值的二分，O(n log n) 上界）

## 规划目标（按优先级）

在所有合法分页方案中依次比较：

1. **页数最少**；
2. 页数相同时，**所有翻页点中最短的静默时间最大**（最紧张的那次翻页尽量从容）；
3. 仍并列时，**断页序号序列字典序最小**（同等条件下尽量早翻页，使方案确定、可复现）。

合法性约束：

- 每页包含连续若干小节，小节不可拆开，总高度 ≤ 页面可用高度（恰好等于允许）；
- 除最后一页外，翻页位置（某小节之后）的静默秒数必须 ≥ 最低翻页时间；
- 某**单小节高度超过页面可用高度**，或按上述规则**不存在合法分页方案**时，接口返回 `422` 并给出明确中文错误信息。

## 单条命令启动

需要安装 Docker（含 Docker Compose 插件），在项目根目录执行：

```bash
docker compose up -d --build
```

启动后服务监听 **http://localhost:8000**（容器内 8000 端口）。依赖安装与 SQLite 建表初始化均随容器启动自动完成，无需手动初始化。

停止：`docker compose down`（结果数据保留在宿主机 `./data` 目录）。

## 必要配置（docker-compose.yml）

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| 端口映射 | `8000:8000` | 宿主机端口:容器端口，宿主机 8000 被占用时改左边即可 |
| `DATA_DIR` | `/data` | 结果文件目录，SQLite 数据库 `plans.db` 存于此处 |
| 数据卷 | `./data:/data` | 将规划结果持久化到宿主机；删除该映射则数据仅存于容器内 |

不使用 Compose 时等价命令：

```bash
docker build -t page-turn-planner .
docker run -d -p 8000:8000 -v "$(pwd)/data:/data" -e DATA_DIR=/data page-turn-planner
```

## API 地址与端口

基址：`http://localhost:8000`（Docker Compose 映射宿主机 `8000` 端口到容器 `8000`）

- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`GET http://localhost:8000/health`
- 提交规划：`POST http://localhost:8000/api/v1/plans`
- 按 ID 查询：`GET http://localhost:8000/api/v1/plans/{id}`
- 排练录音时间戳定位：`POST http://localhost:8000/api/v1/plans/{id}/rehearsal/locate`

### 1. 提交规划

`POST http://localhost:8000/api/v1/plans`

请求体（JSON）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `measure_heights` | number[] | 依次排列的每小节高度（与页面高度同单位），非空、元素为正 |
| `silence_seconds` | number[] | 每小节之后可供翻页的静默秒数，长度与高度数组一致、非负 |
| `page_height` | number | 页面可用高度，正数 |
| `min_turn_time` | number | 最低翻页时间（秒），非负 |

```bash
curl -X POST http://localhost:8000/api/v1/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "measure_heights": [3, 3, 4, 2],
    "silence_seconds": [2, 5, 1, 0],
    "page_height": 6,
    "min_turn_time": 2
  }'
```

响应 `201`：

```json
{
  "id": "2fb0c76562414d59ae41c49accabbd78",
  "created_at": "2026-09-23T17:24:35.516775+00:00",
  "request": { "...": "回显请求参数" },
  "result": {
    "page_count": 2,
    "shortest_turn_silence": 5.0,
    "breaks": [2],
    "pages": [
      {
        "page_index": 1,
        "start_measure": 0,
        "end_measure": 1,
        "measure_count": 2,
        "used_height": 6.0,
        "turn_silence": 5.0
      },
      {
        "page_index": 2,
        "start_measure": 2,
        "end_measure": 3,
        "measure_count": 2,
        "used_height": 6.0,
        "turn_silence": null
      }
    ]
  }
}
```

结果字段说明：

- `breaks`：断页序号序列，每个元素是“新一页起始小节”的 **0 基**小节序号；
- `pages[].start_measure` / `end_measure`：本页小节范围（0 基，结束为闭区间）；
- `pages[].turn_silence`：本页之后翻页点的静默秒数；最后一页无翻页点，为 `null`；
- `shortest_turn_silence`：所有翻页点中最短的静默秒数；整篇只有一页（无翻页点）时为 `null`。

### 2. 按 ID 查询结果

`GET http://localhost:8000/api/v1/plans/{id}`

```bash
curl http://localhost:8000/api/v1/plans/2fb0c76562414d59ae41c49accabbd78
```

- 找到：`200`，响应结构与提交接口相同；
- 不存在：`404`，`{"detail": "规划结果不存在：..."}`。

### 3. 排练录音时间戳定位

`POST http://localhost:8000/api/v1/plans/{id}/rehearsal/locate`

对一个**已保存的规划**提交排练参数，批量定位录音时间戳在原谱中的位置。重复段按谱面顺序展开——每次完整演奏该段（覆盖起止间全部小节）后再进入后续小节；每小节时长为 `60 × 拍数 / 速度`；时间戳以演奏开始为 `0`，定位采用**左闭右开**区间（`[小节起点, 下一小节起点)`），恰在边界的时间戳归下一小节；恰好等于总时长不可定位。

请求体（JSON）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `beats_per_measure` | number[] | 与原小节等长的每小节拍数，长度必须等于规划小节数、元素为正 |
| `tempo_bpm` | number | 每分钟拍数（BPM），正数 |
| `repeats` | object[] | 若干**不相交且不嵌套**的重复段（可省略）；首尾相邻允许，共享小节即报错 |
| `repeats[].start_measure` | number | 重复段起始小节（0 基，含） |
| `repeats[].end_measure` | number | 重复段结束小节（0 基，含） |
| `repeats[].play_count` | number | 整段演奏次数，`2～4`（遍号从 1 计） |
| `timestamps` | number[] | 录音时间戳数组（秒，以演奏开始为 0，非负），可为空数组 |

```bash
curl -X POST http://localhost:8000/api/v1/plans/2fb0c76562414d59ae41c49accabbd78/rehearsal/locate \
  -H 'Content-Type: application/json' \
  -d '{
    "beats_per_measure": [4, 4, 3, 4],
    "tempo_bpm": 120,
    "repeats": [
      {"start_measure": 1, "end_measure": 2, "play_count": 2}
    ],
    "timestamps": [0, 2, 4, 6, 7.5, 8, 10]
  }'
```

上例中重复小节 1..2 演奏两遍，实际演奏序列为 `0, 1, 2, 1, 2, 3`，总时长 11 秒。响应 `200`：

```json
{
  "id": "2fb0c76562414d59ae41c49accabbd78",
  "request": { "...": "回显定位请求参数" },
  "total_duration": 11.0,
  "locations": [
    {"measure": 0, "playthrough": 1, "beat_offset": 0.0, "page_index": 1},
    {"measure": 1, "playthrough": 1, "beat_offset": 0.0, "page_index": 1},
    {"measure": 2, "playthrough": 1, "beat_offset": 0.0, "page_index": 2},
    {"measure": 1, "playthrough": 2, "beat_offset": 1.0, "page_index": 1},
    {"measure": 2, "playthrough": 2, "beat_offset": 0.0, "page_index": 2},
    {"measure": 2, "playthrough": 2, "beat_offset": 1.0, "page_index": 2},
    {"measure": 3, "playthrough": 1, "beat_offset": 2.0, "page_index": 2}
  ]
}
```

结果字段说明（与 `timestamps` 一一对应）：

- `measure`：时间戳所在小节的**原谱 0 基小节号**；
- `playthrough`：该小节在所属重复段内的**第几遍**（1 基，取值 1～`play_count`）；非重复段小节恒为 `1`；
- `beat_offset`：该小节内的**拍数偏移**（自本小节起点起算，可为小数）；
- `page_index`：该原谱小节在**已存方案中的 1 基页码**（重复演奏不改变分页，同小节各遍页码相同）；
- `total_duration`：按重复段展开后的演奏总时长（秒），即所有被演奏小节的时长之和。

定位规则：小节 `i` 第 `p` 遍的时间区间为 `[t, t + 60·beats[i]/tempo_bpm)`；时间戳等于某遍起点时归该遍小节，等于末尾总时长时报越界错误。

### 错误响应

| 状态码 | 触发条件 |
| --- | --- |
| `422` | 参数校验失败（空数组、长度不一致、负数/零高度、多余字段等） |
| `422` | 单小节超高：`第 k 小节高度 … 超过页面可用高度 …，小节不可拆开，无法排版。` |
| `422` | 无合法方案：`无合法翻页方案：在满足最低翻页时间的静默位置无法完成分页…` |
| `404` | 按 ID 查询无记录 |
| `404` | 排练定位时规划不存在：`规划结果不存在：{id}` |
| `422` | `beats_per_measure 数组长度（…）与规划小节数（…）不符` |
| `422` | 重复段重叠或嵌套：`重复段 [a..b] 与 [c..d] 重叠或嵌套：…（首尾相邻允许）` |
| `422` | 重复段演奏次数超出 2～4，或起止小节越界 / 起始晚于结束 |
| `422` | 时间戳超出总时长或为负：`第 k 个时间戳：时间戳 … 秒超出演奏总时长 … 秒…` |

## 算法说明

设小节 0..n-1，前缀高度和 `P`，断页位置 b 表示下一页从第 b 小节开始。

1. **最少页数**：前向 DP，`f[r]` = 排完 0..r 的最少页数，转移
   `f[r] = 1 + min{ f[l-1] : l 使高度和 ≤ page_height 且 (l=0 或 silences[l-1] ≥ min_turn_time) }`；
   l 的可行区间随 r 单调，用单调队列（滑动窗口最小值）O(n) 维护。不可达即无合法方案。
2. **最大化最短静默**：将所有翻页点静默 ≥ t 的方案视为“可行”，t 越大可行位置越少、可行性单调，故在去重后的候选静默值上二分；每次可行性判定复用第 1 步的 DP。
3. **字典序最小断页序列**：阈值固定后做后缀 DP，求每个位置排完剩余部分所需的最少/最多页数（允许的页数在两者之间连续可达，单调队列 O(n) 维护窗口最小/最大值）；自首页起从小到大试探断页位置 b，要求剩余小节恰可用指定页数排完，第一个可行的 b 即为字典序最小选择。

单小节超高在任何计算前直接判定并报错。

## 本地开发与测试

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload            # 本地启动
python -m pip install pytest && python -m pytest -q   # 运行测试
```

测试包含 200 组随机用例与穷举全部分页方案的结果对照、边界条件与 10 万小节规模性能用例，以及排练重复段展开、左闭右开边界定位、页码映射与接口端到端用例。
