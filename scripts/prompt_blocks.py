#!/usr/bin/env python3
"""Shared prompt fragments for auto-garment production.

Keep long policy text in one place so prompt builders stay compact and consistent.
"""

FRONT_EFFECT_NEGATIVE_EN = (
    "no garment mockup, no front-view clothing render, no fashion model, no mannequin, "
    "no person wearing garment, no on-body render, no T-shirt mockup, no product photo, no lookbook"
)

BOARD_NEGATIVE_EN = (
    "no animals other than intended subjects, no characters, no faces, no people, no text, "
    "no labels, no captions, no titles, no words, no letters, no typography, no logo, no watermark, "
    "no house, no river, no full landscape scene, no scenery, no environment, no background scene, no poster composition, no sticker sheet, "
    "no harsh black outlines, no dense confetti, no neon colors, no muddy dark colors, "
    "no folds, no wrinkles, no draping, no creases, no shadows, no 3D fabric photography, no light variation across surface, "
    "no gradient backgrounds inside individual panels, " + FRONT_EFFECT_NEGATIVE_EN
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
    "main": "seamless tileable low-density tonal leaf repeat pattern on pale ground, faint leaf silhouettes, visible but quiet structure, commercial apparel base fabric, abundant breathing room, no abstract wash, no plain color wash, no blurred background, no empty texture, no scene, no landscape, no text",
    "secondary": "coordinated medium-density pattern on light ground, same palette, no scene, no text",
    "accent_light": "tiny scattered small-scale pattern on light ground, controlled density, no text",
    "accent_mid": "soft geometric or organic lattice on pale ground, same palette, seamless tileable texture, no text",
    "hero_motif_1": "isolated foreground hero motif only, centered subject, transparent PNG cutout, real alpha background, empty transparent pixels around the subject, no background, no background art, no scenery, no garden, no foliage behind subject, no botanical backdrop, no painted wash, no rectangular composition, no full illustration scene, no vignette, no ground shadow, no text",
}

TEXTURE_2X2_POSITIONS_EN = [
    ("Top-left", "main"),
    ("Top-right", "secondary"),
    ("Bottom-left", "accent_light"),
    ("Bottom-right", "accent_mid"),
]


def compact_style_line(style: dict | None) -> str:
    style = style or {}
    return (
        f"{style.get('overall_impression', 'Elegant commercial textile collection')}. "
        f"{style.get('mood', 'Quiet and wearable')}. "
        f"{style.get('medium', 'Watercolor')}. "
        "Low contrast, wearable, cohesive fashion print suite."
    )


def build_texture_2x2_board_prompt_en(panel_prompts: dict, style: dict | None = None) -> str:
    """Build the final English 2x2 texture-board prompt."""
    lines = [
        "Create one square 2x2 commercial textile texture board with thin white gutters. No text anywhere.",
        f"Art direction: {compact_style_line(style)}",
        "All 4 panels are seamless tileable fabric repeats only, arranged as equal square swatches.",
        "All 4 panels must look like one coherent textile family: same palette, same paper grain, same brush language, same saturation range.",
        "Do not mix separate visual worlds such as warm beige line-art mushrooms with green watercolor meadow panels unless the palette and brush style are fully unified.",
        "Every panel must look like a fabric swatch, not a painting, scene, mockup, sticker sheet, or placement motif.",
        "No large figurative subject, complete scene, landscape, scenery, environment, animal, character, mushroom, or flower bouquet as a full-body hero texture.",
    ]
    for label, panel_id in TEXTURE_2X2_POSITIONS_EN:
        prompt = panel_prompts.get(panel_id) or PANEL_DEFAULTS_EN[panel_id]
        lines.append(f"{label}: {prompt}")
    lines.extend([
        "All panels share one palette, fabric texture, brush language, and commercial apparel mood.",
        BOARD_NEGATIVE_EN + ".",
        "All 4 panels must be usable seamless fabric repeats.",
    ])
    return "\n".join(lines)


def build_transparent_hero_prompt_en(hero_prompt: str, style: dict | None = None) -> str:
    """Build the final English single transparent hero motif prompt."""
    prompt = hero_prompt or PANEL_DEFAULTS_EN["hero_motif_1"]
    lines = [
        "Create one isolated foreground apparel placement graphic as a transparent PNG cutout with real alpha background.",
        f"Art direction: {compact_style_line(style)}",
        "The subject must be the user's main desired content only, centered, cleanly separated from the background, with soft but readable edges.",
        prompt,
        "Required output: real transparent alpha pixels around the subject, no background, no plain light box, no colored background box, no filled rectangle, no scenery, no full illustration scene, no sticker sheet, no poster composition, no garment mockup, no model, no text, no logo, no watermark.",
        "Leave balanced empty transparent pixels around the subject so it can be split vertically across left and right front garment pieces.",
    ]
    return "\n".join(lines)


def build_collection_board_prompt_en(panel_prompts: dict, style: dict | None = None) -> str:
    """Compatibility wrapper for callers that still import the old name."""
    return build_texture_2x2_board_prompt_en(panel_prompts, style)
