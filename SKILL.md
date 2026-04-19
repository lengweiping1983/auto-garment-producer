---
name: auto-garment-producer
description: 自动化成衣生产。从主题图或已批准的面料纹理出发，提取纸样裁片、推断服装部位、进行商业级艺术指导填充，最终渲染出带透明背景的确定性裁片 PNG。适用于将设计主题快速转化为可生产的成衣样品。。覆盖场景包括：生成（`T恤`、`T 恤`、`t-shirt`、`tee`、`衬衫`、`男士衬衫`），都必须触发此技能。
---

# 自动化成衣生产

当需要将设计主题可靠地转化为可量产的成衣样品时，使用本 Skill。整个流程遵循服装工业生产逻辑：AI 负责创意提示与美术指导，外部 AI 图像模型或设计师生成面料/图案资产，OpenCV/Pillow 以确定性方式渲染最终裁片。

**核心原则**：不得将叙事性插画直接切割到裁片中；不得让 AI 直接生成最终裁片 PNG。AI 仅提供提示词与美术指导；面料/图案资产必须由外部 AI 图像模型或用户设计流程生成。OpenCV/Pillow 仅使用已批准的资产生成输出。

默认审美方向：**商业畅销款打样**。输出应兼顾可穿性、生产安全性、视觉记忆点，且不过度堆砌。优秀的设计师不会将所有主题元素铺满整件衣服，而是选择一个卖点，辅以安静的支持性纹理，再用克制的饰边收束。

**内置模板强规则**：当用户要求创建 `T恤`、`T 恤`、`t-shirt`、`tee`、`衬衫`、`男士衬衫` 或同义上装，且没有额外提供纸样 mask 时，必须优先复用 `DDS26126XCJ01L` 内置模板（默认 S 码）。不要说“没有现成 T 恤纸样”；该模板就是 T 恤/衬衫共用的 9 片上装模板。只有用户明确禁用模板或指定其它纸样时，才走其它模板/手动 mask 路径。

## 输入

支持两种输入模式。

**模式 A：已提供面料纹理**

```json
{
  "pattern": "/path/to/pattern.png",
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
  "pattern": "/path/to/pattern.png",
  "user_prompt": "柔和水彩森林，纹理中不要出现动物",
  "garment_type": "儿童外套套装"
}
```

在模式 B 中，首先创建 `商业设计简报.json`、`风格档案.json`、`纹理提示词.json` 和 `图案提示词.json`，然后暂停，等待外部 AI 图像模型或用户提供面料/图案图像。只有通过质检与审批后，才能继续渲染。

**主题图落盘规则（强制）**：
- 端到端脚本只能消费本地图片文件；聊天窗口里的 `<image>` 不等于脚本可读取的路径。
- 当用户提供会话图片但没有显式路径时，必须先通过 `scripts/theme_image_resolver.py` 的能力解析/落盘，或把图片放到输出目录的 `input/`、`inputs/`、`theme_inputs/` 中，文件名以 `theme`、`input` 或 `reference` 开头。
- `scripts/端到端自动化.py --theme-image` 现在支持文件路径、目录、URL、`data:image/...;base64,...`、纯 base64，以及 `AUTO_GARMENT_THEME_IMAGE` / `CODEX_ATTACHED_IMAGE_PATHS` 等环境变量。解析成功后会复制到 `out/theme_inputs/theme_image_<hash>.*`，后续视觉提取、缓存和 3×3 看板生成都使用该稳定路径。
- 支持多主题图参考：可重复传入 `--theme-image /path/a.png --theme-image /path/b.png`，也可用 `--theme-images "/path/a.png,/path/b.png"`。多图会作为同一主题参考集合处理，生成一个融合后的 `visual_elements.json`，不会默认生成多套方案。第一张图默认是主参考，其余图补充辅助元素、背景色彩、纹理语言和风格；如用户提示指定“第一张主体、第二张风格/配色”，按用户提示优先。
- 多图解析会生成 `theme_images_manifest.json`，记录顺序、来源、稳定路径和 hash。子 Agent 视觉分析必须逐张观察并在 `dominant_objects` / `supporting_elements` 中用 `source_image_refs` 标注来源图，同时输出 `fusion_strategy`。
- 不得因为“会话图片没有本地路径”而跳过主题图路径、手写 `texture_set.json` 或直接进入裁片渲染；如果确实无法解析主题图，应停下来要求用户提供路径或把图片放入 `out/input/`。

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
  └─ [并行] 视觉元素提取(theme 或 theme_images) → visual_elements.json  (AI-1)

