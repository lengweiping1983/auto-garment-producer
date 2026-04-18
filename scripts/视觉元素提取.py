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
    from image_utils import ensure_thumbnail
except Exception:
    # fallback：如果 image_utils 不可用，直接返回原图
    def ensure_thumbnail(image_path, max_size=1024):
        return Path(image_path).resolve()


def build_vision_prompt(theme_path: Path, user_prompt: str, garment_type: str, season: str) -> str:
    """构造面向子 Agent 的视觉分析 prompt。"""
    lines = [
        "你是一位高级服装印花设计分析师和视觉艺术指导。请详细分析附带的主题参考图像，提取所有可用于商业成衣面料设计的视觉元素。",
        "",
        "===== 分析任务 =====",
        "",
        "1. 主体元素（Dominant Objects）",
        "   - 识别图像中最突出的 1-3 个主体物体",
        "   - 描述每个主体的：名称、颜色、形态、在画面中的位置与占比",
        "   - 估算每个主体的像素尺寸（width × height）和画面占比（%）",
        "   - 判断主体的方向性：vertical（竖向，如直立的花）/ horizontal（横向，如横枝）/ radial（放射形）/ symmetric（对称形）/ irregular（不规则）",
        "   - 判断该主体适合用作：hero_motif（定位图案）还是 main_texture（底纹元素）",
        "",
        "2. 辅助元素（Supporting Elements）",
        "   - 识别次要装饰：边框、背景纹理、点缀图案、配景物体等",
        "   - 描述每个辅助元素的：名称、类型（decoration/background/texture/frame）、视觉特征",
        "",
        "3. 色彩方案（Palette）",
        "   - 从图像中提取真实的 4 组色彩：",
        "     * primary（主色，2-3 个）：画面中最多的颜色，用于大身底纹",
        "     * secondary（辅色，1-2 个）：协调色，用于袖子/侧片",
        "     * accent（点缀色，1 个）：小面积亮色，用于细节点缀",
        "     * dark（深色，1 个）：最深色，用于饰边/袖口/暗部",
        "   - 每个颜色必须提供 HEX 色值（如 #f3f1df）",
        "   - 必须从图像中真实提取，不要编造",
        "",
        "4. 风格特征（Style）",
        "   - medium：媒介（水彩/工笔/油画/矢量/摄影/数字绘画/综合媒介等）",
        "   - brush_quality：笔触特征（如：柔和晕染、清晰勾线、干刷肌理、平滑矢量）",
        "   - mood：情绪关键词（如：优雅安静、活泼童趣、复古浪漫、清冷现代）",
        "   - pattern_density：图案密度（low/medium/high）",
        "   - line_style：线条特征（如：无轮廓、淡墨勾线、硬笔轮廓、渐变边缘）",
        "   - overall_impression：一句话总体印象",
        "",
        "6. 面料工艺推断（Fabric Engineering）",
        "   根据主题图像的风格关键词和视觉特征，推断面料是否有方向性/绒毛：",
        "   - has_nap: true/false —— 是否有倒顺毛（灯芯绒、丝绒、植绒、毛呢、法兰绒、麂皮、羊羔绒等）",
        "   - nap_confidence: 0-1 —— 推断确信度",
        "   - 触发关键词（中文/英文）：灯芯绒、丝绒、植绒、毛呢、法兰绒、麂皮、羊羔绒、corduroy、velvet、fleece、suede、plush、boiled wool",
        "   - **如果 has_nap=true，nap_direction 必填，不允许留空**（vertical/horizontal）。绝大多数绒毛面料为经向裁，默认填 vertical。",
        "   - ⚠️ 如果 has_nap=true 但 nap_direction 为空或未提供，输出将被视为无效，下游程序会强制 fallback 为 vertical。",
        "",
        "5. 自动生成面料提示词（Generated Prompts）",
        "   基于以上分析，为每种面料资产生成英文 AI 图像生成提示词：",
        "   - main：大面积底纹提示词。要求 seamless tileable、low noise、lots of negative space",
        "   - secondary：协调辅纹提示词",
        "   - accent：小型点缀纹提示词",
        "   - dark：深色饰边底纹提示词",
        "   - hero_motif：定位图案提示词。要求 plain light background、centered、suitable for background removal",
        "   所有提示词中必须包含 negative prompt 逻辑：no text, no watermark, no logo, no faces, no animals（除非用户明确要求）",
        "   ⚠️ 重要约束：生成的英文提示词中不得包含停用词（stop words）和禁用词（banned words）。",
        "     停用词包括：very, really, quite, beautiful, nice, good, bad, wonderful, fantastic, great, perfect 等模糊修饰词。",
        "     禁用词包括：任何可能触发内容安全过滤的词汇（暴力、色情、仇恨相关）。",
        "     提示词应使用具体、可视觉化的描述词，避免主观评价性形容词。",
        "",
        "===== 输出格式 =====",
        "请返回严格的 JSON，格式如下（不要任何解释文字、不要 markdown 代码块，只返回纯 JSON）：",
        "",
        json.dumps({
            "dominant_objects": [
                {
                    "name": "蓝牡丹",
                    "type": "main_subject",
                    "description": "淡蓝色牡丹花，中心偏深蓝，花瓣边缘柔和晕染，约占画面中心 15%",
                    "suggested_usage": "hero_motif",
                    "geometry": {
                        "pixel_width": 320,
                        "pixel_height": 480,
                        "canvas_ratio": 0.15,
                        "aspect_ratio": 0.67,
                        "orientation": "vertical",
                        "visual_center": [0.52, 0.48],
                        "form_type": "tall_flower"
                    }
                }
            ],
            "supporting_elements": [
                {
                    "name": "卷草边框",
                    "type": "decoration",
                    "description": "淡蓝色洛可可卷草纹，环绕主体，线条纤细优雅"
                }
            ],
            "palette": {
                "primary": ["#f3f1df", "#e8f0f8"],
                "secondary": ["#a8c4d9"],
                "accent": ["#2d3f5f"],
                "dark": ["#1a2530"]
            },
            "style": {
                "medium": "水彩",
                "brush_quality": "柔和晕染，边缘有纸纹渗透感",
                "mood": "优雅安静，青花瓷洛可可",
                "pattern_density": "low",
                "line_style": "淡墨勾线，无硬笔轮廓",
                "overall_impression": "淡蓝与象牙白交织的东方洛可可水彩风格"
            },
            "fabric_hints": {
                "has_nap": False,
                "nap_confidence": 0.3,
                "nap_direction": "",
                "reason": "水彩纸张风格，无明显绒毛面料特征。若 has_nap=true，nap_direction 必须提供 vertical 或 horizontal，不允许留空。"
            },
            "generated_prompts": {
                "main": "seamless tileable commercial textile texture, pale ivory ground with very faint blue peony scrolls, extremely low noise, abundant negative space, watercolor paper grain, no text, no watermark",
                "secondary": "seamless tileable coordinating textile texture, soft sky-blue ground with delicate blue acanthus scroll pattern, medium density but airy, same watercolor brush style, no text, no watermark",
                "accent": "seamless tileable small-scale accent pattern, tiny scattered blue floral buds on warm ivory, very small scale repeating pattern, charming but controlled density, no text, no watermark",
                "dark": "seamless tileable quiet dark trim texture, deep indigo-navy ground with tiny ivory pin-dot texture, very subtle and dark-quiet, no text, no watermark",
                "hero_motif": "elegant blue peony bloom centered in a delicate scroll frame, plain light ivory background, soft fading edges, balanced negative space, watercolor hand-painted, designed as placement print element, no text, no watermark"
            }
        }, ensure_ascii=False, indent=2),
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
        "- 所有 generated_prompts 的值在输出前必须经过停用词/禁用词过滤",
        "- 不要返回任何解释文字，只返回 JSON",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造子 Agent 视觉元素提取请求。")
    parser.add_argument("--theme-image", required=True, help="主题/参考图像路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--user-prompt", default="", help="用户美术指导或约束")
    parser.add_argument("--garment-type", default="commercial apparel sample", help="服装类型")
    parser.add_argument("--season", default="spring/summer", help="商业季节信号")
    args = parser.parse_args()

    theme_path = Path(args.theme_image)
    if not theme_path.exists():
        print(f"错误: 主题图不存在: {theme_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    theme_thumb = ensure_thumbnail(theme_path, max_size=512)
    prompt = build_vision_prompt(theme_thumb, args.user_prompt, args.garment_type, args.season)
    prompt_path = out_dir / "ai_vision_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    
    # 对示例中的 generated_prompts 也进行过滤（若视觉元素已存在）
    # 注意：实际过滤应在子 Agent 输出 visual_elements.json 后由调用方处理

    request_summary = {
        "request_id": "ai_vision_extraction_v1",
        "theme_image": str(theme_thumb.resolve()),
        "prompt_path": str(prompt_path.resolve()),
        "expected_output": str((out_dir / "visual_elements.json").resolve()),
        "garment_type": args.garment_type,
        "season": args.season,
        "user_prompt": args.user_prompt,
    }
    request_path = out_dir / "ai_vision_request.json"
    request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "视觉分析请求摘要": str(request_path.resolve()),
        "子Agent提示词": str(prompt_path.resolve()),
        "主题图路径": str(theme_thumb.resolve()),
        "预期输出": request_summary["expected_output"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
