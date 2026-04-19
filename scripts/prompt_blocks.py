#!/usr/bin/env python3
"""Shared prompt fragments for auto-garment production.

Keep long policy text in one place so prompt builders stay compact and consistent.
"""

FRONT_EFFECT_NEGATIVE_EN = (
    "no garment mockup, no front-view clothing render, no fashion model, no mannequin, "
    "no person wearing garment, no on-body render, no T-shirt mockup, no product photo, no lookbook"
)

FRONT_EFFECT_NEGATIVE_ZH = (
    "不要正面成衣效果图、不要服装mockup、不要模特上身图、不要假人、不要穿在人身上的衣服、"
    "不要T恤产品图、不要商品摄影、不要lookbook；只生成面料九宫格看板、连续纹理小样和干净定位图案"
)

BOARD_NEGATIVE_EN = (
    "no animals other than approved subjects, no characters, no faces, no people, no text, "
    "no labels, no captions, no titles, no words, no letters, no typography, no logo, no watermark, "
    "no house, no river, no full landscape scene, no poster composition, no sticker sheet, "
    "no harsh black outlines, no dense confetti, no neon colors, no muddy dark colors, "
    "no gradient backgrounds inside individual panels, " + FRONT_EFFECT_NEGATIVE_EN
)

BOARD_NEGATIVE_ZH = (
    "不要动物（除非明确批准的主题元素）、不要人物、不要人脸、不要文字、不要商标、不要水印、"
    "不要房屋、不要河流、不要完整风景场景、不要海报构图、不要贴纸页、不要粗黑轮廓、"
    "不要密集纸屑、不要霓虹色、不要浑浊深色、不要单个面板内的渐变背景。" + FRONT_EFFECT_NEGATIVE_ZH
)

STRICT_JSON_ONLY_ZH = "只返回严格 JSON；不要解释文字、不要 markdown 代码块。"

COMMERCIAL_FILL_RULES_ZH = [
    "同 symmetry_group / same_shape_group 的 base 完全一致",
    "1 个 hero overlay；trim 禁用 motif",
    "大身低噪可穿，辅片协调，饰边克制",
    "不得把叙事插画直接切割进裁片",
    "每个裁片给中文 reason",
]

PANEL_DEFAULTS_EN = {
    "main": "pale base with faint pattern, very low noise, lots of negative space, no text",
    "secondary": "coordinated medium-density pattern on light ground, same palette, no text",
    "dark_base": "deep dark ground with very subtle texture, quiet and minimal, no text",
    "accent_light": "tiny scattered small-scale pattern on light ground, controlled density, no text",
    "accent_mid": "soft geometric or organic lattice on pale ground, same palette, seamless tileable texture, no text",
    "solid_quiet": "quiet warm solid with subtle paper grain, no pattern, seamless tileable solid, no text",
    "hero_motif_1": "single elegant main subject centered, plain light background, soft fading edges, no text",
    "hero_motif_2": "secondary accent subject centered, plain light background, refined brushwork, no text",
    "trim_motif": "small delicate decorative accent, minimal composition, plain warm background, no text",
}

PANEL_DEFAULTS_ZH = {
    "main": "淡色底极低噪底纹",
    "secondary": "协调中密度图案",
    "dark_base": "深底微纹理",
    "accent_light": "浅色小图案点缀",
    "accent_mid": "中调几何/有机格子",
    "solid_quiet": "安静纯色/衬里",
    "hero_motif_1": "主卖点定位图案",
    "hero_motif_2": "次卖点定位图案",
    "trim_motif": "小型饰边装饰图案",
}

BOARD_POSITIONS_EN = [
    ("Top-left", "main"),
    ("Top-center", "secondary"),
    ("Top-right", "dark_base"),
    ("Middle-left", "accent_light"),
    ("Middle-center", "accent_mid"),
    ("Middle-right", "solid_quiet"),
    ("Bottom-left", "hero_motif_1"),
    ("Bottom-center", "hero_motif_2"),
    ("Bottom-right", "trim_motif"),
]

