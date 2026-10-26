# 本地接口契约

基础地址：`http://127.0.0.1:5288`。所有响应为 JSON（文件端点除外）。

两套路径指向同一套路由：

- `/api/*` — 当前版本，Web UI 使用。
- `/api/v1/*` — 稳定别名，供 Cutput 等外部工具固定版本使用，不出现在 OpenAPI 里。

字段只增不改。新增能力先加字段，不改语义；破坏性变化时升 `v2` 并保留 `v1`。

## 查询

`GET /api/clips`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `q` | string | 关键词，空格/逗号/斜杠分词，匹配描述、标签、分类、文件名 |
| `tags` | string | 逗号分隔的 `维度:标签`；不带维度时自动推断 |
| `category` | string | 逗号分隔的分类路径；父分类自动包含子分类 |
| `min_duration` / `max_duration` | float | 片段时长范围（秒） |
| `status` | string | `pending` / `ready` / `needs_review` / `rejected` |
| `material_type` | string | `普通素材` / `成片` |
| `source_id` | int | 限定源文件 |
| `limit` / `offset` | int | 分页，limit ≤ 200 |

响应：

```json
{
  "total": 12,
  "offset": 0,
  "limit": 40,
  "items": [
    {
      "id": "clip_06fbee4dbe8f0992b90f",
      "source_id": 1,
      "shot_id": 3,
      "path": "D:\\materials\\demo.mp4",
      "start": 4.0,
      "end": 5.867,
      "duration": 1.867,
      "description": "女生在纯色背景前涂抹产品",
      "transcript": "痘痘反复长真的很烦",
      "material_type": "成片",
      "category": "疼点使用 / 疼点涂抹",
      "action_score": 1.0,
      "quality_score": 0.5,
      "status": "ready",
      "reviewed": true,
      "score": 0.72,
      "tags": {
        "分类": ["疼点使用 / 疼点涂抹"],
        "景别": ["近景"],
        "动作": ["涂抹"]
      },
      "thumbnails": ["D:\\...\\cache\\thumbs\\cl\\clip_....jpg"]
    }
  ]
}
```

**稳定字段**：`id`、`path`、`start`、`end`、`duration`、`score`、`tags`、`category`、`status`。
剪辑工具只需要 `path + start + end`。

## 单条与编辑

- `GET /api/clips/{id}` → 单条片段，结构同上。
- `PATCH /api/clips/{id}`

```json
{
  "description": "可选",
  "tags": {"景别": ["近景"]},
  "category": "疼点使用 / 疼点涂抹",
  "reviewed": true,
  "status": "ready",
  "start": 4.1,
  "end": 6.0
}
```

- `tags` 会整体替换人工标签；不在词表内的值被忽略。`分类` 不能通过 `tags` 写入；
  `素材类型` 是由扫描模式生成的系统标签，也不能通过 `tags` 手工写入。
- `category` 写在源文件上，会同步到该源文件的全部片段。
- `分类` 还可以由 AI/人工在片段级补充；目录分类保留为 `origin=folder`，不会被覆盖。
- `transcript` 是可选字幕辅助文本；同名 `.srt/.vtt/.ass` 会按时间区间自动读取。
- 修改 `start/end` 会把片段标记为 `origin='manual'`，后续自动分析不覆盖。

- `POST /api/clips/{id}/relabel` → 强制 AI 重新标注（清空 auto 标签，保留人工与分类）。
- `DELETE /api/clips/{id}` → 只删索引，不删源文件。

## 媒体

- `GET /api/clips/{id}/thumbnail?index=0..2` → JPEG。
- `GET /api/clips/{id}/preview` → 按区间裁剪的 MP4（首次请求生成并缓存）。
- `GET /api/clips/{id}/media` → 原文件（供浏览器 seek 预览，建议配合 `#t=`）。

## 任务

`POST /api/scan` `{"path": "D:\\materials", "recursive": true, "material_type": "普通素材"}`

返回后台任务。任务结果的 `source_ids` 是本次新增或内容变化的源文件，可直接回传给
`/api/analyze`，因此 UI 的「扫描并分析」不会顺手把整库重新分析一遍：

```json
{
  "found": 203, "added": 134, "updated": 0,
  "unchanged": 69, "duplicate": 0, "failed": 0,
  "source_ids": [12, 13, 14]
}
```

`material_type` 可选值为 `普通素材` 或 `成片`。成片模式保留硬切镜头区间，并写入
系统标签 `素材类型=成片`；普通素材模式优先保留完整动作区间。

`POST /api/analyze`

- `{"source_ids": [12, 13, 14]}` — 只分析指定源文件。
- `{"limit": 50}` — 分析最早的 50 个待处理源文件。
- `{"source_ids": []}` — 表示「没有新素材」，返回 `200 {"started": false, "reason": ...}`，
  而不是错误。

`GET /api/job` → `{kind,status,current,total,message,error,result}`

同一时刻仅允许一个任务；忙时返回 `409`。任务在后台线程执行，关闭网页不会中断。

## 元数据

- `GET /api/health` → 版本、AI 状态、数据目录
- `GET /api/stats` → 来源/片段状态计数、片段总秒数
- `GET /api/tags` → 维度、完整词表、已用标签计数、分类计数

```json
{
  "dimensions": ["分类", "人物", "景别"],
  "ai_dimensions": ["分类", "人物", "景别", "场景", "动作", "产品", "情绪"],
  "vocabulary": {"动作": ["涂抹"], "分类": ["疼点使用", "疼点使用 / 疼点涂抹"]},
  "counts": {"动作": {"涂抹": 12}},
  "categories": ["疼点使用", "疼点使用 / 疼点涂抹"],
  "category_counts": {"疼点使用": 50, "疼点使用 / 疼点涂抹": 20}
}
```

`category_counts` 按树形顺序排列（父级紧跟子级），父级计数包含所有后代。

- `GET /api/sources?limit=200` → 已登记文件

## 面向 Cutput 的最小用法

```python
import httpx

BASE = "http://127.0.0.1:5288/api/v1"

def find_clip(tags: list[str], duration: float, category: str = ""):
    params = {
        "tags": ",".join(tags),
        "min_duration": duration * 0.8,
        "max_duration": duration * 1.2,
        "limit": 20,
    }
    if category:
        params["category"] = category
    response = httpx.get(f"{BASE}/clips", params=params, timeout=5)
    response.raise_for_status()
    items = response.json()["items"]
    if not items:
        return None
    best = items[0]
    return best["path"], best["start"], best["end"], best["score"]
```

按叙事节奏选材由调用方编排，素材库只负责逐条查询：

```python
beats = [
    ("开场钩子", "美好展示", 2.0),
    ("疼点呈现", "疼点使用", 3.0),
    ("美好展示", "美好展示", 2.5),
]
plan = [find_clip([beat], seconds, category) for beat, category, seconds in beats]
```

素材库不负责渲染、替换或拼接；这些仍由 Cutput 完成。

## 并发与迁移

- 一个进程写库（`BEGIN IMMEDIATE` + WAL）；外部工具请只读查询，不要并发写同一个
  `library.db`。需要写入时走 HTTP。
- `PRAGMA user_version` 记录 schema 版本（当前 3）。启动时自动做加法迁移
  （`ALTER TABLE ADD COLUMN`）；如果库版本高于程序支持版本，会拒绝启动而不是悄悄
  写坏数据。
- 片段 ID = `clip_<sha256(源文件哈希, 起止毫秒)[:20]>`，同一个源文件的同一区间永远
  得到同一个 ID，可安全用作去重键。
