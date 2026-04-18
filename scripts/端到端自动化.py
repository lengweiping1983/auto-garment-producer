#!/usr/bin/env python3
"""
端到端自动化：

1. 生成或接收 Neo AI 3×3 面料看板。
2. 裁剪为待审批的设计候选资产。
3. 构建面料组合.json。
4. 从纸样 mask 提取服装裁片。
5. 构建部位映射与艺术指导填充计划。
6. 渲染透明裁片 PNG、预览图、清单和成衣 QC。

Neo AI 负责创作 artwork。本脚本仅准备已批准资产并以确定性方式渲染到裁片中。
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageEnhance, ImageFilter, ImageStat


SKILL_DIR = Path(__file__).resolve().parents[1]
NEO_AI_SCRIPT = Path("/Users/lengweiping/.agents/skills/neo-ai/scripts/generate_texture_collection_board.py")

# 导入模板加载器（用于多尺寸自动渲染）
sys.path.insert(0, str(SKILL_DIR / "scripts"))
try:
    from template_loader import (
        find_template_by_pattern_path,
        load_size_mappings,
        load_size_pieces,
    )
    HAS_TEMPLATE_LOADER = True
except Exception:
    HAS_TEMPLATE_LOADER = False


def file_sha256(path: str | Path) -> str:
    """计算文件的 SHA256 哈希。"""
    p = Path(path)
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def dict_sha256(data: dict) -> str:
    """计算字典的确定性 SHA256 哈希。"""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_dir(out_dir: Path) -> Path:
    """返回缓存目录路径。"""
    return out_dir / ".cache"


def cache_lookup(out_dir: Path, stage: str, input_hash: dict) -> Path | None:
    """按 input_hash 查找缓存。命中时返回缓存文件路径，否则返回 None。"""
    cd = cache_dir(out_dir)
    if not cd.exists():
        return None
    key = dict_sha256(input_hash)
    meta_path = cd / f"{stage}_{key}.meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stored_hash = meta.get("input_hash")
        if stored_hash != input_hash:
            return None
        output_path = cd / meta.get("output_file", "")
        if output_path.exists():
            return output_path
    except Exception:
        pass
    return None


def cache_save(out_dir: Path, stage: str, input_hash: dict, output_path: Path) -> None:
    """将输出文件保存到缓存。"""
    cd = cache_dir(out_dir)
    cd.mkdir(parents=True, exist_ok=True)
    key = dict_sha256(input_hash)
    cached_file = cd / f"{stage}_{key}{output_path.suffix}"
    cached_file.write_bytes(output_path.read_bytes())
    meta = {
        "stage": stage,
        "input_hash": input_hash,
        "output_file": str(cached_file.name),
        "created_at": datetime.datetime.now().isoformat(),
    }
    meta_path = cd / f"{stage}_{key}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_geometry_hints(pieces_path: Path, out_path: Path) -> None:
    """基于裁片几何数据生成 geometry_hints.json，供 AI 决策参考。"""
    try:
        data = json.loads(pieces_path.read_text(encoding="utf-8"))
        pieces = data.get("pieces", [])
        if not pieces:
            return
        largest_area = max(p.get("area", 0) for p in pieces)
        # 计算中心点和对称性候选
        xs = [p.get("x", 0) + p.get("width", 0) / 2 for p in pieces]
        ys = [p.get("y", 0) + p.get("height", 0) / 2 for p in pieces]
        cx = sum(xs) / len(xs) if xs else 0
        cy = sum(ys) / len(ys) if ys else 0
        hints = []
        for p in sorted(pieces, key=lambda x: x.get("area", 0), reverse=True):
            area = p.get("area", 0)
            aspect = p.get("width", 1) / max(1, p.get("height", 1))
            px = p.get("x", 0) + p.get("width", 0) / 2
            py = p.get("y", 0) + p.get("height", 0) / 2
            area_ratio = area / max(1, largest_area)
            # 简单的几何角色推断（仅为 AI 提供候选，不强制）
            geo_role = "unknown"
            if area_ratio > 0.6:
                geo_role = "body_large"
            elif area_ratio > 0.3:
                geo_role = "body_medium"
            elif aspect >= 3 or aspect <= 0.34:
                geo_role = "strip_or_trim"
            elif area_ratio < 0.12:
                geo_role = "small_detail"
            else:
                geo_role = "panel"
            hint = {
                "piece_id": p["piece_id"],
                "area": area,
                "area_ratio": round(area_ratio, 3),
                "width": p.get("width", 0),
                "height": p.get("height", 0),
                "aspect_ratio": round(aspect, 2),
                "centroid": [round(px, 1), round(py, 1)],
                "relative_to_center": [round(px - cx, 1), round(py - cy, 1)],
                "geometry_role_hint": geo_role,
            }
            # 透传裁片方向信息（若提取裁片.py 已生成）
            if "pattern_orientation" in p:
                hint["pattern_orientation"] = p["pattern_orientation"]
                hint["orientation_confidence"] = p.get("orientation_confidence", 0)
                hint["orientation_reason"] = p.get("orientation_reason", "")
            hints.append(hint)
        out_path.write_text(json.dumps({"pieces": hints, "center": [round(cx, 1), round(cy, 1)]}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[几何推断] geometry_hints 已生成: {out_path}")
    except Exception as exc:
        print(f"[警告] geometry_hints 生成失败: {exc}")


def build_production_context(args, out_dir: Path) -> Path:
    """生成 production_context.json，统一索引所有输入和中间产物。"""
    ctx = {
        "input_hash": {},
        "paths": {},
        "computed": {},
        "created_at": datetime.datetime.now().isoformat(),
        "script_version": "2.0.0",
    }
    # 输入文件 hash
    if args.theme_image:
        ctx["input_hash"]["theme_image"] = file_sha256(args.theme_image)
        ctx["paths"]["theme_image"] = str(Path(args.theme_image).resolve())
    if args.pattern:
        ctx["input_hash"]["pattern_image"] = file_sha256(args.pattern)
        ctx["paths"]["pattern_image"] = str(Path(args.pattern).resolve())
    ctx["input_hash"]["garment_type"] = args.garment_type
    ctx["input_hash"]["user_prompt"] = getattr(args, "user_prompt", "")

    # 中间产物路径
    for name, fname in [
        ("pieces_json", "pieces.json"),
        ("piece_overview", "piece_overview.png"),
        ("garment_map", "garment_map.json"),
        ("texture_set", "texture_set.json"),
        ("visual_elements", "visual_elements.json"),
        ("brief", "commercial_design_brief.json"),
        ("geometry_hints", "geometry_hints.json"),
    ]:
        p = out_dir / fname
        if p.exists():
            ctx["paths"][name] = str(p.resolve())

    # 计算字段
    pieces_path = out_dir / "pieces.json"
    if pieces_path.exists():
        try:
            pieces = json.loads(pieces_path.read_text(encoding="utf-8"))
            pc = pieces.get("pieces", [])
            ctx["computed"]["piece_count"] = len(pc)
            if pc:
                ctx["computed"]["largest_piece_area"] = max(p.get("area", 0) for p in pc)
        except Exception:
            pass

    ctx_path = out_dir / "production_context.json"
    ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return ctx_path


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_step(cmd: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("运行:", " ".join(cmd))
    return subprocess.run(cmd, check=check, env=env)


def collect_texture_qc_issues(report: dict) -> list[dict]:
    """收集 texture_qc_report.json 中所有资产级 issues。"""
    issues = []
    for item in report.get("textures", []) + report.get("motifs", []):
        asset_id = item.get("texture_id") or item.get("motif_id") or item.get("role", "")
        for issue in item.get("issues", []):
            issues.append({**issue, "asset_id": asset_id, "asset_role": item.get("role", "")})
    for issue in report.get("solid_issues", []):
        issues.append({**issue, "asset_id": "solids", "asset_role": "solid"})
    return issues


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


def _feather_alpha(img: Image.Image, radius: int | None = None) -> Image.Image:
    """对图像的 alpha 通道进行高斯模糊羽化，消除硬边。"""
    if radius is None:
        w, h = img.size
        radius = max(1, min(3, round(min(w, h) / 200)))
    if radius < 1:
        return img
    alpha = img.getchannel("A")
    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=radius))
    img.putalpha(alpha)
    return img


def make_motif_transparent(panel: Image.Image, threshold: int = 235) -> Image.Image:
    """自适应透明背景去除：根据四角采样自动估计背景色范围。
    兼容暖白/冷白/微蓝/浅灰背景，深色调主题也能正确处理。
    增加边缘距离感知去背景和 alpha 羽化，处理渐变背景和羽化边缘。"""
    # 先裁剪底部可能的文字条带
    img = clean_motif_bottom(panel)
    img = img.convert("RGBA")
    pixels = img.load()
    width, height = img.size

    # 自适应估计背景色
    bg_mean, bright_thresh, chroma_thresh = _estimate_bg_color(img)

    # ---- 阶段 1：基础阈值去背景 ----
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

    # ---- 阶段 2：边缘距离感知清理 ----
    # 检测前景边缘，基于像素到边缘的距离动态调整透明度
    # 距离边缘越近的残留背景像素，越容易被清除
    gray = img.convert("L").filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(gray.get_flattened_data())
    alpha = img.getchannel("A")
    alpha_pixels = list(alpha.get_flattened_data())

    # 构建边缘掩码（edge = 255, non-edge = 0）
    edge_mask = [255 if e > 80 else 0 for e in edge_pixels]
    # 计算每个像素到最近边缘的距离（简化：用两次 pass 近似）
    dist_map = [width + height] * (width * height)
    for idx, is_edge in enumerate(edge_mask):
        if is_edge:
            dist_map[idx] = 0
    # 水平传播
    INF = width + height
    for y in range(height):
        base = y * width
        # 左→右
        best = INF
        for x in range(width):
            idx = base + x
            best = min(best + 1, dist_map[idx])
            dist_map[idx] = best
        # 右→左
        best = INF
        for x in range(width - 1, -1, -1):
            idx = base + x
            best = min(best + 1, dist_map[idx])
            dist_map[idx] = best
    # 垂直传播
    for x in range(width):
        # 上→下
        best = INF
        for y in range(height):
            idx = y * width + x
            best = min(best + 1, dist_map[idx])
            dist_map[idx] = best
        # 下→上
        best = INF
        for y in range(height - 1, -1, -1):
            idx = y * width + x
            best = min(best + 1, dist_map[idx])
            dist_map[idx] = best

    # 根据距离边缘的远近，动态清理残留背景
    for idx, dist in enumerate(dist_map):
        if dist <= 8 and alpha_pixels[idx] > 0:
            y_pos, x_pos = divmod(idx, width)
            r, g, b, a = pixels[x_pos, y_pos]
            # 距离边缘 8px 内的像素：如果接近背景色，则降低 alpha
            bg_dist = abs(r - bg_mean[0]) + abs(g - bg_mean[1]) + abs(b - bg_mean[2])
            if bg_dist < chroma_thresh * 3:
                # 距离边缘越近，alpha 降得越多
                fade = max(0, int(a * (dist / 8)))
                pixels[x_pos, y_pos] = (r, g, b, fade)

    # ---- 阶段 3：形态学收缩（切除渐变晕染残留）----
    # 水彩/手绘 motif 常有渐变过渡带，阈值法无法彻底去除。
    # 用 MinFilter 模拟 erode：让前景向内收缩 3-5px，切除边缘渐变残留。
    alpha = img.getchannel("A")
    # 自适应收缩半径：大图 5px，小图 3px
    erode_radius = max(3, min(5, round(min(width, height) / 80)))
    if erode_radius >= 2:
        # MinFilter 模拟 erode：alpha 区域向内收缩
        alpha = alpha.filter(ImageFilter.MinFilter(size=erode_radius * 2 + 1))
        img.putalpha(alpha)

    # ---- 阶段 4：alpha 边缘羽化 ----
    # 对 alpha 通道进行高斯模糊，消除 erode 后的硬边
    # 这样收缩后的新边缘会柔和自然
    img = _feather_alpha(img)

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


def crop_collection_board(board_path: Path, out_dir: Path, inset: int, repair_tiles: bool, palette: dict = None, suffix: str = "") -> Path:
    """将 3×3 面料看板裁剪为九种资产，并生成面料组合.json。
    支持智能分隔带检测，自动扩大安全边距，并清理面板内部文字。
    suffix 参数用于区分双源输出（如 "_A" / "_B"）。"""
    assets_dir = out_dir / f"assets{suffix}"
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
        print(f"[智能裁剪{suffix}] 检测到分隔带文字，边距从 {inset} 扩大到 {effective_inset}")

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

    source_name = "dual-source" if suffix else "neo-ai"
    texture_set = {
        "texture_set_id": f"{out_dir.name}_{source_name}_collection_texture_set{suffix}",
        "locked": False,
        "source_collection_board": str(board_path.resolve()),
        "textures": [
            {
                "texture_id": "main",
                "path": str(paths["main"].resolve()),
                "role": "main",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：主底纹",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
            {
                "texture_id": "secondary",
                "path": str(paths["secondary"].resolve()),
                "role": "secondary",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：辅纹理",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
            {
                "texture_id": "dark_base",
                "path": str(paths["dark_base"].resolve()),
                "role": "dark_base",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：深色底纹",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
            {
                "texture_id": "accent_light",
                "path": str(paths["accent_light"].resolve()),
                "role": "accent_light",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：浅色点缀纹理",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
            {
                "texture_id": "accent_mid",
                "path": str(paths["accent_mid"].resolve()),
                "role": "accent_mid",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：中调点缀纹理",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
            {
                "texture_id": "solid_quiet",
                "path": str(paths["solid_quiet"].resolve()),
                "role": "solid_quiet",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：安静纯色面板",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
        ],
        "motifs": [
            {
                "motif_id": "hero_motif_1",
                "texture_id": "hero_motif_1",
                "path": str(paths["hero_motif_1"].resolve()),
                "role": "hero",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：卖点定位图案 1",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
            {
                "motif_id": "hero_motif_2",
                "texture_id": "hero_motif_2",
                "path": str(paths["hero_motif_2"].resolve()),
                "role": "hero",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：卖点定位图案 2",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
            {
                "motif_id": "trim_motif",
                "texture_id": "trim_motif",
                "path": str(paths["trim_motif"].resolve()),
                "role": "trim",
                "approved": False,
                "candidate": True,
                "prompt": f"从 {source_name} 3×3 面料看板裁剪：饰边定位图案",
                "model": source_name,
                "seed": "",
                "qc": {"approved": False, "status": "candidate", "notes": "需经 AI 视觉 QC 或人工审批"},
            },
        ],
        "solids": [
            {"solid_id": "quiet_solid", "color": quiet_solid, "approved": False, "candidate": True},
            {"solid_id": "quiet_moss", "color": moss_color, "approved": False, "candidate": True},
            {"solid_id": "warm_ivory", "color": warm_ivory, "approved": False, "candidate": True},
        ],
    }
    return write_json(out_dir / f"texture_set{suffix}.json", texture_set)


def run_garment_mapping(args, pieces_path: Path, out_dir: Path) -> None:
    """执行部位映射。提取为独立函数以便与看板生成并行执行。"""
    garment_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "部位映射.py"),
        "--pieces", str(pieces_path),
        "--out", str(out_dir),
    ]
    if args.ai_map:
        garment_cmd.extend(["--ai-map", args.ai_map])
    if args.template:
        garment_cmd.extend(["--template", args.template])
    if args.template_size and args.template_size != "base":
        garment_cmd.extend(["--template-size", args.template_size])
    if args.template_file:
        garment_cmd.extend(["--template-file", args.template_file])
    if args.garment_type:
        garment_cmd.extend(["--garment-type", args.garment_type])
    if args.no_template:
        garment_cmd.append("--no-template")
    run_step(garment_cmd)


def _run_render_pipeline(args, out_dir: Path, texture_set_path: Path, suffix: str, pieces_path: Path) -> int:
    """基于指定 texture_set 执行剩余流水线：质检 → 生产规划 → 渲染 → 时尚质检 → 商业复审。
    suffix 用于区分双源输出（如 "_A" / "_B" / ""）。
    返回 exit code（0=成功）。
    """
    print(f"\n{'='*60}")
    print(f"[渲染流水线{suffix}] 基于 texture_set: {texture_set_path}")
    print(f"{'='*60}")

    # ---- 质检纹理 ----
    qc_out = out_dir / f"texture_qc_report{suffix}.json"
    qc_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "质检纹理.py"),
        "--texture-set", str(texture_set_path),
        "--out", str(qc_out),
    ]
    qc_result = run_step(qc_cmd, check=False)
    if qc_out.exists():
        texture_qc = load_json(qc_out)
        texture_qc_issues = collect_texture_qc_issues(texture_qc)
        high_issues = [issue for issue in texture_qc_issues if issue.get("severity") == "high"]
        blocking_issues = [issue for issue in high_issues if issue.get("type") != "not_user_approved"]
        if blocking_issues:
            print(f"[错误{suffix}] 面料质检存在 high severity 问题，停止渲染：", file=sys.stderr)
            for issue in blocking_issues[:10]:
                print(f"  - {issue.get('asset_id')}: {issue.get('message', issue.get('type'))}", file=sys.stderr)
            return 1
        if high_issues:
            approval_request = {
                "request_id": f"asset_approval_required{suffix}_v1",
                "texture_set": str(texture_set_path.resolve()),
                "texture_qc_report": str(qc_out.resolve()),
                "message": "面料/图案/纯色仍为 candidate，必须经 AI 视觉 QC 或人工审批后才能继续渲染。",
                "next_step": f"审批后将 texture_set{suffix}.json 中对应 assets 的 approved 设为 true，并使用 --texture-set 指向该文件重新运行。",
                "assets": [
                    {"asset_id": issue.get("asset_id", ""), "asset_role": issue.get("asset_role", ""), "issue": issue.get("message", "")}
                    for issue in high_issues
                ],
            }
            approval_path = out_dir / f"asset_approval_request{suffix}.json"
            write_json(approval_path, approval_request)
            print(f"\n[暂停{suffix}] 已生成候选面料组合，但资产尚未审批，按生产规则停止在渲染前。")
            print(f"  面料组合: {texture_set_path.resolve()}")
            print(f"  质检报告: {qc_out.resolve()}")
            print(f"  审批请求: {approval_path.resolve()}")
            return 0
    elif qc_result.returncode != 0:
        return qc_result.returncode

    # ---- 生产规划 ----
    use_legacy = args.mode == "legacy"
    production_plan_path = out_dir / "ai_production_plan.json"

    if args.production_plan:
        provided = Path(args.production_plan)
        if provided.exists():
            apply_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "应用生产规划.py"),
                "--production-plan", str(provided),
                "--out", str(out_dir),
            ]
            run_step(apply_cmd)
        else:
            print(f"[错误{suffix}] 提供的生产规划不存在: {provided}", file=sys.stderr)
            return 1
    elif use_legacy:
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
            print(f"\n[提示{suffix}] 子 Agent 审美请求已构造。请启动子 Agent 阅读以下文件并输出 ai_piece_fill_plan.json：")
            print(f"  提示词文件: {out_dir / 'ai_fill_plan_prompt.txt'}")
            print(f"  预期输出: {out_dir / 'ai_piece_fill_plan.json'}")
            return 0
    else:
        plan_loaded_from_cache = False
        if args.reuse_cache:
            plan_hash = {
                "pattern_image": file_sha256(args.pattern),
                "texture_set": file_sha256(texture_set_path),
                "garment_type": args.garment_type,
                "brief": file_sha256(args.brief) if args.brief else "",
                "template": args.template,
                "template_size": args.template_size,
                "mode": args.mode,
                "multi_scheme": args.multi_scheme,
                "max_schemes": args.max_schemes,
                "visual_elements": file_sha256(args.visual_elements) if args.visual_elements else "",
            }
            cached = cache_lookup(out_dir, "production_plan", plan_hash)
            if cached:
                print(f"[缓存复用{suffix}] 生产规划: {cached}")
                production_plan_path.write_bytes(cached.read_bytes())
                plan_loaded_from_cache = True

        if not plan_loaded_from_cache:
            if args.construct_ai_request or not production_plan_path.exists():
                plan_request_cmd = [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "构造生产规划请求.py"),
                    "--pieces", str(pieces_path),
                    "--texture-set", str(texture_set_path),
                    "--out", str(out_dir),
                ]
                if args.brief:
                    plan_request_cmd.extend(["--brief", args.brief])
                gh_path = out_dir / "geometry_hints.json"
                if gh_path.exists():
                    plan_request_cmd.extend(["--geometry-hints", str(gh_path)])
                if args.visual_elements:
                    plan_request_cmd.extend(["--visual-elements", args.visual_elements])
                if args.ai_map:
                    plan_request_cmd.extend(["--garment-map", args.ai_map])
                run_step(plan_request_cmd)
                print(f"\n[提示{suffix}] 生产规划 AI 请求已构造。请启动子 Agent 阅读以下文件并输出 ai_production_plan.json：")
                print(f"  提示词文件: {out_dir / 'ai_production_plan_prompt.txt'}")
                print(f"  预期输出: {out_dir / 'ai_production_plan.json'}")
                return 0

        if production_plan_path.exists():
            apply_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "应用生产规划.py"),
                "--production-plan", str(production_plan_path),
                "--out", str(out_dir),
            ]
            run_step(apply_cmd)
            if args.reuse_cache and not plan_loaded_from_cache:
                cache_save(out_dir, "production_plan", plan_hash, production_plan_path)
        else:
            print(f"[警告{suffix}] ai_production_plan.json 不存在，将使用后端规则生成填充计划（draft preview only）。")

    # ---- 创建填充计划 ----
    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--garment-map", str(out_dir / "garment_map.json"),
        "--out", str(out_dir),
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
            print(f"[警告{suffix}] AI 计划不存在: {ai_plan_path}，将使用后端规则生成。")
    else:
        auto_ai_plan = out_dir / "ai_piece_fill_plan.json"
        if auto_ai_plan.exists():
            plan_cmd.extend(["--ai-plan", str(auto_ai_plan)])
            print(f"[自动{suffix}] 检测到 AI 填充计划，自动使用: {auto_ai_plan}")
    run_step(plan_cmd)

    # ---- 渲染裁片 ----
    rendered_dir = out_dir / f"rendered{suffix}"
    render_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "渲染裁片.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--fill-plan", str(out_dir / "piece_fill_plan.json"),
        "--out", str(rendered_dir),
    ]
    run_step(render_cmd)

    # ---- 多尺寸自动渲染 ----
    if args.full_set and HAS_TEMPLATE_LOADER:
        _render_size_variants(args, out_dir, texture_set_path, suffix=suffix)

    # ---- 时尚质检 ----
    fashion_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "时尚质检.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--fill-plan", str(out_dir / "piece_fill_plan.json"),
        "--rendered", str(rendered_dir),
        "--out", str(out_dir / f"fashion_qc_report{suffix}.json"),
    ]
    run_step(fashion_cmd)

    # ---- 备份本套中间产物（便于双源模式区分） ----
    if suffix:
        for src_name, dst_name in [
            ("piece_fill_plan.json", f"piece_fill_plan{suffix}.json"),
            ("garment_map.json", f"garment_map{suffix}.json"),
        ]:
            src = out_dir / src_name
            dst = out_dir / dst_name
            if src.exists():
                dst.write_bytes(src.read_bytes())

    # ---- 商业复审 ----
    if args.commercial_review:
        brief_for_review = args.brief or str(out_dir / "commercial_design_brief.json")
        if not Path(brief_for_review).exists():
            print(f"[警告{suffix}] 未找到商业设计简报: {brief_for_review}，跳过商业复审")
        else:
            review_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "构造商业复审请求.py"),
                "--preview", str(rendered_dir / "preview.png"),
                "--fill-plan", str(out_dir / "piece_fill_plan.json"),
                "--brief", brief_for_review,
                "--qc-report", str(out_dir / f"fashion_qc_report{suffix}.json"),
                "--out", str(out_dir),
            ]
            review_json_path = out_dir / "ai_commercial_review.json"
            if review_json_path.exists():
                review_cmd.extend(["--selected", str(review_json_path)])
            run_step(review_cmd)

    return 0


def _run_render_pipeline_for_scheme(
    args,
    out_dir: Path,
    texture_set_path: Path,
    pieces_path: Path,
    scheme: dict,
) -> int:
    """针对单个 scheme 执行渲染流水线（创建填充计划 → 渲染 → 质检 → 商业复审）。
    scheme 字典包含: scheme_id, suffix, garment_map, fill_plan
    失败时返回非零 exit code，但调用方负责决定是否继续下一个 scheme。"""
    scheme_id = scheme["scheme_id"]
    suffix = scheme["suffix"]
    gm_path = Path(scheme["garment_map"])
    fp_path = Path(scheme["fill_plan"])

    print(f"\n{'='*60}")
    print(f"[方案渲染 {scheme_id}] 开始独立渲染流水线")
    print(f"  garment_map: {gm_path}")
    print(f"  fill_plan:   {fp_path}")
    print(f"{'='*60}")

    # ---- 创建填充计划 ----
    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--garment-map", str(gm_path),
        "--out", str(out_dir),
    ]
    if args.brief:
        plan_cmd.extend(["--brief", args.brief])
    if fp_path.exists():
        plan_cmd.extend(["--ai-plan", str(fp_path)])
    rc = run_step(plan_cmd, check=False).returncode
    if rc != 0:
        print(f"[错误 {scheme_id}] 创建填充计划失败 (rc={rc})，跳过本方案", file=sys.stderr)
        return rc

    # ---- 渲染裁片 ----
    rendered_dir = out_dir / f"rendered{suffix}"
    render_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "渲染裁片.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--fill-plan", str(out_dir / "piece_fill_plan.json"),
        "--out", str(rendered_dir),
    ]
    rc = run_step(render_cmd, check=False).returncode
    if rc != 0:
        print(f"[错误 {scheme_id}] 渲染裁片失败 (rc={rc})，跳过本方案", file=sys.stderr)
        return rc

    # ---- 多尺寸自动渲染 ----
    if HAS_TEMPLATE_LOADER:
        _render_size_variants(args, out_dir, texture_set_path, suffix=suffix)

    # ---- 时尚质检 ----
    fashion_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "时尚质检.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--fill-plan", str(out_dir / "piece_fill_plan.json"),
        "--rendered", str(rendered_dir),
        "--out", str(out_dir / f"fashion_qc_report{suffix}.json"),
    ]
    run_step(fashion_cmd)

    # ---- 备份 scheme 中间产物 ----
    for src_name, dst_name in [
        ("piece_fill_plan.json", f"piece_fill_plan{suffix}.json"),
        ("garment_map.json", f"garment_map{suffix}.json"),
    ]:
        src = out_dir / src_name
        dst = out_dir / dst_name
        if src.exists():
            dst.write_bytes(src.read_bytes())

    # ---- 商业复审 ----
    if args.commercial_review:
        brief_for_review = args.brief or str(out_dir / "commercial_design_brief.json")
        if not Path(brief_for_review).exists():
            print(f"[警告 {scheme_id}] 未找到商业设计简报，跳过商业复审")
        else:
            review_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "构造商业复审请求.py"),
                "--preview", str(rendered_dir / "preview.png"),
                "--fill-plan", str(out_dir / "piece_fill_plan.json"),
                "--brief", brief_for_review,
                "--qc-report", str(out_dir / f"fashion_qc_report{suffix}.json"),
                "--out", str(out_dir),
            ]
            review_json_path = out_dir / f"ai_commercial_review{suffix}.json"
            if review_json_path.exists():
                review_cmd.extend(["--selected", str(review_json_path)])
            run_step(review_cmd)

    print(f"[方案渲染 {scheme_id}] 完成 ✓")
    return 0


def render_size_variants_core(
    base_fill_plan: dict,
    texture_set_path: Path,
    out_dir: Path,
    template_id: str,
    size_data: dict,
) -> None:
    """纯程序渲染多尺寸变体（可复用核心，无AI）。"""
    for size_label, mapping in size_data.items():
        piece_map = mapping.get("piece_map", {})
        scale_factor = mapping.get("scale_factor", {}).get("area_sqrt", 1.0)
        if not piece_map:
            continue

        size_pieces = load_size_pieces(template_id, size_label)
        if not size_pieces:
            print(f"[多尺寸渲染] 未找到 {size_label} 的 pieces.json，跳过")
            continue

        size_pieces_path = SKILL_DIR / "templates" / template_id / size_label / f"pieces_{size_label}.json"

        mapped_fill_plan = {
            "plan_id": f"{base_fill_plan.get('plan_id', 'auto')}_{size_label}",
            "texture_set_id": base_fill_plan.get("texture_set_id", ""),
            "locked": base_fill_plan.get("locked", False),
            "pieces": [],
        }
        base_entries = {e.get("piece_id"): e for e in base_fill_plan.get("pieces", [])}

        # aspect 反转保护：读取 aspect_warnings，对 aspect 翻转严重的裁片纠正 rotation
        warnings = mapping.get("aspect_warnings", [])
        warning_pieces = {w["target_id"]: w for w in warnings if abs(w.get("delta", 0)) > 0.3}

        for base_pid, target_pid in piece_map.items():
            entry = base_entries.get(base_pid)
            if not entry:
                continue
            mapped_entry = dict(entry)
            mapped_entry["piece_id"] = target_pid
            if target_pid in warning_pieces:
                # aspect 翻转 ≈ 朝向反了，对 base.rotation 加 90°
                base = mapped_entry.setdefault("base", {})
                if isinstance(base, dict):
                    base["rotation"] = (base.get("rotation", 0) + 90) % 360
                mapped_entry.setdefault("issues", []).append({
                    "type": "aspect_orientation_corrected",
                    "delta": warning_pieces[target_pid]["delta"],
                    "note": "auto +90° rotation for aspect inversion",
                })
            mapped_fill_plan["pieces"].append(mapped_entry)

        if not mapped_fill_plan["pieces"]:
            print(f"[多尺寸渲染] {size_label.upper()} 映射后无有效填充计划，跳过")
            continue

        mapped_plan_path = out_dir / f"piece_fill_plan_{size_label}.json"
        mapped_plan_path.write_text(json.dumps(mapped_fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")

        size_rendered_dir = out_dir / f"rendered_{size_label}"
        render_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "渲染裁片.py"),
            "--pieces", str(size_pieces_path),
            "--texture-set", str(texture_set_path),
            "--fill-plan", str(mapped_plan_path),
            "--out", str(size_rendered_dir),
            "--scale-factor", str(scale_factor),
            "--size-label", size_label,
        ]
        print(f"[多尺寸渲染] 渲染 {size_label.upper()} (scale={scale_factor:.4f}) ...")
        run_step(render_cmd)


def _render_size_variants(args, out_dir: Path, texture_set_path: Path, suffix: str = "") -> None:
    """基于-S渲染结果，纯程序生成其他尺寸的渲染输出（无AI）。"""
    pattern_path = getattr(args, "pattern", "")
    if not pattern_path:
        return

    template = find_template_by_pattern_path(pattern_path)
    if not template:
        return

    template_id = template.get("template_id", "")
    mappings = load_size_mappings(template_id)
    if not mappings:
        return

    size_data = mappings.get("sizes", {})
    if not size_data:
        return

    base_fill_plan_path = out_dir / "piece_fill_plan.json"
    if not base_fill_plan_path.exists():
        print("[多尺寸渲染] 未找到基准 fill_plan，跳过")
        return
    try:
        base_fill_plan = json.loads(base_fill_plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[多尺寸渲染] 读取基准 fill_plan 失败: {exc}")
        return

    print(f"[多尺寸渲染] 检测到多尺寸模板: {template_id}，准备渲染 {list(size_data.keys())}")
    render_size_variants_core(base_fill_plan, texture_set_path, out_dir, template_id, size_data)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Neo AI 面料看板并自动渲染服装裁片。")
    parser.add_argument("--pattern", required=True, help="透明纸样 mask PNG/WebP")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--collection-board", default="", help="已有的 Neo AI 3×3 面料看板。若省略，则调用 Neo AI 生成。")
    parser.add_argument("--texture-set", default="", help="已审批的 texture_set.json。提供后跳过看板生成/裁剪，直接使用该面料组合继续裁片映射、填充和渲染。")
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
    parser.add_argument("--brief", default="", help="商业设计简报 JSON 路径。若提供，校验 garment_type 必填；若未提供，尝试从输出目录自动读取。")
    parser.add_argument("--garment-type", default="", help="服装类型（如'儿童外套套装'、'女装连衣裙'）。走主题图路径时必填，会写入设计简报并传给部位识别。")
    parser.add_argument("--ai-plan", default="", help="子 Agent 生成的 AI 填充计划 JSON 路径。若提供，优先使用 AI 审美决策。")
    parser.add_argument("--construct-ai-request", action="store_true", help="在部位映射后构造子 Agent 审美请求并退出，等待外部子 Agent 生成 ai_piece_fill_plan.json。")
    parser.add_argument("--selected-collection", default="", help="子Agent已选择的 selected_variants.json 路径。若提供，直接生成最终看板 prompt 并跳过选择请求构造。")
    parser.add_argument("--auto-retry", type=int, default=0, help="自动重试次数（0=不重试）。时尚QC发现 high severity issues 或商业复审未通过时，自动构造返工请求并重新渲染。")
    parser.add_argument("--retry-agent-cmd", default="", help="子 Agent 自调用命令。当 auto-retry 需要修订计划但 rev 文件不存在时，脚本会尝试 subprocess 调用此命令自动生成修订计划。支持 {prompt_path} 和 {output_path} 占位符（如 \"claude -p --file {prompt_path} > {output_path}\"）。示例: \"kimi chat -p\" 或 \"claude -p\" 或 \"python3 /path/to/agent_runner.py\"")
    parser.add_argument("--ai-map", default="", help="AI子Agent输出的 ai_garment_map.json 路径。若提供，部位映射优先使用AI识别结果。")
    parser.add_argument("--template", default="", help="模板ID。优先于 garment_type 自动匹配。如 children_outerwear_set。指定后跳过AI识别，直接用模板匹配裁片部位。")
    parser.add_argument("--template-size", default="base", help="模板尺寸变体。默认 base。如 m/l/xl。")
    parser.add_argument("--template-file", default="", help="用户自定义模板 JSON 文件路径。优先于内置模板。")
    parser.add_argument("--no-template", action="store_true", help="禁用模板匹配，强制走 AI/几何推断路径。")
    parser.add_argument("--commercial-review", action=argparse.BooleanOptionalAction, default=True, help="启用整体商业感复审（默认开启）。传 --no-commercial-review 显式关闭。")
    parser.add_argument("--full-set", action="store_true", help="生成整套所有尺寸。默认只生成-S基准尺寸，加此参数后额外输出 M/L/XL/XXL（基于-S映射，纯程序）。")
    parser.add_argument("--mode", default="standard", choices=["fast", "standard", "production", "legacy"], help="运行模式。fast=跳过看板选择AI和商业复审（草稿预览），standard=默认完整流程，production=含资产审批gate和强制返工，legacy=旧分步脚本兼容模式。")
    parser.add_argument("--reuse-cache", action="store_true", help="启用缓存复用。若输入未变化，跳过对应阶段的AI调用和程序计算。")
    parser.add_argument("--production-plan", default="", help="已完成的 ai_production_plan.json 路径。若提供且缓存允许，跳过生产规划AI调用，直接应用该计划。")
    parser.add_argument("--skip-collection-selection", action="store_true", help="跳过看板候选选择AI（等效fast模式行为），程序直接取每个panel第一个variant。")
    parser.add_argument("--dual-source", action="store_true", help="启用双源并行看板生成（Neo AI + libtv）")
    parser.add_argument("--libtv-key", default="", help="libtv Access Key。优先使用 LIBTV_ACCESS_KEY 环境变量。")
    parser.add_argument("--dual-prompts", default="", help="dual_collection_prompts.json 路径。若提供，跳过设计简报中的双提示词生成，直接使用该文件。")
    parser.add_argument("--max-retries", type=int, default=2, help="双源均失败时的最大重试次数")
    parser.add_argument("--multi-scheme", action="store_true", help="启用多方案渲染模式。双源模式下，合并 A/B 资产后由 AI 生成多套设计方案并分别渲染。")
    parser.add_argument("--max-schemes", type=int, default=4, help="多方案模式下的最大方案数（默认4）")
    args = parser.parse_args()
    # fast 模式自动关闭商业复审和看板选择
    # fast 模式自动关闭商业复审和看板选择
    if args.mode == "fast":
        args.commercial_review = False
        args.skip_collection_selection = True
    if args.skip_collection_selection:
        print(f"[模式] {args.mode} — 跳过看板选择AI")
    if not args.commercial_review and args.mode != "fast":
        print("[模式] 商业复审已关闭")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ===== brief 校验 =====
    brief_path = Path(args.brief) if args.brief else None
    auto_brief = out_dir / "commercial_design_brief.json"
    if not brief_path and auto_brief.exists():
        brief_path = auto_brief
        args.brief = str(auto_brief)
    effective_garment_type = args.garment_type.strip()
    if brief_path and brief_path.exists():
        try:
            brief_data = json.loads(brief_path.read_text(encoding="utf-8"))
            garment_type = brief_data.get("garment_type", "")
            if not garment_type or garment_type.strip() == "":
                print(f"[错误] {brief_path} 中 garment_type 为空，必须提供有效的服装类型（如'儿童外套套装'、'女装连衣裙'）", file=sys.stderr)
                return 1
            if not effective_garment_type:
                effective_garment_type = garment_type.strip()
            print(f"[校验通过] garment_type='{garment_type}'")
        except Exception as exc:
            print(f"[警告] 无法读取 brief: {exc}")
    else:
        print("[警告] 未提供商业设计简报，后续步骤（部位识别、商业复审）可能缺少 garment_type 上下文")

    if (args.theme_image or args.visual_elements) and not effective_garment_type:
        print("[错误] 走主题图/视觉元素路径时必须提供 --garment-type，或提供包含 garment_type 的 --brief。", file=sys.stderr)
        return 1
    args.garment_type = effective_garment_type

    # ============================================================
    # Phase 1: 程序-only 准备层（与 AI 调用无关，可并行执行）
    # ============================================================
    # 1a. 裁片提取 —— 纯程序，不依赖任何 AI 输出，优先执行
    pieces_path = out_dir / "pieces.json"
    if not pieces_path.exists() or not args.reuse_cache:
        pieces_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "提取裁片.py"),
            "--pattern", args.pattern,
            "--out", str(out_dir),
        ]
        run_step(pieces_cmd)
    else:
        print(f"[缓存] pieces.json 已存在，跳过裁片提取")

    # 1b. 程序几何推断 → geometry_hints.json（供后续 AI 参考）
    geometry_hints_path = out_dir / "geometry_hints.json"
    if pieces_path.exists() and (not geometry_hints_path.exists() or not args.reuse_cache):
        _build_geometry_hints(pieces_path, geometry_hints_path)

    # ============================================================
    # Phase 2: 主题图/视觉元素路径（可能涉及 AI 调用，可能中途退出）
    # ============================================================
    # 注意：此阶段与 Phase 1 无依赖关系，理论上可并行
    ve_handled = False
    if args.theme_image and not args.visual_elements:
        theme_path = Path(args.theme_image)
        if not theme_path.exists():
            raise RuntimeError(f"主题图不存在: {theme_path}")
        ve_out = out_dir / "visual_elements.json"
        # 缓存检查
        if args.reuse_cache:
            ve_hash = {"theme_image": file_sha256(theme_path), "garment_type": args.garment_type}
            cached = cache_lookup(out_dir, "visual_elements", ve_hash)
            if cached:
                print(f"[缓存复用] visual_elements: {cached}")
                ve_out.write_bytes(cached.read_bytes())
                args.visual_elements = str(ve_out)
                ve_handled = True
        if not ve_handled:
            if ve_out.exists():
                print(f"[视觉提取] 已存在视觉元素分析: {ve_out}，直接使用。")
                args.visual_elements = str(ve_out)
                ve_handled = True
            else:
                # 构造子Agent视觉分析请求
                ve_cmd = [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "视觉元素提取.py"),
                    "--theme-image", str(theme_path),
                    "--out", str(out_dir),
                ]
                if args.garment_type:
                    ve_cmd.extend(["--garment-type", args.garment_type])
                run_step(ve_cmd)
                print("\n[提示] 子 Agent 视觉分析请求已构造。请启动子 Agent 阅读以下文件并输出 visual_elements.json：")
                print(f"  主题图: {theme_path}")
                print(f"  提示词文件: {out_dir / 'ai_vision_prompt.txt'}")
                print(f"  预期输出: {ve_out}")
                print("  完成后重新运行本脚本并传入 --visual-elements 参数。\n")
                return 0

    if args.visual_elements and not ve_handled:
        ve_path = Path(args.visual_elements)
        if not ve_path.exists():
            raise RuntimeError(f"visual_elements 不存在: {ve_path}")
        # 保存正确的 visual_elements 缓存（只有文件存在且有效时才缓存）
        if args.reuse_cache and args.theme_image:
            ve_hash = {"theme_image": file_sha256(args.theme_image), "garment_type": args.garment_type}
            cache_save(out_dir, "visual_elements", ve_hash, ve_path)
        # 基于视觉元素分析生成设计简报与纹理提示词
        brief_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "生成设计简报.py"),
            "--visual-elements", str(ve_path),
            "--out", str(out_dir),
        ]
        if args.garment_type:
            brief_cmd.extend(["--garment-type", args.garment_type])
        run_step(brief_cmd)
        # 构造看板候选选择请求
        if not args.skip_collection_selection and not args.selected_collection:
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
        elif args.selected_collection:
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

        if not args.prompt_file:
            ve_path_obj = Path(args.visual_elements) if args.visual_elements else None
            generated_prompt = _build_collection_prompt_from_visual_elements(out_dir, ve_path_obj)
            if generated_prompt:
                prompt_path = out_dir / "generated_collection_prompt.txt"
                prompt_path.write_text(generated_prompt, encoding="utf-8")
                args.prompt_file = str(prompt_path)
                print(f"[视觉提取] 已基于子Agent分析自动生成看板提示词: {prompt_path}")

    # ============================================================
    # 尝试读取 palette（供颜色校验和纯色提取使用）
    # ============================================================
    palette = None
    style_profile_path = out_dir / "style_profile.json"
    if style_profile_path.exists():
        try:
            sp = json.loads(style_profile_path.read_text(encoding="utf-8"))
            palette = sp.get("palette")
        except Exception:
            pass

    pieces_path = out_dir / "pieces.json"

    # ============================================================
    # 双源模式（Neo AI + libtv 并行生成）
    # ============================================================
    if args.dual_source and not args.texture_set:
        # 1. 确保 dual_collection_prompts.json 存在
        dual_prompts_path = out_dir / "dual_collection_prompts.json"
        if args.dual_prompts:
            dual_prompts_path = Path(args.dual_prompts)
        if not dual_prompts_path.exists():
            print(f"[错误] 双源模式需要 dual_collection_prompts.json。请先运行生成设计简报.py，或传入 --dual-prompts。", file=sys.stderr)
            return 1

        # 2. 初始化双源生成器
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        from 双源看板生成器 import DualBoardGenerator

        generator = DualBoardGenerator(
            out_dir=out_dir,
            neo_token=args.token,
            libtv_key=args.libtv_key or os.environ.get("LIBTV_ACCESS_KEY", ""),
            max_retries=args.max_retries,
            timeout=300,
            neo_model=args.neo_model,
            neo_size=args.neo_size,
        )

        # 3. 并行：双源看板生成 + 部位映射（部位映射不依赖 texture_set）
        from concurrent.futures import ThreadPoolExecutor
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                boards_future = executor.submit(generator.generate, dual_prompts_path)
                mapping_future = executor.submit(run_garment_mapping, args, pieces_path, out_dir)

                board_results = boards_future.result()
                mapping_future.result()
        except Exception as exc:
            print(f"[错误] 双源看板生成失败: {exc}", file=sys.stderr)
            return 1

        # 4. 处理结果
        if not board_results:
            print("[错误] 双源看板生成全部失败", file=sys.stderr)
            return 1

        # 5. 分别裁剪每套看板为 texture_set
        texture_sets = []
        for result in board_results:
            suffix = "_A" if result["style"] == "style_a" else "_B"
            board_path = result["path"]

            # 颜色校验
            if palette:
                color_warnings = validate_board_colors(board_path, palette)
                if color_warnings:
                    print(f"[颜色校验警告{suffix}] 以下面板颜色与 palette 偏差较大：")
                    for w in color_warnings:
                        print(f"  {w['panel']}: 实际 RGB{w['actual_rgb']} vs 预期 {w['expected_hex']} (偏差 {w['distance']})")
                    warn_path = out_dir / f"board_color_warnings{suffix}.json"
                    warn_path.write_text(json.dumps(color_warnings, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"  详细报告: {warn_path}")
                else:
                    print(f"[颜色校验{suffix}] 所有面板颜色与 palette 协调。")

            ts_path = crop_collection_board(board_path, out_dir, args.crop_inset, not args.no_tile_repair, palette=palette, suffix=suffix)
            texture_sets.append({"source": result["source"], "path": ts_path, "style": result["style"], "suffix": suffix})

        # 6. 多方案模式：合并 A/B 资产 → 构造多方案生产规划 → 拆解 → 逐 scheme 渲染
        if args.multi_scheme:
            print("\n[多方案模式] 启用多方案并行渲染")

            # 6a. 准备 merged_texture_set（双源合并 或 单源直接使用）
            sys.path.insert(0, str(SKILL_DIR / "scripts"))
            from 合并面料组合 import merge_texture_sets
            ts_a = next((ts for ts in texture_sets if ts["suffix"] == "_A"), None)
            ts_b = next((ts for ts in texture_sets if ts["suffix"] == "_B"), None)

            available_sources = []
            if ts_a:
                available_sources.append(("a", ts_a["path"]))
            if ts_b:
                available_sources.append(("b", ts_b["path"]))

            if not available_sources:
                print("[错误] 多方案模式需要至少一个源成功", file=sys.stderr)
                return 1

            if len(available_sources) == 2:
                # 双源均成功：合并为 18 个资产
                merged_ts_path = merge_texture_sets(ts_a["path"], ts_b["path"], out_dir)
                print(f"[多方案] 双源均成功，合并面料组合: {merged_ts_path}")
            else:
                # 单源成功：直接复制该套为 merged_texture_set.json（让 AI 从 9 个资产中组合多套方案）
                single_name, single_path = available_sources[0]
                merged_ts_path = out_dir / "merged_texture_set.json"
                ts_data = load_json(single_path)
                # 为资产 ID 统一加上源后缀，使下游逻辑一致
                for tex in ts_data.get("textures", []):
                    tex["texture_id"] = f"{tex['texture_id']}_{single_name}"
                for motif in ts_data.get("motifs", []):
                    motif["motif_id"] = f"{motif['motif_id']}_{single_name}"
                    motif["texture_id"] = f"{motif['texture_id']}_{single_name}"
                for solid in ts_data.get("solids", []):
                    solid["solid_id"] = f"{solid['solid_id']}_{single_name}"
                ts_data["texture_set_id"] = f"{out_dir.name}_merged_from_{single_name}"
                ts_data["source_sets"] = {single_name: str(Path(single_path).resolve())}
                write_json(merged_ts_path, ts_data)
                print(f"[多方案] 仅源{single_name.upper()} 成功，从 9 个资产中组合多套方案: {merged_ts_path}")

            # 6b. 质检合并后的资产（只做一次）
            qc_out = out_dir / "texture_qc_report_merged.json"
            qc_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "质检纹理.py"),
                "--texture-set", str(merged_ts_path),
                "--out", str(qc_out),
            ]
            qc_result = run_step(qc_cmd, check=False)
            if qc_out.exists():
                texture_qc = load_json(qc_out)
                texture_qc_issues = collect_texture_qc_issues(texture_qc)
                blocking_issues = [issue for issue in texture_qc_issues if issue.get("severity") == "high" and issue.get("type") != "not_user_approved"]
                if blocking_issues:
                    print("[错误] 合并面料质检存在 high severity 问题，停止渲染：", file=sys.stderr)
                    for issue in blocking_issues[:10]:
                        print(f"  - {issue.get('asset_id')}: {issue.get('message', issue.get('type'))}", file=sys.stderr)
                    return 1
            elif qc_result.returncode != 0:
                return qc_result.returncode

            # 6c. 构造多方案生产规划请求
            multi_plan_path = out_dir / "ai_multi_production_plan.json"
            plan_loaded_from_cache = False
            if args.reuse_cache:
                plan_hash = {
                    "pattern_image": file_sha256(args.pattern),
                    "texture_set": file_sha256(merged_ts_path),
                    "garment_type": args.garment_type,
                    "brief": file_sha256(args.brief) if args.brief else "",
                    "template": args.template,
                    "template_size": args.template_size,
                    "mode": args.mode,
                    "multi_scheme": args.multi_scheme,
                    "max_schemes": args.max_schemes,
                    "visual_elements": file_sha256(args.visual_elements) if args.visual_elements else "",
                }
                cached = cache_lookup(out_dir, "multi_production_plan", plan_hash)
                if cached:
                    print(f"[缓存复用] 多方案生产规划: {cached}")
                    multi_plan_path.write_bytes(cached.read_bytes())
                    plan_loaded_from_cache = True

            if not plan_loaded_from_cache:
                if args.construct_ai_request or not multi_plan_path.exists():
                    plan_request_cmd = [
                        sys.executable,
                        str(SKILL_DIR / "scripts" / "构造生产规划请求.py"),
                        "--pieces", str(pieces_path),
                        "--texture-set", str(merged_ts_path),
                        "--out", str(out_dir),
                        "--multi-scheme",
                        "--max-schemes", str(args.max_schemes),
                    ]
                    if args.brief:
                        plan_request_cmd.extend(["--brief", args.brief])
                    gh_path = out_dir / "geometry_hints.json"
                    if gh_path.exists():
                        plan_request_cmd.extend(["--geometry-hints", str(gh_path)])
                    if args.visual_elements:
                        plan_request_cmd.extend(["--visual-elements", args.visual_elements])
                    if args.ai_map:
                        plan_request_cmd.extend(["--garment-map", args.ai_map])
                    run_step(plan_request_cmd)
                    print(f"\n[提示] 多方案生产规划 AI 请求已构造。请启动子 Agent 阅读以下文件并输出 ai_multi_production_plan.json：")
                    print(f"  提示词文件: {out_dir / 'ai_production_plan_prompt.txt'}")
                    print(f"  预期输出: {multi_plan_path}")
                    print("  该文件应包含 schemes 数组，每套方案含 garment_map + piece_fill_plan。")
                    print("  完成后重新运行本脚本并传入 --dual-source --multi-scheme 参数。\n")
                    return 0

            # 6d. 拆解多方案
            if multi_plan_path.exists():
                apply_cmd = [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "应用生产规划.py"),
                    "--production-plan", str(multi_plan_path),
                    "--out", str(out_dir),
                    "--multi-scheme",
                ]
                run_step(apply_cmd)
                if args.reuse_cache and not plan_loaded_from_cache:
                    cache_save(out_dir, "multi_production_plan", plan_hash, multi_plan_path)
            else:
                print(f"[错误] {multi_plan_path} 不存在，无法拆解多方案", file=sys.stderr)
                return 1

            # 6e. 读取 schemes 元数据并逐 scheme 渲染
            schemes_meta_path = out_dir / "schemes_meta.json"
            if not schemes_meta_path.exists():
                print(f"[错误] schemes_meta.json 不存在", file=sys.stderr)
                return 1
            schemes_meta = load_json(schemes_meta_path)
            schemes = schemes_meta.get("schemes", [])
            if not schemes:
                print("[警告] schemes_meta.json 中无 scheme 定义", file=sys.stderr)
                return 1

            print(f"\n[多方案渲染] 共 {len(schemes)} 套方案，开始逐套独立渲染（失败跳过）")
            success_schemes = []
            failed_schemes = []
            for scheme in schemes:
                rc = _run_render_pipeline_for_scheme(args, out_dir, merged_ts_path, pieces_path, scheme)
                if rc == 0:
                    success_schemes.append(scheme["scheme_id"])
                else:
                    failed_schemes.append(scheme["scheme_id"])
                    print(f"[多方案渲染] {scheme['scheme_id']} 失败，继续下一套...")

            print(f"\n[多方案渲染完成] 成功 {len(success_schemes)}/{len(schemes)} 套")
            if failed_schemes:
                print(f"  失败方案: {', '.join(failed_schemes)}")

            summary = {
                "面料看板": [str(r["path"]) for r in board_results],
                "面料组合_A": str(ts_a["path"]),
                "面料组合_B": str(ts_b["path"]),
                "合并面料组合": str(merged_ts_path),
                "多方案生产规划": str(multi_plan_path),
                "方案元数据": str(schemes_meta_path),
                "成功方案": success_schemes,
                "失败方案": failed_schemes,
                "渲染目录": [str((out_dir / f"rendered{sc['suffix']}").resolve()) for sc in schemes],
            }
            write_json(out_dir / "automation_summary.json", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        # 6. 非多方案模式：分别执行后续渲染流水线
        if len(texture_sets) == 2:
            print(f"\n[双源模式] 两套看板均成功，将分别渲染两种风格")
            for ts in texture_sets:
                rc = _run_render_pipeline(args, out_dir, ts["path"], ts["suffix"], pieces_path)
                if rc != 0:
                    print(f"[警告] 风格 {ts['suffix']} 的渲染流水线返回非零: {rc}")
        else:
            print(f"\n[双源模式] 仅一套看板成功 ({texture_sets[0]['source']})，使用该套继续")
            rc = _run_render_pipeline(args, out_dir, texture_sets[0]["path"], texture_sets[0]["suffix"], pieces_path)
            if rc != 0:
                return rc

        # 双源模式下，后续渲染已在 _run_render_pipeline 中完成，直接返回
        summary = {
            "面料看板": [str(r["path"]) for r in board_results],
            "面料组合": [str(ts["path"]) for ts in texture_sets],
            "渲染目录": [str((out_dir / f"rendered{ts['suffix']}").resolve()) for ts in texture_sets],
        }
        write_json(out_dir / "automation_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    # ============================================================
    # 单源模式（原有逻辑）
    # ============================================================
    if args.texture_set:
        texture_set_path = Path(args.texture_set)
        if not texture_set_path.is_absolute():
            texture_set_path = texture_set_path.resolve() if texture_set_path.exists() else (out_dir / texture_set_path).resolve()
        if not texture_set_path.exists():
            raise RuntimeError(f"面料组合不存在: {texture_set_path}")
        texture_set_payload = load_json(texture_set_path)
        source_board = texture_set_payload.get("source_collection_board", "")
        board_path = Path(source_board).resolve() if source_board else Path(args.collection_board or texture_set_path).resolve()
        print(f"使用已提供面料组合: {texture_set_path}")
    else:
        board_path = Path(args.collection_board).resolve() if args.collection_board else generate_board(args, out_dir).resolve()
        if not board_path.exists():
            raise RuntimeError(f"面料看板未找到: {board_path}")
        print(f"使用面料看板: {board_path}")

    # 看板颜色协调性校验
    if palette and not args.texture_set:
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

    if not args.texture_set:
        texture_set_path = crop_collection_board(board_path, out_dir, args.crop_inset, not args.no_tile_repair, palette=palette)

    # 部位映射（单源模式下执行；双源模式下已在并行线程中完成）
    run_garment_mapping(args, pieces_path, out_dir)

    qc_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "质检纹理.py"),
        "--texture-set",
        str(texture_set_path),
        "--out",
        str(out_dir / "texture_qc_report.json"),
    ]
    qc_result = run_step(qc_cmd, check=False)
    texture_qc_report_path = out_dir / "texture_qc_report.json"
    if texture_qc_report_path.exists():
        texture_qc = load_json(texture_qc_report_path)
        texture_qc_issues = collect_texture_qc_issues(texture_qc)
        high_issues = [issue for issue in texture_qc_issues if issue.get("severity") == "high"]
        blocking_issues = [issue for issue in high_issues if issue.get("type") != "not_user_approved"]
        if blocking_issues:
            print("[错误] 面料质检存在 high severity 问题，停止渲染。请修复资产后重试：", file=sys.stderr)
            for issue in blocking_issues[:10]:
                print(f"  - {issue.get('asset_id')}: {issue.get('message', issue.get('type'))}", file=sys.stderr)
            return 1
        if high_issues:
            approval_request = {
                "request_id": "asset_approval_required_v1",
                "texture_set": str(texture_set_path.resolve()),
                "texture_qc_report": str(texture_qc_report_path.resolve()),
                "message": "面料/图案/纯色仍为 candidate，必须经 AI 视觉 QC 或人工审批后才能继续渲染。",
                "next_step": "审批后将 texture_set.json 中对应 assets 的 approved 设为 true，并使用 --texture-set 指向该文件重新运行。",
                "assets": [
                    {
                        "asset_id": issue.get("asset_id", ""),
                        "asset_role": issue.get("asset_role", ""),
                        "issue": issue.get("message", ""),
                    }
                    for issue in high_issues
                ],
            }
            approval_path = out_dir / "asset_approval_request.json"
            write_json(approval_path, approval_request)
            print("\n[暂停] 已生成候选面料组合，但资产尚未审批，按生产规则停止在渲染前。")
            print(f"  面料组合: {texture_set_path.resolve()}")
            print(f"  质检报告: {texture_qc_report_path.resolve()}")
            print(f"  审批请求: {approval_path.resolve()}")
            print("  审批后重新运行时传入 --texture-set 指向已审批的 texture_set.json。")
            return 0
    elif qc_result.returncode != 0:
        return qc_result.returncode

    # ============================================================
    # Phase 3: 生产规划（合并部位识别 + 审美决策）
    # ============================================================
    # 生成 production_context（用于缓存 key 和状态追踪）
    ctx_path = build_production_context(args, out_dir)

    # 根据模式选择路径
    use_legacy = args.mode == "legacy"
    production_plan_path = out_dir / "ai_production_plan.json"

    if args.production_plan:
        # 用户已提供生产规划，直接应用
        provided = Path(args.production_plan)
        if provided.exists():
            apply_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "应用生产规划.py"),
                "--production-plan", str(provided),
                "--out", str(out_dir),
            ]
            run_step(apply_cmd)
            print(f"[生产规划] 已应用用户提供的规划: {provided}")
        else:
            print(f"[错误] 提供的生产规划不存在: {provided}", file=sys.stderr)
            return 1
    elif use_legacy:
        # legacy 模式：保持旧流程（构造审美请求 → ai_plan → 填充计划）
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
    else:
        # 新流程：构造合并生产规划请求
        # 缓存检查
        plan_loaded_from_cache = False
        if args.reuse_cache:
            plan_hash = {
                "pattern_image": file_sha256(args.pattern),
                "texture_set": file_sha256(texture_set_path),
                "garment_type": args.garment_type,
                "brief": file_sha256(args.brief) if args.brief else "",
                "template": args.template,
                "template_size": args.template_size,
                "mode": args.mode,
                "multi_scheme": args.multi_scheme,
                "max_schemes": args.max_schemes,
                "visual_elements": file_sha256(args.visual_elements) if args.visual_elements else "",
            }
            cached = cache_lookup(out_dir, "production_plan", plan_hash)
            if cached:
                print(f"[缓存复用] 生产规划: {cached}")
                production_plan_path.write_bytes(cached.read_bytes())
                plan_loaded_from_cache = True

        if not plan_loaded_from_cache:
            if args.construct_ai_request or not production_plan_path.exists():
                # 构造生产规划请求（合并部位识别 + 审美决策）
                plan_request_cmd = [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "构造生产规划请求.py"),
                    "--pieces", str(pieces_path),
                    "--texture-set", str(texture_set_path),
                    "--out", str(out_dir),
                ]
                if args.brief:
                    plan_request_cmd.extend(["--brief", args.brief])
                gh_path = out_dir / "geometry_hints.json"
                if gh_path.exists():
                    plan_request_cmd.extend(["--geometry-hints", str(gh_path)])
                if args.visual_elements:
                    plan_request_cmd.extend(["--visual-elements", args.visual_elements])
                if args.ai_map:
                    plan_request_cmd.extend(["--garment-map", args.ai_map])
                run_step(plan_request_cmd)
                print("\n[提示] 生产规划 AI 请求已构造。请启动子 Agent 阅读以下文件并输出 ai_production_plan.json：")
                print(f"  提示词文件: {out_dir / 'ai_production_plan_prompt.txt'}")
                print(f"  预期输出: {out_dir / 'ai_production_plan.json'}")
                print("  该文件应包含 garment_map + piece_fill_plan 两部分。")
                print("  完成后重新运行本脚本并传入 --production-plan 参数，或直接放入输出目录。\n")
                return 0

        # 应用生产规划（拆解为 garment_map + ai_piece_fill_plan）
        if production_plan_path.exists():
            apply_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "应用生产规划.py"),
                "--production-plan", str(production_plan_path),
                "--out", str(out_dir),
            ]
            run_step(apply_cmd)
            if args.reuse_cache and not plan_loaded_from_cache:
                cache_save(out_dir, "production_plan", plan_hash, production_plan_path)
        else:
            print("[警告] ai_production_plan.json 不存在，将使用后端规则生成填充计划（draft preview only）。")

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
    else:
        auto_ai_plan = out_dir / "ai_piece_fill_plan.json"
        if auto_ai_plan.exists():
            plan_cmd.extend(["--ai-plan", str(auto_ai_plan)])
            print(f"[自动] 检测到 AI 填充计划，自动使用: {auto_ai_plan}")
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

    # ========== 多尺寸自动渲染（纯程序，无AI）==========
    if args.full_set and HAS_TEMPLATE_LOADER:
        _render_size_variants(args, out_dir, texture_set_path)

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

    # ===== 商业复审 =====
    commercial_approved = True
    if args.commercial_review:
        brief_for_review = args.brief or str(out_dir / "commercial_design_brief.json")
        if not Path(brief_for_review).exists():
            print(f"[警告] 未找到商业设计简报: {brief_for_review}，跳过商业复审")
            args.commercial_review = False
        else:
            review_cmd = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "构造商业复审请求.py"),
                "--preview", str(rendered_dir / "preview.png"),
                "--fill-plan", str(out_dir / "piece_fill_plan.json"),
                "--brief", brief_for_review,
                "--qc-report", str(out_dir / "fashion_qc_report.json"),
                "--out", str(out_dir),
            ]
            # 如果子Agent已输出复审结果，进入验证模式
            review_json_path = out_dir / "ai_commercial_review.json"
            if review_json_path.exists():
                review_cmd.extend(["--selected", str(review_json_path)])
            run_step(review_cmd)

            # 读取验证后的结果
            review_result_path = out_dir / "commercial_review_result.json"
            if review_result_path.exists():
                review = json.loads(review_result_path.read_text(encoding="utf-8"))
                commercial_approved = review.get("approved", False)
            else:
                commercial_approved = False
                print("\n[提示] 商业复审请求已构造。请启动子Agent完成商业复审：")
                print(f"  提示词文件: {out_dir / 'ai_commercial_review_prompt.txt'}")
                print(f"  预期输出: {out_dir / 'ai_commercial_review.json'}")
                if use_legacy:
                    print("  完成后重新运行本脚本并传入 --commercial-review --ai-plan <修订计划>")
                else:
                    print("  完成后重新运行本脚本并传入 --commercial-review --production-plan <修订规划>")

    # 质检反馈闭环：自动重试模式
    if args.auto_retry > 0:
        qc_report_path = out_dir / "fashion_qc_report.json"
        retry_count = 0
        while retry_count < args.auto_retry and qc_report_path.exists():
            qc = json.loads(qc_report_path.read_text(encoding="utf-8"))
            high_issues = [i for i in qc.get("issues", []) if i.get("severity") == "high"]
            qc_fail = bool(high_issues)

            if not qc_fail and commercial_approved:
                print(f"[自动重试] 第 {retry_count} 轮全部通过（质检+商业复审）")
                break

            all_issues = qc.get("issues", [])
            if not all_issues and commercial_approved:
                break

            retry_count += 1
            total_blocks = len(high_issues) + (0 if commercial_approved else 1)
            print(f"\n[自动重试] 第 {retry_count}/{args.auto_retry} 轮：发现 {len(high_issues)} 个 high severity issues" + ("" if commercial_approved else " + 商业复审未通过"))
            # 使用返工提示词让子Agent修订
            # 新流程优先找 production_plan_rev，legacy 找 ai_piece_fill_plan_rev
            revised_plan_path = out_dir / f"ai_production_plan_rev{retry_count}.json"
            revised_is_production_plan = True
            if not revised_plan_path.exists():
                revised_plan_path = out_dir / f"ai_piece_fill_plan_rev{retry_count}.json"
                revised_is_production_plan = False
            if revised_plan_path.exists():
                print(f"[自动重试] 使用修订计划: {revised_plan_path}")
                # 重新运行创建填充计划（使用修订后的 plan）
                if revised_is_production_plan and not use_legacy:
                    # 新流程：先应用生产规划，再跑填充计划
                    apply_cmd = [
                        sys.executable,
                        str(SKILL_DIR / "scripts" / "应用生产规划.py"),
                        "--production-plan", str(revised_plan_path),
                        "--out", str(out_dir),
                    ]
                    run_step(apply_cmd)
                    plan_cmd = [
                        sys.executable,
                        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
                        "--pieces", str(pieces_path),
                        "--texture-set", str(texture_set_path),
                        "--garment-map", str(out_dir / "garment_map.json"),
                        "--out", str(out_dir),
                        "--ai-plan", str(out_dir / "ai_piece_fill_plan.json"),
                    ]
                else:
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
                # 若启用商业复审，重新调用（必须清除旧结果，避免审批污染）
                if args.commercial_review and Path(brief_for_review).exists():
                    # 清除旧商业复审结果，防止新渲染被旧审批放行
                    for stale in [out_dir / "ai_commercial_review.json", out_dir / "commercial_review_result.json"]:
                        if stale.exists():
                            stale.unlink()
                            print(f"[自动重试] 清除旧商业复审结果: {stale.name}")
                    review_cmd = [
                        sys.executable,
                        str(SKILL_DIR / "scripts" / "构造商业复审请求.py"),
                        "--preview", str(rendered_dir / "preview.png"),
                        "--fill-plan", str(out_dir / "piece_fill_plan.json"),
                        "--brief", brief_for_review,
                        "--qc-report", str(out_dir / "fashion_qc_report.json"),
                        "--out", str(out_dir),
                    ]
                    run_step(review_cmd)
                    review_result_path = out_dir / "commercial_review_result.json"
                    if review_result_path.exists():
                        review = json.loads(review_result_path.read_text(encoding="utf-8"))
                        commercial_approved = review.get("approved", False)
                    else:
                        commercial_approved = False
            else:
                # 尝试通过外部命令自调用子 Agent
                if args.retry_agent_cmd:
                    import shlex
                    import subprocess
                    prompt_path = out_dir / "rework_prompt.txt"
                    if prompt_path.exists():
                        cmd_str = args.retry_agent_cmd
                        print(f"[自动重试] 尝试调用子 Agent: {cmd_str}")
                        try:
                            import re
                            prompt_text = prompt_path.read_text(encoding="utf-8")
                            env = os.environ.copy()
                            env["AGENT_OUTPUT_PATH"] = str(revised_plan_path.resolve())
                            env["AGENT_TASK"] = "revise_fill_plan"
                            env["AGENT_PROMPT_PATH"] = str(prompt_path.resolve())

                            # 支持占位符替换：{prompt_path} / {output_path}
                            resolved_cmd = cmd_str.replace("{prompt_path}", str(prompt_path.resolve())).replace("{output_path}", str(revised_plan_path.resolve()))
                            cmd_parts = shlex.split(resolved_cmd)

                            # 判断命令是否包含文件重定向（> output_path），如果有则 stdin 不传 prompt_text
                            has_file_redirection = ">" in resolved_cmd
                            if has_file_redirection:
                                proc = subprocess.run(
                                    cmd_parts,
                                    capture_output=True,
                                    text=True,
                                    env=env,
                                    timeout=300,
                                )
                            else:
                                proc = subprocess.run(
                                    cmd_parts,
                                    input=prompt_text,
                                    capture_output=True,
                                    text=True,
                                    env=env,
                                    timeout=300,
                                )
                            stdout = proc.stdout if proc.returncode == 0 else ""
                            if proc.returncode != 0:
                                print(f"[自动重试] 子 Agent 调用失败 (rc={proc.returncode})")
                                if proc.stderr:
                                    print(f"  stderr: {proc.stderr[:200]}")
                            elif stdout.strip():
                                # 尝试解析 stdout 为 JSON
                                candidate_json = stdout.strip()
                                extracted = None
                                # 优先匹配 ```json ... ``` 代码块
                                code_block = re.search(r"```json\s*(.*?)\s*```", candidate_json, re.DOTALL)
                                if code_block:
                                    extracted = code_block.group(1).strip()
                                else:
                                    #  fallback：找最外层 { ... }
                                    start = candidate_json.find("{")
                                    end = candidate_json.rfind("}")
                                    if start != -1 and end != -1 and end > start:
                                        extracted = candidate_json[start:end+1]

                                if extracted:
                                    try:
                                        parsed = json.loads(extracted)
                                        # 轻量 schema 校验：必须有 pieces 数组，每个 piece 有 piece_id 和 base
                                        pieces = parsed.get("pieces")
                                        if not isinstance(pieces, list) or len(pieces) == 0:
                                            raise ValueError("缺少 pieces 数组")
                                        for p in pieces:
                                            if not p.get("piece_id"):
                                                raise ValueError(f"piece 缺少 piece_id: {p}")
                                            if "base" not in p:
                                                raise ValueError(f"piece {p.get('piece_id')} 缺少 base 字段")
                                        # schema 校验通过
                                        revised_plan_path.write_text(extracted, encoding="utf-8")
                                        print(f"[自动重试] 子 Agent 成功生成修订计划: {revised_plan_path}")
                                        continue
                                    except (json.JSONDecodeError, ValueError) as ve:
                                        print(f"[自动重试] 子 Agent 输出 JSON schema 校验失败: {ve}")
                                        print(f"  stdout 前 300 字:\n{proc.stdout[:300]}")
                                        failed_path = revised_plan_path.with_suffix(".failed.txt")
                                        failed_path.write_text(proc.stdout, encoding="utf-8")
                                        print(f"  完整 stdout 已保存到: {failed_path}")
                                else:
                                    print(f"[自动重试] 子 Agent 输出无法解析为 JSON，stdout 前 200 字:\n{proc.stdout[:200]}")
                                    failed_path = revised_plan_path.with_suffix(".failed.txt")
                                    failed_path.write_text(proc.stdout, encoding="utf-8")
                                    print(f"  完整 stdout 已保存到: {failed_path}")
                        except subprocess.TimeoutExpired:
                            print("[自动重试] 子 Agent 调用超时（300s）")
                        except Exception as exc:
                            print(f"[自动重试] 子 Agent 调用异常: {exc}")
                # fallback：提示用户手动
                print(f"[自动重试] 等待外部子Agent生成修订计划: {revised_plan_path}")
                print("  请启动子Agent，传入 rework_prompt.txt，输出 ai_piece_fill_plan_rev1.json")
                if args.commercial_review:
                    print(f"  同时更新商业复审: {out_dir / 'ai_commercial_review.json'}")
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
        "商业复审结果": str((out_dir / "commercial_review_result.json").resolve()) if args.commercial_review else "",
    }
    write_json(out_dir / "automation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
