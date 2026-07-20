"""Safe artwork validation and export helpers.

The only Qt use is the final trusted decoder boundary.  Qt image instances
never cross this module boundary or enter the metadata model.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import zlib

from core.metadata_models import ArtworkEntry

MAX_ENCODED_BYTES = 20 * 1024 * 1024
MAX_DIMENSION = 12_000
MAX_PIXELS = 50_000_000
_MIME_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}


class ArtworkValidationError(ValueError):
    """Stable error key suitable for localized UI messaging."""
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def validate_artwork_bytes(data: bytes, *, description: str = "", picture_type: int = 3) -> ArtworkEntry:
    """Accept only fully structured and Qt-decodable JPEG/PNG bytes."""
    data = bytes(data)
    if not data or len(data) > MAX_ENCODED_BYTES:
        raise ArtworkValidationError("meta_artwork_file_too_large")
    try:
        mime, width, height, depth = _image_header(data)
    except ArtworkValidationError:
        raise
    except Exception as exc:
        raise ArtworkValidationError("meta_artwork_invalid_image") from exc
    if mime not in _MIME_EXTENSIONS:
        raise ArtworkValidationError("meta_artwork_unsupported_image")
    if width <= 0 or height <= 0:
        raise ArtworkValidationError("meta_artwork_invalid_image")
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise ArtworkValidationError("meta_artwork_dimensions_too_large")
    # The production Inspector and the core proposal boundary deliberately
    # share the same decoder.  A header-looking blob cannot become writable
    # unless the app can decode it.
    try:
        from PySide6.QtGui import QImage
        image = QImage.fromData(data)
        if image.isNull() or image.width() != width or image.height() != height:
            raise ArtworkValidationError("meta_artwork_invalid_image")
    except ArtworkValidationError:
        raise
    except Exception as exc:
        raise ArtworkValidationError("meta_artwork_invalid_image") from exc
    return ArtworkEntry(data, mime, picture_type, description, width, height, depth)


def _image_header(data: bytes) -> tuple[str, int, int, int]:
    """Bounded PNG/JPEG structure verification before the trusted decode."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _validate_png(data)
    if data.startswith(b"\xff\xd8"):
        return _validate_jpeg(data)
    raise ArtworkValidationError("meta_artwork_unsupported_image")


def _validate_png(data: bytes) -> tuple[str, int, int, int]:
    offset = 8; seen_ihdr = seen_idat = seen_iend = False; width = height = depth = 0
    while offset < len(data):
        if offset + 12 > len(data):
            raise ArtworkValidationError("meta_artwork_invalid_image")
        length = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + length
        if end > len(data):
            raise ArtworkValidationError("meta_artwork_invalid_image")
        kind = data[offset + 4:offset + 8]; payload = data[offset + 8:offset + 8 + length]
        crc = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ArtworkValidationError("meta_artwork_invalid_image")
        if kind == b"IHDR":
            if seen_ihdr or length != 13 or offset != 8:
                raise ArtworkValidationError("meta_artwork_invalid_image")
            seen_ihdr = True
            width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            valid_bits = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
            if not width or not height or bits not in valid_bits.get(color, set()) or compression or filtering or interlace not in {0, 1}:
                raise ArtworkValidationError("meta_artwork_invalid_image")
            depth = bits * channels
        elif kind == b"acTL":
            raise ArtworkValidationError("meta_artwork_animated")
        elif kind == b"IDAT":
            if not seen_ihdr or seen_iend or not length:
                raise ArtworkValidationError("meta_artwork_invalid_image")
            seen_idat = True
        elif kind == b"IEND":
            if length or not seen_ihdr or not seen_idat or end != len(data):
                raise ArtworkValidationError("meta_artwork_invalid_image")
            seen_iend = True
            break
        offset = end
    if not seen_iend:
        raise ArtworkValidationError("meta_artwork_invalid_image")
    return "image/png", width, height, depth


def _validate_jpeg(data: bytes) -> tuple[str, int, int, int]:
    i = 2; width = height = depth = 0; saw_sof = saw_sos = False
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while i < len(data):
        if data[i] != 0xFF:
            raise ArtworkValidationError("meta_artwork_invalid_image")
        while i < len(data) and data[i] == 0xFF: i += 1
        if i >= len(data): break
        marker = data[i]; i += 1
        if marker == 0xD9:
            if not (saw_sof and saw_sos and i == len(data)):
                raise ArtworkValidationError("meta_artwork_invalid_image")
            return "image/jpeg", width, height, depth
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            raise ArtworkValidationError("meta_artwork_invalid_image")
        if i + 2 > len(data): raise ArtworkValidationError("meta_artwork_invalid_image")
        size = int.from_bytes(data[i:i + 2], "big")
        if size < 2 or i + size > len(data): raise ArtworkValidationError("meta_artwork_invalid_image")
        payload = data[i + 2:i + size]
        if marker in sof_markers:
            if saw_sof or len(payload) < 6: raise ArtworkValidationError("meta_artwork_invalid_image")
            bits = payload[0]; height = int.from_bytes(payload[1:3], "big"); width = int.from_bytes(payload[3:5], "big"); components = payload[5]
            if not width or not height or bits not in {8, 12} or not 1 <= components <= 4 or len(payload) != 6 + components * 3:
                raise ArtworkValidationError("meta_artwork_invalid_image")
            depth = bits * components; saw_sof = True
        if marker == 0xDA:
            if not saw_sof or len(payload) < 6: raise ArtworkValidationError("meta_artwork_invalid_image")
            saw_sos = True; i += size
            # entropy data continues until an actual marker; stuffed FF00 and
            # restart markers remain part of the scan.
            while i < len(data) - 1:
                if data[i] != 0xFF: i += 1; continue
                nxt = data[i + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7: i += 2; continue
                break
            continue
        i += size
    raise ArtworkValidationError("meta_artwork_invalid_image")


def load_artwork_file(path: Path) -> ArtworkEntry:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ArtworkValidationError("meta_artwork_invalid_image") from exc
    return validate_artwork_bytes(data)


def artwork_export_suffix(mime_type: str) -> str:
    return _MIME_EXTENSIONS.get(mime_type.lower(), ".bin")


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_artwork_export_name(stem: str, entry: ArtworkEntry, index: int) -> str:
    """Stable, path-safe name for an original encoded artwork payload."""
    role = {3: "front", 4: "back", 8: "artist"}.get(entry.picture_type, f"picture-{entry.picture_type}")
    description = "".join("_" if char in '<>:"/\\|?*' else char for char in entry.description).strip(" ._")
    parts = ["".join("_" if char in '<>:"/\\|?*' else char for char in stem).strip(" ._") or "artwork",
             f"{index + 1:02d}", role]
    if description: parts.append(description[:48])
    name = "-".join(parts)
    if name.upper() in _WINDOWS_RESERVED: name = "artwork-" + name
    return name + artwork_export_suffix(entry.mime_type)


def export_artwork_entries(destination: Path, stem: str, entries: tuple[ArtworkEntry, ...]) -> list[Path]:
    """Write every original payload with collision-safe deterministic names."""
    if not destination.is_dir():
        raise ArtworkValidationError("meta_artwork_export_invalid_destination")
    results: list[Path] = []
    for index, entry in enumerate(entries):
        # Do not accidentally offer corrupted embedded data as a normal export.
        validate_artwork_bytes(entry.data, description=entry.description, picture_type=entry.picture_type)
        target = destination / safe_artwork_export_name(stem, entry, index)
        if target.exists():
            raise ArtworkValidationError("meta_artwork_export_collision")
        target.write_bytes(entry.data)
        results.append(target)
    return results
