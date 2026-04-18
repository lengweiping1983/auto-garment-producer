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


def make_motif_transparent(panel: Image.Image, threshold: int = 235) -> Image.Image:
    """增强版透明背景去除：亮度+饱和度+边缘密度联合判断。
    threshold 默认为 235，以兼容暖象牙白背景（如 #f3f1df ~ 243,241,223）。"""
    # 先裁剪底部可能的文字条带
    img = clean_motif_bottom(panel)
    img = img.convert("RGBA")
    pixels = img.load()
    width, height = img.size

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            # 使用相对亮度和色度判断，兼容暖白/冷白/微蓝背景
            total = r + g + b
            bright = total >= 705  # 平均 235，覆盖暖象牙白
            low_chroma = max(r, g, b) - min(r, g, b) < 45
            if bright and low_chroma:
                pixels[x, y] = (r, g, b, 0)
            elif bright:
                pixels[x, y] = (r, g, b, max(0, min(a, 140)))

    # 二次清理：检测并去除孤立的高对比度文字像素（小面积高边缘区域）
    gray = img.convert("L").filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(gray.get_flattened_data())
    alpha = img.getchannel("A")
    alpha_pixels = list(alpha.get_flattened_data())
    for idx, edge_val in enumerate(edge_pixels):
        if edge_val > 120 and alpha_pixels[idx] > 0:
            # 高边缘 + 不透明 = 可能是文字笔画
            y, x = divmod(idx, width)
            # 检查周围像素亮度，如果周围是白色背景则设为透明
            r, g, b, a = pixels[x, y]
            if r >= 230 and g >= 230 and b >= 230:
                pixels[x, y] = (r, g, b, 0)

    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        img = img.crop(bbox)
    return img


def quiet_solid_from_image(image: Image.Image, fallback: str = "#78965c") -> str:
    """从图像平均色提取一个安静的商业饰边纯色。"""
    sample = image.convert("RGB").resize((1, 1), Image.Resampling.LANCZOS)
    r, g, b = sample.getpixel((0, 0))
    # 将平均色向商业苔藓饰边色靠拢
    moss = ImageColor.getrgb(fallback)
    mixed = tuple(round(channel * 0.35 + moss_channel * 0.65) for channel, moss_channel in zip((r, g, b), moss))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


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


def crop_collection_board(board_path: Path, out_dir: Path, inset: int, repair_tiles: bool) -> Path:
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

    quiet_solid = quiet_solid_from_image(Image.open(paths["solid_quiet"]))
    moss_color = quiet_solid_from_image(Image.open(paths["secondary"]))

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
            {"solid_id": "warm_ivory", "color": "#f3f1df", "approved": True},
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

    texture_set_path = crop_collection_board(board_path, out_dir, args.crop_inset, not args.no_tile_repair)

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