资产层：
  ├─ 生成设计简报 → brief.json
  ├─ 看板候选 → AI 优选变体  (AI-2，fast 模式跳过)
  ├─ Neo AI + libtv-skill 双源生成连续面料看板 → board_A/board_B.png
  ├─ 裁剪 → texture_set_A/B.json
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
- **0a. 构造请求**：运行 `scripts/视觉元素提取.py --theme-image`，可重复传入多张，生成 `ai_vision_prompt.txt`。
- **0b. 启动子 Agent**：使用 `Agent` 工具启动子 Agent，传入主题图路径列表和 `ai_vision_prompt.txt`，要求子 Agent 阅读所有图像后输出严格的 `visual_elements.json`。
  - 多图时输出必须是融合后的单一主题方向，不是多套方案。
  - `visual_elements.json` 保持旧字段兼容，并可新增 `source_images`、`image_analyses`、`fusion_strategy`、`source_image_refs`。
  - 子 Agent 会同时推断 `fabric_hints.has_nap`（根据风格关键词判断是否有绒毛面料：灯芯绒/丝绒/植绒/毛呢/法兰绒/麂皮/羊羔绒等）。
- **0c. 生成设计简报**：运行 `scripts/生成设计简报.py --visual-elements`，基于子 Agent 分析自动生成 `商业设计简报.json`、`风格档案.json`、`纹理提示词.json`、`图案提示词.json`。
  - 设计简报中的 `fabric.has_nap` 字段由 visual_elements.json 中的 `fabric_hints` 自动推断填充，无需手动设置。
- **离线 fallback**：无网络时运行 `scripts/纯CV视觉元素提取.py --theme`，输出 `visual_elements_cv.json`。

**1. 设计简报**
- `garment_type` 为必填字段（如"儿童外套套装"、"女装连衣裙"）。端到端自动化入口会校验非空。
- `fabric.has_nap` 默认为 false；AI 视觉分析会根据关键词自动推断 true（灯芯绒/丝绒等）。

**2-3. 面料资产生成**
- 主题图路径下，先由子 Agent 提取主体、辅助元素、背景色彩与风格，再生成可铺满面料的连续纹理提示词。
- 未提供已审批 `--texture-set` 或已有 `--collection-board` 时，必须调用 Neo AI 与 libtv-skill 双源生成面料看板；二者与本 skill 默认按同级目录解析（`auto-garment-producer`、`neo-ai`、`libtv-skill`）。
- 双源都必须被调用并等待结果；双源都成功则分别渲染 A/B，一源成功且另一源明确失败/超时后可用成功源继续，只有双源都失败才重试并重新发起 AI 生成。
- 双源只能输出面料看板、纹理集、透明裁片 PNG、裁片预览/contact sheet/QC 报告；不得输出或声称输出“正面效果图”、服装 mockup、模特上身图、假人图、产品照。
- 9 面板候选提示词（每面板 3 变体）→ 子 Agent 选择最优组合 → 生成最终看板 prompt → 双源生成看板。
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
  - 输入包含：piece_overview.png（必看）、款式参考图（匹配模板时必看）、面料缩略图（必看）、geometry_hints（程序推断，仅供参考）、motif 几何信息。
  - 内置款式参考图位于 `references/styles/`。当前覆盖 `BFSK26308XCJ01L` 男士防晒服和 `DDS26126XCJ01L` 上装/T恤/衬衫通用模板；构造请求时会根据模板资产路径、模板匹配结果或 `garment_type` 自动注入对应参考图，让 AI 先做标准纸样对照，再判断每个 mask 的部位、上下方向和对称关系。
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
| pieces | pattern_asset sha256 | pattern 未变 |
| production_plan | pieces_asset sha256 + texture_set sha256 + garment_type + brief sha256 | 核心输入均未变 |
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
Agent(subagent_type="coder", prompt="你是一位资深服装印花艺术指导兼专业打版师。请阅读文件 /path/to/output/ai_production_plan_prompt.txt，先查看纸样总览图 /path/to/output/piece_overview.png、提示词中列出的款式参考图（如有）以及所有面料缩略图。先对照款式参考图识别每个裁片的部位、上下方向和对称关系，再制定填充计划。输出严格的 JSON 到 /path/to/output/ai_production_plan.json。不要任何解释文字，只返回 JSON。")
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
# 子Agent阅读 ai_production_plan_prompt.txt + piece_overview.png + 款式参考图（如有）+ 面料缩略图
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

