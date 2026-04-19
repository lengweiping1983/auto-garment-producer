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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from image_utils import ensure_thumbnail
from prompt_blocks import COMMERCIAL_FILL_RULES_ZH, STRICT_JSON_ONLY_ZH
try:
    from template_loader import normalize_piece_asset_paths, relative_json_metadata_path
except Exception:
    normalize_piece_asset_paths = None
    def relative_json_metadata_path(target: str | Path, owner_json_path: str | Path) -> str:
        import os
        return os.path.relpath(Path(target).resolve(), Path(owner_json_path).resolve().parent)

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
        "label": "DDS26126XCJ01L 上装/T恤/衬衫标准裁片参考图",
        "notes": "T 恤/衬衫前后片、袖片、领/门襟/窄条在该参考图中已有成衣印花方向，可用于判断部位、对称组和饰边。",
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
    """根据模板资产或服装类型匹配款式参考图。"""
    haystack = " ".join(
        str(value)
        for value in (
            pieces_payload.get("prepared_pattern", ""),
            pieces_payload.get("overview_image", ""),
            brief.get("garment_type", ""),
        )
    ).lower()
    compact_haystack = re.sub(r"[\s\-_'/]+", "", haystack)
    refs = []
    for template_id, ref in STYLE_REFERENCE_BY_TEMPLATE.items():
        template_lower = template_id.lower()
        if template_lower in haystack or (
            template_id == "BFSK26308XCJ01L" and any(term in haystack for term in ("防晒", "sun protection"))
        ) or (
            template_id == "DDS26126XCJ01L"
            and (
                any(term in haystack for term in ("衬衫", "男士衬衫", "shirt"))
                or any(term in compact_haystack for term in ("t恤", "tshirt", "男士t恤", "tee"))
            )
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
    garment_map: dict | None,
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

    if garment_map and garment_map.get("pieces"):
        is_template_map = (
            str(garment_map.get("method", "")).startswith("template_")
            or str(garment_map.get("map_id", "")).startswith("template_")
            or bool(garment_map.get("template_id"))
        )
        if is_template_map:
            lines.extend([
                "===== 固定模板部位映射（必须遵守）=====",
                "本任务命中了内置模板库。下面的 garment_map 是模板库预生成的固定生产资料，不需要重新识别，也不要修改 piece_id、garment_role、zone、symmetry_group、same_shape_group 或 grain_direction。",
                "你的主要任务是基于该固定部位映射制定 piece_fill_plan；如果输出 JSON 中包含 garment_map，也必须与下方模板映射保持一致。",
                json.dumps(garment_map, ensure_ascii=False, indent=2),
                "",
            ])
        else:
            lines.extend([
                "===== 已有部位映射参考 =====",
                "下面是当前流程已有的 garment_map。请优先参考它制定填充计划；只有在明显不合理时才调整。",
                json.dumps(garment_map, ensure_ascii=False, indent=2),
                "",
            ])

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
        "  " + "；".join(COMMERCIAL_FILL_RULES_ZH) + "。",
        "  纹理方向自主决定；可声明 intentional_asymmetry: true 保留有意不对称。",
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
        image_assets = texture_set.get("textures", []) + texture_set.get("motifs", [])
        solid_assets = texture_set.get("solids", [])
        all_assets = image_assets + solid_assets
        def _asset_id(a):
            return a.get("texture_id", a.get("motif_id", a.get("solid_id", "")))
        has_a = any(_asset_id(a).endswith("_a") for a in all_assets)
        has_b = any(_asset_id(a).endswith("_b") for a in all_assets)
        source_count = sum([has_a, has_b])

        image_asset_count = len(image_assets)
        solid_count = len(solid_assets)
        if source_count >= 2:
            strategy_lines = [
                f"生成 {max_schemes} 套 schemes。资产池含 A/B 两源 {image_asset_count} 个图片资产 + {solid_count} 个纯色；双源各 3x3 时应视为 9+9 完整资产池，必须从完整资产池重新判断组合。",
                "方案之间要有实质差异，覆盖安全量产、强卖点、深色高级、轻量呼吸、局部点缀、年轻化/秀场感等方向。",
                "允许全A、全B、A/B混合；不用低质资产可以，但在 asset_coverage.unused_assets 说明原因。",
                "所有 asset id 必须真实存在，例如 main_a、main_b、hero_motif_1_a、quiet_solid_b。",
                "不要只输出 A/B 两个来源结果；每个 scheme 都必须是独立设计方案，并说明资产选择理由。",
            ]
        else:
            # 单源情况（1 套或 0 套有后缀都 fallback 为单源）：从 9 个资产中组合多套方案
            source_label = "源A" if has_a else "源B" if has_b else "当前可用"
            source_tag_note = "资产 ID 带 _a 后缀" if has_a else "资产 ID 带 _b 后缀" if has_b else "使用原始资产 ID（无后缀）"
            strategy_lines = [
                f"生成 {max_schemes} 套 schemes。当前为单源资产（{source_label}，{source_tag_note}）：{image_asset_count} 个图片资产 + {solid_count} 个纯色。",
                "每套都从完整资产池重新判断 base/secondary/accent/hero/trim；差异不能只靠交换小面积 trim。",
                "覆盖安全量产、强卖点、深色高级、轻量呼吸、局部点缀、年轻化/秀场感等方向。",
                "不用低质资产可以，但在 asset_coverage.unused_assets 说明原因。",
                "不要只输出单一结果；每个 scheme 都必须是独立设计方案，并说明资产选择理由。",
            ]

        lines.extend(["", "===== 多方案策略指导 ====="] + strategy_lines + [""])

    lines.extend([
        "===== 输出格式 =====",
        STRICT_JSON_ONLY_ZH + " 格式如下：",
        "",
    ])

    if multi_scheme:
        lines.append(json.dumps({
            "schemes": [
                {
                    "scheme_id": "scheme_01",
                    "design_positioning": "量产安全款 / 精品陈列款 / 年轻潮流款等",
                    "strategy_note": "从完整资产池独立判断后的组合策略",
                    "asset_mix_summary": {"body_base_assets": ["main_a"], "hero_assets": ["hero_motif_1_a"], "trim_assets": ["quiet_solid_b"], "reason": "..."},
                    "diversity_tags": ["quiet_body", "bold_hero", "accent_trim"],
                    "garment_map": {"pieces": [{"piece_id": "piece_001", "garment_role": "front_body", "zone": "body", "symmetry_group": "sg_front", "same_shape_group": "", "texture_direction": "transverse", "confidence": 0.88, "needs_ai_review": False}]},
                    "piece_fill_plan": {
                        "pieces": [
                            {
                                "piece_id": "piece_001",
                                "base": {"fill_type": "texture", "texture_id": "main_a", "scale": 1.0, "rotation": 0, "offset_x": 0, "offset_y": 0, "mirror_x": False, "mirror_y": False},
                                "overlay": {"fill_type": "motif", "motif_id": "hero_motif_1_a", "anchor": "center", "scale": 0.72, "opacity": 0.92, "offset_x": 0, "offset_y": -40},
                                "trim": None,
                                "texture_direction": "transverse",
                                "reason": "中文原因",
                                "intentional_asymmetry": False
                            }
                        ],
                        "art_direction": {"strategy": "单一卖点定位，低噪身片，协调副片，安静饰边", "hero_piece_ids": ["piece_001"]}
                    }
                }
            ],
            "portfolio_notes": "...",
            "asset_coverage": {"used_assets": ["main_a"], "unused_assets": [{"asset_id": "trim_motif_a", "reason": "..."}], "coverage_strategy": "..."},
            "risk_notes": []
        }, ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("注：顶层必须包含 schemes 数组、portfolio_notes 和 asset_coverage。每个 scheme 必须包含 scheme_id、design_positioning、strategy_note、asset_mix_summary、diversity_tags、piece_fill_plan。")
        lines.append("注：模板模式下 garment_map 可省略；若提供 garment_map，也会被固定模板映射覆盖。")
        lines.append("注：art_direction 可额外包含 notes[] 和可选的 self_assessment（overall_score/wearability/cohesion/hero_clarity/trim_quality/season_fit/customer_match/production_safety/color_balance/negative_space/narrative_control）。")
    else:
        lines.append(json.dumps({
            "garment_map": {
                "pieces": [
                    {"piece_id": "piece_001", "garment_role": "front_body", "zone": "body", "symmetry_group": "sg_front", "same_shape_group": "", "texture_direction": "transverse", "confidence": 0.88, "needs_ai_review": False}
                ]
            },
            "piece_fill_plan": {
                "pieces": [
                    {
                        "piece_id": "piece_001",
                        "base": {"fill_type": "texture", "texture_id": "main", "scale": 1.0, "rotation": 0, "offset_x": 0, "offset_y": 0, "mirror_x": False, "mirror_y": False},
                        "overlay": {"fill_type": "motif", "motif_id": "hero_motif_1", "anchor": "center", "scale": 0.72, "opacity": 0.92, "offset_x": 0, "offset_y": -40},
                        "trim": None,
                        "texture_direction": "transverse",
                        "reason": "中文原因",
                        "intentional_asymmetry": False
                    }
                ],
                "art_direction": {"strategy": "单一卖点定位，低噪身片，协调副片，安静饰边", "hero_piece_ids": ["piece_001"]}
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
    parser.add_argument("--multi-scheme", action="store_true", help="启用多方案模式。要求 AI 输出 schemes 数组，每套包含独立的 piece_fill_plan。")
    parser.add_argument("--max-schemes", type=int, default=8, help="最大方案数（默认 8；需要更丰富组合可设为 12）。")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pieces_payload = load_json(args.pieces)
    if normalize_piece_asset_paths:
        pieces_payload = normalize_piece_asset_paths(pieces_payload, args.pieces)
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

    garment_map = None
    if args.garment_map:
        try:
            garment_map = load_json(args.garment_map)
        except Exception as exc:
            print(f"[警告] 无法读取 garment_map: {exc}", file=sys.stderr)

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
        visual_elements, garment_map, overview_path, texture_thumbnails,
        style_reference_paths=style_references,
        multi_scheme=args.multi_scheme,
        max_schemes=args.max_schemes,
    )

    prompt_path = out_dir / "ai_production_plan_prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    request_path = out_dir / "ai_production_plan_request.json"
    request_summary = {
        "request_id": "ai_multi_production_plan_v1" if args.multi_scheme else "ai_production_plan_v1",
        "pieces_json": relative_json_metadata_path(args.pieces, request_path),
        "texture_set": str(Path(args.texture_set).resolve()),
        "brief": str(Path(args.brief).resolve()) if args.brief else "",
        "geometry_hints": str(Path(args.geometry_hints).resolve()) if args.geometry_hints else "",
        "visual_elements": str(Path(args.visual_elements).resolve()) if args.visual_elements else "",
        "garment_map": str(Path(args.garment_map).resolve()) if args.garment_map else "",
        "piece_overview": overview_path,
        "texture_thumbnails": texture_thumbnails,
        "style_references": style_references,
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / ("ai_multi_production_plan.json" if args.multi_scheme else "ai_production_plan.json")).resolve()),
        "multi_scheme": args.multi_scheme,
        "max_schemes": args.max_schemes if args.multi_scheme else 1,
        "expected_top_level": "schemes" if args.multi_scheme else "garment_map + piece_fill_plan",
        "scheme_required_fields": ["scheme_id", "design_positioning", "strategy_note", "asset_mix_summary", "diversity_tags", "piece_fill_plan"] if args.multi_scheme else [],
    }
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
