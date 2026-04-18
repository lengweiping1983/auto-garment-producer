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


def build_production_plan_prompt(
    pieces_payload: dict,
    texture_set: dict,
    brief: dict,
    geometry_hints: dict,
    visual_elements: dict | None,
    piece_overview_path: str,
    texture_thumbnail_paths: list[str],
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

    if texture_thumbnail_paths:
        lines.append("【必看 2】面料资产缩略图（必须查看后再分配）：")
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
        "请为每个裁片标注以下字段：",
        "  - garment_role: 服装部位角色。可选值（中英均可）：",
        "      front_body / back_body / sleeve_left / sleeve_right /",
        "      collar_or_upper_trim / hem_or_lower_trim / trim_strip /",
        "      side_or_long_panel / front_hero / yoke / pocket / lining /",
        "      small_detail / unknown",
        "  - zone: body / secondary / trim / detail",
        "  - symmetry_group: 左右对称组的统一标识（如 'sg_front'）。左右对称的裁片必须相同。",
        "  - same_shape_group: 形状相同但位置不同的裁片组（如 'ssg_sleeve'）。",
        "  - texture_direction: longitudinal（纵向/经向）/ transverse（横向/纬向）",
        "  - confidence: 0-1 的确信度",
        "  - needs_ai_review: true/false —— 若 confidence < 0.6 或部位模糊，标为 true",
        "",
        "===== 裁片几何信息（程序推断，仅供参考，请以图像为准）=====",
    ])

    # 使用 geometry_hints + pieces 数据
    hint_by_id = {h["piece_id"]: h for h in geometry_hints.get("pieces", [])}
    for p in sorted(pieces, key=lambda x: x.get("area", 0), reverse=True):
        pid = p["piece_id"]
        h = hint_by_id.get(pid, {})
        aspect = p.get("width", 1) / max(1, p.get("height", 1))
        orient = h.get("pattern_orientation", 0)
        orient_str = ""
        if orient == 180:
            orient_str = f", 方向=倒置(领口在下, pattern_orientation=180°, conf={h.get('orientation_confidence',0)})"
        elif orient != 0:
            orient_str = f", 方向={orient}°"
        lines.append(
            f"  {pid}: 面积={p.get('area',0)} ({h.get('area_ratio','?')} of max), "
            f"尺寸={p.get('width',0)}×{p.get('height',0)}, 长宽比={round(aspect,2)}, "
            f"中心=({round(h.get('centroid',[0,0])[0],0)},{round(h.get('centroid',[0,0])[1],0)}), "
            f"程序推测={h.get('geometry_role_hint','unknown')}{orient_str}"
        )

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

    for tex in texture_set.get("textures", []):
        if tex.get("approved", False) or tex.get("candidate", False):
            lines.append(f"  [texture] {tex.get('texture_id')}: {tex.get('role','')} — {tex.get('prompt','')}")
    for motif in texture_set.get("motifs", []):
        if motif.get("approved", False) or motif.get("candidate", False):
            lines.append(f"  [motif]   {motif.get('motif_id')}: {motif.get('role','')} — {motif.get('prompt','')}")
    for solid in texture_set.get("solids", []):
        lines.append(f"  [solid]   {solid.get('solid_id')}: {solid.get('color','')}")

    lines.extend([
        "",
        "--- 填充规则（硬性约束，不可违反）---",
        "  1. symmetry_group / same_shape_group 内所有裁片的 base 层必须完全相同（fill_type, texture_id, scale, rotation, offset, mirror）。",
        "  2. 仅允许 1 个 hero（overlay.motif）。trim 禁用 motif overlay。",
        "  3. trim 区域（zone=trim）的 base 应为 quiet solid 或 subtle dark texture，不使用复杂图案。",
        "  4. 每个裁片必须提供 reason（中文，解释为什么这样填）。",
        "  5. 纹理方向由你根据面料图案方向和裁片形状自主决定，不要硬套规则。",
        "  6. 可声明 'intentional_asymmetry': true 保留有意不对称设计。",
        "",
        "--- 审美原则 ---",
        "  - 商业畅销款打样：可穿性优先，大身低噪，hero 醒目但不突兀。",
        "  - 避免将叙事插画直接切割到裁片中。",
        "  - 优秀设计 = 1个卖点 + 安静支持性纹理 + 克制的饰边。",
        "  - 大身裁片低对比度，在零售距离下依然可穿。",
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

    lines.extend([
        "===== 输出格式 =====",
        "请返回严格的 JSON，格式如下（不要任何解释文字、不要 markdown 代码块，只返回纯 JSON）：",
        "",
        json.dumps({
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
                    "hero_piece_ids": ["piece_001"],
                    "notes": [],
                    "self_assessment": {
                        "overall_score": 8.5,
                        "wearability": 9,
                        "cohesion": 8,
                        "hero_clarity": 9,
                        "trim_quality": 8,
                        "season_fit": 8,
                        "customer_match": 8,
                        "production_safety": 9,
                        "color_balance": 8,
                        "negative_space": 9,
                        "narrative_control": 9
                    }
                }
            },
            "risk_notes": []
        }, ensure_ascii=False, indent=2),
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
        for cand in ["piece_overview.png", "piece_overview.jpg", "garment_map_overview.jpg"]:
            cp = pieces_dir / cand
            if cp.exists():
                overview_path = str(cp.resolve())
                break

    # 收集面料资产缩略图路径
    texture_thumbnails = []
    base_dir = Path(args.texture_set).parent
    for tex in texture_set.get("textures", []) + texture_set.get("motifs", []):
        p = tex.get("path", "")
        if p:
            tp = Path(p) if Path(p).is_absolute() else base_dir / p
            if tp.exists():
                texture_thumbnails.append(str(tp.resolve()))

    prompt_text = build_production_plan_prompt(
        pieces_payload, texture_set, brief, geometry_hints,
        visual_elements, overview_path, texture_thumbnails,
    )

    prompt_path = out_dir / "ai_production_plan_prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    request_summary = {
        "request_id": "ai_production_plan_v1",
        "pieces_json": str(Path(args.pieces).resolve()),
        "texture_set": str(Path(args.texture_set).resolve()),
        "brief": str(Path(args.brief).resolve()) if args.brief else "",
        "geometry_hints": str(Path(args.geometry_hints).resolve()) if args.geometry_hints else "",
        "visual_elements": str(Path(args.visual_elements).resolve()) if args.visual_elements else "",
        "piece_overview": overview_path,
        "texture_thumbnails": texture_thumbnails,
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / "ai_production_plan.json").resolve()),
    }
    request_path = out_dir / "ai_production_plan_request.json"
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "生产规划请求摘要": str(request_path.resolve()),
        "子Agent提示词": str(prompt_path.resolve()),
        "纸样总览图": overview_path,
        "预期输出": request_summary["expected_output"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
