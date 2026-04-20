#!/usr/bin/env python3
"""
端到端自动化：

1. 生成或接收 Neo AI 单纹理面料资产，并独立生成透明主图。
2. 组装面料资产。
3. 构建面料组合.json。
4. 复用固定模板裁片和部位映射。
5. 构建填充计划。
6. 渲染透明裁片 PNG、预览图与清单。

Neo AI 负责创作 artwork。本脚本准备可用资产并以确定性方式渲染到裁片中。
"""
import argparse
import concurrent.futures
import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image


SKILL_DIR = Path(__file__).resolve().parents[1]
NEO_AI_SCRIPT = SKILL_DIR.parent / "neo-ai" / "scripts" / "generate_texture_collection_board.py"
NEO_UPLOAD_SCRIPT = SKILL_DIR.parent / "neo-ai" / "scripts" / "upload_oss.py"
DEFAULT_TEXTURE_IDS = ("main", "secondary", "accent_light")
HERO_NEGATIVE_PROMPT = (
    "text, labels, captions, titles, typography, words, letters, signage, logo, watermark, "
    "plain light box, colored background box, filled rectangle, background art, scenery, landscape, environment, "
    "checkerboard transparency preview, fake transparency grid, "
    "full illustration scene, poster composition, sticker sheet, garment mockup, fashion model, mannequin, "
    "person wearing garment, product photo, lookbook, semi-transparent full-image patch"
)

# 导入模板加载器
sys.path.insert(0, str(SKILL_DIR / "scripts"))
from prompt_blocks import build_single_texture_prompt_en, build_texture_2x2_board_prompt_en, build_transparent_hero_prompt_en
try:
    from prompt_sanitizer import sanitize_prompt_with_report
except Exception:
    sanitize_prompt_with_report = None
try:
    from template_loader import (
        normalize_piece_asset_paths,
        relative_json_metadata_path,
        resolve_template_assets,
    )
    HAS_TEMPLATE_LOADER = True
except Exception:
    def relative_json_metadata_path(target: str | Path, owner_json_path: str | Path) -> str:
        return os.path.relpath(Path(target).resolve(), Path(owner_json_path).resolve().parent)

    HAS_TEMPLATE_LOADER = False

try:
    from theme_image_resolver import resolve_theme_images
except Exception:
    resolve_theme_images = None

try:
    from theme_front_splitter import create_front_split_assets, inject_front_split_motifs
except Exception:
    create_front_split_assets = None
    inject_front_split_motifs = None


def file_sha256(path: str | Path) -> str:
    """计算文件的 SHA256 哈希。"""
    p = Path(path)
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def files_sha256(paths: list[str | Path]) -> list[str]:
    return [file_sha256(path) for path in paths]


def _flat_pixels(image: Image.Image):
    """Return a flat pixel iterator across Pillow versions.

    Pillow 12+ recommends get_flattened_data(); older versions only expose
    getdata(). Keep the compatibility fallback in one place.
    """
    if hasattr(image, "get_flattened_data"):
        return image.get_flattened_data()
    return image.getdata()


def dict_sha256(data: dict) -> str:
    """计算字典的确定性 SHA256 哈希。"""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_dir(out_dir: Path) -> Path:
    """返回缓存目录路径。"""
    return out_dir / ".cache"


def cache_lookup(out_dir: Path, stage: str, input_hash: dict) -> Path | None:
    """按 input_hash 查找缓存。命中时返回缓存文件路径，否则返回 None。"""
    cd = cache_dir(out_dir)
    if not cd.exists():
        return None
    key = dict_sha256(input_hash)
    meta_path = cd / f"{stage}_{key}.meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stored_hash = meta.get("input_hash")
        if stored_hash != input_hash:
            return None
        output_path = cd / meta.get("output_file", "")
        if output_path.exists():
            return output_path
    except Exception as exc:
        print(f"[缓存警告] 读取 {stage} 缓存失败: {exc}")
    return None


