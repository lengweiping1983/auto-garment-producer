#!/usr/bin/env python3
"""
轮询驱动端到端自动化

解决双源看板生成（Neo AI + libtv）耗时过长导致 Shell 超时的问题。

工作流程：
1. 调用端到端自动化脚本
2. 若返回 exit code 2（双源看板生成进行中），等待一段时间后重试
3. 若返回 exit code 0（成功）或 1（失败），退出并报告结果

用法：
    python3 轮询驱动端到端.py \
      --theme-image /path/to/theme.png \
      --out /path/to/output \
      --garment-type "T恤" \
      --mode standard \
      --dual-source \
      --multi-scheme \
      --max-schemes 4

所有参数会透传给端到端自动化.py。
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

EXIT_DUAL_SOURCE_IN_PROGRESS = 2


def build_end_to_end_cmd(args: argparse.Namespace) -> list[str]:
    """将轮询驱动的参数转换为端到端脚本的参数。"""
    skill_dir = Path(__file__).resolve().parents[1]
    e2e_script = skill_dir / "scripts" / "端到端自动化.py"
    cmd = [sys.executable, str(e2e_script)]

    # 透传所有已知参数
    if args.theme_image:
        for img in args.theme_image:
            cmd.extend(["--theme-image", img])
    if args.theme_images:
        cmd.extend(["--theme-images", args.theme_images])
    if args.out:
        cmd.extend(["--out", args.out])
    if args.collection_board:
        cmd.extend(["--collection-board", args.collection_board])
    if args.texture_set:
        cmd.extend(["--texture-set", args.texture_set])
    if args.pattern:
        cmd.extend(["--pattern", args.pattern])
    if args.garment_type:
        cmd.extend(["--garment-type", args.garment_type])
    if args.template:
        cmd.extend(["--template", args.template])
    if args.mode:
        cmd.extend(["--mode", args.mode])
    if args.brief:
        cmd.extend(["--brief", args.brief])
    if args.visual_elements:
        cmd.extend(["--visual-elements", args.visual_elements])
    if args.prompt_file:
        cmd.extend(["--prompt-file", args.prompt_file])
    if args.negative_prompt_file:
        cmd.extend(["--negative-prompt-file", args.negative_prompt_file])
    if args.dual_prompts:
        cmd.extend(["--dual-prompts", args.dual_prompts])
    if args.production_plan:
        cmd.extend(["--production-plan", args.production_plan])
    if args.ai_plan:
        cmd.extend(["--ai-plan", args.ai_plan])
    if args.token:
        cmd.extend(["--token", args.token])
    if args.libtv_key:
        cmd.extend(["--libtv-key", args.libtv_key])
    if args.neo_model:
        cmd.extend(["--neo-model", args.neo_model])
    if args.neo_size:
        cmd.extend(["--neo-size", args.neo_size])
    if args.max_retries is not None:
        cmd.extend(["--max-retries", str(args.max_retries)])
    if args.crop_inset is not None:
        cmd.extend(["--crop-inset", str(args.crop_inset)])
    if args.construct_ai_request:
        cmd.append("--construct-ai-request")
    if args.no_tile_repair:
        cmd.append("--no-tile-repair")
    if args.reuse_cache:
        cmd.append("--reuse-cache")
    if args.skip_collection_selection:
        cmd.append("--skip-collection-selection")
    if args.dual_source:
        cmd.append("--dual-source")
    if args.multi_scheme:
        cmd.append("--multi-scheme")
    if args.max_schemes is not None:
        cmd.extend(["--max-schemes", str(args.max_schemes)])
    if args.user_prompt:
        cmd.extend(["--user-prompt", args.user_prompt])
    if args.selected_collection:
        cmd.extend(["--selected-collection", args.selected_collection])
    if args.require_theme_image:
        cmd.append("--require-theme-image")
    if args.font_file:
        cmd.extend(["--font-file", args.font_file])

    return cmd


def run_end_to_end(cmd: list[str]) -> int:
    """运行端到端脚本，返回 exit code。"""
    result = subprocess.run(cmd, capture_output=True, text=True)
    # 将 stdout/stderr 透传
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="轮询驱动端到端自动化（解决双源看板生成超时问题）"
    )

    # ==== 以下参数与端到端自动化.py 保持一致，透传 ====
    parser.add_argument("--theme-image", action="append", default=[], help="主题/参考图像（可重复）")
    parser.add_argument("--theme-images", default="", help="多张主题/参考图像")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--collection-board", default="", help="已有的面料看板")
    parser.add_argument("--texture-set", default="", help="已有面料组合")
    parser.add_argument("--pattern", default="", help="透明纸样 mask PNG/WebP")
    parser.add_argument("--garment-type", default="", help="服装类型")
    parser.add_argument("--template", default="", help="模板编号")
    parser.add_argument("--mode", default="standard", choices=["fast", "standard", "production", "legacy"])
    parser.add_argument("--brief", default="", help="商业设计简报路径")
    parser.add_argument("--visual-elements", default="", help="已完成的 visual_elements.json")
    parser.add_argument("--prompt-file", default="", help="看板生成提示词文件")
    parser.add_argument("--negative-prompt-file", default="", help="看板生成反向提示词文件")
    parser.add_argument("--dual-prompts", default="", help="dual_collection_prompts.json 路径")
    parser.add_argument("--production-plan", default="", help="已提供的生产规划")
    parser.add_argument("--ai-plan", default="", help="AI 填充计划路径")
    parser.add_argument("--token", default="", help="Neodomain 访问令牌")
    parser.add_argument("--libtv-key", default="", help="libtv Access Key")
    parser.add_argument("--neo-model", default="gemini-3-pro-image-preview")
    parser.add_argument("--neo-size", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--crop-inset", type=int, default=None)
    parser.add_argument("--construct-ai-request", action="store_true")
    parser.add_argument("--no-tile-repair", action="store_true")
    parser.add_argument("--reuse-cache", action="store_true")
    parser.add_argument("--skip-collection-selection", action="store_true")
    parser.add_argument("--dual-source", action="store_true")
    parser.add_argument("--multi-scheme", action="store_true")
    parser.add_argument("--max-schemes", type=int, default=None)
    parser.add_argument("--user-prompt", default="", help="用户补充说明")
    parser.add_argument("--selected-collection", default="", help="已选择的 selected_variants.json")
    parser.add_argument("--require-theme-image", action="store_true")
    parser.add_argument("--font-file", default="", help="字体文件路径")

    # ==== 轮询驱动特有参数 ====
    parser.add_argument("--interval", type=int, default=30, help="轮询间隔秒数（默认 30）")
    parser.add_argument("--max-wait", type=int, default=1200, help="最大等待秒数（默认 1200 = 20 分钟）")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")

    args = parser.parse_args()

    cmd = build_end_to_end_cmd(args)

    start_time = time.time()
    attempt = 0

    while True:
        attempt += 1
        elapsed = int(time.time() - start_time)
        remaining = args.max_wait - elapsed

        if remaining <= 0:
            print(f"\n[轮询驱动] 已达最大等待时间 {args.max_wait} 秒，退出。", file=sys.stderr)
            return 1

        if args.verbose or attempt > 1:
            print(f"\n[轮询驱动] 第 {attempt} 次尝试（已等待 {elapsed}s，剩余 {remaining}s）")

        rc = run_end_to_end(cmd)

        if rc == 0:
            print(f"\n[轮询驱动] ✅ 端到端脚本成功完成（第 {attempt} 次尝试）")
            return 0
        elif rc == EXIT_DUAL_SOURCE_IN_PROGRESS:
            print(f"[轮询驱动] ⏳ 双源看板生成进行中，等待 {args.interval} 秒后重试...")
            time.sleep(args.interval)
            continue
        else:
            print(f"\n[轮询驱动] ❌ 端到端脚本失败（exit code={rc}）", file=sys.stderr)
            return rc


if __name__ == "__main__":
    raise SystemExit(main())
