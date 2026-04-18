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


def generate_prompt_variants(base_prompt: str, count: int = 3) -> list[str]:
    """基于一个基础提示词生成多个措辞变体。

    策略：
    1. 主变体 = 原始提示词
    2. 变体A = 调整密度描述（low noise → very subtle texture 等）
    3. 变体B = 调整媒介/质感描述（watercolor → hand-painted gouache 等）
    """
    if not base_prompt:
        return [""] * count
    variants = [base_prompt]

    # 密度/空间替换词表
    density_swaps = [
        ("low noise", "very subtle texture"),
        ("lots of negative space", "generous breathing room"),
        ("tiny scattered", "small clustered"),
        ("medium density", "light airy pattern"),
        ("abundant negative space", "plenty of quiet ground"),
    ]

    # 媒介/质感替换词表
    texture_swaps = [
        ("watercolor", "hand-painted gouache"),
        ("seamless tileable", "continuous repeat"),
        ("soft fading edges", "gentle blurred boundaries"),
        ("paper grain", "canvas texture"),
    ]

    # 变体A：密度调整
    v1 = base_prompt
    for old, new in density_swaps:
        if old.lower() in v1.lower():
            v1 = v1.replace(old, new)
            break
    if v1 != base_prompt:
        variants.append(v1)

    # 变体B：质感调整
    v2 = base_prompt
    for old, new in texture_swaps:
        if old.lower() in v2.lower():
            v2 = v2.replace(old, new)
            break
    if v2 != base_prompt and v2 not in variants:
        variants.append(v2)

    # 如果变体不足，用更保守的改写补充
    while len(variants) < count:
        # 简单改写：调整形容词强度
        extra = base_prompt.replace("very ", "extremely ").replace("delicate ", "fine ")
        if extra not in variants:
            variants.append(extra)
        else:
            variants.append(base_prompt)

    return variants[:count]


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

    def _make_prompt(texture_id: str, purpose: str, prompt_text: str, panel: str = "", role: str = "") -> dict:
        item = {
            "texture_id": texture_id,
            "purpose": purpose,
            "prompt": prompt_text,
            "negative_prompt": "animals, characters, faces, people, text, labels, captions, titles, typography, words, letters, logo, watermark, house, full landscape, poster, sticker, harsh black outline, dense confetti, neon colors, muddy colors",
        }
        if panel:
            item["panel"] = panel
        if role:
            item["role"] = role
        return item

    # 构建 9 面板提示词（3×3 看板全动态）
    # 优先使用 generated_prompts（LLM 路径），否则 fallback 到基于 motifs 的模板
    gp = generated_prompts or {}
    motif_str = ", ".join(motifs[:3]) if motifs else "theme elements"
    medium = style_details.get("medium", "watercolor") if style_details else "watercolor"
    mood = style_details.get("mood", "quiet and elegant") if style_details else "quiet and elegant"

    # Row 1 — Base textures
    main_prompt = gp.get("main", f"seamless tileable commercial textile texture, pale ground with very faint pattern inspired by {motif_str}, extremely low noise, abundant negative space, {medium} paper grain, no text")
    secondary_prompt = gp.get("secondary", f"seamless tileable coordinating textile texture, soft light ground with delicate pattern inspired by {motif_str}, medium density but airy, same {medium} brush style, no text")
    dark_prompt = gp.get("dark", f"seamless tileable quiet dark trim texture, deep ground with tiny subtle texture, very low noise, dark-quiet and minimal, {medium} grain, no text")

    # Row 2 — Mid-scale accent textures（全部走 gp.get，无硬编码）
    accent_prompt = gp.get("accent", gp.get("accent_light", f"seamless tileable small-scale accent pattern, tiny scattered elements inspired by {motif_str}, very small scale repeating, charming but controlled density, no text"))
    accent_mid_prompt = gp.get("accent_mid", f"seamless tileable soft geometric or organic lattice on pale ground, same {mood} palette, {medium} hand-painted, low noise, seamless tileable texture for secondary panels, no text")
    solid_quiet_prompt = gp.get("solid_quiet", f"quiet warm solid with only subtle {medium} paper grain, no pattern, calm and minimal, seamless tileable solid texture for quiet trim or lining, {mood}, no text")

    # Row 3 — Placement motifs (plain backgrounds for background removal)（全部走 gp.get，无硬编码）
    hero_motif_1_prompt = gp.get("hero_motif", gp.get("hero_motif_1", f"a single elegant main subject centered, plain light background, soft fading edges, balanced negative space, {medium} hand-painted, designed as placement print element, no text"))
    hero_motif_2_prompt = gp.get("hero_motif_2", f"a secondary accent subject, centered, plain light background, refined {medium} brushwork, designed as placement accent motif, {mood}, no text")
    trim_motif_prompt = gp.get("trim_motif", f"a small delicate decorative accent, minimal composition, plain warm background, designed as trim detail placement element, {medium} style, no text")

    def _inject_palette_constraints(prompt_text: str, texture_id: str, palette: dict) -> str:
        """为提示词追加具体的 hex 颜色硬约束，减少 AI 生成时的颜色偏差。"""
        if not palette:
            return prompt_text
        primary = palette.get("primary", [])
        secondary = palette.get("secondary", [])
        accent = palette.get("accent", [])
        dark = palette.get("dark", [])

        constraints = []
        if texture_id == "main" and primary:
            constraints.append(f"ground color must be exactly {primary[0]}")
        elif texture_id == "secondary" and secondary:
            constraints.append(f"light ground and pattern tones must stay within {secondary[0]} family, no warm cast")
        elif texture_id == "dark_base" and dark:
            constraints.append(f"deep ground color must be exactly {dark[0]} or darker, no brown, no green cast")
        elif texture_id == "accent_light" and (accent or primary):
            c = accent[0] if accent else primary[0]
            constraints.append(f"scattered accent elements must use {c} tones only")
        elif texture_id == "accent_mid" and secondary:
            constraints.append(f"lattice lines must use {secondary[0]} tones, pale ground stays within {primary[0] if primary else 'light'} family")
        elif texture_id == "solid_quiet" and primary:
            constraints.append(f"solid surface color must be exactly {primary[0]} with only subtle texture, no pattern")
        elif texture_id == "hero_motif_1" and primary:
            bg = primary[0] if primary else "#ffffff"
            fg = accent[0] if accent else (secondary[0] if secondary else bg)
            constraints.append(f"plain background exactly {bg}, subject painted in {fg} tones, soft fading edges")
        elif texture_id == "hero_motif_2" and primary:
            bg = primary[0] if primary else "#ffffff"
            fg = secondary[0] if secondary else (accent[0] if accent else bg)
            constraints.append(f"plain background exactly {bg}, accent subject in {fg} tones")
        elif texture_id == "trim_motif" and (accent or secondary):
            c = accent[0] if accent else secondary[0]
            constraints.append(f"minimal decorative accent in {c} tones on plain warm background")

        if constraints:
            return f"{prompt_text}, color constraint: {', '.join(constraints)}"
        return prompt_text

    # 构建 9 面板提示词并注入 palette 约束
    palette = style_profile.get("palette", {}) if style_details else {}
    _prompts = [
        ("main", "可穿大身裁片", main_prompt, "row1_left", "base_texture"),
        ("secondary", "协调大副裁片", secondary_prompt, "row1_center", "base_texture"),
        ("dark_base", "深色饰边/打底片", dark_prompt, "row1_right", "base_texture"),
        ("accent_light", "小面板与受控点缀", accent_prompt, "row2_left", "accent_texture"),
        ("accent_mid", "中格几何/有机格子", accent_mid_prompt, "row2_center", "accent_texture"),
        ("solid_quiet", "安静纯色/衬里", solid_quiet_prompt, "row2_right", "solid_texture"),
        ("hero_motif_1", "主卖点定位图案", hero_motif_1_prompt, "row3_left", "placement_motif"),
        ("hero_motif_2", "次卖点定位图案", hero_motif_2_prompt, "row3_center", "placement_motif"),
        ("trim_motif", "小型装饰点缀", trim_motif_prompt, "row3_right", "placement_motif"),
    ]
    prompts = [_make_prompt(tid, purpose, _inject_palette_constraints(ptext, tid, palette), panel=panel, role=role)
               for tid, purpose, ptext, panel, role in _prompts]

    texture_prompts = {
        "style_id": style_id,
        "generation_owner": "external_ai_image_model",
        "prompts": prompts,
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
            {"asset_id": "dark_base", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#dark_base"},
            {"asset_id": "accent_light", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#accent_light"},
            {"asset_id": "accent_mid", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#accent_mid"},
            {"asset_id": "solid_quiet", "asset_type": "texture", "output_requirement": "无缝可平铺正方形 PNG，至少 1024×1024", "prompt_ref": "texture_prompts.json#solid_quiet"},
            {"asset_id": "hero_motif_1", "asset_type": "motif", "output_requirement": "透明 PNG 定位图案，至少 1024px 宽", "prompt_ref": "texture_prompts.json#hero_motif_1"},
            {"asset_id": "hero_motif_2", "asset_type": "motif", "output_requirement": "透明 PNG 定位图案，至少 1024px 宽", "prompt_ref": "texture_prompts.json#hero_motif_2"},
            {"asset_id": "trim_motif", "asset_type": "motif", "output_requirement": "透明 PNG 定位图案，至少 1024px 宽", "prompt_ref": "texture_prompts.json#trim_motif"},
        ],
        "notes": [
            "Codex 仅提供提示词与成衣美术指导。",
            "使用 AI 图像生成器或设计师创建资产，然后将已批准文件列入面料组合.json。",
            "在面料组合.json 引用真实已批准生成资产之前，不要渲染最终裁片。",
        ],
    }

    # 生成每面板的 3 候选变体
    candidates = {"panels": []}
    for p in texture_prompts.get("prompts", []):
        variants = generate_prompt_variants(p.get("prompt", ""), count=3)
        candidates["panels"].append({
            "panel_id": p.get("texture_id", ""),
            "position": p.get("panel", ""),
            "role": p.get("role", ""),
            "variants": variants,
        })

    outputs = {
        "商业设计简报": str(write_json(out_dir / "commercial_design_brief.json", brief).resolve()),
        "风格档案": str(write_json(out_dir / "style_profile.json", style_profile).resolve()),
        "纹理提示词": str(write_json(out_dir / "texture_prompts.json", texture_prompts).resolve()),
        "图案提示词": str(write_json(out_dir / "motif_prompts.json", motif_prompts).resolve()),
        "候选提示词": str(write_json(out_dir / "collection_prompt_candidates.json", candidates).resolve()),
        "资产生成清单": str(write_json(out_dir / "asset_generation_manifest.json", asset_generation_manifest).resolve()),
    }
    return outputs


if __name__ == "__main__":
    raise SystemExit(main())
