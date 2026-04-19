---
name: auto-garment-producer
description: 自动化成衣生产。从主题图或已批准面料出发，生成商业成衣设计简报、面料看板、裁片填充计划，并用 OpenCV/Pillow 确定性渲染透明裁片 PNG。生成 / 创建 T 恤、T恤、t-shirt、tee、衬衫、男士衬衫或同义上装时必须触发。
---

# 自动化成衣生产

把主题图或已审批面料转成可生产的服装裁片样品。AI 只做视觉分析、提示词和审美决策；最终裁片 PNG 必须由程序用已批准资产渲染。禁止把叙事插画直接切进纸样，禁止声称生成正面成衣 mockup、模特上身图或产品照。

## 默认模板

- 用户要 `T恤`、`T 恤`、`t-shirt`、`tee`、`衬衫`、`男士衬衫` 等上装且未提供纸样时，优先复用内置 `DDS26126XCJ01L`，默认 `s` 码。
- 用户要防晒服/防晒衣时，优先复用 `BFSK26308XCJ01L`，默认 `s` 码。
- 只有用户明确禁用模板、指定其它模板，或模板资产不完整时，才走手动 `--pattern` 路径。
- 内置模板命中时复用 `templates/<template>/<size>/pieces_*.json`、`garment_map_*.json`、mask 和 overview，不重复提取已有裁片。
- 模板 mask PNG 是生产权威资产，不得转 JPG；模板目录可预生成 `*_kimi.jpg` 和 `template_assets_*.json`，用于 AI 看图和请求体减重。
- `references/styles/*-style-reference.jpg` 和 `*-wearable-zoning.jpg` 是历史效果参考资产，只能用于人工排查模板方向；不得进入自动生产规划 prompt、`kimi_images` 或任何子 Agent 视觉输入。

## 标准流程

1. 主题图落盘：端到端脚本只能消费本地图片。`--theme-image` 支持文件、目录、URL、base64、`AUTO_GARMENT_THEME_IMAGE`、`CODEX_ATTACHED_IMAGE_PATHS`，解析后写入 `out/theme_inputs/`。
2. 视觉分析：运行 `scripts/视觉元素提取.py` 生成 `ai_vision_prompt.txt`，启动子 Agent 看图并输出 `visual_elements.json`。
3. 设计简报：运行 `scripts/生成设计简报.py`，生成 `commercial_design_brief.json`、`style_profile.json`、`texture_prompts.json`、`dual_collection_prompts.json`。
4. 资产生成：默认双源调用 Neo AI + libtv-skill 生成 3x3 面料看板。已有 `--texture-set` 或 `--collection-board` 时跳过重复生成。
5. 裁片准备：命中内置模板则复用模板资产；否则运行 `scripts/提取裁片.py` 和几何推断。
6. 生产规划：运行 `scripts/构造生产规划请求.py` 生成 `ai_production_plan_prompt.txt`，启动子 Agent 输出 `ai_production_plan.json`，其中必须包含 `garment_map`、`pre_design_self_check`、`asset_shortlist`、`pre_submit_self_audit` 和 `piece_fill_plan`。生产规划只传 `piece_overview`、`texture_contact_sheet` 和结构化 JSON；不得传入 `references/styles/*-style-reference.jpg` 或 `*-wearable-zoning.jpg`。
7. 程序执行：`scripts/应用生产规划.py`、`scripts/创建填充计划.py`、`scripts/渲染裁片.py`、`scripts/时尚质检.py` 依次校验并渲染。后端只做安全修正，不替代审美决策。
8. 商业复审：运行 `scripts/构造商业复审请求.py` 生成 `ai_commercial_review_prompt.txt`。复审只传 `piece_contact_sheet.jpg` 的 Kimi 缩略图及其 3m 模拟缩略图；`preview.png` 和 `preview_white.jpg` 只给用户查看，不参与 LLM 流程。

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

普通 `--dual-source` 会把 Neo AI 和 libtv-skill 两个来源分别作为风格结果继续渲染；需要让 AI 基于双源 9+9 完整资产池输出多套设计方案时，必须同时传 `--multi-scheme`。

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

