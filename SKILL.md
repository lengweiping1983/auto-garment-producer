---
name: auto-garment-producer
description: 自动化成衣生产。从主题图或已批准的面料纹理出发，提取纸样裁片、推断服装部位、进行商业级艺术指导填充，最终渲染出带透明背景的确定性裁片 PNG。适用于将设计主题快速转化为可生产的成衣样品。
---

# 自动化成衣生产

当需要将设计主题可靠地转化为可量产的成衣样品时，使用本 Skill。整个流程遵循服装工业生产逻辑：AI 负责创意提示与美术指导，外部 AI 图像模型或设计师生成面料/图案资产，OpenCV/Pillow 以确定性方式渲染最终裁片。

**核心原则**：不得将叙事性插画直接切割到裁片中；不得让 AI 直接生成最终裁片 PNG。AI 仅提供提示词与美术指导；面料/图案资产必须由外部 AI 图像模型或用户设计流程生成。OpenCV/Pillow 仅使用已批准的资产生成输出。

默认审美方向：**商业畅销款打样**。输出应兼顾可穿性、生产安全性、视觉记忆点，且不过度堆砌。优秀的设计师不会将所有主题元素铺满整件衣服，而是选择一个卖点，辅以安静的支持性纹理，再用克制的饰边收束。

## 输入

支持两种输入模式。

**模式 A：已提供面料纹理**

```json
{
  "pattern_image": "/path/to/pattern.png",
  "textures": {
    "main": "/path/to/main_texture.png",
    "secondary": "/path/to/secondary_texture.png",
    "accent": "/path/to/accent_texture.png",
    "dark": "/path/to/dark_texture.png"
  },
  "solids": ["#6f9a4d", "#476f38"]
}
```

**模式 B：主题到商业成衣设计**

```json
{
  "theme_image": "/path/to/theme.png",
  "pattern_image": "/path/to/pattern.png",
  "user_prompt": "柔和水彩森林，纹理中不要出现动物",
  "garment_type": "儿童外套套装"
}
```

在模式 B 中，首先创建 `商业设计简报.json`、`风格档案.json`、`纹理提示词.json` 和 `图案提示词.json`，然后暂停，等待外部 AI 图像模型或用户提供面料/图案图像。只有通过质检与审批后，才能继续渲染。

## 可靠工作流

> ⚠️ **重要提示（给当前阅读此文档的 Agent）**：你正在阅读本 Skill 文档，没有之前任何轮次的上下文。裁片用什么纹理、方向、比例，**不能由后端硬编码规则决定**——必须通过**子 Agent 审美决策**完成。后端只做强制校验与兜底。你必须在流程中显式启动子 Agent。

本 Skill 支持两种工作流：
- **标准工作流（推荐，默认）**：把「部位识别」和「审美决策」合并为一次 AI 调用，减少往返。
- **Legacy 分步工作流**：保留旧的 5 次 AI 调用路径，用于精细调试。

### 标准工作流（3 层架构）

```
准备层（程序-only，可并行）：
  ├─ 裁片提取(pattern) → pieces.json + piece_overview.png
  ├─ 程序几何推断 → geometry_hints.json
  └─ [并行] 视觉元素提取(theme) → visual_elements.json  (AI-1)

资产层：
  ├─ 生成设计简报 → brief.json
  ├─ 看板候选 → AI 优选变体  (AI-2，fast 模式跳过)
  ├─ Neo AI 生成看板 → board.png
  ├─ 裁剪 → texture_set.json
  └─ 质检纹理 → texture_qc_report.json

决策层（核心 AI，合并识别+审美）：
  └─ 生产规划 AI → ai_production_plan.json  (AI-3)
       含：garment_map + piece_fill_plan

执行层（程序-only）：
  ├─ 应用生产规划 → garment_map.json + ai_piece_fill_plan.json
  ├─ 创建填充计划（后端校验）→ piece_fill_plan.json
  ├─ 渲染裁片 → 透明 PNG + preview.png
  └─ 时尚质检 → fashion_qc_report.json

复审层（后置 AI，fast 模式跳过）：
  └─ 商业复审 → ai_commercial_review.json  (AI-4)
```

**AI 调用数对比**：
| 模式 | AI 调用 | 适用场景 |
|------|---------|----------|
| fast | 2 次 | 快速草稿预览，跳过看板选择 AI 和商业复审 |
| standard | 3 次 | 默认推荐，完整流程 |
| production | 3-4 次 | 含资产审批 gate 和强制返工 |
| legacy | 5 次 | 旧分步路径，用于调试 |

