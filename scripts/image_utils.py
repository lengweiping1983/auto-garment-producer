#!/usr/bin/env python3
"""图像工具函数：缩略图、Kimi payload 预算等。"""
import hashlib
import json
import math
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None


KIMI_TOTAL_BUDGET_BYTES = 700 * 1024
KIMI_SINGLE_IMAGE_BUDGET_BYTES = 180 * 1024
KIMI_MAX_IMAGES = 6


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def _flatten_for_kimi(im: Image.Image, bg=(246, 246, 242)) -> Image.Image:
    """Kimi 看图不需要 alpha；透明图用浅底合成后保存 JPEG，避免 PNG 过大。"""
    if _has_alpha(im):
        rgba = im.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, bg + (255,))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return im.convert("RGB")


def ensure_thumbnail(
    image_path: str | Path,
    max_size: int = 256,
    *,
    provider: str = "",
    quality: int | None = None,
    max_bytes: int | None = None,
) -> Path:
    """为图片生成缩略图，避免发送超大图导致 413。

    策略：
    - 原图 < 200KB 且长边不超过 max_size*1.5 时直接返回原图
    - 有透明通道的保存为 PNG（保留透明度）
    - 无透明通道的保存为 JPEG（更小，quality=85）
    - provider="kimi" 时始终输出 JPEG；透明图合成到浅色背景，不保留 alpha
    - 缩略图保存在原图同目录的 .thumbnails/ 中，文件名含内容/mtime/size 指纹，复用已存在的有效缩略图

    9 张面料缩略图总量约 100-300KB，远低于 nginx 1MB 限制。
    """
    src = Path(image_path).resolve()
    if not src.exists():
        return src
    if Image is None:
        return src

    try:
        stat = src.stat()
        with Image.open(src) as probe:
            width, height = probe.size
            if provider != "kimi" and stat.st_size < 200 * 1024 and max(width, height) <= max_size * 1.5:
                return src
    except Exception:
        return src

    thumb_dir = src.parent / ".thumbnails"
    thumb_dir.mkdir(exist_ok=True)

    provider_tag = f"_{provider}" if provider else ""
    max_bytes = max_bytes or (KIMI_SINGLE_IMAGE_BUDGET_BYTES if provider == "kimi" else 0)
    quality = quality if quality is not None else (82 if provider == "kimi" else 85)
    if provider == "kimi":
        ext = ".jpg"
    else:
        try:
            with Image.open(src) as im:
                ext = ".png" if _has_alpha(im) else ".jpg"
        except Exception:
            ext = ".png"
    stat = src.stat()
    source_fingerprint = hashlib.sha1(
        f"{src.name}:{stat.st_size}:{stat.st_mtime_ns}:{max_size}:{provider}:{quality}:{max_bytes}".encode("utf-8")
    ).hexdigest()[:10]
    thumb_path = thumb_dir / f"{src.stem}_{max_size}px{provider_tag}_{source_fingerprint}{ext}"
    if thumb_path.exists():
        return thumb_path

    try:
        with Image.open(src) as im:
            im.thumbnail((max_size, max_size), Image.LANCZOS)
            if provider == "kimi":
                rgb = _flatten_for_kimi(im)
                current_quality = quality
                current_size = max_size
                while True:
                    rgb.save(thumb_path, quality=current_quality, optimize=True, progressive=True)
                    if not max_bytes or thumb_path.stat().st_size <= max_bytes or (current_quality <= 54 and current_size <= 160):
                        break
                    if current_quality > 58:
                        current_quality -= 8
                    else:
                        current_size = max(160, int(current_size * 0.82))
                        with Image.open(src) as retry:
                            retry.thumbnail((current_size, current_size), Image.LANCZOS)
                            rgb = _flatten_for_kimi(retry)
            elif _has_alpha(im):
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA")
                im.save(thumb_path, optimize=True)
            else:
                rgb = im.convert("RGB") if im.mode != "RGB" else im
                rgb.save(thumb_path, quality=85, optimize=True)
        return thumb_path
    except Exception:
        return src


