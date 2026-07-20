from pathlib import Path

from core.change_sets import FileIdentity
from core.filesystem_monitoring import (
    EventCoalescer, FilesystemEvent, FilesystemEventKind, OwnOperationMatch,
    WatchRootSession, normalize_filesystem_event, path_within_root,
    strong_same_file,
)


def raw(session, sequence, kind, path, **kwargs):
    return FilesystemEvent(session.session_id, session.generation, sequence,
                           kind, path, **kwargs)


def test_normalization_restates_create_remove_directory_and_root(tmp_path):
    root = tmp_path / "music"; root.mkdir()
    session = WatchRootSession.create(root, 3, 7)
    created = root / "song.mp3"; created.write_bytes(b"x")
    event = normalize_filesystem_event(root, raw(
        session, 1, FilesystemEventKind.MODIFIED, created))
    assert event.kind is FilesystemEventKind.MODIFIED and event.evidence.exists

    missing = normalize_filesystem_event(root, raw(
        session, 2, FilesystemEventKind.MODIFIED, root / "missing.mp3"))
    assert missing.kind is FilesystemEventKind.REMOVED

    missing_dir = normalize_filesystem_event(root, raw(
        session, 3, FilesystemEventKind.REMOVED, root / "gone",
        is_directory_hint=True))
    assert missing_dir.kind is FilesystemEventKind.DIRECTORY_REMOVED

    lost = normalize_filesystem_event(root, raw(
        session, 4, FilesystemEventKind.ROOT_LOST, root / "not-the-root"))
    assert lost.kind is FilesystemEventKind.ROOT_LOST


def test_root_boundary_rejects_sibling_prefix_and_outside_event(tmp_path):
    root = tmp_path / "music"; root.mkdir()
    sibling = tmp_path / "music-copy"; sibling.mkdir()
    session = WatchRootSession.create(root, 1, 1)
    assert path_within_root(root, root / "a.mp3")
    assert not path_within_root(root, sibling / "a.mp3")
    normalized = normalize_filesystem_event(root, raw(
        session, 1, FilesystemEventKind.MODIFIED, sibling / "a.mp3"))
    assert normalized.kind is FilesystemEventKind.OVERFLOW_UNKNOWN
    assert normalized.evidence.outside_root


def test_coalescer_collapses_bursts_and_preserves_authoritative_absence(tmp_path):
    root = tmp_path / "music"; root.mkdir(); path = root / "song.mp3"
    path.write_bytes(b"a")
    session = WatchRootSession.create(root, 1, 1)
    created = normalize_filesystem_event(root, raw(
        session, 1, FilesystemEventKind.CREATED, path))
    modified = normalize_filesystem_event(root, raw(
        session, 2, FilesystemEventKind.MODIFIED, path))
    removed_hint = raw(session, 3, FilesystemEventKind.REMOVED, root / "gone.mp3")
    removed = normalize_filesystem_event(root, removed_hint)
    coalescer = EventCoalescer(); coalescer.extend([created, modified, removed])
    batch = coalescer.drain()
    by_path = {event.path: event for event in batch.events}
    assert by_path[path].kind is FilesystemEventKind.CREATED
    assert by_path[root / "gone.mp3"].kind is FilesystemEventKind.REMOVED
    assert coalescer.pending_count == 0


def test_coalescer_is_deterministic_and_bounded(tmp_path):
    root = tmp_path / "music"; root.mkdir()
    session = WatchRootSession.create(root, 2, 4)
    events = [normalize_filesystem_event(root, raw(
        session, index, FilesystemEventKind.MODIFIED, root / f"{index}.mp3"))
        for index in range(1, 8)]
    first = EventCoalescer(maximum_paths=3); first.extend(reversed(events))
    batch = first.drain()
    assert batch.overflowed and len(batch.events) == 1
    assert batch.events[0].kind is FilesystemEventKind.OVERFLOW_UNKNOWN


def test_strong_identity_requires_device_and_inode():
    left = FileIdentity("a", 10, 20, 3, 4)
    moved = FileIdentity("b", 10, 21, 3, 4)
    weak = FileIdentity("b", 10, 20, None, None)
    assert strong_same_file(left, moved)
    assert not strong_same_file(left, weak)


def test_root_event_supersedes_path_noise(tmp_path):
    root = tmp_path / "music"; root.mkdir()
    session = WatchRootSession.create(root, 1, 1)
    coalescer = EventCoalescer()
    coalescer.add(normalize_filesystem_event(root, raw(
        session, 1, FilesystemEventKind.MODIFIED, root / "song.mp3")))
    root_event = normalize_filesystem_event(root, raw(
        session, 2, FilesystemEventKind.ROOT_LOST, root))
    coalescer.add(root_event)
    batch = coalescer.drain()
    assert batch.events == (root_event,)
