#!/usr/bin/env python3
"""
根据服装部位映射、面料组合和商业设计简报，创建艺术指导裁片填充计划。

支持两种模式：
1. 优先使用 ai_piece_fill_plan.json（子 Agent 审美决策输出）
2. 回退到后端规则生成（当 AI 计划不存在或格式错误时）

无论哪种模式，最终都会经过后端强制校验修正。
"""
import argparse
import copy
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


def _compute_motif_render_bounds(piece_w: int, piece_h: int, motif_w: int, motif_h: int, scale: float, anchor: str, offset_x: int, offset_y: int) -> tuple[float, float, float, float]:
    """计算 motif 在裁片上的渲染位置和尺寸。返回 (x, y, render_w, render_h)。"""
    rw, rh = simulate_motif_size(motif_w, motif_h, piece_w, piece_h, scale)
    positions = {
        "center": ((piece_w - rw) / 2 + offset_x, (piece_h - rh) / 2 + offset_y),
        "top": ((piece_w - rw) / 2 + offset_x, offset_y),
        "bottom": ((piece_w - rw) / 2 + offset_x, piece_h - rh + offset_y),
        "left": (offset_x, (piece_h - rh) / 2 + offset_y),
        "right": (piece_w - rw + offset_x, (piece_h - rh) / 2 + offset_y),
        "top_left": (offset_x, offset_y),
        "top_right": (piece_w - rw + offset_x, offset_y),
        "bottom_left": (offset_x, piece_h - rh + offset_y),
        "bottom_right": (piece_w - rw + offset_x, piece_h - rh + offset_y),
    }
    x, y = positions.get(anchor, positions["center"])
    return x, y, rw, rh


def compute_optimal_scale(motif_w: int, motif_h: int, piece_w: int, piece_h: int, desired_coverage: float = 0.55) -> float:
    """二分搜索找到使 motif coverage 接近 desired_coverage 的 scale。"""
    def coverage_at(s):
        mw, mh = simulate_motif_size(motif_w, motif_h, piece_w, piece_h, s)
        return (mw * mh) / max(1, piece_w * piece_h)

    lo, hi = 0.05, 1.0
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


def _group_similar_pieces(pieces: list[dict]) -> dict[str, str]:
    """为几何相似的裁片自动分配 same_shape_group。
    配对条件：面积比 ≥0.65、aspect 比 ≥0.55、y 坐标接近（在各自高度 2.5 倍内）。
    主要用于 fallback 路径中让领子/袖口等小裁片保持同组一致。"""
    group_map: dict[str, str] = {}
    assigned: set[str] = set()
    for i, p1 in enumerate(pieces):
        if p1["piece_id"] in assigned:
            continue
        group = [p1]
        for p2 in pieces[i + 1 :]:
            if p2["piece_id"] in assigned:
                continue
            # 面积相似
            area_ratio = min(p1["area"], p2["area"]) / max(p1["area"], p2["area"])
            if area_ratio < 0.65:
                continue
            # y 坐标接近（都在顶部或都在底部）
            cy1 = p1.get("cy", p1.get("y", 0) + p1.get("height", 0) / 2)
            cy2 = p2.get("cy", p2.get("y", 0) + p2.get("height", 0) / 2)
            if abs(cy1 - cy2) > max(p1.get("height", 1), p2.get("height", 1)) * 2.5:
                continue
            # aspect 相似
            a1 = p1["width"] / max(1, p1["height"])
            a2 = p2["width"] / max(1, p2["height"])
            aspect_ratio = min(a1, a2) / max(a1, a2)
            if aspect_ratio < 0.55:
                continue
            group.append(p2)
        if len(group) >= 2:
            gname = f"ssg_{p1['piece_id']}"
            for p in group:
                assigned.add(p["piece_id"])
                group_map[p["piece_id"]] = gname
    return group_map


