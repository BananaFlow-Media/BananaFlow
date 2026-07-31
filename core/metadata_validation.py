"""Pure, proposal-aware validation contracts for the Tag Editor Problems center.

The engine deliberately owns no Qt objects, paths as identity, filesystem I/O or
proposal mutation.  It turns the current workspace state into immutable evidence;
the controller is responsible for the explicit, preview-first fix acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from typing import Callable, Iterable

from core.metadata_models import ArtworkReadState


class ValidationSeverity(str, Enum):
    INFORMATION = "information"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ValidationCategory(str, Enum):
    BASIC = "basic_metadata"
    NUMBERING = "numbering"
    CAPABILITY = "format_capability"
    PENDING = "pending_changes"
    ARTWORK = "artwork"
    FILENAME = "filename_path"
    DUPLICATES = "duplicates"

class IssueState(str, Enum):
    PRESENT_ON_DISK = "present_on_disk"
    RESOLVED_BY_PENDING = "resolved_by_pending"
    INTRODUCED_BY_PENDING = "introduced_by_pending"
    PENDING_BLOCKER = "pending_blocker"
    CHANGED_EXCLUDED = "changed_excluded"


class DuplicateEvidence(str, Enum):
    AUDIO_PAYLOAD = "audio_payload"
    WHOLE_FILE = "whole_file"
    SIZE_ONLY = "size_only"


class DuplicateConfidence(str, Enum):
    HIGH = "high"
    POSSIBLE = "possible"


@dataclass(frozen=True)
class DuplicateGroup:
    """Structured, transient duplicate evidence; paths remain display data."""

    id: str
    paths: tuple[str, ...]
    evidence: DuplicateEvidence
    confidence: DuplicateConfidence
    strategy: str
    size: int | None = None
    workspace_ids: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def safe_for_destructive_resolution(self) -> bool:
        return self.confidence is DuplicateConfidence.HIGH

    @property
    def confidence_key(self) -> str:
        """i18n key describing how this group was matched.

        Both the manager dialog and the inspector pane have to say the same
        thing about a group, so the mapping lives with the evidence itself.
        """
        if self.confidence is not DuplicateConfidence.HIGH:
            return "duplicates_confidence_possible"
        if self.evidence is DuplicateEvidence.AUDIO_PAYLOAD:
            return "duplicates_confidence_same_audio"
        return "duplicates_confidence_same_file"


@dataclass(frozen=True)
class DuplicateScanResult:
    groups: tuple[DuplicateGroup, ...] = ()
    generation: int = 0
    request_id: int = 0
    cancelled: bool = False
    partial: bool = False
    warnings: tuple[tuple[str, str, str], ...] = ()  # path, stage, stable code

@dataclass(frozen=True)
class DuplicateHash:
    digest: str
    evidence: DuplicateEvidence


@dataclass(frozen=True)
class IssueFixDescriptor:
    """A stable action reference, never a translated label or an implicit value."""

    action_id: str
    fields: tuple[str, ...]
    requires_value: bool = False
    unambiguous: bool = False


@dataclass(frozen=True)
class MetadataIssue:
    id: str
    rule_id: str
    severity: ValidationSeverity
    category: ValidationCategory
    item_ids: tuple[int, ...]
    fields: tuple[str, ...]
    message_key: str
    message_args: tuple[tuple[str, object], ...] = ()
    evidence: tuple[tuple[str, object], ...] = ()
    display_paths: tuple[str, ...] = ()
    state: IssueState = IssueState.PRESENT_ON_DISK
    source: str = "validation"
    fix: IssueFixDescriptor | None = None
    generation: int = 0
    revision: int = 0

    @property
    def fixable(self) -> bool:
        return self.fix is not None


@dataclass(frozen=True)
class ValidationSnapshot:
    generation: int
    revision: int
    issues: tuple[MetadataIssue, ...]
    cancelled: bool = False
    error: str = ""
    content_revision: int = 0

    def current_for(self, workspace) -> bool:
        return (self.generation == workspace.generation
                and self.revision == workspace.change_set.revision
                and (not self.content_revision
                     or self.content_revision == workspace.content_revision))


@dataclass(frozen=True)
class ProblemFixPreview:
    """Immutable, no-mutation preview for an explicit Problems-center fix."""

    generation: int
    revision: int
    issue_ids: tuple[str, ...]
    item_ids: tuple[int, ...]
    action_preview: object
    field: str
    value: str
    skipped_ids: tuple[int, ...] = ()
    content_revision: int = 0


RuleEvaluator = Callable[[object, object, int], Iterable[MetadataIssue]]


@dataclass(frozen=True)
class MetadataValidationRule:
    id: str
    name_key: str
    description_key: str
    category: ValidationCategory
    severity: ValidationSeverity
    fields: tuple[str, ...]
    evaluator: RuleEvaluator


def _issue_id(rule_id: str, item_ids: Iterable[int], fields: Iterable[str], evidence: object) -> str:
    payload = "|".join((rule_id, ",".join(map(str, sorted(item_ids))),
                        ",".join(sorted(fields)), repr(evidence)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _make_issue(workspace, rule: MetadataValidationRule, item, identity: int, *,
                message_key: str, fields: tuple[str, ...] = (), evidence=(),
                severity: ValidationSeverity | None = None,
                category: ValidationCategory | None = None, source: str = "validation",
                fix: IssueFixDescriptor | None = None,
                state: IssueState = IssueState.PRESENT_ON_DISK) -> MetadataIssue:
    evidence_tuple = tuple(sorted((str(key), value) for key, value in dict(evidence).items()))
    fields = fields or rule.fields
    return MetadataIssue(
        id=_issue_id(rule.id, (identity,), fields, evidence_tuple), rule_id=rule.id,
        severity=severity or rule.severity, category=category or rule.category,
        item_ids=(identity,), fields=tuple(fields), message_key=message_key,
        evidence=evidence_tuple, display_paths=(str(item.path),), state=state, source=source,
        fix=fix, generation=workspace.generation, revision=workspace.change_set.revision,
    )


def _missing_field(field: str, message_key: str) -> RuleEvaluator:
    def evaluate(workspace, item, identity: int) -> Iterable[MetadataIssue]:
        if not item.metadata_editable:
            return ()
        effective = item.proposed.effective_tags(item.original)
        value = effective.field_value(field)
        if isinstance(value, str):
            missing = not value.strip()
        else:
            missing = value in (None, "")
        original = item.original.field_value(field)
        if not missing:
            if not (isinstance(original, str) and not original.strip()):
                return ()
            state, severity = IssueState.RESOLVED_BY_PENDING, ValidationSeverity.INFORMATION
        else:
            state = (IssueState.INTRODUCED_BY_PENDING if isinstance(original, str) and original.strip()
                     else IssueState.PRESENT_ON_DISK)
            severity = _RULE_BY_ID[f"metadata.{field}.required.v1"].severity
        rule = _RULE_BY_ID[f"metadata.{field}.required.v1"]
        return (_make_issue(
            workspace, rule, item, identity, message_key=message_key,
            evidence={"effective": value or "", "original": original or ""}, severity=severity, state=state,
            fix=IssueFixDescriptor("tag.set_field.v1", (field,), requires_value=True, unambiguous=False),
        ),)
    return evaluate


def _numbering(kind: str) -> RuleEvaluator:
    number_field, total_field = ("track_num", "track_total") if kind == "track" else ("disc_num", "disc_total")
    def evaluate(workspace, item, identity: int) -> Iterable[MetadataIssue]:
        if not item.metadata_editable:
            return ()
        effective = item.proposed.effective_tags(item.original)
        number, total = effective.field_value(number_field), effective.field_value(total_field)
        invalid = ((number is not None and number <= 0) or (total is not None and total <= 0)
                   or (number is not None and total is not None and number > total))
        if not invalid:
            return ()
        rule = _RULE_BY_ID[f"numbering.{kind}.invalid.v1"]
        return (_make_issue(workspace, rule, item, identity, message_key="meta_problem_numbering_invalid",
                            fields=(number_field, total_field), evidence={"number": number, "total": total}),)
    return evaluate


def _excluded(workspace, item, identity: int) -> Iterable[MetadataIssue]:
    if not (item.has_changes and item.excluded_from_apply):
        return ()
    rule = _RULE_BY_ID["pending.changed_excluded.v1"]
    return (_make_issue(workspace, rule, item, identity, message_key="meta_problem_changed_excluded",
                        evidence={"changed": True, "excluded": True}, severity=ValidationSeverity.WARNING,
                        source="change_set", state=IssueState.CHANGED_EXCLUDED),)


def _change_set_evidence(workspace, item, identity: int) -> Iterable[MetadataIssue]:
    rule = _RULE_BY_ID["pending.proposal_capability.v1"]
    issues = []
    for record in workspace.change_set.records(item_ids={identity}):
        if not (record.capability or record.diagnostic):
            continue
        category = ValidationCategory.FILENAME if record.field == "filename" else ValidationCategory.CAPABILITY
        issues.append(_make_issue(
            workspace, rule, item, identity, message_key="meta_problem_proposal_blocked",
            fields=(record.field,), evidence={"capability": record.capability, "diagnostic": record.diagnostic},
            severity=ValidationSeverity.BLOCKER, category=category, source="change_set", state=IssueState.PENDING_BLOCKER,
        ))
    return tuple(issues)


def _artwork_state(workspace, item, identity: int) -> Iterable[MetadataIssue]:
    artwork = item.proposed.effective_tags(item.original).artwork
    if artwork.read_state not in {ArtworkReadState.INVALID, ArtworkReadState.READ_FAILED}:
        return ()
    rule = _RULE_BY_ID["artwork.read_failed.v1"]
    return (_make_issue(workspace, rule, item, identity, message_key="meta_problem_artwork_invalid",
                        fields=("artwork",), evidence={"state": artwork.read_state.value},
                        severity=ValidationSeverity.WARNING, source="artwork"),)


def _external_change_state(workspace, item, identity: int) -> Iterable[MetadataIssue]:
    state = getattr(item, "external_state", "current")
    if state in {"current", "ignored_own_operation"}:
        return ()
    rule = _RULE_BY_ID["filesystem.external_change.v1"]
    blocking = bool(item.has_changes or state in {"stale_with_proposals", "conflict", "replaced"})
    severity = (ValidationSeverity.BLOCKER if blocking
                else ValidationSeverity.WARNING if state in {
                    "missing", "unreadable", "cloud_placeholder"}
                else ValidationSeverity.INFORMATION)
    return (_make_issue(
        workspace, rule, item, identity,
        message_key="meta_problem_external_change_body", fields=("filename",),
        evidence={"external_state": state}, severity=severity,
        category=ValidationCategory.FILENAME, source="filesystem",
        state=(IssueState.PENDING_BLOCKER if blocking
               else IssueState.PRESENT_ON_DISK)),)


def _rules() -> tuple[MetadataValidationRule, ...]:
    return (
        MetadataValidationRule("metadata.title.required.v1", "meta_problem_title", "meta_problem_title_body",
                               ValidationCategory.BASIC, ValidationSeverity.WARNING, ("title",),
                               _missing_field("title", "meta_problem_missing_title")),
        MetadataValidationRule("metadata.artist.required.v1", "meta_problem_artist", "meta_problem_artist_body",
                               ValidationCategory.BASIC, ValidationSeverity.WARNING, ("artist",),
                               _missing_field("artist", "meta_problem_missing_artist")),
        MetadataValidationRule("numbering.track.invalid.v1", "meta_problem_track", "meta_problem_track_body",
                               ValidationCategory.NUMBERING, ValidationSeverity.ERROR, ("track_num", "track_total"),
                               _numbering("track")),
        MetadataValidationRule("numbering.disc.invalid.v1", "meta_problem_disc", "meta_problem_disc_body",
                               ValidationCategory.NUMBERING, ValidationSeverity.ERROR, ("disc_num", "disc_total"),
                               _numbering("disc")),
        MetadataValidationRule("pending.changed_excluded.v1", "meta_problem_excluded", "meta_problem_excluded_body",
                               ValidationCategory.PENDING, ValidationSeverity.WARNING, (), _excluded),
        MetadataValidationRule("pending.proposal_capability.v1", "meta_problem_capability", "meta_problem_capability_body",
                               ValidationCategory.CAPABILITY, ValidationSeverity.BLOCKER, (), _change_set_evidence),
        MetadataValidationRule("artwork.read_failed.v1", "meta_problem_artwork", "meta_problem_artwork_body",
                               ValidationCategory.ARTWORK, ValidationSeverity.WARNING, ("artwork",), _artwork_state),
        MetadataValidationRule("filesystem.external_change.v1", "meta_problem_external_change",
                               "meta_problem_external_change_body", ValidationCategory.FILENAME,
                               ValidationSeverity.WARNING, ("filename",), _external_change_state),
    )


_RULES = _rules()
_RULE_BY_ID = {rule.id: rule for rule in _RULES}
if len(_RULE_BY_ID) != len(_RULES):
    raise RuntimeError("duplicate validation rule id")


class MetadataValidationEngine:
    """Deterministic, synchronous validation over an in-memory workspace."""

    @property
    def rules(self) -> tuple[MetadataValidationRule, ...]:
        return _RULES

    def validate(self, workspace, *, cancelled: Callable[[], bool] | None = None) -> ValidationSnapshot:
        generation, revision = workspace.generation, workspace.change_set.revision
        content_revision = workspace.content_revision
        issues: list[MetadataIssue] = []
        for item in workspace.tracks:
            if cancelled and cancelled():
                return ValidationSnapshot(
                    generation, revision, tuple(issues), cancelled=True,
                    content_revision=content_revision)
            identity = workspace.item_id(item)
            for rule in _RULES:
                issues.extend(rule.evaluator(workspace, item, identity))
        return ValidationSnapshot(
            generation, revision, tuple(sorted(issues, key=lambda issue: issue.id)),
            content_revision=content_revision)
