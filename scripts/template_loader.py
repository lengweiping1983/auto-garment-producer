#!/usr/bin/env python3
"""
模板注册表加载器与匹配引擎。

功能：
1. 加载内置模板或用户自定义模板文件
2. 固定解析内置模板的 s 资产
3. 将提取的 pieces 与模板 slot 按面积排名匹配
4. 验证匹配质量，返回 garment_map 列表
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
    均存在且 pieces 内部引用的固定文件也存在时返回资产路径；否则返回 None，
    由调用方回退到运行时提取流程。
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


def find_template_by_id(template_id: str, size: str = "s") -> dict | None:
    """按 template_id 查找并加载固定模板。"""
    template_dir = TEMPLATES_DIR / template_id
    if not template_dir.exists():
        return None
    size_file = template_dir / "base.json"
    if not size_file.exists():
        return None
    return _load_json(size_file)


def find_template_by_garment_type(garment_type: str) -> dict | None:
    """按 garment_type 或 aliases 模糊匹配模板。"""
    index = load_index()
    gt_norm = _normalize_match_text(garment_type)
    for entry in index.get("templates", []):
        if _normalize_match_text(entry.get("garment_type", "")) == gt_norm:
            return find_template_by_id(entry["template_id"])
        # 也匹配 template_name
        if _normalize_match_text(entry.get("template_name", "")) == gt_norm:
            return find_template_by_id(entry["template_id"])
        # 匹配 aliases（支持中文别名如"T恤"、"防晒服"等）
        for alias in entry.get("aliases", []):
            if _normalize_match_text(alias) == gt_norm:
                return find_template_by_id(entry["template_id"])
    return None


def load_template_file(path: Path) -> dict | None:
    """加载用户自定义模板文件。"""
    if not path.exists():
        return None
    return _load_json(path)


def match_pieces_to_template(pieces: list[dict], template: dict) -> tuple[list[dict], float]:
    """将提取的 pieces 与模板 slot 匹配。

    匹配策略：
    1. 按面积降序排序 pieces
    2. 验证 piece 数量是否匹配模板
    3. 按 expected_area_rank 直接匹配
    4. 验证宽高比是否在期望范围内

    返回：(garment_map_entries, avg_confidence)
    """
    sorted_pieces = sorted(pieces, key=lambda p: p.get("area", 0), reverse=True)
    expected_count = template.get("piece_count", 0)

    if len(sorted_pieces) != expected_count:
        return [], 0.0

    slot_by_rank = {}
    for slot in template.get("pieces", []):
        rank = slot.get("expected_area_rank", 0)
        if rank > 0:
            slot_by_rank[rank] = slot

    matched = []
    total_conf = 0.0

    # 预构建 rank→piece 映射，用于 symmetry_relations 中的 target_slot_index → target_piece_id
    piece_by_rank = {rank: piece for rank, piece in enumerate(sorted_pieces, 1)}

    for rank, piece in enumerate(sorted_pieces, 1):
        slot = slot_by_rank.get(rank)
        if not slot:
            return [], 0.0

        aspect = piece.get("width", 1) / max(1, piece.get("height", 1))
        aspect_min = slot.get("expected_aspect_min", 0)
        aspect_max = slot.get("expected_aspect_max", float("inf"))
        aspect_ok = aspect_min <= aspect <= aspect_max

        # 匹配置信度：宽高比在范围内 0.95，否则 0.70
        conf = 0.95 if aspect_ok else 0.70
        total_conf += conf

        entry = {
            "piece_id": piece["piece_id"],
            "garment_role": slot.get("garment_role", "unknown"),
            "zone": slot.get("zone", "detail"),
            "symmetry_group": slot.get("symmetry_group", ""),
            "same_shape_group": slot.get("same_shape_group", ""),
            "direction_degrees": 0,
            "texture_direction": slot.get("texture_direction_hint", ""),
            "texture_direction_hint": slot.get("texture_direction_hint", ""),
            "grain_direction": slot.get("grain_direction", "vertical"),
            "confidence": round(conf, 2),
            "reason": f"模板匹配: {slot.get('piece_name', '?')} (slot={slot.get('slot_index', '?')}, rank={rank})",
            "template_matched": True,
            "template_id": template.get("template_id", ""),
        }
        # 透传 symmetry_relations：将 target_slot_index 映射为 target_piece_id
        if slot.get("symmetry_relations"):
            relations = []
            for rel in slot["symmetry_relations"]:
                target_slot = rel.get("target_slot_index")
                if target_slot is not None:
                    target_piece = piece_by_rank.get(target_slot + 1)
                    if target_piece:
                        relations.append({
                            "target_piece_id": target_piece["piece_id"],
                            "mirror_x": rel.get("mirror_x", False),
                            "mirror_y": rel.get("mirror_y", False),
                        })
            if relations:
                entry["symmetry_relations"] = relations
        # 透传 pieces 中的方向信息
        if "pattern_orientation" in piece:
            entry["pattern_orientation"] = piece["pattern_orientation"]
            entry["orientation_confidence"] = piece.get("orientation_confidence", 0)
            entry["orientation_reason"] = piece.get("orientation_reason", "")
        matched.append(entry)

    avg_conf = total_conf / len(matched) if matched else 0.0
    return matched, avg_conf


def format_template_garment_map(entries: list[dict], template: dict) -> dict:
    """将匹配结果格式化为 garment_map.json 结构。"""
    return {
        "map_id": f"template_{template.get('template_id', 'unknown')}",
        "method": "template_registry_match",
        "confidence": round(
            sum(e.get("confidence", 0) for e in entries) / max(1, len(entries)), 2),
        "template_id": template.get("template_id", ""),
        "template_name": template.get("template_name", ""),
        "pieces": entries,
    }
