#!/usr/bin/env python3
"""
构造 3D 服装部位识别请求，供子 Agent 根据纸样总览图识别每个裁片的部位角色。

使用方式：
1. 运行本脚本生成选择任务文件（ai_garment_map_prompt.txt）
2. 外层 Agent 启动 coder 子Agent，传入该 prompt + piece_overview.png
3. 子Agent输出 ai_garment_map.json
4. 部位映射.py 优先使用 ai_garment_map.json，无则 fallback 到几何启发

输入：
- pieces.json（裁片几何信息）
- piece_overview.png（裁片总览图路径）
- commercial_design_brief.json（含 garment_type）

输出：
- ai_garment_map_prompt.txt：面向子Agent的自然语言识别任务
- ai_garment_map_request.json：结构化请求摘要
- ai_garment_map.json（在 --selected 模式下）：验证并输出 garment_map.json
"""
import argparse
import json
import sys
from pathlib import Path


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_identification_prompt(pieces: list[dict], garment_type: str, overview_path: str) -> str:
    """构造面向子Agent的部位识别 prompt。"""
    type_hints = {
        # 英文 key
        "shirt": "衬衫/上衣类：通常有前片、后片、2个袖子、领、袖口。前片可能有口袋。",
        "dress": "连衣裙：前片、后片、袖子（或无袖）、裙摆、可能有育克或拼接。",
        "jacket": "外套：前片、后片、2个袖子、领、门襟、可能有口袋盖。trim较宽。",
        "coat": "大衣：前片、后片、2个袖子、领、下摆。body区比例大。",
        "pants": "裤装：前片、后片、腰头、裤脚。无袖子。可能有口袋。",
        "children": "童装：部位较小，比例紧凑。hero通常在胸口。",
        "children outerwear set": "儿童外套套装：通常有前片、后片、2个袖子、领、门襟、下摆。",
        "commercial apparel": "通用成衣：根据几何特征判断部位。",
        # 中文 key（常见输入）
        "衬衫": "衬衫/上衣类：通常有前片、后片、2个袖子、领、袖口。前片可能有口袋。",
        "上衣": "衬衫/上衣类：通常有前片、后片、2个袖子、领、袖口。前片可能有口袋。",
        "连衣裙": "连衣裙：前片、后片、袖子（或无袖）、裙摆、可能有育克或拼接。",
        "外套": "外套：前片、后片、2个袖子、领、门襟、可能有口袋盖。trim较宽。",
        "大衣": "大衣：前片、后片、2个袖子、领、下摆。body区比例大。",
        "裤装": "裤装：前片、后片、腰头、裤脚。无袖子。可能有口袋。",
        "裤子": "裤装：前片、后片、腰头、裤脚。无袖子。可能有口袋。",
        "童装": "童装：部位较小，比例紧凑。hero通常在胸口。",
        "儿童外套套装": "儿童外套套装：通常有前片、后片、2个袖子、领、门襟、下摆。",
        "儿童外套": "儿童外套套装：通常有前片、后片、2个袖子、领、门襟、下摆。",
        "套装": "套装：可能包含上衣+下装，或外套+裤子组合。",
        "女装": "女装通用：根据几何特征判断部位。",
        "男装": "男装通用：根据几何特征判断部位。",
        "通用成衣": "通用成衣：根据几何特征判断部位。",
    }
    gt_lower = garment_type.lower().strip()
    type_hint = type_hints.get(gt_lower, f"{garment_type}类服装，请根据裁片形状和上下文推断")

    lines = [
        "你是一位专业服装打版师，精通从纸样总览图识别每个裁片的服装部位。",
        f"以下是一个 {garment_type} 的纸样排版图，共有 {len(pieces)} 个裁片。",
        f"预览图路径: {overview_path}",
        "【强制要求】你必须先使用 see_image 查看上述预览图，再进行任何判断。未看图直接输出的识别结果无效。",
        "",
        "请为每个裁片标注其服装部位角色、所属区域、对称关系。",
        "注意：看图时关注裁片的实际形状、大小、位置关系，不要仅依赖下方几何数据。"
        "",
        "===== 服装类型指导 =====",
        f"类型: {garment_type}",
        f"特征: {type_hint}",
        "",
        "===== 裁片几何信息 =====",
    ]

    for p in pieces:
        aspect = round(p["width"] / max(1, p["height"]), 2)
        lines.append(
            f"{p['piece_id']}: 位置({p['source_x']},{p['source_y']}) "
            f"尺寸{p['width']}×{p['height']} 面积{p['area']} 宽高比{aspect}"
        )

    lines.extend([
        "",
        "===== 部位角色定义 =====",
        "- front_hero: 前片主卖点区（通常是最大或最显眼的前片）",
        "- back_body: 后片主体",
        "- secondary_body: 次要大身裁片（如侧片、拼接片）",
        "- sleeve_pair: 成对袖子（左右对称）",
        "- sleeve_or_side_panel: 单袖子或侧片",
        "- side_or_long_panel: 长条侧片或装饰条",
        "- trim_strip: 细窄饰边",
        "- collar_or_upper_trim: 领部或上部饰边",
        "- hem_or_lower_trim: 下摆或下部饰边",
        "- matched_panel: 成对相同形状裁片（如口袋盖）",
        "- small_detail: 小型细节裁片",
        "",
        "===== 区域定义 =====",
        "- body: 大身区（前片、后片、侧片）",
        "- secondary: 副区（袖子、侧条、拼接）",
        "- trim: 饰边区（领、袖口、下摆、门襟）",
        "- detail: 细节区（口袋盖、小装饰）",
        "",
        "===== 识别原则 =====",
        "1. 最大裁片不一定是 front_hero——带育克或拼接的款式，后片可能更大。",
        "2. 画布位置不是部位位置的可靠指标——纸样排版位置取决于排料效率。",
        "3. 窄长条不一定是 trim——可能是腰带、袖头、侧缝装饰。",
        "4. 同尺寸+镜像位置可能是袖对，也可能是口袋对、装饰条对。",
        "5. 服装类型决定部位集合：裤装无袖子，童装部位较小。",
        "6. 纹理方向（texture_direction）由你根据裁片形状 + 服装类型 + 面料方向性判断：",
        "   - 条纹衬衫前片常用竖纹（显瘦），body 区可设为 longitudinal",
        "   - 苏格兰格必须正向（不能斜），所有裁片应同向",
        "   - 印花面料有方向性主体（花朵向上）时，相关裁片应同向",
        "   - 同 symmetry_group / same_shape_group 的裁片方向必须一致",
        "7. 经向（grain_direction）基于服装设计学知识判断：",
        "   - body 区裁片（前片、后片）通常 vertical（人体上下方向）",
        "   - 袖子通常 vertical（手臂方向）",
        "   - 饰边/腰带沿长边方向（窄长条=horizontal，短宽条=vertical）",
        "",
        "===== 输出格式 =====",
        "请返回严格的 JSON，不要任何解释文字：",
        "",
        json.dumps({
            "pieces": [
                {
                    "piece_id": "piece_001",
                    "garment_role": "front_hero",
                    "zone": "body",
                    "symmetry_group": "",
                    "same_shape_group": "",
                    "texture_direction": "transverse",
                    "grain_direction": "vertical",
                    "confidence": 0.85,
                    "reason": "最大裁片，位于左侧，判断为前片",
                }
            ],
        }, ensure_ascii=False, indent=2),
        "",
        "要求：",
        "- piece_id 必须覆盖所有裁片",
        "- confidence 用 0–1 表示确信度",
        "- alternatives 列出备选角色（最多2个）",
        "- reason 用中文说明判断依据",
    ])
    return "\n".join(lines)


