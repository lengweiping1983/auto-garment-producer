import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_pieces", ROOT / "scripts" / "渲染裁片.py")
render_pieces = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_pieces)


class FrontPairRenderingTest(unittest.TestCase):
    def test_front_pair_uses_sewn_canvas_not_paper_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            left_mask = Image.new("L", (30, 50), 0)
            ImageDraw.Draw(left_mask).rectangle((0, 5, 29, 44), fill=255)
            right_mask = Image.new("L", (30, 60), 0)
            ImageDraw.Draw(right_mask).rectangle((0, 15, 29, 54), fill=255)
            left_mask_path = tmp_path / "left_mask.png"
            right_mask_path = tmp_path / "right_mask.png"
            left_mask.save(left_mask_path)
            right_mask.save(right_mask_path)

            texture = Image.new("RGBA", (8, 8), (0, 0, 0, 255))
            px = texture.load()
            for x in range(texture.width):
                for y in range(texture.height):
                    px[x, y] = (x * 30, y * 20, 0, 255)
            texture_path = tmp_path / "texture.png"
            texture.save(texture_path)

            motif = Image.new("RGBA", (40, 24), (0, 0, 0, 0))
            motif_path = tmp_path / "theme_front_full.png"
            motif.save(motif_path)

            pieces_payload = {
                "pieces": [
                    {
                        "piece_id": "front_left",
                        "width": 30,
                        "height": 50,
                        "source_x": 0,
                        "source_y": 0,
                        "mask_path": str(left_mask_path),
                    },
                    {
                        "piece_id": "front_right",
                        "width": 30,
                        "height": 60,
                        "source_x": 120,
                        "source_y": 25,
                        "mask_path": str(right_mask_path),
                    },
                ]
            }
            texture_set = {
                "textures": [{"texture_id": "main", "path": str(texture_path), "approved": True}],
                "motifs": [{"motif_id": "theme_front_full", "path": str(motif_path), "approved": True}],
            }
            base = {
                "fill_type": "texture",
                "texture_id": "main",
                "scale": 1,
                "rotation": 0,
                "offset_x": 0,
                "offset_y": 0,
                "global_front_texture": True,
            }
            overlay = {
                "fill_type": "motif",
                "motif_id": "theme_front_full",
                "global_front_motif": True,
                "front_pair_scale_multiplier": 0.8,
            }
            fill_plan = {
                "pieces": [
                    {
                        "piece_id": "front_left",
                        "base": dict(base),
                        "overlay": dict(overlay, legacy_split_motif_id="theme_front_left"),
                        "front_pair_seam_locked": True,
                    },
                    {
                        "piece_id": "front_right",
                        "base": dict(base),
                        "overlay": dict(overlay, legacy_split_motif_id="theme_front_right"),
                        "front_pair_seam_locked": True,
                    },
                ]
            }

            rendered = render_pieces.render_all(
                pieces_payload,
                texture_set,
                fill_plan,
                tmp_path,
                tmp_path / "texture_set.json",
            )
            by_id = {item["piece_id"]: item for item in rendered}
            self.assertEqual({"front_left", "front_right"}, set(by_id))
            self.assertTrue((tmp_path / "front_pair_check.png").exists())

            left = Image.open(by_id["front_left"]["output_path"]).convert("RGBA")
            right = Image.open(by_id["front_right"]["output_path"]).convert("RGBA")
            global_y = 30
            # The seam spans are center-aligned: left local y is global_y - 10,
            # right local y is global_y. The paper source_x gap must not affect x phase.
            left_pixel = left.getpixel((29, global_y - 10))
            right_pixel = right.getpixel((0, global_y))
            self.assertEqual(left_pixel[:3], (5 * 30, 6 * 20, 0))
            self.assertEqual(right_pixel[:3], (6 * 30, 6 * 20, 0))

    def test_inverted_right_front_restores_seam_to_paper_right_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            left_mask = Image.new("L", (30, 50), 0)
            ImageDraw.Draw(left_mask).rectangle((0, 5, 29, 44), fill=255)
            right_mask = Image.new("L", (30, 50), 0)
            ImageDraw.Draw(right_mask).rectangle((0, 5, 29, 44), fill=255)
            left_mask_path = tmp_path / "left_mask.png"
            right_mask_path = tmp_path / "right_mask.png"
            left_mask.save(left_mask_path)
            right_mask.save(right_mask_path)

            texture = Image.new("RGBA", (8, 8), (0, 0, 0, 255))
            px = texture.load()
            for x in range(texture.width):
                for y in range(texture.height):
                    px[x, y] = (x * 30, y * 20, 0, 255)
            texture_path = tmp_path / "texture.png"
            texture.save(texture_path)

            pieces_payload = {
                "pieces": [
                    {
                        "piece_id": "front_left",
                        "width": 30,
                        "height": 50,
                        "source_x": 0,
                        "source_y": 0,
                        "mask_path": str(left_mask_path),
                        "pattern_orientation": 0,
                    },
                    {
                        "piece_id": "front_right",
                        "width": 30,
                        "height": 50,
                        "source_x": 100,
                        "source_y": 0,
                        "mask_path": str(right_mask_path),
                        "pattern_orientation": 180,
                    },
                ]
            }
            texture_set = {
                "textures": [{"texture_id": "main", "path": str(texture_path), "approved": True}],
                "motifs": [],
            }
            base = {
                "fill_type": "texture",
                "texture_id": "main",
                "scale": 1,
                "rotation": 0,
                "offset_x": 0,
                "offset_y": 0,
                "global_front_texture": True,
            }
            fill_plan = {
                "pieces": [
                    {
                        "piece_id": "front_left",
                        "base": dict(base),
                        "front_pair_seam_locked": True,
                    },
                    {
                        "piece_id": "front_right",
                        "base": dict(base),
                        "front_pair_seam_locked": True,
                    },
                ]
            }

            rendered = render_pieces.render_all(
                pieces_payload,
                texture_set,
                fill_plan,
                tmp_path,
                tmp_path / "texture_set.json",
            )
            by_id = {item["piece_id"]: item for item in rendered}
            right = Image.open(by_id["front_right"]["output_path"]).convert("RGBA")
            # The right panel is inverted on the paper. Its normalized left seam
            # must rotate back onto the paper-space right edge.
            paper_right_edge = right.getpixel((29, 24))
            paper_left_edge = right.getpixel((0, 24))
            self.assertEqual(paper_right_edge[:3], (6 * 30, 1 * 20, 0))
            self.assertEqual(paper_left_edge[:3], (3 * 30, 1 * 20, 0))
            self.assertTrue((tmp_path / "front_pair_check.png").exists())


if __name__ == "__main__":
    unittest.main()
