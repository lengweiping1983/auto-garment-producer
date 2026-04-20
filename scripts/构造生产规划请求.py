#!/usr/bin/env python3
"""
构造生产规划 AI 请求。

输出：
- ai_production_plan_prompt.txt：生产规划请求
- ai_production_plan_request.json：结构化请求摘要

预期输出：ai_production_plan.json，包含 garment_map + piece_fill_plan。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from image_utils import ensure_thumbnail, estimate_payload_budget, print_payload_budget_warning
from prompt_blocks import COMMERCIAL_FILL_RULES_ZH, STRICT_JSON_ONLY_ZH
try:
    from template_loader import normalize_piece_asset_paths, relative_json_metadata_path, template_kimi_preview_for_pieces
except Exception:
    normalize_piece_asset_paths = None
    def relative_json_metadata_path(target: str | Path, owner_json_path: str | Path) -> str:
        import os
        return os.path.relpath(Path(target).resolve(), Path(owner_json_path).resolve().parent)
    def template_kimi_preview_for_pieces(pieces_json_path, image_kind="piece_overview"):
        return ""

SKILL_DIR = Path(__file__).resolve().parents[1]


def load_json(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text = text.replace("False", "false").replace("True", "true")
        return json.loads(text)


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
    garment_map: dict | None,
    piece_overview_path: str,
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

    theme_strategy = brief.get("theme_to_piece_strategy", {})
    if theme_strategy:
        lines.extend([
            "",
            "===== 主题落地策略（必须执行）=====",
            f"base_atmosphere: {theme_strategy.get('base_atmosphere', '')}",
            f"hero_motif: {theme_strategy.get('hero_motif', '')}",
            f"accent_details: {theme_strategy.get('accent_details', '')}",
            f"quiet_zones: {theme_strategy.get('quiet_zones', '')}",
            f"不得作为大身满版纹理的具象元素: {theme_strategy.get('do_not_use_as_full_body_texture', [])}",
            "你必须在每个方案的 strategy_note 或 art_direction.notes 中说明：哪个裁片承载 hero，哪些裁片只承载主题氛围，哪些裁片保持安静。",
        ])

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
        "请为每个裁片标注：garment_role, zone, symmetry_group, same_shape_group, texture_direction, confidence",
        "可选 garment_role: front_body / back_body / sleeve_left / sleeve_right / collar_or_upper_trim / hem_or_lower_trim / trim_strip / side_or_long_panel / front_hero / yoke / pocket / lining / small_detail / unknown",
        "zone: body / secondary / trim / detail",
        "",
        "===== 裁片几何信息（JSON 摘要，程序推断仅供参考，请以纸样总览缩略图为准）=====",
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
        "基于 Step 1 的部位识别结果制定裁片结构、分层和主图 overlay 计划。默认主题图流程会生成 main、secondary、accent_light 三套单纹理变体，不需要你挑选具体纹理。",
        "",
        "--- 可用面料资产 ---",
    ])

    # 面料资产列表：只提供 ID + role，AI 不需要查看纹理缩略图或选择纹理。
    asset_lines = []
    for tex in texture_set.get("textures", []):
        if tex.get("approved", False) or tex.get("candidate", False):
            asset_lines.append(f"{tex.get('texture_id')}:{tex.get('role','')}")
    for motif in texture_set.get("motifs", []):
        if motif.get("approved", False) or motif.get("candidate", False):
            asset_lines.append(f"{motif.get('motif_id')}:{motif.get('role','')}")
    for solid in texture_set.get("solids", []):
        asset_lines.append(f"{solid.get('solid_id')}:{solid.get('color','')}")
    if asset_lines:
        lines.append("  " + ", ".join(asset_lines))
    else:
        lines.append("  （无可用资产）")

    lines.extend([
        "",
        "--- 填充规则 ---",
        "  " + "；".join(COMMERCIAL_FILL_RULES_ZH) + "。",
        "  不要在不同纹理之间做择优选择；程序会对每个 approved 单纹理分别生成一套完整裁片。",
        "  蘑菇、动物、角色、花丛、完整场景等具象元素不得作为大面积满版 body texture，除非用户明确要求；它们只能作为 1 个 hero motif 或小面积 accent。",
        "  禁止半透明整张主题图贴片；motif overlay 必须是干净定位图案，只允许用于 1 个 hero 裁片。",
        "  纹理方向自主决定；可声明 intentional_asymmetry: true 保留有意不对称。",
        "",
        "--- 成衣视角四问（写 piece_fill_plan 之前必须回答）---",
        "  Q1 整体印象：这件衣服第一眼让人记住的具体视觉锚是什么？不超过 12 个字，不能写抽象词。",
        "  Q2 三米测试：距离 3 米看，哪些纹理会糊、乱、花？大身 base 必须塌缩为受控色相。",
        "  Q3 挂架理由：和同价位 20 件上衣挂在一起，消费者为什么拿起它？",
        "  Q4 日常舒适：开会、吃饭、接孩子是否用力过猛？大身满版插画必须降级为低噪 base。",
        "  可在 art_direction.notes 中简要说明这些判断如何影响裁片层级和主图位置。",
        "",
        "--- Hero Motif 几何契约（硬约束）---",
        "  hero 只允许在 front_body/front_hero；宽度占前片 18%–42%，高度 18%–38%。",
        "  hero 顶端距上沿 >=12%，底端距下沿 >=22%，距任意裁片边 >=8%，旋转限制 -8° 到 +8°。",
        "  motif 与底色明度差 ΔL* >=25；不满足时远看会糊，必须换 motif 或放弃 hero。",
        "  piece_fill_plan 中每个 hero overlay 的 scale/anchor/offset 必须能换算成目标裁片 bounding box 内 18%–42% 宽、18%–38% 高；否则视为越界。",
        "  禁止半透明整张主题图 overlay、禁止跨缝线/领口/袖窿/肩缝、禁止同一件衣服出现 2 个以上 hero。",
        "  禁止完整场景叙事做 hero；只能提取 1 个角色、剪影或简化图标。",
        "",
        "--- 模板 zone 规则 ---",
        "  若固定模板 garment_map 中提供 zone/garment_role/symmetry_group，必须直接按这些结构化生产事实执行。",
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
        "===== 输出顺序硬约束 =====",
        "你必须先完成 Part 1，再写 Part 2。不要在确认 garment_map 和成衣自检之前生成 piece_fill_plan。",
        "Part 1: 输出 garment_map、garment_map_confidence_per_piece、garment_map_uncertainties。",
        "Part 2: 输出 asset_shortlist、theme_landing_summary、asset_mix_summary、piece_fill_plan。",
        "写 piece_fill_plan 时，body/base 资产必须来自 asset_shortlist.primary_base_ids。",
        "",
        "===== 输出格式 =====",
        STRICT_JSON_ONLY_ZH + " 格式如下：",
        "",
    ])

    lines.append(json.dumps({
            "garment_map": {
                "pieces": [
                    {"piece_id": "piece_001", "garment_role": "front_body", "zone": "body", "symmetry_group": "sg_front", "same_shape_group": "", "texture_direction": "transverse", "confidence": 0.88}
                ]
            },
            "garment_map_confidence_per_piece": {"piece_001": 0.88},
            "garment_map_uncertainties": [
                {"piece_id": "piece_004", "reason": "窄长裁片角色不确定，按 trim 安静处理"}
            ],
            "asset_shortlist": {
                "primary_base_ids": ["main"],
                "rejected_assets": [{"asset_id": "busy_scene", "reason": "完整叙事插画，不适合大身"}]
            },
            "piece_fill_plan": {
                "pieces": [
                    {
                        "piece_id": "piece_001",
                        "base": {"fill_type": "texture", "texture_id": "main", "scale": 1.0, "rotation": 0, "offset_x": 0, "offset_y": 0, "mirror_x": False, "mirror_y": False},
                        "overlay": {"fill_type": "motif", "motif_id": "hero_motif_1", "anchor": "center", "scale": 0.72, "opacity": 1.0, "offset_x": 0, "offset_y": -40},
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
    lines.append("注：art_direction 可额外包含 notes[]，用于解释核心视觉策略。")

    lines.extend([
        "",
        "===== 重要声明 =====",
        "以上所有程序推断（裁片几何、部位候选、面料描述）均为参考建议，不是事实。",
        "你必须结合纸样总览缩略图确认每个裁片的部位和填充结构；不要要求查看纹理拼图，也不要把某一个纹理作为唯一结果。",
    ])

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造生产规划 AI 请求。")
    parser.add_argument("--pieces", required=True, help="pieces.json 路径")
    parser.add_argument("--texture-set", required=True, help="texture_set.json 路径")
    parser.add_argument("--brief", default="", help="commercial_design_brief.json 路径")
    parser.add_argument("--geometry-hints", default="", help="geometry_hints.json 路径")
    parser.add_argument("--visual-elements", default="", help="visual_elements.json 路径（可选，用于 motif 几何信息）")
    parser.add_argument("--garment-map", required=True, help="固定模板 garment_map.json 路径")
    parser.add_argument("--piece-overview", default="", help="piece_overview.png 路径。若省略，尝试从 pieces.json 所在目录推断。")
    parser.add_argument("--out", required=True, help="输出目录")
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

    garment_map = load_json(args.garment_map)

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

    overview_for_kimi = overview_path
    overview_preprocessed = False
    if overview_path:
        preprocessed_overview = template_kimi_preview_for_pieces(args.pieces, "piece_overview")
        if preprocessed_overview:
            overview_for_kimi = preprocessed_overview
            overview_preprocessed = True
        else:
            overview_for_kimi = str(ensure_thumbnail(overview_path, max_size=512, provider="kimi").resolve())

    prompt_text = build_production_plan_prompt(
        pieces_payload, texture_set, brief, geometry_hints,
        visual_elements, garment_map, overview_for_kimi,
    )

    prompt_path = out_dir / "ai_production_plan_prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    kimi_images = [overview_for_kimi] if overview_for_kimi else []
    payload_budget = estimate_payload_budget(prompt_path, kimi_images)

    request_path = out_dir / "ai_production_plan_request.json"
    request_summary = {
        "request_id": "ai_production_plan_v1",
        "pieces_json": relative_json_metadata_path(args.pieces, request_path),
        "texture_set": str(Path(args.texture_set).resolve()),
        "brief": str(Path(args.brief).resolve()) if args.brief else "",
        "geometry_hints": str(Path(args.geometry_hints).resolve()) if args.geometry_hints else "",
        "visual_elements": str(Path(args.visual_elements).resolve()) if args.visual_elements else "",
        "garment_map": str(Path(args.garment_map).resolve()),
        "piece_overview": overview_for_kimi,
        "piece_overview_original": overview_path,
        "piece_overview_preprocessed": overview_preprocessed,
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / "ai_production_plan.json").resolve()),
        "expected_top_level": "garment_map + piece_fill_plan",
        "payload_budget": payload_budget,
        "kimi_images": kimi_images,
        "kimi_input_note": "只传 piece_overview 的 Kimi 压缩图；不生成或传入纹理拼图，纹理变体由程序逐一渲染。",
    }
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "生产规划请求摘要": str(request_path.resolve()),
        "AI生产规划提示词": str(prompt_path.resolve()),
        "纸样总览图": overview_path,
        "Kimi纸样总览图": overview_for_kimi,
        "预期输出": request_summary["expected_output"],
        "Kimi请求体预算": payload_budget,
    }, ensure_ascii=False, indent=2))
    print_payload_budget_warning(payload_budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
