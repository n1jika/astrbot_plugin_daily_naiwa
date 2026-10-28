"""Persistent image catalog and atomic daily draws."""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import sqlite3
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


class CatalogError(Exception):
    """A configured image cannot be loaded safely."""


class Catalog:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.images = self.directory / "catalog_images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.db = self.directory / "catalog.sqlite3"
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    ordinal INTEGER NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS draws (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    asset_id TEXT NOT NULL REFERENCES assets(id),
                    PRIMARY KEY (platform, user_id, day)
                );
                CREATE TABLE IF NOT EXISTS collection (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL REFERENCES assets(id),
                    PRIMARY KEY (platform, user_id, asset_id)
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def image_path(self, asset_id: str) -> Path:
        return self.images / f"{asset_id}.png"

    def sync(self, configured_files: list[str]):
        """Copy uploads into immutable storage; update the active pool together.

        Uploads are relative to the plugin data directory in AstrBot. Failed
        imports do not partially replace the active pool.
        """
        ids = []
        root = self.directory.resolve()
        for name in configured_files:
            if not isinstance(name, str) or not name:
                raise CatalogError("配置中的图片路径无效")
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise CatalogError("图片路径超出插件数据目录")
            try:
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise CatalogError(f"图片过大（最大 20 MB）：{path.name}")
                data = path.read_bytes()
                asset_id = hashlib.sha256(data).hexdigest()
                dest = self.image_path(asset_id)
                if not dest.is_file():
                    with Image.open(io.BytesIO(data)) as image:
                        image.seek(0)
                        image = ImageOps.exif_transpose(image).convert("RGB")
                        image.thumbnail((4096, 4096))
                        with tempfile.NamedTemporaryFile(dir=self.images, suffix=".png", delete=False) as tmp:
                            tmp_name = tmp.name
                        try:
                            image.save(tmp_name, "PNG")
                            os.replace(tmp_name, dest)
                        finally:
                            if os.path.exists(tmp_name):
                                os.unlink(tmp_name)
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                raise CatalogError(f"无法读取图片：{path.name}") from exc
            if asset_id not in ids:
                ids.append(asset_id)

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE assets SET active=0")
            for asset_id in ids:
                db.execute("""INSERT INTO assets(id, ordinal, active)
                    VALUES (?, (SELECT COALESCE(MAX(ordinal), 0) + 1 FROM assets), 1)
                    ON CONFLICT(id) DO UPDATE SET active=1""", (asset_id,))

    def existing_draw(self, platform: str, user_id: str, day: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT asset_id FROM draws WHERE platform=? AND user_id=? AND day=?",
                (platform, user_id, day),
            ).fetchone()
        return (row[0], False, False) if row else None

    def resolve_daily(self, configured_files, platform, user_id, day):
        # Today’s already committed result remains usable even when an upload
        # has subsequently been deleted or corrupted in the configuration.
        existing = self.existing_draw(platform, user_id, day)
        if existing is not None:
            return existing
        self.sync(configured_files)
        return self.draw(platform, user_id, day)

    def draw(self, platform: str, user_id: str, day: str):
        """Return (asset_id, is_new_draw, is_new_collection), or None."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT asset_id FROM draws WHERE platform=? AND user_id=? AND day=?",
                (platform, user_id, day),
            ).fetchone()
            if existing:
                return existing[0], False, False
            active = [row[0] for row in db.execute("SELECT id FROM assets WHERE active=1 ORDER BY ordinal")]
            if not active:
                return None
            asset_id = secrets.choice(active)
            db.execute("INSERT INTO draws VALUES (?, ?, ?, ?)", (platform, user_id, day, asset_id))
            inserted = db.execute(
                "INSERT OR IGNORE INTO collection VALUES (?, ?, ?)",
                (platform, user_id, asset_id),
            ).rowcount
            return asset_id, True, bool(inserted)

    def entries(self, platform: str, user_id: str):
        with self._connect() as db:
            return db.execute("""
                SELECT a.id, a.ordinal, c.asset_id IS NOT NULL, a.active
                FROM assets a LEFT JOIN collection c
                    ON c.asset_id=a.id AND c.platform=? AND c.user_id=?
                WHERE a.active=1 OR c.asset_id IS NOT NULL
                ORDER BY a.ordinal
            """, (platform, user_id)).fetchall()
