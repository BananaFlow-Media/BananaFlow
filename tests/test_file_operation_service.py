from pathlib import Path

import pytest

from ui.services.file_operation_service import FileOperationError, FileOperationService


def test_rename_move_create_and_properties_stay_inside_root(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    source = root / "track 2.mp3"
    source.write_bytes(b"audio")
    service = FileOperationService(root)

    renamed = service.rename(source, "track 10.mp3")
    assert renamed == root / "track 10.mp3"
    assert renamed.exists()

    album = service.create_folder(root, "Album")
    moved = service.move(renamed, album / renamed.name)
    assert moved == album / "track 10.mp3"
    assert moved.exists()

    props = service.properties(moved)
    assert props.path == moved
    assert props.size_bytes == len(b"audio")
    assert not props.is_directory


def test_service_rejects_root_escape_invalid_names_and_root_mutation(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    source = root / "track.mp3"
    source.write_bytes(b"audio")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    external = outside / "outside.mp3"
    external.write_bytes(b"audio")
    service = FileOperationService(root)

    with pytest.raises(FileOperationError, match="outside"):
        service.copy_path(external)
    with pytest.raises(FileOperationError, match="not valid"):
        service.rename(source, "bad:name.mp3")
    with pytest.raises(FileOperationError, match="root"):
        service.recycle(root)
    with pytest.raises(FileOperationError, match="outside"):
        service.move(source, outside / source.name)


def test_recycle_uses_shared_recycle_backend(monkeypatch, tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    source = root / "track.mp3"
    source.write_bytes(b"audio")
    service = FileOperationService(root)
    seen = []
    monkeypatch.setattr(
        "ui.services.file_operation_service.send_to_recycle_bin",
        lambda path: seen.append(path),
    )

    service.recycle(source)

    assert seen == [source]
