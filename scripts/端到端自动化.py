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
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image


SKILL_DIR = Path(__file__).resolve().parents[1]
NEO_AI_SCRIPT = SKILL_DIR.parent / "neo-ai" / "scripts" / "generate_texture_collection_board.py"
NEO_UPLOAD_SCRIPT = SKILL_DIR.parent / "neo-ai" / "scripts" / "upload_oss.py"
DEFAULT_TEXTURE_IDS = ("main", "secondary", "accent_light")
GENERATION_STATUS_FILE = "request_status.json"
HERO_NEGATIVE_PROMPT = (
    "text, labels, captions, titles, typography, words, letters, signage, logo, watermark, "
    "plain light box, colored background box, filled rectangle, background art, scenery, landscape, environment, "
    "checkerboard transparency preview, fake transparency grid, "
    "full illustration scene, poster composition, sticker sheet, garment mockup, fashion model, mannequin, "
    "person wearing garment, product photo, lookbook, semi-transparent full-image patch"
)

# 导入模板加载器
sys.path.insert(0, str(SKILL_DIR / "scripts"))
from prompt_blocks import build_single_texture_prompt_en, build_transparent_hero_prompt_en
try:
    from prompt_sanitizer import sanitize_prompt_with_report
except Exception:
    sanitize_prompt_with_report = None
try:
    from template_loader import resolve_template_assets
    HAS_TEMPLATE_LOADER = True
except Exception:
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


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


def _cmd_option_value(cmd: list[str], option: str, default: str = "") -> str:
    try:
        idx = cmd.index(option)
    except ValueError:
        return default
    if idx + 1 >= len(cmd):
        return default
    return str(cmd[idx + 1])


def _cmd_option_values(cmd: list[str], option: str) -> list[str]:
    values = []
    for idx, item in enumerate(cmd):
        if item == option and idx + 1 < len(cmd):
            values.append(str(cmd[idx + 1]))
    return values


def _redact_cmd(cmd: list[str]) -> list[str]:
    redacted = []
    skip_next = False
    for idx, item in enumerate(cmd):
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(str(item))
        if item in {"--token", "--access-token"}:
            skip_next = True
    return redacted


def _prompt_text_from_cmd(cmd: list[str]) -> str:
    prompt_file = _cmd_option_value(cmd, "--prompt-file")
    if prompt_file and Path(prompt_file).exists():
        return Path(prompt_file).read_text(encoding="utf-8").strip()
    return _cmd_option_value(cmd, "--prompt")


def _generation_identity(texture_id: str, cmd: list[str]) -> dict:
    prompt_file = _cmd_option_value(cmd, "--prompt-file")
    prompt_text = _prompt_text_from_cmd(cmd)
    negative_prompt = _cmd_option_value(cmd, "--negative-prompt")
    return {
        "texture_id": texture_id,
        "prompt_sha256": text_sha256(prompt_text) if prompt_text else "",
        "prompt_file": str(Path(prompt_file).resolve()) if prompt_file else "",
        "reference_images": _cmd_option_values(cmd, "--reference-image"),
        "model": _cmd_option_value(cmd, "--model"),
        "size": _cmd_option_value(cmd, "--size"),
        "output_format": _cmd_option_value(cmd, "--output-format", "png"),
        "num_images": _cmd_option_value(cmd, "--num-images", "1"),
        "negative_prompt_sha256": text_sha256(negative_prompt) if negative_prompt else "",
    }


def _status_path(work_dir: Path) -> Path:
    return work_dir / GENERATION_STATUS_FILE


def _write_generation_status(
    work_dir: Path,
    texture_id: str,
    status: str,
    cmd: list[str],
    *,
    attempt: int = 0,
    max_attempts: int = 1,
    task_code: str = "",
    error: str = "",
    generated_image: str = "",
    final_asset: str = "",
    extra: dict | None = None,
) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    previous = {}
    path = _status_path(work_dir)
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    payload = {
        **previous,
        "request_id": "neo_generation_request_v1",
        "texture_id": texture_id,
        "status": status,
        "attempt": attempt or previous.get("attempt", 0),
        "max_attempts": max_attempts or previous.get("max_attempts", 1),
        "command": _redact_cmd(cmd),
        "identity": _generation_identity(texture_id, cmd),
        "updated_at": _now_iso(),
    }
    if status == "pending":
        payload.setdefault("created_at", _now_iso())
        payload["started_at"] = _now_iso()
        payload.pop("error", None)
    if status == "submitted":
        payload["submitted_at"] = _now_iso()
    if status in {"success", "failed", "downloaded"}:
        payload["finished_at"] = _now_iso()
    if task_code:
        payload["task_code"] = task_code
    if generated_image:
        payload["generated_image"] = generated_image
    if final_asset:
        payload["final_asset"] = final_asset
    if error:
        payload["error"] = error
    if extra:
        payload.update(extra)
    write_json(path, payload)


