#!/usr/bin/env python3
"""
评估渲染后成衣的商业美学与生产安全性。
"""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageStat


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def collect_layer_refs(plan_entry: dict) -> list[dict]:
    if "base" in plan_entry or "overlay" in plan_entry or "trim" in plan_entry:
        return [layer for layer in (plan_entry.get("base"), plan_entry.get("overlay"), plan_entry.get("trim")) if isinstance(layer, dict)]
    return [plan_entry]


def approved_assets(texture_set: dict) -> tuple[set[str], set[str], set[str]]:
    textures = {item.get("texture_id") or item.get("role") for item in texture_set.get("textures", []) if item.get("approved", False)}
    motifs = {item.get("motif_id") or item.get("role") for item in texture_set.get("motifs", []) if item.get("approved", False)}
    solids = {item.get("solid_id") for item in texture_set.get("solids", []) if item.get("approved", True)}
    return textures, motifs, solids


def image_variation(path: Path) -> float:
    """计算渲染后裁片的视觉变化度。"""
    with Image.open(path).convert("RGBA") as img:
        alpha = img.getchannel("A")
        bbox = alpha.getbbox()
        if not bbox:
            return 0.0
        rgb = img.crop(bbox).convert("RGB").resize((128, 128), Image.Resampling.LANCZOS)
        stat = ImageStat.Stat(rgb)
        return round(sum(stat.stddev) / 3, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description="评估商业成衣美学与生产安全性。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--fill-plan", required=True, help="裁片填充计划 JSON 路径")
    parser.add_argument("--rendered", required=True, help="渲染输出目录，包含 pieces/*.png")
    parser.add_argument("--out", required=True, help="质检报告输出路径")
    args = parser.parse_args()

    pieces_payload = load_json(args.pieces)
    texture_set = load_json(args.texture_set)
    fill_plan = load_json(args.fill_plan)
    rendered_dir = Path(args.rendered)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    textures, motifs, solids = approved_assets(texture_set)

    issues = []
    warnings = []
    hero_entries = []
    texture_usage: dict[str, int] = {}
    missing_reasons = []
    busy_trim = []
    large_solid_pieces = []
    unapproved_refs = []
    alpha_failures = []
    variation_by_piece = {}
    piece_lookup = {piece["piece_id"]: piece for piece in pieces_payload.get("pieces", [])}
    largest_area = max((piece.get("area", 0) for piece in pieces_payload.get("pieces", [])), default=1)

    # 同组一致性检查
    group_base_layers: dict[str, dict] = {}
    for entry in fill_plan.get("pieces", []):
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if not group:
            continue
        base = entry.get("base")
        if not isinstance(base, dict):
            continue
        if group not in group_base_layers:
            group_base_layers[group] = {
                "ref": base,
                "pieces": [entry["piece_id"]],
            }
        else:
            ref = group_base_layers[group]["ref"]
            mismatches = []
            for key in ("texture_id", "scale", "rotation", "offset_x", "offset_y", "mirror_x", "mirror_y"):
                if base.get(key) != ref.get(key):
                    mismatches.append(key)
            if mismatches:
                issues.append({
                    "type": "group_layer_mismatch",
                    "piece_id": entry["piece_id"],
                    "group": group,
                    "mismatched_keys": mismatches,
                    "message": f"同组裁片 base 层参数不一致: {mismatches}",
                })
            group_base_layers[group]["pieces"].append(entry["piece_id"])

    for entry in fill_plan.get("pieces", []):
        piece_id = entry.get("piece_id", "")
        role = entry.get("garment_role", "")
        zone = entry.get("zone", "")
        if "hero" in role or (entry.get("overlay") or {}).get("fill_type") == "motif":
            hero_entries.append(piece_id)
        if not entry.get("reason") and not any(layer.get("reason") for layer in collect_layer_refs(entry)):
            missing_reasons.append(piece_id)
        for layer in collect_layer_refs(entry):
            fill_type = layer.get("fill_type")
            if fill_type == "texture":
                texture_id = layer.get("texture_id")
                texture_usage[texture_id] = texture_usage.get(texture_id, 0) + 1
                if texture_id not in textures:
                    unapproved_refs.append({"piece_id": piece_id, "type": "texture", "id": texture_id})
            elif fill_type == "motif":
                motif_id = layer.get("motif_id")
                if motif_id not in motifs:
                    unapproved_refs.append({"piece_id": piece_id, "type": "motif", "id": motif_id})
                if zone == "trim" or "trim" in role or "cuff" in role:
                    busy_trim.append(piece_id)
            elif fill_type == "solid":
                solid_id = layer.get("solid_id")
                if solid_id not in solids:
                    unapproved_refs.append({"piece_id": piece_id, "type": "solid", "id": solid_id})
                if (piece_lookup.get(piece_id, {}).get("area", 0) / max(1, largest_area)) >= 0.12:
                    large_solid_pieces.append(piece_id)

        image_path = rendered_dir / "pieces" / f"{piece_id}.png"
        if image_path.exists():
            with Image.open(image_path).convert("RGBA") as img:
                alpha = img.getchannel("A")
                if alpha.getextrema()[0] == 255:
                    alpha_failures.append(piece_id)
            variation_by_piece[piece_id] = image_variation(image_path)
        else:
            issues.append({"type": "missing_rendered_piece", "piece_id": piece_id, "message": f"缺失渲染裁片: {image_path}"})

    if len(hero_entries) == 0:
        warnings.append({"type": "missing_hero_piece", "message": "未找到卖点裁片或图案定位。"})
    if len(hero_entries) > 2:
        issues.append({"type": "too_many_hero_pieces", "message": f"卖点裁片过多（{len(hero_entries)} 个）: {hero_entries}"})
    if len(texture_usage) <= 1 and len(fill_plan.get("pieces", [])) > 3:
        warnings.append({"type": "flat_texture_hierarchy", "message": "大部分裁片使用相同面料族；输出可能感觉均匀填充。"})
    if missing_reasons:
        issues.append({"type": "missing_plan_reasons", "piece_ids": missing_reasons})
    if busy_trim:
        issues.append({"type": "busy_trim_motif", "piece_ids": busy_trim, "message": "饰边/窄条裁片使用了复杂图案。"})
    if unapproved_refs:
        issues.append({"type": "unapproved_asset_reference", "refs": unapproved_refs})
    if alpha_failures:
        issues.append({"type": "missing_transparency", "piece_ids": alpha_failures, "message": "渲染后 PNG 缺少透明通道。"})
    if large_solid_pieces:
        issues.append({"type": "large_piece_uses_flat_solid", "piece_ids": large_solid_pieces, "message": "大面板需要协调纹理或工程化图案，而非不匹配的纯色块。"})

    # Hero 裁片 overlay 透明度检查（检查原始 motif 资产，而非渲染后的合成裁片）
    for entry in fill_plan.get("pieces", []):
        piece_id = entry.get("piece_id", "")
        if piece_id not in hero_entries:
            continue
        overlay = entry.get("overlay")
        if not overlay or overlay.get("fill_type") != "motif":
            issues.append({"type": "hero_missing_motif_overlay", "piece_id": piece_id, "message": "Hero 裁片缺少 motif overlay 层。"})
            continue
        motif_id = overlay.get("motif_id", "")
        motif_path = None
        for m in texture_set.get("motifs", []):
            if m.get("motif_id") == motif_id or m.get("texture_id") == motif_id:
                motif_path = Path(m.get("path", ""))
                break
        if motif_path and motif_path.exists():
            with Image.open(motif_path).convert("RGBA") as img:
                alpha = img.getchannel("A")
                # 检查 motif 中心区域是否有半透明像素（motif 应有透明背景）
                cx, cy = img.width // 2, img.height // 2
                center_region = alpha.crop((cx - cx // 3, cy - cy // 3, cx + cx // 3, cy + cy // 3))
                if center_region.getextrema()[0] == 255:
                    issues.append({"type": "hero_overlay_not_transparent", "piece_id": piece_id, "message": "Hero motif 中心区域完全不透明，背景可能未正确去除。"})

    # 方向合理性检查
    for entry in fill_plan.get("pieces", []):
        piece_id = entry.get("piece_id", "")
        role = entry.get("garment_role", "")
        base = entry.get("base")
        if not base:
            continue
        rotation = abs(float(base.get("rotation", 0) or 0)) % 360
        if rotation > 180:
            rotation = 360 - rotation
        is_trim = entry.get("zone") == "trim" or "trim" in role
        if is_trim and rotation > 30:
            warnings.append({"type": "trim_rotation_unusual", "piece_id": piece_id, "rotation": rotation, "message": "饰边裁片纹理旋转角度较大，可能影响生产对齐。"})

    # 标记饰边裁片变化度过高作为商业可穿性风险
    for entry in fill_plan.get("pieces", []):
        piece_id = entry.get("piece_id", "")
        piece = piece_lookup.get(piece_id, {})
        aspect = piece.get("width", 1) / max(1, piece.get("height", 1))
        is_trim = entry.get("zone") == "trim" or "trim" in entry.get("garment_role", "") or aspect >= 3 or aspect <= 0.34
        if is_trim and variation_by_piece.get(piece_id, 0) > 42:
            warnings.append({"type": "trim_may_be_too_busy", "piece_id": piece_id, "variation": variation_by_piece[piece_id], "message": "饰边裁片可能过于繁忙。"})

    approved = not issues
    report = {
        "approved": approved,
        "summary": {
            "pieces": len(fill_plan.get("pieces", [])),
            "hero_piece_count": len(hero_entries),
            "texture_usage": texture_usage,
            "issues": len(issues),
            "warnings": len(warnings),
        },
        "issues": issues,
        "warnings": warnings,
        "variation_by_piece": variation_by_piece,
        "group_consistency": {k: v["pieces"] for k, v in group_base_layers.items()},
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 质检报告生成成功即返回 0；issues/warnings 供人工/子 Agent 审阅，不阻塞流程
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
