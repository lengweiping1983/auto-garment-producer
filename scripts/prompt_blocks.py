#!/usr/bin/env python3
"""Shared prompt fragments for auto-garment production.

Keep long policy text in one place so prompt builders stay compact and consistent.
"""

try:
    from prompt_sanitizer import sanitize_blur_risks
except Exception:
    def sanitize_blur_risks(text):
        return text

FRONT_EFFECT_NEGATIVE_EN = (
    "no garment mockup, no front-view clothing render, no fashion model, no mannequin, "
    "no person wearing garment, no on-body render, no T-shirt mockup, no product photo, no lookbook"
)

# 纹理专用负向提示词（聚焦"不要出现具象主体、不要场景、不要抽象 wash"）
TEXTURE_NEGATIVE_EN = (
    "no animals, no characters, no faces, no people, no text, "
    "no labels, no captions, no titles, no words, no letters, no typography, no logo, no watermark, "
    "no house, no river, no full landscape scene, no scenery, no environment, no background scene, no poster composition, no sticker sheet, "
    "no harsh black outlines, no dense confetti, no neon colors, no muddy dark colors, "
    "no abstract wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only, no blurred background, "
    "no folds, no wrinkles, no draping, no creases, no shadows, no 3D fabric photography, no light variation across surface, "
    "no gradient backgrounds inside individual panels, no photographic realism, no vector flatness, no digital gradient, "
    "blurry, out of focus, smeared, smudged, vignette, distorted, deformed, low quality, jpeg artifacts, grainy, "
    + FRONT_EFFECT_NEGATIVE_EN
)

# Hero 专用负向提示词（聚焦"背景残留和格式问题"）
HERO_NEGATIVE_EN = (
    "text, labels, captions, titles, typography, words, letters, signage, logo, watermark, "
    "plain light box, colored background box, filled rectangle, background art, scenery, landscape, environment, "
    "checkerboard transparency preview, fake transparency grid, semi-transparent full-image patch, "
    "gradient wash fade to transparent, colored fringe on edge, halo effect around subject, "
    "full illustration scene, poster composition, sticker sheet, garment mockup, fashion model, mannequin, "
    "person wearing garment, product photo, lookbook, ground shadow, vignette, "
    "botanical backdrop, foliage behind subject, painted wash behind subject, garden background, meadow background, "
    "blurry, out of focus, smeared, smudged, distorted, deformed, low quality, jpeg artifacts, grainy"
)

PANEL_DEFAULTS_EN = {
    "main": "seamless tileable visible repeat pattern with concrete small botanical or geometric motifs on pale ground, stable low-to-medium density, clearly repeatable elements, commercial apparel base fabric, no abstract wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only, no blurred background, no scene, no landscape, no text",
    "secondary": "seamless tileable coordinating visible repeat pattern with concrete small motifs, lattice, linework, leaves, dots, or controlled geometric elements, stable repeat structure on light ground, same palette, no abstract wash, no plain texture, no paper grain only, no gradient, no empty background, no tonal atmosphere only, no scene, no text",
    "accent_light": "tiny scattered small-scale pattern on light ground, controlled density, no text",
    "hero_motif_1": "isolated foreground hero motif only, centered complete subject, transparent PNG cutout, real alpha background, preserve and recreate the primary subject from the user's reference image as much as possible, keep the recognizable silhouette, color identity, pose, proportions, and key visual details, full head and hair visible, uncropped subject, generous transparent margin above and around the subject, no background, no checkerboard transparency preview, no background art, no scenery, no garden, no foliage behind subject, no botanical backdrop, no painted wash, no rectangular composition, no full illustration scene, no vignette, no ground shadow, no text",
}

def compact_style_line(style: dict | None) -> str:
    style = style or {}
    return (
        f"{style.get('overall_impression', 'Elegant commercial textile collection')}. "
        f"{style.get('mood', 'Quiet and wearable')}. "
        f"{style.get('medium', 'Watercolor')}. "
        "Wearable, cohesive fashion print suite."
    )


def build_family_contract_text(style: dict | None = None, palette: dict | None = None, design_dna: dict | None = None) -> str:
    """Build the family contract paragraph injected into every texture prompt.

    This ensures three independently-generated textures still feel like one
    coherent textile family even when Neo AI never sees the other two panels.
    """
    style = style or {}
    palette = palette or {}
    design_dna = design_dna or {}

    parts = []

    # Palette
    shared_palette = palette.get("primary", []) + palette.get("secondary", []) + palette.get("accent", [])
    if shared_palette:
        parts.append(f"Shared palette: {', '.join(shared_palette[:6])}.")

    # Brush signature
    brush = design_dna.get("brushwork") or style.get("brush_quality") or style.get("medium", "watercolor")
    if brush:
        parts.append(f"Brush signature: {brush}.")

    # Line quality
    line = design_dna.get("linework") or style.get("line_style", "")
    if line:
        parts.append(f"Line quality: {line}.")

    # Material feel
    material = design_dna.get("material_feel", "")
    if material:
        parts.append(f"Material feel: {material}.")

    # Saturation range
    saturation = design_dna.get("saturation_range", "")
    if saturation:
        parts.append(f"Saturation range: {saturation}.")
    else:
        parts.append("Saturation range: low to medium, no neon or muddy darks.")

    # Fusion rule
    fusion = design_dna.get("fusion_rule", "")
    if fusion:
        parts.append(f"Family rule: {fusion}")

    # Negative space
    ns = design_dna.get("negative_space", "")
    if ns:
        parts.append(f"Negative space: {ns}")

    family_negative = (
        "Forbidden family-wide: vector flatness, digital gradient, photographic realism, "
        "sticker-cut harsh edges, hard black outlines, neon colors, muddy dark colors."
    )
    parts.append(family_negative)

    result = " ".join(parts)
    # 最后过一遍模糊风险词清理
    return sanitize_blur_risks(result)


