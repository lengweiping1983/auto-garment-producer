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
    "dark_base": "perfectly flat dark solid, only microscopic grain, no ribs, no corduroy, no stripes, no folds, no shadows, no 3D fabric photography, uniform surface, textile swatch, flat lay, no forest, no foliage photo, no camouflage, no atmospheric scene, no moody landscape, no text",
    "accent_light": "tiny scattered small-scale pattern on light ground, controlled density, no text",
    "accent_mid": "soft geometric or organic lattice on pale ground, same palette, seamless tileable texture, no text",
    "solid_quiet": "perfectly flat uniform solid, only subtle microscopic weave texture on very close inspection, no visible pattern, no folds, no wrinkles, no draping, no shadows, no 3D fabric photography, no creases, no light variation, flat lay textile swatch, no paper grain, no blank canvas, no text",
    "hero_motif_1": "isolated foreground hero motif only, centered subject, transparent PNG cutout, real alpha background, empty transparent pixels around the subject, no background, no background art, no scenery, no garden, no foliage behind subject, no botanical backdrop, no painted wash, no rectangular composition, no full illustration scene, no vignette, no ground shadow, no text",
    "hero_motif_2": "isolated secondary accent motif only, centered subject, transparent PNG cutout, real alpha background, empty transparent pixels around the subject, no background, no colored background box, no scenery, refined brushwork, no text",
    "trim_motif": "isolated small decorative accent motif only, minimal composition, transparent PNG cutout, real alpha background, empty transparent pixels around the subject, no background, no colored background box, no scenery, no text",
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
        "Rows: 1 base seamless textures; 2 accent/solid seamless textures; 3 isolated transparent PNG placement motifs with real alpha backgrounds.",
        "All 9 panels must look like one coherent textile family: same palette, same paper grain, same brush language, same saturation range.",
        "Do not mix separate visual worlds such as warm beige line-art mushrooms with green watercolor meadow panels unless the palette and brush style are fully unified.",
        "Rows 1-2 are fabric repeats only: no large figurative subject, no complete scene, no landscape, no scenery, no environment, no animal/character/mushroom/flower bouquet as a full-body hero texture. Each panel must look like a fabric swatch, not a painting or scene.",
        "Row 3 motifs must be AI-generated as clean isolated transparent cutout artwork with real alpha backgrounds, never plain-color boxes, background art, scenery, rectangular patches, or semi-transparent full-image backgrounds.",
    ]
    for label, panel_id in BOARD_POSITIONS_EN:
        prompt = panel_prompts.get(panel_id) or PANEL_DEFAULTS_EN[panel_id]
        lines.append(f"{label}: {prompt}")
    lines.extend([
        "All panels share one palette, fabric texture, brush language, and commercial apparel mood.",
        BOARD_NEGATIVE_EN + ".",
        "Rows 1-2 must be usable fabric repeats; row 3 must be isolated transparent placement motifs with real alpha backgrounds.",
    ])
    return "\n".join(lines)
