"""
ui/controllers/metadata_controller.py  –  Tag Editor business logic controller
===============================================================================
QObject — zero widget imports.  Owns MetadataScanWorker and MetadataApplyWorker.
Manages TagEditSession state and exposes a clean public API for the panel.

AppWindow wires all signals in _connect_metadata_signals().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from core.change_sets import ChangeOrigin
from core.filesystem_monitoring import FilesystemEventKind
from core.metadata_lookup import LookupMode, LookupState
from core.metadata_models import AudioTrackItem, TagEditSession, TrackStatus
from core.tag_actions import LEGACY_ACTION_IDS, builtin_registry
from core.tag_action_presets import PresetStep
from core.tag_action_service import TagActionService
from core.metadata_validation import (
    DuplicateScanResult, MetadataValidationEngine, ProblemFixPreview, ValidationSnapshot,
)
from ui.i18n import t
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.services.file_operation_service import FileOperationError, FileOperationService

logger = logging.getLogger(__name__)

# Backup files land in the app-data tag_backups directory.
from utils.paths import get_tag_backup_dir

_BACKUP_DIR = get_tag_backup_dir()

# A physical operation may legitimately surface as any of these; identity
# evidence — not the kind — is what actually proves the event was ours.
_EXPECTED_EVENT_KINDS = frozenset({
    FilesystemEventKind.MODIFIED, FilesystemEventKind.CREATED,
    FilesystemEventKind.REMOVED, FilesystemEventKind.RELOCATED,
    FilesystemEventKind.DIRECTORY_CREATED, FilesystemEventKind.DIRECTORY_REMOVED,
})


@dataclass(frozen=True)
class FileOperationOutcome:
    """What one physical path operation actually did."""

    operation: str
    source: Path
    destination: Path | None = None
    succeeded: bool = False
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class FileOperationBatchResult:
    """The result of one controller-owned physical operation batch."""

    operation: str
    outcomes: tuple[FileOperationOutcome, ...] = ()

    @property
    def succeeded(self) -> tuple[FileOperationOutcome, ...]:
        return tuple(value for value in self.outcomes if value.succeeded)

    @property
    def failed(self) -> tuple[FileOperationOutcome, ...]:
        return tuple(value for value in self.outcomes if not value.succeeded)

    @property
    def all_succeeded(self) -> bool:
        return bool(self.outcomes) and not self.failed

    @property
    def error_messages(self) -> tuple[str, ...]:
        return tuple(value.error_message for value in self.failed if value.error_message)


class MetadataController(QObject):
    """
    Manages the tag-editor session lifecycle.

    Signals
    -------
    scan_started          — emitted when a new scan begins
    track_discovered      — one AudioTrackItem found during scan
    scan_complete         — ScanResult with totals
    auto_rules_applied    — proposed tags were bulk-computed
    apply_started         — apply worker launched
    apply_progress        — (done: int, total: int)
    apply_file_outcome    — per-file ApplyOutcome (structured, TE-SAFE-06)
    apply_batch_complete  — batch ApplyBatchResult (structured, TE-SAFE-06)
    apply_complete        — (success: int, fail: int, skip: int) [derived, compat]
    apply_error           — str error message (batch-level abort)
    recovery_available    — review-first recovery summary dict (TE-SAFE-11)
    shutdown_ready        — safe to close after an in-flight disk op finished
    status_update         — human-readable status string for UI
    """

    scan_started       = Signal()
    workspace_replacement_started = Signal(object)  # accepted root, after lifecycle guard
    track_discovered   = Signal(object)        # AudioTrackItem
    track_batch_discovered = Signal(object)    # list[AudioTrackItem]
    scan_progress      = Signal(int, int)      # done, total
    scan_failed        = Signal(str)
    scan_complete      = Signal(object)        # ScanResult
    auto_rules_applied = Signal()
    tags_modified      = Signal()              # any in-memory tag edit (triggers table refresh)
    apply_started      = Signal()
    apply_progress     = Signal(int, int)      # done, total
    apply_file_outcome = Signal(object)        # ApplyOutcome (TE-SAFE-06)
    apply_complete     = Signal(int, int, int) # success, fail, skip (derived, kept for compat)
    apply_batch_complete = Signal(object)      # ApplyBatchResult (TE-SAFE-06)
    apply_error        = Signal(str)
    restore_started    = Signal()
    restore_progress   = Signal(int, int)      # done, total
    restore_complete   = Signal(object)        # list[RestoreOutcome]
    recovery_available = Signal(object)        # recovery summary dict (TE-SAFE-11)
    recovery_complete  = Signal(object, bool)  # (outcomes, all_ok)
    shutdown_ready     = Signal()              # safe to close after a disk op finished
    shutdown_timed_out = Signal()              # bounded shutdown elapsed; keep app open
    status_update      = Signal(str)
    replaygain_analysis_started = Signal(str, int)  # mode, total files
    replaygain_analysis_progress = Signal(int, int)
    replaygain_proposal_ready = Signal(object, object)  # item, canonical values
    replaygain_analysis_complete = Signal(object)       # structured summary
    draft_available      = Signal(object)               # safe proposal-only recovery information
    unsaved_changes_action_required = Signal(object)    # lifecycle request with four safe choices
    action_preview_ready = Signal(object)               # immutable ActionPreview
    action_preview_accepted = Signal(object)            # immutable ActionPreview
    validation_updated = Signal(object)                  # immutable ValidationSnapshot
    problem_fix_preview_ready = Signal(object)
    problem_fix_preview_failed = Signal(str)
    online_lookup_started = Signal(object)
    online_lookup_finished = Signal(object)
    online_release_detail_finished = Signal(object)
    online_match_preview_ready = Signal(object)
    online_artwork_ready = Signal(object, object, object)
    online_artwork_error = Signal(str)
    online_acceptance_error = Signal(str)
    online_acceptance_complete = Signal(bool)
    metadata_io_started = Signal(object)
    metadata_io_finished = Signal(object, object)
    metadata_io_error = Signal(object, object)
    monitoring_state_changed = Signal(object, str)
    external_changes_updated = Signal(int)
    workspace_refresh_applied = Signal(object)
    conflict_resolution_finished = Signal(object)

    # Duplicate detector signals
    duplicate_scan_progress  = Signal(int, int, str)     # done, total, eta_str
    duplicate_scan_complete  = Signal(object, float, str) # groups dict, elapsed, strategy
    duplicate_scan_error     = Signal(str)
    duplicate_delete_complete = Signal(int, int)          # success, fail

    def __init__(self, config=None, parent: QObject = None) -> None:
        super().__init__(parent)
        self._cfg            = config
        self._scan_worker    = None
        self._apply_worker   = None
        self._restore_worker = None
        self._undo_worker = None
        self._dup_worker     = None
        self._online_worker = None
        self._online_release_detail_worker = None
        self._online_artwork_worker = None
        self._online_request = None
        self._online_provider = None
        self._online_selected_candidate = None
        self._online_artwork_request = None
        self._online_artwork_candidate = None
        self._online_pending_acceptance = None
        self._online_artwork_entry = None
        self._metadata_io_worker = None
        self._metadata_io_workers: set[object] = set()
        self._dup_request_id = 0
        self._dup_generation = 0
        self._session        = TagEditSession()
        self.workspace_state = TagEditorWorkspaceState(self)
        self.workspace_state.item_relocated.connect(self._on_history_item_relocated)
        from core.filesystem_monitoring import OwnOperationLedger
        from ui.services.filesystem_watch_service import FilesystemWatchService
        self._watch_service = FilesystemWatchService(self)
        self._watch_service.batch_ready.connect(self._on_filesystem_batch)
        self._watch_service.state_changed.connect(self.monitoring_state_changed)
        self._watch_session = None
        self._refresh_workers: set[object] = set()
        self._current_refresh_worker = None
        self._incremental_updater = None
        self._own_operations = OwnOperationLedger()
        self._active_monitored_operation_id: str | None = None
        self._tag_action_service = TagActionService(builtin_registry())
        self._validation_engine = MetadataValidationEngine()
        self._validation_snapshot = None
        # Bumped on every new scan / root / workspace change. Each apply/restore
        # worker captures the value; stale-generation finish signals are dropped
        # so a slow worker can never mutate a newer workspace (TE-SAFE-09).
        self._recovery_worker = None
        self._replaygain_worker = None
        self._op_generation  = 0
        self._shutting_down  = False
        self._shutdown_timer = None
        self._shutdown_connected_ids: set[int] = set()
        self._proposal_origin = ChangeOrigin.MANUAL
        self._action_capture_in_progress = False
        self.tags_modified.connect(self._capture_proposal_action)
        from core.change_drafts import resolve_draft_store
        import uuid
        # Canonical app-data location, adopting any pre-0.2.0 draft from the
        # legacy home-directory path on the way (F-13). The migration is
        # idempotent and reads nothing when there is nothing to adopt.
        self._draft_store, self._draft_migration = resolve_draft_store()
        self._pending_draft_snapshot = None
        self._draft_review_required = False
        self._pending_lifecycle_action = None
        self._lifecycle_bypass = False
        self._draft_session_id = uuid.uuid4().hex
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self._flush_draft)
        self.tags_modified.connect(self._schedule_draft_save)
        self.tags_modified.connect(self.revalidate_problems)

    # -- Phase 12 metadata IO -----------------------------------------------------

    def io_root(self) -> Path | None:
        return self._session.scan_result.root if self._session.scan_result else None

    def set_incremental_updater(self, updater) -> None:
        """Install the panel/model adapter before monitoring can publish plans."""
        self._incremental_updater = updater

    def _on_history_item_relocated(self, item, prior_path) -> None:
        """Keep the model's path index with an item Undo/Redo moved back."""
        updater = self._incremental_updater
        if updater is None or getattr(updater, "model", None) is None:
            return
        try:
            updater.model.reindex_track_path(item, Path(prior_path))
        except (AttributeError, RuntimeError, KeyError):
            logger.debug("[MetadataController] Could not reindex relocated item",
                         exc_info=True)

    @property
    def watch_session(self):
        return self._watch_session

    def manual_filesystem_refresh(self) -> None:
        self._watch_service.manual_refresh()

    def resolve_external_conflict(self, conflict, action, target_path=None):
        if self._incremental_updater is None or self._watch_session is None:
            return None
        from core.file_refresh_service import ConflictResolutionAction
        action = action if isinstance(action, ConflictResolutionAction) else ConflictResolutionAction(str(action))
        summary = self._incremental_updater.resolve_conflict(
            conflict, action, root=self._watch_session.root,
            session_id=self._watch_session.session_id,
            target_path=Path(target_path) if target_path else None)
        self.conflict_resolution_finished.emit(summary)
        self.external_changes_updated.emit(self._external_change_count())
        if summary.accepted:
            self.revalidate_problems()
        return summary

    def io_scope_ids(self, scope, *, ordered_item_ids=None) -> tuple[int, ...]:
        """Resolve an explicit scope once; selection never becomes Apply scope."""
        from core.metadata_io import IOScope, resolve_workspace_scope
        scope = scope if isinstance(scope, IOScope) else IOScope(str(scope))
        ordered_items = None
        if ordered_item_ids is not None:
            ordered_items = [self.workspace_state.track_for_id(int(identity))
                             for identity in ordered_item_ids]
            ordered_items = [item for item in ordered_items if item is not None]
        return resolve_workspace_scope(self.workspace_state, scope,
                                       ordered_items=ordered_items)

    def create_metadata_csv_export_plan(self, options: dict):
        from core.metadata_csv import (
            CANONICAL_FIELDS, CsvDelimiter, CsvDialectSpec, CsvEncoding,
            CsvEncodingSpec, build_metadata_export_plan,
        )
        from core.metadata_io import IOScope, MetadataValueSource
        root = self.io_root()
        if root is None:
            return None
        scope = IOScope(str(options.get("scope", IOScope.ALL_LOADED.value)))
        ids = self.io_scope_ids(scope, ordered_item_ids=options.get("ordered_item_ids"))
        return build_metadata_export_plan(
            self.workspace_state, root=root, item_ids=ids, scope=scope,
            value_source=MetadataValueSource(str(options.get("value_source", "effective"))),
            fields=options.get("fields") or CANONICAL_FIELDS,
            encoding=CsvEncodingSpec(CsvEncoding(str(options.get("encoding", "utf_8_bom")))),
            dialect=CsvDialectSpec(CsvDelimiter(str(options.get("delimiter", ",")))),
            include_absolute_paths=bool(options.get("include_absolute_paths", False)),
        )

    def start_metadata_csv_export(self, plan, destination: Path, *, overwrite: bool = False):
        from core.metadata_csv import export_metadata_csv
        return self._start_metadata_io_worker(
            plan.identity,
            lambda cancellation: export_metadata_csv(
                plan, Path(destination), overwrite=overwrite, cancellation=cancellation),
        )

    def create_metadata_csv_import_preview(self, options: dict, *, cancellation=None):
        from core.metadata_csv import (
            BlankValuePolicy, CsvDelimiter, CsvEncoding, CsvIdentityMapping,
            app_generated_mapping, build_metadata_import_preview, parse_csv_file,
        )
        from core.metadata_io import IOScope
        root = self.io_root()
        if root is None:
            return None
        encoding_value = options.get("encoding")
        delimiter_value = options.get("delimiter")
        parsed = parse_csv_file(
            Path(options["path"]),
            encoding=CsvEncoding(str(encoding_value)) if encoding_value else None,
            delimiter=CsvDelimiter(str(delimiter_value)) if delimiter_value else None,
            cancellation=cancellation,
        )
        mapping = options.get("mapping")
        identity = options.get("identity_mapping")
        if mapping is None or identity is None:
            automatic_mapping, automatic_identity = app_generated_mapping(parsed.headers)
            mapping = automatic_mapping if mapping is None else mapping
            identity = automatic_identity if identity is None else identity
        if not isinstance(identity, CsvIdentityMapping):
            identity = CsvIdentityMapping(*identity)
        scope = IOScope(str(options.get("scope", IOScope.ALL_LOADED.value)))
        ids = self.io_scope_ids(scope, ordered_item_ids=options.get("ordered_item_ids"))
        return build_metadata_import_preview(
            self.workspace_state, parsed, root=root, item_ids=ids, scope=scope,
            mapping=mapping, identity_mapping=identity,
            blank_policy=BlankValuePolicy(str(options.get("blank_policy", "no_change"))),
            cancellation=cancellation,
        )

    def start_metadata_csv_import_preview(self, options: dict):
        from core.metadata_csv import (
            BlankValuePolicy, CsvIdentityMapping, import_mapping_identity,
        )
        from core.metadata_io import IORequestIdentity, IOScope, MetadataIOError, IOErrorInfo, IOErrorKind
        scope = IOScope(str(options.get("scope", IOScope.ALL_LOADED.value)))
        ids = self.io_scope_ids(scope, ordered_item_ids=options.get("ordered_item_ids"))
        mapping = options.get("mapping")
        identity_mapping = options.get("identity_mapping")
        source_identity = options.get("source_identity")
        if mapping is None or identity_mapping is None or source_identity is None:
            raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_MAPPING))
        if not isinstance(identity_mapping, CsvIdentityMapping):
            identity_mapping = CsvIdentityMapping(*identity_mapping)
        mapping_hash = import_mapping_identity(
            mapping, identity_mapping,
            BlankValuePolicy(str(options.get("blank_policy", "no_change"))),
        )
        identity = IORequestIdentity.create(
            "metadata_csv_import_preview", self.workspace_state.generation,
            self.workspace_state.change_set.revision, ids,
            source_identity=source_identity, mapping_identity=mapping_hash,
            content_revision=self.workspace_state.content_revision,
        )
        frozen_options = dict(options)
        def operation(cancellation):
            cancellation.raise_if_cancelled()
            preview = self.create_metadata_csv_import_preview(
                frozen_options, cancellation=cancellation)
            cancellation.raise_if_cancelled()
            if not identity.current_for(self.workspace_state, exact_ids=ids):
                raise MetadataIOError(IOErrorInfo(IOErrorKind.STALE_PREVIEW))
            if preview.source != source_identity:
                raise MetadataIOError(IOErrorInfo(IOErrorKind.SOURCE_CHANGED))
            if preview.mapping_identity != mapping_hash:
                raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_MAPPING))
            return preview
        return self._start_metadata_io_worker(
            identity, operation,
        )

    def start_metadata_csv_header_preview(self, options: dict):
        from core.metadata_csv import CsvDelimiter, CsvEncoding, parse_csv_file
        from core.metadata_io import IORequestIdentity
        identity = IORequestIdentity.create(
            "metadata_csv_header_preview", self.workspace_state.generation,
            self.workspace_state.change_set.revision, (),
            content_revision=self.workspace_state.content_revision,
        )
        path = Path(options["path"])
        encoding = options.get("encoding")
        delimiter = options.get("delimiter")
        return self._start_metadata_io_worker(
            identity,
            lambda cancellation: parse_csv_file(
                path,
                encoding=CsvEncoding(str(encoding)) if encoding else None,
                delimiter=CsvDelimiter(str(delimiter)) if delimiter else None,
                cancellation=cancellation,
            ),
        )

    def accept_metadata_csv_import(self, preview, selected_cell_ids) -> object:
        from core.metadata_csv import accept_import_preview
        result = accept_import_preview(self.workspace_state, preview, selected_cell_ids)
        if result.accepted:
            # Core already captured exactly one IMPORT ChangeCommand. Notify
            # draft/Problems/UI listeners without a second generic capture.
            self._action_capture_in_progress = True
            try:
                self.tags_modified.emit()
            finally:
                self._action_capture_in_progress = False
        return result

    def create_change_report_snapshot(self, scope, *, ordered_item_ids=None,
                                      include_absolute_paths: bool = False):
        from core.metadata_io import IOScope
        from core.metadata_reports import build_change_report_snapshot
        root = self.io_root()
        if root is None:
            return None
        scope = scope if isinstance(scope, IOScope) else IOScope(str(scope))
        ids = self.io_scope_ids(scope, ordered_item_ids=ordered_item_ids)
        return build_change_report_snapshot(self.workspace_state, root=root,
                                            item_ids=ids, scope=scope,
                                            include_absolute_paths=include_absolute_paths)

    def create_problems_report_snapshot(self, *, issue_ids=None,
                                        include_absolute_paths: bool = False):
        from core.metadata_reports import build_problems_report_snapshot
        root = self.io_root()
        if root is None:
            return None
        validation = self.revalidate_problems()
        return build_problems_report_snapshot(
            self.workspace_state, validation, root=root, issue_ids=issue_ids,
            scope_label_key=("meta_report_scope_filtered_issues" if issue_ids is not None
                             else "meta_report_scope_all_issues"),
            include_absolute_paths=include_absolute_paths,
        )

    def start_report_export(self, snapshot, destination: Path, *, report_type: str,
                            format: str, language: str = "en",
                            include_technical_ids: bool = False,
                            spreadsheet_safe: bool = False,
                            overwrite: bool = False):
        from core.metadata_io import IORequestIdentity
        from core.metadata_reports import (
            export_report, render_change_report_csv, render_change_report_html,
            render_problems_report_csv, render_problems_report_html,
        )
        identity = IORequestIdentity.create(
            f"{report_type}_report_export", snapshot.generation, snapshot.revision, (),
            content_revision=snapshot.content_revision,
        )

        def operation(cancellation):
            if report_type == "change":
                data = (render_change_report_html(snapshot, t, language=language)
                        if format == "html" else
                        render_change_report_csv(snapshot, t,
                            include_technical_ids=include_technical_ids,
                            spreadsheet_safe=spreadsheet_safe))
            else:
                data = (render_problems_report_html(snapshot, t, language=language)
                        if format == "html" else
                        render_problems_report_csv(snapshot, t,
                            include_technical_ids=include_technical_ids,
                            spreadsheet_safe=spreadsheet_safe))
            return export_report(Path(destination), data, overwrite=overwrite,
                                 cancellation=cancellation, html_report=format == "html")

        return self._start_metadata_io_worker(identity, operation)

    def create_playlist_export_plan(self, options: dict):
        from core.metadata_io import IOScope, MetadataValueSource
        from core.playlist_export import (
            PlaylistFormat, PlaylistOrder, PlaylistPathMode, build_playlist_plan,
        )
        scope = IOScope(str(options.get("scope", IOScope.ALL_LOADED.value)))
        ids = self.io_scope_ids(scope, ordered_item_ids=options.get("ordered_item_ids"))
        return build_playlist_plan(
            self.workspace_state, item_ids=ids, scope=scope,
            order=PlaylistOrder(str(options.get("order", "current_view"))),
            path_mode=PlaylistPathMode(str(options.get("path_mode", "auto"))),
            value_source=MetadataValueSource(str(options.get("value_source", "effective"))),
            format=PlaylistFormat(str(options.get("format", "m3u8"))),
        )

    def start_playlist_export(self, plan, destination: Path, *, overwrite: bool = False):
        from core.playlist_export import export_playlist
        return self._start_metadata_io_worker(
            plan.identity,
            lambda cancellation: export_playlist(
                plan, Path(destination), overwrite=overwrite, cancellation=cancellation),
        )

    def _start_metadata_io_worker(self, request_identity, operation):
        from ui.workers.metadata_io_worker import MetadataIOWorker
        if self._metadata_io_worker is not None and self._metadata_io_worker.isRunning():
            self._metadata_io_worker.cancel()
        worker = MetadataIOWorker(request_identity, operation, self)
        self._metadata_io_workers.add(worker)
        self._metadata_io_worker = worker
        worker.result_ready.connect(self._on_metadata_io_result)
        worker.error.connect(self._on_metadata_io_error)
        worker.finished.connect(lambda: self._retire_metadata_io_worker(worker))
        self.metadata_io_started.emit(request_identity)
        worker.start()
        return worker

    def _on_metadata_io_result(self, identity, result) -> None:
        worker = self.sender()
        if (worker is self._metadata_io_worker
                and getattr(worker, "request_identity", None) == identity
                and identity.current_for(self.workspace_state)):
            self.metadata_io_finished.emit(identity, result)

    def _on_metadata_io_error(self, identity, error) -> None:
        worker = self.sender()
        if (worker is self._metadata_io_worker
                and getattr(worker, "request_identity", None) == identity
                and identity.current_for(self.workspace_state)):
            self.metadata_io_error.emit(identity, error)

    def _retire_metadata_io_worker(self, worker) -> None:
        self._metadata_io_workers.discard(worker)
        if self._metadata_io_worker is worker:
            self._metadata_io_worker = None

    def cancel_metadata_io(self) -> None:
        if self._metadata_io_worker is not None and self._metadata_io_worker.isRunning():
            self._metadata_io_worker.cancel()

    def _invalidate_metadata_io_workers(self) -> None:
        """Cancel and retain every live IO worker until its own finished signal."""
        self._metadata_io_worker = None
        for worker in tuple(self._metadata_io_workers):
            try:
                if worker.isRunning():
                    worker.cancel()
            except (AttributeError, RuntimeError):
                pass

    # -- Phase 11 explicit online metadata ----------------------------------------

    def start_online_lookup(self, parameters: dict) -> object | None:
        """Start a user-requested lookup; this is the only Tag Editor network entry."""
        from uuid import uuid4
        from core.metadata_lookup import LocalTrackSnapshot, LookupMode, LookupRequest
        from core.metadata_backend import FORMAT_CAPABILITIES
        from core.providers.musicbrainz_provider import MusicBrainzProvider
        from ui.workers.metadata_lookup_worker import MetadataLookupWorker

        raw_ids = parameters.get("item_ids") or ()
        item_ids = tuple(sorted({int(value) for value in raw_ids}))
        if not item_ids:
            return None
        tracks = []
        snapshots = []
        for identity in item_ids:
            item = self.workspace_state.track_for_id(identity)
            if item is None:
                return None
            tracks.append(item)
            tags = item.proposed.effective_tags(item.original)
            duration = tags.file_properties.get("duration_seconds")
            try:
                duration_ms = int(float(duration) * 1000) if duration is not None else None
            except (TypeError, ValueError):
                duration_ms = None
            snapshots.append(LocalTrackSnapshot(
                identity, title=tags.title, artist=str(tags.artist), album=tags.album,
                album_artist=str(tags.album_artist), track_num=tags.track_num,
                track_total=tags.track_total, disc_num=tags.disc_num,
                disc_total=tags.disc_total, date=tags.year, genre=str(tags.genre),
                isrc=tags.isrc, publisher=tags.publisher, duration_ms=duration_ms,
                format_id=item.format_id, metadata_editable=item.metadata_editable,
                editable_fields=FORMAT_CAPABILITIES.by_id(item.format_id).editable_fields,
            ))
        request = LookupRequest(
            request_id=uuid4().hex, workspace_generation=self.workspace_state.generation,
            change_revision=self.workspace_state.change_set.revision, item_ids=item_ids,
            provider_id="musicbrainz",
            mode=LookupMode.ALBUM if parameters.get("mode") == "album" else LookupMode.TRACK,
            title=str(parameters.get("title") or "").strip(),
            artist=str(parameters.get("artist") or "").strip(),
            album=str(parameters.get("album") or "").strip(),
            local_tracks=tuple(snapshots),
        )
        self.cancel_online_lookup()
        self._online_request = request
        self._online_selected_candidate = None
        self._online_artwork_request = None
        self._online_artwork_candidate = None
        self._online_artwork_entry = None
        self._online_provider = MusicBrainzProvider()
        worker = MetadataLookupWorker(self._online_provider, request, self)
        self._online_worker = worker
        worker.result_ready.connect(self._on_online_lookup_result)
        worker.finished.connect(lambda: self._retire_online_worker(worker))
        self.online_lookup_started.emit(request)
        worker.start()
        return request

    def cancel_online_lookup(self) -> None:
        for worker in (self._online_worker, self._online_release_detail_worker, self._online_artwork_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
        self._online_artwork_request = None
        self._online_artwork_candidate = None
        self._online_artwork_entry = None
        self._online_pending_acceptance = None

    def _retire_online_worker(self, worker) -> None:
        if self._online_worker is worker:
            self._online_worker = None
        if self._online_release_detail_worker is worker:
            self._online_release_detail_worker = None
        if self._online_artwork_worker is worker:
            self._online_artwork_worker = None

    def _online_request_current(self, request) -> bool:
        current = self._online_request
        if current is None or current.request_id != request.request_id:
            return False
        if (self.workspace_state.generation != request.workspace_generation
                or self.workspace_state.change_set.revision != request.change_revision):
            return False
        selected = tuple(sorted(self.workspace_state.item_id(item)
                                for item in self.workspace_state.selected_tracks()))
        return selected == request.item_ids

    def _on_online_lookup_result(self, result) -> None:
        if not self._online_request_current(result.request):
            return
        if result.candidates:
            from dataclasses import replace
            from core.metadata_match_service import MetadataMatchService
            result = replace(result, candidates=MetadataMatchService().rank(result.request, result.candidates))
        self.online_lookup_finished.emit(result)

    def preview_online_candidate(self, candidate) -> object | None:
        request = self._online_request
        if request is None or not self._online_request_current(request):
            return None
        self._invalidate_online_artwork()
        self._online_selected_candidate = candidate
        if request.mode is LookupMode.ALBUM and not candidate.tracks:
            from uuid import uuid4
            from core.metadata_lookup import ReleaseDetailRequest
            from ui.workers.metadata_lookup_worker import ReleaseDetailWorker
            if self._online_release_detail_worker and self._online_release_detail_worker.isRunning():
                self._online_release_detail_worker.cancel()
            detail_request = ReleaseDetailRequest(uuid4().hex, request, candidate.candidate_id, candidate.release_id)
            worker = ReleaseDetailWorker(self._online_provider, detail_request, self)
            self._online_release_detail_worker = worker
            worker.result_ready.connect(self._on_release_detail_result)
            worker.finished.connect(lambda: self._retire_online_worker(worker))
            self.online_release_detail_finished.emit(None)  # localized loading state
            worker.start()
            return None
        from core.metadata_match_service import MetadataMatchService
        preview = MetadataMatchService().preview(request, candidate)
        self.online_match_preview_ready.emit(preview)
        return preview

    def _on_release_detail_result(self, result) -> None:
        worker = self.sender()
        request = result.request
        selected = self._online_selected_candidate
        if (worker is not self._online_release_detail_worker
                or not self._online_request_current(request.parent_request)
                or selected is None or selected.candidate_id != request.candidate_id
                or selected.release_id != request.release_id):
            return
        self.online_release_detail_finished.emit(result)
        if result.state is not LookupState.READY or result.candidate is None:
            return
        self._online_selected_candidate = result.candidate
        from core.metadata_match_service import MetadataMatchService
        preview = MetadataMatchService().preview(request.parent_request, result.candidate)
        self.online_match_preview_ready.emit(preview)

    def _invalidate_online_artwork(self) -> None:
        if self._online_artwork_worker and self._online_artwork_worker.isRunning():
            self._online_artwork_worker.cancel()
        self._online_artwork_request = None
        self._online_artwork_candidate = None
        self._online_artwork_entry = None
        self._online_pending_acceptance = None

    def preview_online_artwork(self, candidate) -> None:
        request = self._online_request
        if request is None or not self._online_request_current(request) or not candidate.release_id:
            return
        if not any(track.supports("artwork") for track in request.local_tracks):
            self.online_artwork_error.emit("meta_online_artwork_unsupported"); return
        from uuid import uuid4
        from core.metadata_lookup import ArtworkRequest
        from core.providers.cover_art_archive_provider import CoverArtArchiveProvider
        from ui.workers.metadata_lookup_worker import ArtworkLookupWorker
        if (self._online_selected_candidate is None
                or self._online_selected_candidate.candidate_id != candidate.candidate_id):
            return
        self._invalidate_online_artwork()
        artwork_request = ArtworkRequest(uuid4().hex, request.request_id,
            request.workspace_generation, request.change_revision, request.item_ids,
            candidate.candidate_id, candidate.release_id)
        self._online_artwork_request = artwork_request
        worker = ArtworkLookupWorker(CoverArtArchiveProvider(), artwork_request, parent=self)
        self._online_artwork_worker = worker
        worker.result_ready.connect(self._on_online_artwork_result)
        worker.finished.connect(lambda: self._retire_online_worker(worker))
        worker.start()

    def _artwork_result_current(self, worker, result) -> bool:
        request = self._online_request
        artwork = self._online_artwork_request
        selected = self._online_selected_candidate
        identity = result.request
        return bool(worker is self._online_artwork_worker and request and artwork and selected
            and self._online_request_current(request)
            and identity == artwork and identity.parent_request_id == request.request_id
            and identity.workspace_generation == self.workspace_state.generation
            and identity.change_revision == self.workspace_state.change_set.revision
            and identity.item_ids == request.item_ids
            and identity.candidate_id == selected.candidate_id
            and identity.release_id == selected.release_id)

    def _on_online_artwork_result(self, result) -> None:
        worker = self.sender()
        if not self._artwork_result_current(worker, result):
            return
        if result.state is not LookupState.READY:
            self._online_artwork_candidate = None; self._online_artwork_entry = None
            self.online_artwork_error.emit(result.error.message_key if result.error else "meta_online_artwork_unavailable")
            return
        entry = None
        if result.state is LookupState.READY and result.data:
            try:
                from core.artwork import validate_artwork_bytes
                entry = validate_artwork_bytes(result.data, description="Cover Art Archive")
            except Exception:
                self._online_artwork_candidate = None; self._online_artwork_entry = None
                self.online_artwork_error.emit("meta_online_artwork_invalid"); return
        self._online_artwork_entry = entry
        self._online_artwork_candidate = result.selected if entry is not None else None
        self.online_artwork_ready.emit(result.candidates, result.selected, entry)

    def accept_online_match(self, payload: object) -> bool:
        preview, selection = payload
        request = self._online_request
        if (request is None or not self._online_request_current(request)
                or self._online_selected_candidate is None
                or preview.candidate.candidate_id != self._online_selected_candidate.candidate_id
                or preview.candidate.release_id != self._online_selected_candidate.release_id):
            self.online_acceptance_complete.emit(False); return False
        if selection.artwork_item_ids:
            art_request = self._online_artwork_request
            art_candidate = self._online_artwork_candidate
            if (art_request is None or art_candidate is None
                    or art_request.candidate_id != preview.candidate.candidate_id
                    or art_request.release_id != preview.candidate.release_id):
                self.online_acceptance_complete.emit(False); return False
            from ui.workers.metadata_lookup_worker import ArtworkLookupWorker
            from core.providers.cover_art_archive_provider import CoverArtArchiveProvider
            self._online_pending_acceptance = (preview, selection)
            worker = ArtworkLookupWorker(CoverArtArchiveProvider(), art_request,
                                         operation="final", selected=art_candidate, parent=self)
            self._online_artwork_worker = worker
            worker.result_ready.connect(self._on_online_final_artwork_result)
            worker.finished.connect(lambda: self._retire_online_worker(worker))
            self.status_update.emit(t("meta_online_artwork_final_loading"))
            worker.start()
            return True
        return self._finish_online_acceptance(preview, selection, None)

    def _on_online_final_artwork_result(self, result) -> None:
        worker = self.sender(); pending = self._online_pending_acceptance
        if pending is None or not self._artwork_result_current(worker, result):
            return
        self._online_pending_acceptance = None
        if result.state is not LookupState.READY or not result.data:
            key = result.error.message_key if result.error else "meta_online_artwork_unavailable"
            self.status_update.emit(t(key)); self.online_acceptance_error.emit(key); return
        try:
            from dataclasses import replace
            from core.artwork import validate_artwork_bytes
            entry = validate_artwork_bytes(result.data, description="Cover Art Archive")
            entry = replace(entry, source_id=result.selected.source_url if result.selected else "")
        except Exception:
            self.status_update.emit(t("meta_online_artwork_invalid"))
            self.online_acceptance_error.emit("meta_online_artwork_invalid"); return
        self._finish_online_acceptance(*pending, entry)

    def _finish_online_acceptance(self, preview, selection, artwork_entry) -> bool:
        from core.metadata_match_service import MetadataMatchService
        accepted = MetadataMatchService().accept(
            self.workspace_state, preview, selection,
            artwork_entry=artwork_entry,
        )
        if accepted:
            self._action_capture_in_progress = True
            try:
                self.tags_modified.emit()
            finally:
                self._action_capture_in_progress = False
        self.online_acceptance_complete.emit(accepted)
        return accepted

    # ── Phase 9 declarative actions ───────────────────────────────────────────

    def preview_tag_action(self, action_id: str, parameters: dict | None = None, *, item_ids=None):
        """Evaluate a Phase 9 action with no proposal, media, or path mutation."""
        try:
            preview = self._tag_action_service.preview(
                self.workspace_state, action_id, item_ids=item_ids, parameters=parameters,
            )
        except (KeyError, ValueError) as exc:
            self.status_update.emit(str(exc))
            return None
        self.action_preview_ready.emit(preview)
        return preview

    def accept_tag_action_preview(self, preview) -> bool:
        """Commit one fresh preview as a canonical, undoable Phase 8 proposal action."""
        accepted = self._tag_action_service.accept(self.workspace_state, preview)
        if accepted:
            self.action_preview_accepted.emit(preview)
            # The service already captured one origin-correct ChangeCommand.
            # Notify UI/draft listeners without recapturing it as a second
            # generic manual/template command.
            self._action_capture_in_progress = True
            try:
                self.tags_modified.emit()
            finally:
                self._action_capture_in_progress = False
        return accepted

    # -- Phase 10 Problems center ---------------------------------------------------

    @property
    def validation_snapshot(self):
        return self._validation_snapshot

    def revalidate_problems(self):
        """Recompute transient issue evidence without changing proposals or media."""
        try:
            snapshot = self._validation_engine.validate(self.workspace_state)
        except Exception:
            # Validation is advisory: malformed in-memory data must leave a
            # visible error state, not crash the inspector.
            snapshot = ValidationSnapshot(self.workspace_state.generation,
                                          self.workspace_state.change_set.revision,
                                          (), error="validation_failed",
                                          content_revision=self.workspace_state.content_revision)
        self._validation_snapshot = snapshot
        self.validation_updated.emit(snapshot)
        return snapshot

    def preview_problem_fix(self, issue_ids, value: object):
        """Build an explicit-value fix preview.  This method is intentionally pure."""
        snapshot = self._validation_snapshot or self.revalidate_problems()
        if not snapshot.current_for(self.workspace_state):
            snapshot = self.revalidate_problems()
        wanted = {str(issue_id) for issue_id in issue_ids}
        selected = [issue for issue in snapshot.issues if issue.id in wanted]
        if not selected or not isinstance(value, str) or not value.strip():
            return None
        descriptors = {issue.fix for issue in selected if issue.fix}
        fields = {issue.fix.fields[0] for issue in selected
                  if issue.fix and issue.fix.requires_value and len(issue.fix.fields) == 1}
        if len(fields) != 1 or len(descriptors) != 1:
            return None
        field = fields.pop()
        target_ids = tuple(sorted({identity for issue in selected for identity in issue.item_ids}))
        applicable, skipped = [], []
        for identity in target_ids:
            item = self.workspace_state.track_for_id(identity)
            if item is None or not item.metadata_editable:
                skipped.append(identity)
            else:
                applicable.append(identity)
        if not applicable:
            return None
        descriptor = next(iter(descriptors))
        action_preview = self._tag_action_service.preview(
            self.workspace_state, descriptor.action_id, item_ids=applicable,
            parameters={"field": field, "value": value.strip()}, scope="explicit",
        )
        return ProblemFixPreview(snapshot.generation, snapshot.revision, tuple(sorted(wanted)),
                                 tuple(applicable), action_preview, field, value.strip(), tuple(skipped),
                                 self.workspace_state.content_revision)

    def accept_problem_fix(self, preview: ProblemFixPreview) -> bool:
        """Add one previewed explicit fix to the canonical Change Set as one Undo command."""
        if (preview.generation != self.workspace_state.generation
                or preview.revision != self.workspace_state.change_set.revision
                or (preview.content_revision
                    and preview.content_revision != self.workspace_state.content_revision)
                or not preview.item_ids or preview.field not in {"title", "artist"}):
            return False
        return self.accept_tag_action_preview(preview.action_preview)

    def apply_problem_fix(self, issue_ids, value: object) -> bool:
        preview = self.preview_problem_fix(issue_ids, value)
        return bool(preview and self.accept_problem_fix(preview))

    def create_problem_fix_preview(self, issue_ids, value: object) -> None:
        preview = self.preview_problem_fix(issue_ids, value)
        if preview is None:
            self.problem_fix_preview_failed.emit(t("meta_problems_no_safe_fix"))
        else:
            self.problem_fix_preview_ready.emit(preview)

    def _execute_registered_action(self, action_id: str, tracks: list[AudioTrackItem],
                                   parameters: dict | None = None):
        """Run a legacy entry point through the single Phase 9 evaluator."""
        if not tracks:
            return None
        try:
            ids = [self.workspace_state.item_id(item) for item in tracks]
            workspace = self.workspace_state
        except KeyError:
            # Backward-compatible detached callers still use the registry's
            # transformation logic, but never acquire IDs in the live session.
            workspace = TagEditorWorkspaceState()
            workspace.set_tracks(list(tracks))
            ids = [workspace.item_id(item) for item in tracks]
        preview = self._tag_action_service.preview(
            workspace, action_id, item_ids=ids, parameters=parameters,
        )
        if workspace is self.workspace_state:
            self.accept_tag_action_preview(preview)
        elif self._tag_action_service.accept(workspace, preview):
            self.tags_modified.emit()
        return preview

    def apply_auto_sequence(self, tracks: list[AudioTrackItem], legacy_ops: list[str]) -> None:
        """Run Auto Arrange and configured actions as one preview/Undo command."""
        if not tracks:
            return
        steps = [PresetStep("tag.auto_arrange.v1")]
        for legacy_id in legacy_ops:
            action_id = LEGACY_ACTION_IDS.get(legacy_id)
            if not action_id:
                continue
            parameters = ({"strip_numbering": False} if legacy_id == "title_full"
                          else {"strip_numbering": True} if legacy_id == "title_strip"
                          else {"field": "title"} if legacy_id == "normalize_spaces" else {})
            steps.append(PresetStep(action_id, parameters))
        ids = [self.workspace_state.item_id(item) for item in tracks]
        preview = self._tag_action_service.preview_sequence(
            self.workspace_state, steps, item_ids=ids,
        )
        accepted = self.accept_tag_action_preview(preview)
        self.auto_rules_applied.emit()
        changed = preview.changed_count if accepted else 0
        self.status_update.emit(t("md_auto_changes_proposed", n=changed) if changed else t("md_auto_no_changes"))

    # ── Operation coordination (TE-SAFE-09/13) ──────────────────────────────────

    @property
    def op_generation(self) -> int:
        return self._op_generation

    def _bump_generation(self) -> None:
        self._op_generation += 1

    def edit_scope(self) -> list[AudioTrackItem]:
        """Editing helper: selected rows only; visibility is not action scope."""
        return self.workspace_state.edit_scope()

    def _capture_proposal_action(self) -> None:
        """Mirror every completed production proposal action into ChangeSet."""
        if self._action_capture_in_progress:
            return
        self.workspace_state.capture_proposals(
            self.workspace_state.tracks, self._proposal_origin,
        )
        self._proposal_origin = ChangeOrigin.MANUAL

    def _schedule_draft_save(self) -> None:
        if self.workspace_state.change_set.records():
            self._draft_timer.start()
        else:
            self._draft_timer.stop()
            try:
                self._draft_store.discard()
            except Exception as exc:
                logger.warning("[MetadataController] Could not discard draft: %s", exc)

    def _flush_draft(self) -> bool:
        if not self.workspace_state.change_set.records():
            return True
        root = self._session.scan_result.root if self._session.scan_result else None
        try:
            from dataclasses import asdict
            from core.change_sets import capture_file_identity
            def target_identity(path):
                value = capture_file_identity(path)
                return asdict(value) if value is not None else None
            targets = {
                self.workspace_state.item_id(item): {
                    "path": str(item.path), "identity": target_identity(item.path),
                }
                for item in self.workspace_state.tracks
                if self.workspace_state.change_set.records(
                    item_ids={self.workspace_state.item_id(item)})
            }
            self._draft_store.save(self.workspace_state.change_set.snapshot(self.workspace_state.generation),
                                   root=root, session_id=self._draft_session_id, targets=targets)
            return True
        except Exception as exc:
            logger.warning("[MetadataController] Could not save proposal draft: %s", exc)
            self.status_update.emit(t("meta_draft_unavailable"))
            return False

    def draft_path(self) -> Path:
        """Where pending drafts are read from and written to, for this process."""
        return self._draft_store.path

    def peek_draft(self) -> bool:
        """Report whether a pending draft exists, without offering to restore it."""
        return self._draft_store.path.exists()

    def check_for_draft(self):
        """Read a prior draft without applying it or touching media files."""
        if not self._draft_store.path.exists():
            return None
        try:
            metadata, snapshot = self._draft_store.load()
        except Exception as exc:
            logger.warning("[MetadataController] Invalid proposal draft: %s", exc)
            return None
        from core.change_sets import FileIdentity, file_identity_status
        target_states = {}
        for key, target in metadata.get("targets", {}).items():
            path = Path(str(target.get("path") or ""))
            identity = target.get("identity")
            expected = FileIdentity(**identity) if isinstance(identity, dict) else None
            target_states[int(key)] = file_identity_status(expected, path)
        info = {"root": metadata.get("root"), "created": metadata.get("created"),
                "affected_files": len({record.item_id for record in snapshot.records}),
                "snapshot": snapshot, "targets": metadata.get("targets", {}),
                "target_states": target_states,
                "missing": sum(value == "missing" for value in target_states.values()),
                "stale": sum(value not in {"current", "unavailable", "missing"}
                             for value in target_states.values())}
        # A preserved conflicting draft from the legacy location is the user's
        # work too. We will not merge or discard it, so the recovery prompt is
        # where they get told it exists and where it was kept (F-13).
        if self._draft_migration.needs_user_attention:
            info["migration_notice"] = self._draft_migration.outcome.value
            if self._draft_migration.preserved_copy is not None:
                info["migration_preserved_copy"] = str(self._draft_migration.preserved_copy)
        self.draft_available.emit(info)
        return info

    def restore_draft(self, snapshot) -> bool:
        """Restore proposals into the current compatible workspace only."""
        restored = self.workspace_state.restore_draft_snapshot(snapshot)
        if restored:
            self._draft_review_required = True
            self.tags_modified.emit()
        return restored

    def restore_draft_from_info(self, info: dict) -> bool:
        """Rescan the draft root, then restore proposals only into that workspace."""
        if self.is_disk_op_active():
            self.status_update.emit(t("md_busy_disk_op"))
            return False
        root = info.get("root")
        snapshot = info.get("snapshot")
        if not root or snapshot is None or not Path(root).is_dir():
            self.status_update.emit(t("meta_draft_unavailable"))
            return False
        self._pending_draft_snapshot = info
        self.scan(Path(root), recursive=True)
        return True

    def discard_draft(self) -> None:
        self._draft_timer.stop()
        self._draft_store.discard()

    def request_lifecycle_action(self, operation: str, continuation) -> bool:
        """Return True if an incompatible action may proceed immediately."""
        if not self.workspace_state.change_set.records():
            return True
        if self._pending_lifecycle_action is None:
            self._pending_lifecycle_action = {
                "operation": operation, "continuation": continuation,
                "affected_files": len({record.item_id for record in self.workspace_state.change_set.records()}),
            }
        self.unsaved_changes_action_required.emit(dict(self._pending_lifecycle_action, continuation=None))
        return False

    def guard_lifecycle_action(self, operation: str, continuation) -> bool:
        bypass, self._lifecycle_bypass = self._lifecycle_bypass, False
        return True if bypass else self.request_lifecycle_action(operation, continuation)

    def _begin_monitored_disk_operation(self, operation_id: str, operation_type: str,
                                        items, expected_final=None) -> None:
        """Register evidence before a worker can emit filesystem changes."""
        from core.change_sets import capture_file_identity
        self._active_monitored_operation_id = operation_id
        for value in items:
            path = Path(value.path if hasattr(value, "path") else value)
            final_path = Path(expected_final(value)) if expected_final else path
            self._own_operations.begin(
                operation_id, operation_type, path, final_path,
                original_identity=capture_file_identity(path),
                expected_kinds=_EXPECTED_EVENT_KINDS)
        self._watch_service.set_paused(True)

    def _record_operation_terminal_state(self, operation_id: str, records) -> None:
        """Prove what each path actually became, from the canonical outcome.

        Suppression is only safe with real evidence, so every entry states the
        identity it truly reached — or its proven absence.  A path that failed,
        was cancelled, or cannot be restated presents no evidence at all and is
        therefore reconciled as external rather than silently ignored.
        """
        from core.change_sets import capture_file_identity
        for original_path, final_path, succeeded, expected_absence in records:
            final = Path(final_path) if final_path is not None else Path(original_path)
            identity = None
            absent = bool(expected_absence)
            if succeeded and not absent:
                identity = capture_file_identity(final)
                if identity is None:
                    # The operation reported success but the file is gone: that
                    # is not evidence we can suppress on.
                    absent, identity = False, None
            elif succeeded and absent:
                absent = not final.exists()
            self._own_operations.record_final_state(
                operation_id, Path(original_path),
                terminal_result="success" if succeeded else "failed",
                final_path=final, final_identity=identity,
                expected_absence=absent and succeeded,
            )

    def _finish_monitored_disk_operation(self, operation_id: str,
                                         terminal_result: str, *, records=None) -> None:
        if records:
            self._record_operation_terminal_state(operation_id, records)
        self._own_operations.complete(operation_id, terminal_result)
        if self._active_monitored_operation_id == operation_id:
            self._active_monitored_operation_id = None
        self._watch_service.set_paused(False)
        # Canonical outcomes update baselines first; this full bounded pass then
        # validates both expected own changes and any unexpected concurrent one.
        self._watch_service.manual_refresh()

    # ── Controller-owned physical file operations (TE-WATCH-10) ─────────────────

    def _file_operations(self) -> FileOperationService:
        """The one canonical filesystem service; root validation lives in it."""
        root = self._session.scan_result.root if self._session.scan_result else None
        service = FileOperationService()
        if root is not None:
            try:
                service.set_root(root)
            except FileOperationError:
                pass
        return service

    def rename_path(self, path, new_name: str):
        """Rename one loaded file or folder through the owned lifecycle."""
        path = Path(path)
        return self._run_owned_file_operation(
            "rename", lambda: self.rename_path(path, new_name),
            [(path, path.with_name(str(new_name).strip() or path.name), False)],
            lambda service, source, _target: service.rename(source, new_name))

    def move_paths(self, sources, destination_folder):
        """Move loaded files or folders into one destination folder."""
        destination_folder = Path(destination_folder)
        sources = [Path(value) for value in sources]
        return self._run_owned_file_operation(
            "move", lambda: self.move_paths(sources, destination_folder),
            [(source, destination_folder / source.name, False) for source in sources],
            lambda service, source, target: service.move(source, target))

    def recycle_paths(self, paths):
        """Send loaded files or folders to the Recycle Bin."""
        paths = [Path(value) for value in paths]
        return self._run_owned_file_operation(
            "recycle", lambda: self.recycle_paths(paths),
            [(path, path, True) for path in paths],
            lambda service, source, _target: (service.recycle(source), source)[1])

    def create_folder(self, parent, name: str):
        """Create one folder inside the workspace through the owned lifecycle."""
        parent = Path(parent)
        return self._run_owned_file_operation(
            "create_folder", lambda: self.create_folder(parent, name),
            [(parent / str(name).strip(), parent / str(name).strip(), False)],
            lambda service, _source, _target: service.create_folder(parent, name))

    def _run_owned_file_operation(self, operation_type: str, retry, plans, execute):
        """One lifecycle for every physical mutation of a loaded path.

        Validate, refuse incompatible disk work, register exact evidence before
        touching disk, collect events instead of acting on them, execute through
        the canonical service, prove the terminal identity or absence, then run
        one authoritative reconciliation and resume monitoring.
        """
        from core.change_sets import capture_file_identity
        import uuid

        if self.is_disk_op_active() or self.is_replaygain_analysis_active():
            self.status_update.emit(t("md_busy_disk_op"))
            return None
        bypass, self._lifecycle_bypass = self._lifecycle_bypass, False
        if not bypass and not self.request_lifecycle_action(operation_type, retry):
            return None

        service = self._file_operations()
        operation_id = f"{operation_type}-{uuid.uuid4().hex}"
        for source, target, _absence in plans:
            self._own_operations.begin(
                operation_id, operation_type, source, target,
                original_identity=capture_file_identity(source),
                expected_kinds=_EXPECTED_EVENT_KINDS)
        self._active_monitored_operation_id = operation_id
        self._watch_service.set_paused(True)

        outcomes: list[FileOperationOutcome] = []
        records: list[tuple[Path, Path | None, bool, bool]] = []
        try:
            for source, target, absence in plans:
                try:
                    final = execute(service, source, target)
                    final = Path(final) if final is not None else Path(target)
                    outcomes.append(FileOperationOutcome(
                        operation_type, source, final, True))
                    records.append((source, final, True, absence))
                except FileOperationError as exc:
                    logger.warning("[MetadataController] %s failed for %s: %s",
                                   operation_type, source, exc)
                    outcomes.append(FileOperationOutcome(
                        operation_type, source, None, False, exc.code, str(exc)))
                    # A failed path proves nothing, so it must never suppress.
                    records.append((source, None, False, absence))
        finally:
            succeeded = sum(outcome.succeeded for outcome in outcomes)
            terminal = ("success" if succeeded == len(plans)
                        else "failed" if not succeeded else "partial")
            self._finish_monitored_disk_operation(operation_id, terminal, records=records)
        return FileOperationBatchResult(operation_type, tuple(outcomes))

    def _stop_watch_session(self) -> None:
        self._watch_service.stop_session()
        self._watch_session = None
        for worker in tuple(self._refresh_workers):
            worker.cancel()
        self._current_refresh_worker = None

    def _start_watch_session(self, result) -> None:
        from core.filesystem_monitoring import WatchRootSession
        # The session inherits the scope the user actually scanned with.  A
        # non-recursive scan must never grow subfolders through monitoring,
        # Manual Refresh, overflow or root restoration.
        recursive = bool(getattr(result, "recursive", True))
        self._watch_session = WatchRootSession.create(
            result.root, self.workspace_state.generation,
            self.workspace_state.content_revision, recursive=recursive)
        self._watch_service.start_session(self._watch_session, result.folder_set)

    def _on_filesystem_batch(self, batch) -> None:
        session = self._watch_session
        if (session is None or batch.session_id != session.session_id
                or batch.generation != session.generation):
            return
        if self.is_disk_op_active():
            # Adapter remains bounded and paused during normal disk paths. A
            # race-delivered batch is superseded by the terminal full pass.
            return
        from core.filesystem_monitoring import (
            FilesystemEventBatch, OwnOperationMatch,
        )
        actionable = []
        for event in batch.events:
            match = self._own_operations.classify(event)
            if match is not OwnOperationMatch.EXPECTED:
                actionable.append(event)
        if not actionable and not batch.overflowed:
            self.external_changes_updated.emit(self._external_change_count())
            return
        current = self._current_refresh_worker
        if current is not None and current.isRunning():
            current.cancel()
        from core.file_refresh_service import snapshot_workspace_files
        from ui.workers.filesystem_refresh_worker import FilesystemRefreshWorker
        accepted_batch = FilesystemEventBatch(
            batch.session_id, batch.generation, tuple(actionable), batch.overflowed)
        worker = FilesystemRefreshWorker(
            root=session.root, batch=accepted_batch,
            generation=self.workspace_state.generation,
            content_revision=self.workspace_state.content_revision,
            change_revision=self.workspace_state.change_revision,
            tracked=snapshot_workspace_files(self.workspace_state),
            recursive=session.recursive, parent=self)
        self._refresh_workers.add(worker)
        self._current_refresh_worker = worker
        worker.plan_ready.connect(self._on_filesystem_plan)
        worker.refresh_failed.connect(self._on_filesystem_refresh_failed)
        worker.finished.connect(lambda w=worker: self._on_filesystem_refresh_finished(w))
        worker.start()

    def _on_filesystem_plan(self, plan) -> None:
        worker = self.sender()
        session = self._watch_session
        if (worker is not self._current_refresh_worker or session is None
                or plan.session_id != session.session_id
                or plan.generation != session.generation
                or self._incremental_updater is None):
            return
        summary = self._incremental_updater.apply(plan, root=session.root)
        if not summary.accepted:
            if summary.diagnostic.startswith("stale_"):
                self._watch_service.manual_refresh()
            return
        if plan.root_lost:
            # An unreachable root is not a directory change.  Rebuilding the
            # watch set or the folder tree from a root that is not there would
            # erase the session the user must be able to resume.
            self.workspace_refresh_applied.emit({"summary": summary, "folders": ()})
            self.external_changes_updated.emit(self._external_change_count())
            return
        current_dirs = {path for path in self._watch_service.watched_directories
                        if path.is_dir()}
        current_dirs.update(path for path in plan.watched_directories if path.is_dir())
        self._watch_service.update_directories(current_dirs)
        if self._session.scan_result is not None:
            self._session.scan_result.tracks = self.workspace_state.tracks
            self._session.scan_result.folder_set = set(current_dirs)
        self._dup_request_id += 1
        self._validation_snapshot = None
        self.workspace_refresh_applied.emit({
            "summary": summary, "folders": tuple(current_dirs)})
        self.external_changes_updated.emit(self._external_change_count())
        self.revalidate_problems()

    def _on_filesystem_refresh_failed(self, diagnostic: str) -> None:
        from core.filesystem_monitoring import MonitoringState
        if self.sender() is self._current_refresh_worker:
            self.monitoring_state_changed.emit(
                MonitoringState.DEGRADED, diagnostic or "refresh_failed")

    def _on_filesystem_refresh_finished(self, worker) -> None:
        self._refresh_workers.discard(worker)
        if self._current_refresh_worker is worker:
            self._current_refresh_worker = None
        if self._shutting_down:
            self._on_shutdown_worker_finished()

    def _external_change_count(self) -> int:
        from core.filesystem_monitoring import is_external_change
        return sum(is_external_change(getattr(item, "external_state", "current"))
                   for item in self.workspace_state.tracks)

    def resolve_unsaved_changes(self, choice: str) -> bool:
        """Resolve Apply/Keep Draft/Discard/Cancel and continue exactly once."""
        pending = self._pending_lifecycle_action
        if pending is None:
            return False
        if choice == "cancel":
            self._pending_lifecycle_action = None
            return True
        if choice == "keep_draft":
            if not self._flush_draft():
                return False
            return self._continue_lifecycle_action()
        if choice == "discard":
            self.workspace_state.revert_items(self.workspace_state.tracks)
            self._draft_timer.stop()
            self.discard_draft()
            self.tags_modified.emit()
            return self._continue_lifecycle_action()
        if choice == "apply":
            self.apply_changes()
            return True
        return False

    def _continue_lifecycle_action(self) -> bool:
        pending, self._pending_lifecycle_action = self._pending_lifecycle_action, None
        if pending is None:
            return False
        self._lifecycle_bypass = True
        pending["continuation"]()
        return True

    def acknowledge_draft_review(self) -> None:
        self._draft_review_required = False

    def undo_proposals(self) -> bool:
        if not self.workspace_state.undo_proposals():
            return False
        self.tags_modified.emit()
        return True

    def redo_proposals(self) -> bool:
        if not self.workspace_state.redo_proposals():
            return False
        self.tags_modified.emit()
        return True

    def revert_fields(self, tracks: list[AudioTrackItem], fields: set[str] | None = None) -> None:
        self.workspace_state.revert_items(tracks, fields)
        self.tags_modified.emit()

    def set_apply_excluded_ids(self, identities: list[int], excluded: bool) -> None:
        self.workspace_state.set_apply_excluded_ids(identities, excluded)
        self.tags_modified.emit()

    def revert_review_records(self, records: list[tuple[int, str]]) -> None:
        by_id: dict[int, set[str]] = {}
        for identity, field_name in records:
            by_id.setdefault(identity, set()).add(field_name)
        self.workspace_state.revert_record_targets(by_id)
        self.tags_modified.emit()

    def revert_review_files(self, identities: list[int]) -> None:
        self.workspace_state.revert_record_targets({identity: None for identity in identities})
        self.tags_modified.emit()

    def apply_scope(self) -> list[AudioTrackItem]:
        """Phase 2 Apply helper: changed files less explicit exclusions only."""
        return self.workspace_state.apply_candidates()

    def reconcile_apply_outcome(self, outcome) -> AudioTrackItem | None:
        """Reconcile a worker outcome after its item may have been renamed."""
        return self.workspace_state.reconcile_apply_outcome(outcome)

    def _active_disk_worker(self):
        for w in (self._apply_worker, self._restore_worker, self._recovery_worker, self._undo_worker):
            if w and w.isRunning():
                return w
        return None

    def is_disk_op_active(self) -> bool:
        """True while an Apply, Restore, or Recovery worker is writing to disk.

        Apply, Restore and Recovery are mutually exclusive disk-changing
        operations (blocker 1): while any is active, scans/root replacement and
        the other operations are refused and application close is deferred."""
        return bool(
            (self._apply_worker and self._apply_worker.isRunning())
            or (self._restore_worker and self._restore_worker.isRunning())
            or (self._recovery_worker and self._recovery_worker.isRunning())
            or (self._undo_worker and self._undo_worker.isRunning())
        )

    def is_replaygain_analysis_active(self) -> bool:
        return bool(self._replaygain_worker and self._replaygain_worker.isRunning())

    def _active_shutdown_workers(self) -> tuple[object, ...]:
        """Return every worker that must finish before resources can close."""
        workers = (
            self._scan_worker,
            self._apply_worker,
            self._restore_worker,
            self._undo_worker,
            self._recovery_worker,
            self._replaygain_worker,
            self._dup_worker,
            self._online_worker,
            self._online_release_detail_worker,
            self._online_artwork_worker,
            *tuple(self._metadata_io_workers),
            *tuple(self._refresh_workers),
        )
        active: list[object] = []
        seen: set[int] = set()
        for worker in workers:
            if worker is None or id(worker) in seen:
                continue
            try:
                running = worker.isRunning()
            except (AttributeError, RuntimeError):
                running = False
            if running:
                active.append(worker)
                seen.add(id(worker))
        return tuple(active)

    def has_active_shutdown_work(self) -> bool:
        return bool(self._active_shutdown_workers())

    # ── Public API ─────────────────────────────────────────────────────────────

    def scan(self, folder: Path, recursive: bool) -> None:
        """Cancel any running scan and start a new one."""
        from ui.workers.metadata_worker import MetadataScanWorker

        self.cancel_online_lookup()

        # Never replace the workspace out from under an in-flight disk op.
        if self.is_disk_op_active():
            self.status_update.emit(t("md_busy_disk_op"))
            return
        bypass, self._lifecycle_bypass = self._lifecycle_bypass, False
        if (not bypass and not self.request_lifecycle_action(
                "scan", lambda: self.scan(folder, recursive))):
            return

        self._stop_watch_session()
        self._cancel_scan()
        self._invalidate_metadata_io_workers()
        # A new root invalidates every duplicate request before its late Qt
        # signals can reach the current workspace.
        self._dup_request_id += 1
        if self._dup_worker and self._dup_worker.isRunning():
            self._dup_worker.cancel()
        self.cancel_replaygain_analysis()
        self._bump_generation()
        self._session = TagEditSession()
        self.workspace_state.set_tracks([])

        self.workspace_replacement_started.emit(folder)
        self.scan_started.emit()
        self.status_update.emit(t("md_scanning_folder", folder=folder.name))

        self._scan_worker = MetadataScanWorker(folder, recursive, parent=self)
        self._scan_worker.track_batch_found.connect(self._on_track_batch_found)
        self._scan_worker.scan_progress.connect(self.scan_progress)
        self._scan_worker.scan_complete.connect(self._on_scan_complete)
        self._scan_worker.scan_error.connect(self._on_scan_error)
        self._scan_worker.start()

    def cancel_scan(self) -> None:
        self._cancel_scan()

    def apply_auto_rules(self, tracks: list[AudioTrackItem]) -> None:
        """Compatibility entry point delegated to the Phase 9 registry."""
        preview = self._execute_registered_action("tag.auto_arrange.v1", tracks)
        changed_count = preview.changed_count if preview else 0
        self.auto_rules_applied.emit()
        self.status_update.emit(
            t("md_auto_changes_proposed", n=changed_count)
            if changed_count else t("md_auto_no_changes")
        )

    def apply_artist_to_scope(self, artist: str, tracks: list[AudioTrackItem]) -> None:
        """Set artist through the declarative action engine."""
        self._execute_registered_action("tag.set_artist.v1", tracks, {"value": artist})
        self.status_update.emit(t("md_artist_applied", artist=artist, n=len(tracks)))

    def apply_album_to_folder(
        self, album: str, folder: Path, tracks: list[AudioTrackItem]
    ) -> None:
        """Set album for the requested folder through the action engine."""
        affected = [track for track in tracks if track.folder == folder]
        self._execute_registered_action(
            "tag.set_field.v1", affected, {"field": "album", "value": album}
        )
        self.status_update.emit(t("md_album_applied", album=album, n=len(affected)))

    def apply_album_to_scope(self, album: str, tracks: list[AudioTrackItem]) -> None:
        """Set album for a stable scope through the action engine."""
        preview = self._execute_registered_action(
            "tag.set_field.v1", tracks, {"field": "album", "value": album}
        )
        affected = preview.changed_count if preview else 0
        self.status_update.emit(t("md_album_applied", album=album, n=affected))

    def apply_title_from_filename(
        self, tracks: list[AudioTrackItem], strip_numbering: bool = True
    ) -> None:
        self._execute_registered_action(
            "tag.title_from_filename.v1", tracks,
            {"strip_numbering": strip_numbering},
        )

    def apply_track_from_filename(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.track_from_filename.v1", tracks)

    def clear_comments(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_comments.v1", tracks)

    def apply_changes(
        self,
        backup_dir: Optional[Path] = None,
        tracks_to_apply: Optional[list] = None,
    ) -> None:
        """Launch Apply from workspace candidates, never Qt selection/visibility.

        ``tracks_to_apply`` remains a compatibility argument for direct callers
        before a workspace scan has populated the shared state.
        """
        import uuid
        from ui.workers.metadata_worker import MetadataApplyWorker

        if self._shutting_down:
            return
        if self._draft_review_required:
            self.status_update.emit(t("meta_draft_review_required"))
            return
        if self._apply_worker and self._apply_worker.isRunning():
            return
        # Apply, Restore and Recovery are mutually exclusive disk operations.
        if self.is_disk_op_active():
            self.status_update.emit(t("md_busy_disk_op"))
            return

        candidates = self.apply_scope()
        if not self.workspace_state.tracks:
            candidates = [t for t in (tracks_to_apply or []) if t.has_changes]
        # One canonical blocker set, shared with Apply scope, Review and the
        # toolbar.  An explicitly excluded stale item is not in this batch, so
        # it blocks nothing here and its proposal stays pending and reviewable.
        blocked_external = self.workspace_state.apply_blockers()
        if blocked_external:
            self.status_update.emit(t("meta_external_apply_blocked", n=len(blocked_external)))
            self.tags_modified.emit()
            return
        changed = candidates

        if not changed:
            self.status_update.emit(t("md_no_changes_to_apply"))
            return

        # Re-check the scan baseline immediately before creating a backup or
        # worker.  A stale path is a hard blocker: never write a proposal to a
        # replacement file merely because it has the old name.
        from core.change_sets import file_identity_status
        stale = [item for item in changed
                 if file_identity_status(item.baseline_identity, item.path) not in {"current", "unavailable"}]
        if stale:
            for item in stale:
                item.status = TrackStatus.ERROR
                item.error_msg = "stale_file"
            self.status_update.emit(t("meta_apply_blocked_title"))
            self.tags_modified.emit()
            return

        bd = backup_dir or _BACKUP_DIR
        operation_id = uuid.uuid4().hex
        self._apply_plan = None
        if self.workspace_state.tracks:
            try:
                from core.apply_plan import ApplyPlanBlocked, build_apply_plan
                # Compatibility callers can still mutate ProposedTags directly;
                # synchronising here makes the frozen plan the sole Apply input.
                self.workspace_state.synchronize_proposals(self.workspace_state.tracks)
                self._apply_plan = build_apply_plan(self.workspace_state, operation_id=operation_id)
            except ApplyPlanBlocked as exc:
                self.status_update.emit(t("meta_apply_blocked_title"))
                return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = bd / f"bananaflow_tag_backup_{timestamp}_{operation_id[:8]}.json"
        self._session.backup_path = backup_path

        root = self._session.scan_result.root if self._session.scan_result else None

        self.apply_started.emit()
        self.status_update.emit(t("md_writing_tags_to_n", n=len(changed)))

        self._apply_worker = MetadataApplyWorker(
            changed, backup_path,
            operation_id=operation_id,
            op_generation=self._op_generation,
            root=root,
            review_plan=self._apply_plan,
            parent=self,
        )
        self._apply_worker.progress.connect(self._on_apply_progress)
        self._apply_worker.file_outcome.connect(self._on_apply_file_outcome)
        self._apply_worker.finished.connect(self._on_apply_finished)
        self._begin_monitored_disk_operation(
            operation_id, "apply", changed,
            expected_final=lambda item: item.path.with_name(item.proposed_filename)
            if item.proposed_filename else item.path)
        self._apply_worker.finished.connect(
            lambda result, op=operation_id: self._finish_monitored_disk_operation(
                op, "failed" if result.aborted else
                "partial" if (result.failed_count or result.partial_count
                               or result.cancelled_count) else "success",
                records=[(outcome.original_path, outcome.final_path,
                          outcome.status == "success", False)
                         for outcome in result.outcomes]))
        self._apply_worker.start()

    def analyze_replaygain_tracks(self, tracks: list[AudioTrackItem]) -> None:
        self._start_replaygain_analysis(tracks, "track")

    def analyze_replaygain_album(self, tracks: list[AudioTrackItem]) -> None:
        self._start_replaygain_analysis(tracks, "album")

    def _start_replaygain_analysis(self, tracks: list[AudioTrackItem], mode: str) -> None:
        """Start proposal-only loudness analysis for the exact selection."""
        import uuid
        from core.metadata_backend import FORMAT_CAPABILITIES
        from core.metadata_models import REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK
        from core.replay_gain import group_album_scope
        from ui.workers.metadata_worker import ReplayGainAnalysisWorker

        if self._shutting_down:
            return
        if self.is_replaygain_analysis_active():
            return
        supported = [
            item for item in tracks
            if item.metadata_editable
            and FORMAT_CAPABILITIES.by_id(item.format_id).supports_field(REPLAYGAIN_TRACK_GAIN)
            and FORMAT_CAPABILITIES.by_id(item.format_id).supports_field(REPLAYGAIN_TRACK_PEAK)
        ]
        if not supported:
            self.status_update.emit(t("md_replaygain_no_supported_files"))
            return
        try:
            selection_ids = tuple(sorted(self.workspace_state.item_id(item) for item in tracks))
        except KeyError:
            selection_ids = tuple(sorted(id(item) for item in tracks))
        groups = (
            group_album_scope(supported, item_id=self.workspace_state.item_id)
            if mode == "album" else ()
        )
        worker = ReplayGainAnalysisWorker(
            supported,
            mode=mode,
            groups=groups,
            operation_id=uuid.uuid4().hex,
            op_generation=self._op_generation,
            selection_ids=selection_ids,
            parent=self,
        )
        self._replaygain_worker = worker
        worker.progress.connect(self._on_replaygain_progress)
        worker.result_ready.connect(self._on_replaygain_result)
        worker.finished.connect(self._on_replaygain_finished)
        self.replaygain_analysis_started.emit(mode, len(supported))
        self.status_update.emit(t("md_replaygain_analysis_started", n=len(supported)))
        worker.start()

    def cancel_replaygain_analysis(self) -> None:
        if self._replaygain_worker and self._replaygain_worker.isRunning():
            self._replaygain_worker.cancel()

    def _on_replaygain_progress(self, done: int, total: int) -> None:
        if not self._shutting_down and self.sender() is self._replaygain_worker:
            self.replaygain_analysis_progress.emit(done, total)

    def _replaygain_result_is_current(self, worker) -> bool:
        if worker is None or self._is_stale(worker):
            return False
        try:
            current = tuple(sorted(
                self.workspace_state.item_id(item)
                for item in self.workspace_state.selected_tracks()
            ))
        except KeyError:
            return False
        return current == getattr(worker, "selection_ids", ())

    def _on_replaygain_result(self, item: AudioTrackItem, values: dict) -> None:
        worker = self.sender()
        if self._shutting_down or not self._replaygain_result_is_current(worker):
            return
        for field_name, value in values.items():
            item.proposed.set_replay_gain(field_name, value)
        self._proposal_origin = ChangeOrigin.REPLAYGAIN
        self.replaygain_proposal_ready.emit(item, values)
        self.tags_modified.emit()

    def _on_replaygain_finished(self, summary: dict) -> None:
        worker = self.sender()
        stale = not self._replaygain_result_is_current(worker)
        summary = dict(summary)
        summary["stale"] = stale
        if worker is self._replaygain_worker:
            self._replaygain_worker = None
        if self._shutting_down:
            return
        self.replaygain_analysis_complete.emit(summary)
        if stale:
            self.status_update.emit(t("md_replaygain_stale_results"))
        elif summary.get("cancelled"):
            self.status_update.emit(t("md_replaygain_analysis_cancelled", n=summary.get("completed", 0)))
        elif summary.get("failed"):
            self.status_update.emit(t(
                "md_replaygain_analysis_partial",
                done=summary.get("completed", 0) - summary.get("failed", 0),
                fail=summary.get("failed", 0),
            ))
        else:
            self.status_update.emit(t("md_replaygain_analysis_complete", n=summary.get("completed", 0)))

    # ── Restore from backup ────────────────────────────────────────────────────

    @staticmethod
    def default_backup_dir() -> Path:
        """Where apply_changes() writes its JSON tag backups."""
        return _BACKUP_DIR

    @staticmethod
    def load_backup(backup_path: Path):
        """
        Parse a backup file into (path, OriginalTags) records — read-only.

        The panel calls this first so it can show the user exactly which
        files would be touched before anything is written. Raises
        ValueError/OSError/json.JSONDecodeError on unusable files.
        """
        from core.metadata_processor import load_tag_backup
        return load_tag_backup(backup_path)

    def restore_from_backup(self, records) -> None:
        """Launch MetadataRestoreWorker for pre-loaded, user-confirmed records."""
        from ui.workers.metadata_worker import MetadataRestoreWorker
        import uuid

        backup_path = None
        if isinstance(records, dict):
            backup_path = records.get("backup_path")
            records = records.get("records", [])

        if self._restore_worker and self._restore_worker.isRunning():
            return
        # Restore, Apply and Recovery are mutually exclusive disk operations.
        if self.is_disk_op_active():
            self.status_update.emit(t("md_busy_disk_op"))
            return

        bypass, self._lifecycle_bypass = self._lifecycle_bypass, False
        request_payload = ({"records": records, "backup_path": backup_path}
                           if backup_path else records)
        if (not bypass and not self.request_lifecycle_action(
                "restore", lambda: self.restore_from_backup(request_payload))):
            return

        self.restore_started.emit()
        self.status_update.emit(t("md_restoring_tags", n=len(records)))

        restore_operation_id = f"restore-{uuid.uuid4().hex}"
        self._restore_worker = MetadataRestoreWorker(
            records, backup_path=Path(backup_path) if backup_path else None,
            operation_id=restore_operation_id, op_generation=self._op_generation, parent=self,
        )
        self._restore_worker.progress.connect(self._on_restore_progress)
        self._restore_worker.finished.connect(self._on_restore_finished)
        self._begin_monitored_disk_operation(
            restore_operation_id, "restore", [path for path, _tags in records])
        self._restore_worker.finished.connect(
            lambda outcomes, op=restore_operation_id:
            self._finish_monitored_disk_operation(
                op, "partial" if any(getattr(value, "status", "") in {
                    "failed", "cancelled"} for value in outcomes) else "success",
                records=[(value.path, value.path,
                          getattr(value, "status", "") in {"restored", "unchanged"}, False)
                         for value in outcomes]))
        self._restore_worker.start()

    def undo_applied_batch(self, manifest_path: Path, *, restore_paths: bool = False) -> None:
        """Start the explicit disk-level undo worker after a reviewed preview."""
        from ui.workers.metadata_worker import UndoAppliedBatchWorker
        if self.is_disk_op_active():
            self.status_update.emit(t("md_busy_disk_op"))
            return
        bypass, self._lifecycle_bypass = self._lifecycle_bypass, False
        if (not bypass and not self.request_lifecycle_action(
                "undo_applied_batch",
                lambda: self.undo_applied_batch(manifest_path, restore_paths=restore_paths))):
            return
        self._undo_worker = UndoAppliedBatchWorker(
            manifest_path, restore_paths=restore_paths,
            op_generation=self._op_generation, parent=self,
        )
        self._undo_worker.finished.connect(self._on_undo_applied_batch_finished)
        undo_operation_id = f"undo-{datetime.now().timestamp()}"
        self._begin_monitored_disk_operation(
            undo_operation_id, "undo_applied_batch", ())
        self._undo_worker.finished.connect(
            lambda result, op=undo_operation_id: self._finish_monitored_disk_operation(
                op, "failed" if isinstance(result, Exception) else
                "partial" if result.partial else "success"))
        self._undo_worker.start()

    def _on_undo_applied_batch_finished(self, result) -> None:
        if self._is_stale(self.sender()):
            return
        if isinstance(result, Exception):
            self.apply_error.emit(str(result))
            return
        if result.partial:
            self.status_update.emit(t("md_recovery_incomplete", failed=1, missing=0))
        else:
            self.status_update.emit(t("md_recovery_done", restored=len(result.metadata_outcomes)))

    # ── Startup recovery (TE-SAFE-11) ───────────────────────────────────────────

    def check_for_recovery(self, backup_dir: Optional[Path] = None):
        """
        Look for an incomplete Apply journal from a previous crashed run and,
        if found, emit `recovery_available` with a review-first summary. Never
        performs any destructive action automatically.
        """
        from core.metadata_processor import find_incomplete_journals, inspect_recovery_journal

        if self.is_disk_op_active():
            return None
        bd = backup_dir or _BACKUP_DIR
        try:
            journals = find_incomplete_journals(bd)
        except Exception as exc:
            logger.warning("[MetadataController] Recovery scan failed: %s", exc)
            return None
        if not journals:
            return None
        summary = inspect_recovery_journal(journals[0])
        self.recovery_available.emit(summary)
        return summary

    def recover_from_journal_backup(self, summary: dict) -> None:
        """
        Execute the reviewed recovery (defect 2, option 1): a background worker
        renames each file back to its original path (never overwriting) and
        restores its original tags, combining the journal (paths) and backup
        (tags). The journal is retired only when every operation succeeds.
        """
        from ui.workers.metadata_worker import MetadataRecoveryWorker

        if self.is_disk_op_active() or (
            self._recovery_worker and self._recovery_worker.isRunning()
        ):
            self.status_update.emit(t("md_busy_disk_op"))
            return

        bypass, self._lifecycle_bypass = self._lifecycle_bypass, False
        if (not bypass and not self.request_lifecycle_action(
                "recovery", lambda: self.recover_from_journal_backup(summary))):
            return

        journal_path = summary.get("journal_path")
        if not journal_path:
            self.status_update.emit(t("md_recovery_no_backup"))
            return

        if (summary.get("recommended_action") == "reconcile"
                and not summary.get("incomplete")
                and summary.get("current_disk_state") == "evidence_consistent"):
            self._retire_journal(journal_path)
            self.status_update.emit(t("md_recovery_reconciled"))
            self.recovery_complete.emit([], True)
            return

        self.status_update.emit(t("md_recovery_running"))
        self._recovery_worker = MetadataRecoveryWorker(
            Path(journal_path), op_generation=self._op_generation, parent=self,
        )
        self._recovery_worker.finished.connect(self._on_recovery_finished)
        recovery_operation_id = f"recovery-{datetime.now().timestamp()}"
        self._begin_monitored_disk_operation(
            recovery_operation_id, "recovery", ())
        self._recovery_worker.finished.connect(
            lambda _outcomes, all_ok, _journal, _error, op=recovery_operation_id:
            self._finish_monitored_disk_operation(
                op, "success" if all_ok else "partial"))
        self._recovery_worker.start()

    def _on_recovery_finished(self, outcomes, all_ok: bool, journal_path: str,
                              preflight_error: str = "") -> None:
        from core.metadata_models import RestoreStatus

        if self._is_stale(self.sender()):
            # A newer workspace superseded this recovery — do not mutate it.
            logger.info("[MetadataController] Dropping stale recovery result")
            return

        if preflight_error:
            # Backup invalid/missing — nothing was changed on disk; keep the
            # journal untouched so the user can fix the backup and retry.
            self.status_update.emit(t("md_recovery_preflight_failed", code=preflight_error))
            self.recovery_complete.emit(outcomes, False)
            return

        restored = sum(1 for o in outcomes if o.status == RestoreStatus.RESTORED)
        failed   = sum(1 for o in outcomes if o.status == RestoreStatus.FAILED)
        missing  = sum(1 for o in outcomes if o.status == RestoreStatus.MISSING)

        if all_ok:
            # Every requested recovery op succeeded — safe to retire the journal.
            self._retire_journal(journal_path)
            self.status_update.emit(t("md_recovery_done", restored=restored))
        else:
            # Keep the journal so the user can retry / inspect (defect 2/5).
            self.status_update.emit(
                t("md_recovery_incomplete", failed=failed, missing=missing))
        self.recovery_complete.emit(outcomes, all_ok)

    def keep_recovery_for_later(self, summary: dict) -> None:
        """'Not now': keep the journal so recovery can be offered again."""
        # Intentionally does nothing destructive — the journal is retained.
        logger.info("[MetadataController] Recovery deferred; journal kept: %s",
                    summary.get("journal_path"))

    def forget_recovery(self, summary: dict) -> None:
        """'Forget recovery': permanently delete the journal (explicit, confirmed
        destructive action). The panel must confirm before calling this."""
        if summary.get("discard_allowed") is False:
            self.status_update.emit(t("md_recovery_unresolved_cannot_discard"))
            return
        self._retire_journal(summary.get("journal_path"))
        self.status_update.emit(t("md_recovery_forgotten"))

    def _retire_journal(self, journal_path) -> None:
        if not journal_path:
            return
        try:
            Path(journal_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("[MetadataController] Could not retire journal: %s", journal_path)

    # ── Bounded, event-loop-safe shutdown (TE-SAFE-13) ──────────────────────────

    # How long to wait for an in-flight disk op to finish safely before keeping
    # the app open with a "still finishing" message (never killing the thread).
    _SHUTDOWN_TIMEOUT_MS = 15000

    def request_shutdown(self) -> bool:
        """
        Ask whether the application may close now.

        Returns True when no resource-owning worker is active (safe to close).
        During Apply/Restore/Recovery or ReplayGain analysis, requests
        cancellation at the next safe boundary and returns False;
        `shutdown_ready` fires once the worker finishes so the caller can retry
        the close. If a bounded timeout elapses first,
        `shutdown_timed_out` fires and the app stays open. Never terminates a
        running QThread and never blocks the GUI thread; callbacks are wired at
        most once (defect 6).
        """
        self._watch_service.shutdown()
        self._watch_session = None
        for worker in tuple(self._refresh_workers):
            worker.cancel()
        workers = self._active_shutdown_workers()
        if not workers:
            return True

        if self._shutting_down:
            # Already requested — do not connect duplicate callbacks or restart
            # the timer; just keep deferring the close.
            return False

        self._shutting_down = True
        for worker in workers:
            try:
                worker.cancel()
            except Exception:
                pass
            if id(worker) not in self._shutdown_connected_ids:
                try:
                    worker.finished.connect(self._on_shutdown_worker_finished)
                    self._shutdown_connected_ids.add(id(worker))
                except Exception:
                    pass

        # Bounded timeout: if the op has not finished safely in time, keep the
        # application open and tell the user it is still finishing.
        from PySide6.QtCore import QTimer
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.timeout.connect(self._on_shutdown_timeout)
        self._shutdown_timer.start(self._SHUTDOWN_TIMEOUT_MS)
        return False

    def _on_shutdown_worker_finished(self, *args) -> None:
        if self._active_shutdown_workers():
            return
        if self._shutdown_timer is not None:
            self._shutdown_timer.stop()
            self._shutdown_timer = None
        self._shutting_down = False
        self._shutdown_connected_ids.clear()
        self.shutdown_ready.emit()

    def _on_shutdown_timeout(self) -> None:
        # The disk op is still running; keep the app open. Allow a later close
        # attempt to re-arm the deferral.
        self._shutting_down = False
        self._shutdown_timer = None
        self.status_update.emit(t("md_shutdown_still_finishing"))
        self.shutdown_timed_out.emit()

    # ── New magic operations ───────────────────────────────────────────────────

    def apply_album_artist_from_artist(self, tracks: list[AudioTrackItem]) -> None:
        preview = self._execute_registered_action("tag.album_artist_from_artist.v1", tracks)
        self.status_update.emit(
            t("md_album_artist_copied", n=preview.changed_count if preview else 0)
        )

    def split_artist_title_from_filename(self, tracks: list[AudioTrackItem]) -> None:
        preview = self._execute_registered_action("tag.split_artist_title.v1", tracks)
        self.status_update.emit(
            t("md_artist_title_split_done", n=preview.changed_count if preview else 0)
        )

    def clear_year(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_year.v1", tracks)
        self.status_update.emit(t("md_year_cleared"))

    def clear_genre(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_genre.v1", tracks)
        self.status_update.emit(t("md_genre_cleared"))

    def clear_track_num(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_track_num.v1", tracks)
        self.status_update.emit(t("md_track_num_cleared"))

    def clear_title(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_title.v1", tracks)
        self.status_update.emit(t("md_title_cleared"))

    def clear_artist(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_artist.v1", tracks)
        self.status_update.emit(t("md_artist_cleared"))

    def clear_album(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_album.v1", tracks)
        self.status_update.emit(t("md_album_cleared"))

    def clear_album_artist(self, tracks: list[AudioTrackItem]) -> None:
        self._execute_registered_action("tag.clear_album_artist.v1", tracks)
        self.status_update.emit(t("md_album_artist_cleared"))

    def normalize_title_spaces(self, tracks: list[AudioTrackItem]) -> None:
        preview = self._execute_registered_action(
            "tag.normalize_spaces.v1", tracks, {"field": "title"}
        )
        self.status_update.emit(
            t("md_spaces_normalised", n=preview.changed_count if preview else 0)
        )

    def strip_web_junk_from_title(self, tracks: list[AudioTrackItem]) -> None:
        parameters = {
            "remove_web_junk": not self._cfg or getattr(
                self._cfg, "tag_clean_title_remove_web_junk", True
            ),
            "remove_hebrew": not self._cfg or getattr(
                self._cfg, "tag_clean_title_remove_hebrew", True
            ),
            "fix_punctuation": not self._cfg or getattr(
                self._cfg, "tag_clean_title_fix_punctuation", True
            ),
        }
        preview = self._execute_registered_action(
            "tag.strip_web_junk.v1", tracks, parameters
        )
        self.status_update.emit(
            t("md_junk_removed", n=preview.changed_count if preview else 0)
        )

    def rename_filename_from_title(self, tracks: list[AudioTrackItem]) -> None:
        preview = self._execute_registered_action("file.from_title.v1", tracks)
        self.status_update.emit(
            t("md_filename_from_title", n=preview.changed_count if preview else 0)
        )

    def clean_filename(self, tracks: list[AudioTrackItem]) -> None:
        parameters = {
            "smart_brackets": not self._cfg or getattr(
                self._cfg, "tag_clean_filename_smart_brackets", True
            ),
            "remove_domains": not self._cfg or getattr(
                self._cfg, "tag_clean_filename_remove_domains", True
            ),
            "remove_emojis": not self._cfg or getattr(
                self._cfg, "tag_clean_filename_remove_emojis", True
            ),
            "fix_spaces": not self._cfg or getattr(
                self._cfg, "tag_clean_filename_fix_spaces", True
            ),
        }
        preview = self._execute_registered_action("file.clean.v1", tracks, parameters)
        self.status_update.emit(
            t("md_filename_cleaned", n=preview.changed_count if preview else 0)
        )

    def strip_filename_numbering(self, tracks: list[AudioTrackItem]) -> None:
        preview = self._execute_registered_action("file.strip_numbering.v1", tracks)
        self.status_update.emit(
            t("md_filename_numbering_removed", n=preview.changed_count if preview else 0)
        )

    def find_duplicates(self, folder: Path, recursive: bool) -> None:
        """Launch DuplicateDetectorWorker to scan for duplicate audio files."""
        from ui.workers.duplicate_detector_worker import DuplicateDetectorWorker

        self._dup_request_id += 1
        self._dup_generation = self.workspace_state.generation
        if self._dup_worker and self._dup_worker.isRunning():
            self._dup_worker.cancel()

        self.status_update.emit(t("md_searching_duplicates_in", folder=folder.name))
        self._dup_worker = DuplicateDetectorWorker(folder, recursive, parent=self,
                                                   request_id=self._dup_request_id, generation=self._dup_generation)
        self._dup_worker.progress.connect(lambda done, total, eta, worker=self._dup_worker: self._on_dup_progress(worker, done, total, eta))
        self._dup_worker.finished.connect(self._on_dup_finished)
        self._dup_worker.error.connect(lambda msg, worker=self._dup_worker: self._on_dup_error(worker, msg))
        self._dup_worker.start()

    def delete_duplicate_files(self, paths: list) -> None:
        """Recycle the given paths through the owned, monitored lifecycle."""
        result = self.recycle_paths([Path(value) for value in paths])
        if result is None:
            return
        success, fail = len(result.succeeded), len(result.failed)
        self.duplicate_delete_complete.emit(success, fail)
        note = t("md_duplicates_deleted_errors_suffix", fail=fail) if fail else ""
        self.status_update.emit(t("md_duplicates_deleted", success=success, note=note))

    def delete_files(self, paths: list) -> None:
        """Recycle-Bin send for arbitrary user-selected tracks (Delete key).

        Same disk-side behavior as `delete_duplicate_files` — both delegate
        to send2trash with `unlink()` fallback. Re-uses the existing
        `duplicate_delete_complete` signal so the panel's existing rescan
        hook (`on_duplicate_delete_complete` → `_on_scan`) refreshes the
        model and tree without extra wiring.
        """
        self.delete_duplicate_files(paths)

    def cancel_apply(self) -> None:
        if self._apply_worker and self._apply_worker.isRunning():
            self._apply_worker.cancel()

    def revert_all(self, tracks: list[AudioTrackItem]) -> None:
        """Clear every pending proposal, including filename proposals."""
        loaded_ids = {id(item) for item in self.workspace_state.tracks}
        loaded = [item for item in tracks if id(item) in loaded_ids]
        if loaded:
            self.workspace_state.revert_items(loaded)
        for item in tracks:
            if id(item) not in loaded_ids:
                item.proposed.clear()
                item.proposed_filename = None
                item.excluded_from_apply = False
        self.workspace_state.reconcile()
        self.tags_modified.emit()
        self.status_update.emit(t("md_all_changes_reverted"))

    # ── Private slots ──────────────────────────────────────────────────────────

    def _on_track_found(self, item: AudioTrackItem) -> None:
        if self._session.scan_result is not None:
            self._session.scan_result.tracks.append(item)
            self._session.scan_result.folder_set.add(item.folder)
        self.workspace_state.add_tracks([item])
        self.track_discovered.emit(item)

    def _on_track_batch_found(self, items: list[AudioTrackItem]) -> None:
        if self._session.scan_result is not None:
            self._session.scan_result.tracks.extend(items)
            self._session.scan_result.folder_set.update(item.folder for item in items)
        self.workspace_state.add_tracks(items)
        self.track_batch_discovered.emit(items)

    def _on_scan_complete(self, result) -> None:
        self._session.scan_result = result
        self.workspace_state.set_tracks(result.tracks)
        self.scan_complete.emit(result)
        if self._pending_draft_snapshot is not None:
            info, self._pending_draft_snapshot = self._pending_draft_snapshot, None
            snapshot = info.get("snapshot")
            targets = info.get("targets", {})
            if snapshot is not None and targets:
                from dataclasses import replace
                from core.change_sets import ChangeSetSnapshot
                remap: dict[int, int] = {}
                orphan = -1
                for old_id_text, target in targets.items():
                    old_id = int(old_id_text)
                    track = self.workspace_state.track_for_path(Path(str(target.get("path") or "")))
                    if track is None:
                        remap[old_id] = orphan
                        orphan -= 1
                    else:
                        remap[old_id] = self.workspace_state.item_id(track)
                states = info.get("target_states", {})
                remapped_records = []
                for record in snapshot.records:
                    state = states.get(record.item_id, "unavailable")
                    diagnostic = record.diagnostic
                    if state == "missing":
                        diagnostic = "draft_target_missing"
                    elif state not in {"current", "unavailable"}:
                        diagnostic = "draft_target_stale"
                    remapped_records.append(replace(
                        record, item_id=remap.get(record.item_id, orphan), diagnostic=diagnostic))
                snapshot = ChangeSetSnapshot(
                    self.workspace_state.generation, snapshot.revision,
                    tuple(remapped_records),
                    frozenset(remap.get(value, value) for value in snapshot.excluded_ids),
                )
            if self.restore_draft(snapshot):
                self.status_update.emit(t("meta_draft_restored"))
            else:
                self.status_update.emit(t("meta_draft_incompatible"))
        n = result.files_count
        self.status_update.emit(t("md_scan_done", n=n, folders=result.folders_count))
        self.revalidate_problems()
        self._start_watch_session(result)

    def _on_scan_error(self, msg: str) -> None:
        logger.error("[MetadataController] Scan error: %s", msg)
        self.scan_failed.emit(msg)
        self.status_update.emit(t("md_scan_error", msg=msg))

    def _on_apply_progress(self, done: int, total: int) -> None:
        self.apply_progress.emit(done, total)
        self.status_update.emit(t("md_writing_tags_progress", done=done, total=total))

    def _is_stale(self, worker) -> bool:
        """Drop a finish/outcome signal from a superseded generation (TE-SAFE-09)."""
        gen = getattr(worker, "op_generation", None)
        return gen is not None and gen != self._op_generation

    def _on_apply_file_outcome(self, outcome) -> None:
        if self._is_stale(self.sender()):
            return
        self.reconcile_apply_outcome(outcome)
        self.apply_file_outcome.emit(outcome)

    def _on_apply_finished(self, result) -> None:
        if self._is_stale(self.sender()):
            logger.info("[MetadataController] Dropping stale apply result (gen mismatch)")
            return

        # The rename graph mutates item.path after the first per-file outcome.
        # Reconcile every final outcome before the batch reaches UI consumers;
        # renamed files receive one final UI update with their current path.
        for outcome in result.outcomes:
            self.reconcile_apply_outcome(outcome)
            if outcome.final_path != outcome.original_path:
                self.apply_file_outcome.emit(outcome)

        # Batch-level abort (backup/preflight failure): surface it distinctly.
        if result.aborted:
            # A late executable-preflight failure invalidates the immutable
            # review snapshot.  The unchanged proposals remain pending and the
            # user must return through Review Changes before trying again.
            if result.global_error_key == "meta_apply_blocked_title":
                self._apply_plan = None
            self.apply_error.emit(t(result.global_error_key or "md_apply_backup_aborted"))
            self.apply_batch_complete.emit(result)
            self.apply_complete.emit(0, 0, 0)
            self.status_update.emit(t(result.global_error_key or "md_apply_backup_aborted"))
            if self._pending_lifecycle_action is not None:
                self.unsaved_changes_action_required.emit(
                    dict(self._pending_lifecycle_action, continuation=None))
            return

        # Derived counts for the legacy 3-int signal: partial+failed count as
        # "not fully applied", cancelled counts as skipped.
        success = result.success_count
        fail    = result.failed_count + result.partial_count
        skip    = result.skipped_count + result.cancelled_count
        self._session.apply_done    = success
        self._session.apply_failed  = fail
        self._session.apply_skipped = skip

        self.apply_batch_complete.emit(result)
        self.apply_complete.emit(success, fail, skip)

        # Schema-4 manifests are finalized from worker-verified outcomes only.
        # A manifest failure never rewrites disk state; the journal remains the
        # recovery authority and the result is surfaced as partial.
        if result.backup_path and result.backup_path.exists():
            try:
                from core.operation_manifest import finalize_manifest
                finalize_manifest(
                    result.backup_path, result.outcomes,
                    status="completed" if not (fail or skip or result.recovery_required) else "partial",
                )
            except Exception as exc:
                logger.error("[MetadataController] Could not finalize operation manifest: %s", exc)

        if result.recovery_required:
            # Durable journalling / rollback failed mid-batch — the journal is
            # retained; surface it and offer recovery now (defect 1/3).
            key = result.global_error_key or "meta_journal_transition_failed"
            self.apply_error.emit(t(key))
            self.status_update.emit(t(key))
            self.check_for_recovery()
            return

        # A verified outcome becomes the next stale-file baseline.  Failures
        # deliberately retain their old baseline and proposal for safe retry.
        from core.change_sets import capture_file_identity
        for outcome in result.outcomes:
            if outcome.status == "success":
                item = self.workspace_state.track_for_path(outcome.final_path)
                if item is not None:
                    item.baseline_identity = capture_file_identity(item.path)
                    item.external_state = "ignored_own_operation"
                    item.external_detail = "apply"
        self.workspace_state.synchronize_proposals(self.workspace_state.tracks)
        self.workspace_state.clear_applied_history()
        if not self.workspace_state.change_set.records():
            self.discard_draft()

        bp = self._session.backup_path
        bp_note = t("md_apply_done_backup_note", name=bp.name) if bp else ""
        self.status_update.emit(t("md_apply_done", success=success, fail=fail, skip=skip, bp_note=bp_note))

        if self._pending_lifecycle_action is not None:
            if self.workspace_state.change_set.records():
                self.unsaved_changes_action_required.emit(
                    dict(self._pending_lifecycle_action, continuation=None))
            else:
                self._continue_lifecycle_action()

    def _on_restore_progress(self, done: int, total: int) -> None:
        self.restore_progress.emit(done, total)
        self.status_update.emit(t("md_restoring_progress", done=done, total=total))

    def _on_restore_finished(self, outcomes) -> None:
        from core.metadata_models import RestoreStatus

        if self._is_stale(self.sender()):
            logger.info("[MetadataController] Dropping stale restore result (gen mismatch)")
            return

        restored  = sum(1 for o in outcomes if o.status == RestoreStatus.RESTORED)
        unchanged = sum(1 for o in outcomes if o.status == RestoreStatus.UNCHANGED)
        missing   = sum(1 for o in outcomes if o.status == RestoreStatus.MISSING)
        failed    = sum(1 for o in outcomes if o.status == RestoreStatus.FAILED)
        self.status_update.emit(t(
            "md_restore_done",
            restored=restored, unchanged=unchanged, missing=missing, fail=failed,
        ))
        self.restore_complete.emit(outcomes)

    def _on_dup_finished(self, groups: dict, elapsed: float, strategy: str) -> None:
        result = groups
        worker = self.sender()
        if worker is not self._dup_worker:
            return
        if hasattr(result, "request_id") and result.request_id != self._dup_request_id:
            return                      # a newer scan already owns the panel
        if hasattr(result, "generation") and result.generation != self._dup_generation:
            # The workspace was reloaded mid-scan, so these findings describe
            # files that are no longer the loaded set and nothing may be
            # applied from them.  The panel still has to hear that the scan
            # ended: dropping this in silence left it reporting "searching"
            # with no scan running and no way to start another.
            self._dup_worker = None
            self.duplicate_scan_complete.emit(
                DuplicateScanResult(generation=self.workspace_state.generation,
                                    request_id=self._dup_request_id, cancelled=True),
                elapsed, strategy)
            return
        if hasattr(result, "groups"):
            result = self._attach_duplicate_workspace_ids(result)
            if result.cancelled:
                self.status_update.emit(t("meta_problems_cancelled"))
                self.duplicate_scan_complete.emit(result, elapsed, strategy)
                self._dup_worker = None
                return
            n_groups = len(result.groups)
            n_files = sum(len(group.paths) for group in result.groups)
        else:  # Compatibility with any external legacy worker.
            n_groups = len(groups)
            n_files = sum(len(value) for value in groups.values())
        strat_lbl = t("md_strategy_md5")
        self.status_update.emit(
            t("md_duplicates_found_summary", n_files=n_files, n_groups=n_groups, strat=strat_lbl, elapsed=elapsed)
        )
        self.duplicate_scan_complete.emit(result, elapsed, strategy)
        self._dup_worker = None

    def _attach_duplicate_workspace_ids(self, result):
        """Attach only current stable IDs to an already accepted scan result."""
        from dataclasses import replace
        by_path = {str(item.path): self.workspace_state.item_id(item)
                   for item in self.workspace_state.tracks}
        return replace(result, groups=tuple(
            replace(group, workspace_ids=tuple(sorted(
                by_path[path] for path in group.paths if path in by_path)))
            for group in result.groups))

    def _on_dup_progress(self, worker, done: int, total: int, eta: str) -> None:
        if worker is self._dup_worker and worker._request_id == self._dup_request_id and worker._generation == self._dup_generation:
            self.duplicate_scan_progress.emit(done, total, eta)

    def _on_dup_error(self, worker, message: str) -> None:
        if worker is self._dup_worker and worker._request_id == self._dup_request_id and worker._generation == self._dup_generation:
            self.duplicate_scan_error.emit(message)
            self._dup_worker = None

    def _cancel_scan(self) -> None:
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            self._scan_worker.wait(1000)
