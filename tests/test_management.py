"""Exercise the dashboard handlers without a running AstrBot server."""

import asyncio
import base64
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from test_handlers import Context, Star, plugin
from astrbot_plugin_daily_naiwa import management


class Request:
    def __init__(self, query=None, payload=None):
        self.query = query or {}
        self.payload = payload

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


class ManagementTest(unittest.TestCase):
    def test_listing_edit_preview_and_invalid_ids(self):
        with tempfile.TemporaryDirectory() as root:
            plugin.get_astrbot_data_path = lambda: root
            image_dir = Path(root) / "plugin_data" / Star.name / "files"
            image_dir.mkdir(parents=True)
            Image.new("RGB", (16, 12), "green").save(image_dir / "frog.png")
            instance = plugin.DailyNaiwa(Context(), {"images": ["files/frog.png"]})
            view = instance.asset_management

            async def exercise():
                result = await view.list_assets()
                self.assertEqual(len(result["assets"]), 1)
                asset_id = result["assets"][0]["id"]
                management.request = Request(payload={"id": asset_id, "name": " 青蛙 ", "description": " 你好 "})
                saved = await view.save()
                self.assertEqual(saved, {"saved": True, "name": "青蛙", "description": "你好"})
                self.assertEqual(instance.catalog.get_metadata(asset_id), ("青蛙", "你好"))
                management.request = Request(query={"id": asset_id})
                encoded = (await view.preview())["image"]
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as preview:
                    self.assertEqual(preview.format, "JPEG")
                management.request = Request(query={"id": "../catalog.sqlite3"})
                self.assertEqual((await view.preview())[1], 400)
                management.request = Request(query={"id": "0" * 64})
                self.assertEqual((await view.preview())[1], 404)
                management.request = Request(payload={"id": "../catalog.sqlite3", "name": "bad", "description": ""})
                self.assertEqual((await view.save())[1], 400)
                management.request = Request(payload={"id": "0" * 64, "name": "bad", "description": ""})
                self.assertEqual((await view.save())[1], 400)
                management.request = Request(payload={"id": asset_id, "name": "x" * 101, "description": ""})
                self.assertEqual((await view.save())[1], 400)

            original_request = management.request
            try:
                asyncio.run(exercise())
                reloaded = plugin.DailyNaiwa(Context(), {"images": ["files/frog.png"]})
                self.assertEqual(reloaded.catalog.list_assets()[0]["name"], "青蛙")
            finally:
                management.request = original_request


if __name__ == "__main__":
    unittest.main()
