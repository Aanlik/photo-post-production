# Photo Post-Production Skill

把一个 RAW/JPEG 文件夹交给 Codex：Skill 会生成预览、分类评分、筛选候选照片，并规划可恢复的 Lightroom 与 Photoshop 后期。它保留原片，把运行结果写到输入目录之外；只有取得实际编辑、回读和导出证据，才会报告成片完成。

> [!IMPORTANT]
> 当前仓库尚未完成真实 Adobe 端到端验收。缺少 Lightroom、Photoshop 或聊天窗口 `image_gen` 能力时，相关步骤会保持待处理或降级为 `global-only`；生成计划不等于已经精修。

## 快速开始

在 Codex 对话中直接说：

> 处理 `/path/to/photos`，自动分类、评分并精修优选照片，输出高质量 JPG 和可编辑 PSD。

默认采用自动模式、自然增强意图和 `mixed` 处理策略；只有明确要求查看或确认中间结果时才进入人工审核模式。安装前请确认运行环境：macOS、Python 3.11+ 和 Pillow；RAW 预览依赖 macOS `sips` 或已配置的本地解码方式。实际 Adobe 编辑还需要 Lightroom Classic MCP、Photoshop MCP/UXP Bridge 及相应的健康能力。生成式修改只使用当前聊天窗口内置的 `image_gen`，不使用 API Key。

将仓库中的 Skill 安装到**当前 Codex 配置的 Skill 根目录**；以下是默认路径示例：

```bash
mkdir -p "$HOME/.codex/skills/photo-post-production"
rsync -a photo-post-production/ "$HOME/.codex/skills/photo-post-production/"
```

安装后重新打开 Codex 或开启新对话，让 Skill 清单刷新。若只想先检查预览和计划，无须连接 Adobe；后期执行则必须满足对应能力门。

## 处理流程

1. **准备与筛选**：生成只读源文件清单、预览和技术指标；分类后分别给出保留价值、可编辑性、预期收益，并生成 A/B/C 审核板。候选分按 `65% / 20% / 15%` 加权，低置信度或未通过技术门的照片不会仅凭高分自动进入精修。
2. **计划与执行**：为候选照片记录 Lightroom 全局设置和 Photoshop 操作图。Adobe 适配器可用时，按能力门执行、回读参数并保留检查点；一张照片/变体使用一个 Photoshop 文档和一个最终分层 PSD。
3. **验证与导出**：检查真实像素变化、语义风险、操作回执、可编辑主文件和导出文件。`web-share` 与 `competition-quality` 是 JPEG 输出规格；`competition-standard` 是更严格的成片判定，不能由文件名或输出规格自动推断。

风格记忆和用户反馈可为后续批次提供高层次指导。生成式步骤会保存提示词、来源和输入/输出证据，不会把原始 RAW 直接交给聊天生成工具。

## 从命令行运行

以下命令在仓库根目录执行。`--output` 必须指向输入目录之外的新运行目录。

只准备预览和技术清单：

```bash
python3 photo-post-production/scripts/prepare_batch.py \
  --input "/absolute/path/to/photos" \
  --output "/absolute/path/to/new-run"
```

生成分类评分、审核板、编辑计划和可恢复队列，并尝试执行可用的本地适配器：

```bash
python3 photo-post-production/scripts/run_pipeline.py \
  --input "/absolute/path/to/photos" \
  --output "/absolute/path/to/new-run"
```

此命令默认 `--mode auto --intent natural-enhancement --processing-locality mixed`。只允许本地步骤时增加 `--processing-locality local-only`；指定其他意图或人工审核时，可使用 `--intent`、`--mode review`。它会尝试执行已配置的 Lightroom/Photoshop 适配器；能力不足时留下待处理任务，不会凭计划声称编辑完成。聊天生成式操作仍需当前宿主调用内置 `image_gen`。

在所需适配器可用后，从运行目录中的计划执行或恢复：

```bash
python3 photo-post-production/scripts/execution_engine.py \
  --plan "/absolute/path/to/new-run/execution-plan.json"
```

如果执行结果已存在，只需将其同步到顶层报告：

```bash
python3 photo-post-production/scripts/execution_engine.py \
  --plan "/absolute/path/to/new-run/execution-plan.json" \
  --sync-report
```

完整的审核指令、生成式步骤和更多示例见[中文使用说明](photo-post-production/使用说明.md)。

## 看懂运行结果

| 位置 | 主要内容 |
| --- | --- |
| `report.json` | 顶层状态、能力降级与下一步 |
| `scores.json`、`scores.csv` | 分类、三项评分及解释；有真实渲染证据时追加成片分 |
| `review-board.md`、`review-A.jpg`、`review-B.jpg` | A/B/C 审核材料 |
| `execution-plan.json`、`queue.sqlite3` | 可恢复的操作计划与任务队列 |
| `execution-results.json`、`quality-report.json` | 适配器回执、回读、像素/语义和导出检查 |
| `rollback-ledger.json`、`selected/` 或变体目录 | 回退记录和通过门禁的输出 |

看到 `pending-adapter-or-review` 时，先按 `report.json` 中的原因补足能力或审核；看到 `global-only` 时，该照片只有全局处理证据，不能当作 Photoshop 精修成片。输出 JPEG 是否可发布，以 `quality-report.json` 和导出验证结果为准。

## 开发与接手

源码入口是 [`photo-post-production/SKILL.md`](photo-post-production/SKILL.md)；脚本位于 `photo-post-production/scripts/`，字段、能力和输出契约位于 `photo-post-production/references/`。本次只更新远端 README：公开 `main` 仍保留旧发布源码和 Git 历史，尚未同步本地清理后的完整交接基线。
