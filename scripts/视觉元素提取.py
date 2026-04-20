#!/usr/bin/env python3
"""
主题图视觉元素提取 — 构造 AI 视觉分析请求。

本脚本不直接进行视觉推理，而是构造结构化提示词文件，
供具备视觉理解能力的模型阅读主题图并输出分析结果。

输出：
- ai_vision_prompt.txt：自然语言视觉分析请求
- 预期输出：visual_elements.json

使用方式：
1. 运行本脚本生成提示词
2. 将 ai_vision_prompt.txt 和主题图路径交给视觉模型
3. 视觉模型阅读图像后，输出严格的 visual_elements.json
4. 运行 生成设计简报.py --visual-elements visual_elements.json 生成后续文件
"""

import argparse
import json
import sys
from pathlib import Path

# 导入提示词过滤器
sys.path.insert(0, str(Path(__file__).parent))
try:
    from prompt_sanitizer import sanitize_prompt
except Exception:
    def sanitize_prompt(text, domain="generic"):
        return text

try:
    from image_utils import estimate_payload_budget, print_payload_budget_warning
except Exception:
    def estimate_payload_budget(prompt_path=None, image_paths=None, **kwargs):
        return {}
    def print_payload_budget_warning(budget):
        return


def build_vision_prompt_multi(theme_paths: list[Path], user_prompt: str, garment_type: str, season: str) -> str:
    """构造视觉分析 prompt。"""
    image_lines = [
        f"图 {idx}: {path.resolve()} — {'主参考图' if idx == 1 else '辅助参考图'}"
        for idx, path in enumerate(theme_paths, 1)
    ]

    multi_note = (
        "单图：直接分析该主题。"
        if len(theme_paths) == 1
        else (
            f"多图：逐张观察后融合为一个商业面料主题系统；不要输出多套方案。"
            "默认图1为主体，其余补充风格/配色/纹理；用户提示优先。"
        )
    )
    schema = {
        "dominant_objects": [
            {
                "name": "主体名",
                "type": "main_subject",
                "grade": "S|A|B|C",
                "description": "颜色、形态、位置、占比",
                "suggested_usage": "hero_motif",
                "source_image_refs": [1],
                "geometry": {
                    "pixel_width": 0,
                    "pixel_height": 0,
                    "canvas_ratio": 0.0,
                    "aspect_ratio": 1.0,
                    "orientation": "vertical|horizontal|radial|symmetric|irregular",
                    "visual_center": [0.5, 0.5],
                    "form_type": "short label"
                },
                "garment_placement_hint": {
                    "recommended_target_piece": "front_body|front_hero|none",
                    "recommended_width_ratio_in_piece": 0.30,
                    "recommended_height_ratio_in_piece": 0.28,
                    "recommended_anchor": "chest_center|center|small_accent|do_not_place",
                    "anti_examples": ["full bleed", "shoulder seam crossing", "neckline crossing"]
                }
            }
        ],
        "supporting_elements": [{"name": "元素名", "type": "decoration|background|texture|frame", "description": "...", "source_image_refs": [1]}],
        "palette": {"primary": ["#hex"], "secondary": ["#hex"], "accent": ["#hex"], "dark": ["#hex"]},
        "style": {"medium": "", "brush_quality": "", "mood": "", "pattern_density": "low|medium|high", "line_style": "", "overall_impression": ""},
        "background_palette": {"dominant_background": ["#hex"], "supporting_background": ["#hex"], "contrast_notes": ""},
        "style_signature": {"linework": "", "brushwork": "", "edge_quality": "", "texture_grain": "", "saturation_range": ""},
        "user_intent_interpretation": "结合用户提示和图片内容，对服装主题意图的简短判断",
        "reference_fidelity": {
            "must_preserve": ["主体身份、轮廓、姿态、比例、关键颜色和局部细节"],
            "may_simplify": ["可为了服装定位图简化的细节"],
            "must_not_change": ["不得替换成泛化主体或新角色"]
        },
        "design_dna": {
            "shared_palette": ["#hex"],
            "motif_vocabulary": ["从参考图提取的小型可重复元素"],
            "linework": "",
            "brushwork": "",
            "material_feel": "",
            "negative_space": "",
            "fusion_rule": "主图和纹理必须像同一套设计，不像两张图片拼贴"
        },
        "single_texture_derivation": {
            "main": "从参考图提炼主面料的背景色、笔触和小型 repeat 元素",
            "secondary": "从参考图提炼辅面料的协调小元素、线条或格纹结构",
            "accent_light": "从参考图提炼轻量点缀元素，小尺度 repeat",
            "forbidden_full_body_elements": ["不得进入满版纹理的完整主体/场景/文字/logo"]
        },
        "texture_micro_structure": {
            "main": {
                "motif_scale_relative": "最小重复元素占 tile 宽度的 3-8%",
                "motif_count_per_tile": "每 tile 可见元素 12-20 个",
                "negative_space_ratio": "负空间占比 45-55%",
                "repeat_unit_description": "具体写出最小重复单元里有什么（如 tiny 3-petal flowers + single leaf clusters + occasional dot），不得空泛",
                "element_type_mix": {"botanical": 0.6, "geometric_dot": 0.3, "organic_line": 0.1}
            },
            "secondary": {
                "motif_scale_relative": "协调元素占 tile 宽度的 2-6%",
                "motif_count_per_tile": "每 tile 可见元素 15-25 个",
                "negative_space_ratio": "负空间占比 50-60%",
                "repeat_unit_description": "具体写出协调 repeat 结构（如 lattice + small leaf + dot rhythm）",
                "element_type_mix": {"botanical": 0.4, "geometric": 0.4, "organic_line": 0.2}
            },
            "accent_light": {
                "motif_scale_relative": "点缀元素占 tile 宽度的 1-4%",
                "motif_count_per_tile": "每 tile 可见元素 20-40 个",
                "negative_space_ratio": "负空间占比 60-75%",
                "repeat_unit_description": "具体写出极小规模点缀（如 scattered tiny dots + micro leaf）",
                "element_type_mix": {"botanical": 0.3, "geometric_dot": 0.5, "organic_line": 0.2}
            }
        },
        "hero_edge_contract": {
            "min_margin_ratio": 0.30,
            "edge_fade_pixels": "2-6px soft anti-aliased edge only, no gradient halo beyond 6px",
            "forbidden_alpha_patterns": ["gradient wash fade to transparent", "semi-transparent halo around subject", "colored fringe on edge", "feathered edge wider than 8px"],
            "required_alpha_behavior": "hard binary alpha inside subject silhouette, single-pixel soft anti-alias at boundary, pure transparent outside, no intermediate gray-alpha band"
        },
        "hero_texture_fusion_plan": "透明主图与三张纹理如何共享色彩、笔触、边缘处理和元素呼应",
        "fabric_hints": {"has_nap": False, "nap_confidence": 0.0, "nap_direction": "", "reason": ""},
        "source_images": [{"index": 1, "path": str(theme_paths[0].resolve()) if theme_paths else "", "role": "primary"}],
        "image_analyses": [{"image_ref": 1, "dominant_subject_summary": "", "palette_summary": "", "style_summary": ""}],
        "fusion_strategy": {"primary_reference": 1, "hero_subject_source": [1], "palette_sources": [1], "style_sources": [1], "strategy_note": ""},
        "theme_to_piece_strategy": {
            "base_atmosphere": "大身低噪底纹如何继承主题色彩/氛围，不直接复制主体",
            "hero_motif": "组合主卖点元素，建议放置在前片/指定 hero 裁片；如果 dominant_objects 中有多个 S/A 级 hero_motif，必须组合保留，不要三选一",
            "accent_details": "小花、叶片、蘑菇等只作小面积点缀",
            "quiet_zones": "袖片、后片、领口、窄条等需要安静处理的区域",
            "do_not_use_as_full_body_texture": ["不适合大面积满版的具象元素"]
        },
        "generated_prompts": {
            "main": "英文 seamless tileable visible repeat pattern prompt。必须：1) 写出具体小元素名称（botanical/geometric/line/dot）；2) 包含 motif_scale_relative 估计（如 elements are 3-8% of tile width）；3) 包含 density_estimate（如 12-20 elements per tile）；4) 包含 negative_space_ratio（如 45-55% breathing room）；5) 不得是 abstract wash / plain texture / paper grain only / gradient / empty background / tonal atmosphere only / blurred background / scene / landscape",
            "secondary": "英文 coordinating seamless tileable visible repeat pattern prompt。必须：1) 写出协调 repeat 结构（lattice/linework/leaves/dots/geometric）；2) 包含 motif_scale_relative 和 density_estimate；3) 包含 negative_space_ratio；4) 与 main 共享 palette 和 brush language；5) 不得是 abstract wash / plain texture / paper grain only / gradient / empty background / tonal atmosphere only",
            "accent_light": "英文 small-scale accent repeat prompt。必须：1) 极小规模元素；2) 高密度点缀但负空间充足；3) 与 main/secondary 共享 palette 和 brush language；4) 不得喧宾夺主",
            "hero_motif_1": "英文 isolated foreground hero motif only as transparent PNG cutout with real alpha background。结构要求：先写主体观察段（覆盖 identity/pose/expression/hair/clothing/props/accessories/composition/art_style_details 全部9维），再接透明格式约束。必须：1) preserve and recreate the primary subject from reference image；2) complete uncropped subject, full head and hair visible；3) 主体边界到图像边缘至少 30% 留白（min_margin_ratio 0.30）；4) 边缘仅 2-6px 软抗锯齿，禁止 gradient halo / semi-transparent halo / colored fringe；5) alpha 内部为硬二值，边界单像素软过渡，外部纯透明；6) no background, no checkerboard preview, no fake grid, no colored box, no plain light box, no scenery, no garden, no foliage, no painted wash, no vignette, no ground shadow"
        },
        "prompt_quality_check": {
            "texture_passed": False,
            "hero_passed": False,
            "rewrite_count": 0,
            "texture_violations": [],
            "hero_violations": []
        }
    }
    lines = [
        "你是一位高级服装印花设计分析师。观察参考图，提取可用于商业成衣面料的视觉元素，并生成英文图像提示词。",
        "",
        "===== 参考图像 =====",
        multi_note,
        *image_lines,
        "",
        "===== 任务 =====",
        "1. dominant_objects: 最突出的1-3个主体；写名称、grade(S|A|B|C)、颜色/形态/位置/占比、source_image_refs、geometry、suggested_usage、garment_placement_hint。",
        "2. supporting_elements: 边框/背景/纹理/点缀等；标注 source_image_refs。",
        "3. palette: 从图像真实提取 primary/secondary/accent/dark HEX，不要编造。",
        "4. style: medium、brush_quality、mood、pattern_density、line_style、overall_impression。",
        "5. fabric_hints: 判断 has_nap；若 true，nap_direction 必填 vertical/horizontal。关键词含 corduroy/velvet/fleece/suede/plush/毛呢/法兰绒等。",
        "6. theme_to_piece_strategy: 把主题工程化拆成 base_atmosphere、hero_motif、accent_details、quiet_zones；明确哪些具象元素不得作为大身满版纹理。",
        "7. reference_fidelity/design_dna/single_texture_derivation/hero_texture_fusion_plan: 先明确主图必须保留什么、纹理从图片中提炼什么，以及主图和纹理如何像同一套设计。",
        "8. generated_prompts: 只生成英文 main/secondary/accent_light/hero_motif_1 共4条提示词；前3条 texture 要 seamless tileable、low noise 且有明确 visible repeat 结构，必须包含从参考图提炼的小型元素、植物、几何、线条、散点或格纹等可裁剪图案，不得是空泛底纹；hero_motif_1 必须尽可能包含并复现用户参考图中的主要内容/核心主体（人物、脸、角色、动物、商品、图标、物体都允许作为主图主体）。如果 dominant_objects 中有多个 S/A 级且 suggested_usage=hero_motif 的主体，hero_motif_1 必须把它们组合成一个 cohesive foreground placement graphic，而不是只选一个；保留每个主体的可识别轮廓、色彩、姿态和关键细节，必须完整不裁切，头部和头发完整可见，主体上方和四周保留透明留白，且必须是 isolated foreground、transparent PNG cutout、real alpha background、no background、no checkerboard transparency preview、no fake transparency grid、no colored box。",
        "",
        "===== hero_motif_1 主体描述必须覆盖的细节维度 =====",
        "写 hero_motif_1 时，不能只用一句话概括。必须像视觉观察报告一样逐条写出以下维度，确保 Neo AI 能精确复刻而非凭想象替代：",
        "- subject_identity: 主体身份（性别、年龄、种族/肤色倾向、人物类型）",
        "- pose_action: 姿态与动作（站姿/坐姿/动态、朝向角度：正面/3/4侧/全侧、重心在哪条腿、四肢位置）",
        "- expression: 表情（微笑/露齿笑/严肃/惊讶/自信/俏皮等、眼神方向）",
        "- hair: 发型与发色（长度、卷度、颜色、刘海/偏分/盘发等具体发型）",
        "- clothing: 服装（具体款式名称、颜色、材质质感、关键设计元素如领口/腰带/扣子/图案）",
        "- props: 手持物品/道具（颜色、形状、材质、握持方式、具体朝向）",
        "- accessories: 配饰（首饰、帽子、眼镜、腰带、鞋子等）",
        "- composition: 画面占比与位置（占原图多少、在画面左侧/右侧/居中）",
        "- art_style_details: 艺术风格特征（线条粗细与颜色、着色方式、阴影处理、边缘处理方式）",
        "这些细节必须直接来自你对参考图的观察，不得编造；如果某个维度在图中不清晰，写 'not clearly visible' 即可。最终把这些观察写成一条连贯的英文描述句，放在 hero_motif_1 的最前面作为主体段，然后再接 transparent PNG cutout 等格式约束。",
        "",
        "===== 主题元素 S/A/B/C 分级规则 =====",
        "S级：完整动物、人脸/人像、文字、商标、完整建筑、完整场景、复杂叙事插画。绝不能进入 base texture，只能拒绝、简化为剪影，或作为很小的定位 motif。",
        "A级：简化动物剪影、单朵大花、几何图标、单个清晰角色符号。只允许 1 个 hero 裁片使用，不能满版。",
        "B级：小花、小叶、抽象笔触点缀、小型几何元素。只能作小面积 accent。",
        "C级：主题色彩晕染、无具象形状的抽象纹理、水彩底、低对比噪点底、低对比小循环几何。可作大身 base。",
        "所有 S 级元素、以及不适合大身的 A 级元素，必须写入 theme_to_piece_strategy.do_not_use_as_full_body_texture。",
        "generated_prompts.main/secondary/accent_light 必须是可平铺面料纹理，只能继承色彩、笔触、氛围和小型辅助元素，不得直接包含 S/A 级具象主体名称，不得包含场景、风景、环境、完整画面；main/secondary 也必须像 accent_light 一样有清晰 repeat 图案结构，不得是纸纹、渐变、抽象波纹或空底。",
        "geometry 只描述主体在参考图中的尺寸和位置；真正穿到衣服上时，必须通过 garment_placement_hint 转换成裁片 bounding box 内的比例。",
        "S/A 级主体若允许作为 hero，garment_placement_hint 必须建议小型胸口定位：宽度默认 0.28–0.34，高度默认 0.22–0.32，anchor 默认 chest_center。",
        "garment_placement_hint.anti_examples 必须列出禁止用法，例如 full bleed、跨肩缝、跨袖窿、跨领口、完整场景满版。",
        "",
        "===== 输出 JSON schema =====",
        "只返回严格 JSON，不要解释文字、不要 markdown 代码块：",
        json.dumps(schema, ensure_ascii=False, indent=2),
        "",
        "===== 用户上下文 =====",
        f"服装类型: {garment_type}",
        f"季节: {season}",
        f"用户附加提示: {user_prompt or '无'}",
        "",
        "===== 重要约束 =====",
        "- 颜色必须从图像中真实提取，不要编造",
        "- 提示词必须是英文，可直接用于 Neo AI",
        "- 如果图像中有动物或人物，谨慎建议用途，优先建议用于 motif 而非 texture",
        "- dominant_objects[] 必须包含 grade: S|A|B|C",
        "- dominant_objects[] 必须包含 garment_placement_hint；参考图 geometry 不能直接等同于上身比例",
        "- S级元素必须进入 theme_to_piece_strategy.do_not_use_as_full_body_texture，且不得出现在 generated_prompts.main/secondary",
        "- S/A级主体如果允许作 hero，推荐上身宽度控制在 0.28–0.34，高度控制在 0.22–0.32；不得 full bleed、不得跨肩缝/袖窿/领口",
        "- 主题必须落地到裁片：大身只继承色彩/氛围，唯一 hero motif 承载主体，小元素只做 accent",
        "- 蘑菇、动物、角色、花丛、完整场景等具象元素不得建议为大面积 body texture，除非用户明确要求",
        "- 多张参考图必须融合为同一个主题方向，不要输出多套方案",
        "- 每个主体/辅助元素要标注 source_image_refs，便于后续追溯来源",
        "- generated_prompts 用具体视觉词；避免 very/really/beautiful/nice/good/great/perfect 等空泛词",
        "- generated_prompts.main 和 generated_prompts.secondary 必须明确 concrete visible repeat pattern、small motif repeat、botanical/geometric/line/scattered repeat 等具体图案结构，不得写 abstract wash、plain texture、paper grain only、gradient、empty background、tonal atmosphere only",
        "- generated_prompts 不得包含 accent_mid；默认只生成 main、secondary、accent_light 三张单纹理和 hero_motif_1 透明主图",
        "- 每张 texture prompt 必须写明 use reference image 1 as source for palette, brush language, material feel, small supporting motifs, and user intent；同时禁止复制完整主体/完整场景到满版纹理",
        "- generated_prompts.hero_motif_1 必须明确 preserve and recreate the primary subject(s) from the user's reference image as much as possible，包含用户图片中的主要内容/核心主体。若有多个 S/A 级 hero_motif 主体，必须组合保留全部主体，不得三选一；人物、脸、角色、动物、商品、图标、物体都允许作为透明主图主体，保留可识别轮廓、色彩、姿态和关键特征，并要求 complete uncropped subject(s)、full head and hair visible、generous transparent margin above and around the subject group",
        "- generated_prompts.hero_motif_1 的主体描述段不能只有一句话概括，必须覆盖：subject_identity、pose_action、expression、hair、clothing、props、accessories、composition、art_style_details 等维度，具体细节直接来自参考图观察，不得编造",
        "- generated_prompts.hero_motif_1 必须明确 isolated foreground motif only, transparent PNG cutout, real alpha background, no background, no checkerboard transparency preview, no fake transparency grid, no colored rectangle, no plain light box",
        "- generated_prompts.hero_motif_1 必须是前景主体 cutout，不得写 scene、garden、meadow、landscape、environment、foliage behind subject、botanical backdrop、painted wash、vignette、rectangular composition 或 full illustration scene",
        "- generated_prompts.main 必须是低密度 visible repeat pattern，淡底、可见但安静；不得写 abstract wash、plain color wash、plain texture、paper grain only、gradient、blurred background、empty texture 或 tonal atmosphere only",
        "- 纹理格负向逻辑必须覆盖 no text, no watermark, no logo, no faces；但 hero_motif_1 不得禁止 people/faces/characters/animals，因为用户图片主要内容可能包含这些主体",
        "- texture_micro_structure 必须对 main/secondary/accent_light 分别给出具体数值估计，不得空泛",
        "- hero_edge_contract 必须给出 min_margin_ratio、edge_fade_pixels、forbidden_alpha_patterns、required_alpha_behavior 四项",
        "- 不要返回任何解释文字，只返回 JSON",
        "",
        "===== 输出前自检（必须逐条完成） =====",
        "在输出 JSON 之前，对 generated_prompts 的每一条提示词执行以下自检。若有任何一条不满足，重写该提示词直到满足，并记录 rewrite_count 和 violations。",
        "",
        "[纹理提示词自检清单 — main/secondary/accent_light]",
        "□ 是否明确写出最小重复单元的具体视觉元素名称（如 tiny meadow flowers, small leaves, dots, lattice lines）？",
        "□ 是否包含 motif_scale_relative 估计（如 elements are 3-8% of tile width）？",
        "□ 是否包含 density_estimate（如 12-20 visible elements per tile）？",
        "□ 是否包含 negative_space_ratio（如 45-55% breathing room）？",
        "□ 是否禁止了 abstract wash / plain texture / paper grain only / gradient / empty background / tonal atmosphere only / blurred background / scene / landscape / environment？",
        "□ 是否没有直接写出 S/A 级具象主体名称（如 mushroom, rabbit, full flower bouquet, character）？",
        "□ 是否与 texture_micro_structure 和 design_dna 中描述的 palette/brushwork/linework 一致？",
        "□ 是否包含 'Use reference image 1 as source for palette, brush language, material feel, small supporting motifs, and user intent'？",
        "",
        "[Hero 提示词自检清单 — hero_motif_1]",
        "□ 主体描述段是否覆盖了 subject_identity / pose_action / expression / hair / clothing / props / accessories / composition / art_style_details 全部 9 个维度？",
        "□ 是否明确 preserve and recreate the primary subject from the user's reference image as much as possible？",
        "□ 是否包含 transparent PNG cutout + real alpha background + no background？",
        "□ 是否包含主体边界到图像边缘至少 30% 留白（min_margin_ratio ≥ 0.30）？",
        "□ 是否包含边缘仅 2-6px 软抗锯齿，禁止 gradient halo / semi-transparent halo / colored fringe？",
        "□ 是否包含 alpha 内部硬二值、边界单像素软过渡、外部纯透明？",
        "□ 是否明确 no checkerboard transparency preview / no fake transparency grid / no colored box / no plain light box？",
        "□ 是否没有写 scene / garden / meadow / landscape / environment / foliage behind subject / botanical backdrop / painted wash / vignette / ground shadow？",
        "□ 是否没有禁止 people/faces/characters/animals（这些是用户参考图的主体，允许作为 hero）？",
        "",
        "自检完成后，在 prompt_quality_check 中记录 passed 状态和 violations 列表。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造 AI 视觉元素提取请求。")
    parser.add_argument("--theme-image", action="append", default=[], help="主题/参考图像路径。可重复传入多张。")
    parser.add_argument("--theme-images-manifest", default="", help="theme_images_manifest.json 路径（可选，优先使用其中的 images[].path）")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--user-prompt", default="", help="用户美术指导或约束")
    parser.add_argument("--garment-type", default="commercial apparel sample", help="服装类型")
    parser.add_argument("--season", default="spring/summer", help="商业季节信号")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    theme_paths: list[Path] = []
    if args.theme_images_manifest:
        manifest_path = Path(args.theme_images_manifest)
        if not manifest_path.exists():
            print(f"错误: 主题图 manifest 不存在: {manifest_path}", file=sys.stderr)
            return 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("images", []):
            if item.get("path"):
                theme_paths.append(Path(item["path"]))
    else:
        theme_paths = [Path(p) for p in args.theme_image]

    if not theme_paths:
        print("错误: 必须提供至少一张 --theme-image，或提供 --theme-images-manifest。", file=sys.stderr)
        return 1

    missing = [str(p) for p in theme_paths if not p.exists()]
    if missing:
        print(f"错误: 主题图不存在: {missing[0]}", file=sys.stderr)
        return 1

    prompt = build_vision_prompt_multi(theme_paths, args.user_prompt, args.garment_type, args.season)
    prompt_path = out_dir / "ai_vision_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    
    # 对示例中的 generated_prompts 也进行过滤（若视觉元素已存在）。

    estimate_payload_budget(prompt_path, theme_paths)
    print(json.dumps({
        "AI视觉提示词": str(prompt_path.resolve()),
        "主题图路径": str(theme_paths[0].resolve()),
        "主题图数量": len(theme_paths),
        "预期输出": str((out_dir / "visual_elements.json").resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
