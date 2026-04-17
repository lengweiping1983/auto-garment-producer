#!/usr/bin/env python3
"""
根据服装部位映射、面料组合和商业设计简报，创建艺术指导裁片填充计划。
"""
import argparse
import json
from pathlib import Path


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
        mapped.append({
            "piece_id": piece["piece_id"],
            "garment_role": role,
            "zone": zone,
            "symmetry_group": "",
            "same_shape_group": "",
            "direction_degrees": 90 if aspect >= 1.8 else 0,
            "confidence": confidence,
            "reason": "回退几何推断",
        })
    return {
        "map_id": "fallback_garment_map",
        "method": "fallback_geometry_inference",
        "confidence": 0.48,
        "pieces": mapped,
    }


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


def create_plan(pieces_payload: dict, texture_set: dict, garment_map: dict, brief: dict) -> tuple[dict, dict]:
    """创建艺术指导方案与裁片填充计划。"""
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
                entry["overlay"] = make_layer(
                    "motif",
                    "单一卖点图案置于关键可见裁片",
                    motif_id=motif_id,
                    anchor="center",
                    scale=0.72,
                    opacity=0.92,
                    offset_y=-round(piece["height"] * 0.04),
                )
            entry["reason"] = "前片卖点区承载简化主题，不切割叙事插画"
        elif zone == "body" or role in ("back_body", "secondary_body"):
            quiet_ids.append(piece["piece_id"])
            entry["base"] = make_layer(
                "texture",
                "大身裁片使用可穿安静底纹/辅面料，对齐服装方向",
                texture_id=secondary_id if index % 2 else main_id,
                scale=1.18,
                rotation=direction,
                offset_x=params["offset_x"],
                offset_y=params["offset_y"],
            )
            entry["reason"] = "大身裁片保持低对比度，确保产品可穿"
        elif zone == "secondary" or role in ("sleeve_pair", "sleeve_or_side_panel"):
            secondary_ids.append(piece["piece_id"])
            mirror_x = bool(symmetry_group and piece["source_x"] > (pieces_payload.get("canvas", {}).get("width", 0) / 2))
            texture_id = secondary_id if piece["area"] > largest_area * 0.18 else accent_id
            entry["base"] = make_layer(
                "texture",
                "匹配或副面板使用协调纹理，共享组参数",
                texture_id=texture_id,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="为裁片创建艺术指导商业填充计划。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--garment-map", default="", help="部位映射 JSON 路径（可选）")
    parser.add_argument("--brief", default="", help="商业设计简报 JSON 路径（可选）")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    pieces_payload = load_json(args.pieces)
    texture_set = load_json(args.texture_set)
    garment_map = load_json(args.garment_map) if args.garment_map else infer_map_from_pieces(pieces_payload)
    brief = load_json(args.brief) if args.brief else {"aesthetic_direction": "商业畅销款打样"}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    art_direction, fill_plan = create_plan(pieces_payload, texture_set, garment_map, brief)
    art_path = out_dir / "art_direction_plan.json"
    fill_path = out_dir / "piece_fill_plan.json"
    art_path.write_text(json.dumps(art_direction, ensure_ascii=False, indent=2), encoding="utf-8")
    fill_path.write_text(json.dumps(fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        {"艺术指导方案": str(art_path.resolve()), "裁片填充计划": str(fill_path.resolve()), "卖点裁片": art_direction["hero_piece_ids"]},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