### 双源并行模式（Neo AI + libtv-skill，推荐用于生产）

同时调用 Neo AI 和 libtv-skill 并行生成两套风格一致但表现不同的面料看板。`auto-garment-producer`、`neo-ai`、`libtv-skill` 默认按同级 skill 目录解析。

```bash
# 前置：确保 LIBTV_ACCESS_KEY 已设置
export LIBTV_ACCESS_KEY="sk-libtv-6179c6adce8e4c3f9b28ef74839b5246"

# 步骤 0-1：视觉元素提取 + 生成设计简报（与标准模式相同）
python3 /path/to/auto-garment-producer/scripts/视觉元素提取.py \
  --theme-image /path/to/theme.png \
  --out /path/to/output
# → 启动子 Agent 分析图像

python3 /path/to/auto-garment-producer/scripts/生成设计简报.py \
  --visual-elements /path/to/output/visual_elements.json \
  --out /path/to/output \
  --garment-type "儿童外套套装"
# → 自动生成 dual_collection_prompts.json（两套差异化提示词）

# 步骤 2：双源并行看板生成 + 部位映射（并行执行）
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --visual-elements /path/to/output/visual_elements.json \
  --mode standard \
  --dual-source \
  --token "$NEODOMAIN_ACCESS_TOKEN" \
  --libtv-key "$LIBTV_ACCESS_KEY" \
  --commercial-review --auto-retry 3
# → 同时启动 Neo AI 和 libtv 生成看板
# → 看板生成与部位映射并行执行，互不阻塞
# → 等待两个源完成；双源均成功则输出 A/B，两源均失败才自动重试（默认 2 次）
# → 一源成功且另一源明确失败/超时后，可用成功源继续
# → 若双源均成功，输出 rendered_A/ 和 rendered_B/ 两套结果
```

**双源模式关键行为**：
- **并行生成**：Neo AI（Set A / 英文结构化 prompt）和 libtv（Set B / 中文自然语言描述）同时提交。
- **并行不阻塞**：看板生成与部位映射在独立线程中并行执行。
- **失败容错**：两个源都必须被调用并等待结果；一源成功且另一源明确失败/超时后继续；两个都失败才重试。
- **两套结果**：若双源均成功，分别裁剪为 `texture_set_A.json` 和 `texture_set_B.json`，并分别渲染为 `rendered_A/` 和 `rendered_B/`。
- **禁止正面效果图**：双源只生成面料看板和纹理资产，不生成正面成衣 mockup、模特上身图、假人图或产品照。`preview.png`/contact sheet 是裁片质检预览，不是正面效果图。

**环境变量**：
- `NEODOMAIN_ACCESS_TOKEN`：Neo AI 鉴权令牌
- `LIBTV_ACCESS_KEY`：libtv 鉴权令牌

### 多方案渲染模式（Multi-Scheme Rendering，推荐用于探索设计空间）