### Legacy 工作流（分步 5 次 AI）

如需使用旧路径，传入 `--mode legacy`：

```
0. 视觉元素提取（AI）→ visual_elements.json
1. 生成设计简报 → commercial_design_brief.json
2. 生成面料看板候选 + AI 优选 → collection_board.png
3. 质检纹理 → texture_set.json
4. 提取裁片 → pieces.json + piece_overview.png
5. 程序几何推断 → geometry_hints.json
6. 🔴 AI 部位识别 → ai_garment_map.json → garment_map.json
7. 🔵 AI 审美决策 → ai_piece_fill_plan.json
8. 创建填充计划（后端强制校验）→ piece_fill_plan.json
9. 渲染裁片 → 透明 PNG + 预览图 + 联络单
10. 时尚质检（severity 分级）→ fashion_qc_report.json
11. 🔴 AI 商业复审 → ai_commercial_review.json → commercial_review_result.json
12. 自动重试（≤3 轮）
13. 锁定审批
```

### 步骤详解

**0. 🔴 子 Agent 视觉元素提取（当提供主题图时，不可跳过）**
- **0a. 构造请求**：运行 `scripts/视觉元素提取.py --theme-image`，生成 `ai_vision_prompt.txt`。
- **0b. 启动子 Agent**：使用 `Agent` 工具启动子 Agent，传入主题图路径和 `ai_vision_prompt.txt`，要求子 Agent 阅读图像后输出严格的 `visual_elements.json`。
  - 子 Agent 会同时推断 `fabric_hints.has_nap`（根据风格关键词判断是否有绒毛面料：灯芯绒/丝绒/植绒/毛呢/法兰绒/麂皮/羊羔绒等）。
- **0c. 生成设计简报**：运行 `scripts/生成设计简报.py --visual-elements`，基于子 Agent 分析自动生成 `商业设计简报.json`、`风格档案.json`、`纹理提示词.json`、`图案提示词.json`。
  - 设计简报中的 `fabric.has_nap` 字段由 visual_elements.json 中的 `fabric_hints` 自动推断填充，无需手动设置。
- **离线 fallback**：无网络时运行 `scripts/纯CV视觉元素提取.py --theme`，输出 `visual_elements_cv.json`。

**1. 设计简报**
- `garment_type` 为必填字段（如"儿童外套套装"、"女装连衣裙"）。端到端自动化入口会校验非空。
- `fabric.has_nap` 默认为 false；AI 视觉分析会根据关键词自动推断 true（灯芯绒/丝绒等）。

**2-3. 面料资产生成**
- 9 面板候选提示词（每面板 3 变体）→ 子 Agent 选择最优组合 → 生成最终看板 prompt → Neo AI 生成看板。
- 裁剪看板为面料资产，自适应背景去除（Motif）、MedianCut 取主色（Solid）。

**4. 提取裁片**
- 运行 `scripts/提取裁片.py --pattern` 处理纸样 mask，生成 `裁片清单.json` + `piece_overview.png`。

**5-6. 部位识别（双路径：AI 优先 + 程序兜底）**
- **6a. 构造 AI 识别请求**：运行 `scripts/构造部位识别请求.py --pieces --overview --brief`，生成 `ai_garment_map_prompt.txt`。
- **6b. 启动子 Agent**：传入 prompt + `piece_overview.png`，要求输出 `ai_garment_map.json`。
  - 子 Agent 必须先用 `see_image` 查看图片，再判断部位。
  - 子 Agent 同时输出 `grain_direction`（经向）和 `texture_direction`（纹理方向）。
- **6c. 验证合并**：运行 `scripts/构造部位识别请求.py --selected ai_garment_map.json`，合并为 `garment_map.json`。
  - 未识别比例 >20% 时返回 high severity issue，要求重试。
  - confidence < 0.6 的裁片标记 `needs_ai_review: true`。
- **6d. fallback**：无 AI 结果时，`scripts/部位映射.py` 用几何启发推断，但 `texture_direction` 留空（由后续审美子 Agent 决定）。

**7. 🔵 子 Agent 生产规划（标准模式核心步骤，合并部位识别+审美决策）**
- **7a. 构造请求**：运行 `scripts/构造生产规划请求.py`，生成 `ai_production_plan_prompt.txt`。
  - prompt 要求 AI 分两步思考：Step 1 识别部位 → Step 2 制定填充计划。
  - 输入包含：piece_overview.png（必看）、面料缩略图（必看）、geometry_hints（程序推断，仅供参考）、motif 几何信息。
