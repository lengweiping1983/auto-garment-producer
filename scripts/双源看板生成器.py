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
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 从当前文件位置推导 skills 根目录（当前文件在 auto-garment-producer/scripts/ 下，上两级为 skills 根）
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
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
        _SKILLS_ROOT / "neo-ai" / "scripts" / "generate_texture_collection_board.py",
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
        _SKILLS_ROOT / "libtv-skill" / "scripts",
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
        if item in {"--token", "--libtv-key"}:
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
    ok = 0.75 <= ratio <= 1.34 and min(width, height) >= 64
    message = "ok" if ok else (
        f"看板尺寸不符合正方形/近正方形纹理看板要求: {width}x{height}。"
        "禁止把正面成衣效果图、模特上身图或产品照作为面料看板输入。"
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
        self.health_report_path = self.out_dir / "dual_source_health_report.json"
        self.invocations: list[dict] = []

    def preflight(self) -> dict:
        """检查 Neo AI 与 libtv-skill 是否可调用，并写出健康报告。"""
        libtv_required = ["create_session.py", "query_session.py", "download_results.py"]
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

    def _record_invocation(self, source: str, cmd: list[str], status: str, message: str = "") -> None:
        self.invocations.append({
            "source": source,
            "cmd": _redact_cmd(cmd),
            "status": status,
            "message": message,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        self.preflight()

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
        """调用 libtv 生成看板。

        流程：
        1. create_session.py "description" → 获取 session_id
        2. query_session.py session_id 轮询 → 检测 assistant 消息中的图片 URL
        3. download_results.py session_id --output-dir → 下载图片
        4. 取第一张图片重命名为 collection_board_B.png

        返回看板图像的绝对路径。
        """
        libtv_out_dir = self.out_dir / "libtv_collection_board"
        libtv_out_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["LIBTV_ACCESS_KEY"] = self.libtv_key

        # ---- 1. 创建会话 ----
        create_cmd = [
            sys.executable,
            str(self.libtv_script_dir / "create_session.py"),
            description,
        ]
        print(f"[libtv] 创建会话并发送生成请求...")
        self._record_invocation("libtv", create_cmd, "started")
        try:
            stdout = _run_subprocess(create_cmd, env=env, timeout=60)
        except Exception as exc:
            self._record_invocation("libtv", create_cmd, "failed", str(exc))
            raise

        try:
            session_data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"[libtv] 无法解析 create_session 输出: {exc}\nstdout: {stdout[:300]}")

        session_id = session_data.get("sessionId", "")
        project_uuid = session_data.get("projectUuid", "")
        if not session_id:
            raise RuntimeError("[libtv] create_session 未返回 sessionId")
        print(f"[libtv] 会话已创建: session_id={session_id}, project_uuid={project_uuid}")

        # ---- 2. 轮询等待结果 ----
        image_urls: list[str] = []
        poll_interval = 8  # 秒
        max_poll_time = self.timeout
        poll_start = time.time()
        query_fail_count = 0
        max_query_fails = 3

        url_pattern = re.compile(r'https?://[^\s"\'<>]+\.(?:png|jpg|jpeg|webp)')

        while time.time() - poll_start < max_poll_time:
            time.sleep(poll_interval)

            query_cmd = [
                sys.executable,
                str(self.libtv_script_dir / "query_session.py"),
                session_id,
                "--after-seq", "0",
            ]
            if project_uuid:
                query_cmd.extend(["--project-id", project_uuid])

            try:
                stdout = _run_subprocess(query_cmd, env=env, timeout=30)
                query_fail_count = 0  # 重置失败计数
            except Exception as exc:
                query_fail_count += 1
                print(f"[libtv] 查询失败 ({query_fail_count}/{max_query_fails}): {exc}")
                if query_fail_count >= max_query_fails:
                    raise RuntimeError(f"[libtv] 连续 {max_query_fails} 次查询失败，放弃轮询")
                continue

            try:
                query_data = json.loads(stdout)
            except json.JSONDecodeError:
                continue

            messages = query_data.get("messages", [])
            for msg in messages:
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        found = url_pattern.findall(content)
                        if found:
                            image_urls.extend(found)

            # 同时检查 tool 消息中的 task_result
            for msg in messages:
                if msg.get("role") == "tool":
                    try:
                        data = json.loads(msg.get("content", "{}"))
                        task_result = data.get("task_result", {})
                        for img in task_result.get("images", []):
                            preview = img.get("previewPath", "")
                            if preview:
                                image_urls.append(preview)
                        for vid in task_result.get("videos", []):
                            preview = vid.get("previewPath", vid.get("url", ""))
                            if preview:
                                image_urls.append(preview)
                    except (json.JSONDecodeError, AttributeError):
                        pass

            # 去重
            seen = set()
            unique_urls = []
            for u in image_urls:
                if u not in seen:
                    seen.add(u)
                    unique_urls.append(u)
            image_urls = unique_urls

            if image_urls:
                print(f"[libtv] 检测到 {len(image_urls)} 个结果 URL")
                break
        else:
            raise RuntimeError(f"[libtv] 轮询超时（{self.timeout}s），未检测到生成结果")

        if not image_urls:
            raise RuntimeError("[libtv] 轮询结束但未找到任何图片 URL")

        # ---- 3. 下载结果 ----
        print(f"[libtv] 开始下载结果...")
        download_cmd = [
            sys.executable,
            str(self.libtv_script_dir / "download_results.py"),
            session_id,
            "--output-dir", str(libtv_out_dir),
            "--prefix", "collection_board",
        ]
        try:
            _run_subprocess(download_cmd, env=env, timeout=120)
        except Exception as exc:
            self._record_invocation("libtv", download_cmd, "failed", str(exc))
            raise

        # ---- 4. 定位下载的图片 ----
        downloaded = sorted(libtv_out_dir.glob("collection_board_*.png")) + \
                     sorted(libtv_out_dir.glob("collection_board_*.jpg")) + \
                     sorted(libtv_out_dir.glob("collection_board_*.jpeg")) + \
                     sorted(libtv_out_dir.glob("collection_board_*.webp"))

        if not downloaded:
            raise RuntimeError(f"[libtv] 下载完成但未在 {libtv_out_dir} 找到图片文件")

        board_path = downloaded[0]
        validation = validate_collection_board_shape(board_path)
        if not validation.get("ok"):
            self._record_invocation("libtv", download_cmd, "failed", validation.get("message", "invalid board"))
            raise RuntimeError(validation.get("message", "libtv 输出不是有效面料看板"))
        self._record_invocation("libtv", download_cmd, "succeeded", str(board_path))
        print(f"[libtv] 看板已下载: {board_path}")
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