让 AI 从完整资产池中组合出多套不同的专业设计方案，每套独立渲染产出。支持**双源 18 资产全池探索**和**单源 9 资产全池探索**两种场景。默认生成 12 套探索组合，也可用 `--max-schemes 18` 扩大探索范围。

```bash
# 前置步骤与双源模式相同（视觉提取 → 设计简报）
# 步骤：双源并行看板生成 + 多方案渲染
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --visual-elements /path/to/output/visual_elements.json \
  --mode standard \
  --dual-source \
  --multi-scheme \
  --max-schemes 12 \
  --token "$NEODOMAIN_ACCESS_TOKEN" \
  --libtv-key "$LIBTV_ACCESS_KEY" \
  --commercial-review
# → 第一次运行：生成看板、准备资产、构造多方案生产规划请求后退出，等待子 Agent
# → 子 Agent 输出 ai_multi_production_plan.json（含 schemes 数组）
# → 第二次运行（相同命令）：拆解 schemes，逐套独立渲染
# → 输出 rendered_scheme_01/ ~ rendered_scheme_12/ 十二套结果
```

**多方案模式关键行为**：
- **灵活资产池**：
  - **双源均成功**：A+B 合并为 `merged_texture_set.json`（18 个资产，ID 带 `_a` / `_b` 后缀），AI 从 18 个中自由组合。
  - **仅单源成功**：该源的 9 个资产也会自动加后缀（如 `_a`）并包装为 `merged_texture_set.json`，AI 从这 9 个面板中通过不同的分配策略（谁做 base、谁做 overlay、scale/rotation/anchor 变化）组合出多套方案。**不浪费任何成功生成的资产。**
- **AI 全池探索**：构造生产规划请求时，AI 必须把所有可用资产视为一个完整素材库来判断，不得先选定少数候选资产后只在小范围内交换位置。
  - 双源时：A+B 合并为完整 18 资产池；全 A、全 B、A/B 混合都允许，但只能作为 AI 从完整资产池审美判断后自然得出的结果，不能作为预设模板。
  - 单源时：从完整 9 资产池生成多套差异化组合，不固定“经典主调/深色反转”等模板。
  - 每套 scheme 需要说明 `design_positioning`、`asset_mix_summary`、`diversity_tags`；顶层需要说明 `portfolio_notes` 和 `asset_coverage`。
- **独立渲染**：每套 scheme 拥有独立的 `garment_map` 和 `piece_fill_plan`，独立执行渲染 → 时尚质检 → 商业复审流水线。
- **失败跳过**：单套 scheme 渲染失败不影响其他方案，调用方自动跳过并继续下一套。
- **互为备份**：多套方案中任意一套成功即可交付，天然具有容错能力。

**子 Agent 输出格式**（`ai_multi_production_plan.json`）：
```json
{
  "schemes": [
    {
      "scheme_id": "scheme_01",
      "design_positioning": "量产安全款",
      "strategy_note": "从完整资产池独立判断后的组合策略",
      "asset_mix_summary": {
        "body_base_assets": ["main_a"],
        "secondary_assets": ["accent_mid_b"],
        "hero_assets": ["hero_motif_1_a"],
        "trim_assets": ["quiet_solid_b"],
        "reason": "低噪大身结合更精致的 B 源饰边色，保持可穿性并增加层次"
      },
      "diversity_tags": ["quiet_body", "controlled_hero", "accent_trim"],
      "garment_map": { "map_id": "...", "parts": [...] },
      "piece_fill_plan": { "plan_id": "...", "pieces": [...] }
    },
    {
      "scheme_id": "scheme_02",
      "design_positioning": "精品陈列款",
      "strategy_note": "从完整资产池独立判断后的另一种专业组合",
      "asset_mix_summary": { "...": "..." },
      "diversity_tags": ["dark_ground", "bold_hero"],
      "garment_map": { ... },
      "piece_fill_plan": { ... }
    }
  ],
  "portfolio_notes": "整组方案覆盖量产安全、强视觉卖点、深色高级、轻量呼吸感、局部点缀等不同商业方向。",
  "asset_coverage": {
    "used_assets": ["main_a", "accent_mid_b", "hero_motif_1_a"],
    "unused_assets": [
      { "asset_id": "trim_motif_a", "reason": "trim 禁用 motif，且不适合大面积上身" }
    ],
    "coverage_strategy": "质量优先，不机械使用全部资产；但每套方案都从完整资产池出发判断。"
  }
}
```

