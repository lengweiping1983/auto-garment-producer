#!/usr/bin/env python3
"""
在成衣渲染前验证已批准的面料资产。
"""
import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def edge_similarity_score(img: Image.Image) -> float:
    """计算对边相似度，评估纹理可平铺性。"""
    sample = img.convert("RGB").resize((512, 512), Image.Resampling.LANCZOS)
    left = sample.crop((0, 0, 24, sample.height))
    right = sample.crop((sample.width - 24, 0, sample.width, sample.height)).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    top = sample.crop((0, 0, sample.width, 24))
    bottom = sample.crop((0, sample.height - 24, sample.width, sample.height)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    diff_lr = ImageStat.Stat(ImageChops.difference(left, right)).mean
    diff_tb = ImageStat.Stat(ImageChops.difference(top, bottom)).mean
    avg_diff = sum(diff_lr + diff_tb) / 6
    return max(0.0, min(1.0, 1.0 - avg_diff / 96.0))


def variation_score(img: Image.Image) -> float:
    """计算纹理视觉变化度。"""
    sample = img.convert("RGB").resize((256, 256), Image.Resampling.LANCZOS)
    std = ImageStat.Stat(sample).stddev
    return max(0.0, min(1.0, sum(std) / 3 / 72.0))


def text_residual_score(img: Image.Image) -> float:
    """检测纹理中是否含有文字残留。返回 0-1 分数，越高表示文字越明显。
    通过检测高对比度水平线条簇来识别文字特征。
    """
    gray = img.convert("L").resize((256, 256), Image.Resampling.LANCZOS)
    width, height = gray.size
    pixels = list(gray.get_flattened_data())

    # 水平投影：每行相邻像素的平均差异
    row_variations = []
    for y in range(height):
        row = [pixels[y * width + x] for x in range(width)]
        diffs = [abs(row[i] - row[i - 1]) for i in range(1, len(row))]
        row_variations.append(sum(diffs) / max(1, len(diffs)))

    # 检测边缘密度（文字通常有高边缘密度）
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_mean = ImageStat.Stat(edges).mean[0]

    # 综合分数：高变化行比例 + 边缘密度
    high_variation_ratio = sum(1 for v in row_variations if v > 25) / max(1, height)
    edge_score = min(1.0, edge_mean / 48.0)
    return min(1.0, high_variation_ratio * 0.6 + edge_score * 0.4)


def hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    value = str(value or "").strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def flatten_palette(style_profile: dict | None) -> list[tuple[int, int, int]]:
    if not style_profile:
        return []
    palette = style_profile.get("palette", style_profile)
    values = []
    if isinstance(palette, dict):
        for key in ("primary", "secondary", "accent", "dark"):
            values.extend(palette.get(key, []) or [])
    elif isinstance(palette, list):
        values = palette
    rgbs = [hex_to_rgb(v) for v in values]
    return [rgb for rgb in rgbs if rgb]


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def nearest_palette_distance(rgb: tuple[int, int, int], palette: list[tuple[int, int, int]]) -> float | None:
    if not palette:
        return None
    return min(color_distance(rgb, p) for p in palette)


def dominant_rgb(img: Image.Image) -> tuple[int, int, int]:
    sample = img.convert("RGBA").resize((96, 96), Image.Resampling.LANCZOS)
    pixels = [p[:3] for p in sample.getdata() if len(p) < 4 or p[3] > 32]
    if not pixels:
        pixels = [p[:3] for p in sample.getdata()]
    quant = Image.new("RGB", (len(pixels), 1))
    quant.putdata(pixels)
    q = quant.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    palette = q.getpalette() or []
    counts = q.getcolors() or []
    counts.sort(reverse=True)
    idx = counts[0][1] if counts else 0
    offset = idx * 3
    return tuple(palette[offset:offset + 3]) if offset + 2 < len(palette) else (0, 0, 0)


def alpha_quality(img: Image.Image) -> dict:
    alpha = img.convert("RGBA").getchannel("A").resize((128, 128), Image.Resampling.LANCZOS)
    values = list(alpha.getdata())
    total = max(1, len(values))
    transparent_ratio = sum(1 for v in values if v <= 12) / total
    opaque_ratio = sum(1 for v in values if v >= 245) / total
    semi_ratio = sum(1 for v in values if 12 < v < 245) / total
    edge = Image.new("L", alpha.size, 0)
    edge.paste(alpha.crop((0, 0, alpha.width, 8)), (0, 0))
    edge.paste(alpha.crop((0, alpha.height - 8, alpha.width, alpha.height)), (0, alpha.height - 8))
    edge.paste(alpha.crop((0, 0, 8, alpha.height)), (0, 0))
    edge.paste(alpha.crop((alpha.width - 8, 0, alpha.width, alpha.height)), (alpha.width - 8, 0))
    edge_alpha_mean = ImageStat.Stat(edge).mean[0]
    return {
        "transparent_ratio": round(transparent_ratio, 3),
        "opaque_ratio": round(opaque_ratio, 3),
        "semi_transparent_ratio": round(semi_ratio, 3),
        "edge_alpha_mean": round(edge_alpha_mean, 2),
    }


def validate_texture(texture: dict, base_dir: Path, palette: list[tuple[int, int, int]] | None = None) -> dict:
    """验证单个面料资产。issues=high severity，warnings=medium/low。"""
    issues = []      # high severity
    warnings = []    # medium / low severity
    path = Path(texture.get("path", ""))
    if not path.is_absolute():
        path = base_dir / path
    if not path.exists():
        return {
            "texture_id": texture.get("texture_id", ""),
            "program_qc_status": "fail",
            "approved": False,
            "issues": [{"type": "missing_file", "severity": "high", "message": f"文件不存在: {path}"}],
            "warnings": [],
        }
    try:
        with Image.open(path) as opened:
            img = opened.convert("RGBA")
            width, height = img.size
            tileable = edge_similarity_score(img)
            variation = variation_score(img)
            text_score = text_residual_score(img)
            dom = dominant_rgb(img)
    except Exception as exc:
        return {
            "texture_id": texture.get("texture_id", ""),
            "program_qc_status": "fail",
            "approved": False,
            "issues": [{"type": "open_failed", "severity": "high", "message": f"无法打开: {exc}"}],
            "warnings": [],
        }
    role = texture.get("role", "")
    is_solid = "solid" in role.lower()
    is_body_role = any(token in role.lower() for token in ("base", "main", "body", "secondary"))

    if width < 512 or height < 512:
        issues.append({"type": "too_small", "severity": "high", "message": f"尺寸过小: {width}x{height}"})

    # 可平铺分数：水彩/有机纹理可能天然不平铺，分两级
    if tileable < 0.35:
        issues.append({"type": "low_tileable_score", "severity": "high", "message": f"可平铺分数严重过低: {tileable:.3f}"})
    elif tileable < 0.55 and not is_solid:
        warnings.append({"type": "low_tileable_score", "severity": "medium", "message": f"可平铺分数偏低: {tileable:.3f}（水彩/有机纹理可能误报，请人工复核）"})

    # 变化度：纯色面板允许低变化度
    if variation < 0.03 and not is_solid:
        issues.append({"type": "low_variation", "severity": "high", "message": f"变化度过低: {variation:.3f}"})
    elif variation < 0.06 and not is_solid:
        warnings.append({"type": "low_variation", "severity": "medium", "message": f"变化度偏低: {variation:.3f}"})

    # 文字残留
    if text_score > 0.70:
        issues.append({"type": "text_residual_detected", "severity": "high", "message": f"检测到严重文字残留: {text_score:.3f}"})
    elif text_score > 0.50:
        warnings.append({"type": "text_residual_detected", "severity": "medium", "message": f"可能检测到文字残留: {text_score:.3f}，请人工复核"})

    palette_distance = nearest_palette_distance(dom, palette or [])
    if palette_distance is not None:
        if is_body_role and palette_distance > 145:
            issues.append({"type": "palette_mismatch", "severity": "high", "dominant_rgb": dom, "distance": round(palette_distance, 1), "message": f"大身/基础面料主色偏离 style_profile 色板过大: {round(palette_distance, 1)}"})
        elif palette_distance > 95:
            warnings.append({"type": "palette_mismatch", "severity": "medium", "dominant_rgb": dom, "distance": round(palette_distance, 1), "message": f"面料主色与 style_profile 色板距离较大: {round(palette_distance, 1)}"})

    if not texture.get("approved", False):
        issues.append({"type": "not_user_approved", "severity": "high", "message": "texture.approved 不为 true"})

    program_qc_status = "fail" if issues else ("warn" if warnings else "pass")
    return {
        "texture_id": texture.get("texture_id", ""),
        "role": texture.get("role", ""),
        "path": str(path.resolve()),
        "program_qc_status": program_qc_status,
        "approved": program_qc_status == "pass",
        "tileable_score": round(tileable, 3),
        "variation_score": round(variation, 3),
        "text_residual_score": round(text_score, 3),
        "dominant_rgb": dom,
        "palette_distance": round(palette_distance, 1) if palette_distance is not None else None,
        "issues": issues,
        "warnings": warnings,
    }


def validate_motif(motif: dict, base_dir: Path, palette: list[tuple[int, int, int]] | None = None) -> dict:
    """验证单个图案资产。"""
    issues = []
    warnings = []
    path = Path(motif.get("path", ""))
    if not path.is_absolute():
        path = base_dir / path
    if not path.exists():
        return {
            "motif_id": motif.get("motif_id", ""),
            "program_qc_status": "fail",
            "approved": False,
            "issues": [{"type": "missing_file", "severity": "high", "message": f"文件不存在: {path}"}],
            "warnings": [],
        }
    try:
        with Image.open(path).convert("RGBA") as img:
            width, height = img.size
            alpha = img.getchannel("A")
            alpha_min, alpha_max = alpha.getextrema()
            dom = dominant_rgb(img)
            alpha_stats = alpha_quality(img)
    except Exception as exc:
        return {
            "motif_id": motif.get("motif_id", ""),
            "program_qc_status": "fail",
            "approved": False,
            "issues": [{"type": "open_failed", "severity": "high", "message": f"无法打开: {exc}"}],
            "warnings": [],
        }
    if width < 128 or height < 128:
        issues.append({"type": "too_small", "severity": "high", "message": f"尺寸过小: {width}x{height}"})
    if alpha_min == 255 and alpha_max == 255:
        issues.append({"type": "missing_transparency", "severity": "high", "message": "定位图案资产通常应有透明背景"})
    elif alpha_stats["transparent_ratio"] < 0.08:
        issues.append({"type": "unclean_motif_background", "severity": "high", "alpha_quality": alpha_stats, "message": "定位图案透明区域过少，可能是整张背景贴片而非干净 motif"})
    elif alpha_stats["semi_transparent_ratio"] > 0.55 and alpha_stats["edge_alpha_mean"] > 24:
        issues.append({"type": "semi_transparent_full_patch", "severity": "high", "alpha_quality": alpha_stats, "message": "定位图案大面积半透明且边缘有残留，容易形成主题图残影贴片"})

    palette_distance = nearest_palette_distance(dom, palette or [])
    if palette_distance is not None and palette_distance > 110:
        warnings.append({"type": "motif_palette_mismatch", "severity": "medium", "dominant_rgb": dom, "distance": round(palette_distance, 1), "message": f"motif 主色与 style_profile 色板距离较大: {round(palette_distance, 1)}"})
    if not motif.get("approved", False):
        issues.append({"type": "not_user_approved", "severity": "high", "message": "motif.approved 不为 true"})
    program_qc_status = "fail" if issues else ("warn" if warnings else "pass")
    return {
        "motif_id": motif.get("motif_id", ""),
        "role": motif.get("role", ""),
        "path": str(path.resolve()),
        "program_qc_status": program_qc_status,
        "approved": program_qc_status == "pass",
        "dominant_rgb": dom,
        "palette_distance": round(palette_distance, 1) if palette_distance is not None else None,
        "alpha_quality": alpha_stats,
        "issues": issues,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="在成衣渲染前验证已批准的面料资产。")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--out", required=True, help="质检报告输出路径")
    parser.add_argument("--style-profile", default="", help="style_profile.json 路径（可选，用于色板一致性质检）")
    args = parser.parse_args()

    texture_set_path = Path(args.texture_set)
    payload = load_json(texture_set_path)
    style_profile = load_json(args.style_profile) if args.style_profile and Path(args.style_profile).exists() else {}
    palette = flatten_palette(style_profile)
    base_dir = texture_set_path.parent
    results = [validate_texture(texture, base_dir, palette=palette) for texture in payload.get("textures", [])]
    motif_results = [validate_motif(motif, base_dir, palette=palette) for motif in payload.get("motifs", [])]
    solid_issues = []
    if not payload.get("solids"):
        solid_issues.append({"type": "missing_solids", "message": "建议至少提供一种已批准纯色。"})
    all_issues = []
    all_warnings = []
    for item in results + motif_results:
        all_issues.extend(item.get("issues", []))
        all_warnings.extend(item.get("warnings", []))

    high_issues = [i for i in all_issues if i.get("severity") == "high"]
    base_results = [r for r in results if any(token in str(r.get("role", "")).lower() for token in ("base", "main", "body", "secondary"))]
    cohesion_warnings = []
    if len(base_results) >= 2:
        doms = [tuple(r.get("dominant_rgb", (0, 0, 0))) for r in base_results if r.get("dominant_rgb")]
        max_pair_distance = max((color_distance(a, b) for i, a in enumerate(doms) for b in doms[i + 1:]), default=0)
        if max_pair_distance > 135:
            cohesion_warnings.append({"type": "texture_family_color_split", "severity": "medium", "distance": round(max_pair_distance, 1), "message": "基础/大身面料之间主色差异过大，可能形成跨风格拼贴。"})
    all_warnings.extend(cohesion_warnings)
    program_qc_status = "fail" if high_issues else ("warn" if (all_issues or all_warnings or solid_issues) else "pass")
    approved = program_qc_status == "pass"

    report = {
        "texture_set_id": payload.get("texture_set_id", ""),
        "program_qc_status": program_qc_status,
        "approved": approved,
        "summary": {
            "high_issues": len(high_issues),
            "issues": len(all_issues),
            "warnings": len(all_warnings),
            "solid_issues": len(solid_issues),
        },
        "style_profile": str(Path(args.style_profile).resolve()) if args.style_profile else "",
        "palette_check_enabled": bool(palette),
        "textures": results,
        "motifs": motif_results,
        "solid_issues": solid_issues,
        "cohesion_warnings": cohesion_warnings,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 只有 fail（有 high severity issue）才返回非零退出码
    # warn（只有 medium/low warning）不中断流水线，交给 AI/人工复核
    return 1 if program_qc_status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
