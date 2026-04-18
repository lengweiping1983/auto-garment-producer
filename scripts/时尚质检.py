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


def check_motif_cut(piece_id: str, rendered_path: Path, fill_plan_entry: dict) -> dict | None:
    """检查 motif 是否被裁片边界切断。

    简化方法：检查渲染后裁片中非透明区域的连通域。
    最大连通域如果接触边界 > 10% 周长，判定为 motif 可能被切断。
    """
    if not rendered_path.exists():
        return None
    overlay = fill_plan_entry.get("overlay")
    if not overlay or overlay.get("fill_type") != "motif":
        return None

    with Image.open(rendered_path).convert("RGBA") as img:
        alpha = img.getchannel("A")
        # 找到 alpha > 128 的像素（非透明）
        pixels = list(alpha.get_flattened_data())
        w, h = alpha.size
        # 简单的 flood-fill 找最大连通域
        visited = set()
        max_component = []
        for start_y in range(h):
            for start_x in range(w):
                idx = start_y * w + start_x
                if pixels[idx] <= 128 or (start_x, start_y) in visited:
                    continue
                # BFS
                queue = [(start_x, start_y)]
                visited.add((start_x, start_y))
                comp = []
                while queue:
                    x, y = queue.pop(0)
                    comp.append((x, y))
                    for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < w and 0 <= ny < h:
                            nidx = ny * w + nx
                            if pixels[nidx] > 128 and (nx, ny) not in visited:
                                visited.add((nx, ny))
                                queue.append((nx, ny))
                if len(comp) > len(max_component):
                    max_component = comp

        if not max_component:
            return None

        # 检查最大连通域是否接触边界
        border_pixels = sum(1 for x, y in max_component if x == 0 or x == w - 1 or y == 0 or y == h - 1)
        border_ratio = border_pixels / max(1, len(max_component))
        if border_ratio > 0.15:  # >15% 的像素在边界上，可能被切断
            return {
                "type": "motif_may_be_cut",
                "piece_id": piece_id,
                "border_ratio": round(border_ratio, 3),
                "message": f"motif 可能接触裁片边界（边界像素占比 {round(border_ratio*100)}%），存在被切断风险",
            }
    return None


def check_motif_scale(piece_id: str, rendered_path: Path, piece: dict, layer: dict) -> dict | None:
    """检查 motif 在裁片中的面积占比是否合理。"""
    if not rendered_path.exists():
        return None
    overlay = layer
    if not overlay or overlay.get("fill_type") != "motif":
        return None

    with Image.open(rendered_path).convert("RGBA") as img:
        alpha = img.getchannel("A")
        pixels = list(alpha.get_flattened_data())
        non_transparent = sum(1 for p in pixels if p > 128)
        total_pixels = len(pixels)
        coverage = non_transparent / max(1, total_pixels)

    # hero_motif: 期望 25%~85%
    # accent_motif: 期望 10%~45%
    motif_id = overlay.get("motif_id", "")
    is_hero = "hero" in motif_id.lower()
    if is_hero:
        if coverage < 0.15:
            return {"type": "motif_too_small", "piece_id": piece_id, "coverage": round(coverage, 3), "message": f"hero motif 占比仅 {round(coverage*100)}%，可能过小而失去视觉冲击力"}
        if coverage > 0.9:
            return {"type": "motif_too_large", "piece_id": piece_id, "coverage": round(coverage, 3), "message": f"hero motif 占比达 {round(coverage*100)}%，可能过大而拥挤"}
    else:
        if coverage < 0.05:
            return {"type": "motif_too_small", "piece_id": piece_id, "coverage": round(coverage, 3), "message": f"accent motif 占比仅 {round(coverage*100)}%，可能难以辨识"}
        if coverage > 0.55:
            return {"type": "motif_too_large", "piece_id": piece_id, "coverage": round(coverage, 3), "message": f"accent motif 占比达 {round(coverage*100)}%，可能喧宾夺主"}
    return None