def infer_map_from_pieces(pieces_payload: dict) -> dict:
    """当没有部位映射文件时，从裁片几何回退推断。"""
    pieces = sorted(pieces_payload.get("pieces", []), key=lambda p: p["area"], reverse=True)
    largest_area = pieces[0]["area"] if pieces else 1
    group_map = _group_similar_pieces(pieces)
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
            "same_shape_group": group_map.get(piece["piece_id"], ""),
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

    # 注意：texture_id 实际值为 main/secondary/dark_base/accent_light/accent_mid/solid_quiet
    main_id = choose(texture_ids, ["main", "base", "secondary", "accent_light", "dark_base"])
    secondary_id = choose(texture_ids, ["secondary", "main", "accent_light", "accent_mid", "dark_base"])
    accent_id = choose(texture_ids, ["accent_light", "accent_mid", "accent", "secondary", "main", "dark_base"])
    dark_id = choose(texture_ids, ["dark_base", "dark", "secondary", "accent_light", "main"])
    trim_solid_id = choose(solid_ids, ["quiet_solid", "quiet_moss", "moss_green", "forest_green", "dark", "solid"])
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
        is_trim = is_true_trim and area_ratio < 0.18
        is_hero = role == "front_hero" and hero_count < (1 if len(sorted_pieces) < 8 else 2)
        group_key = same_shape_group or symmetry_group
        if group_key and group_key in group_params:
            params = group_params[group_key]
        else:
            params = {
                "offset_x": 47 * (len(group_params) + index + 1),
                "offset_y": 29 * (len(group_params) + index + 1),
                "scale": None,  # 由第一个成员的条件分支确定后写入
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
            piece_scale = params.get("scale") or 1.18
            if group_key and params.get("scale") is None:
                params["scale"] = piece_scale
            # trim 优先使用 dark 纹理，但允许 subtle accent texture（不再强制降级到 solid）
            if dark_id:
                entry["base"] = make_layer(
                    "texture",
                    "饰边优先使用深色协调纹理",
                    texture_id=dark_id,
                    scale=piece_scale,
                    rotation=0,
                    offset_x=params["offset_x"],
                    offset_y=params["offset_y"],
                )
            elif accent_id:
                entry["base"] = make_layer(
                    "texture",
                    "无 dark 纹理时饰边可使用 subtle accent texture",
                    texture_id=accent_id,
                    scale=piece_scale,
                    rotation=0,
                    offset_x=params["offset_x"],
                    offset_y=params["offset_y"],
                )
            elif trim_solid_id:
                entry["base"] = make_layer(
                    "solid",
                    "仅小型饰边在无纹理可用时使用调色板纯色",
                    solid_id=trim_solid_id,
                )
            entry["reason"] = "饰边使用协调纹理或 subtle accent，保持视觉边界感"
        elif is_hero:
            hero_count += 1
            hero_ids.append(piece["piece_id"])
            piece_scale = params.get("scale") or 1.12
            if group_key and params.get("scale") is None:
                params["scale"] = piece_scale
            entry["base"] = make_layer(
                "texture",
                "前片卖点区使用低噪商业底纹，对齐服装方向",
                texture_id=main_id,
                scale=piece_scale,
                rotation=0,
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
                    opacity=1.0,
                    offset_y=-round(piece["height"] * 0.04),
                )
            entry["reason"] = "前片卖点区承载简化主题，不切割叙事插画"
        elif zone == "body" or role in ("back_body", "secondary_body"):
            quiet_ids.append(piece["piece_id"])
            piece_scale = params.get("scale") or 1.18
            if group_key and params.get("scale") is None:
                params["scale"] = piece_scale
            entry["base"] = make_layer(
                "texture",
                "大身裁片使用可穿安静底纹/辅面料，对齐服装方向",
                texture_id=main_id,
                scale=piece_scale,
                rotation=0,
                offset_x=params["offset_x"],
                offset_y=params["offset_y"],
            )
            entry["reason"] = "大身裁片保持低对比度，确保产品可穿"
        elif zone == "secondary" or role in ("sleeve_pair", "sleeve_or_side_panel"):
            secondary_ids.append(piece["piece_id"])
            mirror_x = bool(symmetry_group and piece["source_x"] > (pieces_payload.get("canvas", {}).get("width", 0) / 2))
            piece_scale = params.get("scale") or 1.22
            if group_key and params.get("scale") is None:
                params["scale"] = piece_scale
            entry["base"] = make_layer(
                "texture",
                "匹配或副面板使用协调纹理，共享组参数",
                texture_id=secondary_id,
                scale=piece_scale,
                rotation=0,
                offset_x=params["offset_x"],
                offset_y=params["offset_y"],
                mirror_x=mirror_x,
            )
            entry["reason"] = "副面板增加节奏感，同形裁片保持视觉一致"
        else:
            secondary_ids.append(piece["piece_id"])
            piece_scale = params.get("scale") or 1.35
            if group_key and params.get("scale") is None:
                params["scale"] = piece_scale
            entry["base"] = make_layer(
                "texture",
                "小型细节使用受控点缀纹理，不使用复杂叙事艺术",
                texture_id=accent_id,
                scale=piece_scale,
                rotation=0,
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


def apply_symmetry_relations(entries: list[dict], garment_map: dict, pieces_payload: dict | None = None) -> list[dict]:
    """应用对称关系优化：slave 裁片复制 master 裁片的填充参数。

    逻辑：
    1. 优先使用 garment_map 中已有的硬编码 symmetry_relations
    2. 如果没有硬编码，调用 symmetry_analyzer 基于 mask 形状自动分析
    3. 自动选择面积更大的裁片作为 master
    4. slave 的 base/overlay/trim 完全复制 master 的
    5. slave 的纹理层面 mirror_x/mirror_y 置 false（镜像在 PNG 层面做）
    6. 在 slave entry 中标记 symmetry_source 和 symmetry_transform

    注意：front / sleeve / collar 这类成对裁片的商业目标是“主纹理观感一致”，
    不是最终 PNG 镜像复制。它们由 enforce_pair_texture_constraints 处理。
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from symmetry_analyzer import find_symmetry_relations
    except Exception:
        find_symmetry_relations = None

    gm_pieces = {p["piece_id"]: p for p in garment_map.get("pieces", [])}
    entries_by_id = {e["piece_id"]: e for e in entries}

    def allows_png_symmetry(piece_id: str, rel: dict) -> bool:
        gm = gm_pieces.get(piece_id, {})
        role = gm.get("garment_role", "")
        group = gm.get("symmetry_group") or gm.get("same_shape_group") or ""
        pair_texture_roles = {"front_body", "front_hero", "sleeve_pair", "collar_or_upper_trim"}
        pair_texture_groups = ("front", "sleeve", "collar")
        if rel.get("render_strategy") == "png_mirror" or gm.get("allow_png_symmetry"):
            return True
        if role in pair_texture_roles or any(token in group for token in pair_texture_groups):
            return False
        return True

    # 构建 slave→master 映射
    slave_map = {}

    # 1. 先收集硬编码的 symmetry_relations
    for piece_id, gm in gm_pieces.items():
        for rel in gm.get("symmetry_relations", []):
            target_pid = rel.get("target_piece_id")
            if target_pid and target_pid in entries_by_id and allows_png_symmetry(piece_id, rel):
                slave_map[target_pid] = {
                    "source": piece_id,
                    "transform": {"mirror_x": rel.get("mirror_x", False), "mirror_y": rel.get("mirror_y", False)},
                }

    # 2. 如果没有硬编码，且提供了 pieces_payload，尝试自动分析
    if not slave_map and pieces_payload and find_symmetry_relations:
        try:
            auto_relations = find_symmetry_relations(pieces_payload, garment_map)
            auto_applied = 0
            auto_skipped = 0
            for master_pid, slaves in auto_relations.items():
                master_gm = gm_pieces.get(master_pid, {})
                master_role = master_gm.get("garment_role", "")
                master_group = master_gm.get("symmetry_group") or master_gm.get("same_shape_group") or ""
                # 大身裁片（front/back/secondary）默认不自动应用，需人工在 base.json 配置
                auto_symmetry_roles = {
                    "sleeve_or_side_panel", "hem_or_lower_trim",
                    "trim_strip", "matched_panel", "small_detail",
                }
                can_auto_apply = master_role in auto_symmetry_roles and "long_trim" not in master_group
                for rel in slaves:
                    target_pid = rel["target_piece_id"]
                    if target_pid not in entries_by_id:
                        continue
                    if can_auto_apply and rel.get("iou", 0) >= 0.99:
                        slave_map[target_pid] = {
                            "source": master_pid,
                            "transform": {"mirror_x": rel.get("mirror_x", False), "mirror_y": rel.get("mirror_y", False)},
                        }
                        auto_applied += 1
                    else:
                        auto_skipped += 1
                        tx = "mirror_x" if rel.get("mirror_x") else ""
                        ty = "mirror_y" if rel.get("mirror_y") else ""
                        tdesc = "/".join(filter(None, [tx, ty])) or "identity"
                        print(f"[对称分析建议] {master_pid} -> {target_pid}: {tdesc} (IoU={rel.get('iou')}), "
                              f"role={master_role} — {'已自动应用' if can_auto_apply else '大身裁片，未自动应用，如需请在 base.json 配置'}")
            if auto_applied:
                print(f"[对称分析] 自动应用 {auto_applied} 个 slave 裁片")
            if auto_skipped:
                print(f"[对称分析] {auto_skipped} 个关系未自动应用（大身裁片或 IoU<0.99），请查看上方建议")
        except Exception as exc:
            print(f"[对称分析] 自动分析失败，回退到独立渲染: {exc}")

    if not slave_map:
        return entries

    new_entries = []
    for entry in entries:
        pid = entry["piece_id"]
        if pid not in slave_map:
            new_entries.append(entry)
            continue

        # 这是 slave piece，复制 master 参数
        slave_info = slave_map[pid]
        master_entry = entries_by_id.get(slave_info["source"])
        if not master_entry:
            new_entries.append(entry)
            continue

        slave = copy.deepcopy(master_entry)
        slave["piece_id"] = pid
        slave["symmetry_source"] = slave_info["source"]
        slave["symmetry_transform"] = slave_info["transform"]
        # 清除纹理层面的 mirror（镜像在 PNG 层面做）
        for layer_key in ("base", "overlay", "trim"):
            layer = slave.get(layer_key)
            if layer and isinstance(layer, dict):
                layer["mirror_x"] = False
                layer["mirror_y"] = False
        # 保留 slave 自身的 garment_role / zone / symmetry_group
        gm_slave = gm_pieces.get(pid, {})
        for key in ("garment_role", "zone", "symmetry_group", "same_shape_group"):
            if key in gm_slave:
                slave[key] = gm_slave[key]
        new_entries.append(slave)

    return new_entries


def _base_texture_direction(entry: dict) -> str:
    base = entry.get("base")
    if isinstance(base, dict):
        return base.get("texture_direction") or entry.get("texture_direction", "")
    return entry.get("texture_direction", "")


def _is_pair_texture_piece(item: dict) -> bool:
    role = item.get("garment_role", "")
    group = item.get("symmetry_group") or item.get("same_shape_group") or ""
    return (
        role in {"front_body", "front_hero", "sleeve_pair", "collar_or_upper_trim", "trim_strip"}
        or any(token in group for token in ("front", "sleeve", "collar", "trim"))
    )


def restore_pair_metadata(entries: list[dict], garment_map: dict, issues: list[dict]) -> None:
    """从 garment_map 恢复左右成对裁片的 group 信息，防止 AI 计划丢掉约束。"""
    gm_by_id = {p.get("piece_id"): p for p in garment_map.get("pieces", [])}
    for entry in entries:
        gm = gm_by_id.get(entry.get("piece_id"))
        if not gm or not _is_pair_texture_piece(gm):
            continue
        changed = []
        for key in ("zone", "symmetry_group", "same_shape_group"):
            value = gm.get(key, "")
            if value and entry.get(key) != value:
                entry[key] = value
                changed.append(key)
        if not entry.get("texture_direction") and gm.get("texture_direction"):
            entry["texture_direction"] = gm.get("texture_direction")
            changed.append("texture_direction")
        # 保留 AI 的 front_hero 判断，但普通 front/sleeve/collar 角色从模板恢复。
        if entry.get("garment_role") != "front_hero" and gm.get("garment_role") and entry.get("garment_role") != gm.get("garment_role"):
            entry["garment_role"] = gm.get("garment_role")
            changed.append("garment_role")
        if changed:
            issues.append({
                "type": "restored_pair_metadata",
                "severity": "high",
                "piece_id": entry["piece_id"],
                "fields": changed,
                "message": "从 garment_map 恢复左右成对裁片的分组/方向信息，防止 AI 计划绕过 pair 约束",
            })


def _texture_tile_size(texture_set: dict, texture_id: str, base: dict) -> tuple[int, int]:
    tex_w, tex_h = 512, 512
    for tex in texture_set.get("textures", []):
        if tex.get("texture_id") != texture_id and tex.get("role") != texture_id:
            continue
        path = tex.get("path", "")
        if path and Path(path).exists():
            try:
                with Image.open(path) as img:
                    tex_w, tex_h = img.size
            except Exception:
                pass
        break
    scale = max(0.05, float(base.get("scale", 1.0) or 1.0))
    tex_w = max(1, round(tex_w * scale))
    tex_h = max(1, round(tex_h * scale))
    rotation = int(float(base.get("rotation", 0) or 0)) % 180
    if rotation == 90:
        tex_w, tex_h = tex_h, tex_w
    return tex_w, tex_h


def _pair_group_mode(group: str) -> str:
    if "front" in group:
        return "front_seam"
    if "sleeve" in group:
        return "identical_pair"
    if "collar" in group:
        return "identical_pair"
    return "pair"


def enforce_pair_texture_constraints(
    entries: list[dict],
    garment_map: dict,
    pieces_payload: dict,
    texture_set: dict,
    issues: list[dict],
) -> None:
    """强制左右成对裁片的 base 主纹理一致；前片额外做中缝相位约束。"""
    restore_pair_metadata(entries, garment_map, issues)
    by_piece = {p["piece_id"]: p for p in pieces_payload.get("pieces", [])}
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("intentional_asymmetry"):
            continue
        if not _is_pair_texture_piece(entry):
            continue
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if not group:
            continue
        groups.setdefault(group, []).append(entry)

    copy_keys = (
        "fill_type", "texture_id", "solid_id", "scale", "rotation",
        "mirror_x", "mirror_y", "texture_direction", "respect_pattern_orientation",
    )
    for group, members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda e: (by_piece.get(e["piece_id"], {}).get("source_x", 0), e["piece_id"]))
        master = members[0]
        master_base = master.get("base")
        if not isinstance(master_base, dict):
            continue
        mode = _pair_group_mode(group)
        master_piece = by_piece.get(master["piece_id"], {})
        tex_w, tex_h = _texture_tile_size(texture_set, master_base.get("texture_id", ""), master_base)
        master_ox = int(master_base.get("offset_x", 0) or 0)
        master_oy = int(master_base.get("offset_y", 0) or 0)

        for member in members:
            base = member.get("base")
            if not isinstance(base, dict):
                member["base"] = dict(master_base)
                base = member["base"]
            changed = []
            for key in copy_keys:
                if key in master_base and base.get(key) != master_base.get(key):
                    base[key] = master_base.get(key)
                    changed.append(key)
            if mode == "front_seam" and member is not master:
                piece = by_piece.get(member["piece_id"], {})
                dx = piece.get("source_x", 0) - master_piece.get("source_x", 0)
                dy = piece.get("source_y", 0) - master_piece.get("source_y", 0)
                new_x = (master_ox - dx) % tex_w
                new_y = (master_oy - dy) % tex_h
            else:
                new_x = master_ox
                new_y = master_oy
            if base.get("offset_x") != round(new_x):
                base["offset_x"] = round(new_x)
                changed.append("offset_x")
            if base.get("offset_y") != round(new_y):
                base["offset_y"] = round(new_y)
                changed.append("offset_y")
            base["pair_texture_constraint"] = mode
            member["pair_texture_constraint"] = {
                "group": group,
                "mode": mode,
                "source_piece_id": master["piece_id"],
            }
            member.pop("symmetry_source", None)
            member.pop("symmetry_transform", None)
            if changed:
                issues.append({
                    "type": "fixed_pair_texture_constraint",
                    "severity": "high",
                    "piece_id": member["piece_id"],
                    "group": group,
                    "mode": mode,
                    "source_piece_id": master["piece_id"],
                    "fields": sorted(set(changed)),
                    "message": "强制左右成对裁片主纹理一致；前片按缝合相位约束，袖片/领贴按同相位约束",
                })


def enforce_validation(
    entries: list[dict],
    pieces_payload: dict,
    texture_set: dict,
    garment_map: dict | None = None,
    motif_geometries: dict = None,
    brief: dict = None,
) -> tuple[list[dict], list[dict]]:
    """后端强制校验修正填充计划。"""
    issues = []
    by_piece = {p["piece_id"]: p for p in pieces_payload.get("pieces", [])}
    largest_area = max((p.get("area", 0) for p in pieces_payload.get("pieces", [])), default=1)
    texture_ids = approved_ids(texture_set, "textures", "texture_id")
    motif_ids = approved_ids(texture_set, "motifs", "motif_id")
    solid_ids = approved_ids(texture_set, "solids", "solid_id")
    main_id = choose(texture_ids, ["main", "base", "secondary", "accent_light", "dark_base"])
    secondary_id = choose(texture_ids, ["secondary", "main", "accent_light", "accent_mid", "dark_base"])
    dark_id = choose(texture_ids, ["dark_base", "dark", "secondary", "accent_light", "main"])
    trim_solid_id = choose(solid_ids, ["quiet_solid", "quiet_moss", "moss_green", "forest_green", "dark", "solid"])

    garment_map = garment_map or {}
    restore_pair_metadata(entries, garment_map, issues)

    # 1. 同组一致性修正（安全修正：支持 intentional_asymmetry 声明时跳过）
    group_templates: dict[str, dict] = {}
    for entry in entries:
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if not group:
            continue
        base = entry.get("base")
        # AI 声明了有意不对称设计，程序不强制修正
        if entry.get("intentional_asymmetry"):
            issues.append({
                "type": "intentional_asymmetry_declared",
                "severity": "low",
                "piece_id": entry["piece_id"],
                "group": group,
                "message": "裁片声明了有意不对称设计，程序跳过同组一致性强制修正",
            })
            continue
        if group not in group_templates:
            # 第一个有 base 的成员设为 template；若 base 为 None，先留空待后续填充
            if isinstance(base, dict):
                group_templates[group] = dict(base)
            continue
        template = group_templates[group]
        if not isinstance(base, dict):
            # 同组成员 base 缺失，复制 template 的完整 base
            entry["base"] = dict(template)
            issues.append({
                "type": "fixed_group_missing_base",
                "severity": "high",
                "piece_id": entry["piece_id"],
                "group": group,
                "message": "同组成员 base 缺失，已复制 template 的完整 base",
            })
            continue
        changed = False
        for key in (
            "fill_type", "texture_id", "solid_id", "scale", "rotation",
            "offset_x", "offset_y", "mirror_x", "mirror_y",
            "texture_direction", "respect_pattern_orientation",
        ):
            if base.get(key) != template.get(key):
                base[key] = template[key]
                changed = True
        if changed:
            issues.append({
                "type": "fixed_group_mismatch",
                "severity": "high",
                "piece_id": entry["piece_id"],
                "group": group,
                "message": "修正为与同组裁片一致的 base 层参数",
            })

    # 2. Hero 数量修正
    hero_entries = [e for e in entries if e.get("garment_role") == "front_hero" or (e.get("overlay") or {}).get("fill_type") == "motif"]
    if len(hero_entries) == 0:
        # 程序不做 hero 决策——留给 AI 子Agent判断哪个裁片最适合做 hero
        body_entries = [e for e in entries if e.get("zone") == "body"]
        if body_entries:
            # 列出候选 body 裁片信息供 AI 参考，但不强制指定
            candidates = [
                {
                    "piece_id": e["piece_id"],
                    "garment_role": e.get("garment_role", ""),
                    "area": by_piece.get(e["piece_id"], {}).get("area", 0),
                }
                for e in body_entries
            ]
            issues.append({
                "type": "missing_hero_decision",
                "severity": "high",
                "message": "当前没有 hero 裁片，需要 AI 决策指定。候选 body 裁片如下（按面积排序）：",
                "candidates": sorted(candidates, key=lambda c: c["area"], reverse=True),
            })
    elif len(hero_entries) > 2:
        # 取消多余的 hero
        for extra in hero_entries[2:]:
            extra["overlay"] = None
            issues.append({"type": "fixed_excess_hero", "severity": "high", "piece_id": extra["piece_id"], "message": "hero 裁片超过 2 个，已取消多余 overlay"})

    # 3. Trim 安全修正：禁用 motif overlay（防切割风险），但允许 subtle accent texture
    for entry in entries:
        zone = entry.get("zone", "")
        role = entry.get("garment_role", "")
        is_trim = zone == "trim" or role in ("trim_strip", "collar_or_upper_trim", "hem_or_lower_trim")
        if not is_trim:
            continue
        overlay = entry.get("overlay")
        if overlay and overlay.get("fill_type") == "motif":
            entry["overlay"] = None
            issues.append({"type": "fixed_trim_motif", "severity": "high", "piece_id": entry["piece_id"], "message": "trim 禁用 motif overlay（防切割风险）"})
        # 不再强制把 accent texture 降级为 dark/solid——允许 subtle accent
        # 如果 accent 变化度过高（>50），才建议降级为 soft warning
        base = entry.get("base")
        if base and base.get("fill_type") == "texture" and "accent" in (base.get("texture_id") or ""):
            piece = by_piece.get(entry["piece_id"], {})
            # 这里无法直接读取纹理变化度，用 scale 作为代理指标
            scale = base.get("scale", 1.0)
            if scale > 1.5:
                issues.append({
                    "type": "trim_accent_may_be_too_busy",
                    "severity": "medium",
                    "piece_id": entry["piece_id"],
                    "message": "trim 使用 accent texture 且 scale 较大，可能过于繁忙，建议 AI 审阅",
                })

    # 4. 大身纯色检查（审美修正 → 只记录 issue，不强制替换）
    for entry in entries:
        zone = entry.get("zone", "")
        piece = by_piece.get(entry["piece_id"], {})
        area_ratio = piece.get("area", 0) / max(1, largest_area)
        if zone == "body" and area_ratio >= 0.15:
            base = entry.get("base")
            if base and base.get("fill_type") == "solid":
                issues.append({
                    "type": "large_body_solid_not_recommended",
                    "severity": "high",
                    "piece_id": entry["piece_id"],
                    "message": "大身裁片使用纯色通常不推荐（缺乏纹理层次），建议改用 texture 或确认设计意图",
                })

    # 5. 方向一致性检查（AI 决定 texture_direction，程序只兜底检查同组一致性）
    group_directions: dict[str, set[str]] = {}
    for entry in entries:
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if not group:
            continue
        direction = _base_texture_direction(entry)
        if direction:
            group_directions.setdefault(group, set()).add(direction)
    for group, directions in group_directions.items():
        if len(directions) > 1:
            issues.append({
                "type": "group_direction_mismatch",
                "severity": "medium",
                "group": group,
                "directions": list(directions),
                "message": f"同组裁片 texture_direction 不一致: {directions}，请AI统一方向",
            })

    # 6. 对花对条修正（Pattern Matching）
    # 使用相同 texture + 相同 direction 的裁片共享全局纹理坐标系，确保相邻裁片缝合处图案对齐
    _apply_pattern_matching(entries, by_piece, texture_set, issues)

    # 6b. 对花对条后重新同步同组一致性（防止对称组因对花被拆散）
    _resync_group_consistency(entries, issues)

    # 6c. 强制左右成对裁片的主纹理约束。放在普通对花之后，确保 pair 约束最终生效。
    enforce_pair_texture_constraints(entries, garment_map, pieces_payload, texture_set, issues)

    # 7. Nap（绒毛方向）一致性检查
    fabric_has_nap = brief.get("fabric", {}).get("has_nap", False) if brief else False
    nap_direction = brief.get("fabric", {}).get("nap_direction", "") if brief else ""
    if fabric_has_nap:
        # 根据 nap_direction 决定目标 rotation；nap_direction 为空时用最常见值兜底
        if nap_direction == "vertical":
            target_rotation = 0
        elif nap_direction == "horizontal":
            target_rotation = 90
        else:
            # nap_direction 未指定时，默认 vertical（服装实务上灯芯绒/丝绒等绝大多数都是经向裁）
            target_rotation = 0

        for entry in entries:
            base = entry.get("base", {})
            if base and base.get("fill_type") == "texture":
                old = base.get("rotation", 0)
                if old % 180 != target_rotation % 180:
                    base["rotation"] = target_rotation
                    issues.append({
                        "type": "fixed_nap_rotation",
                        "severity": "high",
                        "piece_id": entry["piece_id"],
                        "old_rotation": old,
                        "new_rotation": target_rotation,
                        "message": f"绒毛面料(nap_direction={nap_direction or '未指定'})要求所有裁片 rotation 一致，已强制统一为 {target_rotation}°",
                    })
        # 禁止 mirror_y（会翻转绒毛方向）
        for entry in entries:
            base = entry.get("base", {})
            if base and base.get("mirror_y"):
                base["mirror_y"] = False
                issues.append({
                    "type": "fixed_nap_mirror_y",
                    "severity": "high",
                    "piece_id": entry["piece_id"],
                    "message": "绒毛面料禁止 mirror_y（会翻转绒毛方向）",
                })

    # 8. Motif 方向对齐检查（审美修正 → 只记录 warning/issue，不静默修改 rotation）
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
        texture_dir = _base_texture_direction(entry) or "transverse"
        recommended = compute_motif_rotation(geo.get("orientation", "irregular"), texture_dir, piece)
        current = overlay.get("rotation", 0)
        if abs(current - recommended) > 15:
            issues.append({
                "type": "motif_orientation_mismatch",
                "severity": "medium",
                "piece_id": entry["piece_id"],
                "current_rotation": current,
                "suggested_rotation": recommended,
                "message": f"motif 方向({geo.get('orientation')}) 与裁片方向({texture_dir}) 可能不匹配，建议 AI 审阅 rotation",
            })
        # scale 建议（不强制修改）：如果 visibility 过低，给出建议
        fit = compute_motif_fit_score(geo, piece, entry.get("garment_role", ""))
        current_scale = overlay.get("scale", 0.72)
        if fit["visibility_score"] < 0.5 and current_scale < fit["recommended_scale"]:
            issues.append({
                "type": "motif_scale_too_small",
                "severity": "medium",
                "piece_id": entry["piece_id"],
                "current_scale": current_scale,
                "suggested_scale": fit["recommended_scale"],
                "message": f"motif 在裁片内可见度({fit['visibility_score']:.2f})偏低，建议增大 scale 或更换 motif",
            })

    # 9. Motif 边界溢出检测（安全修正）
    for entry in entries:
        overlay = entry.get("overlay")
        if not overlay or overlay.get("fill_type") != "motif":
            continue
        piece = by_piece.get(entry["piece_id"], {})
        pw = piece.get("width", 1)
        ph = piece.get("height", 1)
        motif_id = overlay.get("motif_id", "")
        mw, mh = 256, 256
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
            if geo:
                mw = geo.get("pixel_width", 256)
                mh = geo.get("pixel_height", 256)
        scale = overlay.get("scale", 0.72)
        anchor = overlay.get("anchor", "center")
        offset_x = overlay.get("offset_x", 0)
        offset_y = overlay.get("offset_y", 0)
        new_scale = scale
        for _ in range(15):
            x, y, rw, rh = _compute_motif_render_bounds(pw, ph, mw, mh, new_scale, anchor, offset_x, offset_y)
            overflow_x = max(0, -x, x + rw - pw)
            overflow_y = max(0, -y, y + rh - ph)
            if overflow_x <= 0 and overflow_y <= 0:
                break
            shrink = 1.0
            if overflow_x > 0 and rw > 0:
                shrink = min(shrink, (rw - overflow_x) / rw)
            if overflow_y > 0 and rh > 0:
                shrink = min(shrink, (rh - overflow_y) / rh)
            new_scale *= max(0.5, shrink)
            if new_scale < 0.05:
                break
        if new_scale < scale:
            overlay["scale"] = round(new_scale, 3)
            issues.append({
                "type": "fixed_motif_overflow",
                "severity": "high",
                "piece_id": entry["piece_id"],
                "old_scale": scale,
                "new_scale": round(new_scale, 3),
                "message": f"motif 渲染尺寸超出裁片边界，已自动缩小 scale {scale} → {round(new_scale, 3)}",
            })

    return entries, issues


def _resync_group_consistency(entries: list[dict], issues: list) -> None:
    """对花对条后重新确保对称组/同形组内所有裁片 base 层参数一致。
    但尊重 intentional_asymmetry 声明——AI 有意不对称设计不强制同步。"""
    group_templates: dict[str, dict] = {}
    for entry in entries:
        group = entry.get("symmetry_group") or entry.get("same_shape_group")
        if not group:
            continue
        # 跳过 AI 声明的有意不对称设计
        if entry.get("intentional_asymmetry"):
            continue
        base = entry.get("base")
        if not isinstance(base, dict):
            continue
        if group not in group_templates:
            group_templates[group] = dict(base)
        else:
            template = group_templates[group]
            changed = False
            keys = [
                "fill_type", "texture_id", "solid_id", "scale", "rotation",
                "mirror_x", "mirror_y", "texture_direction", "respect_pattern_orientation",
            ]
            if not entry.get("pair_texture_constraint"):
                keys.extend(["offset_x", "offset_y"])
            for key in keys:
                if base.get(key) != template.get(key):
                    base[key] = template[key]
                    changed = True
            if changed:
                issues.append({
                    "type": "fixed_group_mismatch_post_matching",
                    "severity": "high",
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
        direction = _base_texture_direction(entry) or "transverse"
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
            if group and _pair_group_mode(group) in {"front_seam", "identical_pair"}:
                continue
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
            tex_dx = dx
            tex_dy = dy

            new_x = (anchor_ox - tex_dx) % max(1, round(tex_w * anchor_scale))
            new_y = (anchor_oy - tex_dy) % max(1, round(tex_h * anchor_scale))

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

    # 阶段 1.5：应用对称关系优化（slave 复制 master 参数，避免重复渲染）
    entries = apply_symmetry_relations(entries, garment_map, pieces_payload)

    # 阶段 2：强制校验修正
    entries, fix_issues = enforce_validation(entries, pieces_payload, texture_set, garment_map, motif_geometries, brief)

    # 重新组装 art_direction
    hero_ids = [e["piece_id"] for e in entries if (e.get("overlay") or {}).get("fill_type") == "motif"]
    quiet_ids = [e["piece_id"] for e in entries if e.get("zone") == "body" and e["piece_id"] not in hero_ids]
    secondary_ids = [e["piece_id"] for e in entries if e.get("zone") == "secondary"]
    trim_ids = [e["piece_id"] for e in entries if e.get("zone") == "trim"]

    if ai_plan_used and art_direction:
        art_direction["hero_piece_ids"] = hero_ids
        art_direction["validation_fixes"] = fix_issues
        art_direction["draft_preview_only"] = False
        art_direction["production_ready"] = True
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
            "risk_notes": ["使用后端规则生成，仅作草稿预览，不得作为生产审批稿"] if not ai_plan_used else [],
            "draft_preview_only": True,
            "production_ready": False,
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
        "草稿预览": not ai_plan_used,
        "生产就绪": ai_plan_used,
            "校验修正": len(fix_issues),
            "卖点裁片": art_direction["hero_piece_ids"],
        },
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
