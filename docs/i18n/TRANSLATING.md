# BananaFlow translation and RTL guide

Status: **Current / normative**

BananaFlow ships English and Hebrew as first-class UI languages. A feature is not complete if its user-facing text exists in only one language.

## Adding or changing UI text

- Add every key to both language tables in `ui/i18n.py`.
- Reuse an existing semantic key when the concept is truly identical; do not reuse an unrelated key to avoid translation work.
- Do not hard-code user-facing strings in widgets/controllers when they belong in i18n.
- Keep technical product/library names (for example yt-dlp, YouTube Doctor, FFmpeg) unchanged unless the project has an established localized form.

Coverage is enforced by automated tests.

## Hebrew RTL rules

- Layout is mirrored RTL at the UI level.
- Paths, URLs, identifiers, codecs, extensions and other technical tokens should remain LTR when mirroring would harm readability.
- Punctuation around mixed Hebrew/Latin content must be checked visually; use direction isolation helpers already present in the UI rather than embedding ad-hoc Unicode controls throughout code.
- Back/forward actions keep their navigation meaning.

## Documentation parity

The English and Hebrew full user guides are companions, not independent products. They need not be literal translations, but they must agree on:

- supported capabilities;
- required setup;
- security/privacy warnings;
- settings and defaults that users rely on;
- current limitations/troubleshooting;
- Spotify search vs Spotify URL-import behavior;
- release/update behavior.

A behavior change documented in one guide requires review of the other in the same PR.

## Terminology

Prefer established UI terminology already used in `ui/i18n.py`. If a recurring new concept needs a Hebrew term, choose it once and use it consistently across the UI, user guide and website-facing copy where this repository controls the text.

## Testing

Run i18n coverage/hard-coded-string tests plus relevant UI tests. Complex RTL changes should also receive a real-screen manual pass because headless geometry tests cannot catch every bidi/clipping issue.
