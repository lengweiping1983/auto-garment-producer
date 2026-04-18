#!/usr/bin/env python3
"""
从主题图创建商业成衣设计简报、风格档案、纹理与图案提示词。
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from PIL import Image


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def extract_palette(path: Path, count: int = 8) -> list[str]:
    """从主题图提取主调色板。"""
    with Image.open(path).convert("RGB") as img:
        sample = img.resize((160, 160), Image.Resampling.LANCZOS)
        quantized = sample.quantize(colors=max(count, 4), method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        if hasattr(quantized, "get_flattened_data"):
            used = Counter(quantized.get_flattened_data())
        else:
            used = Counter(quantized.getdata())
        colors = []
        for index, _ in used.most_common(count * 2):
            offset = index * 3
            if offset + 2 >= len(palette):
                continue
            rgb = tuple(palette[offset : offset + 3])
            if max(rgb) - min(rgb) < 8 and sum(rgb) < 45:
                continue
            colors.append(rgb_to_hex(rgb))
            if len(colors) == count:
                break
        return colors


def infer_motifs(user_prompt: str, theme_name: str) -> list[str]:
    """根据用户提示与主题图名称推断图案元素。"""
    text = f"{user_prompt} {theme_name}".lower()
    motifs = []
    candidates = [
        ("rainbow", "受控彩虹弧线"),
        ("flower", "碎花"),
        ("meadow", "柔和草地"),
        ("forest", "叶片纹理"),
        ("river", "柔和水波"),
        ("stream", "柔和水波"),
        ("cottage", "温暖小屋花园氛围"),
        ("animal", "仅在明确批准为图案时才使用动物元素"),
    ]
    for key, label in candidates:
        if key in text and label not in motifs:
            motifs.append(label)
    return motifs or ["主题衍生小图案", "低噪有机纹理"]


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="从主题图创建商业成衣设计简报与提示词合约。")
    parser.add_argument("--theme", default="", help="主题/参考图像路径（与 --visual-elements 二选一）")
    parser.add_argument("--visual-elements", default="", help="视觉元素分析 JSON 路径（由 视觉元素提取.py 生成）。若提供，优先使用此文件，跳过图像分析。")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--user-prompt", default="", help="用户美术指导或约束")
    parser.add_argument("--garment-type", default="commercial apparel sample", help="服装类型（如已知）")
    parser.add_argument("--season", default="spring/summer", help="商业季节信号")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 如果提供了 visual_elements.json，基于子Agent分析结果生成所有文件
    if args.visual_elements:
        ve_path = Path(args.visual_elements)
        if ve_path.exists():
            print(f"[生成设计简报] 使用子Agent视觉元素分析: {ve_path}")
            visual = json.loads(ve_path.read_text(encoding="utf-8"))
            outputs = _generate_from_visual_elements(visual, ve_path, out_dir, args.user_prompt, args.garment_type, args.season)
            print(json.dumps(outputs, ensure_ascii=False, indent=2))
            return 0
        else:
            print(f"[警告] visual_elements 文件不存在: {ve_path}，回退到图像分析模式。")

    theme_path = Path(args.theme) if args.theme else None
    if not theme_path or not theme_path.exists():
        print("错误: 必须提供 --theme 主题图路径或 --visual-elements 视觉元素 JSON 路径。", file=sys.stderr)
        return 1

    palette = extract_palette(theme_path)
    motifs = infer_motifs(args.user_prompt, theme_path.name)
    style_id = f"{theme_path.stem.lower().replace(' ', '_')}_commercial_v1"

    outputs = _generate_outputs(
        style_id=style_id,
        palette=palette,
        motifs=motifs,
        theme_image=str(theme_path.resolve()),
        user_prompt=args.user_prompt,
        garment_type=args.garment_type,
        season=args.season,
        out_dir=out_dir,
    )
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0


def _generate_from_visual_elements(visual: dict, ve_path: Path, out_dir: Path, user_prompt: str, garment_type: str, season: str) -> dict:
    """基于子Agent输出的 visual_elements.json 生成所有设计文件。"""
    palette = visual.get("palette", {})
    style = visual.get("style", {})
    prompts = visual.get("generated_prompts", {})
    motifs = visual.get("dominant_objects", []) + visual.get("supporting_elements", [])
    motif_labels = [m["name"] for m in motifs if "name" in m]
    style_id = f"{ve_path.stem.lower().replace(' ', '_')}_commercial_v1"

    return _generate_outputs(
        style_id=style_id,
        palette=palette,
        motifs=motif_labels,
        theme_image=str(ve_path.resolve()),
        user_prompt=user_prompt,
        garment_type=garment_type,
        season=season,
        out_dir=out_dir,
        style_details=style,
        generated_prompts=prompts,
    )


def _generate_outputs(
    style_id: str,
    palette,
    motifs: list[str],
    theme_image: str,
    user_prompt: str,
    garment_type: str,
    season: str,
    out_dir: Path,
    style_details: dict = None,
    generated_prompts: dict = None,
) -> dict:
    """生成所有设计输出文件的核心逻辑。"""
    # 统一 palette 格式
    if isinstance(palette, dict):
        primary = palette.get("primary", [])
        secondary = palette.get("secondary", [])
        accent = palette.get("accent", [])
        dark = palette.get("dark", [])
        flat_palette = primary + secondary + accent + dark
    else:
        flat_palette = palette if isinstance(palette, list) else []
        primary = flat_palette[:4]
        secondary = flat_palette[4:7]
        accent = flat_palette[7:8]
        dark = flat_palette[-2:] if len(flat_palette) >= 2 else flat_palette

    hero_selling_point = motifs[0] if motifs else "主题核心元素"

    brief = {
        "brief_id": style_id,
        "aesthetic_direction": style_details.get("mood", "商业畅销款打样") if style_details else "商业畅销款打样",
        "garment_type": garment_type,
        "target_customer": "寻找可穿、有记忆点印花的主流客户",
        "season": season,
        "price_tier_signal": "中端精致",
        "hero_selling_point": hero_selling_point,
        "theme_image": theme_image,
        "user_prompt": user_prompt,
        "wearability_notes": [
            "只保留一个明确的卖点概念",
            "大面板使用低噪纹理",
            "饰边和窄条保持安静",
            "图案是简化的成衣定位，而非故事裁剪",
        ],
        "avoid": ["直接叙事裁剪", "人脸", "文字", "商标", "水印", "过度堆砌袖口", "均匀密度全身填充"],
    }

    style_profile = {
        "style_id": style_id,
        "art_style": f"从主题图衍生的商业成衣印花 — {style_details.get('medium', '综合媒介') if style_details else '综合媒介'}",
        "palette": {
            "primary": primary,
            "secondary": secondary,
            "accent": accent,
            "dark": dark,
        },
        "motifs": motifs,
        "avoid": brief["avoid"],
        "texture_density": style_details.get("pattern_density", "低-中") if style_details else "低-中",
        "contrast": "受控",
        "style_details": style_details or {},
    }

    def _make_prompt(texture_id: str, purpose: str, prompt_text: str) -> dict:
        return {
            "texture_id": texture_id,
            "purpose": purpose,
            "prompt": prompt_text,
            "negative_prompt": "animals, characters, faces, people, text, labels, captions, titles, typography, words, letters, logo, watermark, house, full landscape, poster, sticker, harsh black outline, dense confetti, neon colors, muddy colors",
        }

    texture_prompts = {
        "style_id": style_id,
        "generation_owner": "external_ai_image_model",
        "prompts": [
            _make_prompt("main", "可穿大身裁片", generated_prompts.get("main", f"seamless tileable commercial textile texture, low noise, lots of negative space, inspired by {', '.join(motifs[:2])}") if generated_prompts else _make_prompt("main", "可穿大身裁片", f"seamless tileable commercial textile texture, low noise, inspired by {', '.join(motifs[:2])}")),
            _make_prompt("secondary", "协调大副裁片", generated_prompts.get("secondary", f"seamless tileable coordinating textile texture, subtle pattern, low contrast, inspired by {', '.join(motifs[:3])}") if generated_prompts else _make_prompt("secondary", "协调大副裁片", f"seamless tileable coordinating textile texture, subtle pattern, low contrast, inspired by {', '.join(motifs[:3])}")),
            _make_prompt("accent", "小面板与受控点缀", generated_prompts.get("accent", f"seamless tileable small-scale accent pattern, clear but not busy, inspired by {', '.join(motifs[:3])}") if generated_prompts else _make_prompt("accent", "小面板与受控点缀", f"seamless tileable small-scale accent pattern, clear but not busy, inspired by {', '.join(motifs[:3])}")),
            _make_prompt("dark", "饰边、袖口、窄条、打底片", generated_prompts.get("dark", f"seamless tileable quiet dark trim texture, very low noise, deep controlled colors") if generated_prompts else _make_prompt("dark", "饰边、袖口、窄条、打底片", f"seamless tileable quiet dark trim texture, very low noise, deep controlled colors")),
        ],
    }

    motif_prompts = {
        "style_id": style_id,
        "generation_owner": "external_ai_image_model",
        "motifs": [
            {
                "motif_id": "hero_motif",
                "purpose": "单一卖点定位，置于一个 hero 裁片",
                "prompt": generated_prompts.get("hero_motif", f"elegant placement print motif, simplified {hero_selling_point}, balanced negative space, plain light background, soft fading edges, suitable for background removal, no text, no watermark") if generated_prompts else f"elegant placement print motif, simplified {hero_selling_point}, balanced negative space, plain light background, soft fading edges, suitable for background removal, no text, no watermark",
                "negative_prompt": "complex background, full scene, poster, text, logo, watermark, faces, multiple subjects, frame",
            }
        ],
    }

    asset_generation_manifest = {
        "manifest_id": f"{style_id}_asset_generation",
        "status": "waiting_for_ai_generated_assets",
        "required_assets": [
            {"asset_id": "main", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#main"},
            {"asset_id": "secondary", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#secondary"},
            {"asset_id": "accent", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#accent"},
            {"asset_id": "dark", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#dark"},
            {"asset_id": "hero_motif", "asset_type": "motif", "output_requirement": "透明 PNG 定位图案，至少 1024px 宽", "prompt_ref": "motif_prompts.json#hero_motif"},
        ],
        "notes": [
            "Codex 仅提供提示词与成衣美术指导。",
            "使用 AI 图像生成器或设计师创建资产，然后将已批准文件列入面料组合.json。",
            "在面料组合.json 引用真实已批准生成资产之前，不要渲染最终裁片。",
        ],
    }

    outputs = {
        "商业设计简报": str(write_json(out_dir / "commercial_design_brief.json", brief).resolve()),
        "风格档案": str(write_json(out_dir / "style_profile.json", style_profile).resolve()),
        "纹理提示词": str(write_json(out_dir / "texture_prompts.json", texture_prompts).resolve()),
        "图案提示词": str(write_json(out_dir / "motif_prompts.json", motif_prompts).resolve()),
        "资产生成清单": str(write_json(out_dir / "asset_generation_manifest.json", asset_generation_manifest).resolve()),
    }
    return outputs


if __name__ == "__main__":
    raise SystemExit(main())
