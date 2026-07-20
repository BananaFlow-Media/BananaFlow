from pathlib import Path

from core.change_sets import ChangeOperation, ChangeOrigin
from core.metadata_models import AudioTrackItem, ArtworkReadState, ArtworkValue, OriginalTags
from core.metadata_validation import IssueState, MetadataValidationEngine, ValidationCategory
from PySide6.QtWidgets import QApplication
from ui.controllers.metadata_controller import MetadataController
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def track(name="song.mp3", **tags):
    return AudioTrackItem(Path(name), Path("."), ".mp3", original=OriginalTags(**tags))


def workspace(*tracks):
    state = TagEditorWorkspaceState()
    state.set_tracks(list(tracks))
    return state


def by_rule(snapshot, rule_id):
    return [issue for issue in snapshot.issues if issue.rule_id == rule_id]


def test_registry_is_unique_and_issues_are_deterministic_and_path_independent():
    engine = MetadataValidationEngine()
    assert len({rule.id for rule in engine.rules}) == len(engine.rules)
    item = track("before.mp3", title="", artist="")
    state = workspace(item)
    first = engine.validate(state)
    item.path = Path("after.mp3")
    second = engine.validate(state)
    assert [issue.id for issue in first.issues] == [issue.id for issue in second.issues]
    assert all(issue.display_paths == ("after.mp3",) for issue in second.issues)


def test_required_values_use_effective_pending_values_and_are_capability_gated():
    item = track(title="", artist="")
    state = workspace(item)
    engine = MetadataValidationEngine()
    assert len(by_rule(engine.validate(state), "metadata.title.required.v1")) == 1
    item.proposed.title = "Pending title"
    state.capture_proposals([item])
    assert by_rule(engine.validate(state), "metadata.title.required.v1")[0].state is IssueState.RESOLVED_BY_PENDING
    item.proposed.artist = ""
    state.capture_proposals([item])
    assert len(by_rule(engine.validate(state), "metadata.artist.required.v1")) == 1
    item.metadata_editable = False
    assert not by_rule(engine.validate(state), "metadata.artist.required.v1")


def test_required_value_can_be_introduced_by_a_pending_change():
    item = track(title="Stored title", artist="Artist")
    state = workspace(item)
    item.proposed.title = ""
    state.capture_proposals([item])
    issue = by_rule(MetadataValidationEngine().validate(state), "metadata.title.required.v1")[0]
    assert issue.state is IssueState.INTRODUCED_BY_PENDING


def test_numbering_exclusion_capability_and_artwork_rules_are_canonical():
    item = track(track_num=3, track_total=2, disc_num=0, disc_total=1)
    state = workspace(item)
    identity = state.item_id(item)
    item.proposed.artist = "Artist"
    state.capture_proposals([item])
    state.set_apply_excluded_ids([identity], True)
    state.change_set.record(identity, "title", "", "Changed", operation=ChangeOperation.SET,
                            origin=ChangeOrigin.MANUAL, equal=lambda *_: False, capability="read_only")
    item.original.artwork = ArtworkValue(read_state=ArtworkReadState.READ_FAILED)
    snapshot = MetadataValidationEngine().validate(state)
    assert by_rule(snapshot, "numbering.track.invalid.v1")
    assert by_rule(snapshot, "numbering.disc.invalid.v1")
    assert by_rule(snapshot, "pending.changed_excluded.v1")
    capability = by_rule(snapshot, "pending.proposal_capability.v1")
    assert capability and capability[0].category is ValidationCategory.CAPABILITY
    assert by_rule(snapshot, "artwork.read_failed.v1")


def test_explicit_artist_fix_is_preview_first_one_undoable_changeset_command():
    QApplication.instance() or QApplication([])
    controller = MetadataController()
    item_a, item_b = track("a.mp3", title="A", artist=""), track("b.mp3", title="B", artist="")
    controller.workspace_state.set_tracks([item_a, item_b])
    snapshot = controller.revalidate_problems()
    issues = by_rule(snapshot, "metadata.artist.required.v1")
    before_revision = controller.workspace_state.change_set.revision
    preview = controller.preview_problem_fix([issue.id for issue in issues], "Common Artist")
    assert preview is not None
    assert not controller.workspace_state.change_set.records()
    assert item_a.proposed.artist is None and item_b.proposed.artist is None
    assert controller.accept_problem_fix(preview)
    assert item_a.proposed.artist == item_b.proposed.artist == "Common Artist"
    assert controller.workspace_state.change_set.revision > before_revision
    assert controller.workspace_state.undo_proposals()
    assert item_a.proposed.artist is None and item_b.proposed.artist is None


def test_problem_fix_preview_uses_the_immutable_action_contract_and_rejects_stale_acceptance():
    QApplication.instance() or QApplication([])
    controller = MetadataController()
    item = track("a.mp3", title="A", artist="")
    controller.workspace_state.set_tracks([item])
    issue = by_rule(controller.revalidate_problems(), "metadata.artist.required.v1")[0]
    preview = controller.preview_problem_fix([issue.id], "Common Artist")
    assert preview is not None
    assert preview.action_preview.action_id == "tag.set_field.v1"
    assert preview.action_preview.changed_count == 1
    assert not controller.workspace_state.change_set.records()
    # A competing proposal invalidates the immutable preview before it can
    # become a second Change Set command.
    item.proposed.title = "Changed elsewhere"
    controller.workspace_state.capture_proposals([item])
    assert not controller.accept_problem_fix(preview)
    assert item.proposed.artist is None
