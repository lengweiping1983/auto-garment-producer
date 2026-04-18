#!/usr/bin/env python3
"""
双源看板生成器：并行调用 Neo AI 和 libtv-skills 生成面料看板。

职责：
- 读取 dual_collection_prompts.json，获取两套不同但风格一致的提示词
- 使用 ThreadPoolExecutor 并行提交 Neo AI 和 libtv 生成任务
- 任一源成功即返回；双源均失败则自动重试
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

# Neo AI 脚本路径
NEO_AI_SCRIPT = Path("/Users/lengweiping/.agents/skills/neo-ai/scripts/generate_texture_collection_board.py")
# libtv 脚本目录
LIBTV_SCRIPT_DIR = Path("/Users/lengweiping/.agents/skills/libtv-skills/skills/libtv-skill/scripts")


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
        self.neo_token = neo_token
        self.libtv_key = libtv_key
        self.max_retries = max_retries
        self.timeout = timeout
        self.neo_model = neo_model
        self.neo_size = neo_size

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
            str(NEO_AI_SCRIPT),
            "--model", self.neo_model,
            "--size", self.neo_size,
            "--output-format", "png",
            "--output-dir", str(neo_out_dir),
            "--prompt-file", str(prompt_file),
            "--token", self.neo_token,
        ]

        print(f"[Neo AI] 启动看板生成: model={self.neo_model}, size={self.neo_size}")
        _run_subprocess(cmd, timeout=self.timeout)

        board_path = _latest_collection_board(neo_out_dir)
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
            str(LIBTV_SCRIPT_DIR / "create_session.py"),
            description,
        ]
        print(f"[libtv] 创建会话并发送生成请求...")
        stdout = _run_subprocess(create_cmd, env=env, timeout=60)

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
                str(LIBTV_SCRIPT_DIR / "query_session.py"),
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
            str(LIBTV_SCRIPT_DIR / "download_results.py"),
            session_id,
            "--output-dir", str(libtv_out_dir),
            "--prefix", "collection_board",
        ]
        _run_subprocess(download_cmd, env=env, timeout=120)

        # ---- 4. 定位下载的图片 ----
        downloaded = sorted(libtv_out_dir.glob("collection_board_*.png")) + \
                     sorted(libtv_out_dir.glob("collection_board_*.jpg")) + \
                     sorted(libtv_out_dir.glob("collection_board_*.jpeg")) + \
                     sorted(libtv_out_dir.glob("collection_board_*.webp"))

        if not downloaded:
            raise RuntimeError(f"[libtv] 下载完成但未在 {libtv_out_dir} 找到图片文件")

        board_path = downloaded[0]
        print(f"[libtv] 看板已下载: {board_path}")
        return board_path.resolve()

    # ------------------------------------------------------------------
    # 主入口：并行生成 + 失败重试
    # ------------------------------------------------------------------
    def generate(self, dual_prompts_path: Path) -> list[dict]:
        """并行生成双源看板，返回成功结果列表。

        Returns:
            list[dict]: 每个元素为 {"source": "neo|libtv", "path": Path, "style": "style_a|style_b"}
            只要任一源成功即返回（可能只有 1 个元素）。
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

        for attempt in range(self.max_retries + 1):
            print(f"\n[双源生成] 第 {attempt + 1}/{self.max_retries + 1} 轮尝试...")
            results = []
            errors = {}

            with ThreadPoolExecutor(max_workers=2) as executor:
                future_to_source = {
                    executor.submit(self.run_neo_ai, neo_prompt_file): ("neo", "style_a"),
                    executor.submit(self.run_libtv, libtv_description): ("libtv", "style_b"),
                }

                for future in as_completed(future_to_source, timeout=self.timeout + 30):
                    source, style = future_to_source[future]
                    try:
                        path = future.result()
                        results.append({"source": source, "path": path, "style": style})
                        print(f"[双源生成] ✅ {source} ({style}) 成功: {path}")
                    except Exception as exc:
                        errors[source] = str(exc)
                        print(f"[双源生成] ❌ {source} ({style}) 失败: {exc}")

            if results:
                print(f"[双源生成] 本轮成功 {len(results)}/{2} 个源，继续下游流程")
                return results

            if attempt < self.max_retries:
                print(f"[双源生成] 全部失败，等待 5 秒后重试...")
                time.sleep(5)

        # 全部重试耗尽
        err_detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
        raise DualBoardGenerationError(
            f"双源看板生成全部失败（已重试 {self.max_retries} 次）。错误详情: {err_detail}"
        )


# =============================================================================
# 命令行入口（用于独立测试）
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="双源并行看板生成器（Neo AI + libtv）")
    parser.add_argument("--dual-prompts", required=True, help="dual_collection_prompts.json 路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--token", default="", help="Neodomain 访问令牌。优先使用 NEODOMAIN_ACCESS_TOKEN 环境变量。")
    parser.add_argument("--libtv-key", default="", help="libtv Access Key。优先使用 LIBTV_ACCESS_KEY 环境变量。")
    parser.add_argument("--neo-model", default="gemini-3-pro-image-preview")
    parser.add_argument("--neo-size", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=300, help="单源最大等待时间（秒）")
    args = parser.parse_args()

    neo_token = args.token or os.environ.get("NEODOMAIN_ACCESS_TOKEN", "")
    libtv_key = args.libtv_key or os.environ.get("LIBTV_ACCESS_KEY", "")

    if not neo_token:
        print("错误: 必须提供 --token 或设置 NEODOMAIN_ACCESS_TOKEN 环境变量", file=sys.stderr)
        return 1
    if not libtv_key:
        print("错误: 必须提供 --libtv-key 或设置 LIBTV_ACCESS_KEY 环境变量", file=sys.stderr)
        return 1

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
