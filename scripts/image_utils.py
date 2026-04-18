#!/usr/bin/env python3
"""图像工具函数：缩略图生成等。"""
from pathlib import Path

try:
    from PIL import Image
except Exception:
    Image = None


def ensure_thumbnail(image_path: str | Path, max_size: int = 256) -> Path:
    """为图片生成缩略图，避免发送超大图导致 413。

    策略：
    - 原图 < 200KB 直接返回原图
    - 有透明通道的保存为 PNG（保留透明度）
    - 无透明通道的保存为 JPEG（更小，quality=85）
    - 缩略图保存在原图同目录的 .thumbnails/ 中，复用已存在的

    9 张面料缩略图总量约 100-300KB，远低于 nginx 1MB 限制。
    """
    src = Path(image_path).resolve()
    if not src.exists():
        return src
    if src.stat().st_size < 200 * 1024:
        return src
    if Image is None:
        return src

    thumb_dir = src.parent / ".thumbnails"
    thumb_dir.mkdir(exist_ok=True)

    # 判断是否需要保留透明通道
    try:
        with Image.open(src) as im:
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
    except Exception:
        has_alpha = True  # 保守策略：出错时保留 PNG

    ext = ".png" if has_alpha else ".jpg"
    thumb_path = thumb_dir / f"{src.stem}_{max_size}px{ext}"
    if thumb_path.exists():
        return thumb_path

    try:
        with Image.open(src) as im:
            im.thumbnail((max_size, max_size), Image.LANCZOS)
            if has_alpha:
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA")
                im.save(thumb_path, optimize=True)
            else:
                rgb = im.convert("RGB") if im.mode != "RGB" else im
                rgb.save(thumb_path, quality=85, optimize=True)
        return thumb_path
    except Exception:
        return src
