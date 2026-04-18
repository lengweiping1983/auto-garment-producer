#!/usr/bin/env python3
"""
端到端自动化：

1. 生成或接收 Neo AI 3×3 面料看板。
2. 裁剪为已批准的设计资产。
3. 构建面料组合.json。
4. 从纸样 mask 提取服装裁片。
5. 构建部位映射与艺术指导填充计划。
6. 渲染透明裁片 PNG、预览图、清单和成衣 QC。

Neo AI 负责创作 artwork。本脚本仅准备已批准资产并以确定性方式渲染到裁片中。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageEnhance, ImageFilter, ImageStat


SKILL_DIR = Path(__file__).resolve().parents[1]
NEO_AI_SCRIPT = Path("/Users/lengweiping/.agents/skills/neo-ai/scripts/generate_texture_collection_board.py")


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_step(cmd: list[str], env: dict | None = None) -> None:
    print("运行:", " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)


def latest_collection_board(output_dir: Path) -> Path:
    """在输出目录中找到最新的面料看板图像。"""
    candidates = (
        sorted(output_dir.glob("collection_board_*.png"))
        + sorted(output_dir.glob("collection_board_*.jpg"))
        + sorted(output_dir.glob("collection_board_*.jpeg"))
        + sorted(output_dir.glob("collection_board_*.webp"))
    )
    if not candidates:
        raise RuntimeError(f"输出目录中未找到面料看板图像: {output_dir}")
    return candidates[-1]


def _build_collection_prompt_from_visual_elements(out_dir: Path, visual_elements_path: Path = None) -> str:
    """基于子Agent视觉分析结果构造 3×3 面料看板综合 prompt。
    读取 texture_prompts.json 和 visual_elements.json，生成适合 Neo AI 的 prompt 文本。
    9 个面板全部从 texture_prompts.json 动态读取，无硬编码。"""
    texture_prompts_path = out_dir / "texture_prompts.json"
    visual_path = visual_elements_path or (out_dir / "visual_elements.json")
    if not texture_prompts_path.exists() or not visual_path.exists():
        return ""

    try:
        tp = json.loads(texture_prompts_path.read_text(encoding="utf-8"))
        ve = json.loads(visual_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    # 按 texture_id 索引所有面板提示词
    prompts = {}
    for p in tp.get("prompts", []):
        prompts[p.get("texture_id", "")] = p.get("prompt", "")

    style = ve.get("style", {})

    # 9 面板全部从 prompts 字典读取，fallback 为兜底模板（确保任何情况下都有内容）
    lines = [
        "Create a 3x3 commercial textile collection board, nine coordinated fabric panels arranged in a clean equal grid with thin white gutters between all panels, all inside one square image. Absolutely no text, no labels, no captions, no titles, no words, no letters, no typography, no descriptions anywhere in the image.",
        "",
        f"Overall art direction: {style.get('overall_impression', 'Elegant commercial textile collection')}. {style.get('mood', 'Quiet and wearable')}. {style.get('medium', 'Watercolor')}. Low contrast, highly wearable, refined hand-painted brush language, graceful breathing space, not busy, cohesive as one fashion print suite.",
        "",
        "Row 1 — Base textures for large garment panels (seamless tileable):",
        f"Top-left: {prompts.get('main', 'pale base with faint pattern, very low noise, lots of negative space, no text')}",
        f"Top-center: {prompts.get('secondary', 'coordinated medium-density pattern on light ground, same palette, no text')}",
        f"Top-right: {prompts.get('dark_base', 'deep dark ground with very subtle texture, quiet and minimal, no text')}",
        "",
        "Row 2 — Mid-scale accent textures (seamless tileable):",
        f"Middle-left: {prompts.get('accent_light', 'tiny scattered small-scale pattern on light ground, charming but controlled, no text')}",
        f"Middle-center: {prompts.get('accent_mid', 'soft geometric or organic lattice on pale ground, same palette, seamless tileable texture for secondary panels, no text')}",
        f"Middle-right: {prompts.get('solid_quiet', 'quiet warm solid with only subtle paper grain, no pattern, calm and minimal, seamless tileable solid texture for quiet trim or lining, no text')}",
        "",
        "Row 3 — Placement motifs and hero elements (plain backgrounds for background removal):",
        f"Bottom-left: {prompts.get('hero_motif_1', 'a single elegant main subject centered in a delicate decorative frame, plain light background, soft fading edges, balanced negative space, designed as a placement print element, no text')}",
        f"Bottom-center: {prompts.get('hero_motif_2', 'a secondary accent subject, centered, plain light background, refined brushwork, designed as a placement accent motif, no text')}",
        f"Bottom-right: {prompts.get('trim_motif', 'a small delicate decorative accent, minimal composition, plain warm background, designed as a trim detail placement element, no text')}",
        "",
        "All nine panels must look like one coordinated textile collection by the same fashion print designer, identical palette, identical paper texture, identical hand-painted brush style, identical commercial apparel mood.",
        "",
        "No animals other than approved subjects, no characters, no faces, no people, no text, no logo, no watermark, no house, no river, no full landscape scene, no poster composition, no sticker sheet, no harsh black outlines, no dense confetti, no neon colors, no muddy dark colors, no gradient backgrounds inside individual panels.",
        "",
        "Row 1 and Row 2 panels should be seamless tileable textile swatches usable as fabric repeats. Row 3 panels should be clean placement motifs with plain light backgrounds suitable for background removal.",
    ]
    return "\n".join(lines)


def validate_board_colors(board_path: Path, palette: dict, threshold: int = 80) -> list[dict]:
    """验证 3×3 看板各面板颜色是否与 palette 协调。
    返回颜色偏差报告列表。"""
    from PIL import Image
    warnings = []
    if not palette:
        return warnings

    board = Image.open(board_path).convert("RGB")
    w, h = board.size
    div_x1, div_x2 = w // 3, 2 * w // 3
    div_y1, div_y2 = h // 3, 2 * h // 3

    panels = {
        "main": (0, 0, div_x1, div_y1),
        "secondary": (div_x1, 0, div_x2, div_y1),
        "dark_base": (div_x2, 0, w, div_y1),
        "accent_light": (0, div_y1, div_x1, div_y2),
        "accent_mid": (div_x1, div_y1, div_x2, div_y2),
        "solid_quiet": (div_x2, div_y1, w, div_y2),
        "hero_motif_1": (0, div_y2, div_x1, h),
        "hero_motif_2": (div_x1, div_y2, div_x2, h),
        "trim_motif": (div_x2, div_y2, w, h),
    }

    def _hex_to_rgb(hex_str):
        from PIL import ImageColor
        return ImageColor.getrgb(hex_str)

    def _rgb_dist(c1, c2):
        return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5

    mapping = {
        "main": palette.get("primary", []),
        "secondary": palette.get("secondary", []),
        "dark_base": palette.get("dark", []),
        "accent_light": palette.get("accent", []) or palette.get("primary", []),
        "accent_mid": palette.get("secondary", []),
        "solid_quiet": palette.get("primary", []),
    }

    for tid, box in panels.items():
        crop = board.crop(box)
        sample = crop.resize((1, 1), Image.Resampling.LANCZOS)
        r, g, b = sample.getpixel((0, 0))
        actual = (r, g, b)

        candidates = mapping.get(tid, [])
        if not candidates:
            continue
        try:
            expected_rgb = _hex_to_rgb(candidates[0])
            dist = _rgb_dist(actual, expected_rgb)
            if dist > threshold:
                warnings.append({
                    "panel": tid,
                    "actual_rgb": actual,
                    "expected_hex": candidates[0],
                    "distance": round(dist, 1),
                    "severity": "high" if dist > 120 else "medium",
                })
        except Exception:
            continue

    return warnings


def generate_board(args: argparse.Namespace, out_dir: Path) -> Path:
    """调用 Neo AI 生成面料看板。"""
    board_dir = out_dir / "neo_collection_board"
    board_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(NEO_AI_SCRIPT),
        "--model",
        args.neo_model,
        "--size",
        args.neo_size,
        "--output-format",
        "png",
        "--output-dir",
        str(board_dir),
    ]
    if args.prompt_file:
        cmd.extend(["--prompt-file", args.prompt_file])
    if args.negative_prompt_file:
        cmd.extend(["--negative-prompt-file", args.negative_prompt_file])
    if args.seed is not None:
        cmd.extend(["--seed", str(args.seed)])
    if args.num_images:
        cmd.extend(["--num-images", args.num_images])
    if args.token:
        cmd.extend(["--token", args.token])
    env = os.environ.copy()
    run_step(cmd, env=env)
    return latest_collection_board(board_dir)


def mirror_tile(image: Image.Image) -> Image.Image:
    """镜像修复：将纹理裁剪修复为无缝图块。"""
    src = image.convert("RGBA")
    out = Image.new("RGBA", (src.width * 2, src.height * 2), (0, 0, 0, 0))
    out.alpha_composite(src, (0, 0))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (src.width, 0))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, src.height))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_LEFT_RIGHT).transpose(Image.Transpose.FLIP_TOP_BOTTOM), (src.width, src.height))
    return out


def detect_grid_gaps(board: Image.Image, div_x1: int, div_x2: int, div_y1: int, div_y2: int, strip_width: int = 40) -> int:
    """检测 3×3 网格的两条水平分隔带和两条垂直分隔带，返回统一的安全边距。"""
    gray = board.convert("L")
    width, height = gray.size
    gap_insets = []

    # 两条水平分隔带检测
    for mid_y in (div_y1, div_y2):
        y0 = max(0, mid_y - strip_width)
        y1 = min(height, mid_y + strip_width)
        h_strip = gray.crop((0, y0, width, y1))
        h_pixels = list(h_strip.get_flattened_data())
        strip_w = h_strip.width
        row_diffs = []
        for y in range(h_strip.height):
            row = [h_pixels[y * strip_w + x] for x in range(strip_w)]
            diffs = [abs(row[i] - row[i - 1]) for i in range(1, len(row))]
            row_diffs.append(sum(diffs) / max(1, len(diffs)))
        if sum(1 for d in row_diffs if d > 18) > h_strip.height * 0.25:
            gap_insets.append(20)

    # 两条垂直分隔带检测
    for mid_x in (div_x1, div_x2):
        x0 = max(0, mid_x - strip_width)
        x1 = min(width, mid_x + strip_width)
        v_strip = gray.crop((x0, 0, x1, height))
        v_pixels = list(v_strip.get_flattened_data())
        strip_h = v_strip.height
        col_diffs = []
        for x in range(v_strip.width):
            col = [v_pixels[y * v_strip.width + x] for y in range(strip_h)]
            diffs = [abs(col[i] - col[i - 1]) for i in range(1, len(col))]
            col_diffs.append(sum(diffs) / max(1, len(diffs)))
        if sum(1 for d in col_diffs if d > 18) > v_strip.width * 0.25:
            gap_insets.append(20)

    return max(gap_insets) if gap_insets else 0


def clean_motif_bottom(panel: Image.Image, text_threshold: float = 0.08) -> Image.Image:
    """检测并裁剪 motif 底部可能的文字条带。"""
    gray = panel.convert("L")
    width, height = gray.size
    bottom_h = max(30, height // 4)
    bottom_region = gray.crop((0, height - bottom_h, width, height))
    pixels = list(bottom_region.get_flattened_data())
    row_diffs = []
    for y in range(bottom_h):
        row = [pixels[y * width + x] for x in range(width)]
        diffs = [abs(row[i] - row[i - 1]) for i in range(1, len(row))]
        row_diffs.append(sum(diffs) / max(1, len(diffs)))
    # 寻找底部区域内的高差异连续行
    high_diff_rows = 0
    in_text = False
    for y, d in enumerate(row_diffs):
        if d > 12:
            if not in_text:
                in_text = True
        elif d <= 6:
            if in_text:
                in_text = False
    # 统计高差异行数
    high_diff_rows = sum(1 for d in row_diffs if d > 12)
    if high_diff_rows > bottom_h * text_threshold:
        # 找到文字条带的起始位置
        text_start = 0
        for y, d in enumerate(row_diffs):
            if d > 12:
                text_start = y
                break
        # 裁剪掉底部文字区域
        crop_h = max(1, height - bottom_h + text_start)
        print(f"[motif清理] 裁剪掉底部文字区域，高度从 {height} 减至 {crop_h}")
        return panel.crop((0, 0, width, crop_h))
    return panel


def _estimate_bg_color(img: Image.Image) -> tuple[tuple[int, int, int], int, int]:
    """采样四角 + 边缘估计背景色。返回 (mean_rgb, brightness_threshold, chroma_threshold)。"""
    w, h = img.size
    # 采样四角 8x8 区域
    corners = []
    for cx, cy in [(0, 0), (w - 8, 0), (0, h - 8), (w - 8, h - 8)]:
        if cx < 0 or cy < 0:
            continue
        crop = img.crop((cx, cy, min(cx + 8, w), min(cy + 8, h)))
        for px in crop.getdata():
            if len(px) >= 3:
                corners.append(px[:3])
    if not corners:
        return ((255, 255, 255), 700, 50)

    # 计算均值和标准差
    n = len(corners)
    mean_r = sum(c[0] for c in corners) // n
    mean_g = sum(c[1] for c in corners) // n
    mean_b = sum(c[2] for c in corners) // n
    var = sum((c[0] - mean_r) ** 2 + (c[1] - mean_g) ** 2 + (c[2] - mean_b) ** 2 for c in corners) / n
    std = int(var ** 0.5)

    # 亮度阈值：根据背景亮度自适应（暗背景时用更低阈值）
    brightness = mean_r + mean_g + mean_b
    bright_threshold = max(480, brightness - max(30, std * 2))

    # 色度阈值：背景越不均匀，阈值越宽松
    chroma_threshold = min(80, 35 + std)

    return ((mean_r, mean_g, mean_b), bright_threshold, chroma_threshold)


def make_motif_transparent(panel: Image.Image, threshold: int = 235) -> Image.Image:
    """自适应透明背景去除：根据四角采样自动估计背景色范围。
    兼容暖白/冷白/微蓝/浅灰背景，深色调主题也能正确处理。"""
    # 先裁剪底部可能的文字条带
    img = clean_motif_bottom(panel)
    img = img.convert("RGBA")
    pixels = img.load()
    width, height = img.size

    # 自适应估计背景色
    bg_mean, bright_thresh, chroma_thresh = _estimate_bg_color(img)

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            total = r + g + b
            bright = total >= bright_thresh
            low_chroma = max(r, g, b) - min(r, g, b) < chroma_thresh
            if bright and low_chroma:
                pixels[x, y] = (r, g, b, 0)
            elif bright:
                pixels[x, y] = (r, g, b, max(0, min(a, 140)))

    # 二次清理：检测并去除孤立的高对比度文字像素
    gray = img.convert("L").filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(gray.get_flattened_data())
    alpha = img.getchannel("A")
    alpha_pixels = list(alpha.get_flattened_data())
    for idx, edge_val in enumerate(edge_pixels):
        if edge_val > 120 and alpha_pixels[idx] > 0:
            y_pos, x_pos = divmod(idx, width)
            r, g, b, a = pixels[x_pos, y_pos]
            # 自适应：如果像素接近背景色则透明
            bg_dist = abs(r - bg_mean[0]) + abs(g - bg_mean[1]) + abs(b - bg_mean[2])
            if bg_dist < chroma_thresh * 2:
                pixels[x_pos, y_pos] = (r, g, b, 0)

    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        img = img.crop(bbox)
    return img


def quiet_solid_from_image(image: Image.Image, palette: dict = None, target_role: str = "trim") -> str:
    """从图像提取纯色，使用 MedianCut 取主色（避免单像素平均的脏灰问题），
    优先遵循 palette，避免硬编码颜色偏差。

    Args:
        image: 面板图像。
        palette: 从主题图提取的 palette dict，含 primary/secondary/accent/dark 列表。
        target_role: 目标用途，决定从 palette 的哪个 tier 选色。
    """
    from PIL import ImageColor
    from collections import Counter

    # MedianCut 量化提取主色（避免花哨纹理平均成脏灰）
    sample = image.convert("RGB").resize((160, 160), Image.Resampling.LANCZOS)
    quantized = sample.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette_raw = quantized.getpalette() or []
    if hasattr(quantized, "get_flattened_data"):
        used = Counter(quantized.get_flattened_data())
    else:
        used = Counter(quantized.getdata())

    dominant_colors = []
    for index, _ in used.most_common(4):
        offset = index * 3
        if offset + 2 >= len(palette_raw):
            continue
        rgb = tuple(palette_raw[offset:offset + 3])
        # 跳过接近纯黑/纯白的极端值
        brightness = sum(rgb) / 3
        if brightness < 20 or brightness > 250:
            continue
        dominant_colors.append(rgb)

    if not dominant_colors:
        # fallback：单像素平均
        sample = image.convert("RGB").resize((1, 1), Image.Resampling.LANCZOS)
        dominant_colors = [sample.getpixel((0, 0))]

    if not palette:
        r, g, b = dominant_colors[0]
        return "#{:02x}{:02x}{:02x}".format(r, g, b)

    # 根据 target_role 从 palette 选最合适的颜色 tier
    if target_role in ("trim", "dark", "dark_base"):
        candidates = palette.get("dark", []) + palette.get("accent", [])
    elif target_role in ("secondary", "accent"):
        candidates = palette.get("secondary", []) + palette.get("accent", [])
    else:
        candidates = palette.get("primary", []) + palette.get("secondary", [])

    if candidates:
        def _color_distance(c1, c2):
            try:
                rgb1 = ImageColor.getrgb(c1)
                rgb2 = ImageColor.getrgb(c2)
                return sum((a - b) ** 2 for a, b in zip(rgb1, rgb2))
            except Exception:
                return float("inf")

        # 从 dominant_colors 中选与 palette 最接近的一个
        best_color = None
        best_dist = float("inf")
        for dom_rgb in dominant_colors:
            dom_hex = "#{:02x}{:02x}{:02x}".format(*dom_rgb)
            dist = min(_color_distance(dom_hex, c) for c in candidates)
            if dist < best_dist:
                best_dist = dist
                best_color = dom_hex

        if best_color:
            return best_color

    r, g, b = dominant_colors[0]
    return "#{:02x}{:02x}{:02x}".format(r, g, b)


def clean_internal_text_strip(image: Image.Image, min_strip_height: int = 5, diff_threshold: float = 12.0) -> Image.Image:
    """检测并去除图像内部任意位置的水平文字条带（高对比度水平区域）。
    适用于 3×3 看板裁剪后每个面板内部可能含有的文字标签。
    """
    gray = image.convert("L")
    width, height = gray.size
    pixels = list(gray.get_flattened_data())

    row_diffs = []
    for y in range(height):
        row = [pixels[y * width + x] for x in range(width)]
        diffs = [abs(row[i] - row[i - 1]) for i in range(1, len(row))]
        row_diffs.append(sum(diffs) / max(1, len(diffs)))

    # 找连续的高差异行（文字特征）
    text_regions = []
    in_text = False
    start = 0
    for y, diff in enumerate(row_diffs):
        if diff > diff_threshold and not in_text:
            in_text = True
            start = y
        elif diff <= diff_threshold * 0.35 and in_text:
            in_text = False
            if y - start >= min_strip_height:
                text_regions.append((start, y))
    if in_text and height - start >= min_strip_height:
        text_regions.append((start, height))

    if not text_regions:
        return image

    # 评估每个区域是否最可能是文字标签
    best_region = None
    best_score = 0
    for start_y, end_y in text_regions:
        region_h = end_y - start_y
        region_pixels = [pixels[y * width + x] for y in range(start_y, end_y) for x in range(width)]
        mean_brightness = sum(region_pixels) / len(region_pixels)
        avg_diff = sum(row_diffs[start_y:end_y]) / max(1, region_h)
        # 文字区域通常是白底黑字，亮度较高（>180）且差异大，高度适中
        score = avg_diff * (mean_brightness / 255.0) * (1.0 if 6 <= region_h <= 140 else 0.2)
        if score > best_score:
            best_score = score
            best_region = (start_y, end_y)

    if not best_region or best_score < 80:
        return image

    start_y, end_y = best_region
    print(f"[文字清理] 裁剪掉文字条带 y={start_y}-{end_y}（高度{end_y - start_y}，分数{best_score:.1f}）")

    # 合并上下部分（条带可能在图像中间）
    top = image.crop((0, 0, width, start_y + 1))
    bottom = image.crop((0, end_y - 1, width, height))

    if top.height > 10 and bottom.height > 10:
        merged = Image.new(image.mode, (width, top.height + bottom.height))
        merged.paste(top, (0, 0))
        merged.paste(bottom, (0, top.height))
        return merged
    elif top.height > 10:
        return top
    elif bottom.height > 10:
        return bottom
    return image


def crop_collection_board(board_path: Path, out_dir: Path, inset: int, repair_tiles: bool, palette: dict = None) -> Path:
    """将 3×3 面料看板裁剪为九种资产，并生成面料组合.json。
    支持智能分隔带检测，自动扩大安全边距，并清理面板内部文字。"""
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    board = Image.open(board_path).convert("RGBA")
    width, height = board.size
    div_x1, div_x2 = width // 3, 2 * width // 3
    div_y1, div_y2 = height // 3, 2 * height // 3

    # 智能检测网格分隔带文字，动态调整边距
    extra_gap = detect_grid_gaps(board, div_x1, div_x2, div_y1, div_y2)
    effective_inset = inset + extra_gap
    # 确保不越界
    max_inset = min(div_x1, width - div_x2, div_y1, height - div_y2) - 64
    effective_inset = min(effective_inset, max_inset)
    if effective_inset > inset:
        print(f"[智能裁剪] 检测到分隔带文字，边距从 {inset} 扩大到 {effective_inset}")

    boxes = {
        # Row 1: Base textures
        "main": (effective_inset, effective_inset, div_x1 - effective_inset, div_y1 - effective_inset),
        "secondary": (div_x1 + effective_inset, effective_inset, div_x2 - effective_inset, div_y1 - effective_inset),
        "dark_base": (div_x2 + effective_inset, effective_inset, width - effective_inset, div_y1 - effective_inset),
        # Row 2: Mid-scale accents
        "accent_light": (effective_inset, div_y1 + effective_inset, div_x1 - effective_inset, div_y2 - effective_inset),
        "accent_mid": (div_x1 + effective_inset, div_y1 + effective_inset, div_x2 - effective_inset, div_y2 - effective_inset),
        "solid_quiet": (div_x2 + effective_inset, div_y1 + effective_inset, width - effective_inset, div_y2 - effective_inset),
        # Row 3: Placement motifs
        "hero_motif_1": (effective_inset, div_y2 + effective_inset, div_x1 - effective_inset, height - effective_inset),
        "hero_motif_2": (div_x1 + effective_inset, div_y2 + effective_inset, div_x2 - effective_inset, height - effective_inset),
        "trim_motif": (div_x2 + effective_inset, div_y2 + effective_inset, width - effective_inset, height - effective_inset),
    }

    paths = {}
    for asset_id, box in boxes.items():
        crop = board.crop(box)
        # Row 3: motifs → transparent RGBA
        if asset_id in ("hero_motif_1", "hero_motif_2", "trim_motif"):
            crop = make_motif_transparent(crop)
            path = assets_dir / f"{asset_id}.png"
            crop.save(path)
        else:
            # Row 1 & 2: textures → clean + tile repair + RGB
            crop = clean_internal_text_strip(crop)
            if repair_tiles:
                crop = mirror_tile(crop)
            path = assets_dir / f"{asset_id}.png"
            crop.convert("RGB").save(path)
        paths[asset_id] = path

    quiet_solid = quiet_solid_from_image(Image.open(paths["solid_quiet"]), palette=palette, target_role="trim")
    moss_color = quiet_solid_from_image(Image.open(paths["secondary"]), palette=palette, target_role="secondary")

    # warm_ivory 从 palette primary 中最亮颜色派生，不再硬编码
    warm_ivory = "#f3f1df"
    if palette and palette.get("primary"):
        from PIL import ImageColor
        primary_colors = palette["primary"]
        if primary_colors:
            # 选最亮的 primary 颜色
            def _brightness(hex_str):
                try:
                    r, g, b = ImageColor.getrgb(hex_str)
                    return r + g + b
                except Exception:
                    return 0
            brightest = max(primary_colors, key=_brightness)
            warm_ivory = brightest

    texture_set = {
        "texture_set_id": f"{out_dir.name}_neo_collection_texture_set",
        "locked": False,
        "source_collection_board": str(board_path.resolve()),
        "textures": [
            {
                "texture_id": "main",
                "path": str(paths["main"].resolve()),
                "role": "main",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：主底纹",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "texture_id": "secondary",
                "path": str(paths["secondary"].resolve()),
                "role": "secondary",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：辅纹理",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "texture_id": "dark_base",
                "path": str(paths["dark_base"].resolve()),
                "role": "dark_base",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：深色底纹",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "texture_id": "accent_light",
                "path": str(paths["accent_light"].resolve()),
                "role": "accent_light",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：浅色点缀纹理",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "texture_id": "accent_mid",
                "path": str(paths["accent_mid"].resolve()),
                "role": "accent_mid",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：中调点缀纹理",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "texture_id": "solid_quiet",
                "path": str(paths["solid_quiet"].resolve()),
                "role": "solid_quiet",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：安静纯色面板",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
        ],
        "motifs": [
            {
                "motif_id": "hero_motif_1",
                "texture_id": "hero_motif_1",
                "path": str(paths["hero_motif_1"].resolve()),
                "role": "hero",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：卖点定位图案 1",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "motif_id": "hero_motif_2",
                "texture_id": "hero_motif_2",
                "path": str(paths["hero_motif_2"].resolve()),
                "role": "hero",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：卖点定位图案 2",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "motif_id": "trim_motif",
                "texture_id": "trim_motif",
                "path": str(paths["trim_motif"].resolve()),
                "role": "trim",
                "approved": True,
                "prompt": "从 Neo AI 3×3 面料看板裁剪：饰边定位图案",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
        ],
        "solids": [
            {"solid_id": "quiet_solid", "color": quiet_solid, "approved": True},
            {"solid_id": "quiet_moss", "color": moss_color, "approved": True},
            {"solid_id": "warm_ivory", "color": warm_ivory, "approved": True},
        ],
    }
    return write_json(out_dir / "texture_set.json", texture_set)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Neo AI 面料看板并自动渲染服装裁片。")
    parser.add_argument("--pattern", required=True, help="透明纸样 mask PNG/WebP")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--collection-board", default="", help="已有的 Neo AI 3×3 面料看板。若省略，则调用 Neo AI 生成。")
    parser.add_argument("--theme-image", default="", help="主题/参考图像路径。若提供，会先进行视觉元素提取。")
    parser.add_argument("--visual-elements", default="", help="已完成的 visual_elements.json 路径。若提供，跳过视觉提取直接生成设计简报。")
    parser.add_argument("--prompt-file", default="", help="Neo AI 看板生成的提示词文件")
    parser.add_argument("--negative-prompt-file", default="", help="Neo AI 看板生成的反向提示词文件")
    parser.add_argument("--token", default="", help="Neodomain 访问令牌。优先使用 NEODOMAIN_ACCESS_TOKEN 环境变量。")
    parser.add_argument("--neo-model", default="gemini-3-pro-image-preview")
    parser.add_argument("--neo-size", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument("--num-images", default="1", choices=["1", "4"])
    parser.add_argument("--seed", type=int)
    parser.add_argument("--crop-inset", type=int, default=60, help="从每个象限裁剪的像素数，用于去除网格间隙和文字标签。默认 60。")
    parser.add_argument("--no-tile-repair", action="store_true", help="不将纹理裁剪镜像修复为无缝图块。")
    parser.add_argument("--brief", default="", help="可选的商业设计简报 JSON 路径")
    parser.add_argument("--ai-plan", default="", help="子 Agent 生成的 AI 填充计划 JSON 路径。若提供，优先使用 AI 审美决策。")
    parser.add_argument("--construct-ai-request", action="store_true", help="在部位映射后构造子 Agent 审美请求并退出，等待外部子 Agent 生成 ai_piece_fill_plan.json。")
    parser.add_argument("--selected-collection", default="", help="子Agent已选择的 selected_variants.json 路径。若提供，直接生成最终看板 prompt 并跳过选择请求构造。")
    parser.add_argument("--auto-retry", type=int, default=0, help="自动重试次数（0=不重试）。时尚QC发现issues时，自动构造返工请求并重新渲染。")
    parser.add_argument("--ai-map", default="", help="AI子Agent输出的 ai_garment_map.json 路径。若提供，部位映射优先使用AI识别结果。")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ===== 视觉元素提取阶段 =====
    if args.theme_image and not args.visual_elements:
        theme_path = Path(args.theme_image)
        if not theme_path.exists():
            raise RuntimeError(f"主题图不存在: {theme_path}")
        ve_out = out_dir / "visual_elements.json"
        if ve_out.exists():
            print(f"[视觉提取] 已存在视觉元素分析: {ve_out}，直接使用。")
            args.visual_elements = str(ve_out)
        else:
            # 构造子Agent视觉分析请求
            ve_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "视觉元素提取.py"),
                "--theme-image", str(theme_path),
                "--out", str(out_dir),
            ]
            run_step(ve_cmd)
            print("\n[提示] 子 Agent 视觉分析请求已构造。请启动子 Agent 阅读以下文件并输出 visual_elements.json：")
            print(f"  主题图: {theme_path}")
            print(f"  提示词文件: {out_dir / 'ai_vision_prompt.txt'}")
            print(f"  预期输出: {ve_out}")
            print("  完成后重新运行本脚本并传入 --visual-elements 参数。\n")
            return 0

    if args.visual_elements:
        ve_path = Path(args.visual_elements)
        if not ve_path.exists():
            raise RuntimeError(f"visual_elements 不存在: {ve_path}")
        # 基于视觉元素分析生成设计简报与纹理提示词
        brief_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "生成设计简报.py"),
            "--visual-elements", str(ve_path),
            "--out", str(out_dir),
        ]
        run_step(brief_cmd)
        # 构造看板候选选择请求（9 面板 × 3 候选 → 子Agent选择最优组合）
        if not args.selected_collection:
            # 模式A：生成选择任务，等待子Agent
            selection_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "构造看板选择请求.py"),
                "--candidates", str(out_dir / "collection_prompt_candidates.json"),
                "--brief", str(out_dir / "commercial_design_brief.json"),
                "--style-profile", str(out_dir / "style_profile.json"),
                "--out", str(out_dir),
            ]
            run_step(selection_cmd)
            print("\n[提示] 3×3 看板候选选择请求已构造。请启动子Agent完成选择：")
            print(f"  提示词文件: {out_dir / 'ai_collection_selection_prompt.txt'}")
            print(f"  预期输出: {out_dir / 'selected_variants.json'}")
            print("  完成后重新运行本脚本并传入 --selected-collection 参数。\n")
            return 0
        else:
            # 模式B：子Agent已选择，生成最终看板 prompt
            selected_path = Path(args.selected_collection)
            if not selected_path.is_absolute():
                selected_path = out_dir / selected_path
            if selected_path.exists():
                selection_cmd = [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "构造看板选择请求.py"),
                    "--candidates", str(out_dir / "collection_prompt_candidates.json"),
                    "--brief", str(out_dir / "commercial_design_brief.json"),
                    "--style-profile", str(out_dir / "style_profile.json"),
                    "--out", str(out_dir),
                    "--selected", str(selected_path),
                ]
                run_step(selection_cmd)
                final_prompt_path = out_dir / "selected_collection_prompt.txt"
                if final_prompt_path.exists():
                    args.prompt_file = str(final_prompt_path)
                    print(f"[视觉提取] 已基于子Agent选择生成最终看板提示词: {final_prompt_path}")
            else:
                print(f"[警告] 选择结果不存在: {selected_path}，回退到直接构造 prompt")

        # 如果用户未显式提供 prompt-file 且未走选择流程，尝试直接构造综合 prompt
        if not args.prompt_file:
            ve_path = Path(args.visual_elements) if args.visual_elements else None
            generated_prompt = _build_collection_prompt_from_visual_elements(out_dir, ve_path)
            if generated_prompt:
                prompt_path = out_dir / "generated_collection_prompt.txt"
                prompt_path.write_text(generated_prompt, encoding="utf-8")
                args.prompt_file = str(prompt_path)
                print(f"[视觉提取] 已基于子Agent分析自动生成看板提示词: {prompt_path}")

    board_path = Path(args.collection_board).resolve() if args.collection_board else generate_board(args, out_dir).resolve()
    if not board_path.exists():
        raise RuntimeError(f"面料看板未找到: {board_path}")
    print(f"使用面料看板: {board_path}")

    # 尝试读取 palette 以指导纯色提取和颜色校验
    palette = None
    style_profile_path = out_dir / "style_profile.json"
    if style_profile_path.exists():
        try:
            sp = json.loads(style_profile_path.read_text(encoding="utf-8"))
            palette = sp.get("palette")
        except Exception:
            pass

    # 看板颜色协调性校验
    if palette:
        color_warnings = validate_board_colors(board_path, palette)
        if color_warnings:
            print("[颜色校验警告] 以下面板颜色与 palette 偏差较大：")
            for w in color_warnings:
                print(f"  {w['panel']}: 实际 RGB{w['actual_rgb']} vs 预期 {w['expected_hex']} (偏差 {w['distance']})")
            warn_path = out_dir / "board_color_warnings.json"
            warn_path.write_text(json.dumps(color_warnings, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  详细报告: {warn_path}")
        else:
            print("[颜色校验] 所有面板颜色与 palette 协调。")

    texture_set_path = crop_collection_board(board_path, out_dir, args.crop_inset, not args.no_tile_repair, palette=palette)

    pieces_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "提取裁片.py"),
        "--pattern",
        args.pattern,
        "--out",
        str(out_dir),
    ]
    run_step(pieces_cmd)

    pieces_path = out_dir / "pieces.json"
    garment_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "部位映射.py"),
        "--pieces",
        str(pieces_path),
        "--out",
        str(out_dir),
    ]
    if args.ai_map:
        garment_cmd.extend(["--ai-map", args.ai_map])
    run_step(garment_cmd)

    qc_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "质检纹理.py"),
        "--texture-set",
        str(texture_set_path),
        "--out",
        str(out_dir / "texture_qc_report.json"),
    ]
    run_step(qc_cmd)

    # 构造子 Agent 审美请求（如果启用）
    if args.construct_ai_request:
        request_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "构造审美请求.py"),
            "--pieces", str(pieces_path),
            "--garment-map", str(out_dir / "garment_map.json"),
            "--texture-set", str(texture_set_path),
            "--out", str(out_dir),
        ]
        if args.brief:
            request_cmd.extend(["--brief", args.brief])
        run_step(request_cmd)
        print("\n[提示] 子 Agent 审美请求已构造。请启动子 Agent 阅读以下文件并输出 ai_piece_fill_plan.json：")
        print(f"  提示词文件: {out_dir / 'ai_fill_plan_prompt.txt'}")
        print(f"  预期输出: {out_dir / 'ai_piece_fill_plan.json'}")
        print("  完成后重新运行本脚本并传入 --ai-plan 参数。\n")
        return 0

    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces",
        str(pieces_path),
        "--texture-set",
        str(texture_set_path),
        "--garment-map",
        str(out_dir / "garment_map.json"),
        "--out",
        str(out_dir),
    ]
    if args.brief:
        plan_cmd.extend(["--brief", args.brief])
    if args.ai_plan:
        ai_plan_path = Path(args.ai_plan)
        if not ai_plan_path.is_absolute():
            ai_plan_path = out_dir / ai_plan_path
        if ai_plan_path.exists():
            plan_cmd.extend(["--ai-plan", str(ai_plan_path)])
        else:
            print(f"[警告] AI 计划不存在: {ai_plan_path}，将使用后端规则生成。")
    run_step(plan_cmd)

    rendered_dir = out_dir / "rendered"
    render_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "渲染裁片.py"),
        "--pieces",
        str(pieces_path),
        "--texture-set",
        str(texture_set_path),
        "--fill-plan",
        str(out_dir / "piece_fill_plan.json"),
        "--out",
        str(rendered_dir),
    ]
    run_step(render_cmd)

    fashion_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "时尚质检.py"),
        "--pieces",
        str(pieces_path),
        "--texture-set",
        str(texture_set_path),
        "--fill-plan",
        str(out_dir / "piece_fill_plan.json"),
        "--rendered",
        str(rendered_dir),
        "--out",
        str(out_dir / "fashion_qc_report.json"),
    ]
    run_step(fashion_cmd)

    # 质检反馈闭环：自动重试模式
    if args.auto_retry > 0:
        qc_report_path = out_dir / "fashion_qc_report.json"
        retry_count = 0
        while retry_count < args.auto_retry and qc_report_path.exists():
            qc = json.loads(qc_report_path.read_text(encoding="utf-8"))
            if qc.get("approved", False):
                print(f"[自动重试] 第 {retry_count} 轮质检通过")
                break
            issues = qc.get("issues", [])
            if not issues:
                break
            retry_count += 1
            print(f"\n[自动重试] 第 {retry_count}/{args.auto_retry} 轮：发现 {len(issues)} 个问题")
            # 使用返工提示词让子Agent修订
            revised_plan_path = out_dir / f"ai_piece_fill_plan_rev{retry_count}.json"
            if revised_plan_path.exists():
                print(f"[自动重试] 使用修订计划: {revised_plan_path}")
                # 重新运行创建填充计划（使用修订后的ai-plan）
                plan_cmd = [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "创建填充计划.py"),
                    "--pieces", str(pieces_path),
                    "--texture-set", str(texture_set_path),
                    "--garment-map", str(out_dir / "garment_map.json"),
                    "--out", str(out_dir),
                    "--ai-plan", str(revised_plan_path),
                ]
                if args.brief:
                    plan_cmd.extend(["--brief", args.brief])
                run_step(plan_cmd)
                # 重新渲染
                run_step(render_cmd)
                # 重新QC
                run_step(fashion_cmd)
            else:
                print(f"[自动重试] 等待子Agent生成修订计划: {revised_plan_path}")
                print("  请启动子Agent，传入 rework_prompt.txt，输出 ai_piece_fill_plan_rev1.json")
                break

    summary = {
        "面料看板": str(board_path),
        "面料组合": str(texture_set_path.resolve()),
        "裁片清单": str(pieces_path.resolve()),
        "部位映射": str((out_dir / "garment_map.json").resolve()),
        "裁片填充计划": str((out_dir / "piece_fill_plan.json").resolve()),
        "渲染目录": str(rendered_dir.resolve()),
        "预览图": str((rendered_dir / "preview.png").resolve()),
        "白底预览图": str((rendered_dir / "preview_white.jpg").resolve()),
        "成品质检报告": str((out_dir / "fashion_qc_report.json").resolve()),
    }
    write_json(out_dir / "automation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
