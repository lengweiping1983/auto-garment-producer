#!/usr/bin/env python3
"""预处理内置模板资产：校验 PNG mask，生成 Kimi JPEG 预览和 manifest。

注意：本脚本不删除、不替换、不有损压缩任何 mask PNG。
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from image_utils import KIMI_SINGLE_IMAGE_BUDGET_BYTES
from template_loader import load_index, resolve_asset_path

try:
    from PIL import Image
except Exception:
    Image = None


SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = SKILL_DIR / "templates"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path, base: Path) -> str:
    return str(path.resolve().relative_to(base.resolve()))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_kimi_jpeg(src: Path, dst: Path, max_size: int) -> dict:
    """Create deterministic Kimi JPEG preview with a fixed path."""
    if Image is None:
        raise RuntimeError("Pillow 不可用，无法生成 Kimi JPEG")
    with Image.open(src) as im:
        im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
        if im.mode == "RGBA":
            bg = Image.new("RGBA", im.size, (246, 246, 242, 255))
            bg.alpha_composite(im)
            im = bg.convert("RGB")
        im.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        quality = 82
        while True:
            im.save(dst, "JPEG", quality=quality, optimize=True)
            if dst.stat().st_size <= KIMI_SINGLE_IMAGE_BUDGET_BYTES or quality <= 55:
                break
            quality -= 7
    with Image.open(dst) as out:
        width, height = out.size
    return {
        "path": dst.name,
        "bytes": dst.stat().st_size,
        "width": width,
        "height": height,
        "quality": quality,
        "max_size": max_size,
        "sha256": sha256_file(dst),
    }


def inspect_mask(mask_path: Path) -> dict:
    if Image is None:
        raise RuntimeError("Pillow 不可用，无法校验 mask")
    with Image.open(mask_path) as im:
        converted = im.convert("L")
        hist = converted.histogram()
        total = sum(hist) or 1
        binary_pixels = hist[0] + hist[255]
        non_binary_pixels = total - binary_pixels
        width, height = converted.size
        extrema = converted.getextrema()
        mode = im.mode
    return {
        "path": str(mask_path.name),
        "width": width,
        "height": height,
        "mode": mode,
        "bytes": mask_path.stat().st_size,
        "sha256": sha256_file(mask_path),
        "extrema": list(extrema),
        "binary_pixel_ratio": round(binary_pixels / total, 6),
        "non_binary_pixels": non_binary_pixels,
        "valid_binary_mask": non_binary_pixels == 0 and extrema == (0, 255),
    }


def resolve_size_dirs(template_id: str = "", size: str = "s", all_templates: bool = False) -> list[Path]:
    if all_templates:
        template_ids = [entry.get("template_id", "") for entry in load_index().get("templates", []) if entry.get("template_id")]
    elif template_id:
        template_ids = [template_id]
    else:
        template_ids = [entry.get("template_id", "") for entry in load_index().get("templates", []) if entry.get("template_id")]

    result = []
    for tid in template_ids:
        template_dir = TEMPLATES_DIR / tid
        if not template_dir.exists():
            continue
        if size:
            candidate = template_dir / size
            if candidate.exists():
                result.append(candidate)
            continue
        for child in sorted(template_dir.iterdir()):
            if child.is_dir() and (child / f"pieces_{child.name}.json").exists():
                result.append(child)
    return result


def preprocess_size_dir(size_dir: Path, check_only: bool = False) -> dict:
    size_label = size_dir.name
    template_id = size_dir.parent.name
    pieces_path = size_dir / f"pieces_{size_label}.json"
    if not pieces_path.exists():
        return {"template_id": template_id, "size_label": size_label, "ok": False, "errors": [f"missing {pieces_path.name}"]}

    pieces_payload = load_json(pieces_path)
    errors = []
    production_masks = []
    for piece in pieces_payload.get("pieces", []):
        mask_ref = piece.get("mask_path", "")
        mask_path = resolve_asset_path(mask_ref, size_dir)
        if not mask_path.exists():
            errors.append(f"missing mask: {mask_ref}")
            continue
        info = inspect_mask(mask_path)
        info["piece_id"] = piece.get("piece_id", "")
        info["path"] = rel(mask_path, size_dir)
        if not info["valid_binary_mask"]:
            errors.append(f"mask is not strict binary: {mask_ref}")
        production_masks.append(info)

    prepared_ref = pieces_payload.get("prepared_pattern", f"prepared_pattern_{size_label}.png")
    overview_ref = pieces_payload.get("overview_image", f"piece_overview_{size_label}.png")
    prepared_path = resolve_asset_path(prepared_ref, size_dir)
    overview_path = resolve_asset_path(overview_ref, size_dir)
    garment_map_path = size_dir / f"garment_map_{size_label}.json"
    garment_map_overview_path = size_dir / f"garment_map_overview_{size_label}.jpg"

    source_images = {}
    hashes = {"pieces": sha256_file(pieces_path)}
    for key, path in (("prepared_pattern", prepared_path), ("piece_overview", overview_path)):
        if path.exists():
            source_images[key] = rel(path, size_dir)
            hashes[key] = sha256_file(path)
        else:
            errors.append(f"missing source image: {path.name}")

    garment_map = {}
    if garment_map_path.exists():
        garment_map["path"] = rel(garment_map_path, size_dir)
        hashes["garment_map"] = sha256_file(garment_map_path)
    else:
        errors.append(f"missing {garment_map_path.name}")
    if garment_map_overview_path.exists():
        garment_map["overview"] = rel(garment_map_overview_path, size_dir)
        hashes["garment_map_overview"] = sha256_file(garment_map_overview_path)

    ai_previews = {}
    if not check_only:
        if overview_path.exists():
            ai_previews["piece_overview_kimi"] = save_kimi_jpeg(
                overview_path,
                size_dir / f"piece_overview_{size_label}_kimi.jpg",
                512,
            )
        if prepared_path.exists():
            ai_previews["prepared_pattern_kimi"] = save_kimi_jpeg(
                prepared_path,
                size_dir / f"prepared_pattern_{size_label}_kimi.jpg",
                512,
            )
        if garment_map_overview_path.exists():
            ai_previews["garment_map_overview_kimi"] = save_kimi_jpeg(
                garment_map_overview_path,
                size_dir / f"garment_map_overview_{size_label}_kimi.jpg",
                384,
            )

    manifest = {
        "template_id": template_id,
        "size_label": size_label,
        "version": 1,
        "pieces": pieces_path.name,
        "production_masks": production_masks,
        "source_images": source_images,
        "ai_previews": {key: value["path"] for key, value in ai_previews.items()},
        "ai_preview_details": ai_previews,
        "garment_map": garment_map,
        "hashes": hashes,
        "qc": {
            "ok": not errors,
            "errors": errors,
            "mask_count": len(production_masks),
            "mask_png_preserved": True,
        },
    }
    if not check_only:
        manifest_path = size_dir / f"template_assets_{size_label}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "template_id": template_id,
        "size_label": size_label,
        "ok": not errors,
        "errors": errors,
        "manifest": str((size_dir / f"template_assets_{size_label}.json").resolve()),
        "ai_previews": ai_previews,
        "mask_count": len(production_masks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="预处理内置模板资产，生成 Kimi JPEG 预览和 template_assets manifest。")
    parser.add_argument("--template", default="", help="模板 ID；为空时处理 index 中所有模板。")
    parser.add_argument("--size", default="s", help="模板资产目录，固定为 s。")
    parser.add_argument("--all", action="store_true", help="处理 index 中所有模板。")
    parser.add_argument("--check-only", action="store_true", help="只校验，不生成 JPEG/manifest。")
    args = parser.parse_args()

    size_dirs = resolve_size_dirs(args.template, args.size, args.all)
    if not size_dirs:
        print(json.dumps({"ok": False, "error": "未找到可处理的模板资产目录"}, ensure_ascii=False, indent=2))
        return 1

    results = [preprocess_size_dir(path, check_only=args.check_only) for path in size_dirs]
    ok = all(item.get("ok") for item in results)
    print(json.dumps({
        "ok": ok,
        "processed_count": len(results),
        "results": results,
        "note": "mask PNG 均保留为生产权威资产；JPEG 仅用于 Kimi/AI 预览。",
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