def build_single_texture_prompt_en(
    texture_id: str,
    texture_prompt: str,
    style: dict | None = None,
    family_contract: str = "",
) -> str:
    """Build the final English prompt for one standalone textile texture.

    Injects a family contract at the top so the texture feels coordinated
    with the other two panels even when generated independently.
    """
    prompt = texture_prompt or PANEL_DEFAULTS_EN.get(texture_id, PANEL_DEFAULTS_EN["main"])
    role_line = {
        "main": "Main fabric: quiet low-to-medium density repeat for large body pieces.",
        "secondary": "Secondary fabric: coordinating repeat for sleeves, back body, or supporting pieces.",
        "accent_light": "Light accent fabric: small-scale repeat for controlled visual variation.",
    }.get(texture_id, "Commercial apparel fabric repeat.")

    contract_text = family_contract or build_family_contract_text(style)

    lines = [
        "Create one standalone square seamless tileable textile repeat, not a board and not a mockup.",
        f"Family contract: {contract_text}",
        f"Texture role: {role_line}",
        "Use reference image 1 as the source for palette, brush language, material feel, small supporting motifs, and the user's theme intent.",
        "Do not copy the complete hero subject, complete scene, face, animal, character, product, logo, or large foreground object into the repeat.",
        "The repeat must feel designed for the same garment system as the transparent hero motif: same color family, same line quality, same saturation range, same textile mood.",
        f"Art direction: {compact_style_line(style)}",
        prompt,
        TEXTURE_NEGATIVE_EN + ".",
        "Required output: one seamless tileable fabric texture only, full square artwork, no gutters, no labels, no text, no garment, no model.",
    ]
    result = "\n".join(lines)
    return sanitize_blur_risks(result)


def build_transparent_hero_prompt_en(
    hero_prompt: str,
    style: dict | None = None,
    edge_contract: dict | None = None,
) -> str:
    """Build the final English single transparent hero motif prompt.

    Injects a precise edge contract when available to reduce background
    residue and fake-transparency artifacts.
    """
    prompt = hero_prompt or PANEL_DEFAULTS_EN["hero_motif_1"]
    lines = [
        "Create one isolated foreground apparel placement graphic as a transparent PNG cutout with real alpha background.",
        f"Art direction: {compact_style_line(style)}",
        # Place the detailed subject description FIRST so the generative model
        # reads "what to draw" before the negative constraints.
        prompt,
        "The subject must contain the primary subject from the user's reference image as much as possible, not a generic substitute.",
        "People, faces, characters, animals, products, icons, objects, or logos from the user's reference image are allowed as the hero subject when they are the main content.",
        "Preserve the recognizable silhouette, color identity, pose, proportions, and key visual details of the user's main image content while simplifying only enough for a clean apparel placement graphic.",
        "The subject must be centered, cleanly separated from the background, with soft but readable edges.",
    ]

    # Inject precise edge contract if provided
    if edge_contract:
        min_margin = edge_contract.get("min_margin_ratio", 0.30)
        fade = edge_contract.get("edge_fade_pixels", "2-6px soft anti-aliased edge only")
        alpha_behavior = edge_contract.get(
            "required_alpha_behavior",
            "hard binary alpha inside subject silhouette, single-pixel soft anti-alias at boundary, pure transparent outside"
        )
        forbidden_patterns = edge_contract.get("forbidden_alpha_patterns", [])
        lines.append(
            f"Edge contract: minimum {int(min_margin * 100)}% transparent margin around subject. "
            f"Edge fade: {fade}. "
            f"Alpha behavior: {alpha_behavior}."
        )
        if forbidden_patterns:
            lines.append(
                f"Forbidden edge patterns: {', '.join(forbidden_patterns)}."
            )

    lines.append(
        "Required output: complete uncropped subject with full head and hair visible, "
        "real transparent alpha pixels with generous empty margin above and around the subject, "
        "no background, no checkerboard transparency preview, no fake transparency grid, "
        "no plain light box, no colored background box, no filled rectangle, no scenery, "
        "no full illustration scene, no sticker sheet, no poster composition, "
        "no garment mockup, no model, no text, no logo, no watermark."
    )
    lines.append(
        "Leave balanced empty transparent pixels around the subject so it can be split vertically across left and right front garment pieces."
    )
    lines.append(HERO_NEGATIVE_EN + ".")
    result = "\n".join(lines)
    return sanitize_blur_risks(result)