**与双源模式的区别**：
| 维度 | 双源模式 | 多方案模式 |
|------|---------|-----------|
| 资产池 | A 套 9 个 或 B 套 9 个（分别渲染） | 双源=18 个合并；单源=9 个内组合 |
| 设计决策 | 每套独立做生产规划 | 一次生产规划输出多套方案 |
| 输出数量 | 2 套（A/B 各一） | 默认 12 套（可配，建议 12-18），失败跳过 |
| 单源容错 | 仅一个源成功 → 只有 1 套 | 仅一个源成功 → 仍可产出 12 套探索方案 |
| 使用场景 | 对比两种平台风格 | 最大化设计探索，不浪费任何资产 |

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

### 生成整套（首次出全尺寸）

```bash
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/BFSK26308XCJ01L-S_mask.png \
  --collection-board /path/to/collection_board_1.png \
  --out /path/to/output \
  --mode standard \
  --full-set
```

### 已有 -S，只补其他尺寸（零 AI）

```bash
python3 /path/to/auto-garment-producer/scripts/生成整套尺寸.py \
  --base-dir /path/to/output \
  --pattern /path/to/BFSK26308XCJ01L-S_mask.png
```

### 多尺寸模板（一次性初始化）

同版型多尺寸（如 S/M/L/XL/XXL）只需初始化一次，后续自动复用：

```bash
python3 /path/to/auto-garment-producer/scripts/初始化多尺寸模板.py \
  --template-id BFSK26308XCJ01L \
  --base-mask /path/to/*-S_mask.png \
  --size-masks /path/to/*-M_mask.png /path/to/*-L_mask.png /path/to/*-XL_mask.png /path/to/*-XXL_mask.png \
  --size-labels m l xl xxl \
  --garment-type "children outerwear set"
```

初始化后：
1. **人工检查角色**：打开 `templates/BFSK26308XCJ01L/base.json`，确认/修正各 slot 的 `garment_role`。
2. **自动发现**：后续调用 `--pattern` 指向任意尺寸 mask 时，程序自动匹配该模板（按文件名货号匹配）。

**生成控制（默认只出-S，整套才出全尺寸）**：

| 场景 | 命令 | 说明 |
|------|------|------|
| 只生成 -S | 默认（不加 `--full-set`） | 仅输出 rendered/（-S 走 AI） |
| 首次生成整套 | 加 `--full-set` | -S 走 AI，M/L/XL/XXL 纯程序映射 |
| 已有 -S，补整套 | `生成整套尺寸.py` | 零 AI，直接基于已有 -S 结果生成其他尺寸 |

