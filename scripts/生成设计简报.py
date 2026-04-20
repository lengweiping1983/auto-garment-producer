#!/usr/bin/env python3
"""
从主题图创建商业成衣设计简报、风格档案和纹理提示词。
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

# 导入提示词过滤器
sys.path.insert(0, str(Path(__file__).parent))
try:
    from prompt_sanitizer import sanitize_prompt, sanitize_prompts_in_dict
except Exception:
    # 导入失败时使用空过滤函数。
    def sanitize_prompt(text, domain="generic"):
        return text
    def sanitize_prompts_in_dict(data, keys=("prompt",), domain="generic"):
        return data

from prompt_blocks import (
    build_texture_2x2_board_prompt_en,
    FRONT_EFFECT_NEGATIVE_EN,
    TEXTURE_NEGATIVE_EN,
    HERO_NEGATIVE_EN,
    build_family_contract_text,
)


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
        ("animal", "仅在明确作为图案元素时才使用动物元素"),
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
    parser.add_argument("--garment-type", required=True, help="服装类型（如'儿童外套套装'、'女装连衣裙'），必填")
    parser.add_argument("--season", default="spring/summer", help="商业季节信号")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 如果提供了 visual_elements.json，基于视觉分析结果生成所有文件。
    if args.visual_elements:
        ve_path = Path(args.visual_elements)
        if ve_path.exists():
            print(f"[生成设计简报] 使用视觉元素分析: {ve_path}")
            visual = json.loads(ve_path.read_text(encoding="utf-8"))
            outputs = _generate_from_visual_elements(visual, ve_path, out_dir, args.user_prompt, args.garment_type, args.season)
            print(json.dumps(outputs, ensure_ascii=False, indent=2))
            return 0
        else:
            print(f"[警告] visual_elements 文件不存在: {ve_path}，改用图像分析模式。")

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


def _collect_hero_subjects(visual_dict: dict) -> list[tuple[int, float, str, str]]:
    """Return S/A grade hero-suitable subjects sorted by visual importance."""
    dominant = visual_dict.get("dominant_objects", [])
    candidates = []
    for obj in dominant:
        grade = obj.get("grade", "").upper()
        usage = obj.get("suggested_usage", "")
        if grade in ("S", "A") and usage == "hero_motif":
            name = obj.get("name", "").strip()
            desc = obj.get("description", "").strip()
            form_type = obj.get("geometry", {}).get("form_type", "").strip()
            geo = obj.get("geometry", {})
            ratio = geo.get("canvas_ratio", 0)
            grade_score = 2 if grade == "S" else 1
            if desc:
                label = f"{name}: {desc}" if name else desc
                if form_type:
                    label = f"{label} Form type: {form_type}."
                candidates.append((grade_score, ratio, label, name or desc))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates


def _enrich_hero_prompt_from_dominant_objects(visual_dict: dict, base_hero_prompt: str) -> str:
    """Extract all S/A grade hero-suitable subjects and prepend a composite brief."""
    candidates = _collect_hero_subjects(visual_dict)
    if not candidates:
        return base_hero_prompt
    subject_detail = "; ".join(c[2] for c in candidates)
    composition_rule = (
        "Composite hero requirement: include every listed hero subject in one cohesive foreground apparel placement graphic. "
        "Preserve the recognizable relative roles from the reference image, keep each subject complete and readable, "
        "arrange them as a balanced compact group suitable for splitting across left and right front garment pieces, "
        "and simplify only minor background clutter. Do not omit any listed hero subject; do not reduce the hero graphic to only one subject. "
        "If the base prompt gives extra detail for one subject, use that as detail for that subject while still drawing the full composite group."
    )
    # Prepend the visual-fact paragraph so Neo AI sees "what to draw" first
    return f"Subject visual facts from reference image: {subject_detail}. {composition_rule} {base_hero_prompt}"


def _generate_from_visual_elements(visual: dict, ve_path: Path, out_dir: Path, user_prompt: str, garment_type: str, season: str) -> dict:
    """基于 visual_elements.json 生成所有设计文件。"""
    palette = visual.get("palette", {})
    style = visual.get("style", {})
    prompts = visual.get("generated_prompts", {})
    motifs = visual.get("dominant_objects", []) + visual.get("supporting_elements", [])
    motif_labels = [m["name"] for m in motifs if "name" in m]
    style_id = f"{ve_path.stem.lower().replace(' ', '_')}_commercial_v1"
    theme_strategy = visual.get("theme_to_piece_strategy", {})
    hero_subjects = _collect_hero_subjects(visual)
    if isinstance(theme_strategy, dict) and len(hero_subjects) > 1:
        theme_strategy = dict(theme_strategy)
        subject_names = [c[3] for c in hero_subjects]
        theme_strategy["hero_motif"] = (
            "组合主卖点定位图案：保留并组合 "
            + "、".join(subject_names)
            + "，形成一个可前片定位的透明主图，不做三选一。"
        )

    # Enrich hero_motif_1 with dominant_objects descriptions before passing down
    raw_hero = prompts.get("hero_motif_1", "")
    if raw_hero:
        prompts["hero_motif_1"] = _enrich_hero_prompt_from_dominant_objects(visual, raw_hero)

    return _generate_outputs(
        style_id=style_id,
        palette=palette,
        motifs=motif_labels,
        theme_image=str(ve_path.resolve()),
        user_prompt=user_prompt,
        garment_type=garment_type,
        season=season,
        out_dir=out_dir,
        theme_images=visual.get("source_images", []),
        style_details=style,
        generated_prompts=prompts,
        fabric_hints=visual.get("fabric_hints", {}),
        fusion_strategy=visual.get("fusion_strategy", {}),
        theme_to_piece_strategy=theme_strategy,
        reference_fidelity=visual.get("reference_fidelity", {}),
        design_dna=visual.get("design_dna", {}),
        single_texture_derivation=visual.get("single_texture_derivation", {}),
        hero_texture_fusion_plan=visual.get("hero_texture_fusion_plan", ""),
        texture_micro_structure=visual.get("texture_micro_structure", {}),
        hero_edge_contract=visual.get("hero_edge_contract", {}),
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
    theme_images: list | None = None,
    style_details: dict = None,
    generated_prompts: dict = None,
    fabric_hints: dict = None,
    fusion_strategy: dict = None,
    theme_to_piece_strategy: dict = None,
    reference_fidelity: dict = None,
    design_dna: dict = None,
    single_texture_derivation: dict = None,
    hero_texture_fusion_plan: str = "",
    texture_micro_structure: dict = None,
    hero_edge_contract: dict = None,
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

    hero_strategy = ""
    if isinstance(theme_to_piece_strategy, dict):
        hero_strategy = str(theme_to_piece_strategy.get("hero_motif", "")).strip()
    if hero_strategy:
        hero_selling_point = hero_strategy
    elif len(motifs) > 1:
        hero_selling_point = "组合主题主图：" + "、".join(motifs[:4])
    else:
        hero_selling_point = motifs[0] if motifs else "主题核心元素"

    # 从 visual_elements 读取面料工艺推断
    fabric_hints = fabric_hints or {}
    theme_images = theme_images or []
    fusion_strategy = fusion_strategy or {}
    theme_to_piece_strategy = theme_to_piece_strategy or {}
    reference_fidelity = reference_fidelity or {}
    design_dna = design_dna or {}
    single_texture_derivation = single_texture_derivation or {}
    texture_micro_structure = texture_micro_structure or {}
    hero_edge_contract = hero_edge_contract or {}
    if not theme_to_piece_strategy:
        theme_to_piece_strategy = {
            "base_atmosphere": "大身只继承主题色彩、笔触和氛围，保持低噪可穿，不直接复制主体或完整场景。",
            "hero_motif": f"将核心主对象组合成一个清晰定位图案：{hero_selling_point}",
            "accent_details": "辅助元素只用于小面积点缀、局部细节或 secondary/accent 面板。",
            "quiet_zones": "袖片、后片、领口、下摆、窄条保持安静，优先低噪底纹或纯色。",
            "do_not_use_as_full_body_texture": [m for m in motifs[:4] if m],
        }
    has_nap = fabric_hints.get("has_nap", False) if isinstance(fabric_hints, dict) else False
    nap_direction = fabric_hints.get("nap_direction", "") if isinstance(fabric_hints, dict) else ""
    nap_confidence = fabric_hints.get("nap_confidence", 0.0) if isinstance(fabric_hints, dict) else 0.0

    # 强制兜底：has_nap=true 时 nap_direction 不能为空
    if has_nap and not nap_direction:
        nap_direction = "vertical"
        print("[警告] visual_elements 中 has_nap=true 但 nap_direction 为空，已设为 'vertical'。请检查视觉分析输出。")

    brief = {
        "brief_id": style_id,
        "aesthetic_direction": style_details.get("mood", "商业畅销款打样") if style_details else "商业畅销款打样",
        "garment_type": garment_type,
        "target_customer": "寻找可穿、有记忆点印花的主流客户",
        "season": season,
        "price_tier_signal": "中端精致",
        "hero_selling_point": hero_selling_point,
        "fabric": {
            "has_nap": has_nap,
            "nap_direction": nap_direction,
            "nap_confidence": nap_confidence,
            "notes": fabric_hints.get("reason", "") if isinstance(fabric_hints, dict) else "",
        },
        "theme_image": theme_image,
        "theme_images": theme_images,
        "fusion_strategy": fusion_strategy,
        "theme_to_piece_strategy": theme_to_piece_strategy,
        "reference_fidelity": reference_fidelity,
        "design_dna": design_dna,
        "single_texture_derivation": single_texture_derivation,
        "hero_texture_fusion_plan": hero_texture_fusion_plan,
        "user_prompt": user_prompt,
        "wearability_notes": [
            "保留一个明确的组合卖点概念",
            "大面板使用低噪纹理",
            "饰边和窄条保持安静",
            "图案是简化的成衣定位，而非故事裁剪",
        ],
        "avoid": [
            "直接叙事裁剪",
            "人脸",
            "文字",
            "商标",
            "水印",
            "过度堆砌袖口",
            "均匀密度全身填充",
            "正面成衣效果图",
            "模特上身图",
            "服装mockup",
        ],
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
        "fusion_strategy": fusion_strategy,
        "theme_to_piece_strategy": theme_to_piece_strategy,
        "reference_fidelity": reference_fidelity,
        "design_dna": design_dna,
        "single_texture_derivation": single_texture_derivation,
        "hero_texture_fusion_plan": hero_texture_fusion_plan,
    }

    # 构建家族契约文本，供后续注入每张单纹理提示词
    family_contract = build_family_contract_text(
        style=style_details,
        palette=palette if isinstance(palette, dict) else style_profile.get("palette", {}),
        design_dna=design_dna,
    )

    def _make_prompt(texture_id: str, purpose: str, prompt_text: str, panel: str = "", role: str = "") -> dict:
        if role == "placement_motif":
            negative_prompt = HERO_NEGATIVE_EN
        else:
            negative_prompt = TEXTURE_NEGATIVE_EN
        item = {
            "texture_id": texture_id,
            "purpose": purpose,
            "prompt": prompt_text,
            "negative_prompt": negative_prompt,
        }
        if panel:
            item["panel"] = panel
        if role:
            item["role"] = role
        return item

    # 构建 4 条提示词：3 条独立单纹理提示词 + 1 条独立透明主图提示词。
    # 优先使用 generated_prompts；否则基于 motifs 生成模板提示词。
    gp = generated_prompts or {}
    motif_str = ", ".join(motifs[:3]) if motifs else "theme elements"
    medium = style_details.get("medium", "watercolor") if style_details else "watercolor"
    mood = style_details.get("mood", "quiet and elegant") if style_details else "quiet and elegant"

    # Row 1 — Base textures
    base_guard = (
        "commercial apparel repeat, only atmosphere and color from the theme, no large figurative subject, "
        "no mushroom or animal as full-body hero, no complete scene, no landscape, no scenery, no environment, no poster composition, cohesive with all other panels, "
        "must contain concrete visible repeat elements, no abstract wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only"
    )
    main_prompt = gp.get("main", f"seamless tileable visible repeat pattern on pale ground, concrete small botanical or geometric motifs inspired by {motif_str}, stable low-to-medium density, clear repeated elements, commercial apparel base fabric, abundant breathing room, same {medium} brush style, no abstract wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only, no blurred background, no figurative subject, no flower bouquet, no landscape scene, no environment, no scenery, {base_guard}, no text")
    secondary_prompt = gp.get("secondary", f"seamless tileable coordinating visible repeat pattern, soft light ground with concrete small motifs, lattice, linework, leaves, dots, or controlled geometric elements inspired by {motif_str}, medium density but airy, same {medium} brush style, stable repeat structure, no standalone scene, no environment, no abstract wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only, {base_guard}, no text")

    # Accent texture（全部走 gp.get，无硬编码）
    accent_prompt = gp.get("accent_light", gp.get("accent", f"seamless tileable small-scale accent pattern, tiny scattered elements inspired by {motif_str}, very small scale repeating, charming but controlled density, same palette and brush as main panel, no standalone scene, no text"))

    def _force_transparent_motif_prompt(prompt_text: str, motif_id: str = "") -> str:
        required = (
            "isolated foreground motif only, transparent PNG cutout, real alpha background, "
            "empty transparent pixels around the subject, no background, no background art, "
            "no plain light background, no plain warm background, no colored background box, "
            "no filled rectangular background, no checkerboard transparency preview, no fake transparency grid, "
            "no scenery, no semi-transparent full-image patch"
        )
        hero_required = (
            "hero_motif_1 must preserve and recreate the primary subject from the user's reference image as much as possible, "
            "people, faces, characters, animals, products, icons, objects, or logos are allowed if they are the user's main image content, "
            "keep the recognizable silhouette, color identity, pose, proportions, and key visual details, "
            "hero_motif_1 must be foreground subject only, no scene, no garden, "
            "no meadow, no landscape, no environment, no foliage behind subject, "
            "no botanical backdrop, no painted wash behind subject, no vignette, "
            "no rectangular composition, no full illustration scene, no checkerboard transparency preview, "
            "no fake transparency grid, no ground shadow"
        )
        text = prompt_text.strip()
        for old in (
            "plain light background",
            "plain warm background",
            "removable plain background",
            "removable plain backgrounds",
            "suitable for background removal",
            "full illustration scene",
            "rectangular composition",
            "botanical backdrop",
            "foliage behind subject",
            "painted wash behind subject",
            "garden background",
            "background art",
        ):
            text = text.replace(old, "transparent alpha background")
        text = text.replace("as as possible", "as much as possible")
        lower = text.lower()
        suffix = required
        if motif_id == "hero_motif_1":
            suffix = f"{required}, {hero_required}"
        if "transparent png cutout" in lower and "real alpha background" in lower:
            if motif_id == "hero_motif_1":
                return f"{text}, no colored background box, no semi-transparent full-image patch, {hero_required}"
            return f"{text}, isolated foreground motif only, empty transparent pixels around the subject, no colored background box, no scenery, no semi-transparent full-image patch"
        return f"{text}, {suffix}"

    # Independent hero motif must be generated as a transparent cutout.
    motif_guard = "isolated foreground motif only, transparent PNG cutout, real alpha background, no background, no checkerboard transparency preview, no fake transparency grid, no plain-color box, no filled rectangular background, no scenery, no semi-transparent full-image patch"
    hero_source_guard = "preserve and recreate the primary subject from the user's reference image as much as possible, people, faces, characters, animals, products, icons, objects, or logos are allowed if they are the user's main image content, keep the recognizable silhouette, color identity, pose, proportions, and key visual details, complete uncropped subject, full head and hair visible, generous transparent margin above and around the subject"
    hero_guard = f"{hero_source_guard}, {motif_guard}, no garden, no meadow, no landscape, no environment, no foliage behind subject, no botanical backdrop, no painted wash behind subject, no rectangular composition, no full illustration scene, no vignette, no ground shadow"
    hero_motif_1_prompt = _force_transparent_motif_prompt(gp.get("hero_motif_1", gp.get("hero_motif", f"isolated foreground hero motif only, centered subject, transparent PNG cutout with real alpha background, {hero_source_guard}, empty transparent pixels around the subject, soft clean edges, balanced negative space, {medium} hand-painted placement print element, {hero_guard}, no text")), "hero_motif_1")

    # 融入 hero_edge_contract 的精确约束
    if hero_edge_contract:
        min_margin = hero_edge_contract.get("min_margin_ratio", 0.30)
        fade = hero_edge_contract.get("edge_fade_pixels", "")
        alpha_behavior = hero_edge_contract.get("required_alpha_behavior", "")
        forbidden = hero_edge_contract.get("forbidden_alpha_patterns", [])
        edge_bits = []
        edge_bits.append(f"minimum {int(min_margin * 100)}% transparent margin around subject")
        if fade:
            edge_bits.append(f"edge fade: {fade}")
        if alpha_behavior:
            edge_bits.append(f"alpha behavior: {alpha_behavior}")
        if forbidden:
            edge_bits.append(f"forbidden: {', '.join(forbidden)}")
        hero_motif_1_prompt = f"{hero_motif_1_prompt}, edge contract: {', '.join(edge_bits)}"

    def _inject_micro_structure(prompt_text: str, texture_id: str, micro: dict) -> str:
        """将微观结构参数注入提示词，减少 AI 对密度/尺度的自由发挥。"""
        if not micro or texture_id not in micro:
            return prompt_text
        info = micro.get(texture_id, {})
        parts = []
        scale = info.get("motif_scale_relative", "")
        if scale:
            parts.append(f"motif scale: {scale}")
        count = info.get("motif_count_per_tile", "")
        if count:
            parts.append(f"density: {count}")
        ns = info.get("negative_space_ratio", "")
        if ns:
            parts.append(f"negative space: {ns}")
        desc = info.get("repeat_unit_description", "")
        if desc:
            parts.append(f"repeat unit: {desc}")
        mix = info.get("element_type_mix", {})
        if mix:
            mix_str = ", ".join(f"{k} {int(v*100)}%" for k, v in mix.items())
            parts.append(f"element mix: {mix_str}")
        if not parts:
            return prompt_text
        return f"{prompt_text}, micro-structure contract: {', '.join(parts)}"

    def _inject_palette_constraints(prompt_text: str, texture_id: str, palette: dict) -> str:
        """为提示词追加具体的 hex 颜色硬约束，减少 AI 生成时的颜色偏差。"""
        if not palette:
            return prompt_text
        primary = palette.get("primary", [])
        secondary = palette.get("secondary", [])
        accent = palette.get("accent", [])

        constraints = []
        if texture_id == "main" and primary:
            constraints.append(f"ground color must be exactly {primary[0]}, keep a visible repeat pattern with concrete small motifs, no abstract wash, no plain color wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only, no blurred background, no figurative elements, no scene, no landscape")
        elif texture_id == "secondary" and secondary:
            constraints.append(f"light ground and pattern tones must stay within {secondary[0]} family, keep a visible repeat pattern with concrete small motifs or lattice, no abstract wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only, no warm cast, no scene")
        elif texture_id == "accent_light" and (accent or primary):
            c = accent[0] if accent else primary[0]
            constraints.append(f"scattered accent elements must use {c} tones only")
        elif texture_id == "hero_motif_1" and primary:
            bg = primary[0] if primary else "#ffffff"
            fg = accent[0] if accent else (secondary[0] if secondary else bg)
            constraints.append(f"transparent alpha background only, preserve the user's main reference subject as much as possible, isolated foreground subject painted in {fg} tones while keeping recognizable source-image silhouette and key details, complete uncropped subject with full head and hair visible, empty transparent pixels above and around the subject, soft fading edges, no checkerboard transparency preview, no fake transparency grid, no colored background box, no garden, no foliage behind subject, no botanical backdrop, no rectangular composition, no full illustration scene")

        if constraints:
            return f"{prompt_text}, color constraint: {', '.join(constraints)}"
        return prompt_text

    def _reference_context(texture_id: str, prompt_text: str) -> str:
        dna_bits = []
        if design_dna:
            for key in ("fusion_rule", "linework", "brushwork", "material_feel", "negative_space"):
                value = design_dna.get(key)
                if value:
                    dna_bits.append(f"{key}: {value}")
            motifs_from_dna = design_dna.get("motif_vocabulary")
            if motifs_from_dna:
                dna_bits.append(f"motif vocabulary: {', '.join(str(v) for v in motifs_from_dna[:8])}")
        derivation = ""
        if isinstance(single_texture_derivation, dict):
            derivation = single_texture_derivation.get(texture_id, "")
        context = (
            "Use reference image 1 as the source for palette, brush language, material feel, small supporting motifs, and user intent; "
            "the texture must coordinate organically with hero_motif_1 and must not look pasted from a different artwork. "
        )
        if derivation:
            context += f"Texture derivation from reference image: {derivation}. "
        if dna_bits:
            context += f"Shared design DNA: {'; '.join(dna_bits)}. "
        return f"{context}{prompt_text}"

    # 构建 3 张单纹理 + 独立主图提示词，依次注入微观结构、颜色硬约束、参考上下文
    palette = style_profile.get("palette", {})
    _prompts = [
        ("main", "可穿大身裁片", _reference_context("main", _inject_micro_structure(main_prompt, "main", texture_micro_structure)), "single_texture", "base_texture"),
        ("secondary", "协调大副裁片", _reference_context("secondary", _inject_micro_structure(secondary_prompt, "secondary", texture_micro_structure)), "single_texture", "base_texture"),
        ("accent_light", "小面板与受控点缀", _reference_context("accent_light", _inject_micro_structure(accent_prompt, "accent_light", texture_micro_structure)), "single_texture", "accent_texture"),
        ("hero_motif_1", "AI生成主图透明定位图案", hero_motif_1_prompt, "single_hero", "placement_motif"),
    ]
    prompts = [
        _make_prompt(tid, purpose, sanitize_prompt(_inject_palette_constraints(ptext, tid, palette), domain="fashion"), panel=panel, role=role)
        for tid, purpose, ptext, panel, role in _prompts
    ]

    texture_prompts = {
        "style_id": style_id,
        "generation_owner": "neo_ai",
        "family_contract": family_contract,
        "prompts": prompts,
    }
    # 过滤所有 prompt 中的停用词和禁用词
    texture_prompts = sanitize_prompts_in_dict(texture_prompts, domain="fashion")

    outputs = {
        "商业设计简报": str(write_json(out_dir / "commercial_design_brief.json", brief).resolve()),
        "风格档案": str(write_json(out_dir / "style_profile.json", style_profile).resolve()),
        "纹理提示词": str(write_json(out_dir / "texture_prompts.json", texture_prompts).resolve()),
    }
    return outputs


def _build_collection_prompt_from_prompts(prompts: list[dict], style: dict) -> str:
    """基于提示词列表构造完整的英文 2x2 纹理看板 prompt。
    逻辑与端到端自动化.py 的 _build_collection_prompt_from_visual_elements 类似。"""
    panel_map = {p.get("texture_id", ""): p.get("prompt", "") for p in prompts}
    return build_texture_2x2_board_prompt_en(panel_map, style)


if __name__ == "__main__":
    raise SystemExit(main())
