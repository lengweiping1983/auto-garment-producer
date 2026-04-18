#!/usr/bin/env python3
"""
应用 AI 生产规划输出，将 ai_production_plan.json 拆解为现有下游格式：
- garment_map.json（兼容 部位映射.py 输出）
- ai_piece_fill_plan.json（兼容 构造审美请求.py 输出）

这样下游脚本（创建填充计划.py、渲染裁片.py 等）无需修改。
"""
import argparse
import json
import sys
from pathlib import Path


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def apply_production_plan(plan_path: Path, out_dir: Path) -> tuple[Path, Path]:
    """拆解 ai_production_plan.json 为 garment_map.json + ai_piece_fill_plan.json。"""
    plan = load_json(plan_path)

    # 尝试读取 pieces.json 获取 pattern_orientation 等几何信息
    pieces_data = {}
    pieces_path = out_dir / "pieces.json"
    if pieces_path.exists():
        try:
            pieces_payload = load_json(pieces_path)
            for p in pieces_payload.get("pieces", []):
                pieces_data[p["piece_id"]] = p
        except Exception:
            pass

    # 1. 提取 garment_map
    garment_map = plan.get("garment_map", {})
    # 确保有标准字段
    if "map_id" not in garment_map:
        garment_map["map_id"] = "ai_garment_map_v1"
    if "method" not in garment_map:
        garment_map["method"] = "ai_production_plan_extraction"
    if "confidence" not in garment_map:
        pieces = garment_map.get("pieces", [])
        if pieces:
            avg_conf = sum(p.get("confidence", 0.5) for p in pieces) / len(pieces)
            garment_map["confidence"] = round(avg_conf, 2)
        else:
            garment_map["confidence"] = 0.5

    # 确保每个 piece 有必要的字段（兼容 创建填充计划.py 的期望）
    for p in garment_map.get("pieces", []):
        if "garment_role" not in p:
            p["garment_role"] = "unknown"
        if "zone" not in p:
            p["zone"] = "detail"
        if "symmetry_group" not in p:
            p["symmetry_group"] = ""
        if "same_shape_group" not in p:
            p["same_shape_group"] = ""
        if "texture_direction" not in p:
            p["texture_direction"] = ""
        if "confidence" not in p:
            p["confidence"] = 0.5
        if "needs_ai_review" not in p:
            p["needs_ai_review"] = p.get("confidence", 0.5) < 0.6
        # 透传 pieces.json 中的 pattern_orientation
        source = pieces_data.get(p.get("piece_id", ""))
        if source and "pattern_orientation" in source:
            p["pattern_orientation"] = source["pattern_orientation"]
            p["orientation_confidence"] = source.get("orientation_confidence", 0)
            p["orientation_reason"] = source.get("orientation_reason", "")

    garment_map_path = out_dir / "garment_map.json"
    garment_map_path.write_text(json.dumps(garment_map, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2. 提取 piece_fill_plan
    fill_plan = plan.get("piece_fill_plan", {})
    # 确保有标准字段
    if "plan_id" not in fill_plan:
        fill_plan["plan_id"] = "ai_piece_fill_plan_v1"
    if "locked" not in fill_plan:
        fill_plan["locked"] = False

    # 确保每个 piece 有必要的字段
    for p in fill_plan.get("pieces", []):
        if "piece_id" not in p:
            continue
        if "base" not in p:
            p["base"] = None
        if "overlay" not in p:
            p["overlay"] = None
        if "trim" not in p:
            p["trim"] = None
        if "texture_direction" not in p:
            p["texture_direction"] = ""
        if "reason" not in p:
            p["reason"] = ""
        # 确保 base 有 fill_type
        base = p.get("base")
        if isinstance(base, dict) and "fill_type" not in base:
            if base.get("texture_id"):
                base["fill_type"] = "texture"
            elif base.get("solid_id"):
                base["fill_type"] = "solid"
            else:
                base["fill_type"] = "texture"

    # art_direction 必须存在
    if "art_direction" not in fill_plan:
        # 从 pieces 推断 hero
        hero_ids = [p["piece_id"] for p in fill_plan.get("pieces", []) if (p.get("overlay") or {}).get("fill_type") == "motif"]
        fill_plan["art_direction"] = {
            "strategy": "AI 生产规划输出",
            "hero_piece_ids": hero_ids,
            "notes": [],
        }

    fill_plan_path = out_dir / "ai_piece_fill_plan.json"
    fill_plan_path.write_text(json.dumps(fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 额外：输出 asset_approval_request.json（若 production 模式需要）
    risk_notes = plan.get("risk_notes", [])
    if risk_notes:
        approval = {
            "request_id": "ai_plan_risk_review_v1",
            "source_plan": str(plan_path.resolve()),
            "risk_notes": risk_notes,
            "message": "AI 生产规划中标记了风险项，建议人工复核。",
        }
        approval_path = out_dir / "asset_approval_request.json"
        approval_path.write_text(json.dumps(approval, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[风险标记] AI 规划中发现 {len(risk_notes)} 项风险，已写入: {approval_path}")

    return garment_map_path, fill_plan_path


def apply_multi_production_plan(plan_path: Path, out_dir: Path) -> list[dict]:
    """拆解 ai_multi_production_plan.json 为多个独立 scheme 文件。
    返回 scheme 元数据列表，用于下游遍历渲染。"""
    plan = load_json(plan_path)
    schemes = plan.get("schemes", [])

    if not schemes:
        # fallback：当作单方案处理
        gm_path, fp_path = apply_production_plan(plan_path, out_dir)
        return [{"scheme_id": "scheme_01", "suffix": "", "garment_map": str(gm_path), "fill_plan": str(fp_path)}]

    # 预加载标准模式的 garment_map 作为 fallback（多方案 AI 通常不输出 garment_map）
    fallback_garment_map = None
    for fallback_path in [out_dir / "garment_map.json", out_dir / "ai_production_plan.json"]:
        if fallback_path.exists():
            try:
                data = load_json(fallback_path)
                if "garment_map" in data:
                    fallback_garment_map = data["garment_map"]
                elif "pieces" in data:
                    fallback_garment_map = data
                if fallback_garment_map and fallback_garment_map.get("pieces"):
                    break
            except Exception:
                pass

    results = []
    for idx, scheme in enumerate(schemes, 1):
        scheme_id = scheme.get("scheme_id", f"scheme_{idx:02d}")
        suffix = f"_{scheme_id}"

        # 提取并写入 garment_map
        garment_map = scheme.get("garment_map", {})
        # 如果 AI 未输出 garment_map pieces，复用标准模式的 garment_map
        if not garment_map.get("pieces") and fallback_garment_map:
            garment_map = dict(fallback_garment_map)
            garment_map["map_id"] = f"ai_garment_map_{scheme_id}"
            garment_map["method"] = "ai_multi_production_plan_extraction"
        if "map_id" not in garment_map:
            garment_map["map_id"] = f"ai_garment_map_{scheme_id}"
        if "method" not in garment_map:
            garment_map["method"] = "ai_multi_production_plan_extraction"
        gm_path = out_dir / f"garment_map{suffix}.json"
        gm_path.write_text(json.dumps(garment_map, ensure_ascii=False, indent=2), encoding="utf-8")

        # 提取并写入 piece_fill_plan
        fill_plan = scheme.get("piece_fill_plan", {})
        if "plan_id" not in fill_plan:
            fill_plan["plan_id"] = f"ai_piece_fill_plan_{scheme_id}"
        if "locked" not in fill_plan:
            fill_plan["locked"] = False
        fp_path = out_dir / f"ai_piece_fill_plan{suffix}.json"
        fp_path.write_text(json.dumps(fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")

        scheme_meta = {
            "scheme_id": scheme_id,
            "suffix": suffix,
            "garment_map": str(gm_path.resolve()),
            "fill_plan": str(fp_path.resolve()),
        }
        for key in ("design_positioning", "strategy_note", "asset_mix_summary", "diversity_tags"):
            if key in scheme:
                scheme_meta[key] = scheme[key]
        results.append(scheme_meta)

    # 写入 schemes 元数据索引
    meta_path = out_dir / "schemes_meta.json"
    meta_payload = {"schemes": results}
    for key in ("portfolio_notes", "asset_coverage", "risk_notes"):
        if key in plan:
            meta_payload[key] = plan[key]
    meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[多方案拆解] 共 {len(results)} 套 scheme 已拆解:")
    for r in results:
        print(f"  {r['scheme_id']}: garment_map={r['garment_map']}, fill_plan={r['fill_plan']}")

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="应用 AI 生产规划，拆解为 garment_map + fill_plan。支持多方案模式。")
    parser.add_argument("--production-plan", required=True, help="ai_production_plan.json 或 ai_multi_production_plan.json 路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--multi-scheme", action="store_true", help="输入为 ai_multi_production_plan.json，拆解为多个独立 scheme 文件")
    args = parser.parse_args()

    plan_path = Path(args.production_plan)
    if not plan_path.exists():
        print(f"错误: 生产规划文件不存在: {plan_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.multi_scheme:
        schemes = apply_multi_production_plan(plan_path, out_dir)
        print(json.dumps({
            "schemes_meta": str((out_dir / "schemes_meta.json").resolve()),
            "scheme_count": len(schemes),
            "status": "applied_multi",
        }, ensure_ascii=False, indent=2))
    else:
        garment_map_path, fill_plan_path = apply_production_plan(plan_path, out_dir)
        print(json.dumps({
            "garment_map": str(garment_map_path.resolve()),
            "ai_piece_fill_plan": str(fill_plan_path.resolve()),
            "status": "applied",
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
