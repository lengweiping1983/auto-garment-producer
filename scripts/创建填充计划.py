#!/usr/bin/env python3
"""
根据服装部位映射、面料组合和商业设计简报，创建艺术指导裁片填充计划。

支持两种模式：
1. 优先使用 ai_piece_fill_plan.json（子 Agent 审美决策输出）
2. 回退到后端规则生成（当 AI 计划不存在或格式错误时）

无论哪种模式，最终都会经过后端强制校验修正。
"""
import argparse
import json
import math
from pathlib import Path

from PIL import Image


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def approved_ids(texture_set: dict, key: str, id_key: str, fallback_key: str = "role") -> list[str]:
    ids = []
    for item in texture_set.get(key, []):
        if item.get("approved", False):
            value = item.get(id_key) or item.get(fallback_key)
            if value:
                ids.append(value)
    return ids


def choose(ids: list[str], preferred: list[str]) -> str:
    for item in preferred:
        if item in ids:
            return item
    return ids[0] if ids else ""


def make_layer(fill_type: str, reason: str, **kwargs) -> dict:
    layer = {
        "fill_type": fill_type,
        "scale": kwargs.pop("scale", 1.0),
        "rotation": kwargs.pop("rotation", 0),
        "offset_x": kwargs.pop("offset_x", 0),
        "offset_y": kwargs.pop("offset_y", 0),
        "opacity": kwargs.pop("opacity", 1.0),
        "mirror_x": kwargs.pop("mirror_x", False),
        "mirror_y": kwargs.pop("mirror_y", False),
        "reason": reason,
    }
    layer.update({key: value for key, value in kwargs.items() if value not in ("", None)})
    return layer


