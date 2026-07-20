## What does this PR do?

<!-- One or two sentences. Link an Issue if one exists: "Fixes #123" -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / internal change (no user-visible behavior change)
- [ ] Documentation
- [ ] Translation (Hebrew/English)
- [ ] CI / build / packaging
- [ ] Security-relevant change (see `SECURITY.md` if this is a
      vulnerability *report* instead of a fix — do not describe an
      unpatched vulnerability here)

## Screenshots / recording

<!-- Required for any visible UI change. Before/after, or just after for
     a new element. Delete this section if there is no UI change. -->

## How was this tested?

<!-- What you ran, not just "it works." Include the isolated gate result
     if you ran it: `python scripts/run_isolated_tests.py` -->

- [ ] Added or updated tests for the changed behavior
- [ ] `QT_QPA_PLATFORM=offscreen pytest tests/ -q` passes locally
- [ ] `python scripts/run_isolated_tests.py` passes locally (or CI is
      expected to cover it)

## Checklist

- [ ] No secrets, credentials, cookies, or tokens (real or
      realistic-looking) are included anywhere in this diff
- [ ] No generated files or staged release inputs are committed (see
      `CONTRIBUTING.md`'s "Third-Party Code" section)
- [ ] New user-facing strings are added to **both** language tables in
      `ui/i18n.py` (English and Hebrew)
- [ ] `core/`/`utils/` changes still import no Qt/PySide6
- [ ] Documentation updated if setup, behavior, or architecture changed
- [ ] Third-party license notice added to `THIRD_PARTY_NOTICES.md` if a
      new dependency was introduced
