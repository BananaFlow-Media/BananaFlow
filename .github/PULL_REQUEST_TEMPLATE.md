## What does this PR do?

<!-- One or two sentences. Link an Issue when applicable. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / internal change
- [ ] Documentation
- [ ] Translation / RTL
- [ ] CI / build / packaging
- [ ] Security-relevant change

## Documentation impact

<!-- Review docs/DOCUMENTATION_POLICY.md. Select every category that applies. -->

- [ ] User behavior / user guide
- [ ] CLI
- [ ] Architecture / design
- [ ] Configuration / persistence / migration
- [ ] Security / privacy / trust boundary
- [ ] Packaging / release / dependencies / licenses
- [ ] Accessibility / RTL / translation
- [ ] Historical/QA evidence only
- [ ] No documentation impact

No documentation impact reason: <!-- required when the last box is checked; write a real reason after the colon -->

Relevant documentation updated/reviewed:

<!-- List paths or explain why mapped documents remain accurate. -->

## Screenshots / recording

<!-- Required for visible UI changes when practical. Delete if no visible UI change. -->

## How was this tested?

<!-- Exact commands/evidence, not just “works”. -->

- [ ] Added/updated focused tests where behavior changed
- [ ] `python scripts/check_documentation.py` passes
- [ ] `python scripts/run_isolated_tests.py` passes locally or CI provides the full gate
- [ ] Real-network/manual QA performed when required by `docs/testing/TESTING.md`

## Checklist

- [ ] No secrets, credentials, cookies or tokens (real or realistic-looking) are included
- [ ] No generated files or staged release inputs are committed
- [ ] New user-facing strings exist in both English and Hebrew tables
- [ ] `core/`/`utils/` remain free of Qt/PySide6 symbols except the documented i18n lookup exception
- [ ] Persisted-state changes include migration/failure-path handling and tests
- [ ] Security-sensitive changes explicitly review `SECURITY.md`, `PRIVACY.md` and the threat model where applicable
- [ ] New/changed dependencies review `THIRD_PARTY_NOTICES.md` and supply-chain/release impact