def _metadata_matches_identity(metadata: dict, expected_identity: dict) -> bool:
    prompt = metadata.get("prompt", "")
    expected_prompt_hash = expected_identity.get("prompt_sha256", "")
    if expected_prompt_hash and text_sha256(prompt.strip()) != expected_prompt_hash:
        return False
    expected_negative_hash = expected_identity.get("negative_prompt_sha256", "")
    if expected_negative_hash and text_sha256(str(metadata.get("negative_prompt", "")).strip()) != expected_negative_hash:
        return False
    if str(metadata.get("model", "")) != str(expected_identity.get("model", "")):
        return False
    if str(metadata.get("size", "")) != str(expected_identity.get("size", "")):
        return False
    expected_refs = expected_identity.get("reference_images", []) or []
    actual_refs = metadata.get("reference_images", []) or []
    if actual_refs != expected_refs:
        return False
    return True


def _status_matches_identity(status: dict, expected_identity: dict) -> bool:
    identity = status.get("identity") or {}
    keys = ("prompt_sha256", "reference_images", "model", "size", "output_format", "num_images", "negative_prompt_sha256")
    return all(identity.get(key) == expected_identity.get(key) for key in keys)


def _existing_generation_matches(work_dir: Path, final_asset: Path, texture_id: str, cmd: list[str]) -> bool:
    if not final_asset.exists():
        return False
    expected_identity = _generation_identity(texture_id, cmd)
    status_path = _status_path(work_dir)
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("status") == "success" and _status_matches_identity(status, expected_identity):
                return True
        except Exception:
            pass
    metadata_path = work_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            return _metadata_matches_identity(metadata, expected_identity)
        except Exception:
            return False
    return False


def _load_existing_reference_images(out_dir: Path) -> list[str]:
    path = out_dir / "neo_reference_images.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    refs = payload.get("reference_images", [])
    if not isinstance(refs, list):
        return []
    urls = []
    for item in refs:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
        elif isinstance(item, str):
            urls.append(item)
    return urls


def _generation_retry_count(texture_id: str) -> int:
    if texture_id == "main":
        return 2
    if texture_id in {"secondary", "accent_light", "hero_motif_1"}:
        return 1
    return 0