def cache_save(out_dir: Path, stage: str, input_hash: dict, output_path: Path) -> None:
    """将输出文件保存到缓存。"""
    cd = cache_dir(out_dir)
    cd.mkdir(parents=True, exist_ok=True)
    key = dict_sha256(input_hash)
    cached_file = cd / f"{stage}_{key}{output_path.suffix}"
    cached_file.write_bytes(output_path.read_bytes())
    meta = {
        "stage": stage,
        "input_hash": input_hash,
        "output_file": str(cached_file.name),
        "created_at": datetime.datetime.now().isoformat(),
    }
    meta_path = cd / f"{stage}_{key}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_geometry_hints(pieces_path: Path, out_path: Path) -> None:
    """基于裁片几何数据生成 geometry_hints.json，供 AI 决策参考。"""
    try:
        data = json.loads(pieces_path.read_text(encoding="utf-8"))
        pieces = data.get("pieces", [])
        if not pieces:
            return
        largest_area = max(p.get("area", 0) for p in pieces)
        # 计算中心点和对称性候选
        xs = [p.get("x", 0) + p.get("width", 0) / 2 for p in pieces]
        ys = [p.get("y", 0) + p.get("height", 0) / 2 for p in pieces]
        cx = sum(xs) / len(xs) if xs else 0
        cy = sum(ys) / len(ys) if ys else 0
        hints = []
        for p in sorted(pieces, key=lambda x: x.get("area", 0), reverse=True):
            area = p.get("area", 0)
            aspect = p.get("width", 1) / max(1, p.get("height", 1))
            px = p.get("x", 0) + p.get("width", 0) / 2
            py = p.get("y", 0) + p.get("height", 0) / 2
            area_ratio = area / max(1, largest_area)
            # 简单的几何角色推断（仅为 AI 提供候选，不强制）
            geo_role = "unknown"
            if area_ratio > 0.6:
                geo_role = "body_large"
            elif area_ratio > 0.3:
                geo_role = "body_medium"
            elif aspect >= 3 or aspect <= 0.34:
                geo_role = "strip_or_trim"
            elif area_ratio < 0.12:
                geo_role = "small_detail"
            else:
                geo_role = "panel"
            hint = {
                "piece_id": p["piece_id"],
                "area": area,
                "area_ratio": round(area_ratio, 3),
                "width": p.get("width", 0),
                "height": p.get("height", 0),
                "aspect_ratio": round(aspect, 2),
                "centroid": [round(px, 1), round(py, 1)],
                "relative_to_center": [round(px - cx, 1), round(py - cy, 1)],
                "geometry_role_hint": geo_role,
            }
            # 透传裁片方向信息（模板资产中若已提供）
            if "pattern_orientation" in p:
                hint["pattern_orientation"] = p["pattern_orientation"]
                hint["orientation_confidence"] = p.get("orientation_confidence", 0)
                hint["orientation_reason"] = p.get("orientation_reason", "")
            hints.append(hint)
        out_path.write_text(json.dumps({"pieces": hints, "center": [round(cx, 1), round(cy, 1)]}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[几何推断] geometry_hints 已生成: {out_path}")
    except Exception as exc:
        print(f"[警告] geometry_hints 生成失败: {exc}")


def build_production_context(
    args,
    out_dir: Path,
    pieces_path: Path | None = None,
    garment_map_path: Path | None = None,
    template_assets: dict | None = None,
) -> Path:
    """生成 production_context.json，统一索引所有输入和中间产物。"""
    ctx = {
        "input_hash": {},
        "paths": {},
        "computed": {},
        "created_at": datetime.datetime.now().isoformat(),
        "script_version": "2.0.0",
    }
    # 输入文件 hash
    theme_images = [str(p) for p in getattr(args, "theme_images", []) if str(p)]
    if theme_images:
        ctx["input_hash"]["theme_images"] = files_sha256(theme_images)
        ctx["paths"]["theme_images"] = [str(Path(p).resolve()) for p in theme_images]
    if args.theme_image:
        ctx["input_hash"]["theme_image"] = file_sha256(args.theme_image)
        ctx["paths"]["theme_image"] = str(Path(args.theme_image).resolve())
    ctx["input_hash"]["garment_type"] = args.garment_type
    ctx["input_hash"]["user_prompt"] = getattr(args, "user_prompt", "")
    ctx["input_hash"]["mode"] = getattr(args, "mode", "")
    ctx["input_hash"]["template"] = getattr(args, "template", "")
    if getattr(args, "brief", ""):
        ctx["input_hash"]["brief"] = file_sha256(args.brief)
        ctx["paths"]["input_brief"] = str(Path(args.brief).resolve())
    if getattr(args, "visual_elements", ""):
        ctx["input_hash"]["visual_elements"] = file_sha256(args.visual_elements)
        ctx["paths"]["input_visual_elements"] = str(Path(args.visual_elements).resolve())
    if getattr(args, "hero_motif_image", ""):
        ctx["input_hash"]["hero_motif_image"] = file_sha256(args.hero_motif_image)
        ctx["paths"]["hero_motif_image"] = str(Path(args.hero_motif_image).resolve())
    if getattr(args, "texture_set", ""):
        texture_set_path = Path(args.texture_set)
        if not texture_set_path.is_absolute():
            texture_set_path = texture_set_path.resolve()
        ctx["input_hash"]["texture_set"] = file_sha256(texture_set_path)
        ctx["paths"]["texture_set"] = str(texture_set_path)
    if template_assets:
        ctx["computed"]["template_assets_reused"] = True
        ctx["computed"]["template_id"] = template_assets.get("template_id", "")
        ctx["computed"]["template_source"] = template_assets.get("template_source", "")
        ctx["computed"]["original_garment_type"] = getattr(args, "garment_type", "")
        ctx["paths"]["template_asset_dir"] = template_assets.get("asset_dir", "")

    # 中间产物路径
    for name, fname in [
        ("texture_set", "texture_set.json"),
        ("visual_elements", "visual_elements.json"),
        ("brief", "commercial_design_brief.json"),
        ("geometry_hints", "geometry_hints.json"),
    ]:
        p = out_dir / fname
        if p.exists():
            ctx["paths"][name] = str(p.resolve())
    resolved_pieces_path = pieces_path or (out_dir / "pieces.json")
    if resolved_pieces_path.exists():
        ctx_path = out_dir / "production_context.json"
        ctx["paths"]["pieces_json"] = relative_json_metadata_path(resolved_pieces_path, ctx_path)
        try:
            pieces_payload = json.loads(resolved_pieces_path.read_text(encoding="utf-8"))
            if HAS_TEMPLATE_LOADER:
                pieces_payload = normalize_piece_asset_paths(pieces_payload, resolved_pieces_path)
            if pieces_payload.get("overview_image"):
                ctx["paths"]["piece_overview"] = relative_json_metadata_path(pieces_payload["overview_image"], ctx_path)
            if pieces_payload.get("prepared_pattern"):
                ctx["paths"]["prepared_pattern"] = relative_json_metadata_path(pieces_payload["prepared_pattern"], ctx_path)
        except Exception as exc:
            ctx.setdefault("warnings", []).append({
                "type": "pieces_metadata_read_failed",
                "path": str(resolved_pieces_path),
                "message": str(exc),
            })
    if garment_map_path and garment_map_path.exists():
        ctx["paths"]["garment_map"] = str(garment_map_path.resolve())
    elif (out_dir / "garment_map.json").exists():
        ctx["paths"]["garment_map"] = str((out_dir / "garment_map.json").resolve())

    # 计算字段
    if resolved_pieces_path.exists():
        try:
            pieces = json.loads(resolved_pieces_path.read_text(encoding="utf-8"))
            pc = pieces.get("pieces", [])
            ctx["computed"]["piece_count"] = len(pc)
            if pc:
                ctx["computed"]["largest_piece_area"] = max(p.get("area", 0) for p in pc)
        except Exception as exc:
            ctx.setdefault("warnings", []).append({
                "type": "pieces_summary_read_failed",
                "path": str(resolved_pieces_path),
                "message": str(exc),
            })

    ctx_path = out_dir / "production_context.json"
    ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return ctx_path


def load_json(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text = text.replace("False", "false").replace("True", "true")
        return json.loads(text)


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_step(cmd: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("运行:", " ".join(cmd))
    return subprocess.run(cmd, check=check, env=env)


def run_step_after_launch(
    cmd: list[str],
    env: dict | None = None,
    check: bool = True,
    on_launch=None,
) -> subprocess.CompletedProcess:
    print("运行:", " ".join(cmd))
    proc = subprocess.Popen(cmd, env=env)
    if on_launch:
        on_launch()
    rc = proc.wait()
    if check and rc:
        raise subprocess.CalledProcessError(rc, cmd)
    return subprocess.CompletedProcess(cmd, rc)


def latest_collection_board(output_dir: Path) -> Path:
    """在输出目录中找到最新的 Neo AI 生成图像。"""
    candidates = (
        sorted(output_dir.glob("collection_board_*.png"))
        + sorted(output_dir.glob("collection_board_*.jpg"))
        + sorted(output_dir.glob("collection_board_*.jpeg"))
        + sorted(output_dir.glob("collection_board_*.webp"))
    )
    if not candidates:
        raise RuntimeError(f"输出目录中未找到面料看板图像: {output_dir}")
    return candidates[-1]


def _sanitize_generation_prompt(
    prompt_text: str,
    *,
    texture_id: str,
    out_dir: Path,
    report_items: list[dict],
    prompt_role: str = "final",
) -> str:
    """Final preflight rewrite before a prompt is sent to Neo AI."""
    if not sanitize_prompt_with_report:
        return prompt_text
    report = sanitize_prompt_with_report(prompt_text, domain="fashion", prompt_role=prompt_role)
    if report.removed or report.replacements or report.warnings:
        payload = report.to_dict()
        payload["texture_id"] = texture_id
        report_items.append(payload)
    return report.sanitized_text


def _write_prompt_sanitization_report(out_dir: Path, report_items: list[dict]) -> None:
    if not report_items:
        return
    report_path = out_dir / "prompt_sanitization_report.json"
    payload = {
        "report_id": "prompt_sanitization_v1",
        "purpose": "记录最终提交给 Neo AI 前被安全改写或移除的提示词片段。",
        "items": report_items,
    }
    write_json(report_path, payload)
    print(f"[提示词安全] 已写入违禁词/敏感词处理报告: {report_path}")


def _sanitize_prompt_file_for_neo(prompt_file: str, output_dir: Path, texture_id: str = "neo_prompt") -> str:
    """Sanitize externally supplied prompt files at the Neo boundary."""
    if not prompt_file or not sanitize_prompt_with_report:
        return prompt_file
    src = Path(prompt_file)
    if not src.exists() or not src.is_file():
        return prompt_file
    text = src.read_text(encoding="utf-8")
    report_items: list[dict] = []
    sanitized = _sanitize_generation_prompt(
        text,
        texture_id=texture_id,
        out_dir=output_dir,
        report_items=report_items,
        prompt_role="final",
    )
    if sanitized == text:
        return prompt_file
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"{src.stem}.sanitized{src.suffix or '.txt'}"
    dest.write_text(sanitized, encoding="utf-8")
    _write_prompt_sanitization_report(output_dir, report_items)
    return str(dest)


def _build_generation_prompts_from_visual_elements(out_dir: Path, visual_elements_path: Path = None) -> tuple[dict[str, str], str]:
    """基于视觉分析结果构造三张单纹理 prompt 与独立透明主图 prompt。"""
    texture_prompts_path = out_dir / "texture_prompts.json"
    visual_path = visual_elements_path or (out_dir / "visual_elements.json")
    if not texture_prompts_path.exists() or not visual_path.exists():
        return {}, ""

    try:
        tp = json.loads(texture_prompts_path.read_text(encoding="utf-8"))
        ve_text = visual_path.read_text(encoding="utf-8")
        try:
            ve = json.loads(ve_text)
        except json.JSONDecodeError:
            ve_text = ve_text.replace("False", "false").replace("True", "true")
            ve = json.loads(ve_text)
    except Exception:
        return {}, ""

    # 按 texture_id 索引所有提示词
    prompts = {}
    for p in tp.get("prompts", []):
        prompts[p.get("texture_id", "")] = p.get("prompt", "")

    style = ve.get("style", {})
    family_contract = tp.get("family_contract", "")
    edge_contract = ve.get("hero_edge_contract", {})
    texture_prompts = {
        texture_id: build_single_texture_prompt_en(texture_id, prompts.get(texture_id, ""), style, family_contract)
        for texture_id in DEFAULT_TEXTURE_IDS
    }
    hero_prompt = build_transparent_hero_prompt_en(prompts.get("hero_motif_1", ""), style, edge_contract)
    return texture_prompts, hero_prompt


def _build_collection_prompt_from_visual_elements(out_dir: Path, visual_elements_path: Path = None) -> str:
    """Compatibility wrapper: return a legacy grouped texture prompt."""
    texture_prompts, _ = _build_generation_prompts_from_visual_elements(out_dir, visual_elements_path)
    return build_texture_2x2_board_prompt_en(texture_prompts, {})


def validate_board_colors(board_path: Path, palette: dict, threshold: int = 80) -> list[dict]:
    """验证 2×2 看板各面板颜色是否与 palette 协调。
    返回颜色偏差报告列表。"""
    from PIL import Image
    warnings = []
    if not palette:
        return warnings

    board = Image.open(board_path).convert("RGB")
    w, h = board.size
    div_x1 = w // 2
    div_y1 = h // 2

    panels = {
        "main": (0, 0, div_x1, div_y1),
        "secondary": (div_x1, 0, w, div_y1),
        "accent_light": (0, div_y1, div_x1, h),
    }

    def _hex_to_rgb(hex_str):
        from PIL import ImageColor
        return ImageColor.getrgb(hex_str)

    def _rgb_dist(c1, c2):
        return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5

    mapping = {
        "main": palette.get("primary", []),
        "secondary": palette.get("secondary", []),
        "accent_light": palette.get("accent", []) or palette.get("primary", []),
    }

    for tid, box in panels.items():
        crop = board.crop(box)
        sample = crop.resize((1, 1), Image.Resampling.LANCZOS)
        r, g, b = sample.getpixel((0, 0))
        actual = (r, g, b)

        candidates = mapping.get(tid, [])
        if not candidates:
            continue
        try:
            expected_rgb = _hex_to_rgb(candidates[0])
            dist = _rgb_dist(actual, expected_rgb)
            if dist > threshold:
                warnings.append({
                    "panel": tid,
                    "actual_rgb": actual,
                    "expected_hex": candidates[0],
                    "distance": round(dist, 1),
                    "severity": "high" if dist > 120 else "medium",
                })
        except Exception:
            continue

    return warnings


def _neo_generation_cmd(
    args: argparse.Namespace,
    output_dir: Path,
    prompt_file: str = "",
    negative_prompt: str = "",
    reference_images: list[str] | None = None,
) -> list[str]:
    safe_prompt_file = _sanitize_prompt_file_for_neo(prompt_file, output_dir, texture_id=output_dir.name)
    safe_negative_prompt = negative_prompt
    if negative_prompt and sanitize_prompt_with_report:
        safe_negative_prompt = sanitize_prompt_with_report(
            negative_prompt,
            domain="fashion",
            prompt_role="negative",
        ).sanitized_text
    cmd = [
        sys.executable,
        str(NEO_AI_SCRIPT),
        "--model",
        args.neo_model,
        "--size",
        args.neo_size,
        "--output-format",
        "png",
        "--output-dir",
        str(output_dir),
    ]
    if safe_prompt_file:
        cmd.extend(["--prompt-file", safe_prompt_file])
    if safe_negative_prompt:
        cmd.extend(["--negative-prompt", safe_negative_prompt])
    for ref in reference_images or []:
        cmd.extend(["--reference-image", ref])
    if args.num_images:
        cmd.extend(["--num-images", args.num_images])
    if args.token:
        cmd.extend(["--token", args.token])
    return cmd


def generate_board(args: argparse.Namespace, out_dir: Path, prompt_file: str = "", subdir: str = "neo_texture_board", negative_prompt: str = "") -> Path:
    """调用 Neo AI 生成单张图片。保留给旧看板入口和透明主图复用。"""
    board_dir = out_dir / subdir
    board_dir.mkdir(parents=True, exist_ok=True)
    cmd = _neo_generation_cmd(args, board_dir, prompt_file or getattr(args, "texture_prompt_file", "") or getattr(args, "prompt_file", ""), negative_prompt=negative_prompt)
    env = os.environ.copy()
    run_step(cmd, env=env)
    return latest_collection_board(board_dir)


def _token_for_neo(args: argparse.Namespace) -> str:
    return args.token or os.environ.get("NEODOMAIN_ACCESS_TOKEN", "")


def _upload_reference_images_for_neo(args: argparse.Namespace, out_dir: Path) -> list[str]:
    """Upload local theme images to OSS and return URLs for Neo AI imageUrls."""
    refs = []
    token = _token_for_neo(args)
    theme_images = [p for p in (getattr(args, "theme_images", []) or []) if p]
    if theme_images and not token:
        raise RuntimeError("需要 NEODOMAIN_ACCESS_TOKEN 或 --token 才能上传主题图并作为 Neo AI 参考图。")
    if theme_images and not NEO_UPLOAD_SCRIPT.exists():
        raise RuntimeError(f"Neo AI 上传脚本不存在: {NEO_UPLOAD_SCRIPT}")
    if not token:
        return refs
    for path_text in theme_images:
        if not path_text:
            continue
        if re.match(r"^https?://", str(path_text)):
            refs.append(str(path_text))
            continue
        path = Path(path_text)
        if not path.exists():
            raise RuntimeError(f"Neo AI 参考图不存在，无法上传: {path}")
        cmd = [sys.executable, str(NEO_UPLOAD_SCRIPT), str(path)]
        if args.token:
            cmd.extend(["--token", args.token])
        print("运行:", " ".join(cmd))
        proc = subprocess.run(cmd, check=True, text=True, capture_output=True, env=os.environ.copy())
        print(proc.stdout.strip())
        match = re.search(r"https?://\S+", proc.stdout)
        if not match:
            raise RuntimeError(f"Neo AI 参考图上传成功但未找到 URL: {path}")
        refs.append(match.group(0).strip())
    if refs:
        payload = {
            "reference_images": [
                {"index": idx + 1, "url": url, "role": "primary" if idx == 0 else "reference"}
                for idx, url in enumerate(refs)
            ]
        }
        write_json(out_dir / "neo_reference_images.json", payload)
    return refs


def _copy_generated_image(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        mirror_tile(image).convert("RGB").save(dest)
    return dest


def write_single_texture_set(
    out_dir: Path,
    texture_paths: dict[str, Path],
    palette: dict | None = None,
    prompt_map: dict[str, str] | None = None,
    texture_ids: list[str] | tuple[str, ...] | None = None,
    require_all: bool = True,
) -> Path:
    """Write texture_set.json directly from standalone Neo AI textures."""
    prompt_map = prompt_map or {}
    palette = palette or {}
    ordered_ids = list(texture_ids or DEFAULT_TEXTURE_IDS)
    available_ids = [texture_id for texture_id in ordered_ids if texture_paths.get(texture_id)]
    missing = [texture_id for texture_id in ordered_ids if texture_id not in available_ids]
    if require_all and missing:
        raise RuntimeError(f"缺少单纹理资产: {', '.join(missing)}")
    if not available_ids:
        raise RuntimeError("没有可写入 texture_set.json 的单纹理资产。")

    fallback_path = texture_paths[available_ids[0]]
    secondary_img = Image.open(texture_paths.get("secondary") or fallback_path)
    accent_img = Image.open(texture_paths.get("accent_light") or fallback_path)
    quiet_solid = quiet_solid_from_image(accent_img, palette=palette, target_role="trim")
    moss_color = quiet_solid_from_image(secondary_img, palette=palette, target_role="secondary")
    warm_ivory = "#f3f1df"
    if palette and palette.get("primary"):
        from PIL import ImageColor
        def _brightness(hex_str):
            try:
                r, g, b = ImageColor.getrgb(hex_str)
                return r + g + b
            except Exception:
                return 0
        warm_ivory = max(palette["primary"], key=_brightness)

    textures = []
    for texture_id in available_ids:
        path = texture_paths.get(texture_id)
        textures.append({
            "texture_id": texture_id,
            "path": str(path.resolve()),
            "role": texture_id,
            "approved": True,
            "candidate": False,
            "prompt": prompt_map.get(texture_id, f"Neo AI 单纹理生成：{texture_id}"),
            "model": "neo-ai",
            "seed": "",
        })

    texture_set = {
        "texture_set_id": f"{out_dir.name}_neo_ai_single_texture_set",
        "locked": False,
        "source_mode": "single_textures",
        "partial_success": bool(missing),
        "missing_textures": missing,
        "textures": textures,
        "motifs": [],
        "solids": [
            {"solid_id": "quiet_solid", "color": quiet_solid, "approved": True, "candidate": False},
            {"solid_id": "quiet_moss", "color": moss_color, "approved": True, "candidate": False},
            {"solid_id": "warm_ivory", "color": warm_ivory, "approved": True, "candidate": False},
        ],
    }
    return write_json(out_dir / "texture_set.json", texture_set)


def generate_texture_and_hero_assets(args: argparse.Namespace, out_dir: Path) -> tuple[Path, Path | None]:
    """旧入口：并行调用 Neo AI 生成看板 + 独立透明主图。"""
    texture_dir = out_dir / "neo_texture_board"
    hero_dir = out_dir / "neo_hero_motif"
    texture_dir.mkdir(parents=True, exist_ok=True)
    hero_prompt_file = getattr(args, "hero_prompt_file", "")
    if not hero_prompt_file:
        return generate_board(args, out_dir), None
    hero_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    jobs = [
        ("透明主图", hero_dir, _neo_generation_cmd(args, hero_dir, hero_prompt_file, negative_prompt=HERO_NEGATIVE_PROMPT)),
        ("旧纹理看板", texture_dir, _neo_generation_cmd(args, texture_dir, getattr(args, "texture_prompt_file", "") or getattr(args, "prompt_file", ""))),
    ]
    processes = []
    for label, _, cmd in jobs:
        print("运行:", " ".join(cmd))
        processes.append((label, subprocess.Popen(cmd, env=env)))
    failures = []
    for label, proc in processes:
        rc = proc.wait()
        if rc:
            failures.append((label, rc))
    if failures:
        details = ", ".join(f"{label} rc={rc}" for label, rc in failures)
        raise RuntimeError(f"Neo AI 并行生成失败: {details}")
    return latest_collection_board(texture_dir), latest_collection_board(hero_dir)


def generate_single_textures_and_hero_assets(args: argparse.Namespace, out_dir: Path) -> tuple[dict[str, Path], Path | None]:
    """并行调用 Neo AI：三张独立单纹理 + 独立透明主图。

    Reference images are uploaded once, then reused for every generation job.
    Each image generation still runs through the official Neo AI script, but the
    subprocesses are submitted concurrently to reduce end-to-end waiting time.
    """
    prompt_files = getattr(args, "texture_prompt_files", {}) or {}
    if not prompt_files:
        prompt_file = getattr(args, "texture_prompt_file", "") or getattr(args, "prompt_file", "")
        if prompt_file:
            prompt_files = {texture_id: prompt_file for texture_id in DEFAULT_TEXTURE_IDS}
    missing = [texture_id for texture_id in DEFAULT_TEXTURE_IDS if texture_id not in prompt_files]
    if missing:
        raise RuntimeError(f"缺少单纹理提示词文件: {', '.join(missing)}")

    refs = _upload_reference_images_for_neo(args, out_dir)
    texture_root = out_dir / "neo_textures"
    hero_dir = out_dir / "neo_hero_motif"
    texture_root.mkdir(parents=True, exist_ok=True)
    hero_prompt_file = getattr(args, "hero_prompt_file", "")
    if hero_prompt_file:
        hero_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    if hero_prompt_file:
        jobs.append((
            "透明主图",
            "hero_motif_1",
            hero_dir,
            _neo_generation_cmd(args, hero_dir, hero_prompt_file, negative_prompt=HERO_NEGATIVE_PROMPT, reference_images=refs),
        ))
    for texture_id in DEFAULT_TEXTURE_IDS:
        work_dir = texture_root / texture_id
        work_dir.mkdir(parents=True, exist_ok=True)
        jobs.append((
            f"单纹理 {texture_id}",
            texture_id,
            work_dir,
            _neo_generation_cmd(args, work_dir, prompt_files[texture_id], reference_images=refs),
        ))

    env = os.environ.copy()
    texture_paths = {}
    hero_path = None
    hero_started = threading.Event()
    if not hero_prompt_file:
        hero_started.set()

    def _run_generation_job(job: tuple[str, str, Path, list[str]]) -> tuple[str, str, Path]:
        label, texture_id, work_dir, cmd = job
        if texture_id != "hero_motif_1":
            hero_started.wait()
        print(f"[Neo AI] 启动生成任务: {label} ({texture_id})")
        if texture_id == "hero_motif_1":
            run_step_after_launch(cmd, env=env, on_launch=hero_started.set)
        else:
            run_step(cmd, env=env)
        generated = latest_collection_board(work_dir)
        return label, texture_id, generated

    max_workers = max(1, int(getattr(args, "neo_workers", 4) or 4))
    max_workers = min(max_workers, len(jobs))
    print(f"[Neo AI] 优先提交透明主图，然后并行提交纹理；资产数={len(jobs)}, workers={max_workers}")
    failures = []
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_run_generation_job, job): job for job in jobs}
        for future in concurrent.futures.as_completed(future_map):
            label, texture_id, _, _ = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append((label, texture_id, exc))

    if failures:
        details = "; ".join(f"{label}({texture_id}): {exc}" for label, texture_id, exc in failures)
        raise RuntimeError(f"Neo AI 并行生成失败: {details}")

    by_texture_id = {texture_id: generated for _, texture_id, generated in results}
    for texture_id in DEFAULT_TEXTURE_IDS:
        generated = by_texture_id.get(texture_id)
        if not generated:
            raise RuntimeError(f"Neo AI 并行生成缺少输出: {texture_id}")
        texture_paths[texture_id] = _copy_generated_image(generated, texture_root / f"{texture_id}.png").resolve()
        print(f"[单纹理] {texture_id}: {texture_paths[texture_id]}")

    generated_hero = by_texture_id.get("hero_motif_1")
    if generated_hero:
        hero_path = generated_hero.resolve()
        print(f"[透明主图] {hero_path}")
    return texture_paths, hero_path


def load_texture_prompt_map(out_dir: Path) -> dict[str, str]:
    """Load texture prompts for manifests and per-channel texture_set files."""
    prompt_map = {}
    texture_prompts_path = out_dir / "texture_prompts.json"
    if texture_prompts_path.exists():
        try:
            data = json.loads(texture_prompts_path.read_text(encoding="utf-8"))
            prompt_map = {
                item.get("texture_id", ""): item.get("prompt", "")
                for item in data.get("prompts", [])
                if item.get("texture_id") in DEFAULT_TEXTURE_IDS
            }
        except Exception:
            prompt_map = {}
    return prompt_map


def process_single_texture_channel(
    args: argparse.Namespace,
    out_dir: Path,
    texture_id: str,
    texture_path: Path,
    hero_motif_path: Path,
    pieces_path: Path,
    garment_map_path: Path,
    palette: dict | None,
    prompt_map: dict[str, str],
    split_assets: dict,
    template_assets: dict | None,
) -> dict:
    """Run the independent downstream channel for one generated texture."""
    variant_dir = out_dir / "variants" / texture_id
    variant_dir.mkdir(parents=True, exist_ok=True)
    texture_set_path = write_single_texture_set(
        variant_dir,
        {texture_id: texture_path},
        palette=palette,
        prompt_map=prompt_map,
        texture_ids=[texture_id],
        require_all=True,
    )
    if inject_front_split_motifs and split_assets:
        inject_front_split_motifs(texture_set_path, split_assets)

    ai_piece_plan = variant_dir / "ai_piece_fill_plan.json"
    production_source = Path(args.production_plan or out_dir / "ai_production_plan.json")
    if production_source.exists():
        apply_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "应用生产规划.py"),
            "--production-plan", str(production_source),
            "--out", str(variant_dir),
            "--pieces", str(pieces_path),
        ]
        if template_assets:
            apply_cmd.extend(["--fixed-garment-map", str(garment_map_path)])
        run_step(apply_cmd)

    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--garment-map", str(garment_map_path),
        "--out", str(variant_dir),
    ]
    if args.brief:
        plan_cmd.extend(["--brief", args.brief])
    if args.visual_elements:
        plan_cmd.extend(["--visual-elements", args.visual_elements])
    if ai_piece_plan.exists():
        plan_cmd.extend(["--ai-plan", str(ai_piece_plan)])
    run_step(plan_cmd)

    fill_plan_path = variant_dir / "piece_fill_plan.json"
    variant_fill_plan = force_fill_plan_to_single_texture(load_json(fill_plan_path), texture_id)
    fill_plan_path.write_text(json.dumps(variant_fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    rendered_dir = variant_dir
    render_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "渲染裁片.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--fill-plan", str(fill_plan_path),
        "--out", str(rendered_dir),
    ]
    run_step(render_cmd)

    return {
        "status": "success",
        "纹理ID": texture_id,
        "纹理源图": str(texture_path.resolve()),
        "AI生成透明主图": str(hero_motif_path.resolve()),
        "面料组合": str(texture_set_path.resolve()),
        "裁片填充计划": str(fill_plan_path.resolve()),
        "渲染目录": str(rendered_dir.resolve()),
        "预览图": str((rendered_dir / "preview.png").resolve()),
        "白底预览图": str((rendered_dir / "preview_white.jpg").resolve()),
        "清单": str((rendered_dir / "texture_fill_manifest.json").resolve()),
    }


def run_single_texture_channel_pipeline(
    args: argparse.Namespace,
    out_dir: Path,
    pieces_path: Path,
    garment_map_path: Path,
    template_assets: dict | None,
    palette: dict | None,
    prompt_map: dict[str, str],
) -> tuple[Path, Path, list[dict], dict]:
    """Generate hero/textures and start each texture channel as soon as possible."""
    prompt_files = getattr(args, "texture_prompt_files", {}) or {}
    if not prompt_files:
        prompt_file = getattr(args, "texture_prompt_file", "") or getattr(args, "prompt_file", "")
        if prompt_file:
            prompt_files = {texture_id: prompt_file for texture_id in DEFAULT_TEXTURE_IDS}
    missing = [texture_id for texture_id in DEFAULT_TEXTURE_IDS if texture_id not in prompt_files]
    if missing:
        raise RuntimeError(f"缺少单纹理提示词文件: {', '.join(missing)}")
    hero_prompt_file = getattr(args, "hero_prompt_file", "")
    if not hero_prompt_file:
        raise RuntimeError("三通道流水线需要透明主图提示词 hero_prompt_file。")

    refs = _upload_reference_images_for_neo(args, out_dir)
    texture_root = out_dir / "neo_textures"
    hero_dir = out_dir / "neo_hero_motif"
    texture_root.mkdir(parents=True, exist_ok=True)
    hero_dir.mkdir(parents=True, exist_ok=True)

    generation_jobs = [(
        "透明主图",
        "hero_motif_1",
        hero_dir,
        _neo_generation_cmd(args, hero_dir, hero_prompt_file, negative_prompt=HERO_NEGATIVE_PROMPT, reference_images=refs),
    )]
    for texture_id in DEFAULT_TEXTURE_IDS:
        work_dir = texture_root / texture_id
        work_dir.mkdir(parents=True, exist_ok=True)
        generation_jobs.append((
            f"单纹理 {texture_id}",
            texture_id,
            work_dir,
            _neo_generation_cmd(args, work_dir, prompt_files[texture_id], reference_images=refs),
        ))

    hero_started = threading.Event()

    def _run_generation_job(job: tuple[str, str, Path, list[str]]) -> tuple[str, str, Path]:
        label, texture_id, work_dir, cmd = job
        if texture_id != "hero_motif_1":
            hero_started.wait()
        print(f"[Neo AI] 启动生成任务: {label} ({texture_id})")
        if texture_id == "hero_motif_1":
            run_step_after_launch(cmd, env=os.environ.copy(), on_launch=hero_started.set)
        else:
            run_step(cmd, env=os.environ.copy())
        return label, texture_id, latest_collection_board(work_dir)

    hero_motif_path: Path | None = None
    split_assets: dict | None = None
    ready_textures: dict[str, Path] = {}
    scheduled_channels: set[str] = set()
    variant_summaries: list[dict] = []
    channel_futures: dict[concurrent.futures.Future, str] = {}
    generation_failures: list[tuple[str, str, Exception]] = []

    neo_workers = max(1, int(getattr(args, "neo_workers", 4) or 4))
    neo_workers = min(neo_workers, len(generation_jobs))
    pipeline_workers = max(1, int(getattr(args, "pipeline_workers", 3) or 3))
    pipeline_workers = min(pipeline_workers, len(DEFAULT_TEXTURE_IDS))
    print(f"[Neo AI] 优先提交透明主图，然后并行提交纹理；资产数={len(generation_jobs)}, workers={neo_workers}")
    print(f"[纹理通道] 并行后处理 workers={pipeline_workers}")

    def _schedule_ready_channels(executor: concurrent.futures.Executor) -> None:
        if not hero_motif_path or not split_assets:
            return
        for tid in DEFAULT_TEXTURE_IDS:
            texture_path = ready_textures.get(tid)
            if not texture_path or tid in scheduled_channels:
                continue
            scheduled_channels.add(tid)
            future = executor.submit(
                process_single_texture_channel,
                args,
                out_dir,
                tid,
                texture_path,
                hero_motif_path,
                pieces_path,
                garment_map_path,
                palette,
                prompt_map,
                split_assets,
                template_assets,
            )
            channel_futures[future] = tid
            print(f"[纹理通道] 已启动 {tid}: {texture_path}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=neo_workers) as generation_executor, \
            concurrent.futures.ThreadPoolExecutor(max_workers=pipeline_workers) as pipeline_executor:
        future_map = {generation_executor.submit(_run_generation_job, job): job for job in generation_jobs}
        for future in concurrent.futures.as_completed(future_map):
            label, texture_id, _, _ = future_map[future]
            try:
                _, _, generated = future.result()
                if texture_id == "hero_motif_1":
                    hero_motif_path = generated.resolve()
                    args.hero_motif_image = str(hero_motif_path)
                    if not create_front_split_assets:
                        raise RuntimeError("theme_front_splitter 不可用，无法生成主题前片切半资产。")
                    split_assets = create_front_split_assets(hero_motif_path, out_dir)
                    print(f"[透明主图] {hero_motif_path}")
                    print(f"[主题前片] 已生成切半资产: {split_assets['left']}, {split_assets['right']}")
                else:
                    texture_path = _copy_generated_image(generated, texture_root / f"{texture_id}.png").resolve()
                    ready_textures[texture_id] = texture_path
                    print(f"[单纹理] {texture_id}: {texture_path}")
                _schedule_ready_channels(pipeline_executor)
            except Exception as exc:
                generation_failures.append((label, texture_id, exc))
                print(f"[警告] {label} 生成失败: {exc}", file=sys.stderr)

        if not hero_motif_path:
            details = "; ".join(f"{label}({texture_id}): {exc}" for label, texture_id, exc in generation_failures)
            raise RuntimeError(f"透明主图生成失败，无法启动三通道流水线: {details}")
        _schedule_ready_channels(pipeline_executor)

        for future in concurrent.futures.as_completed(list(channel_futures)):
            texture_id = channel_futures[future]
            try:
                variant_summaries.append(future.result())
            except Exception as exc:
                variant_summaries.append({
                    "status": "failed",
                    "纹理ID": texture_id,
                    "纹理源图": str(ready_textures.get(texture_id, "")),
                    "error": str(exc),
                })
                print(f"[警告] 纹理通道 {texture_id} 失败: {exc}", file=sys.stderr)

    for label, texture_id, exc in generation_failures:
        if texture_id != "hero_motif_1":
            variant_summaries.append({
                "status": "failed",
                "纹理ID": texture_id,
                "纹理源图": "",
                "error": f"{label} 生成失败: {exc}",
            })

    success_summaries = [item for item in variant_summaries if item.get("status") == "success"]
    if not success_summaries:
        raise RuntimeError("三通道流水线没有成功生成任何纹理变体。")

    success_paths = {item["纹理ID"]: Path(item["纹理源图"]) for item in success_summaries}
    root_texture_set_path = write_single_texture_set(
        out_dir,
        success_paths,
        palette=palette,
        prompt_map=prompt_map,
        texture_ids=list(DEFAULT_TEXTURE_IDS),
        require_all=False,
    )
    if inject_front_split_motifs and split_assets:
        inject_front_split_motifs(root_texture_set_path, split_assets)

    ordered_summaries = []
    by_id = {item.get("纹理ID"): item for item in variant_summaries}
    for texture_id in DEFAULT_TEXTURE_IDS:
        if texture_id in by_id:
            ordered_summaries.append(by_id[texture_id])
    ordered_summaries.extend(item for item in variant_summaries if item.get("纹理ID") not in DEFAULT_TEXTURE_IDS)

    default_summary = next(
        (item for item in ordered_summaries if item.get("status") == "success" and item.get("纹理ID") == "main"),
        None,
    ) or next(item for item in ordered_summaries if item.get("status") == "success")

    root_plan = Path(default_summary["裁片填充计划"])
    if root_plan.exists():
        (out_dir / "piece_fill_plan.json").write_text(root_plan.read_text(encoding="utf-8"), encoding="utf-8")

    return root_texture_set_path, hero_motif_path, ordered_summaries, default_summary


def run_ready_single_texture_channel_pipeline(
    args: argparse.Namespace,
    out_dir: Path,
    texture_paths: dict[str, Path],
    hero_motif_path: Path,
    pieces_path: Path,
    garment_map_path: Path,
    template_assets: dict | None,
    palette: dict | None,
    prompt_map: dict[str, str],
) -> tuple[Path, Path, list[dict], dict]:
    """Run per-texture downstream channels for already available assets."""
    if not create_front_split_assets:
        raise RuntimeError("theme_front_splitter 不可用，无法生成主题前片切半资产。")
    args.hero_motif_image = str(hero_motif_path)
    split_assets = create_front_split_assets(hero_motif_path, out_dir)
    pipeline_workers = max(1, int(getattr(args, "pipeline_workers", 3) or 3))
    pipeline_workers = min(pipeline_workers, len(DEFAULT_TEXTURE_IDS))
    print(f"[纹理通道] 复用已生成资产，并行后处理 workers={pipeline_workers}")
    print(f"[主题前片] 已生成切半资产: {split_assets['left']}, {split_assets['right']}")

    variant_summaries = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=pipeline_workers) as executor:
        future_map = {
            executor.submit(
                process_single_texture_channel,
                args,
                out_dir,
                texture_id,
                texture_paths[texture_id],
                hero_motif_path,
                pieces_path,
                garment_map_path,
                palette,
                prompt_map,
                split_assets,
                template_assets,
            ): texture_id
            for texture_id in DEFAULT_TEXTURE_IDS
            if texture_paths.get(texture_id)
        }
        for future in concurrent.futures.as_completed(future_map):
            texture_id = future_map[future]
            try:
                variant_summaries.append(future.result())
            except Exception as exc:
                variant_summaries.append({
                    "status": "failed",
                    "纹理ID": texture_id,
                    "纹理源图": str(texture_paths.get(texture_id, "")),
                    "error": str(exc),
                })
                print(f"[警告] 纹理通道 {texture_id} 失败: {exc}", file=sys.stderr)

    success_summaries = [item for item in variant_summaries if item.get("status") == "success"]
    if not success_summaries:
        raise RuntimeError("三通道流水线没有成功生成任何纹理变体。")

    root_texture_set_path = write_single_texture_set(
        out_dir,
        {item["纹理ID"]: Path(item["纹理源图"]) for item in success_summaries},
        palette=palette,
        prompt_map=prompt_map,
        texture_ids=list(DEFAULT_TEXTURE_IDS),
        require_all=False,
    )
    if inject_front_split_motifs:
        inject_front_split_motifs(root_texture_set_path, split_assets)

    by_id = {item.get("纹理ID"): item for item in variant_summaries}
    ordered_summaries = [by_id[texture_id] for texture_id in DEFAULT_TEXTURE_IDS if texture_id in by_id]
    default_summary = next(
        (item for item in ordered_summaries if item.get("status") == "success" and item.get("纹理ID") == "main"),
        None,
    ) or next(item for item in ordered_summaries if item.get("status") == "success")

    root_plan = Path(default_summary["裁片填充计划"])
    if root_plan.exists():
        (out_dir / "piece_fill_plan.json").write_text(root_plan.read_text(encoding="utf-8"), encoding="utf-8")

    return root_texture_set_path, hero_motif_path, ordered_summaries, default_summary


def mirror_tile(image: Image.Image) -> Image.Image:
    """镜像修复：将纹理裁剪修复为无缝图块。"""
    src = image.convert("RGBA")
    out = Image.new("RGBA", (src.width * 2, src.height * 2), (0, 0, 0, 0))
    out.alpha_composite(src, (0, 0))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (src.width, 0))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, src.height))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_LEFT_RIGHT).transpose(Image.Transpose.FLIP_TOP_BOTTOM), (src.width, src.height))
    return out


