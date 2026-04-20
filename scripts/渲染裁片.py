#!/usr/bin/env python3
"""
使用可用的面料、图案和纯色填充服装裁片，输出透明 PNG、预览图与清单。
"""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageColor, ImageOps

try:
    from template_loader import normalize_piece_asset_paths
except Exception:
    normalize_piece_asset_paths = None


def load_json(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text = text.replace("False", "false").replace("True", "true")
        return json.loads(text)


def approved_textures(texture_set: dict, base_dir: Path) -> dict:
    """加载可用面料资产。"""
    textures = {}
    for item in texture_set.get("textures", []):
        if not item.get("approved", False):
            continue
        path = Path(item.get("path", ""))
        if not path.is_absolute():
            path = base_dir / path
        if not path.exists():
            continue
        texture_id = item.get("texture_id") or item.get("role")
        textures[texture_id] = {**item, "path": str(path.resolve())}
        role = item.get("role")
        if role and role not in textures:
            textures[role] = textures[texture_id]
    return textures


def approved_solids(texture_set: dict) -> dict:
    """加载可用纯色。"""
    solids = {}
    for item in texture_set.get("solids", []):
        if item.get("approved", True):
            solids[item.get("solid_id", "solid")] = item
    return solids


def approved_motifs(texture_set: dict, base_dir: Path) -> dict:
    """加载可用图案资产。"""
    motifs = {}
    for item in texture_set.get("motifs", []):
        if not item.get("approved", False):
            continue
        path = Path(item.get("path", ""))
        if not path.is_absolute():
            path = base_dir / path
        if not path.exists():
            continue
        motif_id = item.get("motif_id") or item.get("role")
        motifs[motif_id] = {**item, "path": str(path.resolve())}
        role = item.get("role")
        if role and role not in motifs:
            motifs[role] = motifs[motif_id]
    return motifs


def choose_texture_id(piece: dict, index: int, textures: dict) -> str:
    """根据裁片特征自动选择面料编号。"""
    role = piece.get("piece_role", "")
    aspect = piece["width"] / max(1, piece["height"])
    if role == "main" or index == 0:
        return "main" if "main" in textures else next(iter(textures))
    if role == "strip" or aspect >= 3 or aspect <= 0.3:
        return "dark" if "dark" in textures else "accent" if "accent" in textures else next(iter(textures))
    if piece["area"] > 900000:
        return "secondary" if "secondary" in textures else "main" if "main" in textures else next(iter(textures))
    return "accent" if "accent" in textures else "secondary" if "secondary" in textures else next(iter(textures))


def make_default_fill_plan(pieces_payload: dict, texture_set: dict, textures: dict, solids: dict) -> dict:
    """当未提供填充计划时，根据启发式规则自动生成。"""
    entries = []
    solid_id = next(iter(solids), "")
    for index, piece in enumerate(pieces_payload.get("pieces", [])):
        aspect = piece["width"] / max(1, piece["height"])
        if (piece.get("piece_role") == "strip" or aspect >= 3 or aspect <= 0.3) and solid_id:
            entries.append({"piece_id": piece["piece_id"], "fill_type": "solid", "solid_id": solid_id, "reason": "细条或窄裁片使用可用纯色"})
        else:
            texture_id = choose_texture_id(piece, index, textures)
            entries.append({"piece_id": piece["piece_id"], "fill_type": "texture", "texture_id": texture_id, "scale": 1.0, "rotation": 0, "offset_x": 0, "offset_y": 0, "mirror_x": False, "mirror_y": False, "reason": "按裁片尺寸与角色自动分配"})
    return {"plan_id": "auto_piece_fill_plan", "texture_set_id": texture_set.get("texture_set_id", ""), "locked": False, "pieces": entries}


def tile_image(tile: Image.Image, size: tuple[int, int], offset_x: int = 0, offset_y: int = 0) -> Image.Image:
    """将纹理图块平铺到指定尺寸画布。"""
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    start_x = -tile.width + (offset_x % max(1, tile.width))
    start_y = -tile.height + (offset_y % max(1, tile.height))
    for y in range(start_y, size[1], tile.height):
        for x in range(start_x, size[0], tile.width):
            out.alpha_composite(tile, (x, y))
    return out


def auto_rotation_for_direction(texture: Image.Image, texture_direction: str, piece: dict) -> float:
    """根据裁片方向和纹理方向计算自动旋转角度。

    longitudinal = 纹理沿裁片长度方向
    transverse   = 纹理沿裁片宽度方向
    """
    if not texture_direction:
        return 0
    piece_aspect = piece["width"] / max(1, piece["height"])
    tex_aspect = texture.width / max(1, texture.height)

    # 纹理方向性不明显的（接近正方形），不需要自动旋转
    if 0.7 <= tex_aspect <= 1.4:
        return 0

    is_tex_horizontal = tex_aspect > 1
    is_piece_horizontal = piece_aspect > 1

    if texture_direction == "longitudinal":
        # 纹理应沿裁片长度方向
        if is_piece_horizontal != is_tex_horizontal:
            return 90
    elif texture_direction == "transverse":
        # 纹理应沿裁片宽度方向（与长度垂直）
        if is_piece_horizontal == is_tex_horizontal:
            return 90
    return 0


def transform_texture(texture: Image.Image, plan: dict, piece: dict | None = None) -> Image.Image:
    """对面纹理应用缩放、旋转、镜像变换。"""
    out = texture.convert("RGBA")
    if plan.get("mirror_x"):
        out = ImageOps.mirror(out)
    if plan.get("mirror_y"):
        out = ImageOps.flip(out)
    scale = max(0.05, float(plan.get("scale", 1) or 1))
    if abs(scale - 1) > 0.001:
        out = out.resize((max(1, round(out.width * scale)), max(1, round(out.height * scale))), Image.Resampling.LANCZOS)
    rotation = float(plan.get("rotation", 0) or 0)
    # 应用 texture_direction 自动旋转
    if piece:
        rotation += auto_rotation_for_direction(out, plan.get("texture_direction", ""), piece)
        # 默认不再用纸样摆放方向旋转普通纹理；只有方向性图案显式声明时才补偿。
        piece_orientation = piece.get("pattern_orientation", 0)
        if plan.get("respect_pattern_orientation") and piece_orientation:
            rotation += piece_orientation
    if abs(rotation % 360) > 0.001:
        out = out.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
    return out


def apply_opacity(image: Image.Image, opacity: float) -> Image.Image:
    """调整图像不透明度。"""
    out = image.convert("RGBA")
    opacity = max(0.0, min(1.0, float(opacity)))
    if opacity >= 0.999:
        return out
    alpha = out.getchannel("A").point(lambda value: round(value * opacity))
    out.putalpha(alpha)
    return out


def apply_mask(content: Image.Image, mask_path: str | Path) -> Image.Image:
    """应用裁片遮罩作为 Alpha 通道。"""
    with Image.open(mask_path).convert("L") as mask:
        if content.size != mask.size:
            content = content.resize(mask.size, Image.Resampling.LANCZOS)
        out = content.convert("RGBA")
        out.putalpha(mask)
        return out


def anchor_position(anchor: str, canvas_size: tuple[int, int], item_size: tuple[int, int], offset_x: int, offset_y: int) -> tuple[int, int]:
    """根据锚点计算图案放置位置。"""
    width, height = canvas_size
    item_w, item_h = item_size
    positions = {
        "center": ((width - item_w) // 2, (height - item_h) // 2),
        "top": ((width - item_w) // 2, 0),
        "bottom": ((width - item_w) // 2, height - item_h),
        "left": (0, (height - item_h) // 2),
        "right": (width - item_w, (height - item_h) // 2),
        "top_left": (0, 0),
        "top_right": (width - item_w, 0),
        "bottom_left": (0, height - item_h),
        "bottom_right": (width - item_w, height - item_h),
    }
    x, y = positions.get(anchor, positions["center"])
    return x + offset_x, y + offset_y


def compute_mask_centroid(mask: Image.Image) -> tuple[float, float]:
    """计算二值 mask 的像素 centroid（密度加权重心）。
    对于不对称裁片，centroid 可能偏离几何中心，更接近视觉重心。"""
    pixels = list(mask.get_flattened_data())
    w, h = mask.size
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if pixels[y * w + x] > 128:
                xs.append(x)
                ys.append(y)
    if not xs:
        return w / 2.0, h / 2.0
    return sum(xs) / len(xs), sum(ys) / len(ys)


def compute_motif_visibility(motif: Image.Image, piece_size: tuple[int, int], pos: tuple[int, int], mask_path: str | Path) -> float:
    """计算 motif 在裁片内的可见比例（0-1）。

    方法：
    1. 创建与裁片同尺寸的画布，将 motif 放置在 pos 位置
    2. 提取 alpha 通道
    3. 与裁片 mask 相乘
    4. 可见像素数 / motif 总像素数
    """
    canvas = Image.new("RGBA", piece_size, (0, 0, 0, 0))
    canvas.alpha_composite(motif, pos)
    alpha = canvas.getchannel("A")

    with Image.open(mask_path).convert("L") as mask:
        if mask.size != piece_size:
            mask = mask.resize(piece_size, Image.Resampling.LANCZOS)
        mask_pixels = list(mask.get_flattened_data())
        alpha_pixels = list(alpha.get_flattened_data())

        visible = 0
        total = 0
        for mp, ap in zip(mask_pixels, alpha_pixels):
            if ap > 10:  # motif 有内容的像素
                total += 1
                if mp > 128:  # 且在 mask 内
                    visible += 1

    return visible / max(1, total)


def smart_motif_placement(motif: Image.Image, piece: dict, layer: dict) -> tuple[int, int]:
    """智能计算 motif 放置位置，确保不被裁片边界切断。

    策略：
    1. 先按 anchor + offset 计算初始位置
    2. 用裁片 mask 检查 motif 可见比例
    3. 如果可见比例 < 0.85，在初始位置周围 ±20% 范围内搜索更好的位置
    4. 返回使可见比例最大的位置
    """
    piece_size = (piece["width"], piece["height"])
    mask_path = piece.get("mask_path", "")

    # 计算初始位置
    initial_pos = anchor_position(
        layer.get("anchor", "center"),
        piece_size,
        motif.size,
        int(layer.get("offset_x", 0) or 0),
        int(layer.get("offset_y", 0) or 0),
    )

    if not mask_path or not Path(mask_path).exists():
        return initial_pos

    # 检查初始位置的可见度
    initial_vis = compute_motif_visibility(motif, piece_size, initial_pos, mask_path)
    if initial_vis >= 0.85:
        return initial_pos

    # 搜索更好的位置：在初始位置周围 ±20% 范围内网格搜索
    best_pos = initial_pos
    best_vis = initial_vis
    search_range = min(piece["width"], piece["height"]) // 5  # ±20%
    step = max(2, search_range // 8)

    for dx in range(-search_range, search_range + 1, step):
        for dy in range(-search_range, search_range + 1, step):
            test_pos = (initial_pos[0] + dx, initial_pos[1] + dy)
            # 确保 motif 还在画布范围内（允许部分出界，但不能全出界）
            if test_pos[0] + motif.width < 0 or test_pos[0] > piece["width"]:
                continue
            if test_pos[1] + motif.height < 0 or test_pos[1] > piece["height"]:
                continue
            vis = compute_motif_visibility(motif, piece_size, test_pos, mask_path)
            if vis > best_vis:
                best_vis = vis
                best_pos = test_pos

    return best_pos


def render_texture_piece(piece: dict, plan: dict, texture_info: dict) -> Image.Image:
    """渲染单层纹理裁片。"""
    texture = Image.open(texture_info["path"]).convert("RGBA")
    texture = transform_texture(texture, plan)
    content = tile_image(texture, (piece["width"], piece["height"]), int(plan.get("offset_x", 0) or 0), int(plan.get("offset_y", 0) or 0))
    return apply_mask(content, piece["mask_path"])


def render_solid_piece(piece: dict, plan: dict, solids: dict) -> Image.Image:
    """渲染单层纯色裁片。"""
    solid = solids.get(plan.get("solid_id")) or next(iter(solids.values()), {"color": "#6f9a4d"})
    try:
        color = ImageColor.getrgb(solid.get("color", "#6f9a4d")) + (255,)
    except Exception:
        color = (107, 143, 69, 255)
    return apply_mask(Image.new("RGBA", (piece["width"], piece["height"]), color), piece["mask_path"])


def render_solid_layer(piece: dict, layer: dict, solids: dict) -> Image.Image:
    """渲染纯色图层。"""
    solid = solids.get(layer.get("solid_id")) or next(iter(solids.values()), {"color": "#6f9a4d"})
    try:
        color = ImageColor.getrgb(solid.get("color", "#6f9a4d")) + (255,)
    except Exception:
        color = (107, 143, 69, 255)
    return apply_opacity(Image.new("RGBA", (piece["width"], piece["height"]), color), float(layer.get("opacity", 1) or 1))


def render_texture_layer(piece: dict, layer: dict, texture_info: dict) -> Image.Image:
    """渲染纹理图层。"""
    texture = Image.open(texture_info["path"]).convert("RGBA")
    texture = transform_texture(texture, layer, piece)
    content = tile_image(texture, (piece["width"], piece["height"]), int(layer.get("offset_x", 0) or 0), int(layer.get("offset_y", 0) or 0))
    return apply_opacity(content, float(layer.get("opacity", 1) or 1))


def render_motif_layer(piece: dict, layer: dict, motif_info: dict, underlay: Image.Image = None) -> Image.Image:
    """渲染图案图层，保持 motif 原始透明度和颜色。"""
    motif = Image.open(motif_info["path"]).convert("RGBA")
    if layer.get("mirror_x"):
        motif = ImageOps.mirror(motif)
    if layer.get("mirror_y"):
        motif = ImageOps.flip(motif)
    scale = max(0.05, float(layer.get("scale", 1) or 1))
    width_scale = max(0.05, float(layer.get("max_width_scale", scale) or scale))
    height_scale = max(0.05, float(layer.get("max_height_scale", scale) or scale))
    target_max_w = max(1, round(piece["width"] * width_scale))
    target_max_h = max(1, round(piece["height"] * height_scale))
    if layer.get("seam_lock"):
        if layer.get("fit_within_piece"):
            ratio = min(target_max_w / max(1, motif.width), target_max_h / max(1, motif.height))
        else:
            ratio = target_max_h / max(1, motif.height)
    else:
        ratio = min(target_max_w / max(1, motif.width), target_max_h / max(1, motif.height))
    motif = motif.resize((max(1, round(motif.width * ratio)), max(1, round(motif.height * ratio))), Image.Resampling.LANCZOS)
    rotation = float(layer.get("rotation", 0) or 0)
    # 补偿裁片在 pattern 中的方向（倒置裁片需额外旋转 motif）
    piece_orientation = piece.get("pattern_orientation", 0)
    if piece_orientation:
        rotation += piece_orientation
    if abs(rotation % 360) > 0.001:
        motif = motif.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
    content = Image.new("RGBA", (piece["width"], piece["height"]), (0, 0, 0, 0))
    if layer.get("seam_lock"):
        pos = anchor_position(
            layer.get("anchor", "center"),
            (piece["width"], piece["height"]),
            motif.size,
            int(layer.get("offset_x", 0) or 0),
            int(layer.get("offset_y", 0) or 0),
        )
    else:
        # 智能放置：防切割 + 视觉重心
        pos = smart_motif_placement(motif, piece, layer)
    content.alpha_composite(motif, pos)
    return content


def layer_to_image(piece: dict, layer: dict, textures: dict, solids: dict, motifs: dict, underlay: Image.Image = None) -> Image.Image:
    """将单层定义渲染为图像。"""
    fill_type = layer.get("fill_type", "texture")
    if fill_type == "solid":
        return render_solid_layer(piece, layer, solids)
    if fill_type == "motif":
        motif_id = layer.get("motif_id")
        motif_info = motifs.get(motif_id)
        if not motif_info:
            raise RuntimeError(f"裁片 {piece['piece_id']} 的图案 {motif_id!r} 不可用或缺失。")
        return render_motif_layer(piece, layer, motif_info, underlay=underlay)
    texture_id = layer.get("texture_id")
    texture_info = textures.get(texture_id)
    if not texture_info:
        raise RuntimeError(f"裁片 {piece['piece_id']} 的面料 {texture_id!r} 不可用或缺失。")
    return render_texture_layer(piece, layer, texture_info)


def render_layered_piece(piece: dict, plan: dict, textures: dict, solids: dict, motifs: dict) -> Image.Image:
    """渲染可能包含多层的裁片。"""
    layers = [plan.get("base"), plan.get("overlay"), plan.get("trim")]
    layers = [layer for layer in layers if isinstance(layer, dict)]
    if not layers:
        # 支持单层计划。
        if plan.get("fill_type") == "solid":
            return render_solid_piece(piece, plan, solids)
        texture_id = plan.get("texture_id")
        texture_info = textures.get(texture_id)
        if not texture_info:
            raise RuntimeError(f"裁片 {piece['piece_id']} 的面料 {texture_id!r} 不可用或缺失。")
        return render_texture_piece(piece, plan, texture_info)
    content = Image.new("RGBA", (piece["width"], piece["height"]), (0, 0, 0, 0))
    for layer in layers:
        # Motifs are generated/cropped as final cutouts. Do not post-blend them
        # into the base fabric; keep the original motif alpha and color.
        underlay = None
        layer_image = layer_to_image(piece, layer, textures, solids, motifs, underlay=underlay)
        content.alpha_composite(layer_image)
    return apply_mask(content, piece["mask_path"])


def _align_image_size(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """将图像对齐到目标尺寸，居中裁剪或填充透明背景。"""
    if img.width == target_w and img.height == target_h:
        return img
    new = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    x = (target_w - img.width) // 2
    y = (target_h - img.height) // 2
    new.paste(img, (x, y))
    return new


def _mask_image(piece: dict) -> Image.Image:
    return Image.open(piece["mask_path"]).convert("L")


def _apply_mask_image(content: Image.Image, mask: Image.Image) -> Image.Image:
    if content.size != mask.size:
        content = content.resize(mask.size, Image.Resampling.LANCZOS)
    out = content.convert("RGBA")
    out.putalpha(mask)
    return out


def _largest_histogram_rect(heights: list[int], row_bottom: int, seam_x: int | None = None) -> tuple[int, int, int, int, int]:
    best = (0, 0, 0, 0, 0)
    stack: list[int] = []
    for idx in range(len(heights) + 1):
        current = heights[idx] if idx < len(heights) else 0
        while stack and current < heights[stack[-1]]:
            top = stack.pop()
            h = heights[top]
            left = stack[-1] + 1 if stack else 0
            w = idx - left
            if h > 0 and w > 0 and (seam_x is None or left < seam_x < left + w):
                area = w * h
                if area > best[0]:
                    best = (area, left, row_bottom - h + 1, w, h)
        stack.append(idx)
    return best


def _largest_rect_in_binary(mask: Image.Image, seam_x: int | None = None) -> tuple[int, int, int, int]:
    binary = mask.convert("L").point(lambda value: 255 if value > 128 else 0)
    max_side = 420
    scale = min(1.0, max_side / max(1, max(binary.size)))
    if scale < 1.0:
        resized = binary.resize((max(1, round(binary.width * scale)), max(1, round(binary.height * scale))), Image.Resampling.NEAREST)
        scaled_seam = round(seam_x * scale) if seam_x is not None else None
    else:
        resized = binary
        scaled_seam = seam_x
    pixels = list(resized.get_flattened_data())
    heights = [0] * resized.width
    best = (0, 0, 0, 0, 0)
    for y in range(resized.height):
        row = y * resized.width
        for x in range(resized.width):
            heights[x] = heights[x] + 1 if pixels[row + x] > 128 else 0
        current = _largest_histogram_rect(heights, y, scaled_seam)
        if current[0] > best[0]:
            best = current
    if best[0] <= 0:
        bbox = binary.getbbox()
        return bbox or (0, 0, binary.width, binary.height)
    _, x, y, w, h = best
    if scale < 1.0:
        return (
            max(0, round(x / scale)),
            max(0, round(y / scale)),
            min(mask.width, round((x + w) / scale)),
            min(mask.height, round((y + h) / scale)),
        )
    return (x, y, x + w, y + h)


def _seam_span(mask: Image.Image, side: str) -> tuple[int, int]:
    """Return y span near the actual front seam edge, not the paper top."""
    w, h = mask.size
    depth = max(8, min(64, max(1, w // 6)))
    if side == "right":
        xs = range(max(0, w - depth), w)
    else:
        xs = range(0, min(w, depth))
    pixels = mask.load()
    ys = [
        y
        for y in range(h)
        if any(pixels[x, y] > 128 for x in xs)
    ]
    if ys:
        return min(ys), max(ys) + 1
    bbox = mask.getbbox()
    if bbox:
        return bbox[1], bbox[3]
    return 0, h


def _front_pair_layout(left_piece: dict, right_piece: dict) -> dict:
    left_mask = _mask_image(left_piece)
    right_mask = _mask_image(right_piece)
    left_span = _seam_span(left_mask, "right")
    right_span = _seam_span(right_mask, "left")
    left_mid = (left_span[0] + left_span[1]) / 2
    right_mid = (right_span[0] + right_span[1]) / 2
    left_y = 0
    right_y = round(left_mid - right_mid)
    min_y = min(left_y, right_y)
    if min_y < 0:
        left_y -= min_y
        right_y -= min_y
    width = left_mask.width + right_mask.width
    height = max(left_y + left_mask.height, right_y + right_mask.height)
    return {
        "left_mask": left_mask,
        "right_mask": right_mask,
        "left_xy": (0, left_y),
        "right_xy": (left_mask.width, right_y),
        "size": (width, height),
        "seam_x": left_mask.width,
        "left_span": left_span,
        "right_span": right_span,
    }


def _combined_front_mask(layout: dict) -> Image.Image:
    mask = Image.new("L", layout["size"], 0)
    mask.paste(layout["left_mask"], layout["left_xy"])
    mask.paste(layout["right_mask"], layout["right_xy"])
    return mask


def _front_pair_ids(pieces_payload: dict, entries: dict) -> tuple[str, str] | tuple[None, None]:
    candidates = [
        pid for pid, plan in entries.items()
        if plan.get("front_pair_seam_locked")
        or (isinstance(plan.get("base"), dict) and plan["base"].get("global_front_texture"))
        or (isinstance(plan.get("overlay"), dict) and plan["overlay"].get("global_front_motif"))
        or (isinstance(plan.get("overlay"), dict) and plan["overlay"].get("motif_id") in {"theme_front_full", "theme_front_left", "theme_front_right"})
    ]
    piece_by_id = {piece["piece_id"]: piece for piece in pieces_payload.get("pieces", [])}
    candidates = [pid for pid in candidates if pid in piece_by_id]
    if len(candidates) < 2:
        return None, None

    def _side_score(pid: str) -> tuple[int, int, str]:
        plan = entries[pid]
        overlay = plan.get("overlay") if isinstance(plan.get("overlay"), dict) else {}
        text = " ".join(str(value) for value in (overlay.get("motif_id"), overlay.get("legacy_split_motif_id"), plan.get("reason", "")))
        if "theme_front_left" in text or "左前片" in text:
            side = 0
        elif "theme_front_right" in text or "右前片" in text:
            side = 1
        else:
            side = 0
        return side, piece_by_id[pid].get("source_x", 0), pid

    ordered = sorted(candidates[:], key=_side_score)
    if len(ordered) >= 2:
        return ordered[0], ordered[1]
    return None, None


def _load_front_motif(overlay: dict, motifs: dict) -> Image.Image | None:
    motif_id = overlay.get("motif_id")
    if motif_id in {"theme_front_left", "theme_front_right"} and motifs.get("theme_front_full"):
        motif_id = "theme_front_full"
    motif_info = motifs.get(motif_id)
    if motif_info and motif_id not in {"theme_front_left", "theme_front_right"}:
        return Image.open(motif_info["path"]).convert("RGBA")
    left_info = motifs.get("theme_front_left")
    right_info = motifs.get("theme_front_right")
    if not left_info or not right_info:
        return None
    left = Image.open(left_info["path"]).convert("RGBA")
    right = Image.open(right_info["path"]).convert("RGBA")
    height = max(left.height, right.height)
    full = Image.new("RGBA", (left.width + right.width, height), (0, 0, 0, 0))
    full.alpha_composite(left, (0, (height - left.height) // 2))
    full.alpha_composite(right, (left.width, (height - right.height) // 2))
    return full


def _render_front_pair_base(canvas_size: tuple[int, int], base: dict, textures: dict, solids: dict) -> Image.Image:
    if not isinstance(base, dict):
        return Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    if base.get("fill_type") == "solid":
        solid = solids.get(base.get("solid_id")) or next(iter(solids.values()), {"color": "#6f9a4d"})
        try:
            color = ImageColor.getrgb(solid.get("color", "#6f9a4d")) + (255,)
        except Exception:
            color = (107, 143, 69, 255)
        return apply_opacity(Image.new("RGBA", canvas_size, color), float(base.get("opacity", 1) or 1))
    texture_id = base.get("texture_id")
    texture_info = textures.get(texture_id)
    if not texture_info:
        raise RuntimeError(f"左右前片连续纹理 {texture_id!r} 不可用或缺失。")
    texture = Image.open(texture_info["path"]).convert("RGBA")
    pseudo_piece = {"width": canvas_size[0], "height": canvas_size[1]}
    texture = transform_texture(texture, base, pseudo_piece)
    return apply_opacity(
        tile_image(texture, canvas_size, int(base.get("offset_x", 0) or 0), int(base.get("offset_y", 0) or 0)),
        float(base.get("opacity", 1) or 1),
    )


def _render_front_pair_motif(canvas_size: tuple[int, int], layout: dict, overlay: dict, motifs: dict) -> Image.Image:
    motif = _load_front_motif(overlay, motifs)
    content = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    if motif is None:
        return content
    if overlay.get("mirror_x"):
        motif = ImageOps.mirror(motif)
    if overlay.get("mirror_y"):
        motif = ImageOps.flip(motif)
    rotation = float(overlay.get("rotation", 0) or 0)
    if abs(rotation % 360) > 0.001:
        motif = motif.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
    combined_mask = _combined_front_mask(layout)
    safe = _largest_rect_in_binary(combined_mask, seam_x=layout["seam_x"])
    safe_w = max(1, safe[2] - safe[0])
    safe_h = max(1, safe[3] - safe[1])
    multiplier = max(0.05, float(overlay.get("front_pair_scale_multiplier", overlay.get("scale", 0.70)) or 0.70))
    ratio = min(safe_w / max(1, motif.width), safe_h / max(1, motif.height)) * multiplier
    motif = motif.resize((max(1, round(motif.width * ratio)), max(1, round(motif.height * ratio))), Image.Resampling.LANCZOS)
    x = round(safe[0] + (safe_w - motif.width) / 2 + int(overlay.get("offset_x", 0) or 0))
    y = round(safe[1] + (safe_h - motif.height) / 2 + int(overlay.get("offset_y", 0) or 0))
    content.alpha_composite(motif, (x, y))
    return apply_opacity(content, float(overlay.get("opacity", 1) or 1))


def render_front_pair(
    pieces_payload: dict,
    entries: dict,
    textures: dict,
    solids: dict,
    motifs: dict,
    out_dir: Path,
) -> dict[str, Image.Image]:
    """Render left/right front pieces from one logical sewn-front canvas."""
    left_id, right_id = _front_pair_ids(pieces_payload, entries)
    if not left_id or not right_id:
        return {}
    piece_by_id = {piece["piece_id"]: piece for piece in pieces_payload.get("pieces", [])}
    left_piece = piece_by_id[left_id]
    right_piece = piece_by_id[right_id]
    left_plan = entries[left_id]
    right_plan = entries[right_id]
    layout = _front_pair_layout(left_piece, right_piece)
    content = _render_front_pair_base(layout["size"], left_plan.get("base") or right_plan.get("base"), textures, solids)
    overlay = left_plan.get("overlay") if isinstance(left_plan.get("overlay"), dict) else right_plan.get("overlay")
    if isinstance(overlay, dict) and overlay.get("fill_type") == "motif":
        content.alpha_composite(_render_front_pair_motif(layout["size"], layout, overlay, motifs))

    combined_mask = _combined_front_mask(layout)
    check = Image.new("RGBA", layout["size"], (255, 255, 255, 255))
    check.alpha_composite(_apply_mask_image(content, combined_mask))
    check.save(out_dir / "front_pair_check.png")

    left_x, left_y = layout["left_xy"]
    right_x, right_y = layout["right_xy"]
    left_crop = content.crop((left_x, left_y, left_x + left_piece["width"], left_y + left_piece["height"]))
    right_crop = content.crop((right_x, right_y, right_x + right_piece["width"], right_y + right_piece["height"]))
    rendered = {
        left_id: _apply_mask_image(left_crop, layout["left_mask"]),
        right_id: _apply_mask_image(right_crop, layout["right_mask"]),
    }
    for pid, piece, plan in ((left_id, left_piece, left_plan), (right_id, right_piece, right_plan)):
        trim = plan.get("trim")
        if isinstance(trim, dict):
            trim_image = layer_to_image(piece, trim, textures, solids, motifs)
            rendered[pid].alpha_composite(_apply_mask_image(trim_image, _mask_image(piece)))
    return rendered


def render_all(pieces_payload: dict, texture_set: dict, fill_plan: dict, out_dir: Path, texture_set_path: Path) -> list[dict]:
    """渲染所有裁片。

    对称优化：对于配置了 symmetry_source 的 slave 裁片，复制 master 裁片的
    渲染结果并通过 Pillow 做镜像/翻转变换，不再独立走纹理填充流程。
    """
    textures = approved_textures(texture_set, texture_set_path.parent)
    solids = approved_solids(texture_set)
    motifs = approved_motifs(texture_set, texture_set_path.parent)
    if not textures:
        raise RuntimeError("没有可用面料。请在面料组合.json 中设置 approved=true 后再渲染。")
    entries = {item.get("piece_id"): item for item in fill_plan.get("pieces", [])}
    pieces_dir = out_dir / "pieces"
    pieces_dir.mkdir(parents=True, exist_ok=True)

    # 收集 slave→master 映射
    slave_map = {}
    for item in fill_plan.get("pieces", []):
        src = item.get("symmetry_source")
        if src:
            slave_map[item["piece_id"]] = {
                "source": src,
                "transform": item.get("symmetry_transform", {}),
            }

    rendered_paths = {}
    rendered = []
    front_pair_images = render_front_pair(pieces_payload, entries, textures, solids, motifs, out_dir)
    for piece in pieces_payload.get("pieces", []):
        pid = piece["piece_id"]
        image = front_pair_images.get(pid)
        if image is None:
            continue
        output_path = pieces_dir / f"{pid}.png"
        image.save(output_path)
        rendered_paths[pid] = output_path
        rendered.append({"piece_id": pid, "output_path": str(output_path.resolve()), "plan": entries.get(pid)})

    # 阶段 1：渲染所有 master pieces（非 slave）
    for piece in pieces_payload.get("pieces", []):
        pid = piece["piece_id"]
        if pid in front_pair_images:
            continue
        if pid in slave_map:
            continue
        plan = entries.get(pid)
        if not plan:
            raise RuntimeError(f"裁片 {pid} 缺少填充计划")
        image = render_layered_piece(piece, plan, textures, solids, motifs)
        output_path = pieces_dir / f"{pid}.png"
        image.save(output_path)
        rendered_paths[pid] = output_path
        rendered.append({"piece_id": pid, "output_path": str(output_path.resolve()), "plan": plan})

    # 阶段 2：slave pieces 复制 master PNG + Pillow 变换
    for piece in pieces_payload.get("pieces", []):
        pid = piece["piece_id"]
        if pid not in slave_map:
            continue
        slave_info = slave_map[pid]
        master_path = rendered_paths.get(slave_info["source"])
        if not master_path:
            raise RuntimeError(f"slave 裁片 {pid} 的 master {slave_info['source']} 未渲染")

        with Image.open(master_path).convert("RGBA") as img:
            transform = slave_info["transform"]
            if transform.get("mirror_x"):
                img = ImageOps.mirror(img)
            if transform.get("mirror_y"):
                img = ImageOps.flip(img)

            # 尺寸对齐：slave 的 mask 尺寸理论上和 master 相同，但可能有 1-2px 偏差
            target_w = piece.get("width", img.width)
            target_h = piece.get("height", img.height)
            if img.width != target_w or img.height != target_h:
                if abs(img.width - target_w) > 5 or abs(img.height - target_h) > 5:
                    print(f"[警告] 裁片 {pid} 与 master {slave_info['source']} 尺寸偏差过大 "
                          f"({img.width}x{img.height} vs {target_w}x{target_h})，改为独立渲染")
                    plan = entries.get(pid)
                    img = render_layered_piece(piece, plan, textures, solids, motifs)
                else:
                    img = _align_image_size(img, target_w, target_h)

            output_path = pieces_dir / f"{pid}.png"
            img.save(output_path)
            rendered_paths[pid] = output_path
            rendered.append({"piece_id": pid, "output_path": str(output_path.resolve()), "plan": entries.get(pid)})

    return rendered


def compose_preview(pieces_payload: dict, rendered: list[dict], out_path: Path) -> Path:
    """合成给用户查看的完整预览图；不作为 LLM 流程输入。"""
    canvas = pieces_payload.get("canvas") or {}
    width = int(canvas.get("width") or max(piece["source_x"] + piece["width"] for piece in pieces_payload["pieces"]))
    height = int(canvas.get("height") or max(piece["source_y"] + piece["height"] for piece in pieces_payload["pieces"]))
    preview = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    by_id = {item["piece_id"]: item for item in rendered}
    for piece in pieces_payload.get("pieces", []):
        item = by_id[piece["piece_id"]]
        with Image.open(item["output_path"]).convert("RGBA") as img:
            preview.alpha_composite(img, (piece["source_x"], piece["source_y"]))
    preview.save(out_path)
    white = Image.new("RGBA", preview.size, (255, 255, 255, 255))
    white.alpha_composite(preview)
    white.convert("RGB").save(out_path.with_name("preview_white.jpg"), quality=95)
    return out_path


def write_manifest(texture_set: dict, fill_plan: dict, rendered: list[dict], preview_path: Path, out_path: Path) -> Path:
    """写入填充清单。"""
    manifest_dir = out_path.parent.resolve()
    manifest = {
        "texture_set_id": texture_set.get("texture_set_id", ""),
        "fill_plan_id": fill_plan.get("plan_id", ""),
        "preview_path": str(preview_path.resolve()),
        "preview_file": preview_path.name,
        "preview_relpath": str(preview_path.resolve().relative_to(manifest_dir)) if preview_path.resolve().is_relative_to(manifest_dir) else preview_path.name,
        "pieces": [
            {
                "piece_id": item["piece_id"],
                "output_path": item["output_path"],
                "output_file": Path(item["output_path"]).name,
                "output_relpath": str(Path(item["output_path"]).resolve().relative_to(manifest_dir)) if Path(item["output_path"]).resolve().is_relative_to(manifest_dir) else Path(item["output_path"]).name,
                "fill_type": item["plan"].get("fill_type"),
                "texture_id": item["plan"].get("texture_id"),
                "solid_id": item["plan"].get("solid_id"),
                "scale": item["plan"].get("scale", 1),
                "rotation": item["plan"].get("rotation", 0),
                "texture_direction": item["plan"].get("texture_direction", ""),
                "base": item["plan"].get("base"),
                "overlay": item["plan"].get("overlay"),
                "trim": item["plan"].get("trim"),
                "garment_role": item["plan"].get("garment_role"),
                "reason": item["plan"].get("reason", ""),
            }
            for item in rendered
        ],
    }
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="使用可用面料填充服装裁片，输出透明 PNG。")
    parser.add_argument("--pieces", required=True, help="裁片清单 JSON 路径")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--fill-plan", default="", help="裁片填充计划 JSON 路径（可选）")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    pieces_payload = load_json(args.pieces)
    if normalize_piece_asset_paths:
        pieces_payload = normalize_piece_asset_paths(pieces_payload, args.pieces)
    texture_set_path = Path(args.texture_set)
    texture_set = load_json(texture_set_path)
    textures = approved_textures(texture_set, texture_set_path.parent)
    solids = approved_solids(texture_set)
    fill_plan = load_json(args.fill_plan) if args.fill_plan else make_default_fill_plan(pieces_payload, texture_set, textures, solids)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.fill_plan:
        plan_path = out_dir / "piece_fill_plan.json"
        plan_path.write_text(json.dumps(fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    rendered = render_all(pieces_payload, texture_set, fill_plan, out_dir, texture_set_path)

    preview = compose_preview(pieces_payload, rendered, out_dir / "preview.png")

    manifest = write_manifest(texture_set, fill_plan, rendered, preview, out_dir / "texture_fill_manifest.json")
    print(json.dumps(
        {"裁片数量": len(rendered), "预览图": str(preview.resolve()), "白底预览图": str(preview.with_name("preview_white.jpg").resolve()), "清单": str(manifest.resolve())},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
