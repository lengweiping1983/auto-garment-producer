#!/usr/bin/env python3
"""
构造整体商业感复审请求，供子 Agent 审查最终预览图的商业可接受性。

使用方式：
1. 渲染完成后运行本脚本
2. 子Agent查看 preview_path 指向的 Kimi 缩略图 + 填充计划 → 输出 ai_commercial_review.json
3. 程序读取 review 结果：若 not approved 且 --auto-retry 启用，触发返工流程

输入：
- preview.png（最终预览图；请求中会转换为 Kimi 缩略图）
- piece_fill_plan.json
- commercial_design_brief.json
- fashion_qc_report.json（可选，提供逐裁片质检数据）

输出：
- ai_commercial_review_prompt.txt：面向子Agent的复审任务
- ai_commercial_review_request.json：结构化请求摘要
- ai_commercial_review.json（在 --selected 模式下）：复审结果
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from image_utils import ensure_thumbnail, estimate_payload_budget, print_payload_budget_warning
try:
    from PIL import Image
except Exception:
    Image = None


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def create_three_meter_preview(preview_path: str | Path, out_path: str | Path) -> Path | None:
    """Create a 64px mental-distance preview enlarged to 512px for review."""
    if Image is None:
        return None
    src = Path(preview_path)
    dst = Path(out_path)
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((64, 64), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (64, 64), "white")
            x = (64 - im.width) // 2
            y = (64 - im.height) // 2
            canvas.paste(im, (x, y))
            big = canvas.resize((512, 512), Image.Resampling.NEAREST)
            big.save(dst)
        return dst
    except Exception:
        return None


def build_review_prompt(preview_path: str, fill_plan: dict, brief: dict, qc_report: dict = None, preview_3m_path: str = "") -> str:
    """构造面向子Agent的整体商业感复审 prompt。"""
    pieces = fill_plan.get("pieces", [])
    hero_ids = [p["piece_id"] for p in pieces if (p.get("overlay") or {}).get("fill_type") == "motif"]
    art_direction = fill_plan.get("art_direction", {}) if isinstance(fill_plan.get("art_direction"), dict) else {}
    self_assessment = art_direction.get("self_assessment", {})

    lines = [
        "你是一位资深服装买手总监，精通商业成衣的市场可接受性判断。",
        "请查看以下最终预览图，从整体商业角度判断这套印花成衣是否适合量产销售。",
        "",
        "===== 必看图片 =====",
        f"预览图: {preview_path}",
        f"三米模拟图: {preview_3m_path}" if preview_3m_path else "三米模拟图: 未提供，请在脑中把预览缩成 64×64 像素再判断。",
        "你必须先查看这张图片，再做判断。",
        "三米测试必须以 64px 远看效果为准：在 64×64 模拟图上仍能分辨 hero、身片和袖片关系才算通过；如果只剩花、糊、乱或拼贴感，rule A 必须 fail。",
        "",
        "===== 设计简报 =====",
        f"服装类型: {brief.get('garment_type', '成衣')}",
        f"审美方向: {brief.get('aesthetic_direction', '商业畅销款')}",
        f"季节: {brief.get('season', '四季')}",
        f"目标客群: {brief.get('target_customer', '大众')}",
        f"Hero卖点裁片: {hero_ids}",
    ]

    palette = brief.get("palette", {})
    if palette:
        lines.append(f"色板: {palette}")

    if art_direction:
        lines.extend([
            "",
            "===== 生产规划 AI 的自评（需验证，不可照单全收） =====",
            f"策略: {art_direction.get('strategy', '')}",
            f"自评: {json.dumps(self_assessment, ensure_ascii=False)}" if self_assessment else "自评: 未提供",
            "请对照最终预览图验证这些自评分数。若你不同意，请在 issues 中指出被高估的维度、你认为合理的分数，以及视觉证据。",
        ])

    lines.extend([
        "",
        "===== 商业判断维度 =====",
        "1. 整体和谐度：9个裁片看起来是否像同一设计师的同一系列？色板、笔触、纹理是否统一？",
        "2. 可穿性：大身裁片是否过于繁忙？底纹是否低噪、可穿？",
        "3. 卖点清晰度：hero motif 是否醒目但不突兀？位置和比例是否恰当？",
        "4.  trim 处理：饰边是否协调？不会显得廉价或跳脱？",
        "5. 季节适配：颜色、密度、风格是否符合季节定位？",
        "6. 客群匹配：是否符合目标客群的审美和消费水平？",
        "7. 过度堆砌：是否有多余的装饰？整体是否干净、有呼吸感？",
        "8. 主题落地：主题是否被工程化为 1 个清晰 hero motif + 低噪主题氛围底纹 + 小面积点缀，而不是完整主题图满版铺贴？",
        "9. 配色一致性：是否出现米底/棕色/高饱和色块与主色板割裂，导致像多套资产硬拼？",
        "10. 风格一致性：是否出现线稿、水彩、贴纸、照片质感等笔触混杂？",
        "11. motif 质量：是否有半透明整张贴片、背景未去干净、主题残影或矩形边界？",
        "",
        "===== 硬否决条件（任一触发 approved 必须为 false）=====",
        "A. 三米测试不通过：64px 远看主体不清、纹理糊、整体花乱、像贴图拼接。",
        "B. Hero geometry 越界：hero 过大、靠边、跨缝线/领口/袖窿/肩缝、超过 1 个 hero、或半透明整图 overlay。",
        "C. 双源混用失控：跨源资产超过 2 个小面积 accent/trim，或大身 base 混用 A/B 两源。",
        "D. 关键分数低：hero_clarity 或 style_cohesion 低于 8。",
        "E. 大身 base 出现完整动物、人脸、文字、完整场景、建筑、复杂叙事插画。",
        "F. 出现半透明矩形残影、未去干净背景、明显水印或贴片边界。",
        "",
        "===== Fallback Safe Plan 规则 =====",
        "如果 approved=false，必须判断是否需要 fallback_safe_plan。",
        "fallback_safe_plan 的目标不是视觉冲击，而是统一、可穿、可量产：放弃 hero motif；大身只用低噪 base 或纯色；袖片与身片同源；trim/collar/cuff 使用最低饱和度 secondary 或 solid。",
        "返工建议必须优先重选资产和重做填充计划，不要只建议微调 scale/offset。",
        "",
    ])

    if qc_report:
        qc_issues = qc_report.get("issues", [])
        qc_warnings = qc_report.get("warnings", [])
        if qc_issues:
            lines.append("===== 程序质检发现的问题 =====")
            for issue in qc_issues:
                lines.append(f"- [{issue.get('type')}] {issue.get('piece_id', '')}: {issue.get('message', '')}")
            lines.append("")
        if qc_warnings:
            lines.append("===== 程序质检警告 =====")
            for w in qc_warnings:
                lines.append(f"- [{w.get('type')}] {w.get('piece_id', '')}: {w.get('message', '')}")
            lines.append("")

    lines.extend([
        "===== 输出格式 =====",
        "请返回严格的 JSON，不要任何解释文字：",
        "",
        json.dumps({
            "approved": True,
            "confidence": 0.85,
            "overall_assessment": "一句话总结这套成衣的商业感",
            "strengths": [
                "做得好的地方"
            ],
            "issues": [
                {
                    "severity": "high|medium|low",
                    "category": "harmony|wearability|hero_placement|trim|season|customer|clutter|theme_fidelity|palette_harmony|style_cohesion|motif_cleanliness|self_assessment",
                    "description": "具体问题描述",
                    "suggested_fix": "修改建议"
                }
            ],
            "hard_rejection_reasons": [
                {
                    "rule": "A|B|C|D|E|F",
                    "evidence": "预览图中的具体证据",
                    "required_action": "重选资产 / 重做填充计划 / fallback_safe_plan"
                }
            ],
            "scores": {
                "theme_fidelity": 9,
                "palette_harmony": 9,
                "style_cohesion": 9,
                "hero_clarity": 9,
                "wearability": 9
            },
            "fallback_safe_plan_required": False,
            "fallback_safe_plan": {
                "strategy": "仅当 fallback_safe_plan_required=true 时填写：放弃 hero motif，大身低噪同源，trim 使用最低饱和度 secondary 或 solid",
                "body_base_policy": "single low-noise primary base for all body and sleeve pieces",
                "hero_policy": "remove all hero motif overlays",
                "trim_policy": "quiet solid or lowest-saturation accent only",
                "reason": "为什么必须降级为保守可穿方案"
            },
            "self_assessment_review": {
                "verified": True,
                "disagreements": [
                    {
                        "criterion": "hero_clarity",
                        "claimed_score": 9,
                        "reviewer_score": 6,
                        "evidence": "预览图中卖点图案被裁片边界切断，远看不够清晰"
                    }
                ]
            },
            "priority_fix": "如果有必须修改的问题，指出最关键的一条",
        }, ensure_ascii=False, indent=2),
        "",
        "要求：",
        "- approved=true 表示可以直接量产；approved=false 表示需要修改",
        "- confidence 用 0–1 表示你的确信度",
        "- issues 为空数组表示没有明显问题",
        "- hard_rejection_reasons 为空数组表示没有触发硬否决；只要不为空，approved 必须为 false",
        "- fallback_safe_plan_required=true 时，必须填写 fallback_safe_plan",
        "- 如果出现风格拼贴、配色跳脱、主题残影、过度满版，approved 必须为 false",
        "- priority_fix 优先要求重选资产和重做填充计划，不要只建议微调 scale/offset",
        "- 每条 issue 必须给出具体的 suggested_fix",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造整体商业感复审请求，或验证AI复审结果。")
    parser.add_argument("--preview", required=True, help="preview.png 路径")
    parser.add_argument("--fill-plan", required=True, help="piece_fill_plan.json 路径")
    parser.add_argument("--brief", required=True, help="commercial_design_brief.json 路径")
    parser.add_argument("--qc-report", default="", help="fashion_qc_report.json 路径（可选）")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--selected", default="", help="子Agent输出的 ai_commercial_review.json 路径。若提供，直接验证并输出结果。")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fill_plan = load_json(args.fill_plan)
    brief = load_json(args.brief)
    qc_report = load_json(args.qc_report) if args.qc_report else None

    # 模式A：生成复审请求（使用缩略图避免 413）
    if not args.selected:
        preview_thumb = ensure_thumbnail(args.preview, max_size=384, provider="kimi")
        preview_3m = create_three_meter_preview(args.preview, out_dir / "preview_3m.png")
        preview_3m_thumb = ensure_thumbnail(preview_3m, max_size=384, provider="kimi") if preview_3m else None
        prompt = build_review_prompt(
            str(preview_thumb),
            fill_plan,
            brief,
            qc_report,
            str(preview_3m_thumb) if preview_3m_thumb else "",
        )
        prompt_path = out_dir / "ai_commercial_review_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        kimi_images = [preview_thumb] + ([preview_3m_thumb] if preview_3m_thumb else [])
        payload_budget = estimate_payload_budget(prompt_path, kimi_images)

        request_summary = {
            "request_id": "commercial_review_v1",
            "preview_path": str(preview_thumb.resolve()),
            "preview_3m_path": str(preview_3m_thumb.resolve()) if preview_3m_thumb else "",
            "preview_3m_original_path": str(preview_3m.resolve()) if preview_3m else "",
            "preview_original_path": str(Path(args.preview).resolve()),
            "fill_plan_path": str(Path(args.fill_plan).resolve()),
            "brief_path": str(Path(args.brief).resolve()),
            "prompt_path": str(prompt_path.resolve()),
            "expected_output": str((out_dir / "ai_commercial_review.json").resolve()),
            "payload_budget": payload_budget,
            "kimi_images": [str(path.resolve()) for path in kimi_images],
            "kimi_input_note": "只传 preview_path 和 preview_3m_path 指向的 Kimi 缩略图，不要传 preview_original_path 原图。",
        }
        req_path = out_dir / "ai_commercial_review_request.json"
        req_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps({
            "商业复审请求": str(req_path.resolve()),
            "子Agent提示词": str(prompt_path.resolve()),
            "预期输出": request_summary["expected_output"],
            "Kimi预览图": str(preview_thumb.resolve()),
            "Kimi三米模拟图": str(preview_3m_thumb.resolve()) if preview_3m_thumb else "",
            "Kimi请求体预算": payload_budget,
            "说明": "请启动 coder 子Agent，传入 ai_commercial_review_prompt.txt + preview_path/preview_3m_path 缩略图，要求输出严格的 ai_commercial_review.json；不要传原始 preview.png。",
        }, ensure_ascii=False, indent=2))
        print_payload_budget_warning(payload_budget)
        return 0

    # 模式B：验证子Agent复审结果
    selected_path = Path(args.selected)
    if not selected_path.exists():
        print(f"错误: 复审结果不存在: {selected_path}", file=sys.stderr)
        return 1

    review = load_json(selected_path)
    approved = review.get("approved", False)
    issues = review.get("issues", [])
    hard_rejection_reasons = review.get("hard_rejection_reasons", [])
    fallback_required = bool(review.get("fallback_safe_plan_required", False))
    scores = review.get("scores", {}) if isinstance(review.get("scores", {}), dict) else {}
    critical_score_keys = ["theme_fidelity", "palette_harmony", "style_cohesion", "hero_clarity"]
    low_scores = {k: scores.get(k) for k in critical_score_keys if isinstance(scores.get(k), (int, float)) and scores.get(k) < 8}
    if low_scores and approved:
        approved = False
        issues.append({
            "severity": "high",
            "category": "commercial_review_scores",
            "description": f"关键商业维度低于 8 分: {low_scores}",
            "suggested_fix": "重选资产并重做填充计划，优先修复主题落地、配色一致性和风格统一。",
        })
    if hard_rejection_reasons and approved:
        approved = False
        issues.append({
            "severity": "high",
            "category": "hard_rejection",
            "description": f"触发商业复审硬否决条件: {hard_rejection_reasons}",
            "suggested_fix": "不要局部微调；必须重选资产、重做填充计划，或启用 fallback_safe_plan。",
        })
    if fallback_required and approved:
        approved = False
        issues.append({
            "severity": "high",
            "category": "fallback_required",
            "description": "AI 复审要求启用 fallback_safe_plan，因此当前方案不得直接交付。",
            "suggested_fix": "执行 fallback_safe_plan：放弃 hero motif，大身低噪同源，饰边使用安静纯色或低饱和 accent。",
        })

    # 程序兜底：approved=false 且有 high severity issue 时告警
    high_issues = [i for i in issues if i.get("severity") == "high"]
    if not approved and high_issues:
        print(f"[商业复审未通过] 发现 {len(high_issues)} 个高风险问题：")
        for issue in high_issues:
            print(f"  [{issue.get('category')}] {issue.get('description')}")
        print(f"  建议修改: {review.get('priority_fix', '请参考 issues 列表')}")
    elif not approved:
        print(f"[商业复审未通过] 有 {len(issues)} 个中低风险问题，建议优化")
    else:
        print("[商业复审通过] 整体商业感可接受")

    # 输出最终审查报告
    final_report = {
        "source": "subagent_commercial_review",
        "approved": approved,
        "confidence": review.get("confidence", 0.5),
        "assessment": review.get("overall_assessment", ""),
        "high_issues_count": len(high_issues),
        "total_issues_count": len(issues),
        "scores": scores,
        "hard_rejection_reasons": hard_rejection_reasons,
        "fallback_safe_plan_required": fallback_required,
        "fallback_safe_plan": review.get("fallback_safe_plan", {}),
        "review_path": str(selected_path.resolve()),
    }
    final_path = out_dir / "commercial_review_result.json"
    final_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(final_report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