BOARD_POSITIONS_ZH = [
    ("左上", "main"),
    ("中上", "secondary"),
    ("右上", "dark_base"),
    ("左中", "accent_light"),
    ("中中", "accent_mid"),
    ("右中", "solid_quiet"),
    ("左下", "hero_motif_1"),
    ("中下", "hero_motif_2"),
    ("右下", "trim_motif"),
]


def compact_style_line(style: dict | None) -> str:
    style = style or {}
    return (
        f"{style.get('overall_impression', 'Elegant commercial textile collection')}. "
        f"{style.get('mood', 'Quiet and wearable')}. "
        f"{style.get('medium', 'Watercolor')}. "
        "Low contrast, wearable, cohesive fashion print suite."
    )


def build_collection_board_prompt_en(panel_prompts: dict, style: dict | None = None) -> str:
    """Build the final English 3x3 collection-board prompt."""
    lines = [
        "Create one square 3x3 commercial textile collection board with thin white gutters. No text anywhere.",
        f"Art direction: {compact_style_line(style)}",
        "Rows: 1 base seamless textures; 2 accent/solid seamless textures; 3 placement motifs on plain light backgrounds.",
        "All 9 panels must look like one coherent textile family: same palette, same paper grain, same brush language, same saturation range.",
        "Do not mix separate visual worlds such as warm beige line-art mushrooms with green watercolor meadow panels unless the palette and brush style are fully unified.",
        "Rows 1-2 are fabric repeats only: no large figurative subject, no complete scene, no animal/character/mushroom/flower bouquet as a full-body hero texture.",
        "Row 3 motifs must be clean placement artwork on removable plain backgrounds, never semi-transparent rectangular patches.",
    ]
    for label, panel_id in BOARD_POSITIONS_EN:
        prompt = panel_prompts.get(panel_id) or PANEL_DEFAULTS_EN[panel_id]
        lines.append(f"{label}: {prompt}")
    lines.extend([
        "All panels share one palette, paper texture, brush language, and commercial apparel mood.",
        BOARD_NEGATIVE_EN + ".",
        "Rows 1-2 must be usable fabric repeats; row 3 must be clean placement motifs suitable for background removal.",
    ])
    return "\n".join(lines)


def build_collection_board_prompt_zh(panel_prompts: dict, style: dict | None = None, direction_note: str = "") -> str:
    """Build a compact Chinese board description for libtv."""
    style = style or {}
    direction = f"设计方向：{direction_note}。" if direction_note else ""
    lines = [
        (
            "生成一张正方形3x3商业面料看板，白色细间隔，无任何文字/标签/标题。"
            f"整体：{style.get('overall_impression', '商业畅销款打样')}，"
            f"{style.get('mood', '优雅安静')}，{style.get('medium', '水彩')}风格。{direction}"
        ),
        "第一行是大身可平铺底纹；第二行是中调点缀/纯色可平铺纹理；第三行是浅色干净背景定位图案。",
        "9个面板必须像同一设计师的同一系列：同色板、同纸纹、同手绘语言、同饱和度范围。",
        "禁止在同一看板中混入明显跨风格资产，例如米底线稿蘑菇与绿色水彩草地并列，除非色板和笔触已完全统一。",
        "第一、二行只能是面料 repeat，不要把动物、角色、蘑菇、花丛、完整场景做成大身主纹理。",
        "第三行定位图案必须便于干净去背景，禁止半透明整张贴片。",
    ]
    for label, panel_id in BOARD_POSITIONS_ZH:
        prompt = panel_prompts.get(panel_id) or PANEL_DEFAULTS_ZH[panel_id]
        lines.append(f"{label}：{prompt}")
    lines.extend([
        "9个面板必须像同一设计师的同一系列：同色板、同纸纹、同手绘语言、同成衣气质。",
        BOARD_NEGATIVE_ZH + "。",
        "第一、二行必须可作为连续面料小样；第三行适合后期去背景。",
    ])
    return "\n".join(lines)


def board_negative_prompt_en() -> str:
    return "animals, characters, faces, people, text, labels, captions, titles, typography, words, letters, logo, watermark, house, full landscape, poster, sticker, harsh black outline, dense confetti, neon colors, muddy colors, " + FRONT_EFFECT_NEGATIVE_EN