def detect_grid_gaps(board: Image.Image, div_x1: int, div_y1: int, strip_width: int = 40) -> int:
    """检测 2×2 网格的水平/垂直分隔带，返回统一的安全边距。"""
    gray = board.convert("L")
    width, height = gray.size
    gap_insets = []

    y0 = max(0, div_y1 - strip_width)
    y1 = min(height, div_y1 + strip_width)
    h_strip = gray.crop((0, y0, width, y1))
    h_pixels = list(_flat_pixels(h_strip))
    strip_w = h_strip.width
    row_diffs = []
    for y in range(h_strip.height):
        row = [h_pixels[y * strip_w + x] for x in range(strip_w)]
        diffs = [abs(row[i] - row[i - 1]) for i in range(1, len(row))]
        row_diffs.append(sum(diffs) / max(1, len(diffs)))
    if sum(1 for d in row_diffs if d > 18) > h_strip.height * 0.25:
        gap_insets.append(20)

    x0 = max(0, div_x1 - strip_width)
    x1 = min(width, div_x1 + strip_width)
    v_strip = gray.crop((x0, 0, x1, height))
    v_pixels = list(_flat_pixels(v_strip))
    strip_h = v_strip.height
    col_diffs = []
    for x in range(v_strip.width):
        col = [v_pixels[y * v_strip.width + x] for y in range(strip_h)]
        diffs = [abs(col[i] - col[i - 1]) for i in range(1, len(col))]
        col_diffs.append(sum(diffs) / max(1, len(diffs)))
    if sum(1 for d in col_diffs if d > 18) > v_strip.width * 0.25:
        gap_insets.append(20)

    return max(gap_insets) if gap_insets else 0


