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

0. **🔴 子 Agent 视觉元素提取（当提供主题图时，不可跳过）**
   - **0a. 构造请求**：运行 `scripts/视觉元素提取.py --theme-image`，生成 `ai_vision_prompt.txt`。
   - **0b. 启动子 Agent**：使用 `Agent` 工具启动子 Agent，传入主题图路径和 `ai_vision_prompt.txt`，要求子 Agent 阅读图像后输出严格的 `visual_elements.json`。
   - **0c. 生成设计简报**：运行 `scripts/生成设计简报.py --visual-elements`，基于子 Agent 分析自动生成 `商业设计简报.json`、`风格档案.json`、`纹理提示词.json`、`图案提示词.json`。
   - **0d. 提示词优势**：子 Agent 基于真实视觉分析生成的英文提示词，远比关键词匹配更准确、更具体、更符合图像实际内容。
1. **（若无主题图）分析主题风格与商业意图**。保存 `商业设计简报.json`、`风格档案.json`、`纹理提示词.json`、`图案提示词.json`。
2. **生成精简的资产家族提示词**，但不要在 Codex 内直接生成图像：
   - `main`（主面料）：低噪可穿基础纹理。
   - `secondary`（辅面料）：协调的主体/袖片纹理。
   - `accent`（点缀）：受控的小型点缀纹理。
   - `dark`/`light`/`solids`（深色/浅色/纯色）：饰边、袖口、窄条、打底片。
   - 可选 `motifs`（图案）：简化后的卖点定位图案，而非完整叙事裁剪。
3. **将提示词发送至 AI 图像生成器或设计师**。将生成的图像保存为文件，并记录到 `面料组合.json`。
4. **运行 `scripts/质检纹理.py`** 生成 `纹理质检报告.json`。
5. **运行 `scripts/提取裁片.py`** 处理纸样图。
6. **运行 `scripts/部位映射.py`** 推断服装角色、对称性与置信度。
7. **🔵 子 Agent 审美决策（核心步骤，不可跳过）**
   - **7a. 构造请求**：运行 `scripts/构造审美请求.py`，生成 `ai_fill_plan_prompt.txt` 和 `ai_fill_plan_request.json`。
   - **7b. 启动子 Agent**：使用 `Agent` 工具启动一个 `coder` 类型子 Agent，将 `ai_fill_plan_prompt.txt` 作为输入，要求子 Agent 输出严格的 `ai_piece_fill_plan.json`。
   - **7c. 子 Agent 职责**：为每个裁片决定 base/overlay/trim 层参数（texture_id、scale、rotation、offset、mirror），确保对称组/同形组完全一致，仅 1 个 hero，body=transverse，secondary=longitudinal，trim=quiet solid。
   - **7d. 失败回退**：如果子 Agent 调用失败或返回不可解析，回退到 `scripts/创建填充计划.py` 的后端规则（zone-based，无 index%2）。
8. **运行 `scripts/创建填充计划.py --ai-plan ai_piece_fill_plan.json`** 读取子 Agent 决策，执行 5 条后端强制校验修正，输出 `艺术指导方案.json` 和 `裁片填充计划.json`。
9. **运行 `scripts/渲染裁片.py`** 根据艺术指导填充计划生成透明裁片 PNG、预览图、联络单和清单。
10. **运行 `scripts/时尚质检.py`** 生成 `成品质检报告.json`。
11. **锁定审批**：用户确认结果后，锁定已批准的面料组合和填充计划，禁止覆盖。

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

**裁片填充决策阶段**：
```
Agent(subagent_type="coder", prompt="你是一位高级服装印花艺术指导。请阅读文件 /path/to/output/ai_fill_plan_prompt.txt，为每个裁片制定填充计划。输出严格的 JSON 到 /path/to/output/ai_piece_fill_plan.json。不要任何解释文字，只返回 JSON。")
```

### 子Agent的输入

`ai_fill_plan_prompt.txt` 包含：
- 可用面料资产列表（texture_id、role、描述）
- 设计简报（审美方向、季节、目标客群、色板）
- 裁片完整列表（piece_id、role、zone、尺寸、对称组、同形组、texture_direction）
- **硬性约束**（不可违反）：同组同 base、仅1个 hero、body=transverse、secondary=longitudinal、trim=quiet solid、每个裁片必须提供 reason

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
| 方向对齐 | 根据裁片 aspect 和 garment_role 自动修正 rotation，确保纹理方向与裁片长边语义对齐 |
| 对花对条 | 使用相同 texture + 相同 direction 的裁片共享全局纹理坐标系：以最大独立裁片为锚点，其他裁片的 offset 按 pattern image 中的相对位置对齐，确保相邻裁片缝合处图案连续。对称组/同形组随后重新同步，保持组内一致 |

### 如果子Agent失败

1. 检查 `ai_fill_plan_prompt.txt` 是否生成正确
2. 重新启动子 Agent，明确指出之前的错误
3. 如果子 Agent 持续失败，回退到后端规则：`创建填充计划.py` 不传 `--ai-plan`

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
  --token "$NEODOMAIN_ACCESS_TOKEN"
```

这条命令执行基础自动化路径（后端规则填充计划）。

### 带子Agent审美决策的完整路径（推荐）

```bash
# 步骤 1-6：生成看板、裁剪纹理、提取裁片、部位映射
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --collection-board /path/to/collection_board_1.png

# 步骤 7a：构造子Agent审美请求
python3 /path/to/auto-garment-producer/scripts/构造审美请求.py \
  --pieces /path/to/output/裁片清单.json \
  --garment-map /path/to/output/部位映射.json \
  --texture-set /path/to/output/面料组合.json \
  --out /path/to/output

# 步骤 7b：启动子Agent（使用 Agent 工具）
# 子Agent阅读 ai_fill_plan_prompt.txt，输出 ai_piece_fill_plan.json

# 步骤 8-10：后端校验、渲染、质检
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --collection-board /path/to/collection_board_1.png \
  --ai-plan /path/to/output/ai_piece_fill_plan.json
```

如果已有看板：

```bash
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --collection-board /path/to/collection_board_1.png \
  --out /path/to/output
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
