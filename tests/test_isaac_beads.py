from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

import isaac_beads as beads


class PaletteTests(unittest.TestCase):
    def test_mard_palette_has_221_colours(self) -> None:
        self.assertEqual(221, len(beads.PALETTE))
        self.assertEqual(221, len(set(beads.PALETTE)))

    def test_h1_is_reserved_for_playbeans_transparency(self) -> None:
        self.assertNotIn("H1", beads.MATCHABLE_LAB)
        self.assertEqual("H2", beads.nearest_palette_key((255, 255, 255)))
        self.assertEqual("H7", beads.nearest_palette_key((0, 0, 0)))


class ProcessingTests(unittest.TestCase):
    def test_prepare_rgba_trims_transparent_padding(self) -> None:
        source = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        source.putpixel((5, 5), (255, 0, 0, 255))
        source.putpixel((6, 6), (0, 0, 255, 255))

        result = beads.prepare_rgba(
            source,
            size=11,
            resample="nearest",
            margin=1,
            alpha_threshold=128,
        )

        self.assertEqual((11, 11), result.size)
        self.assertGreater(result.getchannel("A").getbbox()[2], 1)

    def test_quantization_preserves_transparent_cells(self) -> None:
        image = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
        image.putpixel((0, 0), (0, 0, 0, 255))
        image.putpixel((1, 0), (255, 255, 255, 255))
        grid, counts = beads.quantize_image(image, alpha_threshold=128)

        self.assertEqual("H7", grid[(0, 0)])
        self.assertEqual("H2", grid[(1, 0)])
        self.assertEqual(2, sum(counts.values()))
        self.assertEqual(2, len(counts))

    def test_one_annotated_png_is_written_per_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            category = beads.CATEGORIES["characters"]
            original = output / "originals" / category.key / "sample.png"
            original.parent.mkdir(parents=True)
            source = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            for y in range(4, 12):
                for x in range(4, 12):
                    source.putpixel((x, y), (30, 30, 30, 255))
            source.save(original)

            rows = beads.process_category(
                category,
                [
                    {
                        "category": category.key,
                        "category_label": category.label,
                        "source_page": "test",
                        "source_page_url": "",
                        "source_image": "sample.png",
                        "source_url": "",
                        "title": "Sample",
                        "fallback_title": "Sample",
                        "slug": "sample",
                    }
                ],
                output=output,
                size=11,
                resample="nearest",
                margin=1,
                alpha_threshold=128,
                write_beans_files=False,
            )

            self.assertEqual(1, len(rows))
            self.assertEqual("ok", rows[0]["status"])
            png = output / str(rows[0]["png_file"])
            self.assertTrue(png.is_file())
            with Image.open(png) as chart:
                self.assertGreater(chart.width, 500)
                self.assertGreater(chart.height, 500)


class ExportTests(unittest.TestCase):
    def test_optional_playbeans_round_trip(self) -> None:
        payload = {
            "beans": {"H7": ["0,0"]},
            "GRID_WIDTH": 1,
            "GRID_HEIGHT": 1,
            "colorSwatchKey": "mard",
            "beanType": "bean",
        }
        encoded = beads.encrypt_playbeans_json(payload)
        self.assertEqual(payload, beads.decrypt_playbeans_text(encoded))


if __name__ == "__main__":
    unittest.main()
