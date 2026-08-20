# Manual QA Checklist — YouTube Doctor (Settings UI)

Status: **Reusable manual QA checklist**

Automated tests verify YouTube Doctor logic/dialog wiring headlessly. This checklist verifies real-screen rendering, localization and privacy behavior on a release candidate or targeted UI change. Record the tested BananaFlow version/commit, OS and date in the PR/release evidence when you run it.

## 1. Open the app

Launch the source build or release candidate normally. Confirm the main window appears and Settings is usable.

## 2. Locate YouTube Doctor

Navigate to the current Settings page/group containing diagnostics. Confirm:

- a visible **YouTube Doctor** entry/action exists;
- the title/description/button are localized rather than raw i18n keys;
- controls do not overlap/clip at normal scaling;
- keyboard focus reaches the action.

Do not treat an old Settings group order from a screenshot as the contract; the installed version's current information architecture may evolve.

## 3. Run the Doctor

Open the dialog and verify:

- readiness checks render with readable status labels/details;
- the summary/recommended actions are visible;
- long runtime/version lines wrap or resize without horizontal clipping;
- theme/accent/RTL state is respected;
- the dialog closes normally and does not freeze/crash the app.

## 4. Privacy check

Using synthetic/non-sensitive test state where possible, verify the dialog never displays:

- cookie values;
- authorization/proxy/API tokens;
- a full private browser-profile path/user name when a redacted representation is sufficient;
- unredacted sensitive diagnostic text.

Showing a cookie **presence/readiness state** is expected; exposing the secret value is a failure.

## 5. Hebrew / RTL

Switch the application language to Hebrew and repeat the UI check. Technical product/runtime names can remain Latin-script, but surrounding explanation/layout should be translated and RTL-correct. Mixed-direction lines must remain readable.

## 6. Scaling / accessibility

Repeat the critical dialog pass at a higher Windows display scaling level when the change affects layout. Verify keyboard navigation, visible focus and non-color-only status information.

## 7. Failure evidence

If a check fails, capture the smallest safe evidence: version/commit, OS/scaling/theme/language, screenshot, exact step and redacted console/log excerpt. Never attach real cookies or an unreviewed config/log dump.
