"""
utils/metadata_cleaner.py  –  Artist and Title string sanitization
==================================================================
Removes promotional noise, separates artists from titles, and prefers Hebrew over English names.
"""

import re

# Noise keywords to strip from artist names
_ARTIST_NOISE = [
    "הערוץ הרשמי", "ערוץ רשמי", "Official", "Channel", 
    "RedBandLive!", "- Topic", "VEVO", "Vevo"
]

def _extract_hebrew(text: str) -> str:
    """If text contains both Hebrew and English, return only the Hebrew parts."""
    # Check if there's any Hebrew character
    if re.search(r'[\u0590-\u05FF]', text):
        # Extract sequences of Hebrew characters and spaces/punctuation
        # We drop purely English words
        parts = re.split(r'[a-zA-Z]+', text)
        cleaned = " ".join(p.strip() for p in parts if p.strip() and not re.fullmatch(r'[^\w\s]+', p.strip()))
        
        # Clean up double spaces or floating hyphens
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        cleaned = re.sub(r'^[-|]+\s*|\s*[-|]+$', '', cleaned).strip()
        
        if cleaned:
            return cleaned
    return text

def clean_artist(artist: str) -> str:
    """
    Remove noise words like 'הערוץ הרשמי' and extract Hebrew if present alongside English.
    """
    if not artist:
        return ""
        
    cleaned = artist
    for noise in _ARTIST_NOISE:
        # Case insensitive replace
        cleaned = re.sub(re.escape(noise), '', cleaned, flags=re.IGNORECASE)
        
    cleaned = cleaned.strip()
    
    # Strip any trailing/leading hyphens or pipes left over
    cleaned = re.sub(r'^[-|]+\s*|\s*[-|]+$', '', cleaned).strip()
    
    cleaned = _extract_hebrew(cleaned)
    return cleaned

def clean_title_and_artist(title: str, artist: str = "") -> tuple[str, str]:
    """
    Cleans the title and extracts the implied artist from the title if they are merged.
    Returns (cleaned_title, cleaned_artist).
    """
    if not title:
        return "", clean_artist(artist)
        
    cleaned_title = title
    extracted_artist = artist
    
    # Generic aggressive clean for standard YouTube music videos
    # Strip only promotional bracketed suffixes (Official/Audio/HD/...). We intentionally
    # do NOT strip every trailing "(...)" — that would also remove legitimate parts of a
    # title such as "(Live)", "(feat. X)" or any Hebrew parenthetical.
    cleaned_title = re.sub(r'\s*[([].*?(Official|Video|Clip|Audio|Prod|By|Remaster|Lyrics|HD|4K|Direct|Studio).*?[)\]]', '', cleaned_title, flags=re.IGNORECASE).strip()
    
    # Check if title is in "Artist - Title" format
    if "-" in cleaned_title:
        parts = [p.strip() for p in cleaned_title.split("-", 1)]
        if len(parts) == 2:
            left_part, right_part = parts
            
            # If we know the artist and it matches the left part closely
            # we strip it from the title.
            known_artist_cleaned = clean_artist(artist)
            if known_artist_cleaned and known_artist_cleaned.lower() in left_part.lower():
                cleaned_title = right_part
            elif artist and artist.lower() in left_part.lower():
                cleaned_title = right_part
            elif not artist:
                # Only when NO artist is supplied do we guess that the left side is the
                # artist (raw "Artist - Title" YouTube titles). When an artist IS known
                # but doesn't match the left part, the hyphen belongs to the title itself
                # (common in Hebrew titles like "חמישה - ספירה לאחור"), so we leave it intact.
                word_count = len(left_part.split())
                if 0 < word_count <= 4 and len(left_part) < 30:
                    extracted_artist = left_part
                    cleaned_title = right_part

    # Ensure the final artist is cleaned (drops "Official Channel" etc.)
    final_artist = clean_artist(extracted_artist)
    
    # One last pass: if the exact final artist is at the start of the title, strip it
    if final_artist:
        pattern_start = r'^' + re.escape(final_artist) + r'\s*[-|:]\s*'
        cleaned_title = re.sub(pattern_start, '', cleaned_title, flags=re.IGNORECASE)
        
    return cleaned_title, final_artist

def clean_title(title: str, artist: str = "") -> str:
    """Convenience wrapper if you only need the title."""
    t, _ = clean_title_and_artist(title, artist)
    return t
