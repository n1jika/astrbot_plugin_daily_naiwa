"""Persistent image catalog and atomic daily draws."""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_IMAGE_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000
MAX_ARCHIVE_IMAGES = 1000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class CatalogError(Exception):
    """A configured image cannot be loaded safely."""


class Catalog:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.images = self.directory / "catalog_images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.db = self.directory / "catalog.sqlite3"
        # ZIP contents are immutable under their digest. This cache avoids
        # repeatedly decompressing an unchanged archive on every command.
        self._zip_cache: dict[str, tuple[tuple[str, str], ...]] = {}
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
        # GIF snapshots retain their original animation. Older versions wrote
        # PNG snapshots only, so keep those as the fallback for existing data.
        gif = self.images / f"{asset_id}.gif"
        if gif.is_file():
            return gif
        return self.images / f"{asset_id}.png"

    def _archive_images(self, path: Path, data: bytes):
        """Read bounded image entries directly; never extract paths to disk."""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ARCHIVE_ENTRIES:
                    raise CatalogError(f"ZIP 条目过多（最多 {MAX_ARCHIVE_ENTRIES} 项）：{path.name}")
                total = 0
                count = 0
                for entry in entries:
                    name = entry.filename
                    member = PurePosixPath(name)
                    # Reject suspicious names and symlinks even though no entry
                    # is extracted. This also prevents surprising asset names.
                    if (not name or "\\" in name or member.is_absolute()
                            or ".." in member.parts or (member.parts and ":" in member.parts[0])
                            or stat.S_ISLNK(entry.external_attr >> 16)):
                        raise CatalogError(f"ZIP 包含不安全的路径或链接：{path.name}")
                    if entry.is_dir() or "__MACOSX" in member.parts or member.name.startswith("."):
                        continue
                    if member.suffix.lower() not in IMAGE_SUFFIXES:
                        continue
                    count += 1
                    if count > MAX_ARCHIVE_IMAGES:
                        raise CatalogError(f"ZIP 图片过多（最多 {MAX_ARCHIVE_IMAGES} 张）：{path.name}")
                    if entry.file_size > MAX_IMAGE_BYTES:
                        raise CatalogError(f"ZIP 内图片过大（最大 20 MB）：{member.name}")
                    if total + entry.file_size > MAX_ARCHIVE_IMAGE_BYTES:
                        raise CatalogError(f"ZIP 图片解压总大小超过 200 MB：{path.name}")
                    with archive.open(entry) as source:
                        data = source.read(MAX_IMAGE_BYTES + 1)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise CatalogError(f"ZIP 内图片过大（最大 20 MB）：{member.name}")
                    total += len(data)
                    if total > MAX_ARCHIVE_IMAGE_BYTES:
                        raise CatalogError(f"ZIP 图片解压总大小超过 200 MB：{path.name}")
                    yield data, f"{path.name}/{member.name}"
                if count == 0:
                    raise CatalogError(f"ZIP 素材包中没有支持的图片：{path.name}")
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
            raise CatalogError(f"无法读取 ZIP 素材包：{path.name}") from exc

    def _save_image(self, data: bytes, label: str) -> str:
        """Store a snapshot keyed by original bytes; retain original GIFs."""
        asset_id = hashlib.sha256(data).hexdigest()
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format == "GIF":
                    image.seek(0)
                    image.load()
                    dest = self.images / f"{asset_id}.gif"
                    if not dest.is_file():
                        with tempfile.NamedTemporaryFile(dir=self.images, suffix=".gif", delete=False) as tmp:
                            tmp_name = tmp.name
                            tmp.write(data)
                        try:
                            os.replace(tmp_name, dest)
                        finally:
                            if os.path.exists(tmp_name):
                                os.unlink(tmp_name)
                else:
                    dest = self.images / f"{asset_id}.png"
                    if not dest.is_file():
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
            raise CatalogError(f"无法读取图片：{label}") from exc
        return asset_id

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
            if path.suffix.lower() == ".zip":
                try:
                    with path.open("rb") as source:
                        archive_data = source.read(MAX_ARCHIVE_BYTES + 1)
                except OSError as exc:
                    raise CatalogError(f"无法读取 ZIP 素材包：{path.name}") from exc
                if len(archive_data) > MAX_ARCHIVE_BYTES:
                    raise CatalogError(f"ZIP 文件过大（最大 100 MB）：{path.name}")
                digest = hashlib.sha256(archive_data).hexdigest()
                cached = self._zip_cache.get(digest)
                if cached is not None and all((self.images / f"{asset_id}{ext}").is_file() for asset_id, ext in cached):
                    for asset_id, _ in cached:
                        if asset_id not in ids:
                            ids.append(asset_id)
                    continue
                image_data = self._archive_images(path, archive_data)
            else:
                try:
                    with path.open("rb") as source:
                        data = source.read(MAX_IMAGE_BYTES + 1)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise CatalogError(f"图片过大（最大 20 MB）：{path.name}")
                except OSError as exc:
                    raise CatalogError(f"无法读取图片：{path.name}") from exc
                image_data = [(data, path.name)]
            imported = []
            for data, label in image_data:
                asset_id = self._save_image(data, label)
                imported.append((asset_id, self.image_path(asset_id).suffix))
                if asset_id not in ids:
                    ids.append(asset_id)
            if path.suffix.lower() == ".zip":
                if len(self._zip_cache) >= 8:
                    self._zip_cache.pop(next(iter(self._zip_cache)))
                self._zip_cache[digest] = tuple(imported)

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
