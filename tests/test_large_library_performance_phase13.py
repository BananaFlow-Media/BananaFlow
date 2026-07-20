import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.change_sets import ChangeOrigin
from core.filesystem_monitoring import (
    EventCoalescer, FilesystemEvent, FilesystemEventKind, WatchRootSession,
    normalize_filesystem_event,
)
from core.metadata_validation import MetadataValidationEngine
from core.tag_editor_performance import (
    counter_snapshot, format_samples, measure_operation, synthetic_tracks,
)
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.models.metadata_filter_proxy_model import MetadataFilterProxyModel
from ui.models.metadata_table_model import COL_FILENAME, MetadataTableModel


def app():
    return QApplication.instance() or QApplication([])


def test_deterministic_1k_5k_10k_fixtures_have_unique_stable_metadata():
    for count in (1000, 5000, 10000):
        values = synthetic_tracks(count)
        assert len(values) == count
        assert len({value.path for value in values}) == count
        assert values[0].original.title == "Track 00000"
        assert values[-1].baseline_identity.inode == count - 1 + 1000


def test_workspace_construction_scales_without_obvious_quadratic_path():
    timings = {}
    for count in (1000, 5000, 10000):
        values = synthetic_tracks(count)
        started = time.perf_counter()
        workspace = TagEditorWorkspaceState(); workspace.set_tracks(values)
        timings[count] = time.perf_counter() - started
        assert workspace.track_for_id(count) is values[-1]
    assert timings[10000] < max(5.0, timings[1000] * 30)


def test_10k_filter_search_sort_and_selection_use_memory_only(monkeypatch):
    app(); values = synthetic_tracks(10000)
    workspace = TagEditorWorkspaceState(); model = MetadataTableModel(workspace=workspace)
    model.load_tracks(values)
    proxy = MetadataFilterProxyModel(workspace); proxy.setSourceModel(model)
    monkeypatch.setattr(Path, "stat", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("filtering must not touch disk")))
    proxy.set_search_text("artist 042")
    assert 0 < proxy.rowCount() < 10000
    model.sort(COL_FILENAME, Qt.DescendingOrder)
    before = proxy.invalidation_count
    for item in values[::1000]:
        workspace.set_selected_items([item])
    assert proxy.invalidation_count == before


def test_100_file_proposal_batch_and_problems_refresh_are_bounded():
    values = synthetic_tracks(10000)
    workspace = TagEditorWorkspaceState(); workspace.set_tracks(values)
    targets = values[:100]
    for index, item in enumerate(targets):
        item.proposed.title = f"Pending {index}"
    workspace.capture_proposals(targets, ChangeOrigin.MANUAL, label="100-file burst")
    assert len(workspace.change_set.records()) == 100
    sample = measure_operation(
        "problems_10k", 10000,
        lambda: MetadataValidationEngine().validate(workspace),
        samples=2, warmups=0)
    assert sample.slowest_ms < 30000


def test_model_counters_prove_one_file_refresh_has_no_reset():
    app(); values = synthetic_tracks(10000)
    workspace = TagEditorWorkspaceState(); model = MetadataTableModel(workspace=workspace)
    model.load_tracks(values)
    before = counter_snapshot(model)
    values[5000].original.title = "Changed"
    model.refresh_track(values[5000])
    after = counter_snapshot(model)
    assert after.resets == before.resets
    assert after.refreshed_rows == before.refreshed_rows + 1


def test_model_population_is_measured_separately_at_1k_5k_10k():
    """TE-PERF-02: source-model population is its own reference measurement."""
    app()
    samples = []
    for count in (1000, 5000, 10000):
        values = synthetic_tracks(count)
        workspace = TagEditorWorkspaceState()
        model = MetadataTableModel(workspace=workspace)
        samples.append(measure_operation(
            f"model_population_{count}", count,
            lambda m=model, v=values: m.load_tracks(v), samples=2, warmups=1))
        assert model.rowCount() == count
    slowest_10k = samples[-1].slowest_ms
    # Generous reference ceiling (plan: 10k construction under 5s, 3x CI
    # allowance) rather than a fragile microsecond assertion.
    assert slowest_10k < 15000
    assert samples[-1].median_ms >= 0


def test_folder_addition_and_removal_update_incrementally_without_reset():
    """TE-PERF-02/TE-INCR-01: a folder-sized batch never resets the model."""
    app()
    values = synthetic_tracks(10000)
    workspace = TagEditorWorkspaceState()
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks(values)

    folder = Path("C:/phase13-fixture/album-added")
    added = synthetic_tracks(100, root=Path("C:/phase13-fixture-added"), folders=1)
    for item in added:
        item.folder = folder
        item.path = folder / item.path.name

    before = counter_snapshot(model)
    model.add_tracks(added)
    after_add = counter_snapshot(model)
    assert after_add.resets == before.resets
    assert after_add.inserted_rows == before.inserted_rows + 100
    assert model.rowCount() == 10100

    model.remove_paths({item.path for item in added})
    after_remove = counter_snapshot(model)
    assert after_remove.resets == before.resets
    assert after_remove.removed_rows == before.removed_rows + 100
    assert model.rowCount() == 10000
    # Surviving identities are never renumbered by a neighbour's removal.
    assert workspace.item_id(values[0]) == 1
    assert workspace.item_id(values[-1]) == 10000


