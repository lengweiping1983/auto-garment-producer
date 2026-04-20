#!/usr/bin/env python3
"""Create front-left/front-right motif assets from the primary theme image."""
import json
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat


def _has_meaningful_alpha(rgba: Image.Image) -> bool:
    alpha = rgba.getchannel("A")
    extrema = alpha.getextrema()
    if extrema[0] < 245:
        bbox = alpha.getbbox()
        if not bbox:
            return False
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        return area < rgba.width * rgba.height * 0.995
    return False


def _edge_pixels(rgb: Image.Image) -> list[tuple[int, int, int]]:
    w, h = rgb.size
    step = max(1, min(w, h) // 96)
    pixels = rgb.load()
    out = []
    for x in range(0, w, step):
        out.append(pixels[x, 0])
        out.append(pixels[x, h - 1])
    for y in range(0, h, step):
        out.append(pixels[0, y])
        out.append(pixels[w - 1, y])
    return out


def _is_background_like(color: tuple[int, int, int]) -> bool:
    r, g, b = color
    spread = max(color) - min(color)
    avg = (r + g + b) / 3
    return spread <= 42 or avg >= 232


def _quantize(color: tuple[int, int, int], step: int = 16) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(v / step) * step)) for v in color)


def _edge_background_palette(rgb: Image.Image) -> list[tuple[int, int, int]]:
    samples = [c for c in _edge_pixels(rgb) if _is_background_like(c)]
    if not samples:
        return []
    counts: dict[tuple[int, int, int], int] = {}
    for color in samples:
        key = _quantize(color)
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    edge_count = max(1, len(_edge_pixels(rgb)))
    palette = [color for color, count in ranked[:6] if count / edge_count >= 0.015]
    return palette


def _remove_false_transparency_background(img: Image.Image) -> Image.Image:
    """Turn common AI fake-transparent checker/flat edge backgrounds into real alpha."""
    rgba = img.convert("RGBA")
    if _has_meaningful_alpha(rgba):
        return rgba

    rgb = rgba.convert("RGB")
    palette = _edge_background_palette(rgb)
    if not palette:
        return rgba

    src = rgba.load()
    alpha = Image.new("L", rgba.size, 255)
    alpha_px = alpha.load()
    threshold = 54
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, _ = src[x, y]
            for bg in palette:
                dist = abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2])
                if dist <= threshold and _is_background_like((r, g, b)):
                    alpha_px[x, y] = 0
                    break
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.45))
    rgba.putalpha(alpha)
    return rgba


def _background_sample(img: Image.Image) -> tuple[int, int, int]:
    rgb = img.convert("RGB")
    w, h = rgb.size
    sample = Image.new("RGB", (1, 1))
    pts = [
        rgb.crop((0, 0, max(1, w // 8), max(1, h // 8))),
        rgb.crop((max(0, w - w // 8), 0, w, max(1, h // 8))),
        rgb.crop((0, max(0, h - h // 8), max(1, w // 8), h)),
        rgb.crop((max(0, w - w // 8), max(0, h - h // 8), w, h)),
    ]
    colors = []
    for crop in pts:
        stat = ImageStat.Stat(crop)
        colors.append(tuple(round(v) for v in stat.mean[:3]))
    sample.putpixel((0, 0), tuple(round(sum(c[i] for c in colors) / len(colors)) for i in range(3)))
    return sample.getpixel((0, 0))


def _subject_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    rgba = img.convert("RGBA")
    alpha_bbox = rgba.getchannel("A").getbbox()
    if alpha_bbox:
        return alpha_bbox

    bg = _background_sample(rgba)
    rgb = rgba.convert("RGB")
    w, h = rgb.size
    pixels = rgb.load()
    xs, ys = [], []
    threshold = 42
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > threshold:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    bbox = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    if area < w * h * 0.03:
        return None
    return bbox


def _crop_subject(img: Image.Image) -> Image.Image:
    rgba = _remove_false_transparency_background(img)
    w, h = rgba.size
    bbox = _subject_bbox(rgba)
    if not bbox:
        side_w = round(w * 0.82)
        side_h = round(h * 0.82)
        left = max(0, (w - side_w) // 2)
        top = max(0, (h - side_h) // 2)
        bbox = (left, top, min(w, left + side_w), min(h, top + side_h))
    pad = max(8, round(min(w, h) * 0.04))
    bbox = (
        max(0, bbox[0] - pad),
        max(0, bbox[1] - pad),
        min(w, bbox[2] + pad),
        min(h, bbox[3] + pad),
    )
    return rgba.crop(bbox)


def _crop_half_to_seam_alpha(half: Image.Image, seam_side: str) -> Image.Image:
    """Trim transparent padding so the visible half touches the seam edge."""
    rgba = half.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if not bbox:
        return rgba

    pad = max(2, round(min(rgba.size) * 0.015))
    top = max(0, bbox[1] - pad)
    bottom = min(rgba.height, bbox[3] + pad)
    if seam_side == "right":
        left = max(0, bbox[0] - pad)
        right = bbox[2]
    else:
        left = bbox[0]
        right = min(rgba.width, bbox[2] + pad)
    if right <= left or bottom <= top:
        return rgba
    return rgba.crop((left, top, right, bottom))


def create_front_split_assets(theme_image: str | Path, out_dir: str | Path) -> dict:
    """Crop the primary subject and split it into left/right front motifs."""
    out_dir = Path(out_dir)
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(theme_image) as src:
        subject = _crop_subject(src)
    mid = max(1, subject.width // 2)
    overlap = min(8, max(2, round(subject.width * 0.01))) if subject.width > 4 else 0
    left = subject.crop((0, 0, min(subject.width, mid + overlap), subject.height))
    right = subject.crop((max(0, mid - overlap), 0, subject.width, subject.height))
    left = _crop_half_to_seam_alpha(left, "right")
    right = _crop_half_to_seam_alpha(right, "left")
    left_path = assets_dir / "theme_front_left.png"
    right_path = assets_dir / "theme_front_right.png"
    left.save(left_path)
    right.save(right_path)
    return {
        "left": str(left_path.resolve()),
        "right": str(right_path.resolve()),
        "source": str(Path(theme_image).resolve()),
        "split_overlap_px": overlap,
    }


def inject_front_split_motifs(texture_set_path: str | Path, split_assets: dict) -> Path:
    """Register generated front motifs in texture_set.json."""
    path = Path(texture_set_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    motifs = [m for m in data.get("motifs", []) if m.get("motif_id") not in {"theme_front_left", "theme_front_right"}]
    motifs.extend([
        {
            "motif_id": "theme_front_left",
            "texture_id": "theme_front_left",
            "path": split_assets["left"],
            "role": "front_left_theme",
            "approved": True,
            "candidate": False,
            "prompt": "主题/AI主图主体左半，程序生成",
            "model": "deterministic-theme-split",
            "seed": "",
        },
        {
            "motif_id": "theme_front_right",
            "texture_id": "theme_front_right",
            "path": split_assets["right"],
            "role": "front_right_theme",
            "approved": True,
            "candidate": False,
            "prompt": "主题/AI主图主体右半，程序生成",
            "model": "deterministic-theme-split",
            "seed": "",
        },
    ])
    data["motifs"] = motifs
    data["theme_front_split"] = split_assets
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
