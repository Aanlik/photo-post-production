# Photo Post-Production Skill

`photo-post-production` 是一个面向 RAW/JPEG 照片的本地后期 Skill。它将照片分类、评分、筛选、Lightroom 全局处理、Photoshop 精细后期、可选的聊天窗口生成式修改、质量检查和高质量导出组织成一个可恢复流程。

Skill 默认自动运行。用户只需用自然语言描述任务，例如“处理这个 RAW 文件夹，筛选并精修优选照片”，无需手写 Skill 名称、后端、提示词或逐张确认。原始 RAW 只读，所有运行结果写入新的运行目录。

## 功能

- 自动分类：风光/植物、城市风景、建筑/城市空间、街头/纪实、人物/环境肖像、动物/野生动物和其他待确认。
- 三项筛选评分：保留价值、可编辑性、预期收益；候选分为 `65% / 20% / 15%` 加权结果。
- 评分解释：记录得分依据、风险、置信度和后期后可能的视觉方向。
- 自动生成 A/B/C 审核板和聊天窗口联系表，支持按编号批量保留、淘汰或转人工审核。
- Lightroom 负责 RAW 解码、曝光、白平衡、曲线、镜头校正和全局色彩。
- Photoshop 负责图层、蒙版、局部调整、元素移除、人像美化、液化、修复、仿制、减淡/加深、透视和高级精修。
- 一张照片只使用一个 Photoshop 文档、一个分层 PSD 和最终导出文件；中间操作使用图层和 History/History Snapshot 回退，不生成多个中间 PSD。
- 输出 `web-share` 和 `competition-quality` 高质量 JPEG，并保留 PSD 主文件和参数/操作来源。
- 参数写入后执行回读校验；调用成功但实际参数未写入、像素未变化或导出证据不完整时，不报告为成功成片。
- 支持本机风格记忆、参考作品元数据、用户反馈和项目级校准；只保存来源与高层次风格属性，不复制无授权的互联网作品。
- 生成式修改采用 Hybrid：仅在当前聊天窗口提供内置 `image_gen` 时调用，不使用 OpenAI API Key，不把原始 RAW 直接发送给生成式后端。

## 目录结构

```text
photo-post-production/
├── SKILL.md                    # 自动触发规则和完整编排流程
├── agents/openai.yaml           # Codex Skill 显示名称与默认提示
├── references/                  # 评分、编辑计划、Adobe 契约和输出规范
├── scripts/                    # 本地分析、队列、计划、适配器和质量门
└── 使用说明.md                 # 中文操作说明
```

## 安装

将 `photo-post-production/` 目录安装到当前 Codex 的 Skill 目录：

```bash
mkdir -p "$HOME/.codex/skills/photo-post-production"
rsync -a photo-post-production/ "$HOME/.codex/skills/photo-post-production/"
```

实际使用时以当前 Codex 配置的 Skill 根目录为准。安装后重新打开 Codex，或开启新对话以确保 Skill 清单刷新。

## 使用

### 自然语言

直接在对话中描述目标即可：

> 处理 `/path/to/photos`，自动分类、评分，只精修优选照片，并输出高质量 JPG 和可编辑 PSD。

Skill 默认使用 `auto` 模式和 `natural-enhancement` 意图。只有用户明确要求查看或确认中间结果时才使用人工审核模式。

### 命令行准备预览

```bash
python3 photo-post-production/scripts/prepare_batch.py \
  --input "/absolute/path/to/raw-folder" \
  --output "/absolute/path/to/new-run"
```

### 一键分析、评分和生成计划

```bash
python3 photo-post-production/scripts/run_pipeline.py \
  --input "/absolute/path/to/raw-folder" \
  --output "/absolute/path/to/new-run" \
  --mode auto \
  --intent competition-standard \
  --processing-locality mixed
```

仅做本地处理时使用 `--processing-locality local-only`。可选意图包括 `natural-enhancement`、`documentary-truthful`、`editorial-expression`、`competition-standard` 和 `commercial/creative`。

### 断点恢复

```bash
python3 photo-post-production/scripts/execution_engine.py \
  --plan "/absolute/path/to/new-run/execution-plan.json"
```

只同步已有执行结果到顶层报告：

```bash
python3 photo-post-production/scripts/execution_engine.py \
  --plan "/absolute/path/to/new-run/execution-plan.json" \
  --sync-report
```

## 前置条件

- macOS，推荐 Python 3.11 或更高版本。
- Python 包 `Pillow`；视觉预览和本地分析依赖它。
- RAW 预览依赖 macOS `sips` 或已配置的本地 RAW 解码路径。
- Lightroom Classic 已启动，并已配置可用的 Lightroom MCP；Skill 会回读 Develop 状态和导出结果。
- Photoshop 已启动，并已配置可用的 Photoshop MCP/UXP Bridge。精细后期还需要通过健康检查的图层、蒙版、操作清单和导出能力。
- 生成式修改需要当前 Codex/ChatGPT 聊天窗口暴露内置 `image_gen`。没有该工具时，任务会保持待处理或降级为 `global-only`，不会偷偷改走 API。

Lightroom 和 Photoshop 是独立能力门。某一端不可用时，运行会保留结构化计划、失败原因和恢复入口，不会把“已排队”写成“已完成”。

## 输出和质量门

运行目录通常包含：

| 文件 | 用途 |
| --- | --- |
| `report.json` | 顶层状态、照片结果、能力降级和下一步 |
| `scores.json` / `scores.csv` | 分类、三项评分、解释和最终成片分 |
| `review-board.md` / `review-A.jpg` / `review-B.jpg` | 快速人工审核材料 |
| `execution-plan.json` | 可恢复的 Lightroom/Photoshop 操作计划 |
| `execution-results.json` | 适配器实际回执、参数回读和导出证据 |
| `quality-report.json` | 真实像素、语义、技术和发布质量门结果 |
| `rollback-ledger.json` | 回退点和不可逆操作记录 |
| `queue.sqlite3` | 可暂停、可恢复的本地任务队列 |
| `selected/` 或变体目录 | 通过门禁的 PSD/JPEG 输出 |

`competition-quality` 是输出规格，不等于自动保证艺术质量。只有真实像素有材料变化、计划操作有回执、参数回读一致、导出校验通过、语义检查通过且精细后端能力完整时，才允许标记为 `competition-standard`。否则报告会保留 `global-only`、`completed-with-quality-gates` 或 `pending-adapter-or-review` 等准确状态。

## 安全约束

- 不覆盖、移动或修改原始 RAW。
- 输出目录必须与输入目录分离。
- 每个照片/变体只保存一个最终 PSD，不按每个操作生成 PSD/TIFF 节点。
- Lightroom 和 Photoshop 的关键参数必须通过回读或独立证据验证。
- 生成式操作保存输入/输出哈希、提示词、模型/宿主和不变约束。
- 缺少适配器、操作 Descriptor、蒙版、导出或质量证据时失败关闭并结构化降级。
- 单个候选最多三轮有界迭代，保留已验证的最佳版本。

## 贡献

新增或修改流程时，先更新 `references/` 中的契约和说明，再完成本地静态验证与 Adobe 联调。提交保持聚焦，并避免提交个人照片、Adobe 运行目录、测试数据、缓存和本地数据库。