def load_motif_geometries(visual_elements_path: Path) -> dict:
    """从 visual_elements.json 提取所有 motif 候选元素的几何信息。
    返回 {motif_name: geometry_dict} 映射。支持多 key 查找以便匹配。
    兼容 LLM 路径（字段名 suggested_usage）和纯 CV 路径（字段名 suggested_use）。"""
    if not visual_elements_path.exists():
        return {}
    try:
        data = json.loads(visual_elements_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    geometries = {}
    for obj in data.get("dominant_objects", []):
        geo = obj.get("geometry")
        if not geo:
            continue
        name = obj.get("name", "")
        # 兼容两种字段名
        usage = obj.get("suggested_usage", "") or obj.get("suggested_use", "")
        # 宽松匹配：hero_motif / accent_motif / motif 直接是 motif
        # main_texture / secondary_texture 如果带有明显的 form_type（如 tall_flower, branch），也视为潜在 motif
        is_motif = usage in ("hero_motif", "accent_motif", "motif")
        form_type = geo.get("form_type", "")
        if not is_motif and form_type in ("tall_flower", "branch", "round_motif", "elongated"):
            is_motif = True
        if not is_motif:
            continue
        # 使用 name 作为 key
        geometries[name] = geo
        # 额外用 usage 作为 key
        if usage:
            geometries[usage] = geo
        # 用 form_type 作为 key
        if form_type:
            geometries[form_type] = geo
    return geometries


def simulate_motif_size(motif_w: int, motif_h: int, piece_w: int, piece_h: int, scale: float) -> tuple[float, float]:
    """模拟 render_motif_layer 的缩放逻辑，返回缩放后的 motif 尺寸。"""
    target_max_w = piece_w * scale
    target_max_h = piece_h * scale
    ratio = min(target_max_w / max(1, motif_w), target_max_h / max(1, motif_h))
    return motif_w * ratio, motif_h * ratio


def compute_optimal_scale(motif_w: int, motif_h: int, piece_w: int, piece_h: int, desired_coverage: float = 0.55) -> float:
    """二分搜索找到使 motif coverage 接近 desired_coverage 的 scale。"""
    def coverage_at(s):
        mw, mh = simulate_motif_size(motif_w, motif_h, piece_w, piece_h, s)
        return (mw * mh) / max(1, piece_w * piece_h)

    lo, hi = 0.05, 3.0
    for _ in range(20):
        mid = (lo + hi) / 2
        if coverage_at(mid) < desired_coverage:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def compute_motif_fit_score(motif_geo: dict, piece: dict, piece_role: str = "") -> dict:
    """计算 motif 与裁片的综合适配度分数（0-1）。"""
    piece_w = piece.get("width", 1)
    piece_h = piece.get("height", 1)
    piece_aspect = piece_w / max(1, piece_h)
    texture_dir = piece.get("texture_direction", "transverse")

    motif_w = motif_geo.get("pixel_width", 256)
    motif_h = motif_geo.get("pixel_height", 256)
    motif_aspect = motif_w / max(1, motif_h)
    orientation = motif_geo.get("orientation", "irregular")

    # 1. 尺寸匹配度：motif 短边 vs 裁片短边
    motif_short = min(motif_w, motif_h)
    piece_short = min(piece_w, piece_h)
    size_ratio = motif_short / max(1, piece_short)
    # 理想比例 0.75（motif 短边占裁片短边的 75%）
    size_score = max(0.0, 1.0 - abs(size_ratio - 0.75) * 2.5)

    # 2. 长宽比匹配度
    log_diff = abs(math.log(max(motif_aspect, 0.01)) - math.log(max(piece_aspect, 0.01)))
    aspect_score = max(0.0, 1.0 - log_diff / 1.5)

    # 3. 方向匹配度
    if orientation == "vertical" and texture_dir == "longitudinal":
        orientation_score = 1.0
    elif orientation == "horizontal" and texture_dir == "transverse":
        orientation_score = 1.0
    elif orientation in ("radial", "symmetric"):
        orientation_score = 0.85
    else:
        orientation_score = 0.35

    # 4. 防切割预估：用最优 scale 模拟 coverage
    desired_coverage = 0.6 if "hero" in piece_role else 0.35
    optimal_scale = compute_optimal_scale(motif_w, motif_h, piece_w, piece_h, desired_coverage)
    mw, mh = simulate_motif_size(motif_w, motif_h, piece_w, piece_h, optimal_scale)
    visibility = min(1.0, piece_w / max(1, mw)) * min(1.0, piece_h / max(1, mh))
    visibility_score = visibility

    # 综合分数
    total = size_score * 0.25 + aspect_score * 0.2 + orientation_score * 0.3 + visibility_score * 0.25

    # 推荐 rotation
    if orientation == "vertical" and texture_dir == "transverse":
        recommended_rotation = 90
    elif orientation == "horizontal" and texture_dir == "longitudinal":
        recommended_rotation = 90
    else:
        recommended_rotation = 0

    return {
        "total": round(total, 3),
        "size_score": round(size_score, 3),
        "aspect_score": round(aspect_score, 3),
        "orientation_score": round(orientation_score, 3),
        "visibility_score": round(visibility_score, 3),
        "recommended_scale": optimal_scale,
        "recommended_rotation": recommended_rotation,
        "recommended_anchor": "center",
    }


def compute_motif_rotation(motif_orientation: str, texture_direction: str, piece: dict) -> float:
    """根据 motif 方向性和裁片方向计算建议的 rotation。"""
    if motif_orientation == "vertical" and texture_direction == "transverse":
        return 90.0
    if motif_orientation == "horizontal" and texture_direction == "longitudinal":
        return 90.0
    if motif_orientation in ("radial", "symmetric"):
        return 0.0
    # irregular：保持原有 rotation 或根据裁片 aspect 微调
    piece_aspect = piece.get("width", 1) / max(1, piece.get("height", 1))
    if piece_aspect > 1.5 and motif_orientation == "vertical":
        return 90.0
    return 0.0


def infer_map_from_pieces(pieces_payload: dict) -> dict:
    """当没有部位映射文件时，从裁片几何回退推断。"""
    pieces = sorted(pieces_payload.get("pieces", []), key=lambda p: p["area"], reverse=True)
    largest_area = pieces[0]["area"] if pieces else 1
    mapped = []
    for index, piece in enumerate(pieces):
        aspect = piece["width"] / max(1, piece["height"])
        area_ratio = piece["area"] / max(1, largest_area)
        if (aspect >= 3 or aspect <= 0.34) and area_ratio < 0.08:
            role, zone, confidence = "trim_strip", "trim", 0.52
        elif aspect >= 3 or aspect <= 0.34:
            role, zone, confidence = "side_or_long_panel", "secondary", 0.5
        elif index == 0:
            role, zone, confidence = "front_hero", "body", 0.5
        elif index == 1:
            role, zone, confidence = "back_body", "body", 0.48
        else:
            role, zone, confidence = "small_detail", "detail", 0.42
        texture_dir = "longitudinal" if (aspect >= 1.8 or aspect <= 0.55) else "transverse"
        mapped.append({
            "piece_id": piece["piece_id"],
            "garment_role": role,
            "zone": zone,
            "symmetry_group": "",
            "same_shape_group": "",
            "direction_degrees": 90 if aspect >= 1.8 else 0,
            "texture_direction": texture_dir,
            "confidence": confidence,
            "reason": "回退几何推断",
        })
    return {
        "map_id": "fallback_garment_map",
        "method": "fallback_geometry_inference",
        "confidence": 0.48,
        "pieces": mapped,
    }


def fallback_create_plan(pieces_payload: dict, texture_set: dict, garment_map: dict, brief: dict, motif_geometries: dict = None) -> tuple[dict, dict]:
    """后端回退规则生成填充计划（当 AI 计划不可用时）。"""
    texture_ids = approved_ids(texture_set, "textures", "texture_id")
    motif_ids = approved_ids(texture_set, "motifs", "motif_id")
    solid_ids = approved_ids(texture_set, "solids", "solid_id")
    if not texture_ids:
        raise RuntimeError("没有已批准的面料可用于艺术指导填充计划。")

    main_id = choose(texture_ids, ["main", "base", "secondary", "accent", "dark"])
    secondary_id = choose(texture_ids, ["secondary", "main", "accent", "dark"])
    accent_id = choose(texture_ids, ["accent", "secondary", "main", "dark"])
    dark_id = choose(texture_ids, ["dark", "secondary", "accent", "main"])
    trim_solid_id = choose(solid_ids, ["moss_green", "forest_green", "quiet_moss", "dark", "solid"])
    motif_id = choose(motif_ids, ["hero_motif", "hero", "accent_motif"])

    by_piece = {item["piece_id"]: item for item in garment_map.get("pieces", [])}
    sorted_pieces = sorted(pieces_payload.get("pieces", []), key=lambda p: p["area"], reverse=True)
    largest_area = sorted_pieces[0]["area"] if sorted_pieces else 1
    hero_count = 0
    entries = []
    hero_ids, quiet_ids, secondary_ids, trim_ids = [], [], [], []
    risk_notes = []
    if garment_map.get("method", "").startswith("fallback"):
        risk_notes.append("服装部位由几何回退估计得出")
    if not motif_id:
        risk_notes.append("没有已批准的图案资产；卖点处理仅使用纹理层次")
    group_params: dict[str, dict] = {}

    for index, piece in enumerate(sorted_pieces):
        map_item = by_piece.get(piece["piece_id"], {})
        role = map_item.get("garment_role", piece.get("piece_role", "unknown"))
        zone = map_item.get("zone", "detail")
        symmetry_group = map_item.get("symmetry_group", "")
        same_shape_group = map_item.get("same_shape_group", "")
        direction = int(map_item.get("direction_degrees", 0) or 0)
        texture_direction = map_item.get("texture_direction", "")
        aspect = piece["width"] / max(1, piece["height"])
        area_ratio = piece["area"] / max(1, largest_area)
        is_true_trim = zone == "trim" or role in ("trim_strip", "collar_or_upper_trim", "hem_or_lower_trim")
        is_trim = is_true_trim and area_ratio < 0.12
        is_hero = role == "front_hero" and hero_count < (1 if len(sorted_pieces) < 8 else 2)
        group_key = same_shape_group or symmetry_group
        if group_key and group_key in group_params:
            params = group_params[group_key]
        else:
            params = {
                "offset_x": 47 * (len(group_params) + index + 1),
                "offset_y": 29 * (len(group_params) + index + 1),
                "rotation": direction,
            }
            if group_key:
                group_params[group_key] = params
        entry = {
            "piece_id": piece["piece_id"],
            "garment_role": role,
            "zone": zone,
            "symmetry_group": symmetry_group,
            "same_shape_group": same_shape_group,
            "direction_degrees": direction,
            "texture_direction": texture_direction,
            "base": None,
            "overlay": None,
            "trim": None,
            "reason": "",
        }
        if is_trim:
            trim_ids.append(piece["piece_id"])
            if dark_id:
                entry["base"] = make_layer(
                    "texture",
                    "真正饰边使用安静协调纹理，避免不匹配的纯色块",
                    texture_id=dark_id,
                    scale=1.18,
                    rotation=direction,
                    offset_x=params["offset_x"],
                    offset_y=params["offset_y"],
                )
            elif trim_solid_id:
                entry["base"] = make_layer(
                    "solid",
                    "仅小型真正饰边在没有饰边纹理时使用调色板纯色",
                    solid_id=trim_solid_id,
                )
            entry["reason"] = "小型饰边框定服装，但大型长条面板绝不能变成纯色块"
        elif is_hero:
            hero_count += 1
            hero_ids.append(piece["piece_id"])
            entry["base"] = make_layer(
                "texture",
                "前片卖点区使用低噪商业底纹，对齐服装方向",
                texture_id=main_id,
                scale=1.12,
                rotation=direction,
                offset_x=params["offset_x"],
                offset_y=params["offset_y"],
            )
            if motif_id:
                # 动态 scale：基于 motif 实际尺寸与裁片尺寸的比例
                motif_scale = 0.72
                motif_rotation = 0
                if motif_geometries:
                    # 多策略匹配 geometry
                    geo = motif_geometries.get(motif_id)
                    if not geo:
                        # 策略1：按 usage 关键词匹配
                        mid_lower = motif_id.lower()
                        if "hero" in mid_lower:
                            geo = motif_geometries.get("hero_motif")
                        elif "accent" in mid_lower:
                            geo = motif_geometries.get("accent_motif")
                    if not geo:
                        # 策略2：模糊匹配（key 包含 motif_id 或反之）
                        geo = next((g for n, g in motif_geometries.items() if n in motif_id or motif_id in n), None)
                    if not geo:
                        # 策略3：选择最大的 geometry（按 pixel area）
                        geo = max(motif_geometries.values(), key=lambda g: g.get("pixel_width", 0) * g.get("pixel_height", 0))
                    if geo:
                        fit = compute_motif_fit_score(geo, piece, role)
                        motif_scale = fit["recommended_scale"]
                        motif_rotation = fit["recommended_rotation"]
                        entry["_motif_fit_score"] = fit  # 供调试参考
                entry["overlay"] = make_layer(
                    "motif",
                    f"单一卖点图案置于关键可见裁片，动态缩放 scale={motif_scale}",
                    motif_id=motif_id,
                    anchor="center",
                    scale=motif_scale,
                    rotation=motif_rotation,
                    opacity=0.92,
                    offset_y=-round(piece["height"] * 0.04),
                )
            entry["reason"] = "前片卖点区承载简化主题，不切割叙事插画"
        elif zone == "body" or role in ("back_body", "secondary_body"):
            quiet_ids.append(piece["piece_id"])
            entry["base"] = make_layer(
                "texture",
                "大身裁片使用可穿安静底纹/辅面料，对齐服装方向",
                texture_id=main_id,
                scale=1.18,
                rotation=direction,
                offset_x=params["offset_x"],
                offset_y=params["offset_y"],
            )
            entry["reason"] = "大身裁片保持低对比度，确保产品可穿"
        elif zone == "secondary" or role in ("sleeve_pair", "sleeve_or_side_panel"):
            secondary_ids.append(piece["piece_id"])
            mirror_x = bool(symmetry_group and piece["source_x"] > (pieces_payload.get("canvas", {}).get("width", 0) / 2))
            entry["base"] = make_layer(
                "texture",
                "匹配或副面板使用协调纹理，共享组参数",
                texture_id=secondary_id,
                scale=1.22,
                rotation=direction,
                offset_x=params["offset_x"],
                offset_y=params["offset_y"],
                mirror_x=mirror_x,
            )
            entry["reason"] = "副面板增加节奏感，同形裁片保持视觉一致"
        else:
            secondary_ids.append(piece["piece_id"])
            entry["base"] = make_layer(
                "texture",
                "小型细节使用受控点缀纹理，不使用复杂叙事艺术",
                texture_id=accent_id,
                scale=1.35,
                rotation=direction,
                offset_x=params["offset_x"],
                offset_y=params["offset_y"],
            )
            entry["reason"] = "小细节支撑色板，避免杂乱"
        entries.append(entry)

    art_direction = {
        "plan_id": "commercial_art_direction_v1",
        "aesthetic_direction": brief.get("aesthetic_direction", "商业畅销款打样"),
        "hero_piece_ids": hero_ids,
        "quiet_base_piece_ids": quiet_ids,
        "secondary_piece_ids": secondary_ids,
        "trim_piece_ids": trim_ids,
        "strategy": "单一卖点定位，低噪身片，协调副片，安静饰边",
        "risk_notes": risk_notes,
    }
    fill_plan = {
        "plan_id": "commercial_piece_fill_plan_v1",
        "texture_set_id": texture_set.get("texture_set_id", ""),
        "locked": False,
        "pieces": entries,
    }
    return art_direction, fill_plan


def enforce_validation(entries: list[dict], pieces_payload: dict, texture_set: dict, motif_geometries: dict = None) -> tuple[list[dict], list[dict]]:
    """后端强制校验修正填充计划。"""
    issues = []
    by_piece = {p["piece_id"]: p for p in pieces_payload.get("pieces", [])}
    largest_area = max((p.get("area", 0) for p in pieces_payload.get("pieces", [])), default=1)
    texture_ids = approved_ids(texture_set, "textures", "texture_id")
    motif_ids = approved_ids(texture_set, "motifs", "motif_id")
    solid_ids = approved_ids(texture_set, "solids", "solid_id")
    main_id = choose(texture_ids, ["main", "base", "secondary", "accent", "dark"])
    secondary_id = choose(texture_ids, ["secondary", "main", "accent", "dark"])
    dark_id = choose(texture_ids, ["dark", "secondary", "accent", "main"])
    trim_solid_id = choose(solid_ids, ["quiet_moss", "moss_green", "forest_green", "dark", "solid"])

    # 1. 同组一致性修正
    group_templates: dict[str, dict] = {}
    for entry in entries:
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if not group:
            continue
        base = entry.get("base")
        if not isinstance(base, dict):
            continue
        if group not in group_templates:
            group_templates[group] = dict(base)
        else:
            template = group_templates[group]
            changed = False
            for key in ("texture_id", "scale", "rotation", "offset_x", "offset_y", "mirror_x", "mirror_y"):
                if base.get(key) != template.get(key):
                    base[key] = template[key]
                    changed = True
            if changed:
                issues.append({
                    "type": "fixed_group_mismatch",
                    "piece_id": entry["piece_id"],
                    "group": group,
                    "message": "修正为与同组裁片一致的 base 层参数",
                })

    # 2. Hero 数量修正
    hero_entries = [e for e in entries if e.get("garment_role") == "front_hero" or (e.get("overlay") or {}).get("fill_type") == "motif"]
    if len(hero_entries) == 0:
        # 强制指定最大 body 裁片为 hero
        body_entries = [e for e in entries if e.get("zone") == "body"]
        if body_entries:
            largest_body = max(body_entries, key=lambda e: by_piece.get(e["piece_id"], {}).get("area", 0))
            # 动态 scale
            forced_motif_id = choose(motif_ids, ["hero_motif", "hero"])
            forced_scale = 0.72
            forced_rotation = 0
            if motif_geometries:
                geo = motif_geometries.get(forced_motif_id)
                if not geo:
                    mid_lower = forced_motif_id.lower()
                    if "hero" in mid_lower:
                        geo = motif_geometries.get("hero_motif")
                    elif "accent" in mid_lower:
                        geo = motif_geometries.get("accent_motif")
                if not geo:
                    geo = next((g for n, g in motif_geometries.items() if n in forced_motif_id or forced_motif_id in n), None)
                if not geo:
                    geo = max(motif_geometries.values(), key=lambda g: g.get("pixel_width", 0) * g.get("pixel_height", 0))
                if geo:
                    fit = compute_motif_fit_score(geo, largest_body_piece, "front_hero")
                    forced_scale = fit["recommended_scale"]
                    forced_rotation = fit["recommended_rotation"]
            largest_body["overlay"] = make_layer(
                "motif",
                f"强制指定为 hero 裁片，动态缩放 scale={forced_scale}",
                motif_id=forced_motif_id,
                anchor="center",
                scale=forced_scale,
                rotation=forced_rotation,
                opacity=0.92,
            )
            issues.append({"type": "fixed_missing_hero", "piece_id": largest_body["piece_id"]})
    elif len(hero_entries) > 2:
        # 取消多余的 hero
        for extra in hero_entries[2:]:
            extra["overlay"] = None
            issues.append({"type": "fixed_excess_hero", "piece_id": extra["piece_id"]})

    # 3. Trim 安全修正
    for entry in entries:
        zone = entry.get("zone", "")
        role = entry.get("garment_role", "")
        is_trim = zone == "trim" or role in ("trim_strip", "collar_or_upper_trim", "hem_or_lower_trim")
        if not is_trim:
            continue
        overlay = entry.get("overlay")
        if overlay and overlay.get("fill_type") == "motif":
            entry["overlay"] = None
            issues.append({"type": "fixed_trim_motif", "piece_id": entry["piece_id"]})
        base = entry.get("base")
        if base and base.get("fill_type") == "texture" and base.get("texture_id") == "accent":
            if dark_id:
                base["texture_id"] = dark_id
            elif trim_solid_id:
                base["fill_type"] = "solid"
                base["solid_id"] = trim_solid_id
                del base["texture_id"]
            issues.append({"type": "fixed_trim_accent", "piece_id": entry["piece_id"]})

    # 4. 大身纯色修正
    for entry in entries:
        zone = entry.get("zone", "")
        piece = by_piece.get(entry["piece_id"], {})
        area_ratio = piece.get("area", 0) / max(1, largest_area)
        if zone == "body" and area_ratio >= 0.15:
            base = entry.get("base")
            if base and base.get("fill_type") == "solid":
                base["fill_type"] = "texture"
                base["texture_id"] = main_id
                for key in ("solid_id",):
                    base.pop(key, None)
                issues.append({"type": "fixed_large_body_solid", "piece_id": entry["piece_id"]})

    # 5. 方向对齐修正
    for entry in entries:
        piece = by_piece.get(entry["piece_id"], {})
        role = entry.get("garment_role", "")
        aspect = piece.get("width", 1) / max(1, piece.get("height", 1))
        base = entry.get("base")
        if not base:
            continue
        expected_dir = ""
        if role in ("front_hero", "back_body", "secondary_body"):
            expected_dir = "transverse"
        elif role in ("sleeve_pair", "sleeve_or_side_panel", "side_or_long_panel"):
            expected_dir = "longitudinal"
        if expected_dir and entry.get("texture_direction") != expected_dir:
            entry["texture_direction"] = expected_dir
            issues.append({"type": "fixed_texture_direction", "piece_id": entry["piece_id"], "direction": expected_dir})

    # 6. 对花对条修正（Pattern Matching）
    # 使用相同 texture + 相同 direction 的裁片共享全局纹理坐标系，确保相邻裁片缝合处图案对齐
    _apply_pattern_matching(entries, by_piece, texture_set, issues)

    # 6b. 对花对条后重新同步同组一致性（防止对称组因对花被拆散）
    _resync_group_consistency(entries, issues)

    # 7. Motif 方向对齐修正
    for entry in entries:
        overlay = entry.get("overlay")
        if not overlay or overlay.get("fill_type") != "motif":
            continue
        piece = by_piece.get(entry["piece_id"], {})
        motif_id = overlay.get("motif_id", "")
        geo = None
        if motif_geometries:
            geo = motif_geometries.get(motif_id)
            if not geo:
                mid_lower = motif_id.lower()
                if "hero" in mid_lower:
                    geo = motif_geometries.get("hero_motif")
                elif "accent" in mid_lower:
                    geo = motif_geometries.get("accent_motif")
            if not geo:
                geo = next((g for n, g in motif_geometries.items() if n in motif_id or motif_id in n), None)
            if not geo:
                geo = max(motif_geometries.values(), key=lambda g: g.get("pixel_width", 0) * g.get("pixel_height", 0))
        if not geo:
            continue
        texture_dir = entry.get("texture_direction", "transverse")
        recommended = compute_motif_rotation(geo.get("orientation", "irregular"), texture_dir, piece)
        current = overlay.get("rotation", 0)
        if abs(current - recommended) > 15:
            overlay["rotation"] = recommended
            issues.append({
                "type": "fixed_motif_orientation",
                "piece_id": entry["piece_id"],
                "old_rotation": current,
                "new_rotation": recommended,
                "reason": f"motif 方向({geo.get('orientation')}) 与裁片方向({texture_dir}) 不匹配，自动对齐",
            })
        # 同时修正 scale：如果 visibility 过低，增大 scale
        fit = compute_motif_fit_score(geo, piece, entry.get("garment_role", ""))
        current_scale = overlay.get("scale", 0.72)
        if fit["visibility_score"] < 0.7 and current_scale < fit["recommended_scale"]:
            overlay["scale"] = fit["recommended_scale"]
            issues.append({
                "type": "fixed_motif_scale",
                "piece_id": entry["piece_id"],
                "old_scale": current_scale,
                "new_scale": fit["recommended_scale"],
                "reason": f"motif 在裁片内可见度({fit['visibility_score']})不足，自动调整 scale",
            })

    return entries, issues


def _resync_group_consistency(entries: list[dict], issues: list) -> None:
    """对花对条后重新确保对称组/同形组内所有裁片 base 层参数一致。"""
    group_templates: dict[str, dict] = {}
    for entry in entries:
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if not group:
            continue
        base = entry.get("base")
        if not isinstance(base, dict):
            continue
        if group not in group_templates:
            group_templates[group] = dict(base)
        else:
            template = group_templates[group]
            changed = False
            for key in ("texture_id", "scale", "rotation", "offset_x", "offset_y", "mirror_x", "mirror_y"):
                if base.get(key) != template.get(key):
                    base[key] = template[key]
                    changed = True
            if changed:
                issues.append({
                    "type": "fixed_group_mismatch_post_matching",
                    "piece_id": entry["piece_id"],
                    "group": group,
                    "message": "对花对条后重新同步同组裁片参数",
                })


def _apply_pattern_matching(entries: list[dict], by_piece: dict, texture_set: dict, issues: list) -> None:
    """对花对条：让使用相同纹理且方向一致的相邻裁片共享全局纹理相位。
    注意：有 symmetry_group / same_shape_group 的裁片会跳过独立计算，
    由 _resync_group_consistency 统一为组内第一个成员的参数。"""
    # 读取纹理尺寸
    texture_sizes: dict[str, tuple[int, int]] = {}
    for tex in texture_set.get("textures", []):
        tid = tex.get("texture_id", "")
        path = tex.get("path", "")
        if path and Path(path).exists():
            try:
                with Image.open(path) as img:
                    texture_sizes[tid] = (img.width, img.height)
            except Exception:
                texture_sizes[tid] = (512, 512)
        else:
            texture_sizes[tid] = (512, 512)

    # 按 texture_id + texture_direction 分组
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        base = entry.get("base")
        if not base or base.get("fill_type") != "texture":
            continue
        tid = base.get("texture_id", "")
        direction = entry.get("texture_direction", "transverse")
        key = f"{tid}:{direction}"
        groups.setdefault(key, []).append(entry)

    for key, group_entries in groups.items():
        if len(group_entries) <= 1:
            continue

        tid = key.split(":")[0]
        tex_w, tex_h = texture_sizes.get(tid, (512, 512))

        # 找到 anchor（最大面积，且无 group 优先）
        # 优先选择没有 symmetry_group / same_shape_group 的裁片作为 anchor，
        # 这样对称组不会被拆散
        anchor_candidates = [
            e for e in group_entries
            if not e.get("symmetry_group") and not e.get("same_shape_group")
        ]
        if anchor_candidates:
            anchor_entry = max(
                anchor_candidates,
                key=lambda e: by_piece.get(e["piece_id"], {}).get("area", 0)
            )
        else:
            # 全部有 group，选面积最大的
            anchor_entry = max(
                group_entries,
                key=lambda e: by_piece.get(e["piece_id"], {}).get("area", 0)
            )
        anchor_id = anchor_entry["piece_id"]
        anchor_bbox = by_piece.get(anchor_id, {}).get("bbox", {})
        if not anchor_bbox:
            continue
        anchor_ox = anchor_entry["base"].get("offset_x", 0)
        anchor_oy = anchor_entry["base"].get("offset_y", 0)
        anchor_scale = anchor_entry["base"].get("scale", 1.0)

        # 对称组/同形组只处理一次代表成员
        seen_groups: set[str] = set()

        for entry in group_entries:
            if entry["piece_id"] == anchor_id:
                continue

            group = entry.get("symmetry_group") or entry.get("same_shape_group")
            if group:
                if group in seen_groups:
                    continue
                seen_groups.add(group)

            bbox = by_piece.get(entry["piece_id"], {}).get("bbox", {})
            if not bbox:
                continue

            # pattern image 中的相对位移 → 纹理空间位移
            dx = bbox.get("x", 0) - anchor_bbox.get("x", 0)
            dy = bbox.get("y", 0) - anchor_bbox.get("y", 0)
            tex_dx = dx / anchor_scale
            tex_dy = dy / anchor_scale

            new_x = (anchor_ox + tex_dx) % tex_w
            new_y = (anchor_oy + tex_dy) % tex_h

            old_x = entry["base"].get("offset_x", 0)
            old_y = entry["base"].get("offset_y", 0)

            if abs(new_x - old_x) > 1 or abs(new_y - old_y) > 1:
                entry["base"]["offset_x"] = round(new_x)
                entry["base"]["offset_y"] = round(new_y)
                issues.append({
                    "type": "fixed_pattern_matching",
                    "piece_id": entry["piece_id"],
                    "anchor": anchor_id,
                    "texture_id": tid,
                    "old_offset": [old_x, old_y],
                    "new_offset": [round(new_x), round(new_y)],
                    "message": f"对花对条：与锚点裁片 {anchor_id} 的纹理相位对齐，确保缝合处图案连续",
                })


def main() -> int:
    parser = argparse.ArgumentParser(description="为裁片创建艺术指导商业填充计划。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--garment-map", default="", help="部位映射 JSON 路径（可选）")
    parser.add_argument("--brief", default="", help="商业设计简报 JSON 路径（可选）")
    parser.add_argument("--ai-plan", default="", help="子 Agent 生成的 AI 填充计划 JSON 路径（优先使用）")
    parser.add_argument("--visual-elements", default="", help="visual_elements.json 路径（可选，用于读取 motif 几何信息）")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    pieces_payload = load_json(args.pieces)
    texture_set = load_json(args.texture_set)
    garment_map = load_json(args.garment_map) if args.garment_map else infer_map_from_pieces(pieces_payload)
    brief = load_json(args.brief) if args.brief else {"aesthetic_direction": "商业畅销款打样"}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 加载 visual_elements（如果提供）用于 motif 几何分析
    motif_geometries = {}
    if args.visual_elements:
        ve_path = Path(args.visual_elements)
        if ve_path.exists():
            motif_geometries = load_motif_geometries(ve_path)
            if motif_geometries:
                print(f"[适配度引擎] 已加载 {len(motif_geometries)} 个 motif 几何信息")
        else:
            # 尝试从 out_dir 自动查找
            auto_ve = out_dir / "visual_elements.json"
            if auto_ve.exists():
                motif_geometries = load_motif_geometries(auto_ve)
            else:
                auto_ve_cv = out_dir / "visual_elements_cv.json"
                if auto_ve_cv.exists():
                    motif_geometries = load_motif_geometries(auto_ve_cv)

    # 阶段 1：获取填充计划（优先 AI 计划，回退后端规则）
    ai_plan_used = False
    if args.ai_plan:
        ai_plan_path = Path(args.ai_plan)
        if ai_plan_path.exists():
            try:
                ai_plan = load_json(ai_plan_path)
                entries = ai_plan.get("pieces", [])
                art_direction = ai_plan.get("art_direction", {})
                if entries:
                    ai_plan_used = True
                    print(f"使用子 Agent 审美计划: {ai_plan_path}")
                else:
                    print("AI 计划为空，回退到后端规则")
                    art_direction, ai_plan = {}, {}
            except Exception as exc:
                print(f"AI 计划解析失败 ({exc})，回退到后端规则")
                art_direction, ai_plan = {}, {}
        else:
            print(f"AI 计划不存在: {ai_plan_path}，回退到后端规则")
            art_direction, ai_plan = {}, {}
    else:
        art_direction, ai_plan = {}, {}

    if not ai_plan_used:
        art_direction, fill_plan = fallback_create_plan(pieces_payload, texture_set, garment_map, brief, motif_geometries)
        entries = fill_plan.get("pieces", [])

    # 阶段 2：强制校验修正
    entries, fix_issues = enforce_validation(entries, pieces_payload, texture_set, motif_geometries)

    # 重新组装 art_direction
    hero_ids = [e["piece_id"] for e in entries if (e.get("overlay") or {}).get("fill_type") == "motif"]
    quiet_ids = [e["piece_id"] for e in entries if e.get("zone") == "body" and e["piece_id"] not in hero_ids]
    secondary_ids = [e["piece_id"] for e in entries if e.get("zone") == "secondary"]
    trim_ids = [e["piece_id"] for e in entries if e.get("zone") == "trim"]

    if ai_plan_used and art_direction:
        art_direction["hero_piece_ids"] = hero_ids
        art_direction["validation_fixes"] = fix_issues
    else:
        art_direction = {
            "plan_id": "commercial_art_direction_v1",
            "aesthetic_direction": brief.get("aesthetic_direction", "商业畅销款打样"),
            "hero_piece_ids": hero_ids,
            "quiet_base_piece_ids": quiet_ids,
            "secondary_piece_ids": secondary_ids,
            "trim_piece_ids": trim_ids,
            "strategy": "单一卖点定位，低噪身片，协调副片，安静饰边",
            "validation_fixes": fix_issues,
            "risk_notes": ["使用后端规则生成"] if not ai_plan_used else [],
        }

    fill_plan = {
        "plan_id": "commercial_piece_fill_plan_v1",
        "texture_set_id": texture_set.get("texture_set_id", ""),
        "locked": False,
        "ai_plan_used": ai_plan_used,
        "pieces": entries,
    }

    art_path = out_dir / "art_direction_plan.json"
    fill_path = out_dir / "piece_fill_plan.json"
    art_path.write_text(json.dumps(art_direction, ensure_ascii=False, indent=2), encoding="utf-8")
    fill_path.write_text(json.dumps(fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        {
            "艺术指导方案": str(art_path.resolve()),
            "裁片填充计划": str(fill_path.resolve()),
            "使用AI计划": ai_plan_used,
            "校验修正": len(fix_issues),
            "卖点裁片": art_direction["hero_piece_ids"],
        },
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
