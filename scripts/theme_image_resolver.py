#!/usr/bin/env python3
"""Resolve user-provided theme image references into a stable local file.

The desktop/chat layer can provide images in several shapes: a normal path, a
directory containing an uploaded image, a URL, a data URI/base64 payload, or an
environment variable populated by an integration.  The garment pipeline needs a
real local path, so this module normalizes those inputs before visual analysis.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
ENV_IMAGE_KEYS = (
    "AUTO_GARMENT_THEME_IMAGE",
    "AUTO_GARMENT_THEME_IMAGES",
    "CODEX_THEME_IMAGE",
    "CODEX_INPUT_IMAGE",
    "CODEX_INPUT_IMAGES",
    "CODEX_ATTACHED_IMAGE",
    "CODEX_ATTACHED_IMAGES",
    "CODEX_ATTACHED_IMAGE_PATH",
    "CODEX_ATTACHED_IMAGE_PATHS",
)


class ThemeImageResolveError(RuntimeError):
    """Raised when a supplied theme image reference cannot be resolved."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_image(path: Path) -> None:
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception as exc:
        raise ThemeImageResolveError(f"不是可读取的图片: {path} ({exc})") from exc


def _safe_suffix(path: Path, fallback: str = ".png") -> str:
    suffix = path.suffix.lower()
    return suffix if suffix in IMAGE_EXTS else fallback


def _copy_stable(src: Path, out_dir: Path, label: str = "theme_image") -> Path:
    src = src.expanduser().resolve()
    if not src.exists():
        raise ThemeImageResolveError(f"主题图不存在: {src}")
    if src.is_dir():
        src = newest_image_in_dir(src)
    _verify_image(src)

    dest_dir = out_dir / "theme_inputs"
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = _sha256(src)[:12]
    dest = dest_dir / f"{label}_{digest}{_safe_suffix(src)}"
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest.resolve()


def newest_image_in_dir(directory: Path) -> Path:
    directory = directory.expanduser().resolve()
    if not directory.exists() or not directory.is_dir():
        raise ThemeImageResolveError(f"不是有效目录: {directory}")
    images = [
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
    ]
    if not images:
        raise ThemeImageResolveError(f"目录中没有图片文件: {directory}")
    return max(images, key=lambda p: p.stat().st_mtime)


def _split_env_paths(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("data:image/") or _is_url(value):
        return [value]
    parts = re.split(r"[\n,;:]", value)
    return [p.strip().strip("'\"") for p in parts if p.strip()]


def env_image_candidates() -> list[str]:
    candidates: list[str] = []
    for key in ENV_IMAGE_KEYS:
        value = os.environ.get(key, "")
        if not value:
            continue
        candidates.extend(_split_env_paths(value))
    return candidates


def _is_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https", "file"}


def _download_url(value: str, out_dir: Path) -> Path:
    dest_dir = out_dir / "theme_inputs"
    dest_dir.mkdir(parents=True, exist_ok=True)

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        return _copy_stable(Path(urllib.request.url2pathname(parsed.path)), out_dir)

    suffix = _safe_suffix(Path(parsed.path), ".png")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    dest = dest_dir / f"theme_image_url_{digest}{suffix}"
    if not dest.exists():
        req = urllib.request.Request(value, headers={"User-Agent": "auto-garment-producer/1.0"})
        with urllib.request.urlopen(req, timeout=60) as response:
            dest.write_bytes(response.read())
    _verify_image(dest)
    return dest.resolve()


def _decode_data_uri(value: str, out_dir: Path) -> Path | None:
    if value.startswith("data:image/"):
        header, _, payload = value.partition(",")
        if not payload:
            raise ThemeImageResolveError("data URI 中没有图片数据")
        match = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64", header)
        suffix = f".{match.group(1).lower()}" if match else ".png"
        if suffix == ".jpeg":
            suffix = ".jpg"
        raw = base64.b64decode(payload)
    elif len(value) > 512 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value):
        suffix = ".png"
        raw = base64.b64decode(re.sub(r"\s+", "", value))
    else:
        return None

    dest_dir = out_dir / "theme_inputs"
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    dest = dest_dir / f"theme_image_base64_{digest}{suffix}"
    dest.write_bytes(raw)
    _verify_image(dest)
    return dest.resolve()


def _auto_discover(out_dir: Path) -> Path | None:
    """Find a deliberately staged image near the run output.

    This avoids scanning unrelated user folders.  Users or integrations can put
    an attachment in one of these locations when they cannot pass a path.
    """
    search_dirs = [
        out_dir / "input",
        out_dir / "inputs",
        out_dir / "theme_inputs",
        out_dir,
    ]
    candidates: list[Path] = []
    for directory in search_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for p in directory.iterdir():
            name = p.name.lower()
            if (
                p.is_file()
                and p.suffix.lower() in IMAGE_EXTS
                and (name.startswith("theme") or name.startswith("input") or name.startswith("reference"))
            ):
                candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def resolve_theme_image(value: str, out_dir: str | Path, *, required: bool = False) -> Path | None:
    """Resolve a theme image reference into a stable local file.

    Args:
        value: Path, directory, URL, data URI, base64 image payload, or empty.
        out_dir: Run output directory where the normalized input should live.
        required: Raise if no image can be found.
    """
    out_path = Path(out_dir)
    raw = (value or "").strip()

    candidates = [raw] if raw else []
    if not raw:
        candidates.extend(env_image_candidates())

    for candidate in candidates:
        if not candidate:
            continue
        decoded = _decode_data_uri(candidate, out_path)
        if decoded:
            return decoded
        if _is_url(candidate):
            return _download_url(candidate, out_path)
        path = Path(candidate).expanduser()
        if path.exists():
            return _copy_stable(path, out_path)

    discovered = _auto_discover(out_path)
    if discovered:
        return _copy_stable(discovered, out_path)

    if required:
        hint = (
            "没有找到可用主题图。请传 --theme-image /path/to/image，或把图片放入 "
            f"{(out_path / 'input').resolve()}，或设置 AUTO_GARMENT_THEME_IMAGE。"
        )
        raise ThemeImageResolveError(hint)
    return None
