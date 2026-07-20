# Support

This document explains where to get help with BananaFlow, and
where **not** to post certain kinds of information.

## Before asking

1. Check `README.md`'s "Troubleshooting" section for the most common
   issues (FFmpeg not found, Playwright/Chromium missing, PO Token
   errors).
2. Run `bananaflow-cli --doctor` (or the in-app "YouTube Doctor" from
   Settings) — it diagnoses the most common environment problems
   (missing FFmpeg, missing Playwright browser, missing PO Token
   provider) and suggests the fix.
3. Search existing [Issues](../../issues) and
   [Discussions](../../discussions) — your question may already be
   answered.

## Where to ask

* **"How do I...?" / usage questions** → [Discussions → Help](../../discussions/categories/help).
* **Something is broken** → open an Issue using the *Bug Report* template
  (or a more specific template — *Installation Failure*, *Download
  Failure*, *Site Compatibility*, *Converter Problem*, *Tag Editor
  Problem* — if it fits).
* **A specific site or URL doesn't work** → the *Site Compatibility*
  Issue template; include the URL type (not necessarily the exact URL if
  it's private) and the YouTube Doctor / `--doctor` output.
* **Accessibility problem** (screen reader, keyboard navigation, RTL
  layout, contrast) → the *Accessibility Problem* Issue template.
* **Hebrew translation issue** → the *Hebrew/Translation* Issue template
  for a specific wrong or missing string;
  [Discussions → Translations](../../discussions/categories/translations)
  for broader wording, terminology or RTL-convention coordination.
* **Feature idea, not a bug** → [Discussions → Ideas](../../discussions/categories/ideas)
  first, or the *Feature Request* Issue template once it's reasonably
  well-scoped.
* **Beta feedback** (once a public Beta exists) →
  [Discussions → Beta Testing](../../discussions/categories/beta-testing).
  Anything reproducible still belongs in an Issue — the Discussion is for
  impressions, setup notes and "is this expected?" questions.
* **Contributing / architecture questions** →
  [Discussions → Development](../../discussions/categories/development),
  and see `CONTRIBUTING.md`.
* **Security vulnerability** → **do not** open a public Issue or
  Discussion. See `SECURITY.md` — GitHub private vulnerability reporting
  is the official channel.

## What not to post publicly

Never include, in an Issue, Discussion, PR, or log paste:

* cookies, session tokens, or authentication data;
* API keys, passwords, or other credentials;
* another person's private information;
* a live PO Token value; or
* an unredacted `config.json` or full application log without first
  reviewing it (the app applies centralized redaction, but review before
  sharing regardless — see `SECURITY.md`).

## Response time

This is a community-maintained open-source project (see `GOVERNANCE.md`
and `MAINTAINERS.md`). There is no guaranteed response time or support
SLA. Well-scoped, reproducible reports with the requested diagnostic
information (Doctor output, OS/version, exact steps) get triaged fastest.
