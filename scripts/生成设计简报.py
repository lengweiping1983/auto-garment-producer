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
    parser.add_argument("--theme", required=True, help="主题/参考图像路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--user-prompt", default="", help="用户美术指导或约束")
    parser.add_argument("--garment-type", default="commercial apparel sample", help="服装类型（如已知）")
    parser.add_argument("--season", default="spring/summer", help="商业季节信号")
    args = parser.parse_args()

    theme_path = Path(args.theme)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    palette = extract_palette(theme_path)
    motifs = infer_motifs(args.user_prompt, theme_path.name)
    style_id = f"{theme_path.stem.lower().replace(' ', '_')}_commercial_v1"

    brief = {
        "brief_id": style_id,
        "aesthetic_direction": "商业畅销款打样",
        "garment_type": args.garment_type,
        "target_customer": "寻找可穿、有记忆点印花的主流客户",
        "season": args.season,
        "price_tier_signal": "中端精致",
        "hero_selling_point": motifs[0],
        "theme_image": str(theme_path.resolve()),
        "user_prompt": args.user_prompt,
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
        "art_style": "从主题图衍生的商业成衣印花",
        "palette": {
            "primary": palette[:4],
            "accent": palette[4:7],
            "dark": palette[-2:] if len(palette) >= 2 else palette,
        },
        "motifs": motifs,
        "avoid": brief["avoid"],
        "texture_density": "低-中",
        "contrast": "受控",
    }
    texture_prompts = {
        "style_id": style_id,
        "generation_owner": "external_ai_image_model",
        "prompts": [
            {
                "texture_id": "main",
                "purpose": "可穿大身裁片",
                "prompt": f"无缝可平铺商业成衣图案，低噪 {motifs[0]}，受控色板 {', '.join(palette[:5])}，可穿印花，无动物，无人物，无人脸，无文字，无商标，无水印，无中心构图",
                "negative_prompt": "动物，人物，人脸，文字，商标，水印，中心场景，硬边框，海报构图",
            },
            {
                "texture_id": "secondary",
                "purpose": "协调大副裁片",
                "prompt": f"无缝可平铺协调成衣纹理，微妙辅助图案 {', '.join(motifs[:3])}，低对比度，商业面料印花，无动物，无人物，无人脸，无文字，无商标",
                "negative_prompt": "动物，人物，人脸，文字，商标，水印，中心场景，复杂插画",
            },
            {
                "texture_id": "accent",
                "purpose": "小面板与受控点缀",
                "prompt": f"无缝可平铺小型点缀成衣图案，{', '.join(motifs[:3])}，清晰但不繁杂，无动物，无人物，无人脸，无文字，无商标",
                "negative_prompt": "动物，人物，人脸，文字，商标，水印，大型中心场景",
            },
            {
                "texture_id": "dark",
                "purpose": "饰边、袖口、窄条、打底片",
                "prompt": f"无缝可平铺安静饰边纹理，较深受控色 {', '.join(palette[-3:])}，极低噪，无动物，无人物，无文字，无商标",
                "negative_prompt": "动物，人物，人脸，文字，商标，水印，卖点插画",
            },
        ],
    }
    motif_prompts = {
        "style_id": style_id,
        "generation_owner": "external_ai_image_model",
        "motifs": [
            {
                "motif_id": "hero_motif",
                "purpose": "单一卖点定位，置于一个 hero 裁片",
                "prompt": f"透明 PNG 商业成衣定位图案，简化 {motifs[0]}，平衡负空间，色板 {', '.join(palette[:5])}，无动物，无人物，无人脸，无文字，无商标，无水印",
                "negative_prompt": "复杂背景，完整场景，海报，文字，商标，人脸，水印",
            }
        ],
    }
    asset_generation_manifest = {
        "manifest_id": f"{style_id}_asset_generation",
        "status": "waiting_for_ai_generated_assets",
        "required_assets": [
            {
                "asset_id": "main",
                "asset_type": "texture",
                "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024",
                "prompt_ref": "texture_prompts.json#main",
            },
            {
                "asset_id": "secondary",
                "asset_type": "texture",
                "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024",
                "prompt_ref": "texture_prompts.json#secondary",
            },
            {
                "asset_id": "accent",
                "asset_type": "texture",
                "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024",
                "prompt_ref": "texture_prompts.json#accent",
            },
            {
                "asset_id": "dark",
                "asset_type": "texture",
                "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024",
                "prompt_ref": "texture_prompts.json#dark",
            },
            {
                "asset_id": "hero_motif",
                "asset_type": "motif",
                "output_requirement": "透明 PNG 定位图案，至少 1024px 宽",
                "prompt_ref": "motif_prompts.json#hero_motif",
            },
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
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
