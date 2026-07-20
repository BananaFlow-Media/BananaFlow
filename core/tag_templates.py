"""Safe, deterministic filename/tag template parsing and rendering.

The grammar is intentionally small: literals, ``{field}``, numeric padding
(``{track_num:02}``) and optional segments (``[ - {album}]``).  It is not an
expression language and never evaluates code.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


SUPPORTED_FIELDS = frozenset({
    "title", "artist", "album", "album_artist", "track_num", "disc_num", "year", "genre",
    "comment", "composer", "original_stem",
})
_TOKEN = re.compile(r"\{([a-z_]+)(?::(0?[1-9]\d?))?\}")
_OPTIONAL = re.compile(r"\[([^\[\]]*)\]")
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BIDI_CONTROLS = dict.fromkeys(range(0x202A, 0x202F)) | dict.fromkeys(range(0x2066, 0x206A))
MAX_FILENAME_LENGTH = 255


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class Template:
    source: str
    fields: tuple[str, ...]
    direction: str

    def render(self, values: dict[str, object]) -> str:
        normalized = {key: _clean_text(value) for key, value in values.items()}

        def replace(match: re.Match) -> str:
            field = match.group(1)
            value = normalized.get(field, "")
            width = match.group(2)
            if width and value:
                try:
                    return str(int(value)).zfill(int(width))
                except (TypeError, ValueError) as exc:
                    raise TemplateError(f"invalid_numeric_value:{field}") from exc
            return value

        def optional(match: re.Match) -> str:
            body = match.group(1)
            fields = [token.group(1) for token in _TOKEN.finditer(body)]
            if not fields or not all(normalized.get(field, "") for field in fields):
                return ""
            return _TOKEN.sub(replace, body)

        rendered = _OPTIONAL.sub(optional, self.source)
        required_source = _OPTIONAL.sub("", self.source)
        missing = [match.group(1) for match in _TOKEN.finditer(required_source)
                   if not normalized.get(match.group(1), "")]
        if missing:
            raise TemplateError("missing_value:" + ",".join(sorted(set(missing))))
        return unicodedata.normalize("NFC", _TOKEN.sub(replace, rendered))

    def parse(self, text: str) -> dict[str, str]:
        """Parse a filename stem only when literal boundaries are unambiguous."""
        if "[" in self.source or "]" in self.source:
            raise TemplateError("optional_segments_not_parseable")
        pieces: list[str] = []
        cursor = 0
        for match in _TOKEN.finditer(self.source):
            pieces.append(re.escape(self.source[cursor:match.start()]))
            field, width = match.group(1), match.group(2)
            capture = rf"\d{{{int(width)}}}" if width else ".+?"
            pieces.append(f"(?P<{field}>{capture})")
            cursor = match.end()
        pieces.append(re.escape(self.source[cursor:]))
        matched = re.fullmatch("".join(pieces), text)
        if not matched:
            raise TemplateError("template_does_not_match")
        return {name: _clean_text(value).strip() for name, value in matched.groupdict().items()}


def compile_template(source: str, *, direction: str) -> Template:
    if direction not in {"filename_to_tags", "tags_to_filename"}:
        raise TemplateError("invalid_direction")
    if not isinstance(source, str) or not source.strip():
        raise TemplateError("empty_template")
    source = unicodedata.normalize("NFC", source.translate(_BIDI_CONTROLS))
    if source.count("[") != source.count("]") or _OPTIONAL.sub("", source).find("[") >= 0:
        raise TemplateError("invalid_optional_segment")
    fields = tuple(match.group(1) for match in _TOKEN.finditer(source))
    if not fields:
        raise TemplateError("template_has_no_fields")
    unknown = set(fields) - SUPPORTED_FIELDS
    if unknown:
        raise TemplateError("unknown_field:" + ",".join(sorted(unknown)))
    if direction == "filename_to_tags":
        if "[" in source or "]" in source:
            raise TemplateError("optional_segments_not_parseable")
        if len(set(fields)) != len(fields):
            raise TemplateError("repeated_field_is_ambiguous")
        # Adjacent captures have no deterministic boundary.
        if re.search(r"\}[^{]*\{", source):
            for left, right in zip(list(_TOKEN.finditer(source)), list(_TOKEN.finditer(source))[1:]):
                if not source[left.end():right.start()]:
                    raise TemplateError("adjacent_fields_are_ambiguous")
    # Reject stray braces instead of silently treating them as literals.
    if _TOKEN.sub("", source).count("{") or _TOKEN.sub("", source).count("}"):
        raise TemplateError("invalid_placeholder")
    return Template(source=source, fields=fields, direction=direction)


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value).translate(_BIDI_CONTROLS))


def safe_filename(stem: str, extension: str, *, sanitize: bool = True) -> str:
    stem = _clean_text(stem)
    extension = _clean_text(extension)
    if sanitize:
        stem = _INVALID_FILENAME.sub(" ", stem)
    elif _INVALID_FILENAME.search(stem):
        raise TemplateError("invalid_filename_characters")
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        raise TemplateError("empty_filename")
    if stem.upper() in _RESERVED:
        raise TemplateError("reserved_filename")
    if not extension.startswith(".") or extension == ".":
        raise TemplateError("invalid_extension")
    filename = stem + extension
    if len(filename) > MAX_FILENAME_LENGTH:
        raise TemplateError("filename_too_long")
    return filename