- **7b. 启动子 Agent**：传入 `ai_production_plan_prompt.txt`，要求输出 `ai_production_plan.json`。
  - 输出必须包含 `garment_map`（部位识别结果）和 `piece_fill_plan`（填充决策）两个顶层 key。
- **7c. 子 Agent 职责**：
  - 部位识别：判断 garment_role、zone、symmetry_group、texture_direction、confidence。
  - 填充决策：决定每个裁片的 base/overlay/trim 参数、scale、rotation、offset。
  - 自评 Rubric：`art_direction.self_assessment` 10 分制评估 10 项商业维度。
- **7d. 应用规划**：运行 `scripts/应用生产规划.py --production-plan ai_production_plan.json`，拆解为 `garment_map.json` + `ai_piece_fill_plan.json`。
  - 下游脚本（创建填充计划.py、渲染裁片.py）**无需修改**。

**7L. Legacy 分步路径（仅 `--mode legacy`）**
- 如需旧路径，分别运行 `scripts/构造部位识别请求.py` 和 `scripts/构造审美请求.py`。
- 先输出 `ai_garment_map.json`，合并为 `garment_map.json` 后，再构造 `ai_fill_plan_prompt.txt`。

**8. 创建填充计划（后端强制校验）**
- 运行 `scripts/创建填充计划.py --ai-plan ai_piece_fill_plan.json`。
- 后端只执行 **★ 安全修正**（文件缺失/透明度/trim motif 禁用/同组一致性/nap 绒毛方向统一/防切割 offset 微调）。
- **⚠ 审美返工**（motif rotation/scale 建议、大身 solid 建议）只记录 issue，**不静默修改**。
- `ai_plan_used=false` 时标记 `"draft_preview_only": true, "production_ready": false`。

**9. 渲染裁片**
- 运行 `scripts/渲染裁片.py`，生成透明 PNG、预览图、白底预览图、联络单。

**10. 时尚质检（severity 分级）**
- 运行 `scripts/时尚质检.py`，输出 `program_qc_status: pass|warn|fail`。
- high severity issues 触发返工请求（`rework_prompt.txt` + `ai_rework_request.json`）。

**11. 🔴 AI 商业复审**
- 运行 `scripts/构造商业复审请求.py --preview --fill-plan --brief --qc-report`，生成 `ai_commercial_review_prompt.txt`。
- 启动子 Agent 查看预览图，输出 `ai_commercial_review.json`。
- 商业复审 issues 会自动合并到返工 prompt 中（优先级高于程序质检）。

**12. 自动重试**
- `--auto-retry N` 启用，最多 N 轮。
- Blocking 条件：`high severity issues > 0` OR `commercial_approved == false`。
- 标准模式：等待外部 Agent 生成 `ai_production_plan_rev{N}.json`，然后重跑全流程。
- Legacy 模式：等待 `ai_piece_fill_plan_rev{N}.json`。
- **可选自调用**：配置 `--retry-agent-cmd "your-agent-cli"`（如 `"kimi chat -p"` / `"claude -p"`），脚本会在 rev 文件不存在时自动 subprocess 调用该命令，传入 rework_prompt.txt，捕获 stdout 作为修订计划。命令可通过环境变量 `$AGENT_OUTPUT_PATH` 获知预期输出路径。调用失败再 fallback 到手动模式。

### 缓存复用（`--reuse-cache`）

启用缓存后，若输入未变化，对应阶段会被跳过：

| 阶段 | 缓存 key | 复用条件 |
|------|----------|----------|
| visual_elements | theme_image sha256 + garment_type | theme 未变 |
| pieces | pattern_image sha256 | pattern 未变 |
| production_plan | pattern sha256 + texture_set sha256 + garment_type + brief sha256 | 核心输入均未变 |
| texture_set | board sha256 + crop_inset | 看板未变 |
| commercial_review | preview sha256 + fill_plan sha256 | 渲染输出未变 |

缓存文件存储在 `output/.cache/` 目录，以 input_hash 命名。

**13. 锁定审批**
- 用户/买手确认后，锁定面料组合和填充计划。

## 子Agent审美决策（必读）

