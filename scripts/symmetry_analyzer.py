#!/usr/bin/env python3
"""
Mask 形状对称分析器。

职责：
- 读取裁片 mask 图像
- 自动判断两个裁片之间的几何关系：identity / mirror_x / mirror_y / mirror_xy
- 选择面积更大/更完整的作为 master
- 无需人工配置，运行时自动分析

使用方式：
    from symmetry_analyzer import analyze_piece_symmetry, find_symmetry_relations
    relations = find_symmetry_relations(pieces_payload, garment_map)
"""
from pathlib import Path

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None


SYMMETRY_IOU_THRESHOLD = 0.92  # IoU 超过此阈值认为是对称关系


def _mask_to_bitmap(img: Image.Image, w: int, h: int) -> list[int]:
    """将 mask 图像转为二值位图列表（0/1）。"""
    resized = img.resize((w, h), Image.NEAREST)
    return [1 if resized.getpixel((x, y)) > 128 else 0
            for y in range(h) for x in range(w)]


def _compute_iou(a: list[int], b: list[int]) -> float:
    """计算两个二值掩码的 IoU（Intersection over Union）。"""
    inter = sum(1 for i, j in zip(a, b) if i and j)
    uni = sum(1 for i, j in zip(a, b) if i or j)
    return inter / uni if uni else 0.0


def analyze_mask_symmetry(left_mask_path: str | Path, right_mask_path: str | Path) -> tuple[str | None, float]:
    """分析两个 mask 之间的最优对称变换。

    返回：(transform_type, iou)
    - transform_type: "identity" | "mirror_x" | "mirror_y" | "mirror_xy" | None
    - iou: 最优变换的 IoU 分数
    """
    if Image is None:
        return None, 0.0

    left = Image.open(left_mask_path).convert("L")
    right = Image.open(right_mask_path).convert("L")

    w = max(left.width, right.width)
    h = max(left.height, right.height)

    left_bits = _mask_to_bitmap(left, w, h)
    right_bits = _mask_to_bitmap(right, w, h)
    left_mirror_x = _mask_to_bitmap(ImageOps.mirror(left), w, h)
    left_mirror_y = _mask_to_bitmap(ImageOps.flip(left), w, h)
    left_mirror_xy = _mask_to_bitmap(ImageOps.flip(ImageOps.mirror(left)), w, h)

    candidates = [
        ("identity", _compute_iou(left_bits, right_bits)),
        ("mirror_x", _compute_iou(left_mirror_x, right_bits)),
        ("mirror_y", _compute_iou(left_mirror_y, right_bits)),
        ("mirror_xy", _compute_iou(left_mirror_xy, right_bits)),
    ]

    best_type, best_iou = max(candidates, key=lambda x: x[1])
    if best_iou >= SYMMETRY_IOU_THRESHOLD:
        # 将 transform_type 转为 render 层需要的 mirror_x/mirror_y 格式
        if best_type == "identity":
            return {"mirror_x": False, "mirror_y": False}, best_iou
        elif best_type == "mirror_x":
            return {"mirror_x": True, "mirror_y": False}, best_iou
        elif best_type == "mirror_y":
            return {"mirror_x": False, "mirror_y": True}, best_iou
        elif best_type == "mirror_xy":
            return {"mirror_x": True, "mirror_y": True}, best_iou
    return None, best_iou


def find_symmetry_relations(pieces_payload: dict, garment_map: dict) -> dict[str, list[dict]]:
    """基于 mask 形状自动发现所有对称关系。

    策略：
    1. 按 symmetry_group 分组（如果 garment_map 中已有 symmetry_group）
    2. 对每组内的每对裁片进行 mask 形状分析
    3. 如果 IoU > 阈值，记录关系
    4. 每组选择面积最大的裁片作为 master

    返回：{master_piece_id: [slave_info, ...]}
    """
    pieces = pieces_payload.get("pieces", [])
    gm_pieces = {p["piece_id"]: p for p in garment_map.get("pieces", [])}

    # 按 symmetry_group 分组
    groups: dict[str, list[str]] = {}
    for pid, gm in gm_pieces.items():
        sg = gm.get("symmetry_group", "")
        if sg:
            groups.setdefault(sg, []).append(pid)

    # 也检查 same_shape_group
    for pid, gm in gm_pieces.items():
        ssg = gm.get("same_shape_group", "")
        if ssg and ssg not in groups:
            groups.setdefault(ssg, []).append(pid)

    relations: dict[str, list[dict]] = {}

    for group_name, pids in groups.items():
        if len(pids) < 2:
            continue

        # 按面积降序排序，面积最大的作为 master 候选
        pids_sorted = sorted(
            pids,
            key=lambda pid: next((p.get("area", 0) for p in pieces if p["piece_id"] == pid), 0),
            reverse=True,
        )

        master_pid = pids_sorted[0]
        master_piece = next((p for p in pieces if p["piece_id"] == master_pid), None)
        if not master_piece or not master_piece.get("mask_path"):
            continue

        master_mask = Path(master_piece["mask_path"])
        if not master_mask.exists():
            continue

        for slave_pid in pids_sorted[1:]:
            slave_piece = next((p for p in pieces if p["piece_id"] == slave_pid), None)
            if not slave_piece or not slave_piece.get("mask_path"):
                continue

            slave_mask = Path(slave_piece["mask_path"])
            if not slave_mask.exists():
                continue

            transform, iou = analyze_mask_symmetry(master_mask, slave_mask)
            if transform:
                relations.setdefault(master_pid, []).append({
                    "target_piece_id": slave_pid,
                    "mirror_x": transform["mirror_x"],
                    "mirror_y": transform["mirror_y"],
                    "iou": round(iou, 4),
                    "group": group_name,
                })

    return relations
