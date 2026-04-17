#!/usr/bin/env python3
"""
从已提取的裁片清单推断服装部位角色、对称性与置信度。
"""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def center(piece: dict) -> tuple[float, float]:
    return (piece["source_x"] + piece["width"] / 2, piece["source_y"] + piece["height"] / 2)


def direction_degrees(piece: dict) -> int:
    aspect = piece["width"] / max(1, piece["height"])
    if aspect >= 1.8:
        return 90
    return 0


def texture_direction(garment_role: str, zone: str, aspect: float) -> str:
    """推断裁片的纹理方向：transverse（横向）或 longitudinal（纵向）。"""
    if garment_role in ("front_hero", "back_body", "secondary_body"):
        return "transverse"
    if garment_role in ("sleeve_pair", "sleeve_or_side_panel", "side_or_long_panel"):
        return "longitudinal"
    if zone == "trim" or garment_role in ("trim_strip", "collar_or_upper_trim", "hem_or_lower_trim"):
        # 饰边沿长边方向
        return "longitudinal" if aspect >= 1.0 else "transverse"
    if zone == "secondary":
        return "longitudinal" if aspect >= 1.2 else "transverse"
    return "transverse"


def infer_roles(pieces_payload: dict) -> list[dict]:
    """基于几何特征推断每个裁片的服装部位角色。"""
    pieces = pieces_payload.get("pieces", [])
    canvas = pieces_payload.get("canvas", {})
    canvas_w = canvas.get("width") or max(p["source_x"] + p["width"] for p in pieces)
    canvas_h = canvas.get("height") or max(p["source_y"] + p["height"] for p in pieces)
    cx = canvas_w / 2
    sorted_by_area = sorted(pieces, key=lambda p: p["area"], reverse=True)
    largest_id = sorted_by_area[0]["piece_id"] if sorted_by_area else ""
    second_id = sorted_by_area[1]["piece_id"] if len(sorted_by_area) > 1 else ""
    largest_area = sorted_by_area[0]["area"] if sorted_by_area else 1
    average_area = sum(p["area"] for p in pieces) / max(1, len(pieces))

    role_by_id = {}
    for index, piece in enumerate(sorted_by_area):
        piece_cx, piece_cy = center(piece)
        aspect = piece["width"] / max(1, piece["height"])
        area_ratio = piece["area"] / max(1, largest_area)
        reason = []
        confidence = 0.58
        role = "small_detail"
        zone = "detail"
        narrow = aspect >= 3.0 or aspect <= 0.34
        small_enough_for_trim = area_ratio < 0.08 or piece["area"] < average_area * 0.45

        if narrow and small_enough_for_trim:
            role, zone, confidence = "trim_strip", "trim", 0.78
            reason.append("小型窄长饰边几何形状")
        elif piece["piece_id"] == largest_id:
            role, zone, confidence = "front_hero", "body", 0.78
            reason.append("最大裁片选为商业卖点区域")
        elif piece["piece_id"] == second_id and area_ratio > 0.48:
            role, zone, confidence = "back_body", "body", 0.68
            reason.append("第二大身尺制裁片")
        elif piece["area"] >= average_area * 0.75 or (narrow and area_ratio >= 0.08):
            if narrow:
                role, zone, confidence = "side_or_long_panel", "secondary", 0.66
                reason.append("大型长条面板；不按纯色饰边处理")
            elif abs(piece_cx - cx) > canvas_w * 0.15:
                role, zone, confidence = "sleeve_or_side_panel", "secondary", 0.62
                reason.append("大型偏心面板")
            else:
                role, zone, confidence = "secondary_body", "body", 0.62
                reason.append("大型居中面板")
        elif piece_cy < canvas_h * 0.22:
            role, zone, confidence = "collar_or_upper_trim", "trim", 0.56
            reason.append("小型上方裁片")
        elif piece_cy > canvas_h * 0.78:
            role, zone, confidence = "hem_or_lower_trim", "trim", 0.56
            reason.append("小型下方裁片")
        else:
            role, zone, confidence = "small_detail", "detail", 0.52
            reason.append("小型或中型细节裁片")

        role_by_id[piece["piece_id"]] = {
            "piece_id": piece["piece_id"],
            "garment_role": role,
            "zone": zone,
            "symmetry_group": "",
            "same_shape_group": "",
            "direction_degrees": direction_degrees(piece),
            "texture_direction": texture_direction(role, zone, aspect),
            "confidence": round(confidence, 2),
            "reason": "；".join(reason),
        }

    # 将尺寸相近的偏心裁片配对为袖对/侧片对称组
    group_index = 1
    unpaired = sorted(pieces, key=lambda p: p["source_x"])
    for left in unpaired:
        if role_by_id[left["piece_id"]]["symmetry_group"]:
            continue
        lx, ly = center(left)
        best = None
        best_score = 999.0
        for right in unpaired:
            if right["piece_id"] == left["piece_id"] or role_by_id[right["piece_id"]]["symmetry_group"]:
                continue
            rx, ry = center(right)
            if (lx - cx) * (rx - cx) >= 0:
                continue
            area_delta = abs(left["area"] - right["area"]) / max(left["area"], right["area"])
            y_delta = abs(ly - ry) / max(1, canvas_h)
            size_delta = (
                abs(left["width"] - right["width"]) / max(left["width"], right["width"])
                + abs(left["height"] - right["height"]) / max(left["height"], right["height"])
            )
            score = area_delta + y_delta + size_delta
            if score < best_score:
                best = right
                best_score = score
        if best and best_score < 0.34:
            group = f"sym_{group_index:02d}"
            group_index += 1
            for item in (left, best):
                entry = role_by_id[item["piece_id"]]
                entry["symmetry_group"] = group
                if entry["zone"] not in ("trim", "body"):
                    entry["garment_role"] = "sleeve_pair"
                    entry["zone"] = "secondary"
                entry["confidence"] = max(entry["confidence"], 0.7)
                entry["reason"] += "；通过镜像几何配对"

    # 按近同尺寸分组重复纸样裁片（即使源布局并非以画布中心镜像）
    shape_group_index = 1
    grouped: set[str] = set()
    for piece in sorted(pieces, key=lambda p: (-p["area"], p["piece_id"])):
        if piece["piece_id"] in grouped:
            continue
        matches = [piece]
        for other in pieces:
            if other["piece_id"] == piece["piece_id"] or other["piece_id"] in grouped:
                continue
            area_delta = abs(piece["area"] - other["area"]) / max(piece["area"], other["area"])
            width_delta = abs(piece["width"] - other["width"]) / max(piece["width"], other["width"])
            height_delta = abs(piece["height"] - other["height"]) / max(piece["height"], other["height"])
            if area_delta <= 0.08 and width_delta <= 0.08 and height_delta <= 0.08:
                matches.append(other)
        if len(matches) < 2:
            continue
        group = f"shape_{shape_group_index:02d}"
        shape_group_index += 1
        for item in matches:
            grouped.add(item["piece_id"])
            entry = role_by_id[item["piece_id"]]
            entry["same_shape_group"] = group
            if entry["zone"] == "detail":
                entry["zone"] = "secondary"
                entry["garment_role"] = "matched_panel"
            entry["confidence"] = max(entry["confidence"], 0.72)
            entry["reason"] += "；按近同纸样形状分组"

    return [role_by_id[p["piece_id"]] for p in pieces]


