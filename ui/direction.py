"""
ui/direction.py
Layout-direction helpers for surgical LTR within an RTL app.

The application's global layout direction follows the user's language
(``Qt.RightToLeft`` for Hebrew, ``Qt.LeftToRight`` for English) via
``apply_app_direction`` below. These helpers let individual widgets that
hold technical content (URLs, file paths, codec values, etc.) opt out
of the global direction so their content stays readable.

See :mod:`ui.i18n` for the central language coordinator that calls
``apply_app_direction``.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QWidget


def force_ltr(widget: QWidget) -> None:
    """Force LTR on a widget and all its descendants.

    Use for: containers, ComboBoxes with Latin values (codec/quality),
    tables or rows where technical content dominates.
    """
    widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)


def force_ltr_input(line_edit: QLineEdit) -> None:
    """Force LTR + left alignment on a single-line text input.

    Use for: URL fields, output path fields, proxy URL fields,
    API token fields, file-path text fields.

    Alignment must be set explicitly because under an RTL parent the
    default ``AlignLeading`` evaluates to right-aligned, which would
    leave the cursor and text on the wrong side.
    """
    line_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    line_edit.setAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )


def force_ltr_label(label: QLabel) -> None:
    """Force LTR + left alignment on a QLabel showing a path/URL/technical string."""
    label.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    label.setAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )


def apply_app_direction(app: QApplication, lang: str) -> None:
    """Apply the app-wide layout direction for ``lang``.

    Called by :func:`ui.i18n.apply_language`. Hebrew uses RTL; everything
    else defaults to LTR.
    """
    direction = (
        Qt.LayoutDirection.RightToLeft
        if lang == "he"
        else Qt.LayoutDirection.LeftToRight
    )
    app.setLayoutDirection(direction)


_LTR_ISOLATE = "\u2066"
_POP_DIRECTIONAL_ISOLATE = "\u2069"

def isolate_ltr(text: str) -> str:
    """Keep technical Latin snippets stable inside Hebrew/RTL UI text."""
    return f"{_LTR_ISOLATE}{text}{_POP_DIRECTIONAL_ISOLATE}"


def isolate_number(value: object) -> str:
    """Isolate a formatted number, size, or timestamp for RTL display.

    Numeric display strings are the one category that is *always*
    left-to-right regardless of the surrounding language: ``2026-03-05
    14:30``, ``12.4 MB``, ``1,204 bytes``. Left un-isolated inside Hebrew
    prose, Unicode's bidi algorithm reorders their neutral characters
    against the paragraph direction — a date can render as ``14:30
    2026-03-05``, a size as ``MB 12.4`` — which reads as a different value
    rather than as a formatting quirk (issue #43).

    Unlike :func:`isolate_latin` this does not inspect the content: a
    formatted number has no Hebrew-content case to preserve, so there is
    nothing to decide.
    """
    text = str(value)
    return isolate_ltr(text) if text else text


def isolate_latin(text: str) -> str:
    """Isolate Latin/technical text, but leave Hebrew content to Qt's bidi.

    Use for values whose direction is not known in advance \u2014 a metadata field
    can hold a path or a codec (which must read left-to-right inside an RTL
    layout) or a Hebrew title (which must not be forced left-to-right).
    Isolating unconditionally would mangle the second case.
    """
    text = str(text)
    has_ascii = any(("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("a" <= ch <= "z")
                    for ch in text)
    has_hebrew = any("\u0590" <= ch <= "\u05ff" for ch in text)
    return isolate_ltr(text) if has_ascii and not has_hebrew else text


#: Fields whose value is a technical/machine identifier: it must read
#: left-to-right inside an RTL layout regardless of what characters it
#: happens to contain.  A Hebrew filename (``שיר.mp3``) is one technical
#: unit, not prose -- it is not made "Hebrew content" by containing Hebrew
#: characters, so a content-only heuristic (see :func:`isolate_latin`)
#: cannot classify it correctly and the field identity must decide instead.
_TECHNICAL_FIELDS = frozenset({
    "filename", "proposed_filename", "path", "absolute_path", "relative_path",
    "extension", "url", "hash", "identifier", "musicbrainz_id", "mbid",
    "isrc", "mime_type", "format_id", "codec", "dimensions", "sample_rate",
    "bitrate", "file_size", "size_bytes", "modified_time_ns", "timestamp",
    "build", "commit",
    "replaygain_track_gain", "replaygain_track_peak",
    "replaygain_album_gain", "replaygain_album_peak",
    "replaygain_reference_loudness",
})

#: Fields the user writes as prose: keep Unicode's own bidi behaviour, and
#: never force them left-to-right just because they contain a digit, a year
#: or a Latin word.
_NATURAL_LANGUAGE_FIELDS = frozenset({
    "title", "artist", "album", "album_artist", "genre", "composer",
    "publisher", "copyright", "comment", "lyrics", "grouping",
    "sort_title", "sort_artist", "sort_album", "sort_album_artist",
})


def _flatten_field_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (tuple, list)):
        return "; ".join(str(part) for part in value)
    return str(value)


def isolate_value_for_field(field_id: str | None, value: object) -> str:
    """Isolate ``value`` for display using the *field's* type, not the
    characters the value happens to contain, to decide its direction.

    A value's alphabet alone cannot say what direction it needs: a Hebrew
    filename (``שיר.mp3``) is a technical value and must stay one
    left-to-right unit even though it contains Hebrew, while a Hebrew title
    that contains a Latin word or a year must keep its own natural
    bidirectional behaviour rather than being forced left-to-right. A field
    outside both known vocabularies falls back to the same content-based
    heuristic :func:`isolate_latin` already uses.
    """
    text = _flatten_field_value(value)
    if not text:
        return text
    key = str(field_id or "")
    if key in _TECHNICAL_FIELDS:
        return isolate_ltr(text)
    if key in _NATURAL_LANGUAGE_FIELDS:
        return text
    return isolate_latin(text)


def display_part(key: str | None) -> str:
    from ui.i18n import t
    if not key:
        return ""
    return isolate_latin(t(key))

def quality_display(label_key: str, detail_key: str | None) -> str:
    """Build a localized quality row while isolating Latin/numeric fragments."""
    label = display_part(label_key)
    detail = display_part(detail_key)
    return f"{label} · {detail}" if detail else label
