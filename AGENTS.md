# 项目约定

## 先读

1. `docs/REQUIREMENTS.md` — 已对齐的范围与验收标准
2. `docs/ARCHITECTURE.md` — 分层、流水线、数据表
3. `docs/API.md` — 对外契约
4. `docs/TAG_SCHEMA.md` — 标签词表与归一规则
5. `docs/DEVELOPMENT.md` — 运行、测试与已知限制

## 边界

- 本项目是独立模块。**不要修改** `D:\cutput`、`D:\tools\shot_workbench`
  或其他项目的代码与数据。
- 源视频只读。派生文件一律写 `data/`。
- 不提交 `config.local.json`、`data/`、日志或任何密钥。

## 不可破坏的约束

- 片段 ID 算法（源哈希 + 毫秒区间）不得变更；变更必须带迁移。
- 人工锁定 `reviewed=1` 与 `origin='manual'` 的片段不得被自动分析覆盖。
- 标签只存受控词表的规范名；新增标签需同步补测试。
- `分类` 是目录派生的开放维度：由 `library.category_for()` 生成，别让模型输出分类，
  也别在 `VOCABULARY["分类"]` 里写死目录名。改目录拼写用 `category_aliases`。
- AI 失败必须降级为 `needs_review`，不能让整批入库失败。
- 检索不得触发 FFmpeg 或视频分析。
- 当前阶段不引入向量库；向量检索是第二阶段。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q material_library
node --check web/app.js
```

涉及 UI 的改动，启动 `serve` 后用浏览器实际走一遍入库、检索与保存，不只看接口。