def merge_ai_map(pieces_payload: dict, ai_map: dict) -> dict:
    """将AI子Agent输出的部位识别结果与裁片几何信息合并，生成完整的 garment_map。
    同时进行程序兜底校验。"""
    pieces = pieces_payload.get("pieces", [])
    by_id = {p["piece_id"]: p for p in pieces}
    ai_pieces = {p["piece_id"]: p for p in ai_map.get("pieces", [])}

    result = []
    issues = []
    total_pieces = len(pieces)
    ai_pieces_list = ai_map.get("pieces", [])
    ai_ids = {p["piece_id"] for p in ai_pieces_list}

    # 1. 未识别比例检查：>20% 时报错
    unrecognized_count = total_pieces - len(ai_ids)
    unrecognized_ratio = unrecognized_count / max(1, total_pieces)
    if unrecognized_ratio > 0.20:
        issues.append({
            "type": "too_many_unrecognized",
            "severity": "high",
            "message": f"AI 未识别裁片比例过高：{unrecognized_count}/{total_pieces} ({round(unrecognized_ratio*100)}%)。请重新启动子Agent，明确要求查看图片后输出所有裁片。",
        })

    # 2. 必须有 body 区裁片
    body_count = sum(1 for p in ai_pieces_list if p.get("zone") == "body")
    if body_count == 0:
        issues.append({"type": "missing_body", "severity": "high", "message": "AI识别结果缺少 body 区裁片，请检查"})

    # 3. 不允许两个及以上 hero
    hero_count = sum(1 for p in ai_pieces_list if p.get("garment_role") == "front_hero")
    if hero_count > 2:
        issues.append({"type": "too_many_heroes", "severity": "medium", "message": f"发现 {hero_count} 个 hero，建议保留1-2个"})

    for piece in pieces:
        pid = piece["piece_id"]
        ai = ai_pieces.get(pid, {})
        if ai:
            entry = {
                "piece_id": pid,
                "garment_role": ai.get("garment_role", "small_detail"),
                "zone": ai.get("zone", "detail"),
                "symmetry_group": ai.get("symmetry_group", ""),
                "same_shape_group": ai.get("same_shape_group", ""),
                "direction_degrees": 90 if piece["width"] / max(1, piece["height"]) >= 1.8 else 0,
                "confidence": ai.get("confidence", 0.5),
                "reason": ai.get("reason", "AI识别"),
                "alternatives": ai.get("alternatives", []),
            }
        else:
            # fallback
            aspect = piece["width"] / max(1, piece["height"])
            entry = {
                "piece_id": pid,
                "garment_role": "small_detail",
                "zone": "detail",
                "symmetry_group": "",
                "same_shape_group": "",
                "direction_degrees": 90 if aspect >= 1.8 else 0,
                "confidence": 0.5,
                "reason": "AI未识别，回退到默认值",
                "alternatives": [],
            }

        # texture_direction：优先使用AI输出；AI未提供时留空，不设程序硬编码默认值
        ai_dir = ai.get("texture_direction", "")
        if ai_dir in ("transverse", "longitudinal"):
            entry["texture_direction"] = ai_dir
        else:
            entry["texture_direction"] = ""

        # grain_direction：优先使用AI输出；AI未提供时程序兜底推断
        grain = ai.get("grain_direction", "")
        if grain in ("vertical", "horizontal", "bias_45"):
            entry["grain_direction"] = grain
        else:
            # 基于 garment_role 的设计学知识兜底（与画布 aspect 无关）
            if role in ("sleeve_pair", "sleeve_or_side_panel"):
                entry["grain_direction"] = "vertical"  # 袖子 grain 沿手臂方向
            elif role in ("trim_strip", "waistband", "cuff", "neckline_rib"):
                entry["grain_direction"] = "horizontal"
            elif role in ("collar_or_upper_trim", "hem_or_lower_trim", "pocket_flap", "yoke"):
                entry["grain_direction"] = "horizontal"
            elif zone == "body" or role in ("front_hero", "back_body", "secondary_body", "side_or_long_panel"):
                entry["grain_direction"] = "vertical"
            else:
                entry["grain_direction"] = "vertical"

        # 低置信度裁片标记为需 AI 重点审核
        if entry.get("confidence", 0.7) < 0.6:
            entry["needs_ai_review"] = True

        result.append(entry)

    return {"pieces": result, "validation_issues": issues}


