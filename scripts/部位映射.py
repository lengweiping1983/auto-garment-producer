#!/usr/bin/env python3
"""
从已提取的裁片清单推断服装部位角色、对称性与置信度。
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 导入模板加载器
sys.path.insert(0, str(Path(__file__).parent))
try:
    from template_loader import (
        find_template_by_id,
        find_template_by_garment_type,
        load_template_file,
        match_pieces_to_template,
        format_template_garment_map,
        relative_json_metadata_path,
    )
    HAS_TEMPLATE_LOADER = True
except Exception as _exc:
    def relative_json_metadata_path(target: str | Path, owner_json_path: str | Path) -> str:
        import os
        return os.path.relpath(Path(target).resolve(), Path(owner_json_path).resolve().parent)

    HAS_TEMPLATE_LOADER = False


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def center(piece: dict) -> tuple[float, float]:
    return (piece["source_x"] + piece["width"] / 2, piece["source_y"] + piece["height"] / 2)


def direction_degrees(piece: dict) -> int:
    aspect = piece["width"] / max(1, piece["height"])
    if aspect >= 1.8:
        return 90
    return 0


def _fallback_direction_hint(garment_role: str, zone: str, aspect: float) -> str:
    """程序兜底的方向建议（hint 而非决定）。仅当 AI 未提供 texture_direction 时作为参考。
    子 Agent 审美决策可完全覆盖此值。"""
    if garment_role in ("front_hero", "back_body", "secondary_body"):
        return "transverse"
    if garment_role in ("sleeve_pair", "sleeve_or_side_panel", "side_or_long_panel"):
        return "longitudinal"
    if zone == "trim" or garment_role in ("trim_strip", "collar_or_upper_trim", "hem_or_lower_trim"):
        return "longitudinal" if aspect >= 1.0 else "transverse"
    if zone == "secondary":
        return "longitudinal" if aspect >= 1.2 else "transverse"
    return "transverse"


def _fallback_grain_direction(garment_role: str, zone: str, aspect: float) -> str:
    """推断裁片的经向（grain direction）：基于服装设计学知识的兜底推断。
    重要：grain 方向取决于服装部位的穿着方向，与画布上的宽高比无关。
    """
    # 袖类：grain 沿袖长方向（手臂上下）= vertical，无视画布 aspect
    if garment_role in ("sleeve_pair", "sleeve_or_side_panel"):
        return "vertical"
    # 饰边类：腰带/袖口/领条沿条带长边方向
    if garment_role in ("trim_strip", "waistband", "cuff", "neckline_rib"):
        return "horizontal"
    # 领/上饰边/下摆：通常 horizontal（沿服装宽度方向）
    if garment_role in ("collar_or_upper_trim", "hem_or_lower_trim"):
        return "horizontal"
    # 口袋盖/育克：通常 horizontal
    if garment_role in ("pocket_flap", "yoke"):
        return "horizontal"
    # body 区（前片/后片/侧片）：vertical（人体上下方向）
    if zone == "body" or garment_role in ("front_hero", "back_body", "secondary_body", "side_or_long_panel"):
        return "vertical"
    # 其他默认 vertical
    return "vertical"


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
            # 最大裁片不一定是 hero——后片可能更大。hero 应由 AI 决策。
            role, zone, confidence = "back_body", "body", 0.65
            reason.append("最大裁片回退为 back_body；hero 由 AI 审美决策指定")
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

        entry = {
            "piece_id": piece["piece_id"],
            "garment_role": role,
            "zone": zone,
            "symmetry_group": "",
            "same_shape_group": "",
            "direction_degrees": direction_degrees(piece),
            "texture_direction": "",  # fallback 路径不设默认值，由审美子 Agent 决定
            "texture_direction_hint": _fallback_direction_hint(role, zone, aspect),
            "grain_direction": _fallback_grain_direction(role, zone, aspect),
            "confidence": round(confidence, 2),
            "reason": "；".join(reason) + "；texture_direction 未设定，由 AI 审美决策决定",
        }
        # 低置信度裁片标记为需 AI 重点审核
        if confidence < 0.6:
            entry["needs_ai_review"] = True
        role_by_id[piece["piece_id"]] = entry

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
    parser = argparse.ArgumentParser(description="从裁片清单推断服装部位角色与对称性。优先模板匹配，其次AI识别，最后几何启发。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径（pieces.json）")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--ai-map", default="", help="AI子Agent输出的 ai_garment_map.json 路径。若提供，优先使用。")
    parser.add_argument("--template", default="", help="模板ID。如 children_outerwear_set。优先于 garment_type 自动匹配。")
    parser.add_argument("--template-size", default="base", help="模板尺寸变体。默认 base。")
    parser.add_argument("--template-file", default="", help="用户自定义模板 JSON 文件路径。优先于内置模板。")
    parser.add_argument("--garment-type", default="", help="服装类型。若未指定模板，尝试按 garment_type 匹配内置模板。")
    parser.add_argument("--no-template", action="store_true", help="禁用模板匹配，强制走 AI/几何推断路径。")
    args = parser.parse_args()

    pieces_payload = load_json(args.pieces)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    roles = []
    method = "geometry_inference"
    template_used = None

    # ===================== 路径1: 模板匹配（最高优先级）=====================
    if not args.no_template and HAS_TEMPLATE_LOADER:
        template = None
        # 1a. 用户自定义模板文件
        if args.template_file:
            template = load_template_file(Path(args.template_file))
            if template:
                print(f"[模板] 加载用户自定义模板: {args.template_file}")
        # 1b. 内置模板（按ID）
        if not template and args.template:
            template = find_template_by_id(args.template, args.template_size)
            if template:
                print(f"[模板] 加载内置模板: {args.template}/{args.template_size}")
        # 1c. 内置模板（按 garment_type 自动匹配）
        if not template and args.garment_type:
            template = find_template_by_garment_type(args.garment_type)
            if template:
                print(f"[模板] 按 garment_type='{args.garment_type}' 匹配到模板: {template.get('template_id', '?')}")

        if template:
            pieces = pieces_payload.get("pieces", [])
            matched, avg_conf = match_pieces_to_template(pieces, template)
            if matched and avg_conf >= 0.75:
                payload = format_template_garment_map(matched, template)
                map_path = out_dir / "garment_map.json"
                payload["pieces_json"] = relative_json_metadata_path(args.pieces, map_path)
                map_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                overview = draw_overview(pieces_payload, payload, out_dir / "garment_map_overview.jpg")
                print(json.dumps(
                    {"部位映射": str(map_path.resolve()), "总览图": str(overview.resolve()),
                     "平均置信度": payload["confidence"], "方法": "template_match"},
                    ensure_ascii=False,
                ))
                return 0
            else:
                print(f"[模板] 匹配失败 (avg_conf={avg_conf:.2f} < 0.75)，回退到 AI/几何推断")
                template_used = template

    # ===================== 路径2: AI识别 =====================
    ai_map_path = Path(args.ai_map) if args.ai_map else None
    if ai_map_path and ai_map_path.exists():
        try:
            ai_map = load_json(ai_map_path)
            roles = []
            ai_by_id = {p["piece_id"]: p for p in ai_map.get("pieces", [])}
            for piece in pieces_payload.get("pieces", []):
                pid = piece["piece_id"]
                ai = ai_by_id.get(pid, {})
                if ai:
                    aspect = piece["width"] / max(1, piece["height"])
                    role = ai.get("garment_role", "small_detail")
                    zone = ai.get("zone", "detail")
                    ai_dir = ai.get("texture_direction", "")
                    entry = {
                        "piece_id": pid,
                        "garment_role": role,
                        "zone": zone,
                        "symmetry_group": ai.get("symmetry_group", ""),
                        "same_shape_group": ai.get("same_shape_group", ""),
                        "direction_degrees": direction_degrees(piece),
                        "texture_direction": ai_dir if ai_dir in ("transverse", "longitudinal") else "",
                        "texture_direction_hint": _fallback_direction_hint(role, zone, aspect) if ai_dir not in ("transverse", "longitudinal") else "",
                        "grain_direction": ai.get("grain_direction", _fallback_grain_direction(role, zone, aspect)),
                        "confidence": ai.get("confidence", 0.7),
                        "reason": ai.get("reason", "AI识别"),
                    }
                else:
                    aspect = piece["width"] / max(1, piece["height"])
                    entry = {
                        "piece_id": pid,
                        "garment_role": "small_detail",
                        "zone": "detail",
                        "symmetry_group": "",
                        "same_shape_group": "",
                        "direction_degrees": direction_degrees(piece),
                        "texture_direction": "",
                        "texture_direction_hint": _fallback_direction_hint("small_detail", "detail", aspect),
                        "grain_direction": _fallback_grain_direction("small_detail", "detail", aspect),
                        "confidence": 0.5,
                        "reason": "AI未覆盖，回退",
                    }
                roles.append(entry)
            method = "ai_driven"
            print(f"[部位映射] 使用AI识别结果: {ai_map_path}")
        except Exception as exc:
            print(f"[警告] AI识别结果处理失败 ({exc})，回退到几何启发")
            roles = infer_roles(pieces_payload)
            method = "geometry_inference"
    else:
        roles = infer_roles(pieces_payload)
        method = "geometry_inference"

    confidence = round(sum(item["confidence"] for item in roles) / max(1, len(roles)), 2)
    payload = {
        "map_id": "garment_map_v1",
        "method": method,
        "confidence": confidence,
        "pieces": roles,
    }
    map_path = out_dir / "garment_map.json"
    payload["pieces_json"] = relative_json_metadata_path(args.pieces, map_path)
    map_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    overview = draw_overview(pieces_payload, payload, out_dir / "garment_map_overview.jpg")
    print(json.dumps(
        {"部位映射": str(map_path.resolve()), "总览图": str(overview.resolve()), "平均置信度": confidence},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
