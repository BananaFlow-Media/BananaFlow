"""Pure declarative Tag Editor action registry; it never writes media or paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Callable, Mapping

from core.metadata_processor import clean_filename_to_title, extract_track_number
from core.tag_templates import TemplateError, compile_template, safe_filename


class ActionResultStatus(str, Enum):
    CHANGED = "changed"
    NO_OP = "no_op"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True)
class ActionParameter:
    id: str
    kind: str
    default: object = None
    required: bool = False
    choices: tuple[str, ...] = ()

    def validate(self, value: object) -> object:
        if self.required and value in (None, ""):
            raise ValueError(f"parameter_required:{self.id}")
        if self.kind in {"string", "template"}:
            return "" if value is None else str(value)
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"parameter_boolean:{self.id}")
            return value
        if self.kind == "integer":
            if isinstance(value, bool):
                raise ValueError(f"parameter_integer:{self.id}")
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"parameter_integer:{self.id}") from exc
        if self.kind == "choice":
            value = str(value)
            if value not in self.choices:
                raise ValueError(f"parameter_choice:{self.id}")
            return value
        raise ValueError(f"parameter_kind:{self.id}")


@dataclass(frozen=True)
class TagActionContext:
    item_id: int
    filename: str
    extension: str
    format_id: str
    values: Mapping[str, object]
    folder_name: str = ""
    parent_folder_name: str = ""
    editable: bool = True
    sequence_index: int = 1


@dataclass(frozen=True)
class ActionDelta:
    item_id: int
    fields: Mapping[str, object] = field(default_factory=dict)
    filename: str | None = None
    diagnostic: str = ""
    status: ActionResultStatus = ActionResultStatus.CHANGED
    warnings: tuple[str, ...] = ()


Evaluator = Callable[[TagActionContext, Mapping[str, object]], ActionDelta]


@dataclass(frozen=True)
class TagAction:
    id: str
    name_key: str
    description_key: str
    category: str
    scopes: frozenset[str]
    formats: frozenset[str]
    reads: frozenset[str]
    writes: frozenset[str]
    evaluator: Evaluator
    parameters: tuple[ActionParameter, ...] = ()
    renames: bool = False
    requires_editable: bool = True

    @property
    def scope(self) -> str:
        return "selected" if "selected" in self.scopes else next(iter(self.scopes), "selected")

    @property
    def parameter_defaults(self) -> dict[str, object]:
        return {parameter.id: parameter.default for parameter in self.parameters}

    def evaluate(self, context: TagActionContext, parameters: Mapping[str, object] | None = None) -> ActionDelta:
        if self.requires_editable and not context.editable:
            return ActionDelta(context.item_id, diagnostic="unsupported_item",
                               status=ActionResultStatus.UNSUPPORTED)
        if self.formats and context.format_id not in self.formats:
            return ActionDelta(context.item_id, diagnostic="unsupported_format",
                               status=ActionResultStatus.UNSUPPORTED)
        supplied = dict(parameters or {})
        known = {parameter.id for parameter in self.parameters}
        unknown = set(supplied) - known
        if unknown:
            return ActionDelta(context.item_id, diagnostic="unknown_parameter:" + ",".join(sorted(unknown)),
                               status=ActionResultStatus.BLOCKER)
        try:
            merged = {parameter.id: parameter.validate(supplied.get(parameter.id, parameter.default))
                      for parameter in self.parameters}
            return self.evaluator(context, merged)
        except (ValueError, TemplateError) as exc:
            return ActionDelta(context.item_id, diagnostic=str(exc), status=ActionResultStatus.BLOCKER)


class TagActionRegistry:
    def __init__(self, actions: tuple[TagAction, ...] = ()) -> None:
        self._actions: dict[str, TagAction] = {}
        for action in actions:
            self.register(action)

    def register(self, action: TagAction) -> None:
        if not action.id or action.id in self._actions:
            raise ValueError(f"duplicate_or_empty_action_id:{action.id}")
        self._actions[action.id] = action

    def get(self, action_id: str) -> TagAction:
        return self._actions[self.resolve_id(action_id)]

    def resolve_id(self, action_id: str) -> str:
        """Return the canonical stable ID for a supported legacy alias."""
        aliases = {
            "filename_to_tags": "template.filename_to_tags.v1",
            "tags_to_filename": "template.tags_to_filename.v1",
        }
        resolved = aliases.get(action_id, action_id)
        if resolved not in self._actions:
            raise KeyError(action_id)
        return resolved

    def actions(self) -> tuple[TagAction, ...]:
        return tuple(self._actions.values())


def _filename_to_tags(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    try:
        parsed = compile_template(str(parameters.get("template", "")), direction="filename_to_tags").parse(context.filename.rsplit(".", 1)[0])
    except TemplateError as exc:
        return ActionDelta(context.item_id, diagnostic=str(exc), status=ActionResultStatus.SKIPPED)
    values: dict[str, object] = dict(parsed)
    for field in ("track_num", "disc_num"):
        if field in values:
            try:
                values[field] = int(values[field])
            except ValueError:
                return ActionDelta(context.item_id, diagnostic=f"invalid_{field}",
                                   status=ActionResultStatus.BLOCKER)
    if not parameters.get("overwrite", True):
        values = {key: value for key, value in values.items() if context.values.get(key) in (None, "", ())}
    return _field_delta(context, values)


def _tags_to_filename(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    try:
        values = {**context.values, "original_stem": context.filename.rsplit(".", 1)[0]}
        stem = compile_template(str(parameters.get("template", "")), direction="tags_to_filename").render(values)
        filename = safe_filename(stem, context.extension, sanitize=bool(parameters.get("sanitize", True)))
        return _filename_delta(context, filename)
    except TemplateError as exc:
        return ActionDelta(context.item_id, diagnostic=str(exc), status=ActionResultStatus.BLOCKER)


def _field_delta(context: TagActionContext, proposed: Mapping[str, object]) -> ActionDelta:
    changed = {key: value for key, value in proposed.items() if context.values.get(key) != value}
    if not changed:
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    return ActionDelta(context.item_id, fields=changed)


def _filename_delta(context: TagActionContext, filename: str) -> ActionDelta:
    if filename == context.filename:
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    return ActionDelta(context.item_id, filename=filename)


def _auto_arrange(context: TagActionContext, _parameters: Mapping[str, object]) -> ActionDelta:
    values: dict[str, object] = {}
    title = clean_filename_to_title(context.filename)
    if title:
        values["title"] = title
    track = extract_track_number(context.filename)
    if track is not None:
        values["track_num"] = track
    if context.folder_name:
        values["album"] = context.folder_name
    return _field_delta(context, values)


def _title_from_filename(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    title = (clean_filename_to_title(context.filename) if parameters["strip_numbering"]
             else context.filename.rsplit(".", 1)[0])
    return _field_delta(context, {"title": title})


def _track_from_filename(context: TagActionContext, _parameters: Mapping[str, object]) -> ActionDelta:
    value = extract_track_number(context.filename)
    return (ActionDelta(context.item_id, diagnostic="number_not_found", status=ActionResultStatus.SKIPPED)
            if value is None else _field_delta(context, {"track_num": value}))


def _split_artist_title(context: TagActionContext, _parameters: Mapping[str, object]) -> ActionDelta:
    matched = re.match(r"^(.+?)\s*[-–—]\s*(.+)$", context.filename.rsplit(".", 1)[0])
    if not matched:
        return ActionDelta(context.item_id, diagnostic="filename_pattern_not_matched",
                           status=ActionResultStatus.SKIPPED)
    return _field_delta(context, {"artist": matched.group(1).strip(), "title": matched.group(2).strip()})


def _copy_album_artist(context: TagActionContext, _parameters: Mapping[str, object]) -> ActionDelta:
    artist = context.values.get("artist")
    if not artist:
        return ActionDelta(context.item_id, diagnostic="artist_missing", status=ActionResultStatus.SKIPPED)
    return _field_delta(context, {"album_artist": artist})


def _clear_field(field_name: str) -> Evaluator:
    value = -1 if field_name == "track_num" else ""
    return lambda context, _parameters: _field_delta(context, {field_name: value})


def _normalize_spaces(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    field_name = parameters.get("field", "title")
    value = context.values.get(field_name)
    if not value or not isinstance(value, str):
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    normalized = " ".join(value.replace("_", " ").split())
    return _field_delta(context, {str(field_name): normalized})


_WEB_JUNK_TERMS = (
    r"Official\s*(?:Music\s*)?(?:Video|Audio|Lyrics?|MV)|Lyrics?(?:\s*Video)?|HD|HQ|4K|"
    r"Visualizer|Remastered(?:\s*\d{4})?|Live(?:\s*Version)?|Cover|Remix|Acoustic|"
    r"Instrumental|Sped\s*up|Slowed(?:\s*\+\s*Reverb)?|Vevo|"
    r"8D(?:\s*Audio)?|360(?:\s*Audio)?|Extended|Radio\s*Edit|Unplugged"
)
_HEBREW_JUNK_TERMS = (
    r"מוזיקה\s*רשמית|קליפ\s*רשמי|קאבר|רמיקס|הופעה\s*חיה|מילים|"
    r"קליפ\s*מילים|לייב|ביצוע\s*אקוסטי|קריוקי|גרסת\s*כיסוי|אודיו|הקלטה"
)


def _strip_junk(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    value = context.values.get("title")
    if not value or not isinstance(value, str):
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    terms = []
    if parameters["remove_web_junk"]:
        terms.append(_WEB_JUNK_TERMS)
    if parameters["remove_hebrew"]:
        terms.append(_HEBREW_JUNK_TERMS)
    if not terms:
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    pattern = "|".join(terms)
    cleaned = re.sub(rf"\s*[\[(](?:{pattern})[^\])]*[\])]", "", value, flags=re.IGNORECASE)
    cleaned = re.sub(rf"\s*[-|]\s*(?:{pattern})(?:\s*[-|]|\s*$)", "", cleaned, flags=re.IGNORECASE)
    if parameters["fix_punctuation"]:
        cleaned = re.sub(r"[-|]\s*$", "", cleaned).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return _field_delta(context, {"title": cleaned}) if cleaned else ActionDelta(
        context.item_id, diagnostic="cleanup_would_empty_title", status=ActionResultStatus.WARNING)


def _filename_from_title(context: TagActionContext, _parameters: Mapping[str, object]) -> ActionDelta:
    title = context.values.get("title")
    if not title:
        return ActionDelta(context.item_id, diagnostic="title_missing", status=ActionResultStatus.SKIPPED)
    return _filename_delta(context, safe_filename(str(title), context.extension))


def _clean_filename(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    stem = context.filename.rsplit(".", 1)[0]
    cleaned = stem
    if parameters["remove_domains"]:
        cleaned = re.sub(r"(?i)\b(?:yt1s\.com|y2mate\.com|\[SPOTIFY-DL\]|ytdownloader)\s*[-|]?\s*", "", cleaned)
    if parameters["smart_brackets"]:
        cleaned = re.sub(
            rf"\s*[\[(](?:{_WEB_JUNK_TERMS}|{_HEBREW_JUNK_TERMS})[^\])]*[\])]",
            "", cleaned, flags=re.IGNORECASE,
        )
    else:
        cleaned = re.sub(r"\s*[\[(].*?[\])]", "", cleaned)
    if parameters["remove_emojis"]:
        cleaned = re.sub(r"[\U00010000-\U0010ffff]", "", cleaned)
    if parameters["fix_spaces"]:
        cleaned = re.sub(r"\s+", " ", cleaned.replace("_", " ")).strip(" .-")
    return _filename_delta(context, safe_filename(cleaned, context.extension))


def _strip_filename_numbering(context: TagActionContext, _parameters: Mapping[str, object]) -> ActionDelta:
    stem = context.filename.rsplit(".", 1)[0]
    cleaned = re.sub(r"^\s*\d+\s*[-_.]?\s*", "", stem).strip()
    return _filename_delta(context, safe_filename(cleaned, context.extension))


def _set_fields(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    field_name = str(parameters["field"])
    return _field_delta(context, {field_name: parameters["value"]})


def _set_artist(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    artist = parameters["value"]
    proposed = {"artist": artist}
    if not context.values.get("album_artist"):
        proposed["album_artist"] = artist
    return _field_delta(context, proposed)


def _replace_text(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    field_name, needle, replacement = str(parameters["field"]), str(parameters["find"]), str(parameters["replace"])
    value = context.values.get(field_name)
    if not isinstance(value, str) or not needle:
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    if parameters["case_sensitive"]:
        changed = value.replace(needle, replacement)
    else:
        changed = re.sub(re.escape(needle), lambda _match: replacement, value, flags=re.IGNORECASE)
    return _field_delta(context, {field_name: changed})


# Legacy ID3v1/ID3v2 frames written by older Windows software store bytes in a
# local code page but declare Latin-1, so a correct reader decodes them into
# mojibake.  Re-encoding as Latin-1 and decoding with the real code page undoes
# exactly that.  Hebrew is cp1255; the others are here because the same tool
# chain produced the same fault in other locales.
MOJIBAKE_CODEPAGES: tuple[str, ...] = ("cp1255", "cp1251", "cp1256", "cp1253", "utf-8")


def repair_mojibake(value: str, codepage: str) -> str | None:
    """Undo one Latin-1 mis-decode, or return None when there is nothing to fix.

    Returns None rather than the input whenever the round trip is impossible or
    produces nothing better, so a caller can distinguish "already correct" from
    "repaired".  This is intentionally conservative: silently rewriting a tag
    that was fine is worse than leaving a broken one alone.
    """
    if not value:
        return None
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        # Characters outside Latin-1 mean the text was never mis-decoded this
        # way -- it is genuine Unicode and must be left alone.
        return None
    try:
        repaired = raw.decode(codepage)
    except (UnicodeDecodeError, LookupError):
        return None
    if repaired == value or not repaired.strip():
        return None
    # A repair that still leaves replacement characters has not recovered the
    # text; treat it as a failure rather than trading one kind of damage for
    # another.
    if "�" in repaired:
        return None
    return repaired


def _repair_encoding(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    """Recover text stored under a legacy code page but read as Latin-1."""
    codepage = str(parameters["codepage"])
    fields = TEXT_FIELDS if parameters["all_fields"] else (str(parameters["field"]),)
    proposed: dict[str, object] = {}
    for name in fields:
        value = context.values.get(name)
        if isinstance(value, str):
            repaired = repair_mojibake(value, codepage)
            if repaired is not None:
                proposed[name] = repaired
    if not proposed:
        return ActionDelta(context.item_id, diagnostic="nothing_to_repair",
                           status=ActionResultStatus.NO_OP)
    return _field_delta(context, proposed)


def _replace_regex(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    """Regular-expression replace over one field.

    A half-typed pattern is normal while the preview refreshes on every
    keystroke, so a malformed pattern -- or a replacement referring to a group
    that does not exist -- is reported as a warning on that item instead of
    raising and taking the dialog down.

    Not handled: a pathological pattern such as ``(a+)+b`` can still backtrack
    for a long time, and the preview runs on the UI thread.  Bounding that
    needs a regex engine with a timeout, which this does not have.
    """
    field_name = str(parameters["field"])
    pattern, replacement = str(parameters["pattern"]), str(parameters["replace"])
    value = context.values.get(field_name)
    if not isinstance(value, str) or not pattern:
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    flags = 0 if parameters["case_sensitive"] else re.IGNORECASE
    try:
        changed = re.sub(pattern, replacement, value, flags=flags)
    except re.error:
        return ActionDelta(context.item_id, diagnostic="invalid_pattern",
                           status=ActionResultStatus.WARNING)
    return _field_delta(context, {field_name: changed})


def _split_field(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    """Split one field on a separator and put each half in its own field.

    Only the first occurrence splits, so ``Artist - Song - Live`` keeps the
    tail with the second half instead of silently losing it.  An absent
    separator is a no-op, never a destructive "write the whole thing to both".
    """
    source, target = str(parameters["field"]), str(parameters["target_field"])
    separator = str(parameters["separator"])
    value = context.values.get(source)
    if not isinstance(value, str) or not separator or separator not in value:
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    head, _, tail = value.partition(separator)
    head, tail = head.strip(), tail.strip()
    if not head or not tail:
        return ActionDelta(context.item_id, diagnostic="empty_half",
                           status=ActionResultStatus.WARNING)
    # target_first swaps which half stays put: "Artist - Title" in the title
    # field wants the artist moved out, "Title - Artist" wants the opposite.
    if parameters["target_first"]:
        head, tail = tail, head
    if source == target:
        return _field_delta(context, {source: head})
    return _field_delta(context, {source: head, target: tail})


def _change_case(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    field_name, mode = str(parameters["field"]), str(parameters["mode"])
    value = context.values.get(field_name)
    if not isinstance(value, str):
        return ActionDelta(context.item_id, status=ActionResultStatus.NO_OP)
    changed = {"upper": value.upper(), "lower": value.lower(), "title": value.title(),
               "sentence": value[:1].upper() + value[1:].lower() if value else value}[mode]
    return _field_delta(context, {field_name: changed})


def _number_tracks(context: TagActionContext, parameters: Mapping[str, object]) -> ActionDelta:
    value = int(parameters["start"]) + (context.sequence_index - 1) * int(parameters["step"])
    return _field_delta(context, {"track_num": value})


TEXT_FIELDS = ("title", "artist", "album", "album_artist", "genre", "year", "comment")
ALL_SCOPES = frozenset({"current", "selected", "visible", "active_folder"})


def _action(action_id: str, label: str, description: str, category: str,
            reads: set[str], writes: set[str], evaluator: Evaluator, *, parameters=(), renames=False,
            requires_editable=True) -> TagAction:
    return TagAction(action_id, label, description, category, ALL_SCOPES, frozenset(),
                     frozenset(reads), frozenset(writes), evaluator, tuple(parameters), renames,
                     requires_editable)


def builtin_registry() -> TagActionRegistry:
    bool_param = lambda name, default=True: ActionParameter(name, "boolean", default)
    field_param = ActionParameter("field", "choice", "title", choices=TEXT_FIELDS)
    actions = [
        _action("tag.auto_arrange.v1", "meta_action_auto_arrange", "meta_action_auto_arrange_desc", "organize",
                {"filename", "title", "album", "track_num"}, {"title", "album", "track_num"}, _auto_arrange),
        _action("tag.title_from_filename.v1", "meta_op_title_strip_label", "meta_op_title_strip_desc", "filename_to_tags",
                {"filename", "title"}, {"title"}, _title_from_filename,
                parameters=(bool_param("strip_numbering", True),)),
        _action("tag.track_from_filename.v1", "meta_op_track_num_label", "meta_op_track_num_desc", "filename_to_tags",
                {"filename", "track_num"}, {"track_num"}, _track_from_filename),
        _action("tag.split_artist_title.v1", "meta_op_split_at_label", "meta_op_split_at_desc", "filename_to_tags",
                {"filename", "artist", "title"}, {"artist", "title"}, _split_artist_title),
        _action("tag.album_artist_from_artist.v1", "meta_op_album_artist_label", "meta_op_album_artist_desc", "organize",
                {"artist", "album_artist"}, {"album_artist"}, _copy_album_artist),
        _action("tag.normalize_spaces.v1", "meta_op_normalize_spaces_label", "meta_op_normalize_spaces_desc", "cleanup",
                set(TEXT_FIELDS), set(TEXT_FIELDS), _normalize_spaces, parameters=(field_param,)),
        _action("tag.strip_web_junk.v1", "meta_op_strip_junk_label", "meta_op_strip_junk_desc", "cleanup",
                {"title"}, {"title"}, _strip_junk,
                parameters=(bool_param("remove_web_junk"), bool_param("remove_hebrew"),
                            bool_param("fix_punctuation"))),
        _action("file.from_title.v1", "meta_action_filename_from_title", "meta_action_filename_from_title_desc", "filename",
                {"title", "filename"}, {"filename"}, _filename_from_title, renames=True),
        _action("file.clean.v1", "meta_op_clean_filename_label", "meta_op_clean_filename_desc", "filename",
                {"filename"}, {"filename"}, _clean_filename,
                parameters=(bool_param("smart_brackets"), bool_param("remove_domains"),
                            bool_param("remove_emojis"), bool_param("fix_spaces")), renames=True),
        _action("file.strip_numbering.v1", "meta_op_strip_filename_numbering_label", "meta_op_strip_filename_numbering_desc", "filename",
                {"filename"}, {"filename"}, _strip_filename_numbering, renames=True),
        _action("template.filename_to_tags.v1", "meta_action_filename_to_tags", "meta_action_filename_to_tags_desc", "template",
                {"filename", *TEXT_FIELDS, "track_num", "disc_num"}, set(TEXT_FIELDS) | {"track_num", "disc_num"}, _filename_to_tags,
                parameters=(ActionParameter("template", "template", required=True), bool_param("overwrite", False))),
        _action("template.tags_to_filename.v1", "meta_action_tags_to_filename", "meta_action_tags_to_filename_desc", "template",
                {"filename", *TEXT_FIELDS, "track_num", "disc_num", "composer"}, {"filename"}, _tags_to_filename,
                parameters=(ActionParameter("template", "template", required=True), bool_param("sanitize", True)), renames=True),
        _action("tag.set_field.v1", "meta_action_set_field", "meta_action_set_field_desc", "edit",
                set(TEXT_FIELDS), set(TEXT_FIELDS), _set_fields,
                parameters=(field_param, ActionParameter("value", "string", required=True))),
        _action("tag.set_artist.v1", "meta_action_set_artist", "meta_action_set_artist_desc", "edit",
                {"artist", "album_artist"}, {"artist", "album_artist"}, _set_artist,
                parameters=(ActionParameter("value", "string", required=True),)),
        _action("tag.replace_text.v1", "meta_action_replace", "meta_action_replace_desc", "cleanup",
                set(TEXT_FIELDS), set(TEXT_FIELDS), _replace_text,
                parameters=(field_param, ActionParameter("find", "string", required=True),
                            ActionParameter("replace", "string", ""), bool_param("case_sensitive", False))),
        _action("tag.repair_encoding.v1", "meta_action_repair_encoding",
                "meta_action_repair_encoding_desc", "cleanup",
                set(TEXT_FIELDS), set(TEXT_FIELDS), _repair_encoding,
                parameters=(field_param, bool_param("all_fields", True),
                            ActionParameter("codepage", "choice", "cp1255",
                                            choices=MOJIBAKE_CODEPAGES))),
        _action("tag.replace_regex.v1", "meta_action_replace_regex", "meta_action_replace_regex_desc", "cleanup",
                set(TEXT_FIELDS), set(TEXT_FIELDS), _replace_regex,
                parameters=(field_param, ActionParameter("pattern", "string", required=True),
                            ActionParameter("replace", "string", ""), bool_param("case_sensitive", False))),
        _action("tag.split_field.v1", "meta_action_split_field", "meta_action_split_field_desc", "cleanup",
                set(TEXT_FIELDS), set(TEXT_FIELDS), _split_field,
                parameters=(field_param,
                            ActionParameter("separator", "string", " - ", required=True),
                            ActionParameter("target_field", "choice", "artist", choices=TEXT_FIELDS),
                            bool_param("target_first", False))),
        _action("tag.change_case.v1", "meta_action_case", "meta_action_case_desc", "cleanup",
                set(TEXT_FIELDS), set(TEXT_FIELDS), _change_case,
                parameters=(field_param, ActionParameter("mode", "choice", "title",
                                                         choices=("upper", "lower", "title", "sentence")))),
        _action("tag.number_tracks.v1", "meta_action_number", "meta_action_number_desc", "organize",
                {"track_num"}, {"track_num"}, _number_tracks,
                parameters=(ActionParameter("start", "integer", 1), ActionParameter("step", "integer", 1))),
    ]
    clear_fields = {
        "comments": "comment", "track_num": "track_num", "year": "year", "genre": "genre",
        "title": "title", "artist": "artist", "album": "album", "album_artist": "album_artist",
    }
    for suffix, field_name in clear_fields.items():
        actions.append(_action(
            f"tag.clear_{suffix}.v1", f"meta_op_clear_{suffix}_label", f"meta_op_clear_{suffix}_desc",
            "clear", {field_name}, {field_name}, _clear_field(field_name),
        ))
    return TagActionRegistry(tuple(actions))


LEGACY_ACTION_IDS = {
    "title_strip": "tag.title_from_filename.v1",
    "title_full": "tag.title_from_filename.v1",
    "normalize_spaces": "tag.normalize_spaces.v1",
    "track_num": "tag.track_from_filename.v1",
    "split_at": "tag.split_artist_title.v1",
    "album_artist": "tag.album_artist_from_artist.v1",
    "strip_junk": "tag.strip_web_junk.v1",
    "clear_comments": "tag.clear_comments.v1",
    "clear_track_num": "tag.clear_track_num.v1",
    "clear_year": "tag.clear_year.v1",
    "clear_genre": "tag.clear_genre.v1",
    "clear_title": "tag.clear_title.v1",
    "clear_artist": "tag.clear_artist.v1",
    "clear_album": "tag.clear_album.v1",
    "clear_album_artist": "tag.clear_album_artist.v1",
    "clean_filename": "file.clean.v1",
    "strip_filename_numbering": "file.strip_numbering.v1",
    "rename_from_title": "file.from_title.v1",
}
