---
name: auto-garment-producer
description: 自动化成衣生产。从主题图或面料资产出发，生成商业成衣设计简报、Neo AI 面料看板、裁片填充计划，并用 OpenCV/Pillow 确定性渲染透明裁片 PNG。生成 / 创建 T 恤、T恤、t-shirt、tee、衬衫、男士衬衫、防晒服或同义上装时必须触发。
---

# 自动化成衣生产

把主题图或面料资产转成可生产的服装裁片样品。AI 只做视觉分析、提示词和审美决策；最终裁片 PNG 必须由程序渲染。用户提供主题图时，程序会让 AI 独立生成完整不裁头的透明主图，并生成可选的前片切半资产；默认生成 3 张单纹理（main/secondary/accent_light），每张纹理分别生成一套 `preview.png`。

## 固定模板

- `T恤`、`T 恤`、`t-shirt`、`tee`、`衬衫`、`男士衬衫` 等上装默认使用 `DDS26126XCJ01L`。
- 防晒服/防晒衣默认使用 `BFSK26308XCJ01L`。
- 只使用 `templates/<template>/s/` 下的 `pieces_s.json`、`garment_map_s.json`、mask、overview 和 `template_assets_s.json`。
- 模板 mask PNG 是生产权威资产，不得转 JPG；`*_kimi.jpg` 只用于 AI 看图和请求体减重。

## 标准流程

1. 主题图落盘：`--theme-image` 支持文件、目录、URL、base64、`AUTO_GARMENT_THEME_IMAGE`、`CODEX_ATTACHED_IMAGE_PATHS`，解析后写入 `out/theme_inputs/`。
2. 视觉分析：运行 `scripts/视觉元素提取.py` 生成 `ai_vision_prompt.txt`，由具备视觉理解能力的模型输出 `visual_elements.json`。
3. 设计简报：运行 `scripts/生成设计简报.py`，生成 `commercial_design_brief.json`、`style_profile.json`、`texture_prompts.json`。
4. 资产生成：没有 `--texture-set` 或 `--collection-board` 时，先把用户主题图上传为 Neo AI 参考图 URL，再调用 Neo AI 并行生成 3 张独立单纹理和 1 张透明主图。
5. 裁片准备：固定复用内置模板资产。
6. 主题前片：优先使用 AI 透明主图生成 `theme_front_left.png` 与 `theme_front_right.png`，并注册到 `texture_set.json` 作为可选定位资产；单纹理模板预览仍保留这些定位主图，将其分别落位到左前片与右前片。
7. 生产规划：运行 `scripts/构造生产规划请求.py` 生成 `ai_production_plan_prompt.txt`，由 AI 输出 `ai_production_plan.json`。
8. 程序执行：`scripts/应用生产规划.py`、`scripts/创建填充计划.py`、`scripts/渲染裁片.py` 依次生成填充计划和透明裁片；3 个纹理分别生成 3 套单纹理 9 裁片模板，直接输出在 `variants/<texture_id>/`。每套变体输出裁片 PNG、`preview.png`、`preview_white.jpg` 和清单。
9. **变体展示规则**：3 套单纹理变体（main/secondary/accent_light）生成后，**不得向用户展示**。stdout 仅提示“已生成 N 套单纹理结果至 variants/ 目录，不在此处展示”；`automation_summary.json` 仍保留完整变体路径供下游程序使用，但面向用户的输出必须隐藏变体预览图和详细路径。
10. **变体主图规则**：每套单纹理变体仍必须保留主题主图切半资产。`theme_front_left` 置于左前片，`theme_front_right` 置于右前片，左右前片合在一起即构成完整主图。单纹理预览仅将底纹统一为当前纹理，不得移除主题前片 overlay。预览图只作为结果文件，不再进入 AI 输入、复审或自动检查流程。

## 常用命令

主题图到面料与裁片：

```bash
python3 scripts/端到端自动化.py \
  --theme-image /path/to/theme.png \
  --out /path/to/output \
  --garment-type "T恤" \
  --mode standard \
  --reuse-cache
```

已有看板：

```bash
python3 scripts/端到端自动化.py \
  --collection-board /path/to/collection_board.png \
  --out /path/to/output \
  --garment-type "T恤" \
  --mode standard
```

已有面料组合：

```bash
python3 scripts/端到端自动化.py \
  --texture-set /path/to/texture_set.json \
  --out /path/to/output \
  --garment-type "T恤" \
  --mode standard
```

## 缓存与复用

- `--out /path/out` 会自动创建/复用 `/path/out/YYYYMMDD_HHMMSS/` 任务目录；所有业务文件都写入任务子目录。若 `--out` 已是时间戳目录，则直接在该目录续跑。
- 使用 `--reuse-cache` 后，视觉分析、生产规划和可复用程序产物会按输入 hash 复用。
- 图片缩略图写入源图同目录 `.thumbnails/`，文件名带源文件指纹；源图变化后不会误用旧缩略图。
- 模板部位与分区事实以模板 `garment_map` 和 `piece_overview` 为准。
- 已存在并匹配输入的 `visual_elements.json`、`ai_production_plan.json`、`texture_set.json`、模板 pieces 和 mask 不重复生成。

## AI 输出要求

- 视觉分析输出 `visual_elements.json`：包含 `dominant_objects`、`supporting_elements`、`palette`、`style`、`fabric_hints`、`source_images`、`fusion_strategy`、`generated_prompts`。
- `visual_elements.json` 必须包含 `theme_to_piece_strategy`：把主题拆成 `base_atmosphere`、`hero_motif`、`accent_details`、`quiet_zones`，并列出不得作为大身满版纹理的具象元素。
- 生产规划输出 `ai_production_plan.json`：包含 `garment_map.pieces[]` 和 `piece_fill_plan.pieces[]`。模板模式下 `garment_map` 可省略或被固定模板覆盖。
- 所有 JSON 必须是纯 JSON，不要 markdown 代码块或解释文字。

## 参考文档

- `references/数据契约.md`：JSON 文件契约。
- `references/纹理提示词策略.md`：面料与 motif 提示词规则。
- `references/服装艺术指导.md`：商业成衣审美原则。
- `references/渲染器规范.md`：确定性渲染约束。
- `references/服装术语词典.md`：角色/部位术语。
