"""今日奶娃: a per-user daily draw and illustrated collection."""

import asyncio
import hashlib
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .store import Catalog, CatalogError


PAGE_SIZE = 16
CHINA_TIME = timezone(timedelta(hours=8))


def _font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def render_gallery(catalog, entries, page):
    """Render an atlas page; do not reveal locked images or their appearance."""
    start = (page - 1) * PAGE_SIZE
    current = entries[start:start + PAGE_SIZE]
    columns, cell, gap, margin = 4, 150, 12, 16
    rows = max(1, (len(current) + columns - 1) // columns)
    canvas = Image.new("RGB", (margin * 2 + columns * cell + (columns - 1) * gap,
                               margin * 2 + rows * (cell + 26) + (rows - 1) * gap), "#fff5f7")
    draw = ImageDraw.Draw(canvas)
    for index, (asset_id, ordinal, collected, _active) in enumerate(current):
        x = margin + (index % columns) * (cell + gap)
        y = margin + (index // columns) * (cell + 26 + gap)
        draw.rounded_rectangle((x, y, x + cell, y + cell), radius=12, fill="#eadfe4")
        if collected:
            with Image.open(catalog.image_path(asset_id)) as source:
                # The atlas is a static image; animated GIFs use their first frame.
                source.seek(0)
                thumb = ImageOps.fit(source.convert("RGB"), (cell, cell))
                canvas.paste(thumb, (x, y))
        else:
            draw.text((x + cell // 2, y + cell // 2), "?", anchor="mm", fill="#9e8290", font=_font(70))
        draw.text((x + cell // 2, y + cell + 13), f"#{ordinal}", anchor="mm", fill="#6c515e", font=_font(16))
    cache = catalog.directory / "gallery_cache"
    cache.mkdir(parents=True, exist_ok=True)
    # Remove old generated pages while leaving recent in-flight sends intact.
    cutoff = time.time() - 24 * 60 * 60
    for old in cache.glob("*.png"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        except OSError:
            pass
    # Reuse the same page when its contents have not changed, and avoid exposing IDs.
    digest = hashlib.sha256(repr((page, current)).encode()).hexdigest()
    target = cache / f"{digest}.png"
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=cache, suffix=".png", delete=False) as tmp:
            name = tmp.name
        try:
            canvas.save(name, "PNG")
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return target


class DailyNaiwa(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self.catalog = Catalog(data_dir)
        self._lock = asyncio.Lock()

    def _sync(self):
        files = self.config.get("images", [])
        if not isinstance(files, list):
            raise CatalogError("图片素材配置不是文件列表，请重新保存配置")
        self.catalog.sync(files)

    def _draw(self, platform, user_id, day):
        files = self.config.get("images", [])
        if not isinstance(files, list):
            raise CatalogError("图片素材配置不是文件列表，请重新保存配置")
        return self.catalog.resolve_daily(files, platform, user_id, day)

    def _gallery(self, platform, user_id, page):
        self._sync()
        entries = self.catalog.entries(platform, user_id)
        pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
        if page < 1 or page > pages:
            return None, entries, pages
        return render_gallery(self.catalog, entries, page), entries, pages

    @filter.command("今日奶娃")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def daily_naiwa(self, event: AstrMessageEvent):
        """每天抽取一张奶娃图片；当天跨群共用结果。"""
        if not event.get_sender_id():
            yield event.plain_result("无法识别发送者，暂时无法抽取。")
            return
        try:
            async with self._lock:
                result = await asyncio.to_thread(
                    self._draw, event.get_platform_name(), str(event.get_sender_id()),
                    datetime.now(CHINA_TIME).date().isoformat(),
                )
            if result is None:
                yield event.plain_result("素材库还是空的，请管理员在插件配置中上传图片。")
                return
            asset_id, fresh, collected = result
            description = ("今天的奶娃来啦！" if fresh else "你今天已经抽过啦，这是你的今日奶娃！")
            if collected:
                description += "\n图鉴解锁新奶娃！"
            yield event.plain_result(description)
            yield event.image_result(str(self.catalog.image_path(asset_id)))
        except CatalogError as exc:
            logger.warning(f"今日奶娃素材读取失败: {exc}")
            yield event.plain_result(f"素材读取失败：{exc}，请管理员检查插件配置。")
        except Exception:
            logger.exception("今日奶娃抽取失败")
            yield event.plain_result("抽取失败，请管理员查看 AstrBot 日志。")

    @filter.command("奶娃图鉴")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def gallery(self, event: AstrMessageEvent, page: int = 1):
        """查看奶娃收集进度，使用 /奶娃图鉴 页码 翻页。"""
        if not event.get_sender_id():
            yield event.plain_result("无法识别发送者，暂时无法查看图鉴。")
            return
        try:
            async with self._lock:
                path, entries, pages = await asyncio.to_thread(
                    self._gallery, event.get_platform_name(), str(event.get_sender_id()), page,
                )
            if not entries:
                yield event.plain_result("图鉴还是空的，请管理员先上传图片。")
                return
            if path is None:
                yield event.plain_result(f"页码无效，请输入 1 到 {pages}。")
                return
            collected = sum(bool(entry[2]) for entry in entries)
            yield event.plain_result(f"奶娃图鉴：已收集 {collected}/{len(entries)}，第 {page}/{pages} 页。\n使用 /奶娃图鉴 页码 翻页。")
            yield event.image_result(str(path))
        except CatalogError as exc:
            logger.warning(f"奶娃图鉴素材读取失败: {exc}")
            yield event.plain_result(f"素材读取失败：{exc}，请管理员检查插件配置。")
        except Exception:
            logger.exception("奶娃图鉴生成失败")
            yield event.plain_result("图鉴生成失败，请管理员查看 AstrBot 日志。")
