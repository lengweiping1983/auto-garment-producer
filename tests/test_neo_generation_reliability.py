import importlib.util
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("automation", ROOT / "scripts" / "端到端自动化.py")
automation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(automation)


class NeoGenerationReliabilityTest(unittest.TestCase):
    def _write_fake_neo_script(self, path: Path) -> None:
        path.write_text(
            textwrap.dedent(
                """
                import argparse
                import json
                import os
                import sys
                from pathlib import Path
                from PIL import Image

                parser = argparse.ArgumentParser()
                parser.add_argument("--prompt-file")
                parser.add_argument("--negative-prompt", default="")
                parser.add_argument("--model", default="")
                parser.add_argument("--size", default="")
                parser.add_argument("--output-format", default="png")
                parser.add_argument("--output-dir", required=True)
                parser.add_argument("--num-images", default="1")
                parser.add_argument("--reference-image", action="append", default=[])
                args = parser.parse_args()

                out = Path(args.output_dir)
                out.mkdir(parents=True, exist_ok=True)
                count_path = out / "attempt_count.txt"
                count = int(count_path.read_text() or "0") if count_path.exists() else 0
                count += 1
                count_path.write_text(str(count))
                fail_until = int(os.environ.get("FAKE_NEO_FAIL_UNTIL", "0"))
                if count <= fail_until:
                    print("temporary failure before submit")
                    sys.exit(7)

                task_code = f"FAKE_TASK_{count}"
                print(f"Task: {task_code}", flush=True)
                img_path = out / f"collection_board_1.{args.output_format}"
                Image.new("RGB", (8, 8), (count * 30 % 255, 20, 40)).save(img_path)
                prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip() if args.prompt_file else ""
                metadata = {
                    "task_code": task_code,
                    "model": args.model,
                    "size": args.size,
                    "prompt": prompt,
                    "reference_images": args.reference_image,
                    "images": [{"filename": img_path.name, "url": "https://example.invalid/fake.png"}],
                }
                (out / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                """
            ).strip(),
            encoding="utf-8",
        )

    def test_generation_retries_and_writes_success_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_script = tmp_path / "fake_neo.py"
            prompt = tmp_path / "prompt.txt"
            work_dir = tmp_path / "neo_textures" / "main"
            self._write_fake_neo_script(fake_script)
            prompt.write_text("main prompt", encoding="utf-8")
            cmd = [
                "python3",
                str(fake_script),
                "--model",
                "fake-model",
                "--size",
                "2K",
                "--output-format",
                "png",
                "--output-dir",
                str(work_dir),
                "--prompt-file",
                str(prompt),
                "--reference-image",
                "https://example.invalid/ref.png",
            ]

            generated = automation._run_neo_generation_with_status(
                "单纹理 main",
                "main",
                work_dir,
                cmd,
                env={**__import__("os").environ, "FAKE_NEO_FAIL_UNTIL": "2"},
                retries=2,
            )
            final_asset = tmp_path / "neo_textures" / "main.png"
            automation._copy_generated_image(generated, final_asset)
            automation._mark_generation_success(work_dir, "main", cmd, final_asset, generated)

            status = json.loads((work_dir / "request_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "success")
            self.assertEqual(status["attempt"], 3)
            self.assertEqual(status["task_code"], "FAKE_TASK_3")
            self.assertTrue(final_asset.exists())

    def test_reuse_rejects_stale_prompt_even_when_main_png_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "neo_textures" / "main"
            work_dir.mkdir(parents=True)
            final_asset = tmp_path / "neo_textures" / "main.png"
            Image.new("RGB", (4, 4), (1, 2, 3)).save(final_asset)
            old_prompt = tmp_path / "old.txt"
            new_prompt = tmp_path / "new.txt"
            old_prompt.write_text("old main prompt", encoding="utf-8")
            new_prompt.write_text("new main prompt", encoding="utf-8")
            (work_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "task_code": "OLD_TASK",
                        "model": "fake-model",
                        "size": "2K",
                        "prompt": "old main prompt",
                        "reference_images": ["https://example.invalid/ref.png"],
                    }
                ),
                encoding="utf-8",
            )
            cmd = [
                "python3",
                "fake_neo.py",
                "--model",
                "fake-model",
                "--size",
                "2K",
                "--output-format",
                "png",
                "--output-dir",
                str(work_dir),
                "--prompt-file",
                str(new_prompt),
                "--reference-image",
                "https://example.invalid/ref.png",
            ]

            self.assertFalse(automation._existing_generation_matches(work_dir, final_asset, "main", cmd))


if __name__ == "__main__":
    unittest.main()
