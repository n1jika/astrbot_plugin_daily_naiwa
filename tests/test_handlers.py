import asyncio
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# Provide only AstrBot's interface; this checks handlers but is not an E2E test.
for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star", "astrbot.api.message_components", "astrbot.core", "astrbot.core.utils", "astrbot.core.utils.astrbot_path"):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module

components = sys.modules["astrbot.api.message_components"]
class At:
    def __init__(self, qq):
        self.qq = qq
class Plain:
    def __init__(self, text):
        self.text = text
class ImageComponent:
    @classmethod
    def fromFileSystem(cls, path):
        instance = cls()
        instance.path = path
        return instance
components.At, components.Plain, components.Image = At, Plain, ImageComponent

api = sys.modules["astrbot.api"]
api.AstrBotConfig = dict
api.logger = logging.getLogger("test")
event_api = sys.modules["astrbot.api.event"]
event_api.AstrMessageEvent = object
class Filters:
    class EventMessageType:
        GROUP_MESSAGE = 1
    @staticmethod
    def command(*args):
        return lambda fn: fn
    @staticmethod
    def event_message_type(*args):
        return lambda fn: fn
event_api.filter = Filters
star = sys.modules["astrbot.api.star"]
star.Context = object
class Star:
    name = "astrbot_plugin_daily_naiwa"
    def __init__(self, context):
        self.context = context
star.Star = Star
path_api = sys.modules["astrbot.core.utils.astrbot_path"]
path_api.get_astrbot_data_path = lambda: "/tmp/placeholder"
plugin = importlib.import_module("astrbot_plugin_daily_naiwa.main")

class Event:
    def __init__(self, group):
        self.group = group
    def get_platform_name(self):
        return "aiocqhttp"
    def get_sender_id(self):
        return "same-user"
    def plain_result(self, text):
        return ("text", text)
    def chain_result(self, chain):
        return ("chain", chain)

async def collect(generator):
    return [item async for item in generator]

class HandlerTest(unittest.TestCase):
    def test_gif_draw_and_static_gallery_first_frame(self):
        with tempfile.TemporaryDirectory() as root:
            plugin.get_astrbot_data_path = lambda: root
            directory = Path(root) / "plugin_data" / Star.name / "files" / "images"
            directory.mkdir(parents=True)
            image_path = directory / "animated.gif"
            first_frame = Image.new("RGB", (10, 10), "red")
            first_frame.save(image_path, save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")], duration=100, loop=0)
            instance = plugin.DailyNaiwa(None, {"images": ["files/images/animated.gif"]})

            async def run():
                first = await collect(instance.daily_naiwa(Event("group-a")))
                second = await collect(instance.daily_naiwa(Event("group-b")))
                gallery = await collect(instance.gallery(Event("group-b")))
                return first, second, gallery

            first, second, gallery = asyncio.run(run())
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0][0], "chain")
            self.assertEqual(first[0][1][0].qq, "same-user")
            self.assertEqual(first[0][1][1].text, "，这是你的今日奶娃：")
            self.assertEqual(first[0][1][2].path, second[0][1][2].path)
            self.assertEqual(Path(first[0][1][2].path).suffix, ".gif")
            self.assertEqual(Path(first[0][1][2].path).read_bytes(), image_path.read_bytes())
            with Image.open(first[0][1][2].path) as sent:
                self.assertEqual(sent.n_frames, 2)
            self.assertEqual(len(gallery), 1)
            self.assertEqual(gallery[0][1][0].qq, "same-user")
            self.assertEqual(gallery[0][1][1].text, "，这是你的奶娃图鉴：")
            self.assertIn("1/1", gallery[0][1][3].text)
            with Image.open(gallery[0][1][2].path) as atlas:
                self.assertEqual(atlas.format, "PNG")
                self.assertEqual(atlas.getpixel((50, 50)), (255, 0, 0))

    def test_cross_group_and_gallery_render(self):
        with tempfile.TemporaryDirectory() as root:
            plugin.get_astrbot_data_path = lambda: root
            directory = Path(root) / "plugin_data" / Star.name / "files" / "images"
            directory.mkdir(parents=True)
            names = []
            for i in range(17):
                name = f"{i}.png"
                Image.new("RGB", (10, 10), (i * 10, 20, 40)).save(directory / name)
                names.append(f"files/images/{name}")
            instance = plugin.DailyNaiwa(None, {"images": names})
            async def run():
                first = await collect(instance.daily_naiwa(Event("group-a")))
                second = await collect(instance.daily_naiwa(Event("group-b")))
                page1 = await collect(instance.gallery(Event("group-a"), 1))
                page2 = await collect(instance.gallery(Event("group-b"), 2))
                invalid = await collect(instance.gallery(Event("group-b"), 3))
                return first, second, page1, page2, invalid
            first, second, page1, page2, invalid = asyncio.run(run())
            self.assertEqual(first[0][1][2].path, second[0][1][2].path)
            self.assertEqual(second[0][1][0].qq, "same-user")
            self.assertEqual(second[0][1][1].text, "，这是你的今日奶娃：")
            self.assertIn("1/17", page1[0][1][3].text)
            self.assertIn("1/2", page1[0][1][3].text)
            self.assertIn("2/2", page2[0][1][3].text)
            self.assertIn("页码无效", invalid[0][1])
            # A locked tile is painted as a question mark and does not expose the image.
            entries = instance.catalog.entries("aiocqhttp", "same-user")
            locked = next(i for i, row in enumerate(entries[:16]) if not row[2])
            x = 16 + (locked % 4) * 162
            y = 16 + (locked // 4) * 188
            with Image.open(page1[0][1][2].path) as atlas:
                tile = atlas.crop((x, y, x + 150, y + 150))
                self.assertNotEqual(tile.getpixel((75, 75)), tile.getpixel((10, 10)))
            self.assertTrue(Path(first[0][1][2].path).is_file())

if __name__ == "__main__":
    unittest.main()
