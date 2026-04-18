#!/usr/bin/env python3
"""
模板注册表加载器与匹配引擎。

功能：
1. 加载内置模板或用户自定义模板文件
2. 解析模板继承链（尺寸变体继承基准模板）
3. 将提取的 pieces 与模板 slot 按面积排名匹配
4. 验证匹配质量，返回 garment_map 列表
"""
import json
import re
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = SKILL_DIR / "templates"
INDEX_PATH = TEMPLATES_DIR / "index.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_index() -> dict:
    """加载模板注册表索引。"""
    if INDEX_PATH.exists():
        return _load_json(INDEX_PATH)
    return {"templates": [], "version": "1.0.0"}


def _find_index_entry_by_id(template_id: str) -> dict | None:
    if not template_id:
        return None
    for entry in load_index().get("templates", []):
        if entry.get("template_id") == template_id:
            return entry
    return None


def _find_index_entry_by_garment_type(garment_type: str) -> dict | None:
    gt_lower = garment_type.lower().strip()
    if not gt_lower:
        return None
    for entry in load_index().get("templates", []):
        if entry.get("garment_type", "").lower().strip() == gt_lower:
            return entry
        if entry.get("template_name", "").lower().strip() == gt_lower:
            return entry
        for alias in entry.get("aliases", []):
            if alias.lower().strip() == gt_lower:
                return entry
    return None


def _find_index_entry_by_pattern_path(pattern_path: str | Path) -> dict | None:
    candidate_id = _template_id_from_pattern_path(pattern_path)
    if not candidate_id:
        return None

    matches = []
    for entry in load_index().get("templates", []):
        tid = entry.get("template_id", "")
        tname = entry.get("template_name", "")
        if tid == candidate_id or tname == candidate_id:
            matches.append((entry, 100))
        elif candidate_id in tid or candidate_id in tname:
            matches.append((entry, 50))
        elif tid in candidate_id or tname in candidate_id:
            matches.append((entry, 25))
    if not matches:
        return None
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[0][0]


def _template_id_from_pattern_path(pattern_path: str | Path) -> str:
    p = Path(pattern_path)
    candidate_id = p.stem
    for suffix in ("-S_mask", "-M_mask", "-L_mask", "-XL_mask", "-XXL_mask",
                   "_S_mask", "_M_mask", "_L_mask", "_XL_mask", "_XXL_mask",
                   "_mask", "-mask"):
        if candidate_id.endswith(suffix):
            return candidate_id[: -len(suffix)]
    return candidate_id


def _size_from_pattern_path(pattern_path: str | Path) -> str:
    stem = Path(pattern_path).stem.lower()
    match = re.search(r"[-_]([sml]|xl|xxl)_?mask$", stem)
    if match:
        return match.group(1)
    return ""


def _resolve_asset_size(entry: dict, size_label: str = "base", pattern_path: str | Path = "") -> str:
    sizes = {str(s).lower() for s in entry.get("sizes", [])}
    requested = (size_label or "").lower().strip()
    if requested and requested != "base":
        return requested
    pattern_size = _size_from_pattern_path(pattern_path) if pattern_path else ""
    if pattern_size and (not sizes or pattern_size in sizes):
        return pattern_size
    return str(entry.get("default_size") or "s").lower()


