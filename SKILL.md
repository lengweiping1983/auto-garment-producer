---
name: auto-garment-producer
description: 自动化成衣生产。从主题图或已批准面料出发，生成商业成衣设计简报、面料看板、裁片填充计划，并用 OpenCV/Pillow 确定性渲染透明裁片 PNG。生成 / 创建 T 恤、T恤、t-shirt、tee、衬衫、男士衬衫或同义上装时必须触发。
---

# 自动化成衣生产

把主题图或面料资产转成可生产的服装裁片样品。AI 只做视觉分析、提示词和审美决策；最终裁片 PNG 必须由程序渲染。用户提供主题图时，程序会把主题主体裁剪后竖向切成左右两半，并强制落到正面两片衣服上。

## 默认模板

- 用户要 `T恤`、`T 恤`、`t-shirt`、`tee`、`衬衫`、`男士衬衫` 等上装且未提供纸样时，复用内置 `DDS26126XCJ01L`。
- 用户要防晒服/防晒衣时，复用内置 `BFSK26308XCJ01L`。
- 内置模板命中时复用 `templates/<template>/s/pieces_s.json`、`garment_map_s.json`、mask 和 overview，不重复提取已有裁片。
- 模板 mask PNG 是生产权威资产，不得转 JPG；模板目录可预生成 `*_kimi.jpg` 和 `template_assets_s.json`，用于 AI 看图和请求体减重。

## 标准流程

1. 主题图落盘：端到端脚本只能消费本地图片。`--theme-image` 支持文件、目录、URL、base64、`AUTO_GARMENT_THEME_IMAGE`、`CODEX_ATTACHED_IMAGE_PATHS`，解析后写入 `out/theme_inputs/`。
2. 视觉分析：运行 `scripts/视觉元素提取.py` 生成 `ai_vision_prompt.txt`，启动子 Agent 看图并输出 `visual_elements.json`。
3. 设计简报：运行 `scripts/生成设计简报.py`，生成 `commercial_design_brief.json`、`style_profile.json`、`texture_prompts.json`、`dual_collection_prompts.json`。
4. 资产生成：默认双源调用 Neo AI + libtv-skill 生成 3x3 面料看板。已有 `--texture-set` 或 `--collection-board` 时跳过重复生成。
5. 裁片准备：命中内置模板则复用模板资产；否则运行 `scripts/提取裁片.py` 和几何推断。
6. 主题前片：有主题图时，程序生成 `theme_front_left.png` 与 `theme_front_right.png`，并注册到 `texture_set.json`。
7. 生产规划：运行 `scripts/构造生产规划请求.py` 生成 `ai_production_plan_prompt.txt`，启动子 Agent 输出 `ai_production_plan.json`。
8. 程序执行：`scripts/应用生产规划.py`、`scripts/创建填充计划.py`、`scripts/渲染裁片.py` 依次生成填充计划和透明裁片。

## 常用命令

主题图到双源面料与裁片：

```bash
python3 scripts/端到端自动化.py \
  --theme-image /path/to/theme.png \
  --out /path/to/output \
  --garment-type "T恤" \
  --mode standard \
  --dual-source \
  --multi-scheme \
  --max-schemes 8 \
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

只构造生产规划请求，等待子 Agent：

```bash
python3 scripts/端到端自动化.py \
  --texture-set /path/to/texture_set.json \
  --out /path/to/output \
  --garment-type "T恤" \
  --construct-ai-request
```

## 缓存与复用

- `--out /path/out` 会自动创建/复用 `/path/out/YYYYMMDD_HHMMSS/` 任务目录；所有业务文件都写入任务子目录。若 `--out` 已是时间戳目录，则直接在该目录续跑。
- 使用 `--reuse-cache` 后，视觉分析、裁片、纹理集、生产规划会按输入 hash 复用。
- 图片缩略图写入源图同目录 `.thumbnails/`，文件名带源文件指纹；源图变化后不会误用旧缩略图。
- 模板部位与分区事实以模板 `garment_map` 和 `piece_overview` 为准。
- 已存在并匹配输入的 `visual_elements.json`、`ai_production_plan.json`、`texture_set.json`、模板 pieces 和 mask 不重复生成。
- 多方案模式使用 `--dual-source --multi-scheme --max-schemes N`，默认 `N=8`。

## 子 Agent 输出要求

- 视觉分析输出 `visual_elements.json`：包含 `dominant_objects`、`supporting_elements`、`palette`、`style`、`fabric_hints`、`source_images`、`fusion_strategy`、`generated_prompts`。
- `visual_elements.json` 必须包含 `theme_to_piece_strategy`：把主题拆成 `base_atmosphere`、`hero_motif`、`accent_details`、`quiet_zones`，并列出不得作为大身满版纹理的具象元素。
- 生产规划输出 `ai_production_plan.json`：包含 `garment_map.pieces[]` 和 `piece_fill_plan.pieces[]`。模板模式下 `garment_map` 可省略或被固定模板覆盖。
- 多方案生产规划输出 `ai_multi_production_plan.json`：顶层包含 `schemes[]`、`portfolio_notes`、`asset_coverage`。每个 scheme 必须包含 `scheme_id`、`design_positioning`、`strategy_note`、`piece_fill_plan`。
- 所有 JSON 必须是纯 JSON，不要 markdown 代码块或解释文字。

## 参考文档

- `references/数据契约.md`：JSON 文件契约。
- `references/纹理提示词策略.md`：面料与 motif 提示词规则。
- `references/服装艺术指导.md`：商业成衣审美原则。
- `references/渲染器规范.md`：确定性渲染约束。
- `references/服装术语词典.md`：角色/部位术语。
