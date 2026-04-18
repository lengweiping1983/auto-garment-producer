#!/usr/bin/env python3
"""
多尺寸模板初始化脚本。

一次性处理同版型全部尺寸 mask，建立基准模板与跨尺寸映射关系。
所有输出写入 templates/<template_id>/ 目录，供后续直接复用。

用法:
    python3 初始化多尺寸模板.py \
        --template-id BFSK26308XCJ01L \
        --base-mask /path/to/*-S_mask.png \
        --size-masks /path/to/*-M_mask.png /path/to/*-L_mask.png ... \
        [--size-labels m l xl xxl]
"""
import argparse
import json
import math
import sys
from pathlib import Path

# 复用提取裁片的核心函数
sys.path.insert(0, str(Path(__file__).parent))
from 提取裁片 import (
    load_rgba,
    prepare_pattern_image,
    components_from_alpha,
    write_masks,
    build_overview,
    analyze_piece_orientation,
    guess_role,
)
from PIL import Image, ImageDraw, ImageFont

# 模板根目录
SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = SKILL_DIR / "templates"
INDEX_PATH = TEMPLATES_DIR / "index.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_masks_with_suffix(components: list[dict], image_size: tuple[int, int],
                                out_dir: Path, size_label: str) -> list[dict]:
    """为每个连通域生成遮罩 PNG，piece_id 和文件名均带尺寸后缀。"""
    width, _ = image_size
    masks_dir = out_dir / "masks"
    masks_dir.mkdir(parents=True, exist_ok=True)
    total_area = sum(item["area"] for item in components) or 1
    pieces = []
    for index, component in enumerate(components, 1):
        piece_id = f"piece_{index:03d}_{size_label}"
        bbox = component["bbox"]
        mask = Image.new("L", (bbox["width"], bbox["height"]), 0)
        pix = mask.load()
        for pixel_idx in component["pixels"]:
            y, x = divmod(pixel_idx, width)
            pix[x - bbox["x"], y - bbox["y"]] = 255
        mask_path = masks_dir / f"{piece_id}_mask.png"
        mask.save(mask_path)
        orientation_info = analyze_piece_orientation(mask)
        piece_meta = {
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
        piece_meta.update(orientation_info)
        pieces.append(piece_meta)
    return pieces


def _build_overview_with_suffix(prepared_path: Path, pieces: list[dict],
                                out_path: Path, size_label: str) -> Path:
    """生成裁片总览图，文件名带尺寸后缀。"""
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


def extract_pieces(mask_path: Path, out_subdir: Path, size_label: str, min_area: int = 1000) -> dict:
    """对单张 mask 提取裁片，所有文件名带尺寸后缀，返回 pieces_payload。"""
    out_subdir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_pattern_image(mask_path, out_subdir / f"prepared_pattern_{size_label}.png")
    img = load_rgba(prepared)
    components = components_from_alpha(img, threshold=16, min_area=min_area)
    pieces = _write_masks_with_suffix(components, img.size, out_subdir, size_label)
    overview = _build_overview_with_suffix(
        prepared, pieces, out_subdir / f"piece_overview_{size_label}.png", size_label
    )

    payload = {
        "pattern_image": str(mask_path.resolve()),
        "prepared_pattern": str(prepared.resolve()),
        "overview_image": str(overview.resolve()),
        "canvas": {"width": img.width, "height": img.height, "unit": "px"},
        "pieces": pieces,
    }
    pieces_json = out_subdir / f"pieces_{size_label}.json"
    _save_json(pieces_json, payload)
    return payload


def build_size_mapping(base_pieces: list[dict], target_pieces: list[dict], target_label: str) -> dict:
    """建立 base → target 的裁片映射关系。

    策略：按面积排名一一对应 + 宽高比验证 + 中心点距离验证。
    """
    if len(base_pieces) != len(target_pieces):
        raise ValueError(
            f"裁片数量不匹配: base={len(base_pieces)}, {target_label}={len(target_pieces)}"
        )

    # 按面积降序
    base_sorted = sorted(base_pieces, key=lambda p: p["area"], reverse=True)
    target_sorted = sorted(target_pieces, key=lambda p: p["area"], reverse=True)

    piece_map = {}
    scale_factors = []
    aspect_warnings = []

    for rank, (bp, tp) in enumerate(zip(base_sorted, target_sorted), 1):
        base_id = bp["piece_id"]
        target_id = tp["piece_id"]
        piece_map[base_id] = target_id

        # 缩放因子（宽、高、面积）
        sf_w = tp["width"] / max(1, bp["width"])
        sf_h = tp["height"] / max(1, bp["height"])
        sf_area = math.sqrt(tp["area"] / max(1, bp["area"]))
        scale_factors.append({"width": sf_w, "height": sf_h, "area_sqrt": sf_area})

        # 宽高比验证
        base_aspect = bp["width"] / max(1, bp["height"])
        target_aspect = tp["width"] / max(1, tp["height"])
        aspect_delta = abs(base_aspect - target_aspect) / max(base_aspect, target_aspect)
        if aspect_delta > 0.25:
            aspect_warnings.append({
                "rank": rank,
                "base_id": base_id,
                "target_id": target_id,
                "base_aspect": round(base_aspect, 3),
                "target_aspect": round(target_aspect, 3),
                "delta": round(aspect_delta, 3),
            })

    # 平均缩放因子
    avg_sf = {
        "width": round(sum(s["width"] for s in scale_factors) / len(scale_factors), 4),
        "height": round(sum(s["height"] for s in scale_factors) / len(scale_factors), 4),
        "area_sqrt": round(sum(s["area_sqrt"] for s in scale_factors) / len(scale_factors), 4),
    }

    return {
        "scale_factor": avg_sf,
        "piece_map": piece_map,
        "piece_count": len(base_pieces),
        "aspect_warnings": aspect_warnings,
    }


def generate_base_template(template_id: str, base_payload: dict, garment_type: str) -> dict:
    """基于 -S 裁片数据生成 base.json 模板配置。"""
    pieces = base_payload["pieces"]
    # 按面积排名
    sorted_pieces = sorted(pieces, key=lambda p: p["area"], reverse=True)

    # 加载现有通用模板作为初始角色参考
    generic_template = None
    generic_path = TEMPLATES_DIR / "children_outerwear_set" / "base.json"
    if generic_path.exists():
        try:
            generic_template = _load_json(generic_path)
        except Exception:
            pass

    # 构建 slots
    template_pieces = []
    for rank, piece in enumerate(sorted_pieces, 1):
        aspect = piece["width"] / max(1, piece["height"])
        slot = {
            "slot_index": rank - 1,
            "piece_name": f"裁片_{rank}",
            "piece_name_en": f"piece_{rank:03d}",
            "expected_area_rank": rank,
            "expected_aspect_min": round(max(0.05, aspect * 0.6), 2),
            "expected_aspect_max": round(aspect * 1.4 + 0.1, 2),
            "expected_zone": "center",
            "garment_role": "unknown",
            "zone": "detail",
            "symmetry_group": "",
            "same_shape_group": "",
            "texture_direction_hint": "",
            "grain_direction": "vertical",
            "notes": f"面积排名#{rank}, 实际面积={piece['area']}, 宽高比={round(aspect, 3)}",
        }

        # 尝试从通用模板继承角色（按面积排名匹配）
        if generic_template:
            for gp in generic_template.get("pieces", []):
                if gp.get("expected_area_rank") == rank:
                    for key in ("garment_role", "zone", "symmetry_group", "same_shape_group",
                                "texture_direction_hint", "grain_direction", "piece_name", "piece_name_en"):
                        if key in gp:
                            slot[key] = gp[key]
                    slot["notes"] = f"从通用模板继承: {gp.get('piece_name', '?')}. " + slot["notes"]
                    break

        template_pieces.append(slot)

    return {
        "template_id": template_id,
        "template_name": template_id,
        "template_name_en": template_id,
        "garment_type": garment_type,
        "description": f"{template_id} 多尺寸基准模板，基于-S实际裁片数据初始化。请在初始化后手动修正各slot的garment_role。",
        "piece_count": len(pieces),
        "version": "1.0.0",
        "pieces": template_pieces,
    }


def update_template_index(template_id: str, template_name: str, garment_type: str,
                          sizes: list[str], default_size: str) -> None:
    """在 templates/index.json 中注册新模板。"""
    index = {"templates": [], "version": "1.0.0"}
    if INDEX_PATH.exists():
        try:
            index = _load_json(INDEX_PATH)
        except Exception:
            pass

    templates = index.get("templates", [])
    # 去重
    templates = [t for t in templates if t.get("template_id") != template_id]
    templates.append({
        "template_id": template_id,
        "template_name": template_name,
        "template_name_en": template_name,
        "garment_type": garment_type,
        "piece_count": 0,  # 会在后续更新
        "sizes": sizes,
        "default_size": default_size,
    })
    index["templates"] = templates
    _save_json(INDEX_PATH, index)


def main() -> int:
    parser = argparse.ArgumentParser(description="多尺寸模板初始化")
    parser.add_argument("--template-id", required=True, help="模板唯一标识，如 BFSK26308XCJ01L")
    parser.add_argument("--base-mask", required=True, help="基准尺寸 mask 路径（-S）")
    parser.add_argument("--size-masks", nargs="+", required=True,
                        help="其他尺寸 mask 路径列表（-M -L -XL -XXL）")
    parser.add_argument("--size-labels", nargs="+", required=True,
                        help="对应尺寸标签（m l xl xxl），顺序与 --size-masks 一致")
    parser.add_argument("--garment-type", default="children outerwear set", help="服装类型")
    parser.add_argument("--min-area", type=int, default=1000, help="最小裁片面积")
    parser.add_argument("--alpha-threshold", type=int, default=16, help="Alpha 通道阈值")
    args = parser.parse_args()

    if len(args.size_masks) != len(args.size_labels):
        print("[错误] --size-masks 和 --size-labels 数量必须一致", file=sys.stderr)
        return 1

    template_dir = TEMPLATES_DIR / args.template_id
    print(f"[初始化] 模板目录: {template_dir}")

    # ========== 1. 提取基准尺寸 (-S) ==========
    base_mask = Path(args.base_mask)
    if not base_mask.exists():
        print(f"[错误] 基准 mask 不存在: {base_mask}", file=sys.stderr)
        return 1

    print(f"[提取] 基准尺寸 -S: {base_mask}")
    base_out = template_dir / "s"
    base_payload = extract_pieces(base_mask, base_out, "s", args.min_area)
    print(f"  裁片数量: {len(base_payload['pieces'])}")
    print(f"  画布: {base_payload['canvas']['width']}x{base_payload['canvas']['height']}")

    # ========== 2. 提取其他尺寸并建立映射 ==========
    size_mappings = {"base_size": "s", "sizes": {}}
    all_sizes = ["s"] + args.size_labels

    for size_label, mask_path_str in zip(args.size_labels, args.size_masks):
        mask_path = Path(mask_path_str)
        if not mask_path.exists():
            print(f"[错误] mask 不存在: {mask_path}", file=sys.stderr)
            return 1

        print(f"[提取] 尺寸 {size_label.upper()}: {mask_path}")
        size_out = template_dir / size_label
        target_payload = extract_pieces(mask_path, size_out, size_label, args.min_area)
        print(f"  裁片数量: {len(target_payload['pieces'])}")
        if len(target_payload['pieces']) != len(base_payload['pieces']):
            print(f"[错误] 裁片数量不匹配! base(S)={len(base_payload['pieces'])}, "
                  f"{size_label.upper()}={len(target_payload['pieces'])}", file=sys.stderr)
            return 1

        print(f"[映射] {size_label.upper()} → S ...")
        try:
            mapping = build_size_mapping(
                base_payload["pieces"], target_payload["pieces"], size_label
            )
        except ValueError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 1

        if mapping["aspect_warnings"]:
            print(f"  [警告] 有 {len(mapping['aspect_warnings'])} 个裁片宽高比偏差 >25%:")
            for w in mapping["aspect_warnings"]:
                print(f"    rank={w['rank']}: base_aspect={w['base_aspect']} target_aspect={w['target_aspect']}")

        size_mappings["sizes"][size_label] = mapping
        print(f"  平均缩放因子: 宽={mapping['scale_factor']['width']} "
              f"高={mapping['scale_factor']['height']} "
              f"面积√={mapping['scale_factor']['area_sqrt']}")

    # ========== 3. 生成 base.json 模板配置 ==========
    base_template = generate_base_template(args.template_id, base_payload, args.garment_type)
    base_template["piece_count"] = len(base_payload["pieces"])

    # 更新 index.json 中的 piece_count
    update_template_index(
        args.template_id,
        args.template_id,
        args.garment_type,
        all_sizes,
        "s",
    )
    # 重新加载并修正 piece_count
    index = _load_json(INDEX_PATH)
    for t in index["templates"]:
        if t.get("template_id") == args.template_id:
            t["piece_count"] = len(base_payload["pieces"])
    _save_json(INDEX_PATH, index)

    base_template_path = template_dir / "base.json"
    _save_json(base_template_path, base_template)
    print(f"[生成] 基准模板: {base_template_path}")

    # ========== 4. 生成 size_mappings.json ==========
    mappings_path = template_dir / "size_mappings.json"
    _save_json(mappings_path, size_mappings)
    print(f"[生成] 尺寸映射: {mappings_path}")

    # ========== 5. 生成各尺寸变体 JSON ==========
    for size_label in args.size_labels:
        size_template = {
            "inherits": f"{args.template_id}/base",
            "template_id": args.template_id,
            "size_label": size_label,
            "overrides": {
                "pieces": [],
            },
        }
        _save_json(template_dir / f"{size_label}.json", size_template)
        print(f"[生成] 尺寸变体: {template_dir / f'{size_label}.json'}")

    print("\n" + "=" * 60)
    print("初始化完成。请检查以下文件并手动修正角色定义：")
    print(f"  {base_template_path}")
    print("\n关键字段需人工确认：")
    print("  - pieces[*].garment_role")
    print("  - pieces[*].zone")
    print("  - pieces[*].symmetry_group")
    print("  - pieces[*].same_shape_group")
    print("  - pieces[*].texture_direction_hint")
    print("  - pieces[*].grain_direction")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
