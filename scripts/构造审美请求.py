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

import sys
sys.path.insert(0, str(Path(__file__).parent))
from image_utils import ensure_thumbnail, estimate_payload_budget, print_payload_budget_warning


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_agent_prompt(garment_map: dict, texture_set: dict, brief: dict, pieces_payload: dict, visual_elements: dict = None) -> str:
    """构造面向子 Agent 的审美决策 prompt。"""
    lines = [
        "你是一位高级服装印花艺术指导。请根据以下数据为每个裁片制定填充计划。",
        "",
        "===== 可用面料资产（查看缩略图后再分配）====="
    ]
    # 压缩面料资产列表：只保留 ID + role，省略 prompt
    asset_lines = []
    for tex in texture_set.get("textures", []):
        if tex.get("approved", False):
            asset_lines.append(f"{tex.get('texture_id')}:{tex.get('role', '')}")
    for motif in texture_set.get("motifs", []):
        if motif.get("approved", False):
            asset_lines.append(f"{motif.get('motif_id')}:{motif.get('role', '')}")
    for solid in texture_set.get("solids", []):
        if solid.get("approved", True):
            asset_lines.append(f"{solid.get('solid_id')}:{solid.get('color', '')}")
    if asset_lines:
        lines.append("  " + ", ".join(asset_lines))
    else:
        lines.append("  （无可用资产）")

    # 显式列出相关图片路径，供子Agent使用 see_image 查看（使用缩略图避免 413）
    image_paths = []
    for tex in texture_set.get("textures", []):
        p = tex.get("path", "")
        if p:
            image_paths.append(str(ensure_thumbnail(p, max_size=192, provider="kimi")))
    for motif in texture_set.get("motifs", []):
        p = motif.get("path", "")
        if p:
            image_paths.append(str(ensure_thumbnail(p, max_size=192, provider="kimi")))
    # 尝试推断 overview 图路径
    if pieces_payload:
        pieces_path = pieces_payload.get("_source_path", "")
        if pieces_path:
            base = Path(pieces_path).parent
            for cand in ["piece_overview.png", "garment_map_overview.jpg"]:
                cp = base / cand
                if cp.exists():
                    image_paths.append(str(ensure_thumbnail(cp, max_size=512, provider="kimi")))
    if image_paths:
        lines.extend([
            "",
            "===== 相关图片路径（请使用 see_image 查看后再做决策）=====",
            "以下图片是本任务的视觉参考，你必须先查看它们，再结合文本数据做出审美判断：",
        ])
        for ip in image_paths:
            lines.append(f"- {ip}")

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

    # 计算面积排名
    by_id = {p["piece_id"]: p for p in garment_map.get("pieces", [])}
    piece_lookup = {p["piece_id"]: p for p in pieces_payload.get("pieces", [])}
    all_areas = sorted(
        [(pid, piece_lookup.get(pid, {}).get("area", 0)) for pid in by_id.keys()],
        key=lambda x: x[1], reverse=True
    )
    area_rank = {pid: idx + 1 for idx, (pid, _) in enumerate(all_areas)}

    lines.extend([
        "",
        "===== 裁片列表（程序推断仅供参考，请以图片为准）====="
    ])

    # 压缩裁片信息：JSON 摘要形式，只保留关键字段
    compact_pieces = []
    for piece_id in sorted(by_id.keys()):
        gm = by_id[piece_id]
        geo = piece_lookup.get(piece_id, {})
        review_flag = "[审]" if gm.get("needs_ai_review") else ""
        rank = area_rank.get(piece_id, 0)
        compact_pieces.append(
            f"{piece_id}: 排名#{rank}, role={gm.get('garment_role')}, zone={gm.get('zone')}, "
            f"size={geo.get('width',0)}x{geo.get('height',0)}, "
            f"symmetry={gm.get('symmetry_group','无')}, "
            f"direction={gm.get('direction_degrees',0)}°, conf={gm.get('confidence',0)}{review_flag}"
        )
    lines.append("  " + "; ".join(compact_pieces))

    # 如果提供了 visual_elements，注入元素几何特征（精简版，避免 prompt 膨胀）
    if visual_elements:
        lines.extend([
            "",
            "===== 主题元素几何特征（供 motif 放置参考） =====",
        ])
        for obj in visual_elements.get("dominant_objects", []):
            geo = obj.get("geometry")
            if not geo:
                continue
            lines.append(
                f"- {obj.get('name', 'unknown')}: "
                f"{geo.get('pixel_width', '?')}×{geo.get('pixel_height', '?')}px, "
                f"方向={geo.get('orientation', '?')}, 建议用途={obj.get('suggested_usage', '?')}"
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
        "1. 同 symmetry_group / same_shape_group 的裁片，base 层参数必须完全相同。",
        "2. 仅允许 1 个 hero 裁片；base 用 main 纹理，overlay 用 hero_motif 居中。",
        "3. body zone 用低噪底纹（main/secondary）；secondary zone 用协调纹理；trim zone 用 dark/quiet solid（禁用 motif overlay）；detail zone 用 accent 点缀。",
        "4. 每个裁片必须提供 reason（中文）。",
        "5. motif 方向需与裁片 texture_direction 匹配；竖向元素放纵向裁片 rotation=0°，放横向裁片 rotation=90°。",
        "6. motif 不应被裁片边界切断；元素过大时应缩小 scale。",
        "7. 左右裁片如需不对称设计，声明 `\"intentional_asymmetry\": true`。",
        "8. 大身裁片使用 solid 必须提供明确设计理由，否则程序会返工。"
        "",
        "===== 纹理方向决策 =====",
        "【强制】先查看所有 texture/motif 缩略图，判断图案方向语义，再决定每个裁片的 texture_direction。",
        "覆盖规则：有方向纹理（竖条纹/定向花朵/斜纹）让裁片方向与纹理一致；条纹衬衫前片常用竖纹；苏格兰格必须正向；同 symmetry_group 方向必须一致；覆盖默认值请在 reason 中说明。",
        "",
        "===== 经向约束 =====",
        "body 区通常为 vertical；has_nap=true 时所有裁片 base.rotation 必须同向。"
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
    # prompt 内已列出 Kimi 缩略图路径，这里解析出来用于预算诊断。
    kimi_images = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and (".jpg" in stripped.lower() or ".jpeg" in stripped.lower() or ".png" in stripped.lower()):
            kimi_images.append(stripped[2:].strip())
    payload_budget = estimate_payload_budget(prompt_path, kimi_images)

    request_summary = {
        "request_id": "ai_fill_plan_request_v1",
        "piece_count": len(pieces_payload.get("pieces", [])),
        "symmetry_groups": list({p.get("symmetry_group") for p in garment_map.get("pieces", []) if p.get("symmetry_group")}),
        "same_shape_groups": list({p.get("same_shape_group") for p in garment_map.get("pieces", []) if p.get("same_shape_group")}),
        "texture_ids": [t.get("texture_id") for t in texture_set.get("textures", []) if t.get("approved", False)],
        "motif_ids": [m.get("motif_id") for m in texture_set.get("motifs", []) if m.get("approved", False)],
        "solid_ids": [s.get("solid_id") for s in texture_set.get("solids", []) if s.get("approved", True)],
        "kimi_images": kimi_images,
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / "ai_piece_fill_plan.json").resolve()),
        "payload_budget": payload_budget,
        "kimi_input_note": "只传 kimi_images 中的缩略图；不要传原始 texture/motif/piece_overview 图片。",
    }
    request_path = out_dir / "ai_fill_plan_request.json"
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "审美请求摘要": str(request_path.resolve()),
        "子Agent提示词": str(prompt_path.resolve()),
        "预期输出": request_summary["expected_output"],
        "Kimi请求体预算": payload_budget,
    }, ensure_ascii=False))
    print_payload_budget_warning(payload_budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
