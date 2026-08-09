"""
utils/website.py  –  the official BananaFlow website
====================================================

Single source of truth for the address of BananaFlow's official
website. Every part of the project that points a user at the site —
the Settings ▸ About card, ``bananaflow-cli --version``, the Windows
installer, the packaging metadata and the documentation — resolves the
address from here (or, for the non-Python surfaces, is pinned to
``WEBSITE_URL`` by
``tests/test_p0_gates.py::TestOfficialWebsiteURLConsistency``) so the
project can never end up advertising two different websites.

The site is a separate, statically exported project deployed to
Cloudflare Workers; this repository holds the application, not the
site. Downloads themselves are still published as GitHub Releases —
the site's download pages link to those release assets.

The public site is bilingual and serves **every** page under a locale
prefix (``/he/…`` and ``/en/…``); a bare ``/`` is a 308 redirect to
``/he/``. ``site_url()`` therefore builds a locale-correct absolute URL
so the app sends the user to the site in the language the app itself is
running in, and always with the trailing slash the static export
canonicalizes to.

No Qt here, by the ``utils/`` layering rule — the UI language is read
through a deferred import of ``ui.i18n`` (a plain-Python translation
lookup) and falls back cleanly when it is unavailable, e.g. in a
headless CLI run.
"""

from __future__ import annotations

# The live production origin, with its trailing slash. This is the free
# Cloudflare Workers hostname the site is deployed on; it is the
# canonical URL the site itself advertises in its own
# canonical/hreflang/sitemap metadata. Changing it is a coordinated
# change with the website project — never a one-sided edit here.
WEBSITE_URL: str = "https://bananaflow.bananaflow-media.workers.dev/"

# Locales the public site is published in. Mirrors the site's own
# supported locales; anything else has no pages to serve.
SITE_LOCALES: tuple[str, ...] = ("he", "en")

# What a bare visit to the site resolves to (``/`` → 308 → ``/he/``).
SITE_DEFAULT_LOCALE: str = "he"

# What the app links to when it cannot tell which language it is
# running in. This matches the application's own default UI language,
# which is what an unconfigured CLI run reports.
FALLBACK_LOCALE: str = "en"

# Public pages, keyed by a stable id, mapped to their path segment(s)
# below the locale prefix. Keep in sync with the site's route table;
# every entry here is a real, indexed page.
SITE_PAGES: dict[str, str] = {
    "home": "",
    "download": "download",
    "download_windows": "download/windows",
    "download_macos": "download/macos",
    "download_all": "download/all",
    "releases": "releases",
    "help": "help",
    "faq": "faq",
    "support": "support",
    "security": "security",
    "privacy": "privacy",
    "terms": "terms",
    "contact": "contact",
    "accessibility": "accessibility",
}


def _resolve_locale(lang: str | None) -> str:
    """Pick the site locale to link to.

    Explicit ``lang`` wins; otherwise the active UI language is used;
    otherwise :data:`FALLBACK_LOCALE`. A language the site is not
    published in also falls back rather than producing a 404 link.
    """
    if lang is None:
        try:
            from ui.i18n import current_language

            lang = current_language()
        except Exception:  # pragma: no cover - defensive, headless runs
            lang = None

    if lang:
        # Accept "en-US"/"he_IL" style tags as well as bare codes.
        base = lang.strip().lower().replace("_", "-").split("-", 1)[0]
        if base in SITE_LOCALES:
            return base

    return FALLBACK_LOCALE


def site_url(page: str = "home", lang: str | None = None) -> str:
    """Absolute URL of a page on the official website.

    ``page`` is a key of :data:`SITE_PAGES` (default: the localized
    home page). ``lang`` overrides the language; when omitted the
    active UI language is used.

    >>> site_url("download", lang="en")
    'https://bananaflow.bananaflow-media.workers.dev/en/download/'
    >>> site_url(lang="he")
    'https://bananaflow.bananaflow-media.workers.dev/he/'
    """
    try:
        segments = SITE_PAGES[page]
    except KeyError:
        raise ValueError(
            f"Unknown website page {page!r}; expected one of "
            f"{sorted(SITE_PAGES)}"
        ) from None

    path = f"{_resolve_locale(lang)}/"
    if segments:
        path += f"{segments}/"
    return WEBSITE_URL + path
