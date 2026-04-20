import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("theme_front_splitter", ROOT / "scripts" / "theme_front_splitter.py")
splitter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(splitter)


def test_fake_checker_background_removal_preserves_internal_white_details():
    img = Image.new("RGB", (96, 96), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for y in range(0, 96, 8):
        for x in range(0, 96, 8):
            fill = (210, 210, 210) if (x // 8 + y // 8) % 2 else (245, 245, 245)
            draw.rectangle((x, y, x + 7, y + 7), fill=fill)

    draw.ellipse((20, 16, 76, 80), fill=(235, 160, 92), outline=(40, 20, 10), width=3)
    draw.rectangle((36, 48, 60, 56), fill=(246, 246, 242), outline=(90, 20, 20), width=2)

    out = splitter._remove_false_transparency_background(img)
    alpha = out.getchannel("A")

    assert alpha.getpixel((0, 0)) < 16
    assert alpha.getpixel((48, 52)) == 255
