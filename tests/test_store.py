import concurrent.futures
import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from store import Catalog, CatalogError


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog = Catalog(self.root)
        self.upload = self.root / "files" / "images"
        self.upload.mkdir(parents=True)
        self.make_image("one.png", "red")
        self.make_image("two.png", "blue")

    def tearDown(self):
        self.tmp.cleanup()

    def make_image(self, name, color):
        Image.new("RGB", (20, 20), color).save(self.upload / name)

    def sync(self, *names):
        self.catalog.sync([f"files/images/{name}" for name in names])

    def test_cross_group_and_concurrent_draws_are_one_transaction(self):
        self.sync("one.png", "two.png")
        # Group is deliberately not part of the identity.
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.catalog.draw("aiocqhttp", "123", "2026-10-28"), range(24)))
        self.assertEqual(len({item[0] for item in results}), 1)
        self.assertEqual(sum(item[1] for item in results), 1)
        self.assertEqual(sum(item[2] for item in results), 1)
        self.assertEqual(sum(row[2] for row in self.catalog.entries("aiocqhttp", "123")), 1)
        self.assertEqual(sum(row[2] for row in self.catalog.entries("aiocqhttp", "456")), 0)

    def test_removed_or_overwritten_upload_keeps_history(self):
        self.sync("one.png")
        asset, _, _ = self.catalog.draw("aiocqhttp", "123", "2026-10-28")
        saved = self.catalog.image_path(asset).read_bytes()
        self.make_image("one.png", "green")
        self.sync("one.png")
        self.assertEqual(self.catalog.draw("aiocqhttp", "123", "2026-10-28")[0], asset)
        self.assertEqual(self.catalog.image_path(asset).read_bytes(), saved)
        self.assertEqual(len(self.catalog.entries("aiocqhttp", "123")), 2)
        self.assertEqual(len(self.catalog.entries("aiocqhttp", "456")), 1)
        self.sync()
        self.assertEqual(self.catalog.draw("aiocqhttp", "123", "2026-10-28")[0], asset)
        self.assertIsNone(self.catalog.draw("aiocqhttp", "123", "2026-10-29"))
        self.assertEqual(len(self.catalog.entries("aiocqhttp", "123")), 1)
        self.assertEqual(len(self.catalog.entries("aiocqhttp", "456")), 0)

    def test_existing_draw_survives_missing_and_corrupt_configured_file(self):
        self.sync("one.png")
        asset, _, _ = self.catalog.draw("aiocqhttp", "123", "2026-10-28")
        (self.upload / "one.png").unlink()
        self.assertEqual(self.catalog.resolve_daily(["files/images/one.png"], "aiocqhttp", "123", "2026-10-28")[0], asset)
        (self.upload / "one.png").write_text("invalid")
        self.assertEqual(self.catalog.resolve_daily(["files/images/one.png"], "aiocqhttp", "123", "2026-10-28")[0], asset)
        with self.assertRaises(CatalogError):
            self.catalog.resolve_daily(["files/images/one.png"], "aiocqhttp", "123", "2026-10-29")

    def test_duplicate_images_deduplicated_and_reupload_restores(self):
        self.sync("one.png", "one.png")
        first = self.catalog.draw("aiocqhttp", "123", "2026-10-28")[0]
        self.sync()
        self.sync("one.png")
        self.assertEqual(self.catalog.draw("aiocqhttp", "123", "2026-10-29")[0], first)
        self.assertEqual(len(self.catalog.entries("aiocqhttp", "123")), 1)

    def test_failed_sync_preserves_active_pool(self):
        self.sync("one.png")
        with self.assertRaises(CatalogError):
            self.sync("missing.png")
        self.assertIsNotNone(self.catalog.draw("aiocqhttp", "123", "2026-10-28"))
        with self.assertRaises(CatalogError):
            self.catalog.sync(["../../outside.png"])

    def test_animated_gif_is_kept_exactly_after_removal(self):
        path = self.upload / "animated.gif"
        red = Image.new("RGB", (20, 20), "red")
        blue = Image.new("RGB", (20, 20), "blue")
        red.save(path, save_all=True, append_images=[blue], duration=[100, 250], loop=0)
        original = path.read_bytes()
        self.sync("animated.gif")
        asset, _, _ = self.catalog.draw("aiocqhttp", "123", "2026-10-28")
        snapshot = self.catalog.image_path(asset)
        self.assertEqual(snapshot.suffix, ".gif")
        self.assertEqual(snapshot.read_bytes(), original)
        path.unlink()
        self.sync()
        self.assertEqual(self.catalog.draw("aiocqhttp", "123", "2026-10-28")[0], asset)
        with Image.open(self.catalog.image_path(asset)) as image:
            self.assertEqual(image.n_frames, 2)
            self.assertEqual(image.info["duration"], 100)
            image.seek(1)
            self.assertEqual(image.info["duration"], 250)
        self.assertTrue(self.catalog.entries("aiocqhttp", "123")[0][2])

    def test_existing_png_snapshot_is_compatible_and_can_restore_gif(self):
        path = self.upload / "legacy.gif"
        red = Image.new("RGB", (20, 20), "red")
        red.save(path, save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")], duration=100, loop=0)
        asset = hashlib.sha256(path.read_bytes()).hexdigest()
        # Reproduce a previously imported GIF that only had a PNG snapshot.
        red.save(self.catalog.images / f"{asset}.png")
        with self.catalog._connect() as db:
            db.execute("INSERT INTO assets VALUES (?, 1, 1)", (asset,))
            db.execute("INSERT INTO draws VALUES (?, ?, ?, ?)", ("aiocqhttp", "123", "2026-10-28", asset))
            db.execute("INSERT INTO collection VALUES (?, ?, ?)", ("aiocqhttp", "123", asset))
        self.assertEqual(self.catalog.image_path(asset).suffix, ".png")
        self.sync("legacy.gif")
        self.assertEqual(self.catalog.image_path(asset).read_bytes(), path.read_bytes())
        self.assertEqual(self.catalog.draw("aiocqhttp", "123", "2026-10-28")[0], asset)
        self.assertEqual(len(self.catalog.entries("aiocqhttp", "123")), 1)


if __name__ == "__main__":
    unittest.main()