def test_one_file_incremental_update_is_far_cheaper_than_a_full_reset():
    """TE-PERF-04: the incremental path stays well under the reset it replaces."""
    app()
    values = synthetic_tracks(10000)
    workspace = TagEditorWorkspaceState()
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks(values)
    # An active sort is the expensive case: it used to re-key and re-sort every
    # row for a single-file refresh.
    model.sort(COL_FILENAME, Qt.AscendingOrder)

    reset = measure_operation(
        "full_reset_10k", 10000, lambda: model.load_tracks(values),
        samples=3, warmups=1)
    incremental = measure_operation(
        "one_file_refresh_10k", 10000,
        lambda: model.refresh_track(values[5000]), samples=3, warmups=1)

    resets_before = counter_snapshot(model).resets
    model.refresh_track(values[5000])
    assert counter_snapshot(model).resets == resets_before

    # Relative comparison with a 3x CI allowance over the documented 10%
    # reference ceiling; both operations are measured on the same machine.
    assert incremental.median_ms <= max(reset.median_ms * 0.30, 1.0)


def test_sorted_one_file_refresh_reuses_sort_keys_and_stays_flat():
    """TE-PERF-05: a single-row refresh must not re-key the whole library."""
    app()
    timings = {}
    for count in (1000, 10000):
        values = synthetic_tracks(count)
        workspace = TagEditorWorkspaceState()
        model = MetadataTableModel(workspace=workspace)
        model.load_tracks(values)
        model.sort(COL_FILENAME, Qt.AscendingOrder)
        timings[count] = measure_operation(
            f"sorted_refresh_{count}", count,
            lambda m=model, v=values: m.refresh_track(v[count // 2]),
            samples=5, warmups=1).median_ms
    # A full re-sort would grow with the library; a targeted move does not.
    # The 10x allowance keeps this stable on a noisy machine while still
    # failing an O(n log n) regression (which measured ~17x here).
    assert timings[10000] <= max(timings[1000] * 10, 1.0)


def test_sorted_refresh_moves_the_row_to_its_new_position():
    """The cheaper path must still order rows exactly as a full sort would."""
    app()
    values = synthetic_tracks(50)
    workspace = TagEditorWorkspaceState()
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks(values)
    model.sort(COL_FILENAME, Qt.AscendingOrder)
    assert model.track_at_row(0) is values[0]

    moved = values[0]
    old_path = moved.path
    moved.path = moved.path.with_name("zzz-last.mp3")
    assert model.refresh_path(old_path)
    assert model.track_at_row(model.rowCount() - 1) is moved

    old_path = moved.path
    moved.path = moved.path.with_name("aaa-first.mp3")
    assert model.refresh_path(old_path)
    assert model.track_at_row(0) is moved
    names = [model.track_at_row(row).path.name for row in range(model.rowCount())]
    assert names == sorted(names)


def test_100_event_burst_coalesces_to_one_bounded_batch(tmp_path):
    """TE-PERF-04: a burst stays bounded and produces one accepted batch."""
    root = tmp_path / "music"
    root.mkdir()
    session = WatchRootSession.create(root, 1, 1)
    coalescer = EventCoalescer()
    sequence = 0
    for index in range(100):
        path = root / f"track-{index:03d}.mp3"
        path.write_bytes(b"audio")
        # Each file reports create plus repeated modify, as a real backend does.
        for kind in (FilesystemEventKind.CREATED, FilesystemEventKind.MODIFIED,
                     FilesystemEventKind.MODIFIED):
            sequence += 1
            coalescer.add(normalize_filesystem_event(root, FilesystemEvent(
                session.session_id, session.generation, sequence, kind, path)))
    assert coalescer.pending_count == 100

    batch = coalescer.drain()
    assert len(batch.events) == 100
    assert not batch.overflowed
    assert all(event.kind is FilesystemEventKind.CREATED for event in batch.events)
    assert coalescer.drain() is None


def test_measurement_helpers_report_median_slowest_and_memory():
    sample = measure_operation(
        "fixture_1k", 1000, lambda: synthetic_tracks(1000),
        samples=2, warmups=1, measure_memory=True)
    assert len(sample.samples_ms) == 2
    assert 0 <= sample.median_ms <= sample.slowest_ms
    assert sample.peak_bytes > 0
    report = format_samples([sample])
    assert report.startswith("operation,item_count") and "fixture_1k,1000" in report
