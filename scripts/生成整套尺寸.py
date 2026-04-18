#!/usr/bin/env python3
"""
基于已有的 -S 渲染结果，纯程序生成整套其他尺寸（M/L/XL/XXL）。

零 AI 调用，只读取 -S 的 piece_fill_plan.json 和 texture_set.json，
通过模板映射关系渲染其他尺寸。

用法:
    python3 生成整套尺寸.py \
        --base-dir /path/to/已有的-S输出目录 \
        [--sizes m,l,xl] \
        [--pattern /path/to/BFSK26308XCJ01L-S_mask.png]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from template_loader import (
    find_template_by_pattern_path,
    load_size_mappings,
    load_size_pieces,
)
from 端到端自动化 import render_size_variants_core


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="基于已有-S结果，纯程序生成整套其他尺寸。")
    parser.add_argument("--base-dir", required=True, help="-S 输出目录（包含 piece_fill_plan.json + texture_set.json）")
    parser.add_argument("--sizes", default="", help="指定要生成的尺寸，逗号分隔。如 'm,l,xl'。空则生成全部非S尺寸。")
    parser.add_argument("--pattern", default="", help="-S mask 路径，用于自动发现模板。也可从 base-dir/production_context.json 推断。")
    args = parser.parse_args()

    base_dir = Path(args.base_dir)
    if not base_dir.exists():
        print(f"[错误] 目录不存在: {base_dir}", file=sys.stderr)
        return 1

    # 读取 -S 的 fill_plan
    fill_plan_path = base_dir / "piece_fill_plan.json"
    if not fill_plan_path.exists():
        print(f"[错误] 未找到 {fill_plan_path}。请先完成 -S 的渲染。", file=sys.stderr)
        return 1
    try:
        base_fill_plan = _load_json(fill_plan_path)
    except Exception as exc:
        print(f"[错误] 读取 fill_plan 失败: {exc}", file=sys.stderr)
        return 1

    # 读取 texture_set
    texture_set_path = base_dir / "texture_set.json"
    if not texture_set_path.exists():
        # 尝试从 production_context.json 中找
        ctx_path = base_dir / "production_context.json"
        if ctx_path.exists():
            try:
                ctx = _load_json(ctx_path)
                ts = ctx.get("paths", {}).get("texture_set", "")
                if ts:
                    texture_set_path = Path(ts)
            except Exception:
                pass
    if not texture_set_path.exists():
        print(f"[错误] 未找到 texture_set.json。", file=sys.stderr)
        return 1

    # 自动发现模板
    pattern_path = args.pattern
    if not pattern_path:
        # 尝试从 production_context.json 推断
        ctx_path = base_dir / "production_context.json"
        if ctx_path.exists():
            try:
                ctx = _load_json(ctx_path)
                pattern_path = ctx.get("paths", {}).get("pattern_image", "")
            except Exception:
                pass
        # 兜底：尝试找 piece_overview 或 pieces.json
        if not pattern_path:
            pieces_path = base_dir / "pieces.json"
            if pieces_path.exists():
                try:
                    pieces = _load_json(pieces_path)
                    pattern_path = pieces.get("pattern_image", "")
                except Exception:
                    pass

    if not pattern_path:
        print("[错误] 无法自动发现模板。请提供 --pattern 参数。", file=sys.stderr)
        return 1

    template = find_template_by_pattern_path(pattern_path)
    if not template:
        print(f"[错误] 未找到与 pattern 匹配的模板: {pattern_path}", file=sys.stderr)
        return 1

    template_id = template.get("template_id", "")
    mappings = load_size_mappings(template_id)
    if not mappings:
        print(f"[错误] 模板 {template_id} 未找到 size_mappings.json。", file=sys.stderr)
        return 1

    size_data = mappings.get("sizes", {})
    if not size_data:
        print("[错误] 模板无其他尺寸映射。", file=sys.stderr)
        return 1

    # 筛选指定尺寸
    if args.sizes:
        wanted = set(args.sizes.split(","))
        size_data = {k: v for k, v in size_data.items() if k in wanted}
        if not size_data:
            print(f"[错误] 无匹配尺寸: {args.sizes}", file=sys.stderr)
            return 1

    print(f"[整套生成] 模板: {template_id}，生成尺寸: {list(size_data.keys())}")
    render_size_variants_core(base_fill_plan, texture_set_path, base_dir, template_id, size_data)
    print("[整套生成] 完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