> ⚠️ **再次强调**：你（当前阅读此文档的 Agent）没有之前轮次的任何上下文。本 Skill 的核心架构是**子 Agent 审美决策 + 后端强制校验**。不要试图用后端硬编码规则替代子 Agent 的审美判断。

### 为什么必须启动子Agent

后端脚本（`创建填充计划.py`）即使不传入 `--ai-plan` 也能运行，但会退化为 zone-based 硬编码规则。这违背了本 Skill 的设计原则：**裁片用什么纹理、方向、比例，必须由具备视觉理解能力的子 Agent 决定**。

### 何时启动子Agent

在完成以下步骤后，必须启动子Agent：
1. `提取裁片.py` 已运行 → 生成 `裁片清单.json`
2. `部位映射.py` 已运行 → 生成 `部位映射.json`
3. `质检纹理.py` 已运行 → 生成 `面料组合.json`
4. `构造审美请求.py` 已运行 → 生成 `ai_fill_plan_prompt.txt`

### 如何启动子Agent

使用 `Agent` 工具启动一个 `coder` 类型子 Agent：

**视觉元素提取阶段**：
```
Agent(subagent_type="coder", prompt="你是一位高级服装印花设计分析师。请阅读文件 /path/to/output/ai_vision_prompt.txt 和主题图像 /path/to/theme.png，提取所有视觉元素。输出严格的 JSON 到 /path/to/output/visual_elements.json。不要任何解释文字，只返回 JSON。")
```

**生产规划阶段（标准模式，合并识别+审美）**：
```
Agent(subagent_type="coder", prompt="你是一位资深服装印花艺术指导兼专业打版师。请阅读文件 /path/to/output/ai_production_plan_prompt.txt 和纸样总览图 /path/to/output/piece_overview.png，以及所有面料缩略图。先识别每个裁片的部位，再制定填充计划。输出严格的 JSON 到 /path/to/output/ai_production_plan.json。不要任何解释文字，只返回 JSON。")
```

**裁片填充决策阶段（legacy 模式）**：
```
Agent(subagent_type="coder", prompt="你是一位高级服装印花艺术指导。请阅读文件 /path/to/output/ai_fill_plan_prompt.txt，为每个裁片制定填充计划。输出严格的 JSON 到 /path/to/output/ai_piece_fill_plan.json。不要任何解释文字，只返回 JSON。")
```

### 子Agent的输入

`ai_fill_plan_prompt.txt` 包含：
- 可用面料资产列表（texture_id、role、描述）
- 设计简报（审美方向、季节、目标客群、色板）
- 裁片完整列表（piece_id、role、zone、尺寸、对称组、同形组、texture_direction）
- **硬性约束**（不可违反）：同组同 base、仅1个 hero、trim=quiet solid、每个裁片必须提供 reason。纹理方向由子 Agent 根据面料方向性和裁片形状自主决定，程序仅做同组一致性检查。

### 子Agent的输出格式

`ai_piece_fill_plan.json`：

```json
{
  "pieces": [
    {
      "piece_id": "piece_001",
      "base": {
        "fill_type": "texture",
        "texture_id": "main",
        "scale": 1.12,
        "rotation": 0,
        "offset_x": 0,
        "offset_y": 0,
        "mirror_x": false,
        "mirror_y": false
      },
      "overlay": {
        "fill_type": "motif",
        "motif_id": "hero_motif_1",
        "anchor": "center",
        "scale": 0.72,
        "opacity": 0.92,
        "offset_x": 0,
        "offset_y": -40
      },
      "trim": null,
      "texture_direction": "transverse",
      "reason": "前片卖点区使用主底纹横向铺陈，中心定位牡丹图案"
    }
  ],
  "art_direction": {
    "strategy": "单一卖点定位，低噪身片，协调副片，安静饰边",
    "hero_piece_ids": ["piece_001"],
    "notes": []
  }
}
```

### 后端强制校验（不可绕过）

即使子 Agent 已输出计划，`创建填充计划.py` 仍会执行以下校验修正：