def draw_overview(pieces_payload: dict, garment_map: dict, out_path: Path) -> Path:
    """生成部位映射总览图。"""
    canvas = pieces_payload.get("canvas", {})
    width = int(canvas.get("width") or max(p["source_x"] + p["width"] for p in pieces_payload["pieces"]))
    height = int(canvas.get("height") or max(p["source_y"] + p["height"] for p in pieces_payload["pieces"]))
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("Arial.ttf", max(16, min(width, height) // 70))
    except Exception:
        font = ImageFont.load_default()
    by_id = {item["piece_id"]: item for item in garment_map["pieces"]}
    colors = {
        "body": (42, 112, 88),
        "secondary": (62, 117, 180),
        "trim": (110, 82, 44),
        "detail": (150, 82, 150),
    }
    for piece in pieces_payload.get("pieces", []):
        item = by_id[piece["piece_id"]]
        b = piece["bbox"]
        color = colors.get(item["zone"], (40, 40, 40))
        draw.rectangle(
            [b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]],
            outline=color,
            width=max(3, min(width, height) // 400),
        )
        label = f"{piece['piece_id']} {item['garment_role']} {item['confidence']:.2f}"
        draw.text((b["x"] + 8, b["y"] + 8), label, fill=color, font=font)
    image.save(out_path, quality=95)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="从裁片清单推断服装部位角色与对称性。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径（pieces.json）")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    pieces_payload = load_json(args.pieces)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    roles = infer_roles(pieces_payload)
    confidence = round(sum(item["confidence"] for item in roles) / max(1, len(roles)), 2)
    payload = {
        "map_id": "garment_map_v1",
        "method": "geometry_inference",
        "confidence": confidence,
        "pieces": roles,
    }
    map_path = out_dir / "garment_map.json"
    map_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    overview = draw_overview(pieces_payload, payload, out_dir / "garment_map_overview.jpg")
    print(json.dumps(
        {"部位映射": str(map_path.resolve()), "总览图": str(overview.resolve()), "平均置信度": confidence},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
