#!/usr/bin/env python3
"""
构造整体商业感复审请求，供子 Agent 审查最终预览图的商业可接受性。

使用方式：
1. 渲染完成后运行本脚本
2. 子Agent查看 preview.png + 填充计划 → 输出 ai_commercial_review.json
3. 程序读取 review 结果：若 not approved 且 --auto-retry 启用，触发返工流程

输入：
- preview.png（最终预览图）
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
from image_utils import ensure_thumbnail


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_review_prompt(preview_path: str, fill_plan: dict, brief: dict, qc_report: dict = None) -> str:
    """构造面向子Agent的整体商业感复审 prompt。"""
    pieces = fill_plan.get("pieces", [])
    hero_ids = [p["piece_id"] for p in pieces if (p.get("overlay") or {}).get("fill_type") == "motif"]

    lines = [
        "你是一位资深服装买手总监，精通商业成衣的市场可接受性判断。",
        "请查看以下最终预览图，从整体商业角度判断这套印花成衣是否适合量产销售。",
        "",
        "===== 必看图片 =====",
        f"预览图: {preview_path}",
        "你必须先查看这张图片，再做判断。",
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
                    "category": "harmony|wearability|hero_placement|trim|season|customer|clutter",
                    "description": "具体问题描述",
                    "suggested_fix": "修改建议"
                }
            ],
            "priority_fix": "如果有必须修改的问题，指出最关键的一条",
        }, ensure_ascii=False, indent=2),
        "",
        "要求：",
        "- approved=true 表示可以直接量产；approved=false 表示需要修改",
        "- confidence 用 0–1 表示你的确信度",
        "- issues 为空数组表示没有明显问题",
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
        preview_thumb = ensure_thumbnail(args.preview, max_size=256)
        prompt = build_review_prompt(str(preview_thumb), fill_plan, brief, qc_report)
        prompt_path = out_dir / "ai_commercial_review_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        request_summary = {
            "request_id": "commercial_review_v1",
            "preview_path": str(preview_thumb.resolve()),
            "fill_plan_path": str(Path(args.fill_plan).resolve()),
            "brief_path": str(Path(args.brief).resolve()),
            "prompt_path": str(prompt_path.resolve()),
            "expected_output": str((out_dir / "ai_commercial_review.json").resolve()),
        }
        req_path = out_dir / "ai_commercial_review_request.json"
        req_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps({
            "商业复审请求": str(req_path.resolve()),
            "子Agent提示词": str(prompt_path.resolve()),
            "预期输出": request_summary["expected_output"],
            "说明": "请启动 coder 子Agent，传入 ai_commercial_review_prompt.txt + preview.png，要求输出严格的 ai_commercial_review.json",
        }, ensure_ascii=False, indent=2))
        return 0

    # 模式B：验证子Agent复审结果
    selected_path = Path(args.selected)
    if not selected_path.exists():
        print(f"错误: 复审结果不存在: {selected_path}", file=sys.stderr)
        return 1

    review = load_json(selected_path)
    approved = review.get("approved", False)
    issues = review.get("issues", [])

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
        "review_path": str(selected_path.resolve()),
    }
    final_path = out_dir / "commercial_review_result.json"
    final_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(final_report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
