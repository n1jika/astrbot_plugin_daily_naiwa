import asyncio
import importlib
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# Provide only AstrBot's interface; this checks handlers but is not an E2E test.
for name in ("astrbot", "astrbot.api", "astrbot.api.web", "astrbot.api.event", "astrbot.api.star", "astrbot.api.message_components", "astrbot.core", "astrbot.core.utils", "astrbot.core.utils.astrbot_path"):
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
web = sys.modules["astrbot.api.web"]
web.request = object()
web.json_response = lambda data: data
web.error_response = lambda message, status_code=400: (message, status_code)
event_api = sys.modules["astrbot.api.event"]
event_api.AstrMessageEvent = object
class Filters:
    class EventMessageType:
        GROUP_MESSAGE = 1
    @staticmethod
    def command(name):
        def decorate(fn):
            fn.command_name = name
            return fn
        return decorate
    @staticmethod
    def event_message_type(*args):
        return lambda fn: fn
event_api.filter = Filters
star = sys.modules["astrbot.api.star"]
class Context:
    def register_web_api(self, *args):
        pass
star.Context = Context
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
            instance = plugin.DailyNaiwa(Context(), {"images": ["files/images/animated.gif"]})

            async def run():
                first = await collect(instance.daily_naiwa(Event("group-a")))
                second = await collect(instance.daily_naiwa(Event("group-b")))
                gallery = await collect(instance.gallery(Event("group-b")))
                return first, second, gallery

            first, second, gallery = asyncio.run(run())
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0][0], "chain")
            self.assertEqual(first[0][1][0].qq, "same-user")
            self.assertEqual(first[0][1][1].text, "，你的今日奶蛙是：\n")
            self.assertEqual(len(first[0][1]), 3)
            self.assertEqual(first[0][1][2].path, second[0][1][2].path)
            self.assertEqual(Path(first[0][1][2].path).suffix, ".gif")
            self.assertEqual(Path(first[0][1][2].path).read_bytes(), image_path.read_bytes())
            with Image.open(first[0][1][2].path) as sent:
                self.assertEqual(sent.n_frames, 2)
            self.assertEqual(len(gallery), 1)
            self.assertEqual(gallery[0][1][0].qq, "same-user")
            self.assertEqual(gallery[0][1][1].text, "，这是你的奶蛙图鉴：")
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
            instance = plugin.DailyNaiwa(Context(), {"images": names})
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
            self.assertEqual(second[0][1][1].text, "，你的今日奶蛙是：\n")
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

    def test_commands_and_custom_description_with_existing_draw(self):
        self.assertEqual(plugin.DailyNaiwa.daily_naiwa.command_name, "今日奶蛙")
        self.assertEqual(plugin.DailyNaiwa.gallery.command_name, "奶蛙图鉴")
        self.assertEqual(plugin.DailyNaiwa.view_gallery_item.command_name, "查看图鉴")
        with tempfile.TemporaryDirectory() as root:
            plugin.get_astrbot_data_path = lambda: root
            directory = Path(root) / "plugin_data" / Star.name / "files" / "images"
            directory.mkdir(parents=True)
            Image.new("RGB", (10, 10), "green").save(directory / "one.png")
            config = {"images": ["files/images/one.png"]}
            old = plugin.DailyNaiwa(Context(), config)
            first = asyncio.run(collect(old.daily_naiwa(Event("group-a"))))
            asset_id = old.catalog.entries("aiocqhttp", "same-user")[0][0]
            old.catalog.set_metadata(asset_id, "小青", "今天也要开心")

            # Recreating the plugin simulates reload: the existing draw and
            # collection survive, while the latest metadata is used.
            reloaded = plugin.DailyNaiwa(Context(), config)
            result = asyncio.run(collect(reloaded.daily_naiwa(Event("group-b"))))
            self.assertEqual(result[0][1][1].text, "，你的今日奶蛙是“小青”：\n")
            self.assertEqual(result[0][1][2].path, first[0][1][2].path)
            self.assertEqual(result[0][1][3].text, "\n今天也要开心")
            self.assertTrue(reloaded.catalog.entries("aiocqhttp", "same-user")[0][2])
            reloaded.catalog.set_metadata(asset_id, "小青", "")
            name_only = asyncio.run(collect(reloaded.daily_naiwa(Event("group-a"))))
            self.assertEqual(name_only[0][1][1].text, "，你的今日奶蛙是“小青”：\n")
            self.assertEqual(len(name_only[0][1]), 3)
            reloaded.catalog.set_metadata(asset_id, "", "今天也要开心")
            description_only = asyncio.run(collect(reloaded.daily_naiwa(Event("group-a"))))
            self.assertEqual(description_only[0][1][1].text, "，你的今日奶蛙是：\n")
            self.assertEqual(description_only[0][1][3].text, "\n今天也要开心")

    def test_view_gallery_item_uses_page_position_and_checks_unlock(self):
        with tempfile.TemporaryDirectory() as root:
            plugin.get_astrbot_data_path = lambda: root
            directory = Path(root) / "plugin_data" / Star.name / "files" / "images"
            directory.mkdir(parents=True)
            names = []
            for i in range(17):
                name = f"{i}.png"
                Image.new("RGB", (10, 10), (i * 10, 20, 40)).save(directory / name)
                names.append(f"files/images/{name}")
            instance = plugin.DailyNaiwa(Context(), {"images": names})
            instance._sync()
            entries = instance.catalog.entries("aiocqhttp", "same-user")
            first_id, last_id = entries[0][0], entries[16][0]
            with instance.catalog._connect() as db:
                db.execute("INSERT INTO collection VALUES (?, ?, ?)", ("aiocqhttp", "same-user", first_id))
                db.execute("INSERT INTO collection VALUES (?, ?, ?)", ("aiocqhttp", "same-user", last_id))

            async def run():
                event = Event("group-a")
                return [await collect(instance.view_gallery_item(event, page, item))
                        for page, item in [(1, 1), (2, 1), (1, 2), (0, 1), (3, 1), (1, 0), (1, 17), (2, 2)]]
            first, last, locked, bad_page_zero, bad_page_high, bad_item_zero, bad_item_high, missing_item = asyncio.run(run())
            self.assertEqual(first[0][1][1].path, str(instance.catalog.image_path(first_id)))
            self.assertEqual(last[0][1][1].path, str(instance.catalog.image_path(last_id)))
            self.assertEqual(locked[0], ("text", "该奶蛙未解锁！"))
            self.assertIn("页码无效", bad_page_zero[0][1])
            self.assertIn("页码无效", bad_page_high[0][1])
            for result in (bad_item_zero, bad_item_high, missing_item):
                self.assertIn("该页没有这个项目", result[0][1])

    def test_existing_pre_metadata_database_survives_upgrade(self):
        with tempfile.TemporaryDirectory() as root:
            plugin.get_astrbot_data_path = lambda: root
            directory = Path(root) / "plugin_data" / Star.name
            images = directory / "catalog_images"
            images.mkdir(parents=True)
            asset_id = "a" * 64
            Image.new("RGB", (10, 10), "red").save(images / f"{asset_id}.png")
            today = plugin.datetime.now(plugin.CHINA_TIME).date().isoformat()
            with sqlite3.connect(directory / "catalog.sqlite3") as db:
                db.executescript("""
                    CREATE TABLE assets (id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1);
                    CREATE TABLE draws (platform TEXT NOT NULL, user_id TEXT NOT NULL, day TEXT NOT NULL,
                        asset_id TEXT NOT NULL REFERENCES assets(id), PRIMARY KEY (platform, user_id, day));
                    CREATE TABLE collection (platform TEXT NOT NULL, user_id TEXT NOT NULL,
                        asset_id TEXT NOT NULL REFERENCES assets(id), PRIMARY KEY (platform, user_id, asset_id));
                """)
                db.execute("INSERT INTO assets VALUES (?, 1, 1)", (asset_id,))
                db.execute("INSERT INTO draws VALUES (?, ?, ?, ?)", ("aiocqhttp", "same-user", today, asset_id))
                db.execute("INSERT INTO collection VALUES (?, ?, ?)", ("aiocqhttp", "same-user", asset_id))
            db.close()
            instance = plugin.DailyNaiwa(Context(), {"images": []})
            result = asyncio.run(collect(instance.daily_naiwa(Event("group-a"))))
            self.assertEqual(result[0][1][2].path, str(images / f"{asset_id}.png"))
            self.assertEqual(result[0][1][1].text, "，你的今日奶蛙是：\n")
            instance.catalog.set_metadata(asset_id, "旧图名称", "旧图介绍")
            result = asyncio.run(collect(instance.view_gallery_item(Event("group-a"), 1, 1)))
            self.assertEqual(result[0][1][1].path, str(images / f"{asset_id}.png"))
            self.assertEqual(instance.catalog.get_metadata(asset_id), ("旧图名称", "旧图介绍"))

if __name__ == "__main__":
    unittest.main()