def _run_neo_generation_with_status(
    label: str,
    texture_id: str,
    work_dir: Path,
    cmd: list[str],
    *,
    env: dict | None = None,
    retries: int = 0,
    on_launch=None,
) -> Path:
    """Run one Neo generation command, recording status and retrying failures."""
    max_attempts = retries + 1
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        _write_generation_status(
            work_dir,
            texture_id,
            "pending",
            cmd,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        print(f"[Neo AI] 启动生成任务: {label} ({texture_id}) attempt={attempt}/{max_attempts}")
        print("运行:", " ".join(_redact_cmd(cmd)))
        task_code = ""
        output_tail: list[str] = []
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if on_launch:
            on_launch()
            on_launch = None
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            output_tail.append(line.rstrip())
            output_tail = output_tail[-40:]
            match = re.search(r"\bTask:\s*(\S+)", line)
            if match and not task_code:
                task_code = match.group(1)
                _write_generation_status(
                    work_dir,
                    texture_id,
                    "submitted",
                    cmd,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    task_code=task_code,
                )
        proc.stdout.close()
        rc = proc.wait()
        if rc == 0:
            try:
                generated = latest_collection_board(work_dir)
                try:
                    metadata = json.loads((work_dir / "metadata.json").read_text(encoding="utf-8"))
                    task_code = task_code or metadata.get("task_code", "")
                except Exception:
                    pass
                _write_generation_status(
                    work_dir,
                    texture_id,
                    "downloaded",
                    cmd,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    task_code=task_code,
                    generated_image=str(generated.resolve()),
                )
                return generated
            except Exception as exc:
                last_error = str(exc)
        else:
            last_error = f"exit code {rc}; tail=" + " | ".join(output_tail[-8:])
        if attempt < max_attempts:
            _write_generation_status(
                work_dir,
                texture_id,
                "failed",
                cmd,
                attempt=attempt,
                max_attempts=max_attempts,
                task_code=task_code,
                error=last_error,
            )
            print(f"[Neo AI] {label} 失败，准备重试: {last_error}", file=sys.stderr)
            time.sleep(min(2 * attempt, 6))

    _write_generation_status(
        work_dir,
        texture_id,
        "failed",
        cmd,
        attempt=max_attempts,
        max_attempts=max_attempts,
        error=last_error,
    )
    raise RuntimeError(last_error or f"{label} 生成失败")


def _mark_generation_success(work_dir: Path, texture_id: str, cmd: list[str], final_asset: Path, generated_image: Path | None = None) -> None:
    _write_generation_status(
        work_dir,
        texture_id,
        "success",
        cmd,
        final_asset=str(final_asset.resolve()),
        generated_image=str(generated_image.resolve()) if generated_image else "",
    )


def latest_collection_board(output_dir: Path) -> Path:
    """在输出目录中找到最新的 Neo AI 生成图像。"""
    candidates = (
        sorted(output_dir.glob("collection_board_*.png"))
        + sorted(output_dir.glob("collection_board_*.jpg"))
        + sorted(output_dir.glob("collection_board_*.jpeg"))
        + sorted(output_dir.glob("collection_board_*.webp"))
    )
    if not candidates:
        raise RuntimeError(f"输出目录中未找到AI生成图像: {output_dir}")
    return candidates[-1]


def _sanitize_generation_prompt(
    prompt_text: str,
    *,
    prompt_role: str = "final",
) -> str:
    """Final preflight rewrite before a prompt is sent to Neo AI."""
    if not sanitize_prompt_with_report:
        return prompt_text
    report = sanitize_prompt_with_report(prompt_text, domain="fashion", prompt_role=prompt_role)
    return report.sanitized_text


def _sanitize_prompt_file_for_neo(prompt_file: str, output_dir: Path, texture_id: str = "neo_prompt") -> str:
    """Sanitize externally supplied prompt files at the Neo boundary."""
    if not prompt_file or not sanitize_prompt_with_report:
        return prompt_file
    src = Path(prompt_file)
    if not src.exists() or not src.is_file():
        return prompt_file
    text = src.read_text(encoding="utf-8")
    sanitized = _sanitize_generation_prompt(text, prompt_role="final")
    if sanitized == text:
        return prompt_file
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"{src.stem}.sanitized{src.suffix or '.txt'}"
    dest.write_text(sanitized, encoding="utf-8")
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
        image.convert("RGB").save(dest)
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

    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--garment-map", str(garment_map_path),
        "--out", str(variant_dir),
    ]
    if args.visual_elements:
        plan_cmd.extend(["--visual-elements", args.visual_elements])
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
        retries = _generation_retry_count(texture_id)
        if texture_id == "hero_motif_1":
            generated = _run_neo_generation_with_status(
                label,
                texture_id,
                work_dir,
                cmd,
                env=os.environ.copy(),
                retries=retries,
                on_launch=hero_started.set,
            )
        else:
            generated = _run_neo_generation_with_status(
                label,
                texture_id,
                work_dir,
                cmd,
                env=os.environ.copy(),
                retries=retries,
            )
        return label, texture_id, generated

    hero_motif_path: Path | None = None
    split_assets: dict | None = None
    ready_textures: dict[str, Path] = {}
    scheduled_channels: set[str] = set()
    variant_summaries: list[dict] = []
    channel_futures: dict[concurrent.futures.Future, str] = {}
    generation_failures: list[tuple[str, str, Exception]] = []
    generation_cmds = {texture_id: cmd for _, texture_id, _, cmd in generation_jobs}

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
                    _mark_generation_success(hero_dir, texture_id, generation_cmds[texture_id], hero_motif_path, generated)
                    args.hero_motif_image = str(hero_motif_path)
                    if not create_front_split_assets:
                        raise RuntimeError("theme_front_splitter 不可用，无法生成主题前片连续资产。")
                    split_assets = create_front_split_assets(hero_motif_path, out_dir)
                    print(f"[透明主图] {hero_motif_path}")
                    print(f"[主题前片] 已生成完整前身与兼容切半资产: {split_assets.get('full', '')}, {split_assets['left']}, {split_assets['right']}")
                else:
                    texture_path = _copy_generated_image(generated, texture_root / f"{texture_id}.png").resolve()
                    _mark_generation_success(texture_root / texture_id, texture_id, generation_cmds[texture_id], texture_path, generated)
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
    if not any(item.get("status") == "success" and item.get("纹理ID") == "main" for item in variant_summaries):
        main_error = next(
            (item.get("error", "") for item in variant_summaries if item.get("纹理ID") == "main"),
            "main 纹理未生成",
        )
        raise RuntimeError(f"main 纹理是必需资产，生成失败，不能使用 secondary/accent_light 作为默认结果: {main_error}")

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
    palette: dict | None,
    prompt_map: dict[str, str],
) -> tuple[Path, Path, list[dict], dict]:
    """Run per-texture downstream channels for already available assets."""
    if not create_front_split_assets:
        raise RuntimeError("theme_front_splitter 不可用，无法生成主题前片连续资产。")
    args.hero_motif_image = str(hero_motif_path)
    split_assets = create_front_split_assets(hero_motif_path, out_dir)
    pipeline_workers = max(1, int(getattr(args, "pipeline_workers", 3) or 3))
    pipeline_workers = min(pipeline_workers, len(DEFAULT_TEXTURE_IDS))
    print(f"[纹理通道] 复用已生成资产，并行后处理 workers={pipeline_workers}")
    print(f"[主题前片] 已生成完整前身与兼容切半资产: {split_assets.get('full', '')}, {split_assets['left']}, {split_assets['right']}")

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
    if not any(item.get("status") == "success" and item.get("纹理ID") == "main" for item in variant_summaries):
        main_error = next(
            (item.get("error", "") for item in variant_summaries if item.get("纹理ID") == "main"),
            "main 纹理未生成",
        )
        raise RuntimeError(f"main 纹理是必需资产，生成失败，不能使用 secondary/accent_light 作为默认结果: {main_error}")

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
        print(f"[主题前片] 已从{source_label}生成并注册完整前身与兼容切半资产: {split_assets.get('full', '')}, {split_assets['left']}, {split_assets['right']}")
        return split_assets
    except Exception as exc:
        print(f"[警告] 主题前片连续资产生成失败，将继续使用普通面料规划: {exc}", file=sys.stderr)
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
    # Preserve theme front motifs so each variant still carries a seam-locked
    # full-front hero image. All other motifs are dropped.
    theme_motifs = [
        copy.deepcopy(m) for m in texture_set.get("motifs", [])
        if m.get("motif_id") in {"theme_front_full", "theme_front_left", "theme_front_right"}
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
                # Preserve theme front motifs across all variants
                if layer.get("motif_id") in {"theme_front_full", "theme_front_left", "theme_front_right"}:
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
        is_theme_split = piece_motif_id in {"theme_front_full", "theme_front_left", "theme_front_right"}
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


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Neo AI 单纹理面料资产并自动渲染服装裁片。")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--texture-set", default="", help="已有 texture_set.json。提供后跳过面料生成，直接使用该面料组合继续裁片映射、填充和渲染。")
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

    args = parser.parse_args()

    args.prompt_file = ""
    args.texture_prompt_file = ""
    args.texture_prompt_files = {}
    args.hero_prompt_file = ""
    args.hero_motif_image = ""
    if args.mode == "fast":
        print("[模式] fast")

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

    # ===== garment_type 校验 =====
    effective_garment_type = args.garment_type.strip()
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
            if texture_prompts:
                prompt_files = {}
                prompt_dir = out_dir / "generated_texture_prompts"
                prompt_dir.mkdir(parents=True, exist_ok=True)
                for texture_id, prompt_text in texture_prompts.items():
                    prompt_text = _sanitize_generation_prompt(prompt_text, prompt_role="final")
                    prompt_path = prompt_dir / f"{texture_id}.txt"
                    prompt_path.write_text(prompt_text, encoding="utf-8")
                    prompt_files[texture_id] = str(prompt_path)
                args.texture_prompt_files = prompt_files
                args.texture_prompt_file = prompt_files.get("main", "")
                args.prompt_file = args.texture_prompt_file
                print(f"[视觉提取] 已基于视觉分析自动生成3张单纹理提示词: {prompt_dir}")
            if hero_prompt:
                hero_prompt = _sanitize_generation_prompt(hero_prompt, prompt_role="final")
                hero_prompt_path = out_dir / "generated_hero_prompt.txt"
                hero_prompt_path.write_text(hero_prompt, encoding="utf-8")
                args.hero_prompt_file = str(hero_prompt_path)
                print(f"[视觉提取] 已基于视觉分析自动生成透明主图提示词: {hero_prompt_path}")

    # ============================================================
    # ============================================================
    palette = None

    # ============================================================
    # Neo AI 单源模式
    # ============================================================
    hero_motif_path = None
    pipeline_variant_summaries = None
    pipeline_default_summary = None
    if args.texture_set:
        texture_set_path = Path(args.texture_set)
        if not texture_set_path.is_absolute():
            texture_set_path = texture_set_path.resolve() if texture_set_path.exists() else (out_dir / texture_set_path).resolve()
        if not texture_set_path.exists():
            raise RuntimeError(f"面料组合不存在: {texture_set_path}")
        print(f"使用已提供面料组合: {texture_set_path}")
    else:
        hero_dir = out_dir / "neo_hero_motif"
        existing_heroes = sorted(hero_dir.glob("collection_board_*.png")) + sorted(hero_dir.glob("collection_board_*.jpg"))
        texture_root = out_dir / "neo_textures"
        expected_textures = {tid: (texture_root / f"{tid}.png").resolve() for tid in DEFAULT_TEXTURE_IDS}
        existing_refs = _load_existing_reference_images(out_dir)
        existing_hero_path = existing_heroes[-1].resolve() if existing_heroes else None
        expected_cmds = {
            tid: _neo_generation_cmd(args, texture_root / tid, args.texture_prompt_files[tid], reference_images=existing_refs)
            for tid in DEFAULT_TEXTURE_IDS
            if args.texture_prompt_files.get(tid)
        }
        expected_hero_cmd = (
            _neo_generation_cmd(args, hero_dir, args.hero_prompt_file, negative_prompt=HERO_NEGATIVE_PROMPT, reference_images=existing_refs)
            if getattr(args, "hero_prompt_file", "") else []
        )
        reusable_textures = {
            tid: _existing_generation_matches(texture_root / tid, expected_textures[tid], tid, expected_cmds.get(tid, []))
            for tid in DEFAULT_TEXTURE_IDS
            if expected_cmds.get(tid)
        }
        reusable_hero = bool(
            existing_hero_path
            and expected_hero_cmd
            and _existing_generation_matches(hero_dir, existing_hero_path, "hero_motif_1", expected_hero_cmd)
        )
        existing_single_textures = all(reusable_textures.get(tid, False) for tid in DEFAULT_TEXTURE_IDS)
        if existing_single_textures and reusable_hero and existing_hero_path:
            texture_paths = expected_textures
            hero_motif_path = existing_hero_path
            print(f"[资产复用] 已存在3张单纹理且请求身份匹配，跳过重新生成: {texture_root}")
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
                palette=palette,
                prompt_map=prompt_map,
            )
        else:
            if any(path.exists() for path in expected_textures.values()) or existing_heroes:
                invalid = [tid for tid in DEFAULT_TEXTURE_IDS if not reusable_textures.get(tid, False)]
                if not reusable_hero:
                    invalid.append("hero_motif_1")
                print(f"[资产复用] 已有资产与本次请求不匹配或不完整，将重新生成: {', '.join(invalid)}")
            prompt_map = load_texture_prompt_map(out_dir)
            texture_set_path, hero_motif_path, pipeline_variant_summaries, pipeline_default_summary = run_single_texture_channel_pipeline(
                args=args,
                out_dir=out_dir,
                pieces_path=pieces_path,
                garment_map_path=garment_map_path,
                palette=palette,
                prompt_map=prompt_map,
            )
        print(f"使用3张单纹理面料资产: {texture_set_path}")
        if hero_motif_path:
            args.hero_motif_image = str(hero_motif_path)
            print(f"使用AI生成透明主图: {hero_motif_path}")

    print("[模板复用] 使用模板库 garment_map。")

    # ============================================================
    # Phase 3: 填充计划与渲染
    # ============================================================
    if pipeline_variant_summaries is not None and pipeline_default_summary is not None:
        variant_summaries = pipeline_variant_summaries
        default_summary = pipeline_default_summary
        default_rendered_dir = Path(default_summary["渲染目录"])
        summary = {
            "单纹理资产": str((out_dir / "neo_textures").resolve()),

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

    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces", str(pieces_path),
        "--texture-set", str(texture_set_path),
        "--garment-map", str(garment_map_path),
        "--out", str(out_dir),
    ]
    run_step(plan_cmd)

    variant_summaries = render_texture_variants(
        out_dir=out_dir,
        texture_set_path=texture_set_path,
        fill_plan_path=out_dir / "piece_fill_plan.json",
        pieces_path=pieces_path,
        args=args,
    )

    # Default preview now points to variants/main/rendered (or first variant if main missing)
    main_variant = next((s for s in variant_summaries if s["纹理ID"] == "main"), None)
    default_summary = main_variant or (variant_summaries[0] if variant_summaries else None)
    default_rendered_dir = Path(default_summary["渲染目录"]) if default_summary else out_dir / "variants" / "main"

    summary = {
        "单纹理资产": str((out_dir / "neo_textures").resolve()) if not args.texture_set else "",

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
