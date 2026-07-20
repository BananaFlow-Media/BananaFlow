"""
core/thumbnail_cropper.py  –  Square thumbnail crop (Advanced Setting)
=======================================================================
After yt-dlp embeds a 16:9 YouTube thumbnail, this post-processor reads
the embedded art, crops it to a centred 1:1 square, and re-embeds it.

Implementation: reads the cover art bytes from the file, decodes with
Pillow, crops from the centre, re-encodes as JPEG, writes back.

Supported containers: MP3 (ID3 APIC), FLAC, M4A (covr).

This module is only called when AppConfig.square_thumbnails is True.
Zero GUI imports.
"""

from __future__ import annotations

import io
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageFilter, ImageEnhance
    _PIL_AVAILABLE = True
except ImportError:
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

logger = logging.getLogger(__name__)


def crop_embedded_thumbnail(file_path: str, pad: bool = False) -> bool:
    """
    Read the cover art from file_path, crop to 1:1 (or pad to 16:9), and re-embed.

    Returns True on success, False if no art found or on failure.
    """
    if not _PIL_AVAILABLE:
        logger.warning("[ThumbnailCropper] Pillow not installed. Run: pip install Pillow")
        return False

    path = Path(file_path)
    suffix = path.suffix.lower()

    try:
        if suffix == ".mp3":
            return _crop_mp3(path, pad=pad)
        elif suffix == ".flac":
            return _crop_flac(path, pad=pad)
        elif suffix in (".m4a", ".mp4", ".aac"):
            return _crop_m4a(path, pad=pad)
        else:
            logger.debug("[ThumbnailCropper] Unsupported format: %s", suffix)
            return False
    except Exception as exc:
        logger.error("[ThumbnailCropper] Error cropping %s: %s", path.name, exc)
        return False


def embed_custom_thumbnail(media_path: str, image_url: str, crop: bool = True, pad: bool = False) -> bool:
    """
    Download an image from image_url, convert it to JPEG (via PIL), optionally
    crop it to a square (or pad to 16:9), and embed it into the media file using mutagen.
    Always converts to JPEG so that WebP/PNG thumbnails (common from YTM) are
    embedded with the correct format rather than causing blank artwork in players.
    """
    if not image_url:
        return False
    try:
        raw_bytes = _fetch_image(image_url)
        if not raw_bytes:
            return False
        if _PIL_AVAILABLE:
            jpeg_bytes = _to_jpeg(raw_bytes, crop=crop, pad=pad)
            if jpeg_bytes:
                raw_bytes = jpeg_bytes
        return _embed_cover(media_path, raw_bytes)
    except Exception as exc:
        logger.error(f"[Thumbnail] embed_custom_thumbnail failed: {exc}")
        return False


_YTIMG_FALLBACKS = [
    "maxresdefault.jpg",
    "hqdefault.jpg",
    "sddefault.jpg",
    "mqdefault.jpg",
    "default.jpg",
]


def _ytimg_fallback_urls(url: str) -> list[str]:
    """For i.ytimg.com URLs return a priority-ordered list of quality variants."""
    if "i.ytimg.com/vi/" not in url:
        return [url]
    for quality in _YTIMG_FALLBACKS:
        if quality in url:
            base = url[: url.rfind(quality)]
            return [base + q for q in _YTIMG_FALLBACKS]
    return [url]


def _fetch_image(url: str) -> Optional[bytes]:
    """Download image bytes from url, with automatic YouTube quality fallback on 404."""
    import urllib.error
    import urllib.request
    import ssl
    
    # Ignore SSL certificate errors (helpful for proxies like NetFree)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for candidate in _ytimg_fallback_urls(url):
        try:
            req = urllib.request.Request(candidate, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403):
                continue
            logger.warning("[Thumbnail] HTTP error fetching image: %s", exc)
            return None
        except Exception as exc:
            logger.warning("[Thumbnail] Error fetching image: %s", exc)
            return None
            
        if not data or len(data) < 12:
            continue
        is_jpeg = data[:2] == b'\xff\xd8'
        is_png  = data[:8] == b'\x89PNG\r\n\x1a\n'
        is_webp = data[:4] == b'RIFF' and data[8:12] == b'WEBP'
        if not (is_jpeg or is_png or is_webp):
            logger.warning("[Thumbnail] URL returned non-image content: %s", candidate)
            continue
        return data
    return None


