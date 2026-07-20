"""
utils/artwork_cleaner.py  –  Thumbnail URL sanitization
=======================================================
Transforms raw platform thumbnail URLs into high-resolution, square (1:1)
versions to ensure consistent UI display and high-quality embedding.
"""

import re
from urllib.parse import urlparse

from utils.url_cleaner import host_matches_domain


def clean_artwork_url(url: str, platform) -> str:
    """
    Transform a raw thumbnail URL into a high-res square version if possible.
    
    Rules
    -----
    YouTube Music (lh3.googleusercontent / yt3.ggpht):
        Replace size suffixes like =w120-h120-l90-rj with =w1024-h1024-p-rj.
    
    YouTube (i.ytimg.com):
        Prefer maxresdefault.jpg.
        
    Spotify:
        Usually already square, return as-is.
    """
    from core.playlist_parser import SourcePlatform
    if not url:
        return ""

    if platform == SourcePlatform.YOUTUBE_MUSIC:
        # lh3.googleusercontent.com or yt3.ggpht.com URLs often have size params at the end
        # Example: https://lh3.googleusercontent.com/...=w120-h120-l90-rj
        # Examples of matches: =w120-h120, =s120-c, -w120-h120
        # The trailing repeated group intentionally excludes "-" from its
        # character class (unlike a naive [a-z0-9-]+): allowing "-" inside
        # AND as the repetition's own separator lets the same run of dashes
        # be split across the outer group in exponentially many ways,
        # causing catastrophic backtracking (ReDoS) on a non-matching tail
        # — confirmed hanging >30s on a ~30-byte crafted input before this
        # fix. Excluding "-" from the inner class removes the ambiguity.
        pattern = r'(=|-)(w|s)\d+(-b\d+)?(-c)?(-h\d+)?(-[a-z0-9]+)*$'
        if re.search(pattern, url):
            # Force 1200x1200 crop
            return re.sub(pattern, r'\1w1200-h1200-p-rj', url)
        
        # If no suffix found but it's a googleusercontent URL, we can try appending it
        if host_matches_domain(urlparse(url).netloc, "googleusercontent.com", "ggpht.com"):
            if "=" not in url:
                return f"{url}=w1200-h1200-p-rj"
            
    elif platform == SourcePlatform.YOUTUBE:
        # Strip ?sqp= parameter from youtube thumbnails which prevents high-res retrieval
        if "?" in url:
            url = url.split("?")[0]
            
        if "i.ytimg.com/vi/" in url:
            for quality in ["hqdefault.jpg", "mqdefault.jpg", "sddefault.jpg", "default.jpg", "hq720.jpg"]:
                if quality in url:
                    return url.replace(quality, "maxresdefault.jpg")
            
    return url
