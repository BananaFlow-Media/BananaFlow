# Support

This document explains where to get help with BananaFlow and where **not** to post sensitive information.

For ordinary application use, start with the official website:

- Help: <https://bananaflow.bananaflow-media.workers.dev/en/help/> (Hebrew equivalent under `/he/`)
- FAQ: <https://bananaflow.bananaflow-media.workers.dev/en/faq/>
- Support: <https://bananaflow.bananaflow-media.workers.dev/en/support/>

The repository user manual is [`docs/user-guide/user-manual.md`](docs/user-guide/user-manual.md); Hebrew: [`docs/user-guide/user-guide-he.md`](docs/user-guide/user-guide-he.md).

## Before asking

1. Check the website Help/FAQ and the user manual troubleshooting section.
2. Run **YouTube Doctor** from Settings or `bananaflow-cli --doctor` for download-environment problems.
3. Search existing Issues/Discussions.
4. Reproduce with the current public release when practical.

## Where to ask

- Usage/how-to questions → website Help/FAQ, then Discussions/Help if needed.
- Reproducible bug → the closest Issue template (general bug, install/download/converter/Tag Editor/site compatibility, etc.).
- Accessibility/RTL/translation problem → the dedicated template/category when available.
- Feature idea → Discussions/Ideas first for broad proposals; a scoped issue once behavior is clear.
- Architecture/contribution question → `CONTRIBUTING.md` and Development discussions.
- Security vulnerability → **never a public issue**; follow `SECURITY.md`.

## What not to post publicly

Never post:

- cookies/session values;
- passwords/API keys/access tokens/proxy tokens;
- live PO-token values;
- another person's private information;
- an unreviewed full `config.json` or log containing private paths/URLs/media/account details;
- exploit details for an unpatched vulnerability.

Central redaction reduces accidental disclosure but does not remove the need to review material before sharing.

## Good bug evidence

Include the BananaFlow version, OS/install type, exact steps, expected vs actual behavior and the smallest relevant Doctor/log excerpt after redaction. For UI bugs, include a screenshot/short recording when it does not expose private data.

## Response time

BananaFlow is community-maintained open-source software. There is no guaranteed support SLA. Clear, reproducible reports with appropriate diagnostics are easier to triage.
