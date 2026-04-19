#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构造 3×3 面料看板候选提示词的选择请求，供子 Agent 选择最优 9 面板组合。

使用方式：
1. 运行本脚本生成选择任务文件（ai_collection_selection_prompt.txt）
2. 外层 Agent 启动 coder 子Agent，传入该 prompt，要求子Agent输出 selected_variants.json
3. 重新运行本脚本 --selected selected_variants.json，生成最终看板 prompt

输入：
- collection_prompt_candidates.json（9 panels × 3 variants）
- commercial_design_brief.json
- style_profile.json

输出：
- ai_collection_selection_prompt.txt：面向子Agent的自然语言选择任务
- ai_collection_selection_request.json：结构化请求摘要
- selected_collection_prompt.txt（在 --selected 模式下）：最终看板 prompt
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from prompt_blocks import build_collection_board_prompt_en, STRICT_JSON_ONLY_ZH


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_selection_prompt(candidates: dict, brief: dict, style_profile: dict) -> str:
    """构造面向子Agent的看板候选选择 prompt。"""
    style = style_profile.get("style_details", {})
    palette = style_profile.get("palette", {})
    lines = [
        "你是一位高级服装印花艺术指导，精通商业成衣面料设计的协调性判断。",
        "以下是一个 3×3 面料看板的 9 个面板，每个面板有 3 个候选英文提示词变体。",
        "请为每个面板选择最佳的一个变体，确保 9 个面板整体协调、风格统一、符合商业成衣设计原则。",
        "",
        "===== 设计简报 =====",
        f"审美方向: {brief.get('aesthetic_direction', '商业畅销款打样')}",
        f"服装类型: {brief.get('garment_type', '成衣')}",
        f"季节: {brief.get('season', '四季')}",
        f"媒介: {style.get('medium', '水彩/综合媒介')}",
        f"情绪: {style.get('mood', '优雅安静')}",
        f"图案密度: {style.get('pattern_density', '低-中')}",
        "",
        "===== 色板参考 =====",
    ]
    for key, colors in palette.items():
        if colors:
            color_str = ", ".join([str(c) for c in colors[:5]])
            lines.append(f"  {key}: {color_str}")

    lines.extend([
        "",
        "===== 选择原则 =====",
        "同一系列感优先：palette、brush style、paper texture 一致。",
        "Row1/Row2 选择可平铺、低噪、可穿变体；Row3 只能选择 transparent PNG cutout、real alpha background、无纯色底框的定位图案。",
        "避免 dense/busy/high contrast/overcrowded/harsh，除非用户明确要视觉冲击款。",
        "",
        "===== 候选面板（9 panels × 3 variants）=====",
    ])

    for panel in candidates.get("panels", []):
        panel_id = panel.get("panel_id", "")
        position = panel.get("position", "")
        role = panel.get("role", "")
        lines.append(f"\n--- {panel_id} ({position}, {role}) ---")
        for i, variant in enumerate(panel.get("variants", []), 1):
            lines.append(f"  变体{i}: {variant}")

    lines.extend([
        "",
        "===== 输出格式 =====",
        STRICT_JSON_ONLY_ZH,
        "",
        json.dumps({
            "selected_variants": [
                {"panel_id": "main", "variant_index": 1, "reason": "低噪、同色板、适合大身"}
            ],
            "overall_strategy": "一句话策略",
            "coordination_notes": []
        }, ensure_ascii=False, indent=2),
        "",
        "要求：",
        "- 每个 panel_id 必须出现一次且仅一次",
        "- variant_index 必须是 1、2 或 3",
        "- reason 必须用中文，说明为什么选这个变体",
        "- overall_strategy 用中文总结",
    ])
    return "\n".join(lines)


def build_collection_prompt_from_selection(candidates: dict, selected: dict, style: dict) -> str:
    """根据子Agent的选择结果，拼接最终的 3×3 看板 prompt。
    兼容两种 selected_variants 格式：
      - list: [{"panel_id": "main", "variant_index": 1}, ...]
      - dict: {"main": 1, ...}
    """
    raw_variants = selected.get("selected_variants", [])
    if isinstance(raw_variants, dict):
        selected_map = {
            k: {"panel_id": k, "variant_index": int(v)}
            for k, v in raw_variants.items()
        }
    else:
        selected_map = {
            s["panel_id"]: s
            for s in raw_variants
        }

    panel_prompts = {}
    for panel in candidates.get("panels", []):
        pid = panel.get("panel_id", "")
        variants = panel.get("variants", [])
        sel = selected_map.get(pid)
        if sel and 1 <= sel.get("variant_index", 1) <= len(variants):
            panel_prompts[pid] = variants[sel["variant_index"] - 1]
        elif variants:
            panel_prompts[pid] = variants[0]
        else:
            panel_prompts[pid] = ""

    return build_collection_board_prompt_en(panel_prompts, style)


def main() -> int:
    parser = argparse.ArgumentParser(description="构造 3×3 看板候选提示词选择请求，或根据选择结果生成最终看板 prompt。")
    parser.add_argument("--candidates", required=True, help="collection_prompt_candidates.json 路径")
    parser.add_argument("--brief", required=True, help="commercial_design_brief.json 路径")
    parser.add_argument("--style-profile", required=True, help="style_profile.json 路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--selected", default="", help="子Agent输出的 selected_variants.json 路径。若提供，直接生成最终看板 prompt。")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = load_json(args.candidates)
    brief = load_json(args.brief)
    style_profile = load_json(args.style_profile)

    # 模式A：生成选择请求，等待子Agent
    if not args.selected:
        prompt = build_selection_prompt(candidates, brief, style_profile)
        prompt_path = out_dir / "ai_collection_selection_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        request_summary = {
            "request_id": "collection_prompt_selection_v1",
            "candidates_path": str(Path(args.candidates).resolve()),
            "brief_path": str(Path(args.brief).resolve()),
            "style_profile_path": str(Path(args.style_profile).resolve()),
            "prompt_path": str(prompt_path.resolve()),
            "expected_output": str((out_dir / "selected_variants.json").resolve()),
            "panels": len(candidates.get("panels", [])),
            "variants_per_panel": 3,
        }
        request_path = out_dir / "ai_collection_selection_request.json"
        request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps({
            "选择请求摘要": str(request_path.resolve()),
            "子Agent提示词": str(prompt_path.resolve()),
            "预期输出": request_summary["expected_output"],
            "说明": "请启动 coder 子Agent，传入 ai_collection_selection_prompt.txt，要求输出严格的 selected_variants.json",
        }, ensure_ascii=False, indent=2))
        return 0

    # 模式B：子Agent已选择，生成最终看板 prompt
    selected_path = Path(args.selected)
    if not selected_path.exists():
        print(f"错误: 选择结果文件不存在: {selected_path}", file=sys.stderr)
        return 1

    selected = load_json(selected_path)
    style = style_profile.get("style_details", {})
    final_prompt = build_collection_prompt_from_selection(candidates, selected, style)

    final_path = out_dir / "selected_collection_prompt.txt"
    final_path.write_text(final_prompt, encoding="utf-8")

    # 同时生成一个 metadata 文件记录选择理由
    meta = {
        "source": "subagent_selection",
        "selected_variants": selected.get("selected_variants", []),
        "overall_strategy": selected.get("overall_strategy", ""),
        "coordination_notes": selected.get("coordination_notes", []),
        "final_prompt_path": str(final_path.resolve()),
    }
    meta_path = out_dir / "selection_metadata.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "最终看板提示词": str(final_path.resolve()),
        "选择元数据": str(meta_path.resolve()),
        "面板数": len(selected.get("selected_variants", [])),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