| 校验项 | 修正规则 |
|--------|----------|
| 同组一致性 | symmetry_group / same_shape_group 内所有裁片的 base 层必须完全相同，不一致时以组内第一个为准强制复制 |
| Hero 数量 | 不在 [1,2] 范围内时，强制指定最大 body 裁片为 hero，其余取消 overlay |
| Trim 安全 | trim zone 裁片若被分配 motif 或 accent texture，强制降级为 quiet solid 或 dark texture |
| 大身纯色 | body zone 且面积 > 最大面积 15% 的裁片若使用 solid，强制替换为 main texture |
| 方向一致性 | 同 symmetry_group / same_shape_group 内所有裁片的 texture_direction 必须一致，不一致时发出警告 |
| 对花对条 | 使用相同 texture + 相同 direction 的裁片共享全局纹理坐标系：以最大独立裁片为锚点，其他裁片的 offset 按 pattern image 中的相对位置对齐，确保相邻裁片缝合处图案连续。对称组/同形组随后重新同步，保持组内一致 |
| **Motif 方向对齐** | 根据 motif 几何方向（vertical/horizontal）与裁片 texture_direction 自动修正 rotation；方向不匹配时旋转 90° 使其有机融合 |
| **Motif 尺寸适配** | 根据 motif 原始像素尺寸与裁片尺寸的比例，动态计算 scale（替代固定 0.72）；如果 motif 在裁片内可见度 < 70%，自动增大 scale 或发出警告 |
| **Motif 防切割** | `渲染裁片.py` 在放置 motif 前用裁片 mask 模拟计算可见比例；如果 < 85%，自动在 ±20% 范围内微调 offset 寻找最佳位置，确保 motif 不被裁片边界切断 |

### 如果子Agent失败

1. 检查 `ai_fill_plan_prompt.txt` 是否生成正确
2. 重新启动子 Agent，明确指出之前的错误
3. 如果子 Agent 持续失败，回退到后端规则：`创建填充计划.py` 不传 `--ai-plan`——**但此路径仅可作为草稿预览，不允许作为生产审批稿**。必须在重新启动子 Agent 并获得 `ai_plan_used=true` 的计划后才能进入渲染。

> 后端规则生成的计划会在 `art_direction` 中标记 `"draft_preview_only": true, "production_ready": false`，供外层流程识别。

## 商业服装规则

- 只选一个 hero（卖点）概念，放在 1–2 个关键裁片上。
- 大身裁片应低对比度，在零售距离下依然可穿。
- 小裁片和窄条应控制节奏、饰边和色彩呼应；不应承载复杂叙事艺术。
- 严禁将叙事插画直接切割到纸样裁片中。
- 避免均匀密度的全身填充；服装需要层次、呼吸感和明确的卖点。
- 优化审美、生成填充计划或评判输出质量时，阅读 `references/服装艺术指导.md`。

## 命令

### Neo AI 一键自动化（基础路径，无子Agent）

```bash
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --token "$NEODOMAIN_ACCESS_TOKEN" \
  --commercial-review --auto-retry 3
```

这条命令执行基础自动化路径（后端规则填充计划）。

### 带子Agent审美决策的完整路径（推荐，标准模式）

```bash
# 步骤 0：视觉元素提取（二选一）
# 方案 A：LLM 子Agent分析（推荐）
python3 /path/to/auto-garment-producer/scripts/视觉元素提取.py \
  --theme-image /path/to/theme.png \
  --out /path/to/output
# → 启动子 Agent 分析图像，输出 visual_elements.json

# 方案 B：纯 CV 离线提取（无网络时使用）
python3 /path/to/auto-garment-producer/scripts/纯CV视觉元素提取.py \
  --theme /path/to/theme.png \
  --out /path/to/output \
  --grid 6
# → 输出 visual_elements_cv.json

# 步骤 1-6：生成看板、裁剪纹理、提取裁片、生成 geometry_hints
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --visual-elements /path/to/output/visual_elements.json \
  --collection-board /path/to/collection_board_1.png \
  --mode standard \
  --commercial-review --auto-retry 3

# 步骤 7a：构造生产规划 AI 请求（合并部位识别 + 审美决策）
python3 /path/to/auto-garment-producer/scripts/构造生产规划请求.py \
  --pieces /path/to/output/pieces.json \
  --texture-set /path/to/output/texture_set.json \
  --brief /path/to/output/commercial_design_brief.json \
  --geometry-hints /path/to/output/geometry_hints.json \
  --visual-elements /path/to/output/visual_elements.json \
  --out /path/to/output

# 步骤 7b：启动子Agent（使用 Agent 工具）
# 子Agent阅读 ai_production_plan_prompt.txt + piece_overview.png + 面料缩略图
# 输出 ai_production_plan.json（含 garment_map + piece_fill_plan）

# 步骤 7c：应用生产规划
python3 /path/to/auto-garment-producer/scripts/应用生产规划.py \
  --production-plan /path/to/output/ai_production_plan.json \
  --out /path/to/output

# 步骤 8-10：后端校验、渲染、质检
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --collection-board /path/to/collection_board_1.png \
  --production-plan /path/to/output/ai_production_plan.json \
  --mode standard \
  --commercial-review --auto-retry 3
```

