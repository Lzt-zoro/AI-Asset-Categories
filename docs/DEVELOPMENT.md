# 开发说明

## 环境

- Windows + Python 3.12
- ffmpeg / ffprobe 在 `PATH`（已验证 ffmpeg 9.0）
- 可选 NVIDIA GPU，不参与当前 CPU 流水线

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 运行

```powershell
.venv\Scripts\python.exe -m material_library serve
.venv\Scripts\python.exe -m material_library serve --port 5299
```

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q material_library
node --check web/app.js
```

## 修改约定

- 源文件只读；所有派生数据写 `data/`。
- 不修改 `D:\cutput` 或 `D:\tools\shot_workbench`。
- 新增标签必须同时补 `VOCABULARY` 与别名测试。
- 新增目录分类不需要重新识别旧片段：重扫只更新 `sources.category`，新文件才进入分析。
  新增 AI 语义分类写进 `category_labels`，只影响之后的标注；旧片段需要时再单独补标。
  目录变化、标签词表变化与重新调用模型是三个独立动作，不必每次重新切片。
- 需要新增 AI 维度时，同时改 `DIMENSIONS`、`AI_DIMENSIONS` 与 `VOCABULARY`，
  否则模型不会回答该维度。
- 普通素材的动作定位使用 12 fps 灰度采样、滞回阈值和动作前后上下文，默认不返回
  少于 1 秒的片段；源镜头本身短于 1 秒时例外。调整后，旧片段不会自动改时间区间，
  需要重新分析对应源文件才会使用新定位结果。
- 显式重分析使用 `python -m material_library analyze --source-id <id> --reanalyze`；
  只替换未锁定的自动片段，人工锁定和人工时间区间会保留。
- 片段 ID 算法不得变更；如需变更必须提供迁移。
- 检索硬过滤放 SQL，重排序放 `_score`，不要在 SQL 中写权重。
- 任何 AI 相关失败都必须降级为 `needs_review`，不得让入库整体失败。
- 不把密钥写入代码或提交 `config.local.json`。

## 已知限制

- 旧目录中的「痛」会在入库和数据库迁移时归一为「疼」，不修改共享盘原目录。
- 成片模式保留硬切镜头，不做普通素材的动作窗口扩展；普通素材优先输出完整动作区间。
- 分类树只在有视频的层级出现；空目录不会建节点。
- 动作定位基于全局帧差，快速摇镜头或全屏转场也可能被识别为动作；靠 `action_score`
  与人工复核兜底。
- 单镜头最多 3 个候选，密集动作（如连续多次涂抹）可能只保留最佳 3 段。
- 中文关键词检索是子串匹配，不是分词索引；量级增大后换 FTS5。
- AI 描述依赖抽帧，镜头内快速变化可能漏判。
- 任务状态在内存，进程崩溃后需重新触发分析（已完成片段不会重做）。
