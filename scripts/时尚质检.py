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


def base_texture_direction(plan_entry: dict) -> str:
    base = plan_entry.get("base")
    if isinstance(base, dict):
        return base.get("texture_direction") or plan_entry.get("texture_direction", "")
    return plan_entry.get("texture_direction", "")


def pair_group_mode(group: str) -> str:
    if "front" in group:
        return "front_seam"
    if "sleeve" in group or "collar" in group:
        return "identical_pair"
    return ""


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

    # severity 定义：high=必须修复，medium=建议修复，low=参考
    issues = []      # high severity
    warnings = []    # medium / low severity
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

    # 同组一致性检查（base 层参数 + texture_direction）
    group_base_layers: dict[str, dict] = {}
    group_directions: dict[str, set[str]] = {}
    for entry in fill_plan.get("pieces", []):
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if group:
            # 检查 texture_direction 一致性
            direction = base_texture_direction(entry)
            if direction:
                group_directions.setdefault(group, set()).add(direction)
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
            keys = ["texture_id", "scale", "rotation", "mirror_x", "mirror_y"]
            if pair_group_mode(group) != "front_seam":
                keys.extend(["offset_x", "offset_y"])
            for key in keys:
                if base.get(key) != ref.get(key):
                    mismatches.append(key)
            if mismatches:
                issues.append({
                    "type": "group_layer_mismatch",
                    "severity": "high",
                    "piece_id": entry["piece_id"],
                    "group": group,
                    "mismatched_keys": mismatches,
                    "message": f"同组裁片 base 层参数不一致: {mismatches}",
                })
            group_base_layers[group]["pieces"].append(entry["piece_id"])

    # 检查同组 texture_direction 一致性
    for group, directions in group_directions.items():
        if len(directions) > 1:
            warnings.append({
                "type": "group_texture_direction_mismatch",
                "severity": "medium",
                "group": group,
                "directions": list(directions),
                "message": f"同组裁片 texture_direction 不一致: {directions}",
            })

    # 左右成对裁片强约束：front/sleeve/collar 必须有一致主纹理；front 还必须声明前中缝对花。
    for group, payload in group_base_layers.items():
        mode = pair_group_mode(group)
        if not mode or len(payload.get("pieces", [])) < 2:
            continue
        refs = []
        constraint_modes = set()
        for piece_id in payload["pieces"]:
            entry = next((e for e in fill_plan.get("pieces", []) if e.get("piece_id") == piece_id), {})
            base = entry.get("base") if isinstance(entry.get("base"), dict) else {}
            refs.append({
                "piece_id": piece_id,
                "fill_type": base.get("fill_type"),
                "texture_id": base.get("texture_id"),
                "solid_id": base.get("solid_id"),
                "scale": base.get("scale"),
                "rotation": base.get("rotation"),
                "texture_direction": base.get("texture_direction"),
            })
            marker = entry.get("pair_texture_constraint") or base.get("pair_texture_constraint")
            if isinstance(marker, dict):
                constraint_modes.add(marker.get("mode", ""))
            elif isinstance(marker, str):
                constraint_modes.add(marker)
        comparable = [
            (r["fill_type"], r["texture_id"], r["solid_id"], r["scale"], r["rotation"], r["texture_direction"])
            for r in refs
        ]
        if len(set(comparable)) > 1:
            issues.append({
                "type": "pair_base_texture_mismatch",
                "severity": "high",
                "group": group,
                "pieces": refs,
                "message": "左右成对裁片主纹理不一致，必须由 pair texture constraint 修正",
            })
        expected_mode = "front_seam" if mode == "front_seam" else "identical_pair"
        if expected_mode not in constraint_modes:
            issues.append({
                "type": "missing_pair_texture_constraint",
                "severity": "high",
                "group": group,
                "expected_mode": expected_mode,
                "piece_ids": payload["pieces"],
                "message": "左右成对裁片缺少主纹理约束标记；前片必须有缝合相位约束，袖片/领贴必须有同相位约束",
            })

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
                motif_cut["severity"] = "medium"
                warnings.append(motif_cut)
            for layer in collect_layer_refs(entry):
                if layer.get("fill_type") == "motif":
                    motif_scale_issue = check_motif_scale(piece_id, image_path, piece_lookup.get(piece_id, {}), layer)
                    if motif_scale_issue:
                        motif_scale_issue["severity"] = "medium"
                        warnings.append(motif_scale_issue)
                    if visual_elements:
                        motif_ori_issue = check_motif_orientation(piece_id, layer, entry, visual_elements)
                        if motif_ori_issue:
                            motif_ori_issue["severity"] = "medium"
                            warnings.append(motif_ori_issue)
        else:
            issues.append({"type": "missing_rendered_piece", "severity": "high", "piece_id": piece_id, "message": f"缺失渲染裁片: {image_path}"})

    if len(hero_entries) == 0:
        warnings.append({"type": "missing_hero_piece", "severity": "medium", "message": "未找到卖点裁片或图案定位。如设计意图为极简/基础款，请在 reason 中声明。"})
    if len(hero_entries) > 2:
        issues.append({"type": "too_many_hero_pieces", "severity": "high", "message": f"卖点裁片过多（{len(hero_entries)} 个）: {hero_entries}"})
    if len(texture_usage) <= 1 and len(fill_plan.get("pieces", [])) > 3:
        warnings.append({"type": "flat_texture_hierarchy", "severity": "low", "message": "大部分裁片使用相同面料族；输出可能感觉均匀填充。"})
    if missing_reasons:
        warnings.append({"type": "missing_plan_reasons", "severity": "medium", "piece_ids": missing_reasons, "message": "部分裁片缺少决策理由。"})
    if busy_trim:
        issues.append({"type": "busy_trim_motif", "severity": "high", "piece_ids": busy_trim, "message": "饰边/窄条裁片使用了复杂图案。"})
    if unapproved_refs:
        issues.append({"type": "unapproved_asset_reference", "severity": "high", "refs": unapproved_refs, "message": "引用了未审批的面料/图案/纯色资产。"})
    if alpha_failures:
        issues.append({"type": "missing_transparency", "severity": "high", "piece_ids": alpha_failures, "message": "渲染后 PNG 缺少透明通道。"})
    if large_solid_pieces:
        issues.append({"type": "large_piece_uses_flat_solid", "severity": "high", "piece_ids": large_solid_pieces, "message": "大面板需要协调纹理或工程化图案，而非不匹配的纯色块。"})

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
            warnings.append({"type": "trim_rotation_unusual", "severity": "low", "piece_id": piece_id, "rotation": rotation, "message": "饰边裁片纹理旋转角度较大，可能影响生产对齐。"})

    # 标记饰边裁片变化度过高作为商业可穿性风险
    for entry in fill_plan.get("pieces", []):
        piece_id = entry.get("piece_id", "")
        piece = piece_lookup.get(piece_id, {})
        aspect = piece.get("width", 1) / max(1, piece.get("height", 1))
        is_trim = entry.get("zone") == "trim" or "trim" in entry.get("garment_role", "") or aspect >= 3 or aspect <= 0.34
        if is_trim and variation_by_piece.get(piece_id, 0) > 42:
            warnings.append({"type": "trim_may_be_too_busy", "severity": "low", "piece_id": piece_id, "variation": variation_by_piece[piece_id], "message": "饰边裁片可能过于繁忙。"})

    # 程序质检状态：fail=有 high severity issue，warn=只有 medium/low，pass=全空
    high_issues = [i for i in issues if i.get("severity") == "high"]
    program_qc_status = "fail" if high_issues else ("warn" if (issues or warnings) else "pass")

    report = {
        "program_qc_status": program_qc_status,
        "approved": program_qc_status == "pass",  # 向后兼容：只有全通过才 true
        "summary": {
            "pieces": len(fill_plan.get("pieces", [])),
            "hero_piece_count": len(hero_entries),
            "texture_usage": texture_usage,
            "high_issues": len(high_issues),
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

    # 质检反馈闭环：发现 high severity issues 时自动构造返工请求
    if high_issues:
        _build_rework_request(high_issues, warnings, fill_plan, out_path.parent)

    # 质检报告生成成功即返回 0；issues/warnings 供人工/子 Agent 审阅
    return 0


def _build_rework_request(issues: list, warnings: list, fill_plan: dict, out_dir: Path) -> None:
    """构造返工请求，供子Agent修订填充计划。
    自动合并商业复审 issues（如果 commercial_review_result.json 存在）。
    """
    # 尝试读取商业复审结果
    commercial_issues = []
    review_result_path = out_dir / "commercial_review_result.json"
    if review_result_path.exists():
        try:
            review = json.loads(review_result_path.read_text(encoding="utf-8"))
            # 优先从 ai_commercial_review.json 读原始 issues（更详细）
            ai_review_path = out_dir / "ai_commercial_review.json"
            if ai_review_path.exists():
                ai_review = json.loads(ai_review_path.read_text(encoding="utf-8"))
                commercial_issues = ai_review.get("issues", [])
            else:
                # fallback：从 result 中无法获取详细 issues，只标记未通过
                if not review.get("approved", True):
                    commercial_issues = [{
                        "severity": "high",
                        "category": "commercial_review",
                        "description": review.get("assessment", "商业复审未通过"),
                        "suggested_fix": review.get("priority_fix", "请参考商业复审报告"),
                    }]
        except Exception:
            pass

    lines = [
        "你是一位高级服装印花艺术指导。当前填充计划存在以下问题，请修订。",
        "",
        "===== 当前填充计划摘要 =====",
        f"总裁片数: {len(fill_plan.get('pieces', []))}",
        f"Hero裁片: {[p['piece_id'] for p in fill_plan.get('pieces', []) if (p.get('overlay') or {}).get('fill_type') == 'motif']}",
        "",
        "===== 必须修复的问题（程序质检） =====",
    ]
    for issue in issues:
        lines.append(f"- [{issue.get('type')}] {issue.get('piece_id', '')}: {issue.get('message', '')}")

    if commercial_issues:
        lines.extend([
            "",
            "===== 商业复审发现的问题（AI 终审） =====",
            "以下问题来自商业买手视角的整体审美判断，优先级高于程序质检：",
        ])
        for ci in commercial_issues:
            severity = ci.get("severity", "medium")
            category = ci.get("category", "")
            desc = ci.get("description", "")
            fix = ci.get("suggested_fix", "")
            lines.append(f"- [{severity}] {category}: {desc}")
            if fix:
                lines.append(f"  建议修改: {fix}")

    if warnings:
        lines.extend([
            "",
            "===== 建议优化（警告） =====",
        ])
        for w in warnings:
            lines.append(f"- [{w.get('type')}] {w.get('piece_id', '')}: {w.get('message', '')}")

    lines.extend([
        "",
        "===== 修订要求 =====",
        "1. 优先处理商业复审问题（卖点、和谐度、可穿性）",
        "2. 针对每个 issue 给出具体修正：修改哪个裁片的哪个参数",
        "3. 保持已有的正确决策不变",
        "4. 输出完整的 ai_piece_fill_plan.json 格式（与之前相同）",
        "5. 每个修改必须附带 reason",
        "",
        "===== 输出格式 =====",
        "请返回严格的 JSON，格式与之前的 piece_fill_plan.json 完全一致。",
    ])

    prompt_path = out_dir / "rework_prompt.txt"
    prompt_path.write_text("\n".join(lines), encoding="utf-8")

    request = {
        "request_id": "rework_request_v1",
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "commercial_issue_count": len(commercial_issues),
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / "ai_piece_fill_plan_revised.json").resolve()),
        "issues": issues,
        "warnings": warnings,
        "commercial_issues": commercial_issues,
    }
    req_path = out_dir / "ai_rework_request.json"
    req_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[质检闭环] 发现 {len(issues)} 个问题，已构造返工请求:")
    print(f"  返工提示词: {prompt_path}")
    print(f"  预期输出: {request['expected_output']}")


if __name__ == "__main__":
    raise SystemExit(main())
