#!/usr/bin/env python3
"""
固定模板资产加载器。

仅解析内置 BFSK26308XCJ01L / DDS26126XCJ01L 的 s 码资产。
"""
import json
import os
import re
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = SKILL_DIR / "templates"
INDEX_PATH = TEMPLATES_DIR / "index.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _existing_path(value: str | Path, base_dir: Path) -> str:
    """Resolve an optional path and return it only when it exists."""
    if not value:
        return ""
    path = resolve_asset_path(value, base_dir)
    return str(path.resolve()) if path.exists() else ""


def load_index() -> dict:
    """加载模板注册表索引。"""
    if INDEX_PATH.exists():
        return _load_json(INDEX_PATH)
    return {"templates": [], "version": "1.0.0"}


def _normalize_match_text(value: str) -> str:
    """Normalize garment labels so variants like 'T 恤', 't shirt', and 't-shirt' match."""
    return re.sub(r"[\s\-_'/]+", "", str(value).lower().strip())


def _find_index_entry_by_id(template_id: str) -> dict | None:
    if not template_id:
        return None
    for entry in load_index().get("templates", []):
        if entry.get("template_id") == template_id:
            return entry
    return None


def _find_index_entry_by_garment_type(garment_type: str) -> dict | None:
    gt_norm = _normalize_match_text(garment_type)
    if not gt_norm:
        return None
    for entry in load_index().get("templates", []):
        if _normalize_match_text(entry.get("garment_type", "")) == gt_norm:
            return entry
        if _normalize_match_text(entry.get("template_name", "")) == gt_norm:
            return entry
        for alias in entry.get("aliases", []):
            if _normalize_match_text(alias) == gt_norm:
                return entry
    return None


def _resolve_asset_size(entry: dict, size_label: str = "s") -> str:
    return "s"


def resolve_asset_path(value: str | Path, base_dir: Path) -> Path:
    """Resolve an asset reference relative to the pieces JSON directory."""
    path = Path(value)
    if path.is_absolute():
        return path
    return base_dir / path


def resolve_json_metadata_path(value: str | Path, owner_json_path: str | Path) -> Path:
    """Resolve a metadata path relative to the JSON file that owns it."""
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(owner_json_path).resolve().parent / path


def relative_json_metadata_path(target: str | Path, owner_json_path: str | Path) -> str:
    """Serialize a metadata path relative to the JSON file that owns it."""
    return os.path.relpath(Path(target).resolve(), Path(owner_json_path).resolve().parent)


def normalize_piece_asset_paths(pieces_payload: dict, pieces_json_path: str | Path) -> dict:
    """Expand relative prepared/overview/mask paths using the pieces JSON directory."""
    base_dir = Path(pieces_json_path).resolve().parent
    for key in ("prepared_pattern", "overview_image"):
        value = pieces_payload.get(key, "")
        if value:
            pieces_payload[key] = str(resolve_asset_path(value, base_dir).resolve())
    for piece in pieces_payload.get("pieces", []):
        value = piece.get("mask_path", "")
        if value:
            piece["mask_path"] = str(resolve_asset_path(value, base_dir).resolve())
    return pieces_payload


def load_template_assets_manifest(asset_dir: str | Path, size_label: str) -> dict | None:
    """Load preprocessed template asset manifest when present."""
    path = Path(asset_dir) / f"template_assets_{size_label}.json"
    if not path.exists():
        return None
    try:
        manifest = _load_json(path)
    except Exception:
        return None
    manifest["_manifest_path"] = str(path.resolve())
    return manifest