def resolve_template_assets(
    template_id: str = "",
    size_label: str = "base",
    pattern_path: str | Path = "",
    garment_type: str = "",
) -> dict | None:
    """解析可直接复用的内置模板资产。

    仅当 pieces、overview、prepared pattern、garment_map 与 garment_map_overview
    均存在且 pieces 内部引用的固定文件也存在时返回资产路径；否则返回 None，
    由调用方回退到运行时提取流程。
    """
    entry = _find_index_entry_by_id(template_id)
    if not entry and pattern_path:
        entry = _find_index_entry_by_pattern_path(pattern_path)
    if not entry and garment_type:
        entry = _find_index_entry_by_garment_type(garment_type)
    if not entry:
        return None

    tid = entry.get("template_id", "")
    resolved_size = _resolve_asset_size(entry, size_label, pattern_path)
    asset_dir = TEMPLATES_DIR / tid / resolved_size
    pieces_path = asset_dir / f"pieces_{resolved_size}.json"
    piece_overview_path = asset_dir / f"piece_overview_{resolved_size}.png"
    prepared_pattern_path = asset_dir / f"prepared_pattern_{resolved_size}.png"
    garment_map_path = asset_dir / f"garment_map_{resolved_size}.json"
    garment_map_overview_path = asset_dir / f"garment_map_overview_{resolved_size}.jpg"

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

    referenced = [
        pieces_payload.get("prepared_pattern", ""),
        pieces_payload.get("overview_image", ""),
    ]
    referenced.extend(piece.get("mask_path", "") for piece in pieces_payload.get("pieces", []))
    for ref in referenced:
        if not ref or not Path(ref).exists():
            return None

    return {
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


def find_template_by_id(template_id: str, size: str = "base") -> dict | None:
    """按 template_id 和 size 查找并加载模板。"""
    template_dir = TEMPLATES_DIR / template_id
    if not template_dir.exists():
        return None
    size_file = template_dir / f"{size}.json"
    if not size_file.exists():
        size_file = template_dir / "base.json"
    if not size_file.exists():
        return None
    return _resolve_template(_load_json(size_file))


def find_template_by_garment_type(garment_type: str) -> dict | None:
    """按 garment_type 或 aliases 模糊匹配模板。"""
    index = load_index()
    gt_lower = garment_type.lower().strip()
    for entry in index.get("templates", []):
        if entry.get("garment_type", "").lower().strip() == gt_lower:
            return find_template_by_id(entry["template_id"], entry.get("default_size", "base"))
        # 也匹配 template_name
        if entry.get("template_name", "").lower().strip() == gt_lower:
            return find_template_by_id(entry["template_id"], entry.get("default_size", "base"))
        # 匹配 aliases（支持中文别名如"T恤"、"防晒服"等）
        for alias in entry.get("aliases", []):
            if alias.lower().strip() == gt_lower:
                return find_template_by_id(entry["template_id"], entry.get("default_size", "base"))
    return None


def load_template_file(path: Path) -> dict | None:
    """加载用户自定义模板文件。"""
    if not path.exists():
        return None
    return _resolve_template(_load_json(path))


def _resolve_template(template: dict) -> dict:
    """解析模板继承链。尺寸变体可覆盖基准模板的字段。"""
    inherits = template.get("inherits")
    if not inherits:
        return template
    # inherits 格式: "template_id/size" 或 "template_id"（默认 base）
    parts = inherits.split("/")
    base_id = parts[0]
    base_size = parts[1] if len(parts) > 1 else "base"
    base = find_template_by_id(base_id, base_size)
    if not base:
        return template
    # 深拷贝基准模板
    resolved = _deep_copy(base)
    # 应用覆盖
    overrides = template.get("overrides", {})
    if "pieces" in overrides:
        # 按 slot_index 合并 piece 覆盖
        piece_overrides = {p["slot_index"]: p for p in overrides["pieces"]}
        for piece in resolved.get("pieces", []):
            slot_idx = piece.get("slot_index")
            if slot_idx in piece_overrides:
                piece.update(piece_overrides[slot_idx])
    # 覆盖顶层字段（除了 pieces）
    for key, value in template.items():
        if key not in ("inherits", "overrides", "pieces"):
            resolved[key] = value
    return resolved


def _deep_copy(data):
    """简单的深拷贝（仅处理 dict/list/primitive）。"""
    if isinstance(data, dict):
        return {k: _deep_copy(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_deep_copy(v) for v in data]
    return data


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


def find_template_by_pattern_path(pattern_path: str | Path) -> dict | None:
    """根据 pattern 文件路径自动匹配已初始化的模板。

    匹配策略：
    1. 提取文件名中的货号/型号（如 BFSK26308XCJ01L）
    2. 在 index.json 中查找 template_id 或 template_name 包含该货号的模板
    3. 若只有一个匹配，直接返回；多个匹配时返回最精确的那个
    """
    p = Path(pattern_path)
    stem = p.stem  # e.g. "BFSK26308XCJ01L-S_mask"
    # 去掉常见后缀提取货号
    candidate_id = stem
    for suffix in ("-S_mask", "-M_mask", "-L_mask", "-XL_mask", "-XXL_mask",
                   "_mask", "-mask"):
        if candidate_id.endswith(suffix):
            candidate_id = candidate_id[: -len(suffix)]
            break

    index = load_index()
    matches = []
    for entry in index.get("templates", []):
        tid = entry.get("template_id", "")
        tname = entry.get("template_name", "")
        # 精确匹配或包含
        if tid == candidate_id or tname == candidate_id:
            matches.append((entry, 100))  # 精确匹配优先级最高
        elif candidate_id in tid or candidate_id in tname:
            matches.append((entry, 50))
        elif tid in candidate_id or tname in candidate_id:
            matches.append((entry, 25))

    if not matches:
        return None
    # 按优先级排序，返回最高分的模板（默认尺寸）
    matches.sort(key=lambda x: x[1], reverse=True)
    best = matches[0][0]
    return find_template_by_id(best["template_id"], best.get("default_size", "base"))


def load_size_mappings(template_id: str) -> dict | None:
    """加载指定模板的多尺寸映射关系。"""
    path = TEMPLATES_DIR / template_id / "size_mappings.json"
    if not path.exists():
        return None
    return _load_json(path)


def load_size_pieces(template_id: str, size_label: str) -> dict | None:
    """加载指定模板某个尺寸的 pieces.json。"""
    path = TEMPLATES_DIR / template_id / size_label / f"pieces_{size_label}.json"
    if not path.exists():
        return None
    return _load_json(path)