def check_motif_orientation(piece_id: str, layer: dict, fill_plan_entry: dict, visual_elements: dict) -> dict | None:
    """检查 motif 方向是否与裁片方向协调。
    
    注意：texture_direction 从 fill_plan_entry 读取，因为 pieces_payload 中不含此字段。
    """
    overlay = layer
    if not overlay or overlay.get("fill_type") != "motif":
        return None
    motif_id = overlay.get("motif_id", "")
    if not visual_elements:
        return None

    # 查找 motif 对应的 geometry
    geo = None
    for obj in visual_elements.get("dominant_objects", []):
        obj_name = obj.get("name", "")
        if obj_name in motif_id or motif_id in obj_name:
            geo = obj.get("geometry")
            break
    if not geo:
        return None

    orientation = geo.get("orientation", "irregular")
    texture_dir = fill_plan_entry.get("texture_direction", "transverse")
    rotation = overlay.get("rotation", 0)

    # 判定方向协调度
    # vertical motif + longitudinal piece：rotation 应为 0°（|rotation| < 30）
    if orientation == "vertical" and texture_dir == "longitudinal" and abs(rotation % 180) > 30:
        return {"type": "motif_orientation_mismatch", "piece_id": piece_id, "orientation": orientation, "texture_direction": texture_dir, "rotation": rotation, "message": f"竖向 motif 放在纵向裁片上，但 rotation={rotation}°，方向未对齐"}
    # vertical motif + transverse piece：rotation 应为 90°（|rotation-90| < 30 或 |rotation+90| < 30）
    if orientation == "vertical" and texture_dir == "transverse" and not (60 < abs(rotation % 180) < 120):
        return {"type": "motif_orientation_mismatch", "piece_id": piece_id, "orientation": orientation, "texture_direction": texture_dir, "rotation": rotation, "message": f"竖向 motif 放在横向裁片上，但 rotation={rotation}°，建议旋转 90° 对齐"}
    # horizontal motif + transverse piece：rotation 应为 0°
    if orientation == "horizontal" and texture_dir == "transverse" and abs(rotation % 180) > 30:
        return {"type": "motif_orientation_mismatch", "piece_id": piece_id, "orientation": orientation, "texture_direction": texture_dir, "rotation": rotation, "message": f"横向 motif 放在横向裁片上，但 rotation={rotation}°，方向未对齐"}
    # horizontal motif + longitudinal piece：rotation 应为 90°
    if orientation == "horizontal" and texture_dir == "longitudinal" and not (60 < abs(rotation % 180) < 120):
        return {"type": "motif_orientation_mismatch", "piece_id": piece_id, "orientation": orientation, "texture_direction": texture_dir, "rotation": rotation, "message": f"横向 motif 放在纵向裁片上，但 rotation={rotation}°，建议旋转 90° 对齐"}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="评估商业成衣美学与生产安全性。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--fill-plan", required=True, help="裁片填充计划 JSON 路径")
    parser.add_argument("--rendered", required=True, help="渲染输出目录，包含 pieces/*.png")
    parser.add_argument("--visual-elements", default="", help="visual_elements.json 路径（可选）")
    parser.add_argument("--out", required=True, help="质检报告输出路径")
    args = parser.parse_args()

    pieces_payload = load_json(args.pieces)
    texture_set = load_json(args.texture_set)
    fill_plan = load_json(args.fill_plan)
    rendered_dir = Path(args.rendered)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    textures, motifs, solids = approved_assets(texture_set)

    # 加载 visual_elements（如果提供）
    visual_elements = None
    if args.visual_elements:
        ve_path = Path(args.visual_elements)
        if ve_path.exists():
            visual_elements = load_json(str(ve_path))

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
            # motif 级检查
            motif_cut = check_motif_cut(piece_id, image_path, entry)
            if motif_cut:
                warnings.append(motif_cut)
            for layer in collect_layer_refs(entry):
                if layer.get("fill_type") == "motif":
                    motif_scale_issue = check_motif_scale(piece_id, image_path, piece_lookup.get(piece_id, {}), layer)
                    if motif_scale_issue:
                        warnings.append(motif_scale_issue)
                    if visual_elements:
                        motif_ori_issue = check_motif_orientation(piece_id, layer, entry, visual_elements)
                        if motif_ori_issue:
                            warnings.append(motif_ori_issue)
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