def template_kimi_preview_for_pieces(pieces_json_path: str | Path, image_kind: str = "piece_overview") -> str:
    """Return preprocessed Kimi preview path for a template pieces file, if available.

    image_kind: piece_overview | prepared_pattern | garment_map_overview
    """
    pieces_path = Path(pieces_json_path).resolve()
    size_label = pieces_path.stem.removeprefix("pieces_")
    manifest = load_template_assets_manifest(pieces_path.parent, size_label)
    if not manifest:
        return ""
    ai_previews = manifest.get("ai_previews", {}) if isinstance(manifest.get("ai_previews"), dict) else {}
    key_map = {
        "piece_overview": "piece_overview_kimi",
        "prepared_pattern": "prepared_pattern_kimi",
        "garment_map_overview": "garment_map_overview_kimi",
    }
    value = ai_previews.get(key_map.get(image_kind, image_kind), "")
    return _existing_path(value, pieces_path.parent)


def resolve_template_assets(
    template_id: str = "",
    size_label: str = "s",
    garment_type: str = "",
) -> dict | None:
    """解析可直接复用的内置模板资产。

    仅当 pieces、overview、prepared pattern、garment_map 与 garment_map_overview
    均存在且 pieces 内部引用的固定文件也存在时返回资产路径；否则返回 None。
    """
    entry = _find_index_entry_by_id(template_id)
    if not entry and garment_type:
        entry = _find_index_entry_by_garment_type(garment_type)
    if not entry:
        return None

    tid = entry.get("template_id", "")
    resolved_size = _resolve_asset_size(entry, size_label)
    asset_dir = TEMPLATES_DIR / tid / resolved_size
    pieces_path = asset_dir / f"pieces_{resolved_size}.json"
    piece_overview_path = asset_dir / f"piece_overview_{resolved_size}.png"
    prepared_pattern_path = asset_dir / f"prepared_pattern_{resolved_size}.png"
    garment_map_path = asset_dir / f"garment_map_{resolved_size}.json"
    garment_map_overview_path = asset_dir / f"garment_map_overview_{resolved_size}.jpg"
    manifest = load_template_assets_manifest(asset_dir, resolved_size)

    required = [
        pieces_path,
        piece_overview_path,
        prepared_pattern_path,
        garment_map_path,
        garment_map_overview_path,
    ]
    if not all(p.exists() for p in required):
        return None

    try:
        pieces_payload = _load_json(pieces_path)
    except Exception:
        return None
    pieces_payload = normalize_piece_asset_paths(pieces_payload, pieces_path)

    referenced = [
        pieces_payload.get("prepared_pattern", ""),
        pieces_payload.get("overview_image", ""),
    ]
    referenced.extend(piece.get("mask_path", "") for piece in pieces_payload.get("pieces", []))
    for ref in referenced:
        if not ref or not Path(ref).exists():
            return None

    result = {
        "template_id": tid,
        "template_name": entry.get("template_name", ""),
        "size_label": resolved_size,
        "asset_dir": str(asset_dir.resolve()),
        "pieces_path": str(pieces_path.resolve()),
        "piece_overview_path": str(piece_overview_path.resolve()),
        "prepared_pattern_path": str(prepared_pattern_path.resolve()),
        "garment_map_path": str(garment_map_path.resolve()),
        "garment_map_overview_path": str(garment_map_overview_path.resolve()),
    }
    if manifest:
        result["template_assets_manifest_path"] = manifest.get("_manifest_path", "")
        ai_previews = manifest.get("ai_previews", {}) if isinstance(manifest.get("ai_previews"), dict) else {}
        piece_overview_kimi = _existing_path(ai_previews.get("piece_overview_kimi", ""), asset_dir)
        prepared_pattern_kimi = _existing_path(ai_previews.get("prepared_pattern_kimi", ""), asset_dir)
        garment_map_overview_kimi = _existing_path(ai_previews.get("garment_map_overview_kimi", ""), asset_dir)
        if piece_overview_kimi:
            result["piece_overview_kimi_path"] = piece_overview_kimi
        if prepared_pattern_kimi:
            result["prepared_pattern_kimi_path"] = prepared_pattern_kimi
        if garment_map_overview_kimi:
            result["garment_map_overview_kimi_path"] = garment_map_overview_kimi
    return result