def main() -> int:
    parser = argparse.ArgumentParser(description="构造服装部位识别请求，或验证AI识别结果。")
    parser.add_argument("--pieces", required=True, help="pieces.json 路径")
    parser.add_argument("--overview", default="", help="piece_overview.png 路径")
    parser.add_argument("--brief", default="", help="commercial_design_brief.json 路径（含 garment_type）")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--selected", default="", help="子Agent输出的 ai_garment_map.json 路径。若提供，验证并输出 garment_map.json。")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pieces_payload = load_json(args.pieces)
    pieces = pieces_payload.get("pieces", [])

    # 读取 garment_type
    garment_type = "commercial apparel"
    if args.brief:
        try:
            brief = load_json(args.brief)
            garment_type = brief.get("garment_type", garment_type)
        except Exception:
            pass

    overview_path = args.overview or str(out_dir / "piece_overview.png")

    # 模式A：生成识别请求
    if not args.selected:
        prompt = build_identification_prompt(pieces, garment_type, overview_path)
        prompt_path = out_dir / "ai_garment_map_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        request_summary = {
            "request_id": "garment_map_identification_v1",
            "pieces_path": str(Path(args.pieces).resolve()),
            "overview_path": str(Path(overview_path).resolve()),
            "garment_type": garment_type,
            "prompt_path": str(prompt_path.resolve()),
            "expected_output": str((out_dir / "ai_garment_map.json").resolve()),
            "piece_count": len(pieces),
        }
        request_path = out_dir / "ai_garment_map_request.json"
        request_path.write_text(json.dumps(request_summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps({
            "部位识别请求": str(request_path.resolve()),
            "子Agent提示词": str(prompt_path.resolve()),
            "预期输出": request_summary["expected_output"],
            "说明": "请启动 coder 子Agent，传入 ai_garment_map_prompt.txt + piece_overview.png，要求输出严格的 ai_garment_map.json",
        }, ensure_ascii=False, indent=2))
        return 0

    # 模式B：验证AI识别结果并输出 garment_map.json
    selected_path = Path(args.selected)
    if not selected_path.exists():
        print(f"错误: AI识别结果不存在: {selected_path}", file=sys.stderr)
        return 1

    ai_map = load_json(selected_path)
    merged = merge_ai_map(pieces_payload, ai_map)

    gm_path = out_dir / "garment_map.json"
    gm_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    issues = merged.get("validation_issues", [])
    if issues:
        print(f"[警告] 程序兜底发现 {len(issues)} 个问题：")
        for issue in issues:
            print(f"  [{issue['type']}] {issue['message']}")

    print(json.dumps({
        "garment_map": str(gm_path.resolve()),
        "piece_count": len(merged["pieces"]),
        "validation_issues": len(issues),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
