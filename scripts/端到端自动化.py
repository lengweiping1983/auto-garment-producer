#!/usr/bin/env python3
"""
端到端自动化：

1. 生成或接收 Neo AI 2×2 面料看板。
2. 裁剪为已批准的设计资产。
3. 构建面料组合.json。
4. 从纸样 mask 提取服装裁片。
5. 构建部位映射与艺术指导填充计划。
6. 渲染透明裁片 PNG、预览图、清单和成衣 QC。

Neo AI 负责创作 artwork。本脚本仅准备已批准资产并以确定性方式渲染到裁片中。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageEnhance


SKILL_DIR = Path(__file__).resolve().parents[1]
NEO_AI_SCRIPT = Path("/Users/lengweiping/.agents/skills/neo-ai/scripts/generate_texture_collection_board.py")


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_step(cmd: list[str], env: dict | None = None) -> None:
    print("运行:", " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)


def latest_collection_board(output_dir: Path) -> Path:
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


def generate_board(args: argparse.Namespace, out_dir: Path) -> Path:
    """调用 Neo AI 生成面料看板。"""
    board_dir = out_dir / "neo_collection_board"
    board_dir.mkdir(parents=True, exist_ok=True)
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
        str(board_dir),
    ]
    if args.prompt_file:
        cmd.extend(["--prompt-file", args.prompt_file])
    if args.negative_prompt_file:
        cmd.extend(["--negative-prompt-file", args.negative_prompt_file])
    if args.seed is not None:
        cmd.extend(["--seed", str(args.seed)])
    if args.num_images:
        cmd.extend(["--num-images", args.num_images])
    if args.token:
        cmd.extend(["--token", args.token])
    env = os.environ.copy()
    run_step(cmd, env=env)
    return latest_collection_board(board_dir)


def mirror_tile(image: Image.Image) -> Image.Image:
    """镜像修复：将纹理裁剪修复为无缝图块。"""
    src = image.convert("RGBA")
    out = Image.new("RGBA", (src.width * 2, src.height * 2), (0, 0, 0, 0))
    out.alpha_composite(src, (0, 0))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (src.width, 0))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, src.height))
    out.alpha_composite(src.transpose(Image.Transpose.FLIP_LEFT_RIGHT).transpose(Image.Transpose.FLIP_TOP_BOTTOM), (src.width, src.height))
    return out


def make_motif_transparent(panel: Image.Image, threshold: int = 238) -> Image.Image:
    """将看板右下角的 hero 区域处理为透明背景图案。"""
    img = panel.convert("RGBA")
    pixels = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = pixels[x, y]
            bright = r >= threshold and g >= threshold and b >= threshold
            low_chroma = max(r, g, b) - min(r, g, b) < 24
            if bright and low_chroma:
                pixels[x, y] = (r, g, b, 0)
            elif bright:
                pixels[x, y] = (r, g, b, max(0, min(a, 160)))
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        img = img.crop(bbox)
    return img


def quiet_solid_from_image(image: Image.Image, fallback: str = "#78965c") -> str:
    """从图像平均色提取一个安静的商业饰边纯色。"""
    sample = image.convert("RGB").resize((1, 1), Image.Resampling.LANCZOS)
    r, g, b = sample.getpixel((0, 0))
    # 将平均色向商业苔藓饰边色靠拢
    moss = ImageColor.getrgb(fallback)
    mixed = tuple(round(channel * 0.35 + moss_channel * 0.65) for channel, moss_channel in zip((r, g, b), moss))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def crop_collection_board(board_path: Path, out_dir: Path, inset: int, repair_tiles: bool) -> Path:
    """将 2×2 面料看板裁剪为四种资产，并生成面料组合.json。"""
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    board = Image.open(board_path).convert("RGBA")
    width, height = board.size
    mid_x, mid_y = width // 2, height // 2
    boxes = {
        "main": (inset, inset, mid_x - inset, mid_y - inset),
        "secondary": (mid_x + inset, inset, width - inset, mid_y - inset),
        "accent": (inset, mid_y + inset, mid_x - inset, height - inset),
        "hero_motif": (mid_x + inset, mid_y + inset, width - inset, height - inset),
    }
    paths = {}
    for asset_id, box in boxes.items():
        crop = board.crop(box)
        if asset_id == "hero_motif":
            crop = make_motif_transparent(crop)
            path = assets_dir / f"{asset_id}.png"
            crop.save(path)
        else:
            if repair_tiles:
                crop = mirror_tile(crop)
            path = assets_dir / f"{asset_id}.png"
            crop.convert("RGB").save(path)
        paths[asset_id] = path

    solid = quiet_solid_from_image(Image.open(paths["secondary"]))
    texture_set = {
        "texture_set_id": f"{out_dir.name}_neo_collection_texture_set",
        "locked": False,
        "source_collection_board": str(board_path.resolve()),
        "textures": [
            {
                "texture_id": "main",
                "path": str(paths["main"].resolve()),
                "role": "main",
                "approved": True,
                "prompt": "从 Neo AI 2×2 面料看板裁剪：主底纹",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "texture_id": "secondary",
                "path": str(paths["secondary"].resolve()),
                "role": "secondary",
                "approved": True,
                "prompt": "从 Neo AI 2×2 面料看板裁剪：辅纹理",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
            {
                "texture_id": "accent",
                "path": str(paths["accent"].resolve()),
                "role": "accent",
                "approved": True,
                "prompt": "从 Neo AI 2×2 面料看板裁剪：点缀纹理",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            },
        ],
        "motifs": [
            {
                "motif_id": "hero_motif",
                "path": str(paths["hero_motif"].resolve()),
                "role": "hero",
                "approved": True,
                "prompt": "从 Neo AI 2×2 面料看板裁剪：卖点定位图案",
                "model": "neo-ai",
                "seed": "",
                "qc": {"approved": True},
            }
        ],
        "solids": [
            {"solid_id": "quiet_moss", "color": solid, "approved": True},
            {"solid_id": "warm_ivory", "color": "#f3f1df", "approved": True},
        ],
    }
    return write_json(out_dir / "texture_set.json", texture_set)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Neo AI 面料看板并自动渲染服装裁片。")
    parser.add_argument("--pattern", required=True, help="透明纸样 mask PNG/WebP")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--collection-board", default="", help="已有的 Neo AI 2×2 面料看板。若省略，则调用 Neo AI 生成。")
    parser.add_argument("--prompt-file", default="", help="Neo AI 看板生成的提示词文件")
    parser.add_argument("--negative-prompt-file", default="", help="Neo AI 看板生成的反向提示词文件")
    parser.add_argument("--token", default="", help="Neodomain 访问令牌。优先使用 NEODOMAIN_ACCESS_TOKEN 环境变量。")
    parser.add_argument("--neo-model", default="gemini-3-pro-image-preview")
    parser.add_argument("--neo-size", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument("--num-images", default="1", choices=["1", "4"])
    parser.add_argument("--seed", type=int)
    parser.add_argument("--crop-inset", type=int, default=24, help="从每个象限裁剪的像素数，用于去除网格间隙。")
    parser.add_argument("--no-tile-repair", action="store_true", help="不将纹理裁剪镜像修复为无缝图块。")
    parser.add_argument("--brief", default="", help="可选的商业设计简报 JSON 路径")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    board_path = Path(args.collection_board).resolve() if args.collection_board else generate_board(args, out_dir).resolve()
    if not board_path.exists():
        raise RuntimeError(f"面料看板未找到: {board_path}")
    print(f"使用面料看板: {board_path}")

    texture_set_path = crop_collection_board(board_path, out_dir, args.crop_inset, not args.no_tile_repair)

    pieces_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "提取裁片.py"),
        "--pattern",
        args.pattern,
        "--out",
        str(out_dir),
    ]
    run_step(pieces_cmd)

    pieces_path = out_dir / "pieces.json"
    garment_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "部位映射.py"),
        "--pieces",
        str(pieces_path),
        "--out",
        str(out_dir),
    ]
    run_step(garment_cmd)

    qc_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "质检纹理.py"),
        "--texture-set",
        str(texture_set_path),
        "--out",
        str(out_dir / "texture_qc_report.json"),
    ]
    run_step(qc_cmd)

    plan_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "创建填充计划.py"),
        "--pieces",
        str(pieces_path),
        "--texture-set",
        str(texture_set_path),
        "--garment-map",
        str(out_dir / "garment_map.json"),
        "--out",
        str(out_dir),
    ]
    if args.brief:
        plan_cmd.extend(["--brief", args.brief])
    run_step(plan_cmd)

    rendered_dir = out_dir / "rendered"
    render_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "渲染裁片.py"),
        "--pieces",
        str(pieces_path),
        "--texture-set",
        str(texture_set_path),
        "--fill-plan",
        str(out_dir / "piece_fill_plan.json"),
        "--out",
        str(rendered_dir),
    ]
    run_step(render_cmd)

    fashion_cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "时尚质检.py"),
        "--pieces",
        str(pieces_path),
        "--texture-set",
        str(texture_set_path),
        "--fill-plan",
        str(out_dir / "piece_fill_plan.json"),
        "--rendered",
        str(rendered_dir),
        "--out",
        str(out_dir / "fashion_qc_report.json"),
    ]
    run_step(fashion_cmd)

    summary = {
        "面料看板": str(board_path),
        "面料组合": str(texture_set_path.resolve()),
        "裁片清单": str(pieces_path.resolve()),
        "部位映射": str((out_dir / "garment_map.json").resolve()),
        "裁片填充计划": str((out_dir / "piece_fill_plan.json").resolve()),
        "渲染目录": str(rendered_dir.resolve()),
        "预览图": str((rendered_dir / "preview.png").resolve()),
        "白底预览图": str((rendered_dir / "preview_white.jpg").resolve()),
        "成品质检报告": str((out_dir / "fashion_qc_report.json").resolve()),
    }
    write_json(out_dir / "automation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
