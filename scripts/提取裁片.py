#!/usr/bin/env python3
"""
从服装纸样 mask（透明 PNG）中提取连通裁片，生成裁片清单与遮罩。
"""
import argparse
import json
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def load_rgba(path: str | Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def prepare_pattern_image(pattern_path: Path, out_path: Path, white_threshold: int = 245) -> Path:
    """准备纸样图：如果已有 Alpha 通道则保留，否则将纯白背景转为透明。"""
    img = load_rgba(pattern_path)
    alpha = img.getchannel("A")
    min_alpha, max_alpha = alpha.getextrema()
    if min_alpha < 250 and max_alpha > 0:
        out = img
    else:
        src = img.load()
        out = Image.new("RGBA", img.size, (0, 0, 0, 0))
        dst = out.load()
        for y in range(img.height):
            for x in range(img.width):
                r, g, b, _ = src[x, y]
                dst[x, y] = (r, g, b, 0 if r >= white_threshold and g >= white_threshold and b >= white_threshold else 255)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path)
    return out_path


def components_from_alpha(img: Image.Image, threshold: int, min_area: int) -> list[dict]:
    """基于 Alpha 通道的 BFS 连通域分析，提取面积大于 min_area 的裁片。"""
    alpha = img.getchannel("A").tobytes()
    width, height = img.size
    active = bytearray(1 if value >= threshold else 0 for value in alpha)
    seen = bytearray(width * height)
    components = []
    for idx, value in enumerate(active):
        if not value or seen[idx]:
            continue
        q = deque([idx])
        seen[idx] = 1
        pixels = []
        min_x, min_y, max_x, max_y = width, height, -1, -1
        while q:
            current = q.popleft()
            y, x = divmod(current, width)
            pixels.append(current)
            min_x, min_y = min(min_x, x), min(min_y, y)
            max_x, max_y = max(max_x, x), max(max_y, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if nx < 0 or nx >= width or ny < 0 or ny >= height:
                    continue
                nidx = ny * width + nx
                if active[nidx] and not seen[nidx]:
                    seen[nidx] = 1
                    q.append(nidx)
        if len(pixels) >= min_area:
            components.append({
                "pixels": pixels,
                "area": len(pixels),
                "bbox": {
                    "x": min_x,
                    "y": min_y,
                    "width": max_x - min_x + 1,
                    "height": max_y - min_y + 1,
                },
            })
    components.sort(key=lambda item: item["area"], reverse=True)
    return components


def guess_role(index: int, bbox: dict, area: int, total_area: int) -> str:
    """根据几何特征猜测裁片角色。"""
    aspect = bbox["width"] / max(1, bbox["height"])
    if aspect >= 3 or aspect <= 0.3:
        return "strip"
    if index == 0 or area > total_area * 0.22:
        return "main"
    if aspect < 0.55:
        return "long_panel"
    return "panel"


def write_masks(components: list[dict], image_size: tuple[int, int], out_dir: Path) -> list[dict]:
    """为每个连通域生成遮罩 PNG，并返回裁片元数据列表。"""
    width, _ = image_size
    masks_dir = out_dir / "masks"
    masks_dir.mkdir(parents=True, exist_ok=True)
    total_area = sum(item["area"] for item in components) or 1
    pieces = []
    for index, component in enumerate(components, 1):
        piece_id = f"piece_{index:03d}"
        bbox = component["bbox"]
        mask = Image.new("L", (bbox["width"], bbox["height"]), 0)
        pix = mask.load()
        for pixel_idx in component["pixels"]:
            y, x = divmod(pixel_idx, width)
            pix[x - bbox["x"], y - bbox["y"]] = 255
        mask_path = masks_dir / f"{piece_id}_mask.png"
        mask.save(mask_path)
        pieces.append(
            {
                "piece_id": piece_id,
                "piece_role": guess_role(index - 1, bbox, component["area"], total_area),
                "bbox": bbox,
                "source_x": bbox["x"],
                "source_y": bbox["y"],
                "width": bbox["width"],
                "height": bbox["height"],
                "area": component["area"],
                "aspect": round(bbox["width"] / max(1, bbox["height"]), 4),
                "mask_path": str(mask_path.resolve()),
            }
        )
    return pieces


def build_overview(prepared_path: Path, pieces: list[dict], out_path: Path) -> Path:
    """生成裁片总览图，标注每个裁片的编号与角色。"""
    img = load_rgba(prepared_path)
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.alpha_composite(img)
    draw = ImageDraw.Draw(bg)
    try:
        font = ImageFont.truetype("Arial.ttf", max(14, min(img.size) // 45))
    except Exception:
        font = ImageFont.load_default()
    for piece in pieces:
        b = piece["bbox"]
        draw.rectangle(
            [b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]],
            outline=(220, 38, 38, 255),
            width=max(2, min(img.size) // 500),
        )
        draw.text(
            (b["x"] + 6, b["y"] + 6),
            f"{piece['piece_id']} {piece['piece_role']}",
            fill=(15, 23, 42, 255),
            font=font,
        )
    bg.convert("RGB").save(out_path)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="从服装纸样 mask 中提取裁片，生成遮罩与总览图。")
    parser.add_argument("--pattern", required=True, help="输入纸样图路径（透明 PNG 或白底图）")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--min-area", type=int, default=1000, help="最小裁片面积（像素），默认 1000")
    parser.add_argument("--alpha-threshold", type=int, default=16, help="Alpha 通道阈值，默认 16")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_pattern_image(Path(args.pattern), out_dir / "prepared_pattern.png")
    img = load_rgba(prepared)
    pieces = write_masks(components_from_alpha(img, args.alpha_threshold, args.min_area), img.size, out_dir)
    overview = build_overview(prepared, pieces, out_dir / "piece_overview.png")

    payload = {
        "pattern_image": str(Path(args.pattern).resolve()),
        "prepared_pattern": str(prepared.resolve()),
        "overview_image": str(overview.resolve()),
        "canvas": {"width": img.width, "height": img.height, "unit": "px"},
        "pieces": pieces,
    }
    pieces_json = out_dir / "pieces.json"
    pieces_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        {"裁片数量": len(pieces), "裁片清单": str(pieces_json.resolve()), "总览图": str(overview.resolve())},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
