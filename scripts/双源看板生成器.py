#!/usr/bin/env python3
"""
双源看板生成器：并行调用 Neo AI 和 libtv-skill 生成面料看板。

职责：
- 读取 dual_collection_prompts.json，获取两套不同但风格一致的提示词
- 使用 ThreadPoolExecutor 并行提交 Neo AI 和 libtv 生成任务
- 两个源都必须被调用并等待结果；一源成功且另一源明确失败/超时后可继续
- 双源均失败才重试，重试会重新发起 AI 生成
- 返回可用的看板路径列表

使用方式（独立运行）：
    python3 双源看板生成器.py \
        --dual-prompts /path/to/dual_collection_prompts.json \
        --out /path/to/output \
        --token "$NEODOMAIN_ACCESS_TOKEN" \
        --libtv-key "$LIBTV_ACCESS_KEY"

使用方式（模块导入）：
    from 双源看板生成器 import DualBoardGenerator
    generator = DualBoardGenerator(out_dir, neo_token, libtv_key)
    results = generator.generate(dual_prompts_path)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 与端到端脚本保持一致：当前文件在 auto-garment-producer/scripts/ 下，
# SKILL_DIR 是 auto-garment-producer，SKILL_DIR.parent 是同级 skills 目录。
SKILL_DIR = Path(__file__).resolve().parents[1]
_SKILLS_ROOT = SKILL_DIR.parent
_HOME = Path.home()

def _first_existing_path(candidates: list[Path]) -> Path:
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def resolve_neo_ai_script() -> Path:
    """解析 Neo AI 面料看板脚本路径，兼容 .agents 与 .codex skill 安装位置。"""
    override = os.environ.get("NEO_AI_SCRIPT", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend([
        SKILL_DIR.parent / "neo-ai" / "scripts" / "generate_texture_collection_board.py",
        _HOME / ".agents" / "skills" / "neo-ai" / "scripts" / "generate_texture_collection_board.py",
        _HOME / ".codex" / "skills" / "neo-ai" / "scripts" / "generate_texture_collection_board.py",
    ])
    return _first_existing_path(candidates)


def resolve_libtv_script_dir() -> Path:
    """解析 libtv-skill 脚本目录，默认与本 skill 同级。"""
    override = os.environ.get("LIBTV_SCRIPT_DIR", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend([
        SKILL_DIR.parent / "libtv-skill" / "scripts",
        _HOME / ".agents" / "skills" / "libtv-skill" / "scripts",
        _HOME / ".codex" / "skills" / "libtv-skill" / "scripts",
    ])
    return _first_existing_path(candidates)


NEO_AI_SCRIPT = resolve_neo_ai_script()
LIBTV_SCRIPT_DIR = resolve_libtv_script_dir()


class DualBoardGenerationError(Exception):
    """双源看板生成失败的统一异常。"""
    pass


def _latest_collection_board(output_dir: Path) -> Path:
    """在输出目录中找到最新的面料看板图像。"""
    candidates = (
        sorted(output_dir.glob("collection_board_*.png"))
        + sorted(output_dir.glob("collection_board_*.jpg"))
        + sorted(output_dir.glob("collection_board_*.jpeg"))
        + sorted(output_dir.glob("collection_board_*.webp"))
    )
    if not candidates:
        raise RuntimeError(f"输出目录中未找到面料看板图像: {output_dir}")
    return candidates[-1]


def _run_subprocess(cmd: list[str], env: dict | None = None, timeout: int = 300) -> str:
    """运行子进程并返回 stdout 文本。"""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()[:500] if result.stderr else ""
        raise RuntimeError(f"子进程失败 (rc={result.returncode}): {stderr}")
    return result.stdout


def _run_subprocess_capture(cmd: list[str], env: dict | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    """运行子进程，保留 stdout/stderr，调用方负责解释 returncode。"""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _truncate(text: str, limit: int = 1200) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _redact_cmd(cmd: list[str]) -> list[str]:
    """隐藏命令中的 token/key，避免健康报告泄露凭证。"""
    redacted = []
    hide_next = False
    for item in cmd:
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        redacted.append(item)
        if item in {"--token", "--libtv-key", "--access-key"}:
            hide_next = True
    return redacted


def validate_collection_board_shape(board_path: Path) -> dict:
    """轻量验证输出是纹理看板，不接受明显的正面效果图/产品照形态。"""
    try:
        from PIL import Image
        with Image.open(board_path) as img:
            width, height = img.size
    except Exception as exc:
        return {
            "path": str(board_path),
            "ok": False,
            "message": f"无法读取看板图片: {exc}",
        }

    ratio = width / max(1, height)
    ok = 0.92 <= ratio <= 1.08 and min(width, height) >= 512
    message = "ok" if ok else (
        f"看板尺寸异常: {width}x{height} (ratio={ratio:.2f})。"
        "期望为 3x3 面料九宫格看板，宽高比应接近 1:1 且短边不低于 512px。"
        "请确认输出不是正面成衣效果图、模特上身图或产品照。"
    )
    return {
        "path": str(board_path),
        "ok": ok,
        "width": width,
        "height": height,
        "aspect_ratio": round(ratio, 3),
        "message": message,
    }


class DualBoardGenerator:
    """双源看板生成器：并行调用 Neo AI 和 libtv。"""

    def __init__(
        self,
        out_dir: Path,
        neo_token: str,
        libtv_key: str,
        max_retries: int = 2,
        timeout: int = 300,
        neo_model: str = "gemini-3-pro-image-preview",
        neo_size: str = "2K",
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.neo_token = neo_token or os.environ.get("NEODOMAIN_ACCESS_TOKEN", "")
        self.libtv_key = libtv_key or os.environ.get("LIBTV_ACCESS_KEY", "")
        self.max_retries = max_retries
        self.timeout = timeout
        self.neo_model = neo_model
        self.neo_size = neo_size
        self.neo_script = resolve_neo_ai_script()
        self.libtv_script_dir = resolve_libtv_script_dir()
        self.libtv_generate_script = self.libtv_script_dir / "generate_texture_collection_board.py"
        self.health_report_path = self.out_dir / "dual_source_health_report.json"
        self.invocations: list[dict] = []

    def preflight(self) -> dict:
        """检查 Neo AI 与 libtv-skill 是否可调用，并写出健康报告。"""
        libtv_required = ["generate_texture_collection_board.py", "create_session.py", "query_session.py", "download_results.py"]
        source_summary = self._source_summary()
        report = {
            "neo": {
                "script": str(self.neo_script),
                "script_exists": self.neo_script.exists(),
                "token_present": bool(self.neo_token),
                "ok": False,
                "errors": [],
            },
            "libtv": {
                "script_dir": str(self.libtv_script_dir),
                "script_dir_exists": self.libtv_script_dir.exists(),
                "required_scripts": {
                    name: (self.libtv_script_dir / name).exists()
                    for name in libtv_required
                },
                "token_present": bool(self.libtv_key),
                "ok": False,
                "errors": [],
            },
            "policy": {
                "both_sources_must_be_invoked": True,
                "one_success_after_other_failure_can_continue": True,
                "retry_only_when_both_sources_fail": True,
                "forbidden_outputs": [
                    "front-view garment render",
                    "garment mockup",
                    "model wearing garment",
                    "mannequin render",
                    "product photo",
                ],
                "allowed_outputs": [
                    "textile collection board",
                    "texture_set.json",
                    "transparent pattern-piece PNG",
                    "pattern-piece preview/contact sheet",
                    "QC report",
                ],
            },
            "invocations": list(self.invocations),
            "source_summary": source_summary,
            "dual_run_status": source_summary.get("dual_run_status", "not_started"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

        if not report["neo"]["script_exists"]:
            report["neo"]["errors"].append("Neo AI 面料看板脚本不存在")
        if not report["neo"]["token_present"]:
            report["neo"]["errors"].append("缺少 NEODOMAIN_ACCESS_TOKEN 或 --token")
        report["neo"]["ok"] = not report["neo"]["errors"]

        if not report["libtv"]["script_dir_exists"]:
            report["libtv"]["errors"].append("libtv-skill scripts 目录不存在")
        missing_libtv = [name for name, exists in report["libtv"]["required_scripts"].items() if not exists]
        if missing_libtv:
            report["libtv"]["errors"].append(f"libtv-skill 缺少脚本: {', '.join(missing_libtv)}")
        if not report["libtv"]["token_present"]:
            report["libtv"]["errors"].append("缺少 LIBTV_ACCESS_KEY 或 --libtv-key")
        report["libtv"]["ok"] = not report["libtv"]["errors"]

        report["overall_ok"] = bool(report["neo"]["ok"] and report["libtv"]["ok"])
        self.health_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _source_summary(self) -> dict:
        summary = {}
        failure_statuses = {"failed", "poll_timeout", "no_image_result", "no_valid_3x3_board"}
        for source in ("neo", "libtv"):
            events = [item for item in self.invocations if item.get("source") == source]
            statuses = [item.get("status", "") for item in events]
            last_status = statuses[-1] if statuses else "not_started"
            failed = bool(
                last_status.endswith("_failed")
                or last_status in failure_statuses
                or any(item.get("error_type") for item in events if item.get("status") == last_status)
            )
            summary[source] = {
                "called": bool(events),
                "succeeded": last_status == "succeeded",
                "failed": failed,
                "last_status": last_status,
                "event_count": len(events),
            }
        neo_ok = summary["neo"]["succeeded"]
        libtv_ok = summary["libtv"]["succeeded"]
        if neo_ok and libtv_ok:
            dual_run_status = "both_succeeded"
        elif neo_ok and summary["libtv"]["failed"]:
            dual_run_status = "neo_only_libtv_failed"
        elif libtv_ok and summary["neo"]["failed"]:
            dual_run_status = "libtv_only_neo_failed"
        elif summary["neo"]["failed"] and summary["libtv"]["failed"]:
            dual_run_status = "both_failed"
        elif summary["neo"]["called"] or summary["libtv"]["called"]:
            dual_run_status = "in_progress"
        else:
            dual_run_status = "not_started"
        summary["dual_run_status"] = dual_run_status
        return summary

    def ensure_preflight_ok(self) -> None:
        report = self.preflight()
        if report.get("overall_ok"):
            print(f"[双源健康检查] 通过: {self.health_report_path}")
            return
        problems = []
        for source in ("neo", "libtv"):
            errors = report.get(source, {}).get("errors", [])
            if errors:
                problems.append(f"{source}: {'; '.join(errors)}")
        raise DualBoardGenerationError(
            "双源健康检查失败，不能进入主题图纹理生成。"
            f" 报告: {self.health_report_path}；问题: {' | '.join(problems)}"
        )

    def _record_invocation(
        self,
        source: str,
        cmd: list[str] | None,
        status: str,
        message: str = "",
        **extra,
    ) -> None:
        event = {
            "source": source,
            "cmd": _redact_cmd(cmd or []),
            "status": status,
            "message": message,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        event.update({k: v for k, v in extra.items() if v not in (None, "")})
        self.invocations.append(event)
        self.preflight()

    def _ingest_libtv_metadata_events(self, metadata_path: Path, cmd: list[str]) -> dict:
        if not metadata_path.exists():
            return {}
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._record_invocation(
                "libtv",
                cmd,
                "metadata_read_failed",
                f"无法读取 libtv metadata: {exc}",
                metadata_path=str(metadata_path),
            )
            return {}
        for event in metadata.get("events", []):
            if not isinstance(event, dict):
                continue
            status = event.get("status", "")
            if not status:
                continue
            extra = {
                key: value
                for key, value in event.items()
                if key not in {"status", "message", "time"}
            }
            extra["metadata_path"] = str(metadata_path.resolve())
            self._record_invocation("libtv", cmd, status, event.get("message", ""), **extra)
        return metadata

    def _path_within(self, path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except Exception:
            return False

    def _select_libtv_board_from_metadata(self, metadata: dict, libtv_out_dir: Path) -> Path | None:
        """Select only a valid board from the current libtv run directory."""
        selected = metadata.get("selected_board_path") if isinstance(metadata, dict) else ""
        if selected:
            selected_path = Path(selected)
            if selected_path.exists() and self._path_within(selected_path, libtv_out_dir):
                validation = validate_collection_board_shape(selected_path)
                if validation.get("ok"):
                    return selected_path

        images = metadata.get("images", []) if isinstance(metadata, dict) else []
        candidates: list[Path] = []
        for image in images:
            if not isinstance(image, dict) or not image.get("path"):
                continue
            path = Path(image["path"])
            if path.exists() and self._path_within(path, libtv_out_dir):
                candidates.append(path)

        if not candidates:
            candidates = (
                sorted(libtv_out_dir.glob("collection_board_*.png"))
                + sorted(libtv_out_dir.glob("collection_board_*.jpg"))
                + sorted(libtv_out_dir.glob("collection_board_*.jpeg"))
                + sorted(libtv_out_dir.glob("collection_board_*.webp"))
            )

        valid: list[tuple[int, Path]] = []
        for path in candidates:
            validation = validate_collection_board_shape(path)
            if validation.get("ok"):
                valid.append((int(validation.get("width", 0)) * int(validation.get("height", 0)), path))
        if not valid:
            return None
        return sorted(valid, key=lambda item: item[0], reverse=True)[0][1]

    # ------------------------------------------------------------------
    # Neo AI
    # ------------------------------------------------------------------
    def run_neo_ai(self, prompt_file: Path) -> Path:
        """调用 Neo AI 生成看板。

        Neo AI 脚本内部会轮询直到成功/失败，因此这里只需同步等待。
        返回看板图像的绝对路径。
        """
        neo_out_dir = self.out_dir / "neo_collection_board"
        neo_out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            str(self.neo_script),
            "--model", self.neo_model,
            "--size", self.neo_size,
            "--output-format", "png",
            "--output-dir", str(neo_out_dir),
            "--prompt-file", str(prompt_file),
            "--token", self.neo_token,
        ]

        print(f"[Neo AI] 启动看板生成: model={self.neo_model}, size={self.neo_size}")
        self._record_invocation("neo", cmd, "started")
        try:
            _run_subprocess(cmd, timeout=self.timeout)
        except Exception as exc:
            self._record_invocation("neo", cmd, "failed", str(exc))
            raise

        board_path = _latest_collection_board(neo_out_dir)
        validation = validate_collection_board_shape(board_path)
        if not validation.get("ok"):
            self._record_invocation("neo", cmd, "failed", validation.get("message", "invalid board"))
            raise RuntimeError(validation.get("message", "Neo AI 输出不是有效面料看板"))
        self._record_invocation("neo", cmd, "succeeded", str(board_path))
        print(f"[Neo AI] 看板已生成: {board_path}")
        return board_path.resolve()

    # ------------------------------------------------------------------
    # libtv
    # ------------------------------------------------------------------
    def run_libtv(self, description: str) -> Path:
        """调用 libtv-skill 的稳定入口生成看板，并只读取本次 run 目录产物。"""
        run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{int(time.time() * 1000) % 100000}"
        libtv_out_dir = self.out_dir / "libtv_collection_board" / run_id
        libtv_out_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = libtv_out_dir / "libtv_collection_prompt.txt"
        prompt_file.write_text(description, encoding="utf-8")

        cmd = [
            sys.executable,
            str(self.libtv_generate_script),
            "--prompt-file", str(prompt_file),
            "--output-dir", str(libtv_out_dir),
            "--output-format", "png",
            "--prefix", "collection_board",
            "--timeout", str(self.timeout),
            "--access-key", self.libtv_key,
        ]
        env = os.environ.copy()
        env["LIBTV_ACCESS_KEY"] = self.libtv_key
        metadata_path = libtv_out_dir / "metadata.json"

        print(f"[libtv] 启动 libtv-skill 看板生成: {self.libtv_generate_script}")
        self._record_invocation("libtv", cmd, "generate_board_started", "调用 libtv-skill generate_texture_collection_board.py", output_dir=str(libtv_out_dir))
        try:
            proc = _run_subprocess_capture(cmd, env=env, timeout=self.timeout + 180)
        except subprocess.TimeoutExpired as exc:
            metadata = self._ingest_libtv_metadata_events(metadata_path, cmd)
            self._record_invocation(
                "libtv",
                cmd,
                "generate_board_failed",
                "libtv-skill generate_texture_collection_board.py 超时",
                error_type="libtv_generate_timeout",
                metadata_path=str(metadata_path) if metadata else "",
                stdout=_truncate(exc.stdout or ""),
                stderr=_truncate(exc.stderr or ""),
            )
            raise RuntimeError("libtv_generate_timeout: libtv-skill 生成脚本超时")

        metadata = self._ingest_libtv_metadata_events(metadata_path, cmd)
        if proc.returncode != 0:
            self._record_invocation(
                "libtv",
                cmd,
                "generate_board_failed",
                _truncate(proc.stderr or proc.stdout),
                error_type="libtv_generate_failed",
                returncode=proc.returncode,
                metadata_path=str(metadata_path) if metadata else "",
                stdout=_truncate(proc.stdout),
                stderr=_truncate(proc.stderr),
            )
            raise RuntimeError(f"libtv_generate_failed: rc={proc.returncode}, stderr={_truncate(proc.stderr, 300)}")

        board_path = self._select_libtv_board_from_metadata(metadata, libtv_out_dir)
        if not board_path or not board_path.exists():
            self._record_invocation(
                "libtv",
                cmd,
                "generate_board_failed",
                f"本次 run 目录未找到合格 3x3 libtv 看板: {libtv_out_dir}",
                error_type="libtv_no_valid_3x3_board",
                metadata_path=str(metadata_path) if metadata else "",
                stdout=_truncate(proc.stdout),
                stderr=_truncate(proc.stderr),
            )
            raise RuntimeError(f"libtv_no_valid_3x3_board: 本次 run 目录未找到合格 3x3 libtv 看板: {libtv_out_dir}")

        validation = validate_collection_board_shape(board_path)
        if not validation.get("ok"):
            self._record_invocation(
                "libtv",
                cmd,
                "generate_board_failed",
                validation.get("message", "invalid board"),
                error_type="libtv_invalid_board_output",
                output_path=str(board_path.resolve()),
                metadata_path=str(metadata_path) if metadata else "",
            )
            raise RuntimeError(validation.get("message", "libtv 输出不是有效面料看板"))

        # metadata 中的 succeeded 已经被 ingest；这里补一条上游适配器成功事件。
        self._record_invocation(
            "libtv",
            cmd,
            "succeeded",
            str(board_path),
            output_path=str(board_path.resolve()),
            metadata_path=str(metadata_path.resolve()) if metadata_path.exists() else "",
            stdout=_truncate(proc.stdout),
        )
        print(f"[libtv] 看板已生成: {board_path}")

        # ---- 清理历史 run 目录：只保留本次成功的 run ----
        libtv_parent = libtv_out_dir.parent
        if libtv_parent.exists():
            for old_dir in libtv_parent.glob("run_*"):
                if old_dir.is_dir() and old_dir.name != run_id:
                    try:
                        import shutil
                        shutil.rmtree(old_dir)
                        print(f"[libtv] 清理历史 run 目录: {old_dir.name}")
                    except Exception as exc:
                        print(f"[libtv] 清理历史目录失败 {old_dir.name}: {exc}")

        return board_path.resolve()

    # ------------------------------------------------------------------
    # 主入口：并行生成 + 失败重试
    # ------------------------------------------------------------------
    def generate(self, dual_prompts_path: Path) -> list[dict]:
        """并行生成双源看板，返回成功结果列表。

        Returns:
            list[dict]: 每个元素为 {"source": "neo|libtv", "path": Path, "style": "style_a|style_b"}。
            两个源都会被调用并等待结果；至少一个源成功时返回。
            双源均失败且重试耗尽则抛出 DualBoardGenerationError。
        """
        dual_prompts_path = Path(dual_prompts_path)
        if not dual_prompts_path.exists():
            raise FileNotFoundError(f"dual_collection_prompts.json 不存在: {dual_prompts_path}")

        dual = json.loads(dual_prompts_path.read_text(encoding="utf-8"))
        style_a = dual.get("style_a", {})
        style_b = dual.get("style_b", {})

        # 准备 Neo AI 的 prompt 文件
        neo_prompt_text = style_a.get("prompt", "")
        neo_prompt_file = self.out_dir / "style_a_collection_prompt.txt"
        neo_prompt_file.write_text(neo_prompt_text, encoding="utf-8")

        libtv_description = style_b.get("description", "")
        if not neo_prompt_text.strip():
            raise DualBoardGenerationError("dual_collection_prompts.json 中 style_a.prompt 为空")
        if not libtv_description.strip():
            raise DualBoardGenerationError("dual_collection_prompts.json 中 style_b.description 为空")

        self.ensure_preflight_ok()

        for attempt in range(self.max_retries + 1):
            print(f"\n[双源生成] 第 {attempt + 1}/{self.max_retries + 1} 轮尝试...")
            results = []
            errors = {}

            with ThreadPoolExecutor(max_workers=2) as executor:
                future_to_source = {
                    executor.submit(self.run_neo_ai, neo_prompt_file): ("neo", "style_a"),
                    executor.submit(self.run_libtv, libtv_description): ("libtv", "style_b"),
                }

                for future in as_completed(future_to_source):
                    source, style = future_to_source[future]
                    try:
                        path = future.result()
                        results.append({"source": source, "path": path, "style": style})
                        print(f"[双源生成] ✅ {source} ({style}) 成功: {path}")
                    except Exception as exc:
                        errors[source] = str(exc)
                        print(f"[双源生成] ❌ {source} ({style}) 失败: {exc}")

            if results:
                if len(results) == 1:
                    failed_sources = sorted(set(["neo", "libtv"]) - {results[0]["source"]})
                    print(f"[双源生成] 本轮成功 1/2 个源，另一个源已失败/超时: {', '.join(failed_sources)}，继续下游流程")
                else:
                    print("[双源生成] 本轮双源均成功，继续下游流程")
                return sorted(results, key=lambda item: item["source"])

            if attempt < self.max_retries:
                print(f"[双源生成] 双源均失败，等待 5 秒后重新发起 AI 生成...")
                time.sleep(5)

        # 全部重试耗尽
        err_detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
        raise DualBoardGenerationError(
            f"双源看板生成全部失败（已重试 {self.max_retries} 次）。错误详情: {err_detail}；健康报告: {self.health_report_path}"
        )


# =============================================================================
# 命令行入口（用于独立测试）
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="双源并行看板生成器（Neo AI + libtv-skill）")
    parser.add_argument("--dual-prompts", default="", help="dual_collection_prompts.json 路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--token", default="", help="Neodomain 访问令牌。优先使用 NEODOMAIN_ACCESS_TOKEN 环境变量。")
    parser.add_argument("--libtv-key", default="", help="libtv Access Key。优先使用 LIBTV_ACCESS_KEY 环境变量。")
    parser.add_argument("--neo-model", default="gemini-3-pro-image-preview")
    parser.add_argument("--neo-size", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=300, help="单源最大等待时间（秒）")
    parser.add_argument("--preflight-only", action="store_true", help="只检查双源脚本路径/token，不调用远程生成。")
    args = parser.parse_args()

    neo_token = args.token or os.environ.get("NEODOMAIN_ACCESS_TOKEN", "")
    libtv_key = args.libtv_key or os.environ.get("LIBTV_ACCESS_KEY", "")

    generator = DualBoardGenerator(
        out_dir=Path(args.out),
        neo_token=neo_token,
        libtv_key=libtv_key,
        max_retries=args.max_retries,
        timeout=args.timeout,
        neo_model=args.neo_model,
        neo_size=args.neo_size,
    )

    try:
        if args.preflight_only:
            report = generator.preflight()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("overall_ok") else 1
        if not args.dual_prompts:
            print("错误: 生成看板必须提供 --dual-prompts；只做健康检查可使用 --preflight-only。", file=sys.stderr)
            return 1
        results = generator.generate(Path(args.dual_prompts))
        print("\n===== 生成结果 =====")
        for r in results:
            print(f"  来源: {r['source']}, 风格: {r['style']}, 路径: {r['path']}")
        return 0
    except DualBoardGenerationError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
