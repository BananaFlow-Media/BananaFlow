"""
tests/test_rtl_number_formatting.py  –  RTL number/date display consistency
========================================================================
Issue #43 asked to "spot-check whether numbers/dates in Hebrew mode follow
locale conventions consistently across all panels". The first pass unified
the *date format* (utils.time_format.timestamp_to_str) and stopped there,
leaving two thirds of the issue:

1. **Numbers were never looked at.** File sizes and byte counts went into
   Hebrew sentences raw.
2. **The RTL half was never looked at.** A left-to-right numeric string
   dropped into Hebrew prose without a directional isolate gets reordered
   by Unicode's bidi algorithm: "2026-03-05 14:30" can render as
   "14:30 2026-03-05", "12.4 MB" as "MB 12.4". That is not a cosmetic
   wobble — it reads as a different value.
3. **Nothing stopped the next inline strftime.** The unified format was a
   one-time cleanup with no guard, so the next hand-rolled call site would
   silently re-diverge, which is how the DD/MM vs YYYY-MM-DD split
   happened in the first place.

This covers the isolation helper, and lints for both regressions.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime
from pathlib import Path

import pytest

from ui.direction import isolate_number
from utils.time_format import display_timestamp, timestamp_to_str

REPO_ROOT = Path(__file__).resolve().parent.parent

_LTR_ISOLATE = "⁦"
_POP_ISOLATE = "⁩"


class TestIsolateNumber:

    def test_wraps_a_value_in_a_directional_isolate(self):
        result = isolate_number("12.4 MB")
        assert result.startswith(_LTR_ISOLATE) and result.endswith(_POP_ISOLATE)
        assert "12.4 MB" in result

    def test_a_timestamp_survives_embedding_in_hebrew_prose(self):
        """The concrete failure: the date must stay one left-to-right unit
        with its parts in order, not be reordered against the paragraph."""
        stamp = display_timestamp(datetime(2026, 3, 5, 14, 30))
        sentence = f"שונה: {isolate_number(stamp)}"

        body = sentence[sentence.index(_LTR_ISOLATE) + 1:sentence.index(_POP_ISOLATE)]
        assert body == "2026-03-05 14:30"
        assert body.index("2026") < body.index("14:30")

    def test_does_not_isolate_an_empty_value(self):
        """An isolate around nothing is invisible clutter that still counts
        as characters in width calculations and clipboard copies."""
        assert isolate_number("") == ""

    def test_accepts_a_raw_number_as_well_as_a_formatted_string(self):
        assert "1204" in isolate_number(1204)

    def test_is_idempotent_enough_to_be_safe_at_any_call_site(self):
        """Call sites should not have to track whether a value was already
        isolated upstream; double-wrapping must not corrupt the value."""
        once = isolate_number("5 MB")
        twice = isolate_number(once)
        assert twice.count("5 MB") == 1
        assert twice.startswith(_LTR_ISOLATE) and twice.endswith(_POP_ISOLATE)


class TestNoInlineDateFormatting:
    """The guard #82 did not add: one shared formatter, or the formats drift
    apart again."""

    # Timestamps baked into a *filename* or an ISO storage field are not UI
    # text and legitimately have their own formats.
    _ALLOWED_NON_DISPLAY = {
        "core/change_drafts.py",          # draft filename stamp
        "core/history_db.py",             # ISO-8601 storage + filename stamp
        "ui/controllers/metadata_controller.py",  # export filename stamp
        "utils/time_format.py",           # the shared formatter itself
    }

    def test_no_new_inline_strftime_outside_the_shared_formatter(self):
        offenders = []
        for directory in ("core", "ui", "utils"):
            for path in sorted((REPO_ROOT / directory).rglob("*.py")):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel in self._ALLOWED_NON_DISPLAY:
                    continue
                text = path.read_text(encoding="utf-8")
                for match in re.finditer(r"\.strftime\(", text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{rel}:{line}")

        assert not offenders, (
            "format displayed timestamps through utils.time_format."
            "timestamp_to_str so every panel shows the same shape — a "
            "hand-rolled strftime is how duplicate_files_dialog.py ended up "
            "on DD/MM/YYYY while everything else used YYYY-MM-DD (issue "
            f"#43). Offenders: {offenders}"
        )

    def test_the_shared_formatter_is_what_the_display_call_sites_use(self):
        """Inverse of the lint above: prove the helper is actually reached,
        so the lint is not guarding an unused function."""
        users = []
        for directory in ("core", "ui"):
            for path in (REPO_ROOT / directory).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if "timestamp_to_str(" in text or "display_timestamp(" in text:
                    users.append(path.relative_to(REPO_ROOT).as_posix())

        assert len(users) >= 3, f"expected the shared formatter to be widely used, got {users}"


class TestDisplayedTimestampsAreIsolated:
    """Every place that puts a formatted timestamp into UI text must isolate
    it, or Hebrew mode reorders it."""

    def test_every_displayed_timestamp_call_in_ui_is_isolated(self):
        unisolated = []
        for path in sorted((REPO_ROOT / "ui").rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            if not any(name + "(" in source
                       for name in ("timestamp_to_str", "display_timestamp")):
                continue
            tree = ast.parse(source, filename=rel)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in ("timestamp_to_str", "display_timestamp")):
                    continue
                if not _is_wrapped_in_isolation(tree, node):
                    unisolated.append(f"{rel}:{node.lineno}")

        assert not unisolated, (
            "a formatted timestamp shown in the UI must be wrapped in "
            "ui.direction.isolate_number() — without it Unicode's bidi "
            "algorithm reorders it inside Hebrew text and it reads as a "
            f"different value (issue #43). Offenders: {unisolated}"
        )


def _is_isolate_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id.startswith("isolate_"))


def _is_wrapped_in_isolation(tree: ast.AST, target: ast.Call) -> bool:
    """True if the timestamp reaches the UI isolated.

    Two accepted shapes, because both are readable and both are used:

    * ``isolate_number(timestamp_to_str(...))`` — the call is nested inside
      an ``isolate_*`` call;
    * ``value = timestamp_to_str(...)`` followed by
      ``return isolate_number(value)`` — assigned first, then isolated on the
      way out. Accepted only when *every* return in the enclosing function is
      isolated, so a second branch cannot leak a bare value.
    """
    for node in ast.walk(tree):
        if _is_isolate_call(node) and any(child is target for child in ast.walk(node)):
            return True

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(child is target for child in ast.walk(node)):
            continue
        returns = [r for r in ast.walk(node)
                   if isinstance(r, ast.Return) and r.value is not None]
        if returns and all(_is_isolate_call(r.value) for r in returns):
            return True
    return False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
