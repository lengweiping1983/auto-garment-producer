#!/usr/bin/env python3
"""
主题图视觉元素提取 — 构造子 Agent 审美分析请求。

本脚本不直接进行视觉推理，而是构造结构化提示词文件，
供子 Agent（具备视觉理解能力的 Kimi）阅读主题图并输出分析结果。

输出：
- ai_vision_prompt.txt：面向子 Agent 的自然语言视觉分析请求
- ai_vision_request.json：机器可读的结构化请求摘要
- 子 Agent 预期输出：visual_elements.json

使用方式：
1. 运行本脚本生成提示词
2. 启动子 Agent（coder 类型），传入 ai_vision_prompt.txt 和主题图路径
3. 子 Agent 阅读图像后，输出严格的 visual_elements.json
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
    from image_utils import ensure_thumbnail, estimate_payload_budget, print_payload_budget_warning
except Exception:
    # fallback：如果 image_utils 不可用，直接返回原图
    def ensure_thumbnail(image_path, max_size=1024):
        return Path(image_path).resolve()
    def estimate_payload_budget(prompt_path=None, image_paths=None, **kwargs):
        return {}
    def print_payload_budget_warning(budget):
        return


def build_vision_prompt(theme_path: Path, user_prompt: str, garment_type: str, season: str) -> str:
    return build_vision_prompt_multi([theme_path], user_prompt, garment_type, season)


def build_vision_prompt_multi(theme_paths: list[Path], user_prompt: str, garment_type: str, season: str) -> str:
    """构造面向子 Agent 的视觉分析 prompt。"""
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
        "fabric_hints": {"has_nap": False, "nap_confidence": 0.0, "nap_direction": "", "reason": ""},
        "source_images": [{"index": 1, "path": str(theme_paths[0].resolve()) if theme_paths else "", "role": "primary"}],
        "image_analyses": [{"image_ref": 1, "dominant_subject_summary": "", "palette_summary": "", "style_summary": ""}],
        "fusion_strategy": {"primary_reference": 1, "hero_subject_source": [1], "palette_sources": [1], "style_sources": [1], "strategy_note": ""},
        "theme_to_piece_strategy": {
            "base_atmosphere": "大身低噪底纹如何继承主题色彩/氛围，不直接复制主体",
            "hero_motif": "唯一主卖点元素，建议放置在前片/指定 hero 裁片",
            "accent_details": "小花、叶片、蘑菇等只作小面积点缀",
            "quiet_zones": "袖片、后片、领口、窄条等需要安静处理的区域",
            "do_not_use_as_full_body_texture": ["不适合大面积满版的具象元素"]
        },
        "generated_prompts": {
            "main": "英文 seamless tileable low-density tonal leaf repeat pattern prompt，淡底、可见但安静的叶片重复结构，不得是 abstract wash / plain color wash / blurred background / empty texture",
            "secondary": "英文 coordinating repeat prompt",
            "dark_base": "英文 seamless tileable dark green micro stripe or tiny geometric repeat prompt，清晰 jacquard/织纹结构，不得是 forest / foliage photo / camouflage / atmospheric scene / plain dark texture",
            "accent_light": "英文 small-scale accent repeat prompt",
            "accent_mid": "英文 soft geometric or organic lattice repeat prompt",
            "solid_quiet": "英文 seamless tileable quiet light-ground micro dot or mini woven repeat prompt，低对比衬里/饰边微型纹样，不得是 plain solid / blank canvas / paper grain",
            "hero_motif_1": "英文 isolated foreground hero motif only as transparent PNG cutout with real alpha background, no background, no garden, no foliage behind subject, no full illustration scene, no colored box",
            "hero_motif_2": "英文 isolated secondary accent motif only as transparent PNG cutout with real alpha background, no background, no colored box",
            "trim_motif": "英文 isolated small decorative accent motif only as transparent PNG cutout with real alpha background, no background, no colored box"
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
        "7. generated_prompts: 生成英文 main/secondary/dark_base/accent_light/accent_mid/solid_quiet/hero_motif_1/hero_motif_2/trim_motif 共9个面板提示词；texture 要 seamless tileable、low noise 且有明确 repeat 结构；motif 要 isolated foreground、transparent PNG cutout、real alpha background、no background、no colored box。",
        "",
        "===== 主题元素 S/A/B/C 分级规则 =====",
        "S级：完整动物、人脸/人像、文字、商标、完整建筑、完整场景、复杂叙事插画。绝不能进入 base texture，只能拒绝、简化为剪影，或作为很小的定位 motif。",
        "A级：简化动物剪影、单朵大花、几何图标、单个清晰角色符号。只允许 1 个 hero 裁片使用，不能满版。",
        "B级：小花、小叶、抽象笔触点缀、小型几何元素。只能作小面积 accent。",
        "C级：主题色彩晕染、无具象形状的抽象纹理、水彩底、低对比噪点底、低对比小循环几何。可作大身 base。",
        "所有 S 级元素、以及不适合大身的 A 级元素，必须写入 theme_to_piece_strategy.do_not_use_as_full_body_texture。",
        "generated_prompts.main/secondary/dark_base/accent_light/accent_mid 必须是可平铺面料纹理，只能继承色彩、笔触、氛围，不得直接包含 S/A 级具象主体名称，不得包含场景、风景、环境、完整画面。",
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
        "- 提示词必须是英文，可直接用于 AI 图像生成器",
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
        "- generated_prompts.hero_motif_1/hero_motif_2/trim_motif 必须明确 isolated foreground motif only, transparent PNG cutout, real alpha background, no background, no colored rectangle, no plain light box",
        "- generated_prompts.hero_motif_1 必须是前景主体 cutout，不得写 scene、garden、meadow、landscape、environment、foliage behind subject、botanical backdrop、painted wash、vignette、rectangular composition 或 full illustration scene",
        "- generated_prompts.main 必须是低密度 tonal leaf repeat pattern，淡底、可见但安静；不得写 abstract wash、plain color wash、blurred background 或 empty texture",
        "- generated_prompts.dark_base 必须是 dark green micro stripe 或 tiny geometric repeat，清晰织纹结构；不得写 forest、foliage photo、camouflage、atmospheric、moody landscape 或 plain dark texture",
        "- generated_prompts.solid_quiet 必须是 quiet light-ground micro dot 或 mini woven repeat，低对比衬里/饰边微型纹样；不得写 plain solid、no pattern、blank canvas、paper grain 或 pure background function",
        "- 负向逻辑必须覆盖 no text, no watermark, no logo, no faces；除非用户明确要求，不要动物",
        "- 不要返回任何解释文字，只返回 JSON",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造子 Agent 视觉元素提取请求。")
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

    theme_thumbs = [ensure_thumbnail(path, max_size=512, provider="kimi") for path in theme_paths]
    prompt = build_vision_prompt_multi(theme_thumbs, args.user_prompt, args.garment_type, args.season)
    prompt_path = out_dir / "ai_vision_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    
    # 对示例中的 generated_prompts 也进行过滤（若视觉元素已存在）
    # 注意：实际过滤应在子 Agent 输出 visual_elements.json 后由调用方处理

    payload_budget = estimate_payload_budget(prompt_path, theme_thumbs)
    request_summary = {
        "request_id": "ai_vision_extraction_v1" if len(theme_thumbs) == 1 else "ai_vision_multi_extraction_v1",
        "theme_image": str(theme_thumbs[0].resolve()),
        "theme_image_original": str(theme_paths[0].resolve()),
        "theme_images": [
            {
                "index": idx + 1,
                "path": str(path.resolve()),
                "original_path": str(theme_paths[idx].resolve()) if idx < len(theme_paths) else "",
                "role_hint": "primary" if idx == 0 else "reference",
            }
            for idx, path in enumerate(theme_thumbs)
        ],
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / "visual_elements.json").resolve()),
        "garment_type": args.garment_type,
        "season": args.season,
        "user_prompt": args.user_prompt,
        "payload_budget": payload_budget,
        "kimi_input_note": "只把 theme_image/theme_images 中的 Kimi 缩略图传给子 Agent，不要传原图或 base64。",
    }
    request_path = out_dir / "ai_vision_request.json"
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "视觉分析请求摘要": str(request_path.resolve()),
        "子Agent提示词": str(prompt_path.resolve()),
        "主题图路径": str(theme_thumbs[0].resolve()),
        "主题图数量": len(theme_thumbs),
        "主题图列表": [str(path.resolve()) for path in theme_thumbs],
        "预期输出": request_summary["expected_output"],
        "Kimi请求体预算": payload_budget,
    }, ensure_ascii=False, indent=2))
    print_payload_budget_warning(payload_budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