**关键约束**：
- 基准尺寸 `-S` 是唯一走 AI 流程的版本。
- M/L/XL/XXL 纯程序映射：使用 `-S` 的填充方案，仅 mask 和纹理 scale 按面积比调整。
- 所有尺寸裁片数量必须一致（初始化脚本会校验）。

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
dual_collection_prompts.json     ← 双源模式：两套差异化看板提示词（Set A / Set B）
style_a_collection_prompt.txt    ← 双源模式：Neo AI 的英文结构化看板 prompt
ai_vision_prompt.txt             ← 子Agent视觉分析请求提示词
ai_vision_request.json           ← 子Agent视觉分析请求结构化摘要
visual_elements.json             ← 子Agent输出的视觉元素分析（由子Agent生成）
visual_elements_cv.json          ← 纯CV提取的视觉元素分析（零LLM，由纯CV脚本生成）
generated_collection_prompt.txt  ← 自动生成的3×3面料看板综合提示词
selected_collection_prompt.txt   ← 子Agent选择后的最终看板 prompt
面料组合.json
texture_set_A.json               ← 双源模式：Set A（Neo AI）裁剪后的面料组合
texture_set_B.json               ← 双源模式：Set B（libtv）裁剪后的面料组合
merged_texture_set.json          ← 多方案模式：A+B 合并后的面料组合（资产ID带 _a/_b 后缀）
纹理质检报告.json
texture_qc_report_A.json         ← 双源模式：Set A 质检报告
texture_qc_report_B.json         ← 双源模式：Set B 质检报告
texture_qc_report_merged.json    ← 多方案模式：合并资产质检报告
裁片清单.json
部位映射.json
garment_map_A.json               ← 双源模式：Set A 部位映射备份
garment_map_B.json               ← 双源模式：Set B 部位映射备份
garment_map_scheme_01.json       ← 多方案模式：方案 01 部位映射
garment_map_scheme_02.json       ← 多方案模式：方案 02 部位映射
ai_fill_plan_prompt.txt          ← 子Agent审美请求提示词
ai_fill_plan_request.json        ← 子Agent请求结构化摘要
ai_piece_fill_plan.json          ← 子Agent输出的填充计划（由子Agent生成）
ai_multi_production_plan.json    ← 多方案模式：子Agent输出的多方案生产规划（含 schemes 数组）
schemes_meta.json                ← 多方案模式：方案元数据索引（scheme_id → garment_map / fill_plan 路径）
piece_fill_plan_A.json           ← 双源模式：Set A 填充计划备份
piece_fill_plan_B.json           ← 双源模式：Set B 填充计划备份
piece_fill_plan_scheme_01.json   ← 多方案模式：方案 01 填充计划备份
piece_fill_plan_scheme_02.json   ← 多方案模式：方案 02 填充计划备份
艺术指导方案.json
裁片填充计划.json
ai_production_plan_prompt.txt    ← 生产规划请求提示词（单方案）
ai_production_plan_request.json  ← 生产规划请求结构化摘要
渲染结果/                ← -S 基准尺寸输出（单源模式）
  裁片/*.png
  preview_s.png
  preview_white_s.jpg
  piece_contact_sheet_s.jpg
  texture_fill_manifest_s.json
渲染结果_A/              ← 双源模式：Set A 渲染输出
  裁片/*.png
  preview_s.png
  ...
渲染结果_B/              ← 双源模式：Set B 渲染输出
  裁片/*.png
  preview_s.png
  ...
渲染结果_scheme_01/      ← 多方案模式：方案 01 渲染输出
  裁片/*.png
  preview_s.png
  ...
渲染结果_scheme_02/      ← 多方案模式：方案 02 渲染输出
  ...
渲染结果_m/              ← M 尺寸（纯程序映射）
  裁片/*.png
  preview_m.png
  ...
渲染结果_l/              ← L 尺寸
  ...
渲染结果_xl/             ← XL 尺寸
  ...
渲染结果_xxl/            ← XXL 尺寸
  ...
成品质检报告.json
fashion_qc_report_A.json         ← 双源模式：Set A 时尚质检报告
fashion_qc_report_B.json         ← 双源模式：Set B 时尚质检报告
fashion_qc_report_scheme_01.json ← 多方案模式：方案 01 时尚质检报告
fashion_qc_report_scheme_02.json ← 多方案模式：方案 02 时尚质检报告
```

## 参考阅读

- `references/服装艺术指导.md`：商业成衣美术指导原则。
- `references/数据契约.md`：JSON 数据结构定义。
- `references/纹理提示词策略.md`：AI 提示词策略。
- `references/质检规则.md`：可靠性检查规范。
- `references/渲染器规范.md`：渲染器行为说明。
- `references/服装术语词典.md`：裁片类型与部位标准术语。
