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

1. **分析主题风格与商业意图**。保存 `商业设计简报.json`、`风格档案.json`、`纹理提示词.json`、`图案提示词.json`。
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
7. **运行 `scripts/创建填充计划.py`** 创建 `艺术指导方案.json` 和 `裁片填充计划.json`。
8. **运行 `scripts/渲染裁片.py`** 根据艺术指导填充计划生成透明裁片 PNG、预览图、联络单和清单。
9. **运行 `scripts/时尚质检.py`** 生成 `成品质检报告.json`。
10. **锁定审批**：用户确认结果后，锁定已批准的面料组合和填充计划，禁止覆盖。

## 商业服装规则

- 只选一个 hero（卖点）概念，放在 1–2 个关键裁片上。
- 大身裁片应低对比度，在零售距离下依然可穿。
- 小裁片和窄条应控制节奏、饰边和色彩呼应；不应承载复杂叙事艺术。
- 严禁将叙事插画直接切割到纸样裁片中。
- 避免均匀密度的全身填充；服装需要层次、呼吸感和明确的卖点。
- 优化审美、生成填充计划或评判输出质量时，阅读 `references/服装艺术指导.md`。

## 命令

### Neo AI 一键自动化

```bash
python3 /path/to/auto-garment-producer/scripts/端到端自动化.py \
  --pattern /path/to/pattern.png \
  --out /path/to/output \
  --token "$NEODOMAIN_ACCESS_TOKEN"
```

这条命令执行完整的自动化路径：

1. 调用 `neo-ai/scripts/generate_texture_collection_board.py` 生成 2×2 面料看板，
2. 将看板裁剪为 `main`、`secondary`、`accent` 和 `hero_motif`，
3. 写入 `面料组合.json`，
4. 提取裁片并推断服装部位，
5. 创建艺术指导 `裁片填充计划.json`，
6. 渲染透明裁片 PNG、预览图、清单和成衣 QC。

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
面料组合.json
纹理质检报告.json
裁片清单.json
部位映射.json
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
