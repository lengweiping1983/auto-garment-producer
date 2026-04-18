#!/usr/bin/env python3
"""
合并面料组合：将两套 texture_set（如 A 源和 B 源）合并为一套 merged_texture_set.json。

职责：
- 读取两套 texture_set.json
- 为每套资产的 texture_id / motif_id / solid_id 加源后缀（_a / _b）
- 保留 source 标记和原始元数据
- 输出 merged_texture_set.json，供多方案生产规划使用

使用方式：
    python3 合并面料组合.py \
        --set-a /path/to/texture_set_A.json \
        --set-b /path/to/texture_set_B.json \
        --out /path/to/output
"""
import argparse
import json
import sys
from pathlib import Path


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _suffix_assets(items: list[dict], source: str, id_key: str) -> list[dict]:
    """为资产列表中的每个 item 的 id 字段加后缀，并标记 source。
    注意：当 id_key 本身就是 texture_id 时，只修改一次，避免双重后缀。"""
    result = []
    for item in items:
        new_item = dict(item)
        original_id = item.get(id_key, "")
        if original_id:
            new_item[id_key] = f"{original_id}_{source}"
        # 如果 id_key 不是 texture_id，但 item 有 texture_id 字段，也需要后缀（如 motif 引用 texture）
        if id_key != "texture_id" and "texture_id" in item and item["texture_id"]:
            original_tex_id = item["texture_id"]
            new_item["texture_id"] = f"{original_tex_id}_{source}"
        new_item["source"] = source
        result.append(new_item)
    return result


def merge_texture_sets(ts_a_path: Path, ts_b_path: Path, out_dir: Path) -> Path:
    """合并两套 texture_set 为一套 merged_texture_set.json。"""
    ts_a = load_json(ts_a_path)
    ts_b = load_json(ts_b_path)

    ts_a_id = ts_a.get("texture_set_id", "set_a")
    ts_b_id = ts_b.get("texture_set_id", "set_b")

    merged = {
        "texture_set_id": f"{ts_a_id}_merged_{ts_b_id}",
        "locked": False,
        "source_sets": {
            "a": {"path": str(ts_a_path.resolve()), "id": ts_a_id},
            "b": {"path": str(ts_b_path.resolve()), "id": ts_b_id},
        },
        "textures": [],
        "motifs": [],
        "solids": [],
    }

    # A 源资产
    merged["textures"].extend(_suffix_assets(ts_a.get("textures", []), "a", "texture_id"))
    merged["motifs"].extend(_suffix_assets(ts_a.get("motifs", []), "a", "motif_id"))
    merged["solids"].extend(_suffix_assets(ts_a.get("solids", []), "a", "solid_id"))

    # B 源资产
    merged["textures"].extend(_suffix_assets(ts_b.get("textures", []), "b", "texture_id"))
    merged["motifs"].extend(_suffix_assets(ts_b.get("motifs", []), "b", "motif_id"))
    merged["solids"].extend(_suffix_assets(ts_b.get("solids", []), "b", "solid_id"))

    out_path = out_dir / "merged_texture_set.json"
    write_json(out_path, merged)

    print(json.dumps({
        "merged_texture_set": str(out_path.resolve()),
        "total_assets": len(merged["textures"]) + len(merged["motifs"]) + len(merged["solids"]),
        "textures": len(merged["textures"]),
        "motifs": len(merged["motifs"]),
        "solids": len(merged["solids"]),
        "source_a": str(ts_a_path.resolve()),
        "source_b": str(ts_b_path.resolve()),
    }, ensure_ascii=False, indent=2))
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="将两套 texture_set 合并为一套 merged_texture_set.json")
    parser.add_argument("--set-a", required=True, help="第一套 texture_set.json 路径（A 源）")
    parser.add_argument("--set-b", required=True, help="第二套 texture_set.json 路径（B 源）")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    ts_a_path = Path(args.set_a)
    ts_b_path = Path(args.set_b)
    out_dir = Path(args.out)

    if not ts_a_path.exists():
        print(f"错误: A 源面料组合不存在: {ts_a_path}", file=sys.stderr)
        return 1
    if not ts_b_path.exists():
        print(f"错误: B 源面料组合不存在: {ts_b_path}", file=sys.stderr)
        return 1

    merge_texture_sets(ts_a_path, ts_b_path, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
