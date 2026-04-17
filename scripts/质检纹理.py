#!/usr/bin/env python3
"""
在成衣渲染前验证已批准的面料资产。
"""
import argparse
import json
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
    pixels = list(gray.getdata())

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


def validate_texture(texture: dict, base_dir: Path) -> dict:
    """验证单个面料资产。"""
    issues = []
    path = Path(texture.get("path", ""))
    if not path.is_absolute():
        path = base_dir / path
    if not path.exists():
        return {
            "texture_id": texture.get("texture_id", ""),
            "approved": False,
            "issues": [{"type": "missing_file", "message": f"文件不存在: {path}"}],
        }
    try:
        with Image.open(path) as img:
            width, height = img.size
            tileable = edge_similarity_score(img)
            variation = variation_score(img)
    except Exception as exc:
        return {
            "texture_id": texture.get("texture_id", ""),
            "approved": False,
            "issues": [{"type": "open_failed", "message": f"无法打开: {exc}"}],
        }
    text_score = text_residual_score(img)
    if width < 512 or height < 512:
        issues.append({"type": "too_small", "message": f"尺寸过小: {width}x{height}"})
    if tileable < 0.55:
        issues.append({"type": "low_tileable_score", "message": f"可平铺分数过低: {tileable:.3f}"})
    # 纯色面板允许低变化度
    role = texture.get("role", "")
    if variation < 0.06 and "solid" not in role.lower():
        issues.append({"type": "low_variation", "message": f"变化度过低: {variation:.3f}"})
    if text_score > 0.50:
        issues.append({"type": "text_residual_detected", "message": f"检测到文字残留特征: {text_score:.3f}，看板可能含有文字标签"})
    if not texture.get("approved", False):
        issues.append({"type": "not_user_approved", "message": "texture.approved 不为 true"})
    approved = not issues
    return {
        "texture_id": texture.get("texture_id", ""),
        "role": texture.get("role", ""),
        "path": str(path.resolve()),
        "approved": approved,
        "tileable_score": round(tileable, 3),
        "variation_score": round(variation, 3),
        "text_residual_score": round(text_score, 3),
        "issues": issues,
    }


def validate_motif(motif: dict, base_dir: Path) -> dict:
    """验证单个图案资产。"""
    issues = []
    path = Path(motif.get("path", ""))
    if not path.is_absolute():
        path = base_dir / path
    if not path.exists():
        return {
            "motif_id": motif.get("motif_id", ""),
            "approved": False,
            "issues": [{"type": "missing_file", "message": f"文件不存在: {path}"}],
        }
    try:
        with Image.open(path).convert("RGBA") as img:
            width, height = img.size
            alpha = img.getchannel("A")
            alpha_min, alpha_max = alpha.getextrema()
    except Exception as exc:
        return {
            "motif_id": motif.get("motif_id", ""),
            "approved": False,
            "issues": [{"type": "open_failed", "message": f"无法打开: {exc}"}],
        }
    if width < 128 or height < 128:
        issues.append({"type": "too_small", "message": f"尺寸过小: {width}x{height}"})
    if alpha_min == 255 and alpha_max == 255:
        issues.append({"type": "missing_transparency", "message": "定位图案资产通常应有透明背景"})
    if not motif.get("approved", False):
        issues.append({"type": "not_user_approved", "message": "motif.approved 不为 true"})
    return {
        "motif_id": motif.get("motif_id", ""),
        "role": motif.get("role", ""),
        "path": str(path.resolve()),
        "approved": not issues,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="在成衣渲染前验证已批准的面料资产。")
    parser.add_argument("--texture-set", required=True, help="面料组合 JSON 路径")
    parser.add_argument("--out", required=True, help="质检报告输出路径")
    args = parser.parse_args()

    texture_set_path = Path(args.texture_set)
    payload = load_json(texture_set_path)
    base_dir = texture_set_path.parent
    results = [validate_texture(texture, base_dir) for texture in payload.get("textures", [])]
    motif_results = [validate_motif(motif, base_dir) for motif in payload.get("motifs", [])]
    solid_issues = []
    if not payload.get("solids"):
        solid_issues.append({"type": "missing_solids", "message": "建议至少提供一种已批准纯色。"})
    approved = all(item["approved"] for item in results) and all(item["approved"] for item in motif_results) and not solid_issues
    report = {
        "texture_set_id": payload.get("texture_set_id", ""),
        "approved": approved,
        "textures": results,
        "motifs": motif_results,
        "solid_issues": solid_issues,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if approved else 1


if __name__ == "__main__":
    raise SystemExit(main())