def quiet_solid_from_image(image: Image.Image, palette: dict = None, target_role: str = "trim") -> str:
    """从图像提取纯色，使用 MedianCut 取主色（避免单像素平均的脏灰问题），
    优先遵循 palette，避免硬编码颜色偏差。

    Args:
        image: 面板图像。
        palette: 从主题图提取的 palette dict，含 primary/secondary/accent/dark 列表。
        target_role: 目标用途，决定从 palette 的哪个 tier 选色。
    """
    from PIL import ImageColor
    from collections import Counter

    # MedianCut 量化提取主色（避免花哨纹理平均成脏灰）
    sample = image.convert("RGB").resize((160, 160), Image.Resampling.LANCZOS)
    quantized = sample.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette_raw = quantized.getpalette() or []
    used = Counter(_flat_pixels(quantized))

    dominant_colors = []
    for index, _ in used.most_common(4):
        offset = index * 3
        if offset + 2 >= len(palette_raw):
            continue
        rgb = tuple(palette_raw[offset:offset + 3])
        # 跳过接近纯黑/纯白的极端值
        brightness = sum(rgb) / 3
        if brightness < 20 or brightness > 250:
            continue
        dominant_colors.append(rgb)

    if not dominant_colors:
        # 单像素平均。
        sample = image.convert("RGB").resize((1, 1), Image.Resampling.LANCZOS)
        dominant_colors = [sample.getpixel((0, 0))]

    if not palette:
        r, g, b = dominant_colors[0]
        return "#{:02x}{:02x}{:02x}".format(r, g, b)

    # 根据 target_role 从 palette 选最合适的颜色 tier
    if target_role in ("trim", "dark", "dark_base"):
        candidates = palette.get("dark", []) + palette.get("accent", [])
    elif target_role in ("secondary", "accent"):
        candidates = palette.get("secondary", []) + palette.get("accent", [])
    else:
        candidates = palette.get("primary", []) + palette.get("secondary", [])

    if candidates:
        def _color_distance(c1, c2):
            try:
                rgb1 = ImageColor.getrgb(c1)
                rgb2 = ImageColor.getrgb(c2)
                return sum((a - b) ** 2 for a, b in zip(rgb1, rgb2))
            except Exception:
                return float("inf")

        # 从 dominant_colors 中选与 palette 最接近的一个
        best_color = None
        best_dist = float("inf")
        for dom_rgb in dominant_colors:
            dom_hex = "#{:02x}{:02x}{:02x}".format(*dom_rgb)
            dist = min(_color_distance(dom_hex, c) for c in candidates)
            if dist < best_dist:
                best_dist = dist
                best_color = dom_hex

        if best_color:
            return best_color

    r, g, b = dominant_colors[0]
    return "#{:02x}{:02x}{:02x}".format(r, g, b)


