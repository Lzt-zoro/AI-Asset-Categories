# AI 智能视频素材库

本地运行的视频素材索引：批量入库 → 硬切检测 → 镜头内动作定位 → 多维度标签 →
SQLite 检索 → 返回「源文件 + 起止时间」供剪辑工具直接引用。

本源不修改 `D:\tools\shot_workbench` 与 `D:\cutput` 的任何代码或数据；二者仅作为后续
对接目标。当前版本为独立的 MVP 模块。

## 快速开始

```powershell
cd D:\ai_material_classification
install.cmd                      # 创建 .venv 并安装依赖
copy config.example.json config.local.json
run_web.cmd                      # 打开 http://127.0.0.1:5288
```

首次使用流程：

1. 点击右上「入库 / 分析」，填入素材文件夹（如 `D:\materials`）。
2. 选择「扫描并分析」。只会分析本次新增或内容变化的文件；已入库且未变化的素材不会
   重跑 AI。任务在后台执行，关掉窗口不会中断，右上角会继续显示进度。
3. 左侧按标签筛选，或直接输入中文描述检索。
4. 在右侧面板核对描述与标签，必要时修改并「确认并锁定」。

子文件夹会自动成为「分类」：扫描 `\\NAS\素材\ai` 下的 `疼点使用\疼点涂抹\a.mp4`，
该文件的所有片段都带 `疼点使用 / 疼点涂抹`。模型也会在同一维度补充画面用途，
例如开场钩子、使用演示或美好展示；目录分类和画面分类会同时保留。

未配置 AI 时，片段仍会按硬切与动作能量切出并生成缩略图，状态为「待复核」，可人工
补充标签，检索与引用功能完全可用。

### 分类路径与拼写

分类来自共享盘的目录名，同时也保留受控的画面用途分类；程序不会重命名源目录。旧数据
和查询中的「痛」会归一为「疼」，
只影响索引结果，不修改共享盘目录：

```json
{
  "category_aliases": { "痛点": "疼点" }
}
```

## 配置 AI

编辑 `config.local.json`：

```json
{
  "ai": {
    "enabled": true,
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "sk-..."
  }
}
```

或使用环境变量（优先于文件）：`MATERIAL_LIBRARY_AI=1`、
`MATERIAL_LIBRARY_BASE_URL`、`MATERIAL_LIBRARY_MODEL`、`MATERIAL_LIBRARY_API_KEY`。

只支持 OpenAI 兼容的 `POST {base_url}/chat/completions` 多模态协议。每次标注发送
3 张片段关键帧，不发送整段视频。

每次标注只询问 7 个 AI 维度（分类、人物、景别、场景、动作、产品、情绪）。
目录路径不发送给模型；`光线/色彩/构图` 仅在人工编辑时可用，都不占用 token。

普通素材按动作能量定位完整动作区间，并保留动作开始和结束的缓冲，不为凑够 3 秒强行补入其他动作；
默认不会返回少于 1 秒的普通素材片段。成片模式按硬切
镜头保留完整镜头，并增加 `素材类型=成片` 标签。扫描时可在界面选择素材类型，也可使用：

```powershell
.venv\Scripts\python.exe -m material_library scan D:\finished --material-type 成片
```

同名 `.srt/.vtt/.ass` 字幕会按片段时间区间读取，作为 AI 辅助和搜索文本，不是必需输入。

## CLI

```powershell
.venv\Scripts\python.exe -m material_library scan D:\materials
.venv\Scripts\python.exe -m material_library analyze --limit 50
.venv\Scripts\python.exe -m material_library search 涂抹 --tag 景别:近景
.venv\Scripts\python.exe -m material_library search 涂抹祛痘霜
.venv\Scripts\python.exe -m material_library search "" --min-duration 2 --max-duration 4 --json
.venv\Scripts\python.exe -m material_library stats
```

### 对外接口

服务同时提供 `/api/*` 与 `/api/v1/*`（同一套路由，`v1` 是给外部工具的稳定别名）。
其他项目只需要：

```text
GET /api/v1/clips?tags=动作:涂抹&category=疼点使用&min_duration=2&max_duration=4
→ items[].path / start / end / score / tags / category
```

完整契约、并发约定与 schema 迁移说明见 `docs/API.md`。

## 目录结构

```
material_library/      核心包：配置、数据库、媒体、动作定位、AI 标注、检索、服务
web/                   本地前端（无构建步骤）
data/                  运行数据（不入库）
  library.db           SQLite 索引
  cache/thumbs/        片段缩略图
  cache/clips/         按需裁剪的预览缓存
docs/                  需求、架构、标签体系、接口契约、路线图
tests/                 自动化测试
```

## 数据与安全

- 源视频只读：只记录绝对路径与 SHA-256，不移动、不改名、不覆盖。
- 虚拟切片：`clips.start/end` 指向原视频区间，默认不生成重复文件。
- 删除片段只删索引行，不会删除源文件。
- 服务默认只监听 `127.0.0.1`。
- `config.local.json` 与 `data/` 已加入 `.gitignore`，不会提交密钥或素材。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## 后续对接

见 `docs/API.md` 的 `/api` 契约与 `docs/ROADMAP.md`。对接 Cutput 时只需读取
`path/start/end`，由 Cutput 负责裁剪、替换、拼接与导出；素材库不解码输出成片。
