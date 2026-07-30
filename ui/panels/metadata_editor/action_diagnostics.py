"""Localized presentation of stable Phase 9 action diagnostic codes."""

from __future__ import annotations

from ui.i18n import t


_FIELD_KEYS = {
    "title": "meta_field_title",
    "artist": "meta_field_artist",
    "album": "meta_field_album",
    "album_artist": "meta_field_album_artist",
    "track_num": "meta_field_track_num",
    "disc_num": "meta_field_disc_num",
    "year": "meta_field_year",
    "genre": "meta_field_genre",
    "comment": "meta_field_comment",
    "composer": "meta_field_composer",
    "filename": "meta_field_filename",
}
_PARAMETER_KEYS = {
    key: "meta_action_param_" + key for key in (
        "template", "overwrite", "sanitize", "strip_numbering", "field", "value",
        "find", "replace", "case_sensitive", "mode", "start", "step",
        "smart_brackets", "remove_domains", "remove_emojis", "fix_spaces",
        "remove_web_junk", "remove_hebrew", "fix_punctuation",
        "pattern", "separator", "target_field", "target_first",
        "codepage", "all_fields",
    )
}
_RENAME_KEYS = {
    "rename_collision": "meta_action_diag_rename_collision",
    "rename_reserved": "meta_action_diag_rename_reserved",
    "rename_invalid": "meta_action_diag_rename_invalid",
    "rename_escape": "meta_action_diag_rename_escape",
    "rename_locked": "meta_action_diag_rename_locked",
    "rename_failed": "meta_action_diag_rename_failed",
    "rename_blocked_sibling": "meta_action_diag_rename_blocked_sibling",
    "rename_rollback_failed": "meta_action_diag_rename_failed",
}
_SIMPLE_KEYS = {
    "optional_segments_not_parseable": "meta_action_diag_optional_parse",
    "template_does_not_match": "meta_action_diag_template_no_match",
    "invalid_direction": "meta_action_diag_invalid_template",
    "empty_template": "meta_action_diag_empty_template",
    "invalid_optional_segment": "meta_action_diag_invalid_optional",
    "template_has_no_fields": "meta_action_diag_template_no_fields",
    "repeated_field_is_ambiguous": "meta_action_diag_repeated_field",
    "adjacent_fields_are_ambiguous": "meta_action_diag_adjacent_field",
    "invalid_placeholder": "meta_action_diag_invalid_placeholder",
    "invalid_filename_characters": "meta_action_diag_invalid_filename_chars",
    "empty_filename": "meta_action_diag_empty_filename",
    "reserved_filename": "meta_action_diag_reserved_filename",
    "invalid_extension": "meta_action_diag_invalid_extension",
    "filename_too_long": "meta_action_diag_filename_too_long",
    "unsupported_item": "meta_action_diag_unsupported_item",
    "unsupported_format": "meta_action_diag_unsupported_format",
    "number_not_found": "meta_action_diag_number_not_found",
    "filename_pattern_not_matched": "meta_action_diag_filename_pattern",
    "artist_missing": "meta_action_diag_artist_missing",
    "title_missing": "meta_action_diag_title_missing",
    "cleanup_would_empty_title": "meta_action_diag_cleanup_empty_title",
    "invalid_track_num": "meta_action_diag_invalid_track_num",
    "invalid_disc_num": "meta_action_diag_invalid_disc_num",
    "invalid_pattern": "meta_action_diag_invalid_pattern",
    "empty_half": "meta_action_diag_empty_half",
    "nothing_to_repair": "meta_action_diag_nothing_to_repair",
}


def _field_name(field: str) -> str:
    return t(_FIELD_KEYS.get(field, "meta_field_value")).rstrip(":")


def _parameter_name(parameter: str) -> str:
    return t(_PARAMETER_KEYS.get(parameter, "meta_action_parameters"))


def format_action_diagnostic(code: object, *, filename: str = "") -> str:
    """Turn a stable engine/planner code into safe human-facing UI text.

    Core continues to retain codes such as ``missing_value:artist``. Unknown
    codes deliberately use a translated generic explanation rather than
    exposing implementation syntax in the Details column.
    """
    raw = str(code or "").strip()
    if not raw:
        return ""
    name, separator, argument = raw.partition(":")
    argument = argument.strip()

    if name in _RENAME_KEYS:
        return t(_RENAME_KEYS[name])
    if name == "missing_value":
        return t("meta_action_diag_missing_value", field=_field_name(argument))
    if name == "unknown_field":
        return t("meta_action_diag_unknown_field", token="{" + argument + "}")
    if name == "invalid_numeric_value":
        return t("meta_action_diag_invalid_numeric", field=_field_name(argument))
    if name == "unknown_parameter":
        return t("meta_action_diag_unknown_parameter", parameter=_parameter_name(argument.split(",")[0]))
    if name == "parameter_required":
        return t("meta_action_diag_parameter_required", parameter=_parameter_name(argument))
    if name in {"parameter_boolean", "parameter_integer", "parameter_choice", "parameter_kind"}:
        return t("meta_action_diag_invalid_parameter", parameter=_parameter_name(argument))
    if name in _SIMPLE_KEYS:
        return t(_SIMPLE_KEYS[name])
    if separator and name.startswith("unsupported"):
        return t("meta_action_diag_unsupported")
    return t("meta_action_diag_unknown")