def clean_internal_text_strip(image: Image.Image, min_strip_height: int = 5, diff_threshold: float = 12.0) -> Image.Image:
    """检测并去除图像内部任意位置的水平文字条带（高对比度水平区域）。
    适用于看板裁剪后每个面板内部可能含有的文字标签。
    """
    gray = image.convert("L")
    width, height = gray.size
    pixels = list(_flat_pixels(gray))

    row_diffs = []
    for y in range(height):
        row = [pixels[y * width + x] for x in range(width)]
        diffs = [abs(row[i] - row[i - 1]) for i in range(1, len(row))]
        row_diffs.append(sum(diffs) / max(1, len(diffs)))

    # 找连续的高差异行（文字特征）
    text_regions = []
    in_text = False
    start = 0
    for y, diff in enumerate(row_diffs):
        if diff > diff_threshold and not in_text:
            in_text = True
            start = y
        elif diff <= diff_threshold * 0.35 and in_text:
            in_text = False
            if y - start >= min_strip_height:
                text_regions.append((start, y))
    if in_text and height - start >= min_strip_height:
        text_regions.append((start, height))

    if not text_regions:
        return image

    # 评估每个区域是否最可能是文字标签
    best_region = None
    best_score = 0
    for start_y, end_y in text_regions:
        region_h = end_y - start_y
        region_pixels = [pixels[y * width + x] for y in range(start_y, end_y) for x in range(width)]
        mean_brightness = sum(region_pixels) / len(region_pixels)
        avg_diff = sum(row_diffs[start_y:end_y]) / max(1, region_h)
        # 文字区域通常是白底黑字，亮度较高（>180）且差异大，高度适中
        score = avg_diff * (mean_brightness / 255.0) * (1.0 if 6 <= region_h <= 140 else 0.2)
        if score > best_score:
            best_score = score
            best_region = (start_y, end_y)

    if not best_region or best_score < 80:
        return image

    start_y, end_y = best_region
    print(f"[文字清理] 裁剪掉文字条带 y={start_y}-{end_y}（高度{end_y - start_y}，分数{best_score:.1f}）")

    # 合并上下部分（条带可能在图像中间）
    top = image.crop((0, 0, width, start_y + 1))
    bottom = image.crop((0, end_y - 1, width, height))

    if top.height > 10 and bottom.height > 10:
        merged = Image.new(image.mode, (width, top.height + bottom.height))
        merged.paste(top, (0, 0))
        merged.paste(bottom, (0, top.height))
        return merged
    elif top.height > 10:
        return top
    elif bottom.height > 10:
        return bottom
    return image


def crop_collection_board(board_path: Path, out_dir: Path, inset: int, repair_tiles: bool, palette: dict = None) -> Path:
    """将旧看板裁剪为三种纹理资产，并生成面料组合.json。
    支持智能分隔带检测，自动扩大安全边距，并清理面板内部文字。"""
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    board = Image.open(board_path).convert("RGBA")
    width, height = board.size
    div_x1 = width // 2
    div_y1 = height // 2

    # 智能检测网格分隔带文字，动态调整边距
    extra_gap = detect_grid_gaps(board, div_x1, div_y1)
    effective_inset = inset + extra_gap
    # 确保不越界
    max_inset = max(0, min(div_x1, width - div_x1, div_y1, height - div_y1) - 64)
    effective_inset = min(effective_inset, max_inset)
    if effective_inset > inset:
        print(f"[智能裁剪] 检测到分隔带文字，边距从 {inset} 扩大到 {effective_inset}")

    boxes = {
        "main": (effective_inset, effective_inset, div_x1 - effective_inset, div_y1 - effective_inset),
        "secondary": (div_x1 + effective_inset, effective_inset, width - effective_inset, div_y1 - effective_inset),
        "accent_light": (effective_inset, div_y1 + effective_inset, div_x1 - effective_inset, height - effective_inset),
    }

    paths = {}
    for asset_id, box in boxes.items():
        crop = board.crop(box)
        crop = clean_internal_text_strip(crop)
        if repair_tiles:
            crop = mirror_tile(crop)
        path = assets_dir / f"{asset_id}.png"
        crop.convert("RGB").save(path)
        paths[asset_id] = path

    quiet_solid = quiet_solid_from_image(Image.open(paths["accent_light"]), palette=palette, target_role="trim")
    moss_color = quiet_solid_from_image(Image.open(paths["secondary"]), palette=palette, target_role="secondary")

    # warm_ivory 从 palette primary 中最亮颜色派生，不再硬编码
    warm_ivory = "#f3f1df"
    if palette and palette.get("primary"):
        from PIL import ImageColor
        primary_colors = palette["primary"]
        if primary_colors:
            # 选最亮的 primary 颜色
            def _brightness(hex_str):
                try:
                    r, g, b = ImageColor.getrgb(hex_str)
                    return r + g + b
                except Exception:
                    return 0
            brightest = max(primary_colors, key=_brightness)
            warm_ivory = brightest

    source_name = "neo-ai"
    texture_set = {
        "texture_set_id": f"{out_dir.name}_{source_name}_collection_texture_set",
        "locked": False,
        "source_collection_board": str(board_path.resolve()),
        "textures": [
            {
                "texture_id": "main",
                "path": str(paths["main"].resolve()),
                "role": "main",
                "approved": True,
                "candidate": False,
                "prompt": f"从 {source_name} 2×2 面料看板裁剪：主底纹",
                "model": source_name,
                "seed": "",
            },
            {
                "texture_id": "secondary",
                "path": str(paths["secondary"].resolve()),
                "role": "secondary",
                "approved": True,
                "candidate": False,
                "prompt": f"从 {source_name} 2×2 面料看板裁剪：辅纹理",
                "model": source_name,
                "seed": "",
            },
            {
                "texture_id": "accent_light",
                "path": str(paths["accent_light"].resolve()),
                "role": "accent_light",
                "approved": True,
                "candidate": False,
                "prompt": f"从 {source_name} 2×2 面料看板裁剪：浅色点缀纹理",
                "model": source_name,
                "seed": "",
            },
        ],
        "motifs": [],
        "solids": [
            {"solid_id": "quiet_solid", "color": quiet_solid, "approved": True, "candidate": False},
            {"solid_id": "quiet_moss", "color": moss_color, "approved": True, "candidate": False},
            {"solid_id": "warm_ivory", "color": warm_ivory, "approved": True, "candidate": False},
        ],
    }
    return write_json(out_dir / "texture_set.json", texture_set)


def resolve_reusable_template_assets_for_run(args) -> dict | None:
    """内置模板资产完整时直接复用。"""
    if not HAS_TEMPLATE_LOADER:
        return None
    requested_template = bool(args.template)
    assets = resolve_template_assets(
        template_id=args.template,
        size_label="s",
        garment_type=args.garment_type,
    )
    if assets:
        args.template = assets["template_id"]
        if requested_template:
            assets["template_source"] = "template_arg"
        else:
            assets["template_source"] = "garment_type_match"
        return assets


def pieces_asset_hash_for_run(args, pieces_path: Path | None = None) -> str:
    """Return a stable asset hash for explicit masks or reusable template pieces."""
    if pieces_path and pieces_path.exists():
        return file_sha256(pieces_path)
    return f"{getattr(args, 'template', '')}:s"


def resolve_theme_front_source_image(args, out_dir: Path, texture_set_path: Path) -> tuple[str, str]:
    """Find the best available hero image for front-left/front-right split."""
    for label, value in (
        ("AI生成主图", getattr(args, "hero_motif_image", "")),
        ("用户主题图", getattr(args, "theme_image", "")),
    ):
        if value and Path(value).exists():
            return str(Path(value).resolve()), label

    try:
        texture_set = load_json(texture_set_path)
    except Exception:
        texture_set = {}
    for motif in texture_set.get("motifs", []):
        if motif.get("motif_id") == "hero_motif_1" and motif.get("path"):
            path = Path(motif["path"])
            if not path.is_absolute():
                path = Path(texture_set_path).resolve().parent / path
            if path.exists():
                return str(path.resolve()), "texture_set hero_motif_1"

    assets_hero = out_dir / "assets" / "hero_motif_1.png"
    if assets_hero.exists():
        return str(assets_hero.resolve()), "assets hero_motif_1"

    hero_dir = out_dir / "neo_hero_motif"
    candidates = []
    for pattern in ("image_*.png", "collection_board_*.png", "*.png", "*.jpg", "*.jpeg", "*.webp"):
        candidates.extend(p for p in hero_dir.glob(pattern) if p.is_file())
    if candidates:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        return str(newest.resolve()), "neo_hero_motif"

    return "", ""


def ensure_theme_front_split(args, out_dir: Path, texture_set_path: Path) -> dict | None:
    """Generate and register front-half motifs, preferring the AI-generated transparent hero."""
    if not create_front_split_assets or not inject_front_split_motifs:
        return None
    source_image, source_label = resolve_theme_front_source_image(args, out_dir, texture_set_path)
    if not source_image:
        return None
    try:
        split_assets = create_front_split_assets(source_image, out_dir)
        inject_front_split_motifs(texture_set_path, split_assets)
        print(f"[主题前片] 已从{source_label}生成并注册切半资产: {split_assets['left']}, {split_assets['right']}")
        return split_assets
    except Exception as exc:
        print(f"[警告] 主题前片切半资产生成失败，将继续使用普通面料规划: {exc}", file=sys.stderr)
        return None


def _variant_texture_ids(texture_set: dict) -> list[str]:
    preferred = ["main", "secondary", "accent_light"]
    available = [
        item.get("texture_id")
        for item in texture_set.get("textures", [])
        if item.get("approved", False) and item.get("texture_id")
    ]
    ordered = [tid for tid in preferred if tid in available]
    ordered.extend(tid for tid in available if tid not in ordered)
    return ordered


