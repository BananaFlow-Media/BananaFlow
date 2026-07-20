"""
utils/url_cleaner.py  –  URL parsing and cleaning helpers
=========================================================
"""

from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


def host_matches_domain(host: str, *domains: str) -> bool:
    """True if ``host`` is exactly one of ``domains``, or a subdomain of one.

    A substring test (``"youtube.com" in host``) would also match unrelated
    hosts such as ``"youtube.com.evil.example"`` or ``"notyoutube.com"``
    that merely contain the domain as a fragment — compare the host
    component exactly instead. Strips a trailing ``:port`` from ``host``
    since a URL's ``netloc`` may include one.
    """
    host = host.strip().lower().lstrip(".").split(":", 1)[0]
    for domain in domains:
        domain = domain.strip().lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def clean_youtube_url(url: str) -> str:
    """
    Remove 'list' and 'index' parameters from YouTube watch, short, and shared URLs.
    This prevents yt-dlp from inheriting playlist-level indexes for individual downloads
    and avoids playlist-download warnings.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if host_matches_domain(parsed.netloc, "youtube.com", "youtu.be"):
            is_watch_or_short = (
                parsed.path.startswith("/watch")
                or host_matches_domain(parsed.netloc, "youtu.be")
                or parsed.path.startswith("/shorts")
            )
            if is_watch_or_short:
                query_params = parse_qsl(parsed.query)
                filtered_params = [
                    (k, v) for k, v in query_params 
                    if k.lower() not in ("list", "index")
                ]
                new_query = urlencode(filtered_params)
                return urlunparse(parsed._replace(query=new_query))
    except Exception:
        pass
    return url
