# 架构

## 分层

```
web/                 浏览器 UI（原生 ES module 风格的 app.js + CSS）
  └── /api/*         FastAPI
material_library/
  server.py          HTTP 契约、后台任务状态
  cli.py             命令行入口
  library.py         编排：入库、分析、标注、检索、编辑
  media.py           ffmpeg/ffprobe、抽帧、裁剪、场景检测
  motion.py          帧差能量、动作区间定位、候选评分
  labeling.py        provider 协议 + OpenAI 兼容多模态实现
  tags.py            受控词表、别名归一
  db.py              SQLite schema 与连接管理
  config.py          配置合并、环境变量覆盖
```

依赖方向是单向的：`server → library → (media, motion, labeling, tags, db)`。
`media` 与 `motion` 不感知数据库；`tags` 不感知 AI；因此三者都可单独测试与复用。

## 处理流水线

1. **登记**（`scan`）
   - 递归枚举视频扩展名，跳过隐藏目录。
   - 用 `(path, size, mtime)` 做增量判断；未变化直接 `unchanged`。
   - 变化或新增文件计算 SHA-256；已有同哈希记录则标记 `duplicate_of`，跳过分析。

2. **镜头检测**（`analyze_source`）
   - PySceneDetect（默认 `AdaptiveDetector`），阈值与最短镜头来自配置。
   - 结果写入 `shots`，重建时先按 `source_id` 删除旧镜头。

3. **动作定位**（每个镜头）
   - 以 12 fps、宽 160px 灰度解码一次，计算相邻帧平均绝对差；窗口长度按实际时间戳换算，
     不因编码器的 nominal FPS 造成不足 1 秒。
   - 平滑后按 `max(mean + 0.45·std, 0.22·peak)` 阈值二值化，使用滞回并合并约 0.65s 内间隙。
   - 普通素材在每个运动区域内取动作完整区间，并用滞回阈值保留动作开始和结束的
     低速部分，默认补充少量上下文且不返回少于 1 秒的片段；短硬切不再产生更短的伪片段，
     不为了凑目标时长扩展到
     其他动作。静态镜头才回退到目标时长窗口。成片跳过动作窗口，直接保留硬切镜头。
   - 每个镜头最多 3 个互不重叠候选；无运动时退化为单窗口。
   - 画质分 = 区域内清晰度（方差）相对中位数，暗/过曝帧降权。

4. **落库**
   - 片段 ID = `sha256(source_sha256, round(start*1000), round(end*1000))[:20]`。
   - 已人工锁定（`reviewed=1`）或 `origin='manual'` 的片段不会被重新分析覆盖。
   - 抽 3 帧缩略图（15% / 50% / 85%），失败不阻塞流程。
   - 目录分类写在 `sources.category`（相对扫描根目录，` / ` 连接层级），并作为
     `origin='folder'` 的分类标签挂到该源文件的每个片段上；AI/人工画面用途也写入
     同一 `分类` 维度，但保留各自 origin。

5. **标注**
   - provider 收到片段名、时长、区间与 3 张帧。
   - prompt 只包含 7 个 AI 维度；目录路径不发给模型，但内置的画面用途分类属于
     `分类` 维度；`光线/色彩/构图` 为手动维度，也不发送。
   - 返回的标签经 `tags.sanitize` 限定到受控词表，未知词直接丢弃。
   - 失败或未配置时状态为 `needs_review`，不影响检索。
   - 重新标注只删除 `origin='auto'` 的标签，保留人工标签。

6. **检索**
   - SQL 先做硬过滤（标签维度、分类、时长、状态、来源），再在 Python 中计算加权分。
   - 分类是层级路径，选父级时用 `s.category = ? OR s.category LIKE 'parent / %'`
     把子目录一起返回。
   - 排序权重：标签 0.40、文本 0.25、动作 0.15、时长 0.12、画质 0.08。
   - 已锁定片段 +0.03，鼓励使用已确认素材。
   - 检索不调用 FFmpeg，也不触发分析。

## 数据库

| 表 | 用途 |
| --- | --- |
| `sources` | 源文件路径、SHA-256、尺寸、时长、状态、重复指向 |
| `shots` | 每个源文件的硬切区间 |
| `clips` | 虚拟片段：区间、分数、描述、状态、人工锁定 |
| `tags` | 受控词表（维度 + 规范名 + 别名行） |
| `clip_tags` | 片段与标签多对多，含置信度与来源 |
| `thumbnails` | 缩略图路径（按片段 + 位置） |
| `jobs` | 预留：任务持久化（当前任务状态在内存） |

连接策略：每线程一个连接，WAL 模式，写入用 `BEGIN IMMEDIATE`。所有多步写操作在
一个事务内完成，异常自动回滚。

`sources.category` 是 schema 2 新增列，`sources.material_type` 与 `clips.transcript`
是 schema 3 新增列，schema 4 迁移统一旧分类与清理旧自动「使用产品」标签；启动时用 `PRAGMA table_info` 判断并
`ALTER TABLE ADD COLUMN` 平滑迁移；`PRAGMA user_version` 大于程序支持版本时拒绝启动。

## 并发与失败

- 扫描/分析在后台线程执行，同一时刻只允许一个任务；再次提交返回 409。
- 单个文件失败只记该 `sources.status='failed'` 与错误摘要，不中断整批。
- 标注失败回写 `needs_review`，错误挂在 `sources.error`。
- 任务状态在进程内存；重启后未完成的任务恢复为 `pending`，可再次分析。

## 性能取舍

- 源视频只解码一次采样（`sample_frames` 单次读取），避免逐段 seek。
- 分析使用 160px 灰度图，单镜头开销约几十毫秒级。
- 缩略图与预览缓存复用以文件 mtime 生成的 `.stamp` 校验，源文件变化即失效。
- SQLite + 纯 Python 打分在万级片段下仍可毫秒级响应；超过此规模再引入 FTS5/向量库。

## 与现有项目的关系

- `D:\cutput\cutput\scene_detector.py`：本项目采用同类的 PySceneDetect 流程，但未
  import 该模块，避免两个项目版本与依赖互相绑定。
- `D:\tools\shot_workbench\workbench\media.py`：仅参考其 ffmpeg 调用约定，未修改。
- 未来合并时，`library.py` 的 `search()` 与 `/api/*` 是推荐的接缝；Cutput 只需
  消费路径与区间，不需要了解本项目的数据库。

## 对外复用设计

后续「成片拆分入库 → 按叙事节奏组片」不需要改动现有表结构：

1. **拆分入库**：调用方（Cutput/工作台）自己分析成片，把每个镜头作为一条 `clips`
   记录写入，或直接复用本项目的 `scan + analyze`。稳定键是
   `clip_id = sha256(源哈希, 起止毫秒)`，同一区间重复导入不会产生重复行。
2. **打标签**：写入时沿用 `tags.VOCABULARY` 与 `origin` 约定。目录派生的 `分类`
   使用 `origin='folder'`，AI/人工画面分类使用 `auto/manual`；调用方也可通过
   `PATCH category` 修改源文件的目录分类，保证两个项目看到同一套分类。
3. **按节奏组片**：调用方按「钩子 + 痛点 + 美好展示」逐段查询 `/api/v1/clips`
   （标签 + 分类 + 时长），拿到 `path/start/end` 后自行拼接。素材库不感知时间线，
   也不反向依赖任何剪辑项目。

接口是唯一的跨项目接缝：只读 HTTP + `v1` 前缀，避免共享 SQLite schema 带来的耦合。