def _to_jpeg(data: bytes, crop: bool = False, pad: bool = False) -> Optional[bytes]:
    """
    Convert image data (JPEG/PNG/WebP) to JPEG bytes using PIL.
    Optionally crops to a centred square or pads a square to 16:9.
    Returns None on failure so the caller can fall back to raw bytes.
    """
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        
        if crop and w != h:
            side = min(w, h)
            left = (w - side) // 2
            top  = (h - side) // 2
            img  = img.crop((left, top, left + side, top + side))
            
        elif pad and w / h < 1.2:
            # Pad 1:1 to 16:9 using blurred background
            target_w = int(h * (16 / 9))
            target_h = h
            
            # Create the blurred background
            bg = img.copy()
            # Scale background to fill 16:9 canvas
            bg = bg.resize((target_w, target_h), Image.Resampling.LANCZOS)
            # Apply strong blur
            bg = bg.filter(ImageFilter.GaussianBlur(radius=20))
            # Darken the background slightly so the main cover pops out
            enhancer = ImageEnhance.Brightness(bg)
            bg = enhancer.enhance(0.6)
            
            # Paste original image in the center
            offset_x = (target_w - w) // 2
            bg.paste(img, (offset_x, 0))
            img = bg
            
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return None


def _embed_cover(media_path: str, image_bytes: bytes) -> bool:
    """Dispatch to the correct container-specific injector."""
    path = Path(media_path)
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return _inject_mp3_cover(path, image_bytes)
    if suffix == ".flac":
        return _inject_flac_cover(path, image_bytes)
    if suffix in (".m4a", ".mp4", ".aac"):
        return _inject_m4a_cover(path, image_bytes)
    logger.debug("[ThumbnailCropper] Unsupported format for embed: %s", suffix)
    return False


def crop_to_square(image_bytes: bytes) -> Optional[bytes]:
    """Crop image bytes to a centred square and return JPEG bytes."""
    return _to_jpeg(image_bytes, crop=True) if _PIL_AVAILABLE else image_bytes


def _retry_access(action_fn, path: Path, max_retries: int = 5, delay: float = 1.2):
    """Helper to retry metadata access (load/save) when Windows file locks occur."""
    import time
    if not path.exists():
        logger.debug(f"[ThumbnailCropper] Skipping retry: file not found {path.name}")
        return None
        
    last_exc = None
    for i in range(max_retries):
        try:
            return action_fn()
        except (PermissionError, IOError, OSError, Exception) as exc:
            last_exc = exc
            if i < max_retries - 1:
                logger.warning(f"[ThumbnailCropper] File access blocked ({path.name}), retry {i+1}/{max_retries}...")
                time.sleep(delay * (i + 1))
    logger.error(f"[ThumbnailCropper] Failed after {max_retries} retries: {last_exc}")
    return None

def _inject_mp3_cover(path: Path, cropped: bytes) -> bool:
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
    def task():
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            return False
        tags.delall("APIC")
        tags.add(APIC(
            encoding=3, mime="image/jpeg", type=3, desc="", data=cropped
        ))
        tags.save(str(path), v2_version=3)
        return True
    
    res = _retry_access(task, path)
    return bool(res)

def _inject_flac_cover(path: Path, cropped: bytes) -> bool:
    from mutagen.flac import FLAC, Picture
    def task():
        audio = FLAC(str(path))
        audio.clear_pictures()
        new_pic = Picture()
        new_pic.type = 3
        new_pic.mime = "image/jpeg"
        new_pic.desc = "Front Cover"
        new_pic.data = cropped
        audio.add_picture(new_pic)
        audio.save()
        return True
    
    res = _retry_access(task, path)
    return bool(res)

def _inject_m4a_cover(path: Path, cropped: bytes) -> bool:
    from mutagen.mp4 import MP4, MP4Cover
    def task():
        audio = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        audio.tags["covr"] = [MP4Cover(cropped, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return True
    
    res = _retry_access(task, path)
    return bool(res)

def _crop_mp3(path: Path, pad: bool = False) -> bool:
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return False

    apic_keys = [k for k in tags.keys() if k.startswith("APIC")]
    if not apic_keys:
        logger.debug("[ThumbnailCropper] No APIC tag in %s", path.name)
        return False

    key = apic_keys[0]
    apic = tags[key]
    cropped = _to_jpeg(apic.data, crop=not pad, pad=pad) or apic.data
    return _inject_mp3_cover(path, cropped)

def _crop_flac(path: Path, pad: bool = False) -> bool:
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(path))
    pics = audio.pictures
    if not pics:
        return False

    for pic in pics:
        if pic.type == 3:   # Cover (front)
            cropped = _to_jpeg(pic.data, crop=not pad, pad=pad) or pic.data
            return _inject_flac_cover(path, cropped)
    return False

def _crop_m4a(path: Path, pad: bool = False) -> bool:
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))
    if audio.tags is None:
        return False

    covers = audio.tags.get("covr")
    if not covers:
        return False

    cropped = _to_jpeg(bytes(covers[0]), crop=not pad, pad=pad) or bytes(covers[0])
    return _inject_m4a_cover(path, cropped)