def make_contact_sheet(
    items: list[dict],
    out_path: str | Path,
    *,
    cell_size: int = 192,
    provider: str = "kimi",
    title: str = "texture assets",
) -> dict:
    """把多张资产图拼成一张 Kimi 友好的 contact sheet。

    items: [{"id": "main_a", "path": "...", "role": "..."}]
    返回包含 sheet_path、items 映射和尺寸的 dict。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if Image is None or not items:
        return {"sheet_path": "", "items": [], "image_count": 0}

    prepared = []
    for idx, item in enumerate(items, 1):
        path = Path(item.get("path", ""))
        if not path.exists():
            continue
        prepared.append({
            "index": idx,
            "asset_id": item.get("id", f"asset_{idx:02d}"),
            "role": item.get("role", ""),
            "path": str(path.resolve()),
        })
    if not prepared:
        return {"sheet_path": "", "items": [], "image_count": 0}

    cols = min(6, max(1, math.ceil(math.sqrt(len(prepared)))))
    rows = math.ceil(len(prepared) / cols)
    label_h = 34
    pad = 8
    header_h = 30
    sheet_w = cols * cell_size + (cols + 1) * pad
    sheet_h = header_h + rows * (cell_size + label_h) + (rows + 1) * pad
    sheet = Image.new("RGB", (sheet_w, sheet_h), (248, 248, 244))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    draw.text((pad, 8), title[:100], fill=(40, 40, 40), font=font)

    for i, item in enumerate(prepared):
        row, col = divmod(i, cols)
        x = pad + col * (cell_size + pad)
        y = header_h + pad + row * (cell_size + label_h + pad)
        try:
            with Image.open(item["path"]) as im:
                rgb = _flatten_for_kimi(im)
                rgb.thumbnail((cell_size, cell_size), Image.LANCZOS)
                bg = Image.new("RGB", (cell_size, cell_size), (255, 255, 255))
                bg.paste(rgb, ((cell_size - rgb.width) // 2, (cell_size - rgb.height) // 2))
                sheet.paste(bg, (x, y))
        except Exception:
            draw.rectangle((x, y, x + cell_size, y + cell_size), outline=(180, 0, 0))
        label = f"{item['index']:02d} {item['asset_id']} {item['role']}".strip()
        draw.text((x, y + cell_size + 4), label[:42], fill=(20, 20, 20), font=font)

    quality = 82
    current_sheet = sheet
    while True:
        current_sheet.save(out, quality=quality, optimize=True, progressive=True)
        if out.stat().st_size <= KIMI_SINGLE_IMAGE_BUDGET_BYTES:
            break
        if quality > 54:
            quality -= 8
            continue
        if max(current_sheet.size) <= 720:
            break
        scale = 0.86
        current_sheet = current_sheet.resize(
            (max(1, int(current_sheet.width * scale)), max(1, int(current_sheet.height * scale))),
            Image.Resampling.LANCZOS,
        )

    return {
        "sheet_path": str(out.resolve()),
        "items": prepared,
        "image_count": len(prepared),
        "cell_size": cell_size,
        "bytes": out.stat().st_size,
    }


def estimate_payload_budget(
    prompt_path: str | Path | None = None,
    image_paths: list[str | Path] | None = None,
    *,
    total_budget: int = KIMI_TOTAL_BUDGET_BYTES,
    single_image_budget: int = KIMI_SINGLE_IMAGE_BUDGET_BYTES,
    max_images: int = KIMI_MAX_IMAGES,
) -> dict:
    """估算 Kimi 请求体预算，提前发现 413 风险。"""
    image_paths = image_paths or []
    prompt_bytes = 0
    if prompt_path:
        pp = Path(prompt_path)
        if pp.exists():
            prompt_bytes = pp.stat().st_size
    images = []
    image_bytes = 0
    for p in image_paths:
        path = Path(p)
        if not path.exists():
            images.append({"path": str(path), "bytes": 0, "missing": True, "over_single_budget": False})
            continue
        size = path.stat().st_size
        image_bytes += size
        images.append({
            "path": str(path.resolve()),
            "bytes": size,
            "missing": False,
            "over_single_budget": size > single_image_budget,
        })
    largest = max(images, key=lambda item: item.get("bytes", 0), default=None)
    estimated_total = prompt_bytes + image_bytes
    over_budget = estimated_total > total_budget or len([i for i in images if not i.get("missing")]) > max_images or any(i.get("over_single_budget") for i in images)
    return {
        "provider": "kimi",
        "prompt_bytes": prompt_bytes,
        "image_bytes": image_bytes,
        "image_count": len([i for i in images if not i.get("missing")]),
        "largest_image": largest,
        "estimated_total_bytes": estimated_total,
        "total_budget_bytes": total_budget,
        "single_image_budget_bytes": single_image_budget,
        "max_images": max_images,
        "over_budget": over_budget,
        "images": images,
    }


def print_payload_budget_warning(budget: dict) -> None:
    if not budget or not budget.get("over_budget"):
        return
    largest = budget.get("largest_image") or {}
    print(json.dumps({
        "Kimi请求体超预算": True,
        "说明": "不要直接调用 Kimi；请先压缩图片、使用 contact sheet 或减少图片数量，避免 nginx 413。",
        "estimated_total_bytes": budget.get("estimated_total_bytes"),
        "image_count": budget.get("image_count"),
        "largest_image": largest.get("path"),
        "largest_image_bytes": largest.get("bytes"),
        "total_budget_bytes": budget.get("total_budget_bytes"),
        "single_image_budget_bytes": budget.get("single_image_budget_bytes"),
    }, ensure_ascii=False, indent=2))