### Fast 模式（快速草稿预览，2 次 AI）

```bash
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --theme-image /path/to/theme.png \
  --out /path/to/output \
  --garment-type "儿童外套套装" \
  --mode fast \
  --reuse-cache
# fast 模式自动跳过看板选择 AI 和商业复审
# 输出标记 draft_preview_only=true
```

### 如果已有看板（standard 模式）：

```bash
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --collection-board /path/to/collection_board_1.png \
  --out /path/to/output \
  --mode standard \
  --commercial-review --auto-retry 3
```

### 分步命令

提取裁片：

```bash
python3 /path/to/auto-garment-producer/scripts/提取裁片.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output
```

纹理质检：

```bash
python3 /path/to/auto-garment-producer/scripts/质检纹理.py \
  --texture-set /path/to/面料组合.json \
  --out /path/to/output/纹理质检报告.json
```

渲染裁片：

```bash
python3 /path/to/auto-garment-producer/scripts/渲染裁片.py \
  --pieces /path/to/output/裁片清单.json \
  --texture-set /path/to/面料组合.json \
  --fill-plan /path/to/output/裁片填充计划.json \
  --out /path/to/output/渲染结果
```

如果省略 `--fill-plan`，渲染器会自动创建确定性计划：

- 最大裁片使用 `main`
- 大副裁片使用 `secondary`
- 长窄条使用 `accent` 或 `dark`
- 小裁片使用 `accent`
- 细条使用纯色

艺术指导计划：

```bash
python3 /path/to/auto-garment-producer/scripts/创建填充计划.py \
  --pieces /path/to/output/裁片清单.json \
  --texture-set /path/to/面料组合.json \
  --garment-map /path/to/output/部位映射.json \
  --brief /path/to/output/商业设计简报.json \
  --out /path/to/output
```

## 硬性规则

- OpenCV/Pillow **不得**直接从原始主题插画填充裁片。
- OpenCV/Pillow **只能**使用已批准的面料或纯色。
- OpenCV/Pillow 可以放置已批准的图案，但不得使用未批准的图案或叙事源裁剪。
- Codex **不得**用程序化 Pillow/OpenCV 绘图创建最终面料/图案 artwork 并冒充设计输出。
- AI 生成的面料/图案资产必须来自图像生成模型或显式的用户设计流程。
- 面料必须保存为资产后才能渲染。
- 每种面料必须包含元数据：提示词/来源、生成所用模型、种子（如有）、质检状态、审批状态。
- 每个裁片必须有固定的填充计划。
- 每次导出必须包含清单，将裁片关联到面料编号和参数。
- 用户审批通过的版本应被锁定；禁止覆盖已锁定版本。

## 输出

```text
风格档案.json
商业设计简报.json
纹理提示词.json
图案提示词.json
ai_vision_prompt.txt             ← 子Agent视觉分析请求提示词
ai_vision_request.json           ← 子Agent视觉分析请求结构化摘要
visual_elements.json             ← 子Agent输出的视觉元素分析（由子Agent生成）
visual_elements_cv.json          ← 纯CV提取的视觉元素分析（零LLM，由纯CV脚本生成）
generated_collection_prompt.txt  ← 自动生成的3×3面料看板综合提示词
面料组合.json
纹理质检报告.json
裁片清单.json
部位映射.json
ai_fill_plan_prompt.txt          ← 子Agent审美请求提示词
ai_fill_plan_request.json        ← 子Agent请求结构化摘要
ai_piece_fill_plan.json          ← 子Agent输出的填充计划（由子Agent生成）
艺术指导方案.json
裁片填充计划.json
渲染结果/
  裁片/*.png
  预览图.png
  白底预览图.jpg
  填充清单.json
成品质检报告.json
```

## 参考阅读

- `references/服装艺术指导.md`：商业成衣美术指导原则。
- `references/数据契约.md`：JSON 数据结构定义。
- `references/纹理提示词策略.md`：AI 提示词策略。
- `references/质检规则.md`：可靠性检查规范。
- `references/渲染器规范.md`：渲染器行为说明。
- `references/服装术语词典.md`：裁片类型与部位标准术语。
