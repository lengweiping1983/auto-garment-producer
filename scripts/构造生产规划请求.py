#!/usr/bin/env python3
"""
构造生产规划 AI 请求 —— 合并部位识别 + 审美决策为一次 AI 调用。

输出：
- ai_production_plan_prompt.txt：面向子 Agent 的综合规划请求
- ai_production_plan_request.json：结构化请求摘要

子 Agent 预期输出：ai_production_plan.json，包含 garment_map + piece_fill_plan。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from image_utils import ensure_thumbnail

SKILL_DIR = Path(__file__).resolve().parents[1]
STYLE_REFERENCE_DIR = SKILL_DIR / "references" / "styles"
STYLE_REFERENCE_BY_TEMPLATE = {
    "BFSK26308XCJ01L": {
        "path": STYLE_REFERENCE_DIR / "BFSK26308XCJ01L-style-reference.jpg",
        "label": "BFSK26308XCJ01L 男士防晒服标准裁片参考图",
        "notes": "前后身片、袖片、门襟/下摆窄条在该参考图中已有成衣印花方向，可用于判断纸样部位与上下方向。",
    },
    "DDS26126XCJ01L": {
        "path": STYLE_REFERENCE_DIR / "DDS26126XCJ01L-style-reference.jpg",
        "label": "DDS26126XCJ01L 男士衬衫标准裁片参考图",
        "notes": "衬衫前后片、袖片、领/门襟/窄条在该参考图中已有成衣印花方向，可用于判断部位、对称组和饰边。",
    },
}


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_visual_motif_geometries(visual_elements: dict) -> dict:
    """从 visual_elements 提取 motif 几何信息。"""
    geos = {}
    for obj in visual_elements.get("dominant_objects", []):
        usage = obj.get("suggested_usage", "")
        geo = obj.get("geometry", {})
        if usage in ("hero_motif", "accent_motif", "motif"):
            geos[obj.get("name", "motif")] = geo
    return geos


def infer_style_references(pieces_payload: dict, brief: dict) -> list[dict]:
    """根据纸样来源或服装类型匹配款式参考图。"""
    haystack = " ".join(
        str(value)
        for value in (
            pieces_payload.get("pattern_image", ""),
            pieces_payload.get("prepared_pattern", ""),
            pieces_payload.get("overview_image", ""),
            brief.get("garment_type", ""),
        )
    ).lower()
    refs = []
    for template_id, ref in STYLE_REFERENCE_BY_TEMPLATE.items():
        template_lower = template_id.lower()
        if template_lower in haystack or (
            template_id == "BFSK26308XCJ01L" and any(term in haystack for term in ("防晒", "sun protection"))
        ) or (
            template_id == "DDS26126XCJ01L" and any(term in haystack for term in ("衬衫", "shirt"))
        ):
            path = ref["path"]
            if path.exists():
                refs.append({
                    "template_id": template_id,
                    "label": ref["label"],
                    "path": str(path.resolve()),
                    "notes": ref["notes"],
                })
    return refs


def build_production_plan_prompt(
    pieces_payload: dict,
    texture_set: dict,
    brief: dict,
    geometry_hints: dict,
    visual_elements: dict | None,
    piece_overview_path: str,
    texture_thumbnail_paths: list[str],
    style_reference_paths: list[dict] | None = None,
    multi_scheme: bool = False,
    max_schemes: int = 4,
) -> str:
    """构造合并后的生产规划 prompt。"""
    pieces = pieces_payload.get("pieces", [])
    garment_type = brief.get("garment_type", "成衣")
    type_hints = {
        "shirt": "衬衫/上衣类：通常有前片、后片、2个袖子、领、袖口。前片可能有口袋。",
        "dress": "连衣裙：前片、后片、袖子（或无袖）、裙摆、可能有育克或拼接。",
        "jacket": "外套：前片、后片、2个袖子、领、门襟、可能有口袋盖。trim较宽。",
        "coat": "大衣：前片、后片、2个袖子、领、下摆。body区比例大。",
        "pants": "裤装：前片、后片、腰头、裤脚。无袖子。可能有口袋。",
        "children": "童装：部位较小，比例紧凑。hero通常在胸口。",
        "children outerwear set": "儿童外套套装：通常有前片、后片、2个袖子、领、门襟、下摆。",
        "衬衫": "衬衫/上衣类：通常有前片、后片、2个袖子、领、袖口。",
        "上衣": "衬衫/上衣类：通常有前片、后片、2个袖子、领、袖口。",
        "连衣裙": "连衣裙：前片、后片、袖子（或无袖）、裙摆、可能有育克或拼接。",
        "外套": "外套：前片、后片、2个袖子、领、门襟、可能有口袋盖。trim较宽。",
        "大衣": "大衣：前片、后片、2个袖子、领、下摆。body区比例大。",
        "裤装": "裤装：前片、后片、腰头、裤脚。无袖子。可能有口袋。",
        "裤子": "裤装：前片、后片、腰头、裤脚。无袖子。可能有口袋。",
        "童装": "童装：部位较小，比例紧凑。hero通常在胸口。",
        "儿童外套套装": "儿童外套套装：通常有前片、后片、2个袖子、领、门襟、下摆。",
        "儿童外套": "儿童外套套装：通常有前片、后片、2个袖子、领、门襟、下摆。",
        "套装": "套装：可能包含上衣+下装，或外套+裤子组合。",
        "女装": "女装通用：根据几何特征判断部位。",
        "男装": "男装通用：根据几何特征判断部位。",
        "通用成衣": "通用成衣：根据几何特征判断部位。",
    }
    gt_lower = garment_type.lower().strip()
    type_hint = type_hints.get(gt_lower, f"{garment_type}类服装，请根据裁片形状和上下文推断")

    lines = [
        "你是一位资深服装印花艺术指导兼专业打版师。你的任务是为一组已提取的纸样裁片完成「部位识别」和「填充计划」两项决策。",
        "",
        "===== 任务概述 =====",
        "本任务分为两个思考阶段：",
        "  Step 1 — 部位识别：根据纸样排版图判断每个裁片的服装部位、对称关系。",
        "  Step 2 — 填充决策：基于 Step 1 的结果和可用的面料资产，为每个裁片制定 base/overlay/trim 填充计划。",
        "",
        "===== 强制看图要求 =====",
        f"你必须先查看以下图片，再做任何判断。未看图直接输出的结果无效。",
        f"",
        f"【必看 1】纸样排版图: {piece_overview_path}",
        "  这是服装的纸样 mask 总览。每个白色区域是一个裁片。看图时注意：",
        "  - 裁片的实际形状（是矩形、弧形、窄条还是不规则？）",
        "  - 裁片之间的相对位置和大小关系",
        "  - 哪些裁片看起来是左右对称的（大小形状相同、位置左右对应）",
        "",
    ]

    if style_reference_paths:
        lines.append("【必看 2】款式参考图（用于部位识别和上下方向判断）：")
        for ref in style_reference_paths:
            lines.append(f"  - {ref.get('label', '款式参考图')}: {ref.get('path', '')}")
            if ref.get("notes"):
                lines.append(f"    参考重点: {ref['notes']}")
        lines.extend([
            "  请先把款式参考图中的标准裁片形状，与 piece_overview.png 中的白色 mask 一一对照。",
            "  如果几何数字和参考图冲突，以款式参考图 + piece_overview.png 的视觉判断为准。",
            "",
        ])

    if texture_thumbnail_paths:
        lines.append("【必看 3】面料资产缩略图（必须查看后再分配）：")
        for tp in texture_thumbnail_paths:
            lines.append(f"  - {tp}")
        lines.append("")

    lines.extend([
        "===== 服装类型指导 =====",
        f"类型: {garment_type}",
        f"特征: {type_hint}",
        "",
        "===== 设计简报 =====",
        f"审美方向: {brief.get('aesthetic_direction', '商业畅销款')}",
        f"季节: {brief.get('season', '四季')}",
        f"目标客群: {brief.get('target_customer', '大众')}",
        f"需避免: {brief.get('avoid_elements', [])}",
    ])

    palette = brief.get("palette", [])
    if palette:
        lines.append(f"色板: {palette}")

    # fabric has_nap info
    fabric = brief.get("fabric", {})
    if fabric.get("has_nap"):
        lines.extend([
            "",
            f"⚠️ 面料工艺注意：has_nap=true（绒毛面料，如灯芯绒/丝绒）。nap_direction={fabric.get('nap_direction', 'vertical')}。",
            "  所有使用 texture 的裁片 rotation 必须保持一致（程序会强制校验）。",
        ])

    lines.extend([
        "",
        "===== Step 1: 部位识别 =====",
        "请为每个裁片标注：garment_role, zone, symmetry_group, same_shape_group, texture_direction, confidence, needs_ai_review",
        "可选 garment_role: front_body / back_body / sleeve_left / sleeve_right / collar_or_upper_trim / hem_or_lower_trim / trim_strip / side_or_long_panel / front_hero / yoke / pocket / lining / small_detail / unknown",
        "zone: body / secondary / trim / detail",
        "",
        "===== 裁片几何信息（JSON 摘要，程序推断仅供参考，请以 piece_overview.png 为准）=====",
    ])

    # 压缩裁片信息：用 JSON 摘要代替逐条展开，只保留关键字段
    hint_by_id = {h["piece_id"]: h for h in geometry_hints.get("pieces", [])}
    compact_pieces = []
    for p in sorted(pieces, key=lambda x: x.get("area", 0), reverse=True):
        pid = p["piece_id"]
        h = hint_by_id.get(pid, {})
        orient = h.get("pattern_orientation", 0)
        orient_str = ""
        if orient == 180:
            orient_str = f", 倒置(conf={h.get('orientation_confidence',0)})"
        elif orient != 0:
            orient_str = f", {orient}°"
        compact_pieces.append(
            f"{pid}: 面积={p.get('area',0)}, "
            f"尺寸={p.get('width',0)}×{p.get('height',0)}, "
            f"推测={h.get('geometry_role_hint','unknown')}{orient_str}"
        )
    lines.append("  " + "; ".join(compact_pieces))

    lines.extend([
        "",
        "===== 裁片方向补偿（重要）=====",
        "某些裁片在纸样 pattern 中可能是倒置的（领口朝下，pattern_orientation=180°）。",
        "当你在 Step 2 制定填充计划时，必须注意：",
        "  - 若裁片 pattern_orientation=180°，base texture 的 rotation 需额外 +180°，",
        "    或 texture_direction 相应反转，使纹理在最终成衣中呈现正确方向。",
        "  - 若裁片 pattern_orientation=180°，motif overlay 的 rotation 也需额外 +180°，",
        "    确保主题花（如中心大花）在穿着时花头朝上，不被倒置。",
        "  - 程序会在渲染时自动应用 pattern_orientation 补偿，你的 rotation 值是在此基础上的增量。",
        "",
        "===== Step 2: 填充决策 =====",
        "基于 Step 1 的部位识别结果和以下面料资产，为每个裁片制定填充计划。",
        "",
        "--- 可用面料资产 ---",
    ])

    is_merged_set = any("source" in t for t in texture_set.get("textures", []))
    # 面料资产列表：压缩为 ID + role，省略 prompt（AI 会查看缩略图）
    asset_lines = []
    for tex in texture_set.get("textures", []):
        if tex.get("approved", False) or tex.get("candidate", False):
            source_tag = f"[{tex.get('source','')}]" if is_merged_set else ""
            asset_lines.append(f"{tex.get('texture_id')}:{tex.get('role','')}{source_tag}")
    for motif in texture_set.get("motifs", []):
        if motif.get("approved", False) or motif.get("candidate", False):
            source_tag = f"[{motif.get('source','')}]" if is_merged_set else ""
            asset_lines.append(f"{motif.get('motif_id')}:{motif.get('role','')}{source_tag}")
    for solid in texture_set.get("solids", []):
        source_tag = f"[{solid.get('source','')}]" if is_merged_set else ""
        asset_lines.append(f"{solid.get('solid_id')}:{solid.get('color','')}{source_tag}")
    if asset_lines:
        lines.append("  " + ", ".join(asset_lines))
    else:
        lines.append("  （无可用资产）")

    lines.extend([
        "",
        "--- 填充规则 ---",
        "  1. 同 symmetry_group / same_shape_group 的 base 层必须完全相同。",
        "  2. 仅允许 1 个 hero overlay（motif），trim 禁用 motif。",
        "  3. trim 区域 base 用 quiet solid 或 subtle dark texture。",
        "  4. 每个裁片提供 reason（中文解释）。",
        "  5. 纹理方向自主决定，不要硬套规则。",
        "  6. 可声明 intentional_asymmetry: true 保留有意不对称。",
        "",
        "--- 审美原则 ---",
        "  - 可穿性优先，大身低噪，hero 醒目但不突兀。",
        "  - 避免叙事插画被切割到裁片中。",
        "  - 优秀设计 = 1个卖点 + 安静支持纹理 + 克制饰边。",
        "  - 大身裁片低对比度，零售距离可穿。",
        "",
    ])

    # motif 几何信息
    if visual_elements:
        motif_geos = _load_visual_motif_geometries(visual_elements)
        if motif_geos:
            lines.append("--- Motif 几何参考（来自主题图视觉分析）---")
            for name, geo in motif_geos.items():
                lines.append(
                    f"  {name}: 像素尺寸={geo.get('pixel_width', '?')}×{geo.get('pixel_height', '?')}, "
                    f"方向={geo.get('orientation', '?')}, 画面占比={geo.get('canvas_ratio', '?')}"
                )
            lines.append("")

    # 多方案模式：插入策略指导和输出格式改造
    if multi_scheme:
        # 检测可用资产源数量（通过后缀 _a / _b 精确匹配）
        all_assets = texture_set.get("textures", []) + texture_set.get("motifs", []) + texture_set.get("solids", [])
        def _asset_id(a):
            return a.get("texture_id", a.get("motif_id", a.get("solid_id", "")))
        has_a = any(_asset_id(a).endswith("_a") for a in all_assets)
        has_b = any(_asset_id(a).endswith("_b") for a in all_assets)
        source_count = sum([has_a, has_b])

        if source_count >= 2:
            strategy_lines = [
                f"你需要生成 {max_schemes} 套不同的设计方案（schemes）。每套方案必须有明确的商业定位差异。",
                "",
                "当前拥有两套差异化资产（源A + 源B），请充分利用这 18 个资产的组合空间：",
                "  - scheme_01 '保守量产方案'：全部使用 源A 资产（带 _a 后缀），低噪安全，适合大货量产。",
                "  - scheme_02 '大胆秀场方案'：全部使用 源B 资产（带 _b 后缀），视觉冲击，适合提案或限量款。",
                "  - scheme_03 '混搭对比方案'：主底纹/卖点图案用 源A，辅纹/点缀用 源B，形成材质对比。",
                "  - scheme_04 '反向混搭方案'：主底纹用 源B（更大胆），卖点图案用 源A（更克制）。",
                "",
                "每套方案的 piece_fill_plan 中，base.texture_id / overlay.motif_id / trim 必须使用带 _a 或 _b 后缀的 id。",
                "例如：main_a, main_b, hero_motif_1_a, hero_motif_1_b, quiet_solid_a 等。",
            ]
        else:
            # 单源情况（1 套或 0 套有后缀都 fallback 为单源）：从 9 个资产中组合多套方案
            source_label = "源A" if has_a else "源B" if has_b else "当前可用"
            source_tag_note = "资产 ID 带 _a 后缀" if has_a else "资产 ID 带 _b 后缀" if has_b else "使用原始资产 ID（无后缀）"
            strategy_lines = [
                f"你需要生成 {max_schemes} 套不同的设计方案（schemes）。虽然只有一套 3×3 看板的 9 个资产，但这 9 个面板仍有丰富的组合空间，请充分发挥创造力。",
                "",
                f"当前只有一套资产（{source_label}），{source_tag_note}，请从这 9 个面板中组合出多套差异化方案：",
                "  - scheme_01 '经典主调方案'：主底纹 + 标准 hero 定位 + 安静饰边，稳妥可穿。",
                "  - scheme_02 '深色反转方案'：使用 dark_base 作为主底纹，营造沉稳/高级感，hero 图案保持浅色对比。",
                "  - scheme_03 '图案聚焦方案'：将 hero_motif 放大或改变位置（如偏左/偏下），其他区域极度安静，突出单品感。",
                "  - scheme_04 '纹理层次方案'：body 用主底纹，secondary 用 accent_mid，trim 用 accent_light，形成同色系层次变化。",
                "  - scheme_05 '点缀跳跃方案'：body 用 solid_quiet，仅在局部（如口袋、袖口）使用小面积 accent 纹理或 trim_motif，极简克制。",
                "",
                "即使是同一套资产，不同的分配策略（谁做 base、谁做 overlay、scale/rotation/anchor 如何变化）也能产生截然不同的商业效果。",
                "请尽可能给出多套有实质差异的方案，不要敷衍。",
            ]

        lines.extend(["", "===== 多方案策略指导 ====="] + strategy_lines + [""])

    lines.extend([
        "===== 输出格式 =====",
        "请返回严格的 JSON，格式如下（不要任何解释文字、不要 markdown 代码块，只返回纯 JSON）：",
        "",
    ])

    if multi_scheme:
        lines.append(json.dumps({
            "schemes": [
                {
                    "scheme_id": "scheme_01",
                    "strategy_note": "描述策略",
                    "garment_map": {
                        "pieces": [
                            {
                                "piece_id": "piece_001",
                                "garment_role": "front_body",
                                "zone": "body",
                                "symmetry_group": "sg_front",
                                "same_shape_group": "",
                                "texture_direction": "transverse",
                                "confidence": 0.88,
                                "needs_ai_review": False
                            }
                        ]
                    },
                    "piece_fill_plan": {
                        "pieces": [
                            {
                                "piece_id": "piece_001",
                                "base": {
                                    "fill_type": "texture",
                                    "texture_id": "main_a",
                                    "scale": 1.0,
                                    "rotation": 0,
                                    "offset_x": 0,
                                    "offset_y": 0,
                                    "mirror_x": False,
                                    "mirror_y": False
                                },
                                "overlay": {
                                    "fill_type": "motif",
                                    "motif_id": "hero_motif_1_a",
                                    "anchor": "center",
                                    "scale": 0.72,
                                    "opacity": 0.92,
                                    "offset_x": 0,
                                    "offset_y": -40
                                },
                                "trim": None,
                                "texture_direction": "transverse",
                                "reason": "前片使用源A主底纹横向铺陈，中心定位源A牡丹图案",
                                "intentional_asymmetry": False
                            }
                        ],
                        "art_direction": {
                            "strategy": "单一卖点定位，低噪身片，协调副片，安静饰边",
                            "hero_piece_ids": ["piece_001"]
                        }
                    }
                }
            ],
            "risk_notes": []
        }, ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("注：art_direction 可额外包含 notes[] 和可选的 self_assessment（overall_score/wearability/cohesion/hero_clarity/trim_quality/season_fit/customer_match/production_safety/color_balance/negative_space/narrative_control）。")
    else:
        lines.append(json.dumps({
            "garment_map": {
                "pieces": [
                    {
                        "piece_id": "piece_001",
                        "garment_role": "front_body",
                        "zone": "body",
                        "symmetry_group": "sg_front",
                        "same_shape_group": "",
                        "texture_direction": "transverse",
                        "confidence": 0.88,
                        "needs_ai_review": False
                    }
                ]
            },
            "piece_fill_plan": {
                "pieces": [
                    {
                        "piece_id": "piece_001",
                        "base": {
                            "fill_type": "texture",
                            "texture_id": "main",
                            "scale": 1.0,
                            "rotation": 0,
                            "offset_x": 0,
                            "offset_y": 0,
                            "mirror_x": False,
                            "mirror_y": False
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
                        "trim": None,
                        "texture_direction": "transverse",
                        "reason": "前片使用主底纹横向铺陈，中心定位牡丹图案",
                        "intentional_asymmetry": False
                    }
                ],
                "art_direction": {
                    "strategy": "单一卖点定位，低噪身片，协调副片，安静饰边",
                    "hero_piece_ids": ["piece_001"]
                }
            },
            "risk_notes": []
        }, ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("注：art_direction 可额外包含 notes[] 和可选的 self_assessment（overall_score/wearability/cohesion/hero_clarity/trim_quality/season_fit/customer_match/production_safety/color_balance/negative_space/narrative_control）。")

    lines.extend([
        "",
        "===== 重要声明 =====",
        "以上所有程序推断（裁片几何、部位候选、面料描述）均为参考建议，不是事实。",
        "你必须结合 piece_overview.png 和面料缩略图重新确认每个裁片的部位和填充方案。",
    ])

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造合并部位识别+审美决策的生产规划 AI 请求。")
    parser.add_argument("--pieces", required=True, help="pieces.json 路径")
    parser.add_argument("--texture-set", required=True, help="texture_set.json 路径")
    parser.add_argument("--brief", default="", help="commercial_design_brief.json 路径")
    parser.add_argument("--geometry-hints", default="", help="geometry_hints.json 路径")
    parser.add_argument("--visual-elements", default="", help="visual_elements.json 路径（可选，用于 motif 几何信息）")
    parser.add_argument("--garment-map", default="", help="已有 garment_map.json（可选，作为 fallback 参考）")
    parser.add_argument("--piece-overview", default="", help="piece_overview.png 路径。若省略，尝试从 pieces.json 所在目录推断。")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--multi-scheme", action="store_true", help="启用多方案模式。要求 AI 输出多套不同的 piece_fill_plan。")
    parser.add_argument("--max-schemes", type=int, default=4, help="最大方案数（默认 4）。")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pieces_payload = load_json(args.pieces)
    texture_set = load_json(args.texture_set)

    brief = {}
    if args.brief:
        try:
            brief = load_json(args.brief)
        except Exception as exc:
            print(f"[警告] 无法读取 brief: {exc}", file=sys.stderr)

    geometry_hints = {}
    if args.geometry_hints:
        try:
            geometry_hints = load_json(args.geometry_hints)
        except Exception as exc:
            print(f"[警告] 无法读取 geometry_hints: {exc}", file=sys.stderr)
    else:
        # 尝试从 pieces 自动生成简易 hints
        pieces = pieces_payload.get("pieces", [])
        if pieces:
            largest_area = max(p.get("area", 0) for p in pieces)
            hints = []
            for p in sorted(pieces, key=lambda x: x.get("area", 0), reverse=True):
                area_ratio = p.get("area", 0) / max(1, largest_area)
                aspect = p.get("width", 1) / max(1, p.get("height", 1))
                hints.append({
                    "piece_id": p["piece_id"],
                    "area": p.get("area", 0),
                    "area_ratio": round(area_ratio, 3),
                    "width": p.get("width", 0),
                    "height": p.get("height", 0),
                    "aspect_ratio": round(aspect, 2),
                    "geometry_role_hint": "unknown",
                })
            geometry_hints = {"pieces": hints}

    visual_elements = None
    if args.visual_elements:
        try:
            visual_elements = load_json(args.visual_elements)
        except Exception as exc:
            print(f"[警告] 无法读取 visual_elements: {exc}", file=sys.stderr)

    # 推断 piece_overview 路径
    overview_path = args.piece_overview
    if not overview_path:
        pieces_dir = Path(args.pieces).parent
        overview_candidates = ["piece_overview.png", "piece_overview.jpg", "garment_map_overview.jpg"]
        pieces_stem = Path(args.pieces).stem
        if pieces_stem.startswith("pieces_"):
            size_label = pieces_stem.removeprefix("pieces_")
            overview_candidates.insert(0, f"piece_overview_{size_label}.png")
        for cand in overview_candidates:
            cp = pieces_dir / cand
            if cp.exists():
                overview_path = str(cp.resolve())
                break

    # 收集面料资产缩略图路径（生成真正的缩略图，避免发送 1.5MB+ 全尺寸图）
    texture_thumbnails = []
    base_dir = Path(args.texture_set).parent
    for tex in texture_set.get("textures", []) + texture_set.get("motifs", []):
        p = tex.get("path", "")
        if p:
            tp = Path(p) if Path(p).is_absolute() else base_dir / p
            if tp.exists():
                thumb = ensure_thumbnail(tp, max_size=256)
                texture_thumbnails.append(str(thumb.resolve()))

    style_references = []
    for ref in infer_style_references(pieces_payload, brief):
        ref_path = Path(ref["path"])
        thumb = ensure_thumbnail(ref_path, max_size=900)
        ref["path"] = str(thumb.resolve())
        style_references.append(ref)

    prompt_text = build_production_plan_prompt(
        pieces_payload, texture_set, brief, geometry_hints,
        visual_elements, overview_path, texture_thumbnails,
        style_reference_paths=style_references,
        multi_scheme=args.multi_scheme,
        max_schemes=args.max_schemes,
    )

    prompt_path = out_dir / "ai_production_plan_prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    request_summary = {
        "request_id": "ai_multi_production_plan_v1" if args.multi_scheme else "ai_production_plan_v1",
        "pieces_json": str(Path(args.pieces).resolve()),
        "texture_set": str(Path(args.texture_set).resolve()),
        "brief": str(Path(args.brief).resolve()) if args.brief else "",
        "geometry_hints": str(Path(args.geometry_hints).resolve()) if args.geometry_hints else "",
        "visual_elements": str(Path(args.visual_elements).resolve()) if args.visual_elements else "",
        "piece_overview": overview_path,
        "texture_thumbnails": texture_thumbnails,
        "style_references": style_references,
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / ("ai_multi_production_plan.json" if args.multi_scheme else "ai_production_plan.json")).resolve()),
        "multi_scheme": args.multi_scheme,
        "max_schemes": args.max_schemes if args.multi_scheme else 1,
    }
    request_path = out_dir / "ai_production_plan_request.json"
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "生产规划请求摘要": str(request_path.resolve()),
        "子Agent提示词": str(prompt_path.resolve()),
        "纸样总览图": overview_path,
        "款式参考图": [ref["path"] for ref in style_references],
        "预期输出": request_summary["expected_output"],
        "多方案模式": args.multi_scheme,
        "最大方案数": args.max_schemes if args.multi_scheme else 1,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