def write_single_texture_variant_set(texture_set: dict, texture_id: str, variant_dir: Path) -> Path:
    """Write a texture_set that approves only one texture candidate for a variant."""
    variant_dir.mkdir(parents=True, exist_ok=True)
    texture = next((item for item in texture_set.get("textures", []) if item.get("texture_id") == texture_id), None)
    if not texture:
        raise RuntimeError(f"无法创建单纹理变体，texture_id 不存在: {texture_id}")
    variant_set = copy.deepcopy(texture_set)
    variant_set["texture_set_id"] = f"{texture_set.get('texture_set_id', 'texture_set')}_{texture_id}_single_texture"
    variant_set["variant_texture_id"] = texture_id
    variant_set["textures"] = [copy.deepcopy(texture)]
    for item in variant_set["textures"]:
        item["approved"] = True
        item["candidate"] = False
        item["role"] = item.get("role") or texture_id
    # Preserve theme front split motifs so each variant still carries the hero
    # image on the left/right front pieces. All other motifs are dropped.
    theme_motifs = [
        copy.deepcopy(m) for m in texture_set.get("motifs", [])
        if m.get("motif_id") in {"theme_front_left", "theme_front_right"}
    ]
    variant_set["motifs"] = theme_motifs
    if texture_set.get("theme_front_split"):
        variant_set["theme_front_split"] = copy.deepcopy(texture_set["theme_front_split"])
    path = variant_dir / "texture_set.json"
    path.write_text(json.dumps(variant_set, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def force_fill_plan_to_single_texture(fill_plan: dict, texture_id: str) -> dict:
    """Return a copy of fill_plan where every rendered layer uses one texture."""
    plan = copy.deepcopy(fill_plan)
    plan["plan_id"] = f"{plan.get('plan_id', 'piece_fill_plan')}_{texture_id}_single_texture"
    plan["variant_texture_id"] = texture_id

    def _texture_layer(reason: str = "单纹理模板预览统一使用同一张图案纹理") -> dict:
        return {
            "fill_type": "texture",
            "texture_id": texture_id,
            "scale": 1.0,
            "rotation": 0,
            "offset_x": 0,
            "offset_y": 0,
            "mirror_x": False,
            "mirror_y": False,
            "reason": reason,
        }

    def _force_render_layer(layer):
        if isinstance(layer, dict):
            fill_type = layer.get("fill_type")
            if fill_type == "motif":
                # Preserve theme front split motifs across all variants
                if layer.get("motif_id") in {"theme_front_left", "theme_front_right"}:
                    return layer
                return None
            if fill_type in {"texture", "solid"} or "texture_id" in layer or "solid_id" in layer:
                layer["fill_type"] = "texture"
                layer["texture_id"] = texture_id
                layer.pop("solid_id", None)
            for key, value in list(layer.items()):
                forced = _force_render_layer(value)
                if forced is None and isinstance(value, dict):
                    layer.pop(key, None)
                elif forced is not value:
                    layer[key] = forced
            return layer
        elif isinstance(layer, list):
            kept = []
            for item in layer:
                forced = _force_render_layer(item)
                if forced is not None:
                    kept.append(forced)
            return kept
        return layer

    for piece in plan.get("pieces", []):
        piece_motif_id = piece.get("motif_id") if piece.get("fill_type") == "motif" else None
        is_theme_split = piece_motif_id in {"theme_front_left", "theme_front_right"}
        if piece.get("fill_type") == "motif" and not is_theme_split:
            piece.update(_texture_layer("单纹理模板预览移除定位主图，统一使用当前图案纹理"))
        elif piece.get("fill_type") in {"texture", "solid"} or "texture_id" in piece or "solid_id" in piece:
            piece["fill_type"] = "texture"
            piece["texture_id"] = texture_id
            piece.pop("solid_id", None)
        if not any(isinstance(piece.get(key), dict) for key in ("base", "overlay", "trim")) and not is_theme_split:
            piece["fill_type"] = "texture"
            piece["texture_id"] = texture_id
            piece.pop("solid_id", None)
        for key in ("base", "trim"):
            if isinstance(piece.get(key), dict):
                forced = _force_render_layer(piece[key])
                if forced is None:
                    if key == "base":
                        piece[key] = _texture_layer()
                    else:
                        piece.pop(key, None)
                else:
                    piece[key] = forced
        overlay = piece.get("overlay")
        if isinstance(overlay, dict):
            forced_overlay = _force_render_layer(overlay)
            if forced_overlay is None:
                piece.pop("overlay", None)
            else:
                piece["overlay"] = forced_overlay
        piece["variant_texture_id"] = texture_id
        piece["single_texture_preview_only"] = True
    return plan


def render_texture_variants(
    out_dir: Path,
    texture_set_path: Path,
    fill_plan_path: Path,
    pieces_path: Path,
    default_rendered_dir: Path,
    args: argparse.Namespace | None = None,
) -> list[dict]:
    """Render one 9-piece garment template per texture candidate."""
    if args is not None:
        ensure_theme_front_split(args, out_dir, texture_set_path)
    texture_set = load_json(texture_set_path)
    base_fill_plan = load_json(fill_plan_path)
    variant_ids = _variant_texture_ids(texture_set)
    if not variant_ids:
        raise RuntimeError("没有可用于生成裁片模板变体的 approved 纹理。")

    base_plan_backup = out_dir / "piece_fill_plan_base.json"
    base_plan_backup.write_text(json.dumps(base_fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    variants_root = out_dir / "variants"
    summaries = []

    for texture_id in variant_ids:
        variant_dir = variants_root / texture_id
        variant_texture_set_path = write_single_texture_variant_set(texture_set, texture_id, variant_dir)
        variant_fill_plan = force_fill_plan_to_single_texture(base_fill_plan, texture_id)
        variant_fill_plan_path = variant_dir / "piece_fill_plan.json"
        variant_fill_plan_path.write_text(json.dumps(variant_fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        variant_rendered_dir = variant_dir
        render_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "渲染裁片.py"),
            "--pieces", str(pieces_path),
            "--texture-set", str(variant_texture_set_path),
            "--fill-plan", str(variant_fill_plan_path),
            "--out", str(variant_rendered_dir),
        ]
        run_step(render_cmd)

        texture_path = ""
        for texture in texture_set.get("textures", []):
            if texture.get("texture_id") == texture_id:
                texture_path = texture.get("path", "")
                break
        summaries.append({
            "纹理ID": texture_id,
            "纹理源图": texture_path,
            "面料组合": str(variant_texture_set_path.resolve()),
            "裁片填充计划": str(variant_fill_plan_path.resolve()),
            "渲染目录": str(variant_rendered_dir.resolve()),
            "预览图": str((variant_rendered_dir / "preview.png").resolve()),
            "白底预览图": str((variant_rendered_dir / "preview_white.jpg").resolve()),
            "清单": str((variant_rendered_dir / "texture_fill_manifest.json").resolve()),
        })

        # Keep single-texture fill plan in root for potential downstream use
        if texture_id == "main":
            fill_plan_path.write_text(json.dumps(variant_fill_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    return summaries


def _apply_or_request_production_plan(
    args,
    out_dir: Path,
    texture_set_path: Path,
    pieces_path: Path,
    garment_map_path: Path,
    template_assets: dict | None,
    suffix: str = "",
) -> int:
    production_plan_path = out_dir / "ai_production_plan.json"

    def _compute_production_input_fingerprint() -> str:
        parts = []
        for p in (args.visual_elements, args.collection_board, args.texture_set, getattr(args, "hero_motif_image", "")):
            parts.append(file_sha256(p) if p else "")
        parts.extend([args.garment_type or "", args.mode, args.template or ""])
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    prod_fp_path = out_dir / ".production_input_fingerprint.json"
    current_prod_fp = _compute_production_input_fingerprint()
    stale = False
    if prod_fp_path.exists():
        try:
            stale = json.loads(prod_fp_path.read_text(encoding="utf-8")).get("fingerprint", "") != current_prod_fp
        except Exception:
            stale = True
    else:
        stale = production_plan_path.exists()

    if stale:
        for stale_file in (
            "ai_production_plan.json",
            "ai_piece_fill_plan.json",
            "piece_fill_plan.json",
            "art_direction_plan.json",
        ):
            p = out_dir / stale_file
            if p.exists():
                p.unlink()
                print(f"[输入变更] 删除旧生产规划产物: {p.name}")

    prod_fp_path.write_text(json.dumps({
        "fingerprint": current_prod_fp,
        "updated_at": datetime.datetime.now().isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.production_plan:
        provided = Path(args.production_plan)
        if not provided.exists():
            print(f"[错误{suffix}] 提供的生产规划不存在: {provided}", file=sys.stderr)
            return 1
        apply_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "应用生产规划.py"),
            "--production-plan", str(provided),
            "--out", str(out_dir),
            "--pieces", str(pieces_path),
        ]
        if template_assets:
            apply_cmd.extend(["--fixed-garment-map", str(garment_map_path)])
        run_step(apply_cmd)
        return 0

    plan_loaded_from_cache = False
    plan_hash = {}
    if args.reuse_cache:
        plan_hash = {
            "pieces_asset": pieces_asset_hash_for_run(args, pieces_path),
            "texture_set": file_sha256(texture_set_path),
            "garment_type": args.garment_type,
            "brief": file_sha256(args.brief) if args.brief else "",
            "template": args.template,
            "mode": args.mode,
            "visual_elements": file_sha256(args.visual_elements) if args.visual_elements else "",
        }
        cached = cache_lookup(out_dir, "production_plan", plan_hash)
        if cached:
            print(f"[缓存复用{suffix}] 生产规划: {cached}")
            production_plan_path.write_bytes(cached.read_bytes())
            plan_loaded_from_cache = True

    if not plan_loaded_from_cache and not production_plan_path.exists():
        plan_request_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "构造生产规划请求.py"),
            "--pieces", str(pieces_path),
            "--texture-set", str(texture_set_path),
            "--garment-map", str(garment_map_path),
            "--out", str(out_dir),
        ]
        if args.brief:
            plan_request_cmd.extend(["--brief", args.brief])
        gh_path = out_dir / "geometry_hints.json"
        if gh_path.exists():
            plan_request_cmd.extend(["--geometry-hints", str(gh_path)])
        if args.visual_elements:
            plan_request_cmd.extend(["--visual-elements", args.visual_elements])
        run_step(plan_request_cmd)
        print(f"\n[提示{suffix}] 生产规划 AI 请求已构造。请输出 ai_production_plan.json 后重新运行。")
        print(f"  提示词文件: {out_dir / 'ai_production_plan_prompt.txt'}")
        print(f"  预期输出: {out_dir / 'ai_production_plan.json'}")
        return 2

    if production_plan_path.exists():
        apply_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "应用生产规划.py"),
            "--production-plan", str(production_plan_path),
            "--out", str(out_dir),
            "--pieces", str(pieces_path),
        ]
        if template_assets:
            apply_cmd.extend(["--fixed-garment-map", str(garment_map_path)])
        run_step(apply_cmd)
        if args.reuse_cache and not plan_loaded_from_cache:
            cache_save(out_dir, "production_plan", plan_hash, production_plan_path)
    else:
        print(f"[提示{suffix}] 未找到 ai_production_plan.json，将使用后端规则生成填充计划。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Neo AI 面料看板并自动渲染服装裁片。")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--collection-board", default="", help="已有的 Neo AI 2×2 面料纹理看板。若省略，则调用 Neo AI 生成。")
    parser.add_argument("--texture-set", default="", help="已有 texture_set.json。提供后跳过看板生成/裁剪，直接使用该面料组合继续裁片映射、填充和渲染。")
    parser.add_argument(
        "--theme-image",
        action="append",
        default=[],
        help=(
            "主题/参考图像。可重复传入多张；支持文件路径、目录、URL、data:image/base64；为空时会尝试 "
            "AUTO_GARMENT_THEME_IMAGE/CODEX_ATTACHED_IMAGE_PATHS 及 out/input 自动发现。若提供，会先进行视觉元素提取。"
        ),
    )
    parser.add_argument("--theme-images", default="", help="多张主题/参考图像，支持逗号、分号或换行分隔。")
    parser.add_argument("--user-prompt", default="", help="用户对主题图、多图角色或美术方向的补充说明。")
    parser.add_argument("--visual-elements", default="", help="已完成的 visual_elements.json 路径。若提供，跳过视觉提取直接生成设计简报。")
    parser.add_argument("--token", default="", help="Neodomain 访问令牌。优先使用 NEODOMAIN_ACCESS_TOKEN 环境变量。")
    parser.add_argument("--neo-model", default="gemini-3-pro-image-preview")
    parser.add_argument("--neo-size", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument("--num-images", default="1", choices=["1", "4"])
    parser.add_argument("--neo-workers", type=int, default=4, help="Neo AI 图片生成并行数。默认 4，同时生成3张纹理和透明主图。")
    parser.add_argument("--pipeline-workers", type=int, default=3, help="纹理后处理通道并行数。默认 3，同时处理 main/secondary/accent_light。")
    parser.add_argument("--garment-type", default="", help="服装类型（如'儿童外套套装'、'女装连衣裙'）。走主题图路径时必填，会写入设计简报并传给部位识别。")
    parser.add_argument("--template", default="", help="模板ID。未提供时按 garment_type 自动匹配。")
    parser.add_argument("--mode", default="standard", choices=["fast", "standard", "production"], help="运行模式。fast=快速流程，standard=默认流程，production=完整规划流程。")
    parser.add_argument("--reuse-cache", action="store_true", help="启用缓存复用。若输入未变化，跳过对应阶段的AI调用和程序计算。")
    parser.add_argument("--production-plan", default="", help="已完成的 ai_production_plan.json 路径。若提供且缓存允许，跳过生产规划AI调用，直接应用该计划。")
    args = parser.parse_args()
    args.brief = ""
    args.crop_inset = 60
    args.no_tile_repair = False
    args.prompt_file = ""
    args.texture_prompt_file = ""
    args.texture_prompt_files = {}
    args.hero_prompt_file = ""
    args.hero_motif_image = ""
    if args.mode == "fast":
        print("[模式] fast")

    import re

    # 主题图输入归一化前，先保存 CLI 原始值，用于稳定 task key。
    raw_theme_images = list(args.theme_image or [])
    raw_theme_images_extra = args.theme_images

    def _is_timestamp_dir(path: Path) -> bool:
        return bool(re.match(r"^\d{8}_\d{6}$", path.name))

    def _split_identity_values(value) -> list[str]:
        if not value:
            return []
        if isinstance(value, (list, tuple)):
            values = []
            for item in value:
                values.extend(_split_identity_values(item))
            return values
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("data:image/") or re.match(r"^https?://", text) or text.startswith("file://"):
            return [text]
        return [part.strip().strip("'\"") for part in re.split(r"[\n,;]", text) if part.strip()]

    def _identity_for_value(value: str) -> str:
        if not value:
            return ""
        path = Path(value).expanduser()
        if path.exists() and path.is_file():
            return f"file:{path.resolve()}:{file_sha256(path)}"
        if path.exists() and path.is_dir():
            images = sorted(
                p for p in path.iterdir()
                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
            )
            digest_parts = [f"{p.name}:{file_sha256(p)}" for p in images[:20]]
            return f"dir:{path.resolve()}:{'|'.join(digest_parts)}"
        if value.startswith("data:image/") or len(value) > 512:
            return "payload:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        return "literal:" + value

    def _raw_theme_identity_parts() -> list[str]:
        values = _split_identity_values(raw_theme_images) + _split_identity_values(raw_theme_images_extra)
        if not values:
            for key in (
                "AUTO_GARMENT_THEME_IMAGE",
                "AUTO_GARMENT_THEME_IMAGES",
                "CODEX_THEME_IMAGE",
                "CODEX_INPUT_IMAGE",
                "CODEX_INPUT_IMAGES",
                "CODEX_ATTACHED_IMAGE",
                "CODEX_ATTACHED_IMAGES",
                "CODEX_ATTACHED_IMAGE_PATH",
                "CODEX_ATTACHED_IMAGE_PATHS",
            ):
                values.extend(_split_identity_values(os.environ.get(key, "")))
        return [_identity_for_value(value) for value in values if value]

    def _compute_task_key() -> tuple[str, bool]:
        """Compute a stable task identity, excluding stage artifacts."""
        theme_parts = _raw_theme_identity_parts()
        parts = [
            "garment_type=" + (args.garment_type or ""),
            "user_prompt=" + getattr(args, "user_prompt", ""),
            "template=" + (args.template or ""),
        ]
        parts.extend("theme=" + item for item in theme_parts)
        has_primary_identity = bool(theme_parts or args.template)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16], has_primary_identity

    def _next_timestamp_dir(root: Path) -> Path:
        candidate_time = datetime.datetime.now()
        for _ in range(120):
            candidate = root / candidate_time.strftime("%Y%m%d_%H%M%S")
            if not candidate.exists():
                return candidate
            candidate_time += datetime.timedelta(seconds=1)
        raise RuntimeError(f"无法在 {root} 下创建唯一时间戳输出目录")

    def _resolve_run_output_dir(requested_out: Path) -> tuple[Path, Path | None, str]:
        task_key, has_primary_identity = _compute_task_key()
        requested_out = requested_out.expanduser()
        if _is_timestamp_dir(requested_out):
            requested_out.mkdir(parents=True, exist_ok=True)
            print(f"[目录隔离] 使用显式任务目录: {requested_out}")
            return requested_out, None, task_key

        root = requested_out
        root.mkdir(parents=True, exist_ok=True)
        current_path = root / ".current_run.json"
        current = {}
        if current_path.exists():
            try:
                current = json.loads(current_path.read_text(encoding="utf-8"))
            except Exception:
                current = {}

        current_dir = Path(current.get("run_dir", "")) if current.get("run_dir") else None
        if current_dir and not current_dir.is_absolute():
            current_dir = root / current_dir
        can_reuse_current = (
            current_dir is not None
            and current_dir.exists()
            and (
                current.get("task_key") == task_key
                or not has_primary_identity
            )
        )
        if can_reuse_current:
            print(f"[目录隔离] 复用当前任务目录: {current_dir}")
            return current_dir, current_path, str(current.get("task_key") or task_key)

        run_dir = _next_timestamp_dir(root)
        run_dir.mkdir(parents=True, exist_ok=True)
        current_payload = {
            "task_key": task_key,
            "run_dir": run_dir.name,
            "run_dir_abs": str(run_dir.resolve()),
            "created_at": datetime.datetime.now().isoformat(),
            "updated_at": datetime.datetime.now().isoformat(),
        }
        current_path.write_text(json.dumps(current_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[目录隔离] 创建新任务目录: {run_dir}")
        return run_dir, current_path, task_key

    out_dir, current_run_path, task_key = _resolve_run_output_dir(Path(args.out))

    # 主题图输入归一化：端到端流程只能消费本地文件。会话附件如果由
    # 客户端/集成以环境变量、URL、base64 或 out/input 目录提供，在这里落成稳定路径。
    if resolve_theme_images:
        try:
            resolved_themes = resolve_theme_images(
                raw_theme_images,
                out_dir,
                extra_values=args.theme_images,
                required=False,
            )
        except Exception as exc:
            print(f"[错误] 主题图解析失败: {exc}", file=sys.stderr)
            return 1
        args.theme_images = [str(path) for path in resolved_themes]
        args.theme_image = args.theme_images[0] if args.theme_images else ""
        if resolved_themes:
            source_note = ", ".join(raw_theme_images) or args.theme_images[0] or "auto-discovered"
            if args.theme_images and source_note != args.theme_images[0]:
                if len(source_note) > 120:
                    source_note = source_note[:117] + "..."
                print(f"[主题图] 已解析并落盘: {source_note} -> {len(args.theme_images)} 张")
            if len(args.theme_images) > 1:
                print(f"[主题图] 多图参考集合: {args.theme_images}")
    else:
        args.theme_images = raw_theme_images
        args.theme_image = raw_theme_images[0] if raw_theme_images else ""

    # 写入 run 目录指纹；父级 out 只保留 .current_run.json，不写业务产物。
    fingerprint_path = out_dir / ".task_fingerprint.json"
    fingerprint_path.write_text(json.dumps({
        "fingerprint": task_key,
        "task_key": task_key,
        "out_root": str(Path(args.out).expanduser().resolve()) if not _is_timestamp_dir(Path(args.out).expanduser()) else "",
        "run_dir": str(out_dir.resolve()),
        "created_at": datetime.datetime.now().isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if current_run_path and current_run_path.exists():
        try:
            current_payload = json.loads(current_run_path.read_text(encoding="utf-8"))
        except Exception:
            current_payload = {}
        current_payload.update({
            "task_key": task_key,
            "run_dir": out_dir.name,
            "run_dir_abs": str(out_dir.resolve()),
            "updated_at": datetime.datetime.now().isoformat(),
        })
        current_run_path.write_text(json.dumps(current_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ===== brief 校验 =====
    brief_path = Path(args.brief) if args.brief else None
    auto_brief = out_dir / "commercial_design_brief.json"
    if not brief_path and auto_brief.exists():
        brief_path = auto_brief
        args.brief = str(auto_brief)
    effective_garment_type = args.garment_type.strip()
    if brief_path and brief_path.exists():
        try:
            brief_data = json.loads(brief_path.read_text(encoding="utf-8"))
            garment_type = brief_data.get("garment_type", "")
            if not garment_type or garment_type.strip() == "":
                print(f"[错误] {brief_path} 中 garment_type 为空，必须提供有效的服装类型（如'儿童外套套装'、'女装连衣裙'）", file=sys.stderr)
                return 1
            if not effective_garment_type:
                effective_garment_type = garment_type.strip()
            print(f"[校验通过] garment_type='{garment_type}'")
        except Exception as exc:
            print(f"[警告] 无法读取 brief: {exc}")
    else:
        print("[警告] 未提供商业设计简报，后续步骤可能缺少 garment_type 上下文")

    if (args.theme_image or args.visual_elements) and not effective_garment_type:
        print("[错误] 走主题图/视觉元素路径时必须提供 --garment-type，或提供包含 garment_type 的 --brief。", file=sys.stderr)
        return 1
    args.garment_type = effective_garment_type

    # ============================================================
    # Phase 1: 程序-only 准备层（与 AI 调用无关，可并行执行）
    # ============================================================
    # 1a. 裁片准备 —— 固定复用内置模板库资产。
    template_assets = resolve_reusable_template_assets_for_run(args)
    if template_assets:
        pieces_path = Path(template_assets["pieces_path"])
        garment_map_path = Path(template_assets["garment_map_path"])
        print(
            "[模板复用] 使用内置模板资产: "
            f"{template_assets['template_id']}/{template_assets['size_label']}"
        )
        print(f"  pieces: {pieces_path}")
        print(f"  garment_map: {garment_map_path}")
    else:
        print("[错误] 未能通过 --template 或 --garment-type 命中内置模板。仅支持 BFSK26308XCJ01L 与 DDS26126XCJ01L 的 s 码资产。", file=sys.stderr)
        return 1

    # 1b. 程序几何推断 → geometry_hints.json（供后续 AI 参考）
    geometry_hints_path = out_dir / "geometry_hints.json"
    if pieces_path.exists() and (not geometry_hints_path.exists() or not args.reuse_cache):
        _build_geometry_hints(pieces_path, geometry_hints_path)

    # ============================================================
    # Phase 2: 主题图/视觉元素路径（可能涉及 AI 调用，可能中途退出）
    # ============================================================
    # 注意：此阶段与 Phase 1 无依赖关系，理论上可并行
    ve_handled = False
    if args.theme_images and not args.visual_elements:
        theme_path = Path(args.theme_image)
        if not theme_path.exists():
            raise RuntimeError(f"主题图不存在: {theme_path}")
        theme_paths = [Path(p) for p in args.theme_images]
        ve_out = out_dir / "visual_elements.json"
        # 缓存检查
        if args.reuse_cache:
            ve_hash = {
                "theme_images": files_sha256([str(p) for p in theme_paths]),
                "garment_type": args.garment_type,
                "user_prompt": getattr(args, "user_prompt", ""),
            }
            cached = cache_lookup(out_dir, "visual_elements", ve_hash)
            if cached:
                print(f"[缓存复用] visual_elements: {cached}")
                ve_out.write_bytes(cached.read_bytes())
                args.visual_elements = str(ve_out)
                ve_handled = True
        if not ve_handled:
            if ve_out.exists():
                print(f"[视觉提取] 已存在视觉元素分析: {ve_out}，直接使用。")
                args.visual_elements = str(ve_out)
                ve_handled = True
            else:
                # 构造视觉分析请求
                ve_cmd = [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "视觉元素提取.py"),
                    "--out", str(out_dir),
                ]
                for path in theme_paths:
                    ve_cmd.extend(["--theme-image", str(path)])
                if args.garment_type:
                    ve_cmd.extend(["--garment-type", args.garment_type])
                if getattr(args, "user_prompt", ""):
                    ve_cmd.extend(["--user-prompt", args.user_prompt])
                run_step(ve_cmd)
                print("\n[提示] 视觉分析请求已构造。请用视觉模型阅读以下文件并输出 visual_elements.json：")
                print(f"  主题图: {theme_path}")
                if len(theme_paths) > 1:
                    print(f"  多图参考: {[str(p) for p in theme_paths]}")
                print(f"  提示词文件: {out_dir / 'ai_vision_prompt.txt'}")
                print(f"  预期输出: {ve_out}")
                print("  完成后重新运行本脚本并传入 --visual-elements 参数。\n")
                return 0

    if args.visual_elements and not ve_handled:
        ve_path = Path(args.visual_elements)
        if not ve_path.exists():
            raise RuntimeError(f"visual_elements 不存在: {ve_path}")
        # 保存正确的 visual_elements 缓存（只有文件存在且有效时才缓存）
        if args.reuse_cache and args.theme_images:
            ve_hash = {
                "theme_images": files_sha256(args.theme_images),
                "garment_type": args.garment_type,
                "user_prompt": getattr(args, "user_prompt", ""),
            }
            cache_save(out_dir, "visual_elements", ve_hash, ve_path)
        # 基于视觉元素分析生成设计简报与纹理提示词
        brief_cmd = [
            sys.executable,
            str(SKILL_DIR / "scripts" / "生成设计简报.py"),
            "--visual-elements", str(ve_path),
            "--out", str(out_dir),
        ]
        if args.garment_type:
            brief_cmd.extend(["--garment-type", args.garment_type])
        if getattr(args, "user_prompt", ""):
            brief_cmd.extend(["--user-prompt", args.user_prompt])
        run_step(brief_cmd)
        if not args.prompt_file:
            ve_path_obj = Path(args.visual_elements) if args.visual_elements else None
            texture_prompts, hero_prompt = _build_generation_prompts_from_visual_elements(out_dir, ve_path_obj)
            prompt_sanitization_report = []
            if texture_prompts:
                prompt_files = {}
                prompt_dir = out_dir / "generated_texture_prompts"
                prompt_dir.mkdir(parents=True, exist_ok=True)
                for texture_id, prompt_text in texture_prompts.items():
                    prompt_text = _sanitize_generation_prompt(
                        prompt_text,
                        texture_id=texture_id,
                        out_dir=out_dir,
                        report_items=prompt_sanitization_report,
                        prompt_role="final",
                    )
                    prompt_path = prompt_dir / f"{texture_id}.txt"
                    prompt_path.write_text(prompt_text, encoding="utf-8")
                    prompt_files[texture_id] = str(prompt_path)
                args.texture_prompt_files = prompt_files
                args.texture_prompt_file = prompt_files.get("main", "")
                args.prompt_file = args.texture_prompt_file
                print(f"[视觉提取] 已基于视觉分析自动生成3张单纹理提示词: {prompt_dir}")
            if hero_prompt:
                hero_prompt = _sanitize_generation_prompt(
                    hero_prompt,
                    texture_id="hero_motif_1",
                    out_dir=out_dir,
                    report_items=prompt_sanitization_report,
                    prompt_role="final",
                )
                hero_prompt_path = out_dir / "generated_hero_prompt.txt"
                hero_prompt_path.write_text(hero_prompt, encoding="utf-8")
                args.hero_prompt_file = str(hero_prompt_path)
                print(f"[视觉提取] 已基于视觉分析自动生成透明主图提示词: {hero_prompt_path}")
            _write_prompt_sanitization_report(out_dir, prompt_sanitization_report)

    # ============================================================
    # 尝试读取 palette（供颜色提示和纯色提取使用）
    # ============================================================
    palette = None
    style_profile_path = out_dir / "style_profile.json"
    if style_profile_path.exists():
        try:
            sp = json.loads(style_profile_path.read_text(encoding="utf-8"))
            palette = sp.get("palette")
        except Exception:
            pass

    # ============================================================
    # Neo AI 单源模式
    # ============================================================
    hero_motif_path = None
    board_path = None
    pipeline_variant_summaries = None
    pipeline_default_summary = None
    if args.texture_set:
        texture_set_path = Path(args.texture_set)
        if not texture_set_path.is_absolute():
            texture_set_path = texture_set_path.resolve() if texture_set_path.exists() else (out_dir / texture_set_path).resolve()
        if not texture_set_path.exists():
            raise RuntimeError(f"面料组合不存在: {texture_set_path}")
        texture_set_payload = load_json(texture_set_path)
        source_board = texture_set_payload.get("source_collection_board", "")
        board_path = Path(source_board).resolve() if source_board else Path(args.collection_board or texture_set_path).resolve()
        print(f"使用已提供面料组合: {texture_set_path}")
    else:
        if args.collection_board:
            board_path = Path(args.collection_board).resolve()
            if getattr(args, "hero_prompt_file", ""):
                hero_dir = out_dir / "neo_hero_motif"
                existing_hero = sorted(hero_dir.glob("collection_board_*.png")) + sorted(hero_dir.glob("collection_board_*.jpg"))
                if existing_hero:
                    hero_motif_path = existing_hero[-1].resolve()
                    print(f"[资产复用] 已存在AI生成透明主图，跳过重新生成: {hero_motif_path}")
                else:
                    hero_motif_path = generate_board(
                        args,
                        out_dir,
                        prompt_file=args.hero_prompt_file,
                        subdir="neo_hero_motif",
                        negative_prompt=HERO_NEGATIVE_PROMPT,
                    ).resolve()
        else:
            hero_dir = out_dir / "neo_hero_motif"
            existing_heroes = sorted(hero_dir.glob("collection_board_*.png")) + sorted(hero_dir.glob("collection_board_*.jpg"))
            texture_root = out_dir / "neo_textures"
            expected_textures = {tid: (texture_root / f"{tid}.png").resolve() for tid in DEFAULT_TEXTURE_IDS}
            existing_single_textures = all(path.exists() for path in expected_textures.values())
            if existing_single_textures and existing_heroes:
                texture_paths = expected_textures
                hero_motif_path = existing_heroes[-1].resolve()
                print(f"[资产复用] 已存在3张单纹理，跳过重新生成: {texture_root}")
                print(f"[资产复用] 已存在AI生成透明主图，跳过重新生成: {hero_motif_path}")
                if hero_motif_path:
                    hero_motif_path = hero_motif_path.resolve()
                prompt_map = load_texture_prompt_map(out_dir)
                texture_set_path, hero_motif_path, pipeline_variant_summaries, pipeline_default_summary = run_ready_single_texture_channel_pipeline(
                    args=args,
                    out_dir=out_dir,
                    texture_paths=texture_paths,
                    hero_motif_path=hero_motif_path,
                    pieces_path=pieces_path,
                    garment_map_path=garment_map_path,
                    template_assets=template_assets,
                    palette=palette,
                    prompt_map=prompt_map,
                )
            else:
                prompt_map = load_texture_prompt_map(out_dir)
                texture_set_path, hero_motif_path, pipeline_variant_summaries, pipeline_default_summary = run_single_texture_channel_pipeline(
                    args=args,
                    out_dir=out_dir,
                    pieces_path=pieces_path,
                    garment_map_path=garment_map_path,
                    template_assets=template_assets,
                    palette=palette,
                    prompt_map=prompt_map,
                )
        if args.collection_board:
            if not board_path or not board_path.exists():
                raise RuntimeError(f"面料看板未找到: {board_path}")
            print(f"使用面料看板: {board_path}")
            texture_set_path = crop_collection_board(board_path, out_dir, args.crop_inset, not args.no_tile_repair, palette=palette)
        else:
            print(f"使用3张单纹理面料资产: {texture_set_path}")
        if hero_motif_path:
            args.hero_motif_image = str(hero_motif_path)
            print(f"使用AI生成透明主图: {hero_motif_path}")

    # 旧看板入口的颜色协调性提示；默认单纹理路径不做看板校验。
    if palette and (not args.texture_set) and args.collection_board:
        color_warnings = validate_board_colors(board_path, palette)
        if color_warnings:
            print("[颜色提示] 以下面板颜色与 palette 偏差较大：")
            for w in color_warnings:
                print(f"  {w['panel']}: 实际 RGB{w['actual_rgb']} vs 预期 {w['expected_hex']} (偏差 {w['distance']})")
            warn_path = out_dir / "board_color_warnings.json"
            warn_path.write_text(json.dumps(color_warnings, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  详细报告: {warn_path}")
        else:
            print("[颜色提示] 所有面板颜色与 palette 协调。")

    print("[模板复用] 使用模板库 garment_map。")

    # ============================================================
    # Phase 3: 生产规划、填充计划与渲染
    # ============================================================
    if pipeline_variant_summaries is not None and pipeline_default_summary is not None:
        variant_summaries = pipeline_variant_summaries
        default_summary = pipeline_default_summary
        default_rendered_dir = Path(default_summary["渲染目录"])
        build_production_context(
            args,
            out_dir,
            pieces_path=pieces_path,
            garment_map_path=garment_map_path,
            template_assets=template_assets,
        )

        summary = {
            "单纹理资产": str((out_dir / "neo_textures").resolve()),
            "面料看板": "",
            "AI生成透明主图": str(hero_motif_path) if hero_motif_path else "",
            "面料组合": str(texture_set_path.resolve()),
            "裁片清单": str(pieces_path.resolve()),
            "部位映射": str(garment_map_path.resolve()),
            "裁片填充计划": str((out_dir / "piece_fill_plan.json").resolve()),
            "渲染目录": str(default_rendered_dir.resolve()),
            "预览图": str((default_rendered_dir / "preview.png").resolve()),
            "白底预览图": str((default_rendered_dir / "preview_white.jpg").resolve()),
            "清单": str((default_rendered_dir / "texture_fill_manifest.json").resolve()),
            "裁片模板变体": variant_summaries,
        }
        write_json(out_dir / "automation_summary.json", summary)
        success_count = sum(1 for item in variant_summaries if item.get("status") == "success")
        failed_count = sum(1 for item in variant_summaries if item.get("status") == "failed")
        user_summary = {k: v for k, v in summary.items() if k != "裁片模板变体"}
        fail_suffix = f"，{failed_count} 个通道失败，详见 automation_summary.json" if failed_count else ""
        user_summary["裁片模板变体"] = f"已生成 {success_count} 套单纹理结果至 variants/ 目录，不在此处展示{fail_suffix}"
        print(json.dumps(user_summary, ensure_ascii=False, indent=2))
        return 0

    ensure_theme_front_split(args, out_dir, texture_set_path)
    build_production_context(
        args,
        out_dir,
        pieces_path=pieces_path,
        garment_map_path=garment_map_path,
        template_assets=template_assets,
    )

    rc = _apply_or_request_production_plan(
        args, out_dir, texture_set_path, pieces_path, garment_map_path, template_assets
    )
    if rc:
        return 0 if rc == 2 else rc

    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--garment-map", str(garment_map_path),
        "--out", str(out_dir),
    ]
    if args.brief:
        plan_cmd.extend(["--brief", args.brief])
    auto_ai_plan = out_dir / "ai_piece_fill_plan.json"
    if auto_ai_plan.exists():
        plan_cmd.extend(["--ai-plan", str(auto_ai_plan)])
        print(f"[自动] 检测到 AI 填充计划，自动使用: {auto_ai_plan}")
    run_step(plan_cmd)

    variant_summaries = render_texture_variants(
        out_dir=out_dir,
        texture_set_path=texture_set_path,
        fill_plan_path=out_dir / "piece_fill_plan.json",
        pieces_path=pieces_path,
        default_rendered_dir=out_dir / "variants" / "main",
        args=args,
    )

    # Default preview now points to variants/main/rendered (or first variant if main missing)
    main_variant = next((s for s in variant_summaries if s["纹理ID"] == "main"), None)
    default_summary = main_variant or (variant_summaries[0] if variant_summaries else None)
    default_rendered_dir = Path(default_summary["渲染目录"]) if default_summary else out_dir / "variants" / "main"

    summary = {
        "单纹理资产": str((out_dir / "neo_textures").resolve()) if not args.texture_set and not args.collection_board else "",
        "面料看板": str(board_path) if board_path else "",
        "AI生成透明主图": str(hero_motif_path) if hero_motif_path else "",
        "面料组合": str(texture_set_path.resolve()),
        "裁片清单": str(pieces_path.resolve()),
        "部位映射": str(garment_map_path.resolve()),
        "裁片填充计划": str((out_dir / "piece_fill_plan.json").resolve()),
        "渲染目录": str(default_rendered_dir.resolve()),
        "预览图": str((default_rendered_dir / "preview.png").resolve()),
        "白底预览图": str((default_rendered_dir / "preview_white.jpg").resolve()),
        "清单": str((default_rendered_dir / "texture_fill_manifest.json").resolve()),
        "裁片模板变体": variant_summaries,
    }
    write_json(out_dir / "automation_summary.json", summary)
    user_summary = {k: v for k, v in summary.items() if k != "裁片模板变体"}
    user_summary["裁片模板变体"] = f"已生成 {len(variant_summaries)} 套单纹理结果至 variants/ 目录，不在此处展示"
    print(json.dumps(user_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
