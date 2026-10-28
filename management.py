"""Dashboard view API for managing catalog descriptions."""

import asyncio
import base64
import io
import re

from PIL import Image, ImageOps

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .store import CatalogError


PLUGIN_NAME = "astrbot_plugin_daily_naiwa"
ASSET_ID = re.compile(r"^[0-9a-f]{64}$")


class AssetManagement:
    def __init__(self, plugin):
        self.plugin = plugin

    def register(self, context):
        for route, handler, methods, description in (
            ("assets", self.list_assets, ["GET"], "读取奶蛙素材列表"),
            ("assets/preview", self.preview, ["GET"], "查看奶蛙素材缩略图"),
            ("assets/save", self.save, ["POST"], "保存奶蛙素材信息"),
        ):
            context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, description)

    async def list_assets(self):
        try:
            async with self.plugin._lock:
                await asyncio.to_thread(self.plugin._sync)
                assets = await asyncio.to_thread(self.plugin.catalog.list_assets)
            return json_response({"assets": assets})
        except CatalogError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception("读取奶蛙素材列表失败")
            return error_response("读取素材列表失败，请查看 AstrBot 日志", status_code=500)

    def _preview(self, asset_id):
        # Only catalog IDs are accepted, never a client-provided filesystem path.
        self.plugin.catalog.get_metadata(asset_id)
        with Image.open(self.plugin.catalog.image_path(asset_id)) as image:
            image.seek(0)
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((240, 240))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=75)
        return base64.b64encode(output.getvalue()).decode("ascii")

    async def preview(self):
        asset_id = request.query.get("id", "")
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            return error_response("素材编号无效", status_code=400)
        try:
            encoded = await asyncio.to_thread(self._preview, asset_id)
            return json_response({"image": encoded})
        except CatalogError as exc:
            return error_response(str(exc), status_code=404)
        except (OSError, ValueError):
            return error_response("无法读取素材预览", status_code=404)

    async def save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式无效", status_code=400)
        asset_id = payload.get("id")
        name = payload.get("name")
        description = payload.get("description")
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            return error_response("素材编号无效", status_code=400)
        if not isinstance(name, str) or len(name) > 100:
            return error_response("名称须为不超过 100 个字符的文字", status_code=400)
        if not isinstance(description, str) or len(description) > 2000:
            return error_response("介绍须为不超过 2000 个字符的文字", status_code=400)
        name, description = name.strip(), description.strip()
        try:
            async with self.plugin._lock:
                await asyncio.to_thread(self.plugin.catalog.set_metadata, asset_id, name, description)
            return json_response({"saved": True, "name": name, "description": description})
        except CatalogError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception("保存奶蛙素材信息失败")
            return error_response("保存失败，请查看 AstrBot 日志", status_code=500)
