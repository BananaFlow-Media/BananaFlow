"""Immutable Extended M3U/M3U8 planning and atomic export."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import codecs
import os
from pathlib import Path
import re
from typing import Iterable

from core.metadata_io import (
    CancellationToken, IORequestIdentity, IOScope, IOWarningInfo,
    IOWarningKind, MetadataValueSource, atomic_write_bytes,
)


class PlaylistOrder(str, Enum):
    CURRENT_VIEW = "current_view"
    NATURAL_PATH = "natural_path"
    TRACK_DISC = "track_disc"


class PlaylistPathMode(str, Enum):
    AUTO = "auto"
    RELATIVE = "relative"
    ABSOLUTE = "absolute"


class PlaylistFormat(str, Enum):
    M3U = "m3u"
    M3U8 = "m3u8"


@dataclass(frozen=True)
class PlaylistEntry:
    item_id: int
    path: Path
    title: str
    artist: str
    album: str
    disc_number: int | None
    track_number: int | None
    duration_seconds: int | None
    pending_rename: bool = False


@dataclass(frozen=True)
class PlaylistExportPlan:
    identity: IORequestIdentity
    scope: IOScope
    order: PlaylistOrder
    path_mode: PlaylistPathMode
    value_source: MetadataValueSource
    format: PlaylistFormat
    entries: tuple[PlaylistEntry, ...]
    warnings: tuple[IOWarningInfo, ...] = ()


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold()
                 for part in re.split(r"(\d+)", value))


def build_playlist_plan(workspace, *, item_ids: Iterable[int], scope: IOScope,
                        order: PlaylistOrder = PlaylistOrder.CURRENT_VIEW,
                        path_mode: PlaylistPathMode = PlaylistPathMode.AUTO,
                        value_source: MetadataValueSource = MetadataValueSource.EFFECTIVE,
                        format: PlaylistFormat = PlaylistFormat.M3U8) -> PlaylistExportPlan:
    ids = tuple(dict.fromkeys(int(value) for value in item_ids))
    entries: list[PlaylistEntry] = []
    warnings: list[IOWarningInfo] = []
    for identity in ids:
        item = workspace.track_for_id(identity)
        if item is None:
            continue
        if not item.path.is_file():
            warnings.append(IOWarningInfo(IOWarningKind.MISSING_FILE,
                                           arguments=(("filename", item.path.name),)))
            continue
        tags = item.original if value_source is MetadataValueSource.ORIGINAL else item.proposed.effective_tags(item.original)
        duration = tags.file_properties.get("duration_seconds")
        try:
            seconds = max(0, int(float(duration))) if duration is not None else None
        except (TypeError, ValueError):
            seconds = None
        pending = bool(item.proposed_filename and item.proposed_filename != item.path.name)
        if pending:
            warnings.append(IOWarningInfo(IOWarningKind.PENDING_RENAME,
                                           arguments=(("filename", item.path.name),)))
        entries.append(PlaylistEntry(identity, item.path.resolve(), str(tags.title or ""),
                                     str(tags.artist or ""), str(tags.album or ""),
                                     tags.disc_num, tags.track_num, seconds, pending))
    if order is PlaylistOrder.NATURAL_PATH:
        entries.sort(key=lambda entry: _natural_key(str(entry.path)))
    elif order is PlaylistOrder.TRACK_DISC:
        entries.sort(key=lambda entry: (
            entry.disc_number if entry.disc_number is not None else 10**9,
            entry.track_number if entry.track_number is not None else 10**9,
            _natural_key(str(entry.path)),
        ))
    request = IORequestIdentity.create("playlist_export", workspace.generation,
                                       workspace.change_set.revision,
                                       tuple(entry.item_id for entry in entries),
                                       content_revision=workspace.content_revision)
    return PlaylistExportPlan(request, scope, order, path_mode, value_source,
                              format, tuple(entries), tuple(warnings))


def _playlist_path(path: Path, destination: Path, mode: PlaylistPathMode) -> str:
    if mode is PlaylistPathMode.ABSOLUTE:
        return str(path)
    try:
        relative = Path(os.path.relpath(path, destination.parent))
        # On Windows relpath raises for a different drive.  Keep a second
        # explicit drive guard for deterministic tests and non-Windows hosts.
        if path.drive and destination.drive and path.drive.casefold() != destination.drive.casefold():
            raise ValueError("different_drive")
        value = relative.as_posix()
        if mode is PlaylistPathMode.RELATIVE and Path(value).is_absolute():
            raise ValueError("unsafe_relative")
        return value
    except (OSError, ValueError):
        if mode is PlaylistPathMode.RELATIVE:
            raise ValueError("relative_path_unavailable")
        return str(path)


def _one_line(value: str) -> str:
    return " ".join(str(value).replace("\r", "\n").splitlines()).strip()


def render_playlist(plan: PlaylistExportPlan, destination: Path,
                    cancellation: CancellationToken | None = None) -> bytes:
    lines = ["#EXTM3U"]
    for entry in plan.entries:
        if cancellation:
            cancellation.raise_if_cancelled()
        duration = entry.duration_seconds if entry.duration_seconds is not None else -1
        title = _one_line(entry.title) or entry.path.stem
        artist = _one_line(entry.artist)
        label = f"{artist} - {title}" if artist else title
        lines.append(f"#EXTINF:{duration},{label}")
        lines.append(_playlist_path(entry.path, Path(destination), plan.path_mode))
    text = "\r\n".join(lines) + "\r\n"
    data = text.encode("utf-8", errors="strict")
    if plan.format is PlaylistFormat.M3U:
        data = codecs.BOM_UTF8 + data
    return data


def export_playlist(plan: PlaylistExportPlan, destination: Path, *, overwrite: bool = False,
                    cancellation: CancellationToken | None = None):
    data = render_playlist(plan, destination, cancellation)

    def validate(path: Path) -> bool:
        raw = path.read_bytes()
        decoded = raw.decode("utf-8-sig", errors="strict")
        return raw == data and decoded.startswith("#EXTM3U\r\n")

    return atomic_write_bytes(destination, data, overwrite=overwrite,
                              validator=validate, cancellation=cancellation)