生成整套尺寸：

```bash
python3 scripts/端到端自动化.py \
  --out /path/to/output \
  --garment-type "T恤" \
  --full-set
```

## 缓存与复用

- `--out /path/out` 会自动创建/复用 `/path/out/YYYYMMDD_HHMMSS/` 任务目录；所有业务文件都写入任务子目录。若 `--out` 已是时间戳目录，则直接在该目录续跑。
- 使用 `--reuse-cache` 后，视觉分析、裁片、纹理集、生产规划、商业复审会按输入 hash 复用。
- 图片缩略图写入源图同目录 `.thumbnails/`，文件名带源文件指纹；源图变化后不会误用旧缩略图。
- 内置模板应优先运行 `scripts/预处理模板资产.py --all`，提前生成固定命名的 `piece_overview_<size>_kimi.jpg`、`prepared_pattern_<size>_kimi.jpg`、`garment_map_overview_<size>_kimi.jpg` 和 `template_assets_<size>.json`。运行时优先读取 manifest 中的 Kimi JPEG，缺失时才动态生成缩略图。
- 模板部位与分区事实以模板 `garment_map` 和 `piece_overview` 为准；历史款式效果参考图不参与缓存复用、请求构造或子 Agent 决策。
- 已存在并匹配输入的 `visual_elements.json`、`ai_production_plan.json`、`texture_set.json`、模板 pieces/masks 不重复生成。
- 多方案模式使用 `--dual-source --multi-scheme --max-schemes N`，默认 `N=8`。双源都成功时先合并为 18 图资产池（A/B 各 9 张，资产 ID 带 `_a` / `_b` 后缀），再要求 AI 输出 `ai_multi_production_plan.json` 的 `schemes` 数组；单套失败不阻塞其它方案。

## 子 Agent 输出要求

- 视觉分析输出 `visual_elements.json`：包含 `dominant_objects`、`supporting_elements`、`palette`、`style`、`fabric_hints`、`source_images`、`fusion_strategy`、`generated_prompts`。`dominant_objects[]` 必须包含 `grade` 和 `garment_placement_hint`。
- `visual_elements.json` 必须包含 `theme_to_piece_strategy`：把主题拆成 `base_atmosphere`、`hero_motif`、`accent_details`、`quiet_zones`，并列出不得作为大身满版纹理的具象元素。
- 生产规划输出 `ai_production_plan.json`：包含 `garment_map.pieces[]`、结构化 `pre_design_self_check`、`asset_shortlist`、`pre_submit_self_audit` 和 `piece_fill_plan.pieces[]`。任一自检 fail 时不得提交原方案，必须同次改成可交付方案或 fallback safe plan。
- 多方案生产规划输出 `ai_multi_production_plan.json`：顶层包含 `schemes[]`、`portfolio_notes`、`asset_coverage`。每个 scheme 必须包含 `scheme_id`、`design_positioning`、`strategy_note`、`garment_map_confidence_per_piece`、`garment_map_uncertainties`、`pre_design_self_check`、`asset_shortlist`、`pre_submit_self_audit`、`theme_landing_summary`、`asset_mix_summary`、`diversity_tags`、`piece_fill_plan`；模板模式下 `garment_map` 可省略或被固定模板覆盖。
- 面料资产必须通过色板/风格一致性质检。明显跨风格拼贴、配色跳脱、非透明 motif、半透明整张贴片不得进入最终裁片。
- 所有 JSON 必须是纯 JSON，不要 markdown 代码块或解释文字。

## 参考文档

- `references/数据契约.md`：JSON 文件契约。
- `references/纹理提示词策略.md`：面料与 motif 提示词规则。
- `references/服装艺术指导.md`：商业成衣审美原则。
- `references/质检规则.md`：程序质检和返工规则。
- `references/渲染器规范.md`：确定性渲染约束。
- `references/服装术语词典.md`：角色/部位术语。
