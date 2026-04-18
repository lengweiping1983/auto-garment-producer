#!/usr/bin/env python3
"""
将裁片部位映射、面料资产和设计简报打包为结构化审美请求，供子 Agent 做填充计划决策。

输出：
- ai_fill_plan_prompt.txt：面向子 Agent 的自然语言审美请求
- ai_fill_plan_request.json：机器可读的结构化请求摘要
"""
import argparse
import json
from pathlib import Path


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_agent_prompt(garment_map: dict, texture_set: dict, brief: dict, pieces_payload: dict, visual_elements: dict = None) -> str:
    """构造面向子 Agent 的审美决策 prompt。"""
    lines = [
        "你是一位高级服装印花艺术指导。请根据以下数据为每个裁片制定填充计划。",
        "",
        "===== 可用面料资产 =====",
    ]
    for tex in texture_set.get("textures", []):
        if tex.get("approved", False):
            lines.append(f"- {tex.get('texture_id')}: {tex.get('role', '')} — {tex.get('prompt', '面料纹理')}")
    for motif in texture_set.get("motifs", []):
        if motif.get("approved", False):
            lines.append(f"- {motif.get('motif_id')}: {motif.get('role', '')} — {motif.get('prompt', '定位图案')}")
    for solid in texture_set.get("solids", []):
        if solid.get("approved", True):
            lines.append(f"- {solid.get('solid_id')}: 纯色 {solid.get('color', '')}")

    garment_type = brief.get("garment_type", "成衣")
    lines.extend([
        "",
        "===== 设计简报 =====",
        f"服装类型: {garment_type}",
        f"审美方向: {brief.get('aesthetic_direction', '商业畅销款')}",
        f"季节: {brief.get('season', '四季')}",
        f"目标客群: {brief.get('target_customer', '大众')}",
        f"需避免: {brief.get('avoid_elements', [])}",
    ])

    palette = brief.get("palette", [])
    if palette:
        lines.append(f"色板: {palette}")

    lines.extend([
        "",
        "===== 裁片列表 =====",
        "（已按部位角色、对称组、同形组整理）",
    ])

    by_id = {p["piece_id"]: p for p in garment_map.get("pieces", [])}
    piece_lookup = {p["piece_id"]: p for p in pieces_payload.get("pieces", [])}

    for piece_id in sorted(by_id.keys()):
        gm = by_id[piece_id]
        geo = piece_lookup.get(piece_id, {})
        lines.append(
            f"- {piece_id}: "
            f"role={gm.get('garment_role')}, "
            f"zone={gm.get('zone')}, "
            f"size={geo.get('width',0)}x{geo.get('height',0)}, "
            f"area={geo.get('area',0)}, "
            f"aspect={round(geo.get('aspect',1),2)}, "
            f"symmetry={gm.get('symmetry_group','无')}, "
            f"same_shape={gm.get('same_shape_group','无')}, "
            f"direction={gm.get('direction_degrees',0)}°, "
            f"texture_direction={gm.get('texture_direction','')}, "
            f"confidence={gm.get('confidence',0)}"
        )

    # 如果提供了 visual_elements，注入元素几何特征和适配度参考
    if visual_elements:
        lines.extend([
            "",
            "===== 主题元素几何特征 =====",
            "（每个候选 motif 的精确尺寸与方向；用于指导裁片分配决策）",
        ])
        for obj in visual_elements.get("dominant_objects", []):
            geo = obj.get("geometry")
            if not geo:
                continue
            lines.append(
                f"- {obj.get('name', 'unknown')}: "
                f"原始尺寸 {geo.get('pixel_width', '?')}×{geo.get('pixel_height', '?')}px, "
                f"宽高比 {geo.get('aspect_ratio', '?')}, "
                f"方向: {geo.get('orientation', '?')}, "
                f"形态: {geo.get('form_type', '?')}, "
                f"建议用途: {obj.get('suggested_usage', '?')}"
            )

        # 计算适配度参考
        lines.extend([
            "",
            "===== 裁片-元素适配度参考 =====",
            "（后端几何引擎预计算的适配度分数；你可参考但不被强制约束）",
        ])
        # 需要 import 创建填充计划.py 的 compute_motif_fit_score
        # 但为了避免循环依赖，这里直接内联计算
        for obj in visual_elements.get("dominant_objects", []):
            geo = obj.get("geometry")
            if not geo:
                continue
            lines.append(f"- {obj.get('name', 'unknown')} 适配度:")
            for piece_id in sorted(by_id.keys()):
                gm = by_id[piece_id]
                geo_piece = piece_lookup.get(piece_id, {})
                if not geo_piece:
                    continue
                # 简化：只报告方向匹配和尺寸匹配
                piece_aspect = geo_piece.get("width", 1) / max(1, geo_piece.get("height", 1))
                motif_aspect = geo.get("aspect_ratio", 1.0)
                texture_dir = gm.get("texture_direction", "transverse")
                orientation = geo.get("orientation", "irregular")
                # 方向匹配
                dir_match = "✓" if (
                    (orientation == "vertical" and texture_dir == "longitudinal") or
                    (orientation == "horizontal" and texture_dir == "transverse") or
                    orientation in ("radial", "symmetric")
                ) else "✗"
                lines.append(
                    f"  → {piece_id} ({gm.get('garment_role')}, {geo_piece.get('width')}×{geo_piece.get('height')}, {texture_dir}): "
                    f"方向匹配{dir_match}, 宽高比 motif={round(motif_aspect,2)} vs piece={round(piece_aspect,2)}"
                )

    # 根据服装类型生成部位规则提示
    type_hints = {
        "shirt": "衬衫类：前片+后片+袖子+领+袖口，body区比例通常较大，trim较窄",
        "dress": "连衣裙：前片+后片+袖子+裙摆，可能有育克或拼接",
        "jacket": "外套：前片+后片+袖子+领+门襟，可能有口袋盖",
        "coat": "大衣：前片+后片+袖子+领+下摆，trim较宽",
        "pants": "裤装：前片+后片+腰头+裤脚，无袖子",
        "children": "童装：部位较小，比例更紧凑，hero位置通常在胸口",
    }
    type_hint = type_hints.get(garment_type.lower(), f"{garment_type}类服装，请根据部位形状和上下文推断")

    lines.extend([
        "",
        "===== 服装类型指导 =====",
        f"本次服装类型: {garment_type}",
        f"类型特征: {type_hint}",
        "注意：不同服装类型的部位集合和比例规则不同。例如裤装没有袖子，童装部位较小。",
        "",
        "===== 硬性约束（不可违反） =====",
        "1. 同 symmetry_group 或 same_shape_group 的裁片，base 层必须使用完全相同的参数（texture_id、scale、rotation、offset_x、offset_y、mirror_x、mirror_y）。",
        "2. 仅允许 1 个 hero 裁片（通常是 front_hero），base 使用 main 纹理，overlay 使用 hero_motif 居中放置。",
        "3. body zone（front_hero, back_body, secondary_body）使用低噪底纹（main 或 secondary），方向横向（transverse）。",
        "4. secondary zone（sleeve_pair, sleeve_or_side_panel, side_or_long_panel, matched_panel）使用协调纹理（secondary），方向纵向（longitudinal）。",
        "5. trim zone（trim_strip, collar, hem）使用安静纯色（quiet solid 或 dark texture），绝不使用 motif 或 accent texture。",
        "6. detail zone 使用点缀纹理（accent），但保持克制。",
        "7. 每个裁片必须提供 reason（设计理由，中文）。",
        "8. 【新增】motif 的 scale 和 rotation 必须考虑元素方向与裁片方向的匹配：竖向元素放纵向裁片时 rotation=0°，放横向裁片时 rotation=90°；横向元素反之。",
        "9. 【新增】motif 不应被裁片边界切断；如果元素尺寸明显大于裁片，应缩小 scale 或更换为更小的元素。",
        "",
        "===== 输出格式 =====",
        "请返回严格的 JSON，格式如下（不要任何解释文字，只返回 JSON）：",
        json.dumps({
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
                        "mirror_x": False,
                        "mirror_y": False,
                    },
                    "overlay": {
                        "fill_type": "motif",
                        "motif_id": "hero_motif",
                        "anchor": "center",
                        "scale": 0.72,
                        "opacity": 0.92,
                        "offset_x": 0,
                        "offset_y": -40,
                    },
                    "trim": None,
                    "texture_direction": "transverse",
                    "reason": "前片卖点区使用主底纹横向铺陈，中心定位牡丹图案",
                }
            ],
            "art_direction": {
                "strategy": "单一卖点定位，低噪身片，协调副片，安静饰边",
                "hero_piece_ids": ["piece_001"],
                "notes": [],
            }
        }, ensure_ascii=False, indent=2),
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造子 Agent 审美决策请求。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径")
    parser.add_argument("--garment-map", required=True, help="部位映射 JSON 路径")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--brief", default="", help="商业设计简报 JSON 路径（可选）")
    parser.add_argument("--visual-elements", default="", help="visual_elements.json 路径（可选，用于注入元素几何特征）")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pieces_payload = load_json(args.pieces)
    garment_map = load_json(args.garment_map)
    texture_set = load_json(args.texture_set)
    brief = load_json(args.brief) if args.brief else {"aesthetic_direction": "商业畅销款"}

    visual_elements = None
    if args.visual_elements:
        ve_path = Path(args.visual_elements)
        if ve_path.exists():
            visual_elements = load_json(str(ve_path))

    prompt = build_agent_prompt(garment_map, texture_set, brief, pieces_payload, visual_elements)
    prompt_path = out_dir / "ai_fill_plan_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    request_summary = {
        "request_id": "ai_fill_plan_request_v1",
        "piece_count": len(pieces_payload.get("pieces", [])),
        "symmetry_groups": list({p.get("symmetry_group") for p in garment_map.get("pieces", []) if p.get("symmetry_group")}),
        "same_shape_groups": list({p.get("same_shape_group") for p in garment_map.get("pieces", []) if p.get("same_shape_group")}),
        "texture_ids": [t.get("texture_id") for t in texture_set.get("textures", []) if t.get("approved", False)],
        "motif_ids": [m.get("motif_id") for m in texture_set.get("motifs", []) if m.get("approved", False)],
        "solid_ids": [s.get("solid_id") for s in texture_set.get("solids", []) if s.get("approved", True)],
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / "ai_piece_fill_plan.json").resolve()),
    }
    request_path = out_dir / "ai_fill_plan_request.json"
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "审美请求摘要": str(request_path.resolve()),
        "子Agent提示词": str(prompt_path.resolve()),
        "预期输出": request_summary["expected_output"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
