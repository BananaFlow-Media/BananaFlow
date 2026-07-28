"""
ui/i18n.py
Localization helper for the app.

This is intentionally lightweight: translation lookup by key plus a
small API to coordinate language + layout direction at startup or when
the user changes it in Settings.

Public API:
    t(key, **kwargs)                  — translate a key with optional formatting
    set_language(lang)                — update active language code (no side effects)
    current_language()                — read active language code
    apply_language(app, lang)         — single entry point used at startup and
                                        when the user picks a different language;
                                        updates translation state, app-wide layout
                                        direction, and emits language_changed
    request_language_restart(app, lang) — restart the app process with the new
                                          language so every widget rebuilds in it
    language_manager()                — singleton QObject exposing the
                                        ``language_changed(str)`` signal that
                                        widgets can connect to for future
                                        live-retranslation work
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Dict, Optional, Set

_current: str = "en"
_log = logging.getLogger("ui.i18n")
_warned_keys: Set[str] = set()

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        # ── Navigation ──────────────────────────────────────────────────────────
        "app_name": "BananaFlow",
        "queue": "Queue",
        "search": "Search",
        "history": "History",
        "settings": "Settings",
        "tag_editor": "Tag Editor",
        "converter": "Converter",

        # ── Download bar ────────────────────────────────────────────────────────
        "no_tracks_selected": "No tracks selected",
        "download_selected": " Download Selected",
        "download_downloading": " Downloading…",
        "downloads_header_title": "What are we downloading today?",
        "downloads_header_subtitle": "Paste a YouTube link or Spotify link (playlists, albums, tracks, artists) to automatically match and download audio.",
        "downloads_empty_title": "Waiting for content",
        "downloads_empty_subtitle": "Paste a link above to load metadata, previews, and actions.",
        "search_empty_title": "Search for music, videos, or playlists",
        "search_empty_subtitle": "Choose a platform and enter a query above to begin.",
        "history_header_title": "Download History",
        "history_header_subtitle": "Downloads you completed are saved here automatically.",
        "history_empty_title": "No downloads yet",
        "history_empty_subtitle": "Every completed download will appear here for quick access.",
        "converter_drop_title": "Drag files here",
        "converter_drop_subtitle": "Or click Add Files to choose audio files from your computer.",

        # ── Error dialogs ───────────────────────────────────────────────────────



        "cannot_write_output_title": "Cannot Write to Output Folder",
        "cannot_write_output_detail": "The folder cannot be created:\n{path}\n\nError: {exc}",

        # ── Status bar ──────────────────────────────────────────────────────────
        # Status strings carry NO emoji: the footer draws a themed status icon
        # (ui.components.status_icon) beside plain text. See StatusBar.
        "ready": "Ready.",
        "cancel": "Cancel",
        "status_starting": "Preparing downloads…",
        "status_downloading_progress": "Downloading {done} of {total} · {pct}%",
        "status_paused": "Downloads paused",
        "status_cancelling": "Cancelling…",
        "status_completed_summary": "{n} download{plural} completed.",
        "status_completed_with_preexisting": "{completed} of {total} completed — {downloaded} downloaded, {preexisting} already existed.",
        "status_completed_with_errors": "{ok} completed, {failed} failed.",
        "status_stopped_summary": "Downloads stopped — {done} of {total} completed.",
        "status_stopped_error": "Downloads stopped because of an error.",
        # Kept short: it has to fit the fixed-width footer ETA slot alongside
        # the longest real duration string, and it is now shown at the start of
        # every batch while the throughput estimate warms up.
        # Track-card stages. A track spends most of its wall time outside the
        # byte transfer, so each stage names itself rather than leaving the card
        # showing a dead "downloading".
        "phase_matching":    "Finding a match…",
        "phase_waiting":     "Waiting its turn…",
        "phase_starting":    "Starting…",
        "phase_downloading": "Downloading",
        "phase_processing":  "Finishing up…",
        "spotify_metadata_invalid_card": "Spotify track details are invalid — this track will not be downloaded.",
        "eta_calculating": "Calculating…",
        # {time} is a preformatted M:SS / H:MM:SS duration from
        # utils.time_format.seconds_to_str, so the footer can count down by the
        # second instead of rounding to whole minutes. The old per-unit keys
        # (eta_about_sec/min/hr) could not express seconds above a minute.
        "eta_about_left": "About {time} left",
        "eta_range_left": "About {low}–{high} left",
        "eta_current_speed_left": "About {time} at the current speed",
        "eta_tooltip": "Estimated time remaining for the whole batch",
        "starting_downloads": "Starting {n} download{plural}…",
        "download_progress_count": "Downloading {current} / {total}…",
        "cancelling": "Cancelling…",
        "status_offline": "No internet connection.",
        "status_online": "Internet connection restored.",
        "status_batch_done": "All downloads finished.",
        "status_batch_cancelled": "Downloads cancelled.",

        # ── Offline banner ──────────────────────────────────────────────────────
        "offline_banner_msg": "No internet connection — search and downloads are paused until it's restored.",
        "offline_banner_close": "Dismiss",

        # ── Toasts / small notifications ────────────────────────────────────────
        "download_toast_title": "Downloaded",
        "download_toast_fallback": "Track saved.",
        "clipboard_toast_title": "Clipboard",
        "clipboard_toast_detected": "Detected: {url}",

        # ── Duplicate-file confirmation (pre-download check) ────────────────────
        "duplicate_detected_title": "Duplicate Detected",
        "duplicate_detected_msg": (
            "\"{title}\" already exists:\n{path}\n\n"
            "Download again and overwrite?"
        ),

        # ── Batched duplicate confirmation (one dialog for the whole batch) ─────
        "batch_duplicates_title": "Duplicate Files Found",
        "batch_duplicates_subtitle_one": "1 file already exists in the destination folder.",
        "batch_duplicates_subtitle_many": "{n} files already exist in the destination folder.",
        "batch_duplicates_skip_all_btn": "Skip All",
        "batch_duplicates_replace_all_btn": "Replace All",

        # ── File dialogs ────────────────────────────────────────────────────────
        "choose_download_folder": "Choose download folder",

        # ── URL bar ─────────────────────────────────────────────────────────────
        "fetching_button": "Fetching…",
        "fetch_info_button": "Fetch Info",
        "paste_tooltip": "Paste from clipboard",
        "batch_import_tooltip": "Batch import URLs from a .txt file",
        "scrape_tooltip": "Scan this page for embedded media links",
        "clipboard_on_tooltip": "Clipboard monitor is ON — auto-detecting media URLs",
        "clipboard_off_tooltip": "Clipboard monitor is OFF — enable in Settings",
        "url_placeholder": "Paste a YouTube or Spotify URL, or type a search query…",
        "invalid_url_title": "Invalid URL",
        "invalid_url_detail": "That looks like a broken or incomplete URL. Check the address and try again.",

        # ── Batch import ────────────────────────────────────────────────────────
        "batch_import_failed": "Batch Import Failed",
        "no_urls_found": "No supported URLs found in {filename}",
        "batch_import_progress": "Importing URL {current} of {total}…",
        "batch_import_complete": "Import complete: {success} succeeded, {failed} failed, {skipped} skipped.",
        "batch_import_cancelled": "Import cancelled: {success} succeeded, {failed} failed, {skipped} skipped, {remaining} not processed.",

        # ── Scraper ─────────────────────────────────────────────────────────────
        "scrape_multi_found": "{count} media link(s) found. First URL loaded — press Fetch Info to begin.",
        "scraping": "Scanning page for media…",
        "scrape_no_urls": "No media links found on that page.",

        # ── Fetch status ────────────────────────────────────────────────────────
        "fetching": "Fetching information…",
        "fetching_progress": "Loading track {n} of {total}…",
        "fetching_single": "Fetching: {title}",
        "fetch_done": "{n} track{plural} loaded — select and press Download.",
        "collecting_catalog": "Collecting catalog…",
        "found_n_tracks": "Found {n} tracks",

        # ── Settings panel ──────────────────────────────────────────────────────
        "settings_section_basic": "Basic",
        "settings_section_advanced": "Advanced",
        # Doubled ampersand: Qt/QFluentWidgets label painting treats a
        # single "&" as a mnemonic-accelerator marker and swallows it
        # (rendered as "Expert _Diagnostics" in the segmented nav) — "&&"
        # is the standard Qt escape for a literal "&".
        "settings_section_expert": "Expert && Diagnostics",
        "signin_group": "Sign-in",
        "appearance": "Appearance",
        "theme": "Theme",
        "switch_theme": "Switch Theme",
        "language": "Language",
        "select_language": "Select UI language",

        "downloads_group": "Downloads",
        "embed_thumbnail": "Embed Thumbnail",
        "embed_thumbnail_desc": "Write cover art into the downloaded file's metadata (ID3 / MP4 atoms)",
        "embed_metadata": "Embed Metadata",
        "embed_metadata_desc": "Write title, artist, album, and year tags into the file",

        "features": "Features",
        "clipboard_monitor": "Clipboard Monitor",
        "clipboard_monitor_desc": (
            "Auto-detect YouTube / Spotify URLs copied to the clipboard "
            "and populate the URL bar automatically"
        ),
        "check_updates": "Check for Updates on Launch",
        "check_updates_desc": (
            "Silently check GitHub Releases and PyPI at startup and show "
            "a notification when the app or its components are outdated"
        ),

        "search_group": "Search",
        "max_youtube_results": "Max YouTube Results",
        "max_youtube_results_desc": "Maximum number of results to fetch for YouTube searches (1 – 100)",
        "max_spotify_results": "Max Spotify Results",
        "max_spotify_results_desc": "Maximum number of results to fetch for Spotify searches (1 – 100)",
        "spotify_proxy": "Spotify Proxy Server URL",
        "spotify_proxy_desc": "URL to your Spotify proxy server (e.g. http://localhost:8000)",

        "spotify_group": "Spotify",
        "spotify_proxy_api_key": "App API Key",
        "spotify_proxy_api_key_desc": "Security token for your proxy server (X-App-Token)",

        "authentication": "Authentication",
        "cookies_file": "Cookies File",
        "cookies_file_unset": "Not set — click Browse to select a cookies.txt file",
        "cookies_file_configured": "Protected for your Windows account in BananaFlow's private app-data folder — click Browse to update",
        "cookies_updated_title": "Cookies updated",
        "cookies_updated_msg": "Your saved cookies were updated successfully.",
        "cookies_store_failed_title": "Cookies were not stored",
        "cookies_store_failed_msg": "BananaFlow could not safely write or restrict its private cookies file. No imported file was configured.",
        "browse": "Browse…",
        "clear_cookies": "Delete stored sign-in data",
        "clear_cookies_title": "Delete Stored Authentication Data",
        "clear_cookies_desc": "Delete BananaFlow's cookies file and dedicated sign-in browser profile",
        "clear_cookies_confirm": (
            "Cookies may grant access to your account and should be treated like a password.\n\n"
            "Permanently delete BananaFlow's stored cookies and its dedicated sign-in browser profile? "
            "This does not alter your normal browsers, downloads, history, or other settings."
        ),
        "clear_cookies_confirm_yes": "Delete authentication data",
        "clear_cookies_confirm_no": "Keep it",
        "clear_cookies_failed_title": "Authentication data not fully deleted",
        "clear_cookies_failed_msg": "Some BananaFlow authentication data is locked or could not be removed. Close sign-in browser windows and try again.",
        "clear_cookies_success_title": "Authentication data deleted",
        "clear_cookies_success_msg": "BananaFlow's stored cookies and dedicated sign-in browser profile were removed.",

        "about": "About",

        # ── History panel ───────────────────────────────────────────────────────
        "search_history_placeholder": "Search history by title or artist…",
        "export_csv": "Export CSV",
        "clear_history": "Clear History",
        "records_count": "{n} record{plural}",
        "col_date": "Date",
        "col_title_artist": "Title / Artist",
        "col_platform": "Platform",
        "col_type": "Type",
        "col_duration": "Duration",
        "col_size": "Size",
        "col_actions": "Actions",
        "history_empty_hint": (
            "Your download history will appear here.\n"
            "Completed downloads are logged automatically."
        ),
        "export_dialog_title": "Export history as CSV",
        "export_complete": "Export Complete",
        "export_complete_msg": "Exported {count} record(s) to:\n{path}",
        "export_failed": "Export Failed",
        "export_failed_msg": "Could not write CSV file:\n{error}",
        "clear_history_title": "Clear Download History",
        "clear_history_confirm": "This will permanently delete all history records.\n\nAre you sure?",

        # ── Search panel ────────────────────────────────────────────────────────
        "search_placeholder": "Search for tracks, albums, artists…",
        "searching": "Searching…",
        "no_results": "No results found.",
        "results_count": "{n} result{plural}",
        "clear_results": "Clear results",
        "search_empty_hint": (
            "Search for music, videos, or playlists.\n"
            "Select a platform and type your query above."
        ),
        "platform_youtube": "YouTube",
        "platform_ytmusic": "YouTube Music",
        "platform_spotify": "Spotify",
        "platform_both": "Both",

        "search_filter_all": "All",
        "search_filter_tracks": "Tracks",
        "search_filter_albums": "Albums",
        "search_filter_artists": "Artists",
        "search_filter_playlists": "Playlists",
        "search_filter_channels": "Channels",

        # ── Queue panel ─────────────────────────────────────────────────────────
        "queue_label": "Download Queue",
        "no_tracks_loaded": "No tracks loaded",
        "select_deselect_all": "Select / Deselect All",
        "clear_completed": "Clear completed",
        "clear_selected": "Clear selected",
        "clear_all": "Clear all",
        "clear_options": "Clear…",
        "pause_all": "Pause All",
        "resume_all": "Resume All",
        "sel_of_n": "{sel} / {n} selected",
        "queue_stats_done": "· {done}/{total} done",
        "queue_empty_hint": (
            "Paste a YouTube or Spotify URL above\n"
            "and press  Fetch Info  to load tracks."
        ),

        # ── Update system ───────────────────────────────────────────────────────
        "updates_group": "Updates",
        "update_check_btn": "Check Now",
        "check_app_updates_title": "Check for App Updates",
        "check_app_updates_desc": (
            "Recommended: updating BananaFlow is the main update path — each "
            "release includes the tested downloader components"
        ),
        "check_component_updates_title": "Check for Component Updates (Advanced)",
        "check_component_updates_desc": (
            "For advanced users and source installs. Normal users get these "
            "components (yt-dlp, yt-dlp-ejs) automatically with BananaFlow updates"
        ),
        "update_check_failed_title": "Update check failed",
        "update_check_failed_msg": (
            "Could not reach the update server. Check your internet "
            "connection and try again."
        ),
        "up_to_date_title": "You're up to date",
        "app_up_to_date_msg": "BananaFlow {version} is the latest released version.",
        "components_up_to_date_msg": "All components are current:  {versions}",

        "update_prompt_title": "Updates Available",
        "update_prompt_subtitle": "Nothing is installed without your approval.",
        "update_prompt_app_line": "A new version of BananaFlow is available: {new}  (you have v{cur}).",
        "update_prompt_app_note": (
            "Choosing “Open Download Page” opens the BananaFlow release page in your "
            "browser, where you can download and install the new version. BananaFlow "
            "does not update itself automatically."
        ),
        "update_prompt_app_includes_components": (
            "This update also includes updated downloader components "
            "({names}) — updating BananaFlow is all you need to do."
        ),
        "update_prompt_components_heading": "Outdated downloader components:",
        "update_prompt_component_line": "{name}:  {cur}  →  {new}",
        "update_prompt_component_note": (
            "You are running BananaFlow from source, so components can be updated "
            "in place: choosing “Update Components” runs 'pip install --upgrade "
            "yt-dlp[default]' in BananaFlow's own environment. A restart of BananaFlow "
            "is required afterward for the new versions to take effect. Keeping "
            "yt-dlp current is strongly recommended — outdated versions are the "
            "most common cause of failing YouTube downloads."
        ),
        "update_prompt_frozen_note": (
            "The downloader components are bundled inside this installed build "
            "of BananaFlow and are updated together with the app, so there is "
            "nothing to install separately. When a BananaFlow update that includes "
            "newer components is published, this notification will point to it. "
            "“Open Download Page” shows the latest available release — if none "
            "is newer yet, check back later."
        ),
        "update_get_app_btn": "Open Download Page",
        "update_components_btn": "Update Components",
        "update_open_releases_btn": "Open Download Page",
        "update_remind_btn": "Remind Me Later",
        "update_remind_next_launch": "On next launch",
        "update_remind_1_day": "In 1 day",
        "update_remind_3_days": "In 3 days",
        "update_remind_7_days": "In a week",
        "update_skip_btn": "Skip This Version",
        "component_install_running": "Updating components… this can take a minute.",
        "component_install_ok_msg": (
            "Components updated successfully. Restart BananaFlow for the new "
            "versions to take effect."
        ),
        "component_install_failed_msg": (
            "Component update failed. You can try again, or run "
            "'pip install --upgrade yt-dlp[default]' manually.\n\nDetails: {detail}"
        ),

        # ── Browser cookies ─────────────────────────────────────────────────────
        "browser_cookies":      "Browser Cookies Source",
        "browser_cookies_desc": "Read cookies from your browser to authenticate access to age-restricted or members-only content",
        "browser_cookie_migrated_title": "Browser cookie setting updated",
        "browser_cookie_migrated_msg": "BananaFlow removed an older live Chrome, Edge, Brave, or Chromium cookie setting because Windows no longer permits that access safely and reliably. Use BananaFlow's isolated sign-in helper or import cookies.txt instead. Firefox remains available.",
        "disabled":             "Disabled",

        # ── Release types ───────────────────────────────────────────────────────
        "release_album":        "Album",
        "release_single":       "Single",
        "release_ep":           "EP",
        "release_playlist":     "Playlist",
        "release_compilation":  "Compilation",
        "tracks":               "tracks",
        "items":                "items",

        # ── System tray ─────────────────────────────────────────────────────────
        "tray_tooltip": "BananaFlow",
        "tray_open": "Open",
        "tray_cancel_all": "Cancel All Downloads",
        "tray_quit": "Quit",
        "tray_all_done": "All downloads complete!",

        # ── Auth / cookie wizard ────────────────────────────────────────────────
        "auth_wizard_open_btn": "🔑 Fix sign-in",
        "auth_wizard_close_btn": "Close",
        "auth_wizard_manual_btn": "🔧 Manual fix in browser",
        "preflight_warning_title": "Startup Check Warning",
        # preflight_* templates are defined in error_handler.PREFLIGHT_TEXTS_EN
        # and injected into this table further down (see the "Core-produced
        # diagnostic/error texts" block) so the English side can never
        # drift from what error_handler actually renders for the CLI/logs.
        "auth_wizard_title": "Browser sign-in",
        "auth_wizard_url_prompt": "Enter the URL you want to sign into:",
        "auth_wizard_success_title": "Sign-in successful",
        "auth_wizard_success_msg": "Sign-in details were stored in BananaFlow's private per-user app-data folder. You may now resume downloading.",
        "auth_wizard_aborted_title": "Wizard closed without saving",
        "auth_wizard_aborted_msg": "No cookies were saved. The wizard may have been closed before sign-in.",
        "cookie_auth_choice_title": "Sign in for downloads",
        "cookie_auth_choice_body": (
            "YouTube needs a signed-in session for this download. "
            "Cookies may grant account access: treat them like passwords and consider a dedicated account. "
            "Choose how to provide them:"
        ),
        "cookie_auth_choice_app_browser_btn": "🔑 Sign in via app browser",
        "cookie_auth_choice_manual_btn": "📁 Import cookies manually",
        "manual_cookie_import_title": "Manual cookie import",
        "manual_cookie_import_instructions": (
            "1. If you haven't already, install the \"Get cookies.txt LOCALLY\" "
            "extension (safe, open-source — link below).\n"
            "2. Sign in to the site in your regular browser.\n"
            "3. Click the extension and export cookies.txt.\n"
            "4. Come back here and choose that file.\n\n"
            "Cookies may grant account access. Treat the export like a password and delete the original export after import."
        ),
        "manual_cookie_import_open_extension_btn": "🔗 Open extension page",
        "manual_cookie_import_choose_file_btn": "📁 Choose cookies.txt…",
        "resume_downloads_title": "Resume downloads?",
        "resume_downloads_msg": (
            "BananaFlow has {count} unfinished download(s) from a previous session.\n"
            "Would you like to restore and resume them?"
        ),
        "signin_required_title": "Sign-in required",
        "signin_required_detail": (
            "This video requires a signed-in YouTube account.\n\n"
            "Open your regular browser, make sure YouTube is signed in, then retry the download.\n"
            "If browser cookie reading is blocked, export cookies with a browser extension and select the file in Settings."
        ),
        "browser_cookie_read_failed_title": "Could not read browser cookies",
        "browser_cookie_read_failed_detail": (
            "Windows protects and locks your regular browser profile, so BananaFlow will not try to bypass it.\n\n"
            "Use BananaFlow's separate sign-in browser, or import an exported cookies.txt file. "
            "You do not need to close, unlock, or weaken your regular browser."
        ),
        "cancel_btn": "Cancel",
        "details_show_btn": "Show details",
        "details_hide_btn": "Hide details",

        # ── Track card tooltips ─────────────────────────────────────────────────
        "card_remove_tooltip": "Remove from queue",
        "card_pause_tooltip": "Pause download",
        "card_resume_tooltip": "Resume download",

        # ── Options bar labels ──────────────────────────────────────────────────
        "options_type_label": "Type:",
        "options_type_audio": "Audio",
        "options_type_video": "Video",
        "options_video_format_note": "Video is saved as MP4 for maximum compatibility.",
        "options_format_label": "Format:",
        "options_quality_label": "Quality:",
        "options_save_label": "Save to:",
        "options_clipboard_label": "Clipboard:",

        # ── Quality selector labels ─────────────────────────────────────────────
        "quality_best": "Best",
        "quality_high": "High",
        "quality_balanced": "Balanced",
        "quality_economical": "Economical",
        "quality_small_file": "Small file",
        "quality_best_available": "Best available",
        "quality_source_quality": "Source quality",
        "quality_no_additional_lossy_compression": "no additional lossy compression",
        "quality_auto": "Auto",
        "quality_smallest_file": "Smallest file",
        "quality_audio_320": "320 kbps",
        "quality_audio_256": "256 kbps",
        "quality_audio_192": "192 kbps",
        "quality_audio_160": "160 kbps",
        "quality_audio_128": "128 kbps",
        "quality_audio_96": "96 kbps",
        "quality_video_4k": "4K",
        "quality_video_2k": "2K",
        "quality_video_full_hd": "Full HD",
        "quality_video_hd": "HD",
        "quality_video_sd": "SD",
        "quality_video_2160": "2160p",
        "quality_video_1440": "1440p",
        "quality_video_1080": "1080p",
        "quality_video_720": "720p",
        "quality_video_480": "480p",
        "quality_video_360": "360p",
        "quality_tooltip_audio_bitrate": (
            "Controls the output bitrate. Higher values make larger files, "
            "but cannot improve a low-quality source."
        ),
        "quality_tooltip_video_auto": (
            "Downloads the highest quality available. 4K or 8K sources may create very large files."
        ),
        "quality_tooltip_flac": (
            "FLAC avoids another lossy compression step, but cannot restore quality already lost in the source."
        ),

        # ── Converter panel ─────────────────────────────────────────────────────
        "converter_cancel_btn": "⏹  Cancel",
        "converter_convert_all_btn": "Convert All",

        # ── Duplicate files dialog ──────────────────────────────────────────────
        "duplicates_manage_title": "🔍 Manage Duplicate Files",
        "duplicates_strategy_size": "by file size (fast)",
        "duplicates_strategy_md5": "by MD5 content (precise)",
        "duplicates_confidence_same_audio": "High confidence — same extracted audio content",
        "duplicates_confidence_same_file": "High confidence — same complete file bytes",
        "duplicates_confidence_possible": "Possible duplicate — same size only; audio content was not compared",
        "duplicates_partial_warning": "Scan completed with warnings: {n} file(s) could not be checked. Results may be incomplete.",
        "duplicates_partial_detail": "בקובץ {path}: {reason}",
        "duplicates_possible_confirm_msg": "{n} file(s) will be moved to the Recycle Bin. These are possible duplicates based only on equal size; no content comparison was made.",
        "duplicate_stat_failed": "could not read file information",
        "duplicate_read_failed": "could not read file contents",
        "duplicates_header": (
            "Found <b>{n_files}</b> duplicate files in <b>{n_groups}</b> "
            "groups (strategy: {strat}) | scan time: {elapsed:.1f}s"
        ),
        "duplicates_hint": "☑ Checked = keep file    ☐ Unchecked = delete file",
        "duplicates_keep_all_btn": "✅ Keep all",
        "duplicates_keep_all_tooltip": "Mark all files in every group for keep",
        "duplicates_group_label": "Group {n}  —  {count} duplicate files",
        "duplicates_apply_btn": "🗑 Delete & clean up",
        "duplicates_nothing_title": "Nothing to delete",
        "duplicates_nothing_msg": (
            "All files are marked for keeping.\n"
            "Uncheck files you want to delete."
        ),
        "duplicates_confirm_title": "Final delete confirmation",
        "duplicates_confirm_msg": (
            "Warning: this will permanently delete the {n} marked files from disk.\n\n"
            "Are you sure?"
        ),
        "duplicates_confirm_yes": "Yes, delete",
        "duplicates_confirm_no": "No, go back",

        # ── Conflict resolution dialog ──────────────────────────────────────────
        "conflict_sources_count": "{n} sources",
        "conflict_dialog_title": "Manage Duplicates",
        "conflict_dialog_subtitle": "Manage Duplicates — {n} overlapping videos",
        "conflict_videos_header": "📹 Videos / Shorts / Streams",
        "conflict_playlists_header": "📋 Playlists",
        "conflict_explanation": (
            "The following videos were found in more than one source. "
            "Check ✓ the copies you want to download.\n"
            "Different copies will be saved to different folders."
        ),
        "conflict_ok_btn": "Confirm — download all checked",
        "conflict_keep_videos_btn": "✓ Keep in Videos",
        "conflict_keep_playlists_btn": "✓ Keep in Playlists",
        "conflict_keep_both_btn": "✓ Keep both",
        "conflict_clear_all_btn": "✗ Clear all",

        # ── Restart prompt ──────────────────────────────────────────────────────
        "restart_required_title": "Restart required",
        "restart_required_msg": (
            "The language change will take effect after a restart.\n"
            "Restart now?"
        ),
        "restart_now_btn": "Restart now",
        "restart_later_btn": "Later",

        # ── Tray notifications ──────────────────────────────────────────────────
        "tray_minimized_title": "BananaFlow",
        "tray_minimized_message": "Running in the background. Double-click the tray icon to restore.",

        # ── Converter panel (extended) ──────────────────────────────────────────
        "converter_header_title": "Local File Converter",
        "converter_subtitle": (
            "Convert audio files already on your disk to a different format. "
            "Drag files here or use the Add button — no internet connection needed."
        ),
        "converter_drop_hint": "⬆  Drop audio files here or click Add Files",
        "converter_add_files": "Add Files",
        "converter_clear_all": "Clear All",
        "converter_output_format": "Format:",
        "converter_bitrate": "Bitrate:",
        "converter_same_folder": "Same folder as source",
        "converter_output_folder": "Output Folder",
        "converter_select_output_dialog": "Select Output Folder",
        "converter_select_files_dialog": "Select Audio Files",
        "converter_audio_files_filter": "Audio Files",
        "converter_all_files_filter": "All Files",
        "converter_file_x_of_y": "File {x} of {y}",
        "converter_summary": "Finished: {done} converted · {failed} failed · {skipped} skipped · {cancelled} cancelled",
        "converter_collision_title": "Output files already exist",
        "converter_collision_msg": "{n} output file(s) already exist in the destination folder. What should happen to them?",
        "converter_collision_skip": "Skip existing",
        "converter_collision_unique": "Keep both (rename new)",
        "converter_collision_overwrite": "Overwrite existing",
        "converter_collision_abort": "Cancel conversion",
        "converter_wav_warning_title": "Tags and artwork will be lost",
        "converter_wav_warning_msg": "WAV files cannot store the full tag set or embedded artwork. The converted copies will lose this information (the source files are not changed). Continue?",
        "converter_continue_btn": "Continue",
        "converter_abort_btn": "Cancel",
        "converter_status_skipped": "Skipped — destination already exists",
        "converter_status_cancelled": "Cancelled",

        # ── Settings panel (extended) ───────────────────────────────────────────
        "clear": "Clear",
        "select_cookies_file": "Select cookies file (cookies.txt)",
        "accessibility_mode": "Accessibility Mode",
        "accessibility_mode_desc": "High-contrast colours and stronger focus indicators — applies immediately",
        "concurrent_downloads": "Concurrent Downloads",
        "concurrent_downloads_desc": "Number of tracks downloaded simultaneously (1 – 6)",
        "playlist_behaviour": "Playlist Behaviour",
        "playlist_subfolders": "Playlist Sub-folders",
        "playlist_subfolders_desc": "Create a named subfolder for each playlist download",
        "singles_subfolder": "Singles & EPs Sub-folder",
        "singles_subfolder_desc": "Save singles and EPs inside a 'Singles & EPs' category folder (otherwise directly under the Artist folder)",
        "track_index_prefix": "Track Index Prefix",
        "track_index_prefix_desc": "Prefix filenames with 01-, 02- … to preserve playlist order",
        "duplicate_detection": "Duplicate Detection",
        "duplicate_detection_desc": "Action when the output file already exists",
        "duplicate_skip": "Skip silently",
        "duplicate_warn": "Show warning dialog",
        "duplicate_overwrite": "Always overwrite",
        "system_integration": "System Integration",
        "minimise_to_tray": "Minimise to System Tray",
        "minimise_to_tray_desc": "Keep app running in the background when window is closed",
        "global_hotkeys": "Global Hotkeys",
        "global_hotkeys_desc": "Register system-wide keyboard shortcuts (requires restart)",
        "advanced_audio_processing": "⚙  Advanced Audio Processing",
        "sponsorblock_title": "SponsorBlock – Remove Non-Music Segments",
        "sponsorblock_desc": "Automatically cut sponsor reads, intros, and outros from YouTube music videos using the SponsorBlock API",
        "musicbrainz_title": "MusicBrainz Metadata Enrichment",
        "musicbrainz_desc": "After downloading, query MusicBrainz for genre, label, ISRC, release year, and country",
        "lyrics_title": "Lyrics Downloader  [Advanced]",
        "lyrics_desc": "Fetch lyrics automatically and embed them into the file's metadata tags (requires: pip install syncedlyrics)",
        "replay_gain_title": "Replay Gain Analysis  [Advanced]",
        "replay_gain_desc": "Analyse loudness and store playback-adjustment metadata for compatible players. Audio samples are not normalized or changed (requires: rsgain or pip install pyloudnorm soundfile)",
        "square_thumbnails_title": "Square Thumbnail Crop  [Advanced]",
        "square_thumbnails_desc": "Crop the embedded 16:9 YouTube thumbnail to a 1:1 square before embedding — ideal for standard music players (requires: pip install Pillow)",
        "youtube_proxy_title": "YouTube Proxy",
        "youtube_proxy_desc": "HTTP/HTTPS/SOCKS proxy for YouTube downloads (e.g. http://127.0.0.1:7890). Leave empty for direct connection.",
        "accent_color": "Accent Color",
        "expand_square_to_rectangle_title": "Expand square thumbnails to rectangle for video (MP4)",
        "expand_square_to_rectangle_desc": (
            "When downloading a video file with a square thumbnail at the source (like Spotify), "
            "the image will be expanded to a 16:9 rectangle by creating an elegant blurred background."
        ),
        "external_login_title": "Sign in for restricted downloads",
        "external_login_desc": (
            "If a download says it needs sign-in or verification, "
            "authenticate access once here, then try the download again."
        ),
        "external_login_now_btn": "Sign in…",

        # ── YouTube Doctor (diagnostics) ────────────────────────────────────────
        "youtube_doctor_group": "Diagnostics",
        "youtube_doctor_card_title": "YouTube Doctor",
        "youtube_doctor_card_desc": "Check yt-dlp, JS runtime, cookies, and PO Token Provider status for reliable YouTube downloads.",
        "youtube_doctor_run_btn": "Run",
        "youtube_fast_mode_title": "YouTube Fast Mode",
        "youtube_fast_mode_desc": (
            "Download several YouTube videos at once instead of one by one. "
            "Faster, but more likely to trigger errors or sign-in challenges."
        ),
        "youtube_doctor_dialog_title": "YouTube Doctor",
        "youtube_doctor_dialog_subtitle": "Offline diagnostic — checks your local setup only. No data is sent anywhere.",
        "youtube_doctor_cat_yt_dlp_version": "yt-dlp version",
        "youtube_doctor_cat_yt_dlp_ejs": "yt-dlp-ejs",
        "youtube_doctor_cat_js_runtime": "JavaScript runtime",
        "youtube_doctor_cat_cookies": "Cookies",
        "youtube_doctor_cat_po_token_provider": "PO Token Provider",
        "youtube_doctor_cat_reliability_mode": "YouTube reliability mode",
        "youtube_doctor_ready_label": "Ready for public YouTube downloads",
        "youtube_doctor_cookies_label": "Cookies available for gated videos",
        "youtube_doctor_po_label": "PO Token Provider ready",
        "youtube_doctor_yes": "Yes",
        "youtube_doctor_maybe": "Maybe",
        "youtube_doctor_no": "No",
        "youtube_doctor_actions_title": "Recommended actions",

        # ── Channel import (tab selection dialog) ───────────────────────────────
        "import_channel_title": "Import YouTube Channel",
        "import_channel_discovering": "Discovering available tabs…",
        "import_channel_cancel": "Cancel",
        "import_channel_scan_selected": "Scan selected tabs",
        "import_channel_items_count": "{n:,} items",
        "import_channel_error_prefix": "Error discovering tabs: {error}",
        "import_channel_degraded_warning": "Could not read this channel's real tab list — showing the usual tabs as a guess. Some may be empty or missing.",
        "import_channel_scan_complete": "Scan complete — {n:,} items",
        "import_channel_with_name": "Import: {name}",
        "import_channel_tabs_found": "Found {n} tabs — choose what to scan:",
        "import_channel_scanning_selected": "Scanning selected tabs…",
        "import_channel_scanning_tab": "Scanning: {tab}…",
        "import_channel_expanding_playlists": "Expanding playlists: {current}/{total}",
        "import_channel_scrape_error": "Scrape error: {msg}",

        # ── Search result card ──────────────────────────────────────────────────
        "search_card_add_btn": "＋  Add",
        "search_card_browse_btn": "Browse  →",

        # ── Tag Editor: dialogs / headers ───────────────────────────────────────
        "meta_auto_settings_title": "Auto-Order Settings",
        "meta_clean_settings_title": "Clean-up Settings (aggressive)",
        "meta_auto_header": "Choose which actions the 'Auto-Order' button will perform:",
        "meta_auto_album_note": "Note: besides whatever you pick below, 'Auto-Order' always also sets each file's Album to its folder name.",
        "meta_clean_title_group": "Title clean-up (Title)",
        "meta_clean_filename_group": "Physical filename clean-up (Filename)",

        # ── Tag Editor: auto-order operations ───────────────────────────────────
        "meta_op_title_strip_label": "Copy filename to title (without number)",
        "meta_op_title_strip_desc": "Takes the existing filename and copies it into the 'title' field, removing leading numbers (e.g. '01 song' becomes 'song').",
        "meta_op_title_full_label": "Copy filename to title (including number)",
        "meta_op_title_full_desc": "Takes the existing filename and copies it into the 'title' field exactly as it is.",
        "meta_op_normalize_spaces_label": "Remove double spaces and underscores from title",
        "meta_op_normalize_spaces_desc": "Scans the title, replaces underscores (_) with spaces, and removes double or extra spaces.",
        "meta_op_track_num_label": "Extract track number from filename",
        "meta_op_track_num_desc": "Looks for a number at the start of the filename (e.g. '03') and saves it as the track number.",
        "meta_op_split_at_label": "Split filename into 'artist' and 'title'",
        "meta_op_split_at_desc": "Detects a hyphen (-) in the filename. The part before becomes 'artist', the part after becomes 'title'.",
        "meta_op_album_artist_label": "Copy 'artist' to 'album artist'",
        "meta_op_album_artist_desc": "Copies each track's 'artist' into the 'album artist' field too (important for correct album sorting in players).",
        "meta_op_strip_junk_label": "Clean junk words from title",
        "meta_op_strip_junk_desc": "Removes common YouTube additions from the title like '(Official Video)', '[HD]', or 'Lyrics'.",
        "meta_op_clear_comments_label": "Clear 'comments' tag",
        "meta_op_clear_comments_desc": "Completely clears whatever is in the song's comments field.",
        "meta_op_clear_track_num_label": "Clear 'track number' tag",
        "meta_op_clear_track_num_desc": "Completely clears the song's track number.",
        "meta_op_clear_year_label": "Clear 'year' tag",
        "meta_op_clear_year_desc": "Clears the release year from the tags.",
        "meta_op_clear_genre_label": "Clear 'genre' tag",
        "meta_op_clear_genre_desc": "Clears the music style (genre) from the tags.",
        "meta_op_clear_title_label": "Clear 'title' tag",
        "meta_op_clear_title_desc": "Completely clears the song's title.",
        "meta_op_clear_artist_label": "Clear 'artist' tag",
        "meta_op_clear_artist_desc": "Completely clears the song's artist.",
        "meta_op_clear_album_label": "Clear 'album' tag",
        "meta_op_clear_album_desc": "Completely clears the song's album.",
        "meta_op_clear_album_artist_label": "Clear 'album artist' tag",
        "meta_op_clear_album_artist_desc": "Completely clears the song's album artist.",
        "meta_op_clean_filename_label": "Clean physical filename",
        "meta_op_clean_filename_desc": "Cleans the filename itself: removes underscores, strips anything inside parentheses () or [], and normalizes double spaces.",
        "meta_op_strip_filename_numbering_label": "Remove numbering from physical filename",
        "meta_op_strip_filename_numbering_desc": "Removes leading numbering from the physical filename (like '01-', '01 -', or '01_').",

        # ── Tag Editor: inspector rail group titles ─────────────────────────────
        "meta_group_from_filename": "From Filename",
        "meta_group_cleanup": "Clean Up & Clear Tags",
        "meta_section_text_cleanup": "Text Cleanup",
        "meta_section_clear_fields": "Clear Fields",

        # ── Tag Editor: buttons / labels ────────────────────────────────────────
        "meta_cancel": "Cancel",
        "meta_ok": "OK",
        "meta_save_ok": "Save",
        "meta_browse_folder": "  Choose Folder",
        "meta_change_folder": "  Change Folder",
        "meta_no_folder_selected": "No folder selected",
        "meta_include_subdirs": "Include subfolders",
        "meta_auto_btn": "  Auto-Order",
        "meta_apply_changes": "  Apply Changes",
        "meta_revert_changes": "  Revert Changes",
        "meta_undo_changes": "Undo",
        "meta_redo_changes": "Redo",
        "meta_review_changes": "Review Changes",
        "meta_pending_changes": "Pending Changes",
        "meta_stored_value": "Stored Value",
        "meta_proposed_value": "Proposed Value",
        "meta_change_source": "Change Source",
        "meta_change_file": "File",
        "meta_change_field": "Field",
        "meta_change_included": "Included in Apply",
        "meta_change_excluded": "Excluded from Apply",
        "meta_change_summary": "{files} files, {fields} changes; {included} included, {excluded} excluded",
        "meta_change_origin_manual": "Edited manually",
        "meta_change_origin_auto_arrange": "Auto-arranged",
        "meta_change_origin_cleanup": "Cleanup action",
        "meta_change_origin_filename": "Filename edit",
        "meta_change_origin_lyrics": "Lyrics edit",
        "meta_change_origin_replaygain": "Calculated ReplayGain",
        "meta_change_origin_artwork_add": "Artwork added",
        "meta_change_origin_artwork_replace": "Artwork replaced",
        "meta_change_origin_artwork_remove": "Artwork removed",
        "meta_change_origin_restore": "Restored from backup",
        "meta_change_origin_online_metadata": "Online metadata",
        "meta_online_title": "Online Metadata",
        "meta_online_open": "Open Online Metadata",
        "meta_online_explicit_search_hint": "Nothing is sent automatically. Select files, review the search terms, then press Search.",
        "meta_online_scope": "Current lookup scope: {n} file(s)",
        "meta_online_scope_label": "Lookup scope",
        "meta_online_select_files": "Select one or more files before opening Online Metadata.",
        "meta_online_single_track": "Single track",
        "meta_online_selected_album": "Selected files / album",
        "meta_online_search_title": "Title",
        "meta_online_search_artist": "Artist",
        "meta_online_search_album": "Album",
        "meta_online_search_musicbrainz": "Search MusicBrainz",
        "meta_online_retry": "Retry",
        "meta_online_cancel_lookup": "Cancel search",
        "meta_online_searching": "Searching MusicBrainz…",
        "meta_online_candidates": "Candidates",
        "meta_online_candidates_count": "{n} candidate(s) found. Select one to compare.",
        "meta_online_candidate_label": "{title} — {artist} · {score}% confidence",
        "meta_online_comparison": "Local and online metadata comparison",
        "meta_online_use_online": "Use online",
        "meta_online_keep_local": "Keep local",
        "meta_online_field": "Field",
        "meta_online_local_value": "Local value",
        "meta_online_online_value": "Online value",
        "meta_online_status": "Status",
        "meta_online_select_recommended": "Select Recommended Fields",
        "meta_online_clear_selection": "Clear Field Selection",
        "meta_online_artwork_preview": "Preview Artwork",
        "meta_online_use_artwork": "Use this Artwork",
        "meta_online_artwork_loading": "Loading an Artwork preview…",
        "meta_online_release_detail_loading": "Loading the selected release tracks…",
        "meta_online_artwork_final_loading": "Downloading and validating the full Artwork…",
        "meta_online_artwork_none": "No Artwork is available for this release.",
        "meta_online_artwork_invalid_mime": "The Artwork provider returned an unsupported image type.",
        "meta_online_artwork_too_large": "The full Artwork is too large to use safely.",
        "meta_online_artwork_invalid": "The Artwork image is invalid or corrupt.",
        "meta_online_artwork_unsupported": "Artwork cannot be written to the selected file formats.",
        "meta_online_artwork_not_selected": "No online Artwork selected",
        "meta_online_artwork_ready": "Artwork preview is ready. Select it explicitly to use it.",
        "meta_online_artwork_unavailable": "No usable Artwork preview is available.",
        "meta_online_add_pending": "Add to Pending Changes",
        "meta_online_attribution": "Provider attribution",
        "meta_online_attribution_value": "Source: {provider} · {url}",
        "meta_online_confidence_evidence": "Confidence: {score}%. Evidence: {evidence}",
        "meta_online_evidence_component": "{component} {score}%",
        "meta_online_evidence_unavailable": "not enough comparable evidence",
        "meta_online_no_results": "No results",
        "meta_online_offline": "Offline. Check the connection and try again.",
        "meta_online_rate_limited": "MusicBrainz is rate limited. Wait, then retry.",
        "meta_online_timeout": "The provider did not respond in time.",
        "meta_online_provider_unavailable": "The provider is temporarily unavailable.",
        "meta_online_provider_error": "The metadata provider could not complete this request.",
        "meta_online_cancelled": "Search cancelled",
        "meta_online_partial_results": "Partial results — review them before continuing.",
        "meta_online_stale_result": "This result is stale because the selection or pending changes changed. Search again.",
        "meta_online_album_mapping_state": "Album mapping needs review: {unmatched} unmatched, {ambiguous} ambiguous.",
        "meta_online_provider_musicbrainz": "MusicBrainz",
        "meta_online_provider_caa": "Cover Art Archive",
        "meta_online_difference_change": "Different",
        "meta_online_difference_no_op": "No change",
        "meta_online_difference_empty": "Not provided",
        "meta_online_difference_unsupported": "Unsupported",
        "meta_online_difference_ambiguous": "Ambiguous",
        "meta_online_field_title": "Title",
        "meta_online_field_artist": "Artist",
        "meta_online_field_album": "Album",
        "meta_online_field_album_artist": "Album artist",
        "meta_online_field_track_num": "Track number",
        "meta_online_field_track_total": "Track total",
        "meta_online_field_disc_num": "Disc number",
        "meta_online_field_disc_total": "Disc total",
        "meta_online_field_year": "Date",
        "meta_online_field_genre": "Genre",
        "meta_online_field_isrc": "ISRC",
        "meta_online_field_publisher": "Label / publisher",
        "meta_review_all_files": "All files",
        "meta_review_all_types": "All change types",
        "meta_review_all_categories": "All categories",
        "meta_review_all_origins": "All origins",
        "meta_review_all_states": "All states",
        "meta_review_category_metadata": "Metadata",
        "meta_review_category_filename": "Filename",
        "meta_review_category_artwork": "Artwork",
        "meta_review_category_lyrics": "Lyrics",
        "meta_review_category_replaygain": "ReplayGain",
        "meta_review_warning": "Warning",
        "meta_review_warnings": "Warnings",
        "meta_review_blocked": "Blocked",
        "meta_review_counts": "{total} changes; {included} files included, {excluded} excluded, {blocked} blocked; {pending} files remain pending after Apply",
        "meta_review_revert_entries": "Revert Selected Entries",
        "meta_review_revert_files": "Revert Selected Files",
        "meta_review_revert_filename": "Revert Filename",
        "meta_review_revert_artwork": "Revert Artwork",
        "meta_review_revert_lyrics": "Revert Lyrics",
        "meta_review_revert_replaygain": "Revert ReplayGain",
        "meta_review_revert_all": "Revert All",
        "meta_review_blocker_details": "Blocker details",
        "meta_review_missing_target": "The reviewed workspace item is no longer available.",
        "meta_review_stale_target": "The file changed after it was reviewed.",
        "meta_restore_btn": "  Restore from Backup",
        "meta_draft_available_title": "Recover unsaved Tag Editor proposals?",
        "meta_draft_available_message": "A saved proposal draft affects {n} files in {root}. Created: {age}. Restoring it rescans the folder and restores proposals only; no media files are written.",
        "meta_draft_restore": "Restore Draft",
        "meta_draft_discard": "Discard Draft",
        "meta_draft_keep": "Keep for later",
        "meta_draft_unavailable": "The draft root is unavailable.",
        "meta_draft_legacy_conflict": "A second, different draft was found from an earlier version of the app. Nothing was merged or deleted. The draft above is the current one; the older copy was kept here in case you need it:\n{path}",
        "meta_draft_migration_failed": "A draft from an earlier version of the app could not be moved to its new location. It was left untouched and nothing was lost.",
        "meta_draft_restored": "Saved proposals were restored. Review them before Apply.",
        "meta_draft_incompatible": "The saved draft does not match this workspace and was kept for inspection.",
        "meta_draft_unsaved_title": "Pending changes need a decision",
        "meta_draft_unsaved_message": "{operation} cannot replace the workspace while proposals are pending. Apply, revert, or keep the recoverable draft before continuing.",
        "meta_draft_apply": "Review and Apply",
        "meta_draft_keep_action": "Keep Recoverable Draft",
        "meta_draft_review_required": "Review the restored draft in Review Changes before Apply.",
        "md_recovery_unresolved_cannot_discard": "This recovery record is unresolved and cannot be discarded yet.",
        "md_recovery_reconciled": "The already-completed disk result was reconciled; no media was written again.",
        "md_recovery_reconcile_btn": "Reconcile Completed Result",
        "meta_backup_manager": "Backup Manager",
        "meta_backup_manager_note": "Only backups inside BananaFlow's backup directory are shown. Journal-referenced backups are protected.",
        "meta_backup_created": "Created",
        "meta_backup_operation": "Operation",
        "meta_backup_files": "Files",
        "meta_backup_schema": "Schema",
        "meta_backup_app_version": "App version",
        "meta_backup_root": "Source root",
        "meta_backup_status": "Operation ID",
        "meta_backup_size": "Size",
        "meta_backup_validity": "Validity",
        "meta_backup_location": "Location",
        "meta_backup_valid": "Valid",
        "meta_backup_invalid": "Invalid or corrupt",
        "meta_backup_journal_referenced": "Protected by unresolved journal",
        "meta_backup_preview_restore": "Preview Restore",
        "meta_backup_restore": "Restore",
        "meta_backup_undo_batch": "Undo Applied Batch",
        "meta_backup_details": "Details",
        "meta_backup_export": "Export/Copy",
        "meta_backup_delete": "Delete",
        "meta_backup_refresh": "Refresh",
        "meta_backup_preview_message": "The following {n} files would receive metadata restore; no files are written by this preview.",
        "meta_backup_more_files": "… and {n} more files",
        "meta_backup_restore_confirm": "Restore metadata for {n} files from this verified backup?",
        "meta_backup_undo_confirm": "Restore the verified pre-operation metadata. Path reversal is not included and requires separate explicit approval.",
        "meta_backup_delete_protected": "This backup is protected by an active or unresolved operation and cannot be deleted.",
        "meta_backup_delete_confirm": "Delete this backup permanently? This cannot be undone.",
        "meta_restore_tooltip": (
            "Write back the tags saved in an earlier backup — every Apply "
            "automatically creates one first"
        ),
        "meta_find_duplicates": "  Find Duplicates",
        "meta_duplicates_tools_title": "Duplicate Cleanup",
        "meta_problems_cancelled": "Validation was cancelled.",
        "meta_problems_title": "Problems", "meta_problems_empty": "No problems found.",
        "meta_problems_validating": "Validating problems…",
        "meta_problems_error": "Problems could not be validated.", "meta_problems_stale": "Problems are out of date; revalidate before fixing.",
        "meta_problems_all": "All severities", "meta_problems_search": "Search problems",
        "meta_problems_severity": "Severity", "meta_problems_problem": "Problem",
        "meta_problems_file": "File", "meta_problems_fixable": "Fixable",
        "meta_problems_yes": "Yes", "meta_problems_no": "No", "meta_problems_count": "{n} problem(s)",
        "meta_problems_revalidate": "Revalidate", "meta_problems_fix_selected": "Fix Selected",
        "meta_problems_no_safe_fix": "The selected problems do not have one safe shared fix.",
        "meta_problems_value": "Enter the value to add to pending changes:",
        "meta_problems_preview_title": "Preview fixes",
        "meta_problems_preview_body": "Add the entered value to {n} selected problem(s)? No file will be written.",
        "meta_problems_preview_summary": "Preview for {n} file(s): {changed} change(s), new value: {value}. {results}",
        "meta_problems_old_value": "Current value", "meta_problems_new_value": "Proposed value", "meta_problems_return_parameters": "Return to parameters",
        "meta_problems_result": "Result", "meta_problems_details": "Details",
        "meta_problems_add_pending": "Add to Pending Changes",
        "meta_problems_severity_information": "Information", "meta_problems_severity_warning": "Warning",
        "meta_problems_severity_error": "Error", "meta_problems_severity_blocker": "Blocker",
        "meta_problems_all_categories": "All categories", "meta_problems_all_states": "All states", "meta_problems_category": "Category", "meta_problems_state": "State", "meta_problems_field": "Field",
        "meta_problems_select_all": "Select All Filtered", "meta_problems_clear_selection": "Clear Selection",
        "meta_problems_category_basic_metadata": "Basic metadata", "meta_problems_category_numbering": "Numbering", "meta_problems_category_format_capability": "Format/capability", "meta_problems_category_pending_changes": "Pending changes", "meta_problems_category_artwork": "Artwork", "meta_problems_category_filename_path": "Filename/path",
        "meta_problems_state_present": "Present on disk", "meta_problems_state_present_on_disk": "Present on disk", "meta_problems_state_resolved_by_pending": "Resolved by pending changes", "meta_problems_state_introduced_by_pending": "Introduced by pending changes", "meta_problems_state_pending_blocker": "Blocked pending change", "meta_problems_state_changed_excluded": "Changed but excluded",
        "meta_problem_title": "Missing title", "meta_problem_title_body": "A title is missing.",
        "meta_problem_artist": "Missing artist", "meta_problem_artist_body": "An artist is missing.",
        "meta_problem_track": "Invalid track numbering", "meta_problem_track_body": "Track number and total are inconsistent.",
        "meta_problem_disc": "Invalid disc numbering", "meta_problem_disc_body": "Disc number and total are inconsistent.",
        "meta_problem_excluded": "Changed but excluded", "meta_problem_excluded_body": "This changed file is excluded from Apply.",
        "meta_problem_capability": "Pending change blocked", "meta_problem_capability_body": "A pending change is unsupported or blocked.",
        "meta_problem_artwork": "Artwork could not be read", "meta_problem_artwork_body": "Embedded artwork is invalid or unreadable.",
        "meta_problem_missing_title": "Title is required for this editable file.",
        "meta_problem_missing_artist": "Artist is required for this editable file.",
        "meta_problem_numbering_invalid": "The number must be positive and not exceed its known total.",
        "meta_problem_changed_excluded": "This file has pending changes but is excluded from Apply.",
        "meta_problem_proposal_blocked": "A pending field is unsupported or blocked by existing safety evidence.",
        "meta_problem_artwork_invalid": "Artwork is invalid or could not be read.",
        "meta_no_folder_scanned": "No folder scanned",
        "meta_files_folders_header": "Files and Folders",
        "meta_auto_cfg_tooltip": "Configure what Auto-Order will perform",
        "meta_dupes_tooltip": "Scan the folder for duplicate music files",
        "meta_clean_cfg_tooltip": "Clean-up settings",
        "meta_empty_title": "No files to show",
        "meta_empty_body": "Choose a folder and BananaFlow will load your music files here.",
        "meta_loading_scanning_title": "Loading files",
        "meta_loading_scanning_body": "Scanning the selected folder in the background...",
        "meta_loading_apply_title": "Applying changes",
        "meta_loading_apply_body": "Writing the updated tags safely...",
        "meta_loading_restore_title": "Restoring backup",
        "meta_loading_restore_body": "Writing the saved tags back to the files...",

        # ── Tag Editor: inspector ──────────────────────────────────────────────
        "meta_select_files_prompt": "Select files\nor a folder\nto edit",
        "meta_all_checked_files": "All checked files",
        "meta_apply_artist_group": "Apply Artist",
        "meta_artist_placeholder": "Artist name…",
        "meta_apply_artist_btn": "  Apply Artist to Selected",
        "meta_apply_album_group": "Apply Album",
        "meta_album_placeholder": "Album name…",
        "meta_apply_album_btn": "  Apply Album to Selected",
        "meta_tracks_selected_count": "{n} tracks selected",
        "meta_edit_tags_group": "Edit Tags",
        "meta_inspector_no_selection_title": "Select rows to edit",
        "meta_inspector_no_selection_body": "The Inspector edits only selected rows. Filtering, folder navigation, and visible rows do not change pending Apply scope.",
        "meta_inspector_metadata_section": "Metadata",
        "meta_inspector_lyrics_section": "Embedded lyrics",
        "meta_inspector_artwork_section": "Artwork",
        "meta_artwork_add": "Add",
        "meta_artwork_replace": "Replace",
        "meta_artwork_remove": "Remove",
        "meta_artwork_remove_all": "Remove primary from all",
        "meta_artwork_paste": "Paste",
        "meta_artwork_export": "Export stored artwork",
        "meta_artwork_current": "Current stored artwork",
        "meta_artwork_proposed": "Proposed artwork",
        "meta_artwork_pending_removal": "Pending removal",
        "meta_artwork_revert": "Revert pending artwork",
        "meta_artwork_none": "No embedded artwork.",
        "meta_artwork_present": "{n} embedded picture(s).",
        "meta_artwork_mixed": "Mixed artwork — no cover is assumed for all selected files.",
        "meta_artwork_pending": "Pending artwork change. Apply to write it.",
        "meta_artwork_loading": "Loading artwork preview…",
        "meta_artwork_read_only": "Artwork can be viewed but is not safely editable for this format.",
        "meta_artwork_invalid_image": "Choose a valid JPEG or PNG image.",
        "meta_artwork_unsupported_image": "Only JPEG and PNG artwork is supported.",
        "meta_artwork_file_too_large": "The image file is too large.",
        "meta_artwork_dimensions_too_large": "The image dimensions are too large.",
        "meta_artwork_animated": "Animated images cannot be embedded.",
        "meta_artwork_export_title": "Export artwork",
        "meta_artwork_export_collision": "An artwork export file already exists.",
        "meta_artwork_export_invalid_destination": "Choose a valid export folder.",
        "meta_artwork_choose_title": "Choose artwork image",
        "meta_inspector_replaygain_section": "ReplayGain",
        "meta_inspector_file_properties_section": "File properties (read-only)",
        "meta_inspector_clear_short": "Clear",
        "meta_inspector_clear_field": "Propose clearing this field",
        "meta_inspector_empty_value": "Not set",
        "meta_inspector_mixed_value": "Mixed",
        "meta_inspector_pending_marker": "(pending)",
        "meta_inspector_capability_all": "Metadata editing is supported for all {n} selected files.",
        "meta_inspector_capability_some": "Metadata editing is supported for {supported} of {total} selected files.",
        "meta_inspector_capability_none": "The selected files are read-only or their metadata format is unsupported.",
        "meta_inspector_pending_files": "{n} selected file(s) have pending changes.",
        "meta_inspector_field_partial_tooltip": "This field can be changed in {supported} of {total} selected files; unsupported files will be reported and left unchanged.",
        "meta_inspector_field_unsupported_tooltip": "This field cannot be represented safely by the selected format.",
        "meta_inspector_field_pending_tooltip": "This field has a pending proposal.",
        "meta_inspector_partial_scope": "Proposal created for {affected} supported field/file target(s); {unsupported} unsupported target(s) were left unchanged.",
        "meta_inspector_invalid_value_title": "Invalid metadata value",
        "meta_inspector_invalid_value_body": "Check the numeric value for: {fields}.",
        "meta_apply_confirm_title": "Apply pending changes",
        "meta_apply_confirm_body": "Write and verify pending changes for exactly {n} file(s)? A tag backup will be created first.",
        "meta_apply_confirm_button": "Apply changes",
        "meta_lyrics_language": "Language (for example eng or heb)",
        "meta_lyrics_description": "Description",
        "meta_lyrics_propose_replace": "Propose replacement",
        "meta_lyrics_propose_clear": "Propose clear",
        "meta_lyrics_revert_pending": "Revert lyrics proposal",
        "meta_lyrics_none": "No embedded unsynchronized lyrics.",
        "meta_lyrics_present": "Embedded lyrics are present.",
        "meta_lyrics_mixed": "Selected files contain different lyrics. The blank editor does not mean Clear.",
        "meta_lyrics_pending": "A lyrics change is pending Apply.",
        "meta_lyrics_secondary_preserved": "{n} additional lyrics variant(s) will be preserved.",
        "meta_lyrics_synchronized_read_only": "Timed lyrics are present and remain read-only.",
        "meta_lyrics_language_not_supported": "This container stores lyrics text but not language or description.",
        "meta_replaygain_plain_explanation": "ReplayGain stores playback adjustment information for compatible players. It does not normalize, transcode, or change audio samples.",
        "meta_replaygain_track_gain": "Track gain",
        "meta_replaygain_track_peak": "Track peak",
        "meta_replaygain_album_gain": "Album gain",
        "meta_replaygain_album_peak": "Album peak",
        "meta_replaygain_reference_loudness": "Reference loudness",
        "meta_replaygain_analyze_track": "Analyze tracks",
        "meta_replaygain_analyze_album": "Analyze album groups",
        "meta_replaygain_cancel": "Cancel analysis",
        "meta_replaygain_clear_track": "Clear track values",
        "meta_replaygain_clear_album": "Clear album values",
        "meta_replaygain_revert": "Revert ReplayGain proposals",
        "meta_replaygain_album_confirm_title": "Analyze album ReplayGain",
        "meta_replaygain_album_confirm_body": "Analyze {files} selected files as {groups} deterministic album group(s)? {ambiguous} file(s) have insufficient album identity and will receive track values only, never silent album values. The exact files are listed in Details. Analysis creates proposals only.",
        "meta_replaygain_album_group_safe": "Album group:",
        "meta_replaygain_album_group_ambiguous": "Ambiguous (track values only):",
        "meta_property_filename": "Filename",
        "meta_property_path": "Path",
        "meta_property_format": "Format",
        "meta_property_duration": "Duration",
        "meta_property_bitrate": "Bitrate",
        "meta_property_sample_rate": "Sample rate",
        "meta_property_channels": "Channels",
        "meta_property_size": "File size",
        "meta_property_modified": "Modified",
        "meta_property_unavailable": "Properties are unavailable.",
        "meta_property_single_selection_only": "Select one file to see its technical properties.",
        "meta_mixed_placeholder": "empty / mixed",
        "meta_field_title": "Title:",
        "meta_field_artist": "Artist:",
        "meta_field_album": "Album:",
        "meta_field_album_artist": "Album Artist:",
        "meta_field_track": "Track:",
        "meta_field_track_total": "Track total:",
        "meta_field_disc": "Disc:",
        "meta_field_disc_total": "Disc total:",
        "meta_field_date": "Date:",
        "meta_field_genre": "Genre:",
        "meta_field_comment": "Comment:",
        "meta_field_composer": "Composer:",
        "meta_field_publisher": "Publisher:",
        "meta_field_copyright": "Copyright:",
        "meta_field_bpm": "BPM:",
        "meta_field_isrc": "ISRC:",
        "meta_field_grouping": "Grouping:",
        "meta_field_sort_title": "Title sort:",
        "meta_field_sort_artist": "Artist sort:",
        "meta_field_sort_album": "Album sort:",
        "meta_field_sort_album_artist": "Album artist sort:",
        "meta_apply_to_selection": "  Apply to Selection",
        "meta_rename_group": "Rename File",
        "meta_rename_note": "Rename the physical file to match the new title",
        "meta_rename_btn": "  Rename file to match title",

        # ── Tag Editor: clean-up checkboxes ────────────────────────────────────
        "meta_clean_brackets": "Clean brackets with junk (like [HD] etc.)",
        "meta_clean_english_junk": "Clean English junk words (Official, Audio, 4K, Prod...)",
        "meta_clean_hebrew_junk": "Clean Hebrew junk words (cover, remix, live performance...)",
        "meta_clean_punctuation": "Fix spacing, extra hyphens, and pipe separators (|)",
        "meta_clean_filename_brackets": "Smart bracket removal (delete junk, keep feat. etc.)",
        "meta_clean_filename_brackets_tooltip": "If off, blindly removes all brackets including their content.",
        "meta_clean_filename_domains": "Clean download-site residue (y2mate, yt1s, SPOTIFY-DL...)",
        "meta_clean_filename_emojis": "Clean problematic emojis and special characters (!@#$)",
        "meta_clean_filename_spaces": "Fix hyphens and double spaces ( - - )",

        # ── Tag Editor: status / progress / errors ─────────────────────────────
        "meta_choose_music_folder": "Choose Music Folder",
        "meta_delete_to_trash_title": "Move to Recycle Bin",
        "meta_delete_to_trash_body": "{n} file(s) will be moved to the Recycle Bin. Continue?",
        "meta_delete_to_trash_confirm": "Move to Recycle Bin",
        "meta_scanning": "Scanning…",
        "meta_scanning_progress": "Scanning tags… {done}/{total}",
        "meta_searching_duplicates": "Searching for duplicates…",
        "meta_searching_duplicates_progress": "Searching for duplicates… {done}/{total}  ({eta})",
        "meta_writing_tags_progress": "Writing tags… {done}/{total}",
        "meta_done_success_base": "Done: {success} succeeded",
        "meta_done_failed_suffix": ", {fail} failed",
        "meta_done_partial_suffix": ", {partial} partial (rename pending)",
        "meta_done_skipped_suffix": ", {skip} skipped",
        "meta_done_summary_title": "Success",
        "meta_done_with_errors_title": "Completed with errors",
        # Phase 1 — Apply safety (TE-SAFE-*)
        "meta_apply_blocked_title": "Apply blocked",
        "meta_backup_target_failed": "Apply blocked: the backup folder is not usable, so no files were changed.",
        "meta_backup_write_failed": "Apply blocked: the tag backup could not be written, so no files were changed.",
        "meta_apply_cancelled": "Cancelled before this file was written.",
        "meta_apply_write_failed": "Tag write failed — the file was left unchanged.",
        "meta_rename_blocked": "Tags written, but the rename was blocked (kept pending).",
        "meta_rename_failed": "Tags written, but the rename failed (kept pending).",
        "meta_rename_rollback_failed": "A rename could not be rolled back — recovery required.",
        "meta_journal_init_failed": "Apply blocked: the operation journal could not be created, so no files were changed.",
        "meta_journal_transition_failed": "Apply stopped safely: the operation journal could not be updated — recovery is required.",
        "meta_no_duplicates_found": "No duplicates found ({elapsed:.1f}s)",
        "meta_duplicate_search_error": "Duplicate search error: {msg}",
        "meta_files_deleted": "Deleted {success} duplicate files{note}",
        "meta_files_deleted_errors_suffix": " ({fail} errors)",
        "meta_files_count": "{n} files",
        "meta_folders_count": "{n} folders",
        "meta_changes_proposed": "{n} changes proposed",
        "meta_warnings_count": "{n} warnings",
        "meta_total_files": "{total} files",
        "meta_showing_filtered": "Showing {checked} checked of {total}",
        "meta_showing_visible": "Showing {visible} of {total}",
        "meta_n_files_checked": "{n} files checked",
        "meta_n_files_visible": "{n} files visible",
        "meta_apply_scope_label": "{n} files will be applied",
        "meta_exclude_from_apply": "Exclude from Apply",
        "meta_include_in_apply": "Include in Apply",
        "meta_excluded_filter_chip": "Excluded changes ({n})",
        "meta_tracks_selected_summary": "{n} track{plural} selected",
        "meta_nav_back": "Back",
        "meta_nav_forward": "Forward",
        "meta_nav_up": "Up",
        "meta_search_tracks": "Search files, titles, artists…",

        # ── Tag Editor: context menu / dialogs ─────────────────────────────────
        "meta_add_folder": "Add folder",
        "meta_open_file": "Open",
        "meta_reveal_in_explorer": "Show in Explorer",
        "meta_copy_path": "Copy path",
        "meta_move_menu": "Move to…",
        "meta_move_choose_folder": "Choose destination folder",
        "meta_properties": "Properties",
        "meta_properties_item": "{name}\nPath: {path}\nSize: {size} bytes\nModified: {modified}",
        "meta_rename_menu": "Rename",
        "meta_delete_menu": "Delete",
        "meta_new_folder_dialog_title": "Add Folder",
        "meta_new_folder_prompt": "New folder name:",
        "meta_new_folder_default": "New folder",
        "meta_invalid_folder_name": "Invalid folder name.",
        "meta_folder_exists": "A folder with this name already exists.",
        "meta_create_folder_failed": "Failed to create folder:\n{error}",
        "meta_rename_dialog_title": "Rename",
        "meta_rename_prompt": "Enter new name:",
        "meta_target_name_exists": "Target name already exists in this folder.",
        "meta_rename_failed": "Failed to rename:\n{error}",
        "meta_delete_file_title": "Delete File",
        "meta_delete_folder_title": "Delete Folder",
        "meta_delete_confirm": "Move to the Recycle Bin:\n{name}?",
        "meta_delete_recursive_note": "\n(All files inside the folder will be moved too)",
        "meta_delete_failed": "Failed to delete:\n{error}",
        "meta_move_target_exists": "Target already exists:\n{name}",
        "meta_move_failed": "Failed to move file:\n{error}",
        "meta_error_title": "Error",
        "meta_unsupported_format_tooltip": "Unsupported format",
        "meta_format_supported": "Metadata editing is supported for this format.",
        "meta_format_wav_limited": "Limited metadata editing: BananaFlow writes ID3 tags in WAV files; existing RIFF INFO/BWF metadata is left untouched.",
        "meta_format_read_only": "This format can be inspected, but BananaFlow cannot safely edit its metadata.",
        "meta_format_future": "This format is detected but metadata editing is planned for a future update.",

        # ── Downloader hints (authentication / browser / cookies / 403) ────────
        "downloader_auth_required_hint": (
            "💡 YouTube requires authentication (Google account) to continue downloading.\n\n"
            "You have two options:\n"
            "1. Quick sign-in: click 'Fix sign-in' to sign in to your Google account directly from the app (simplest).\n"
            "2. Export cookies: use the 'Get cookies.txt LOCALLY' browser extension to export a text file and pick it in Settings.\n"
            "Extension link: https://chromewebstore.google.com/detail/get-cookiestxt-locally/ccmgnabidkenghhcidlkgeimdbgefecl\n"
        ),
        "downloader_chrome_locked_hint": (
            "💡 Tip: Chrome is locked or encrypted. Close the browser completely and try again.\n"
            "If that doesn't help, use the 'Fix sign-in' button to read Chrome's encrypted cookie file."
        ),
        "downloader_node_missing_hint": (
            "💡 Tip: A JavaScript runtime is missing (needed to solve YouTube's 'puzzles').\n"
            "Run the following commands in your terminal:\n"
        ),
        "downloader_po_token_hint": (
            "💡 Tip: YouTube requires an extra verification component (PO Token) or account sign-in.\n"
            "You may need to refresh your cookies file via the 'Sign-in Wizard' or use the 'Manual fix in browser' button to warm up the Token."
        ),
        "downloader_403_hint": "💡 Tip: Access error (403). You may need to refresh your cookies file or change your IP address.",

        # ── Cookie validator ────────────────────────────────────────────────────
        "cookies_file_not_found": "Cookies file not found: {path}",
        "cookies_read_error": "Error reading cookies file: {exc}",
        "cookies_empty_or_invalid": "Cookies file is empty or invalid.",
        "cookies_all_expired": (
            "⚠️ All cookies have expired! You may receive 403 errors.\n"
            "Re-sign-in via the 'Sign-in Wizard' is recommended."
        ),
        "cookies_missing_login_info": (
            "⚠️ This cookies file has no active YouTube sign-in (missing LOGIN_INFO).\n"
            "Open youtube.com itself, confirm you see your account avatar there, "
            "then re-export cookies while that tab is open."
        ),

        # ── Playwright check ────────────────────────────────────────────────────

        # ── Channel flow status ─────────────────────────────────────────────────
        "channel_discovering_tabs": "Discovering tabs…",
        "channel_import_cancelled": "Channel import cancelled.",
        "channel_items_found": "Found {n:,} items — checking for duplicates…",
        "channel_duplicates_found": "Found {n} duplicates — waiting for user decision…",
        "channel_adding_to_queue": "Adding {n:,} items to queue…",

        # ── Duplicate detector worker ───────────────────────────────────────────
        "dup_calculating": "Calculating…",

        # ── Metadata table headers & row statuses ──────────────────────────────
        "mt_col_filename":     "Filename",
        "mt_col_title":        "Title",
        "mt_col_title_new":    "Title (new)",
        "mt_col_artist":       "Artist",
        "mt_col_artist_new":   "Artist (new)",
        "mt_col_album":        "Album",
        "mt_col_album_new":    "Album (new)",
        "mt_col_track":        "Track",
        "mt_col_track_new":    "Track (new)",
        "mt_col_filename_new": "Filename (new)",
        "mt_col_genre":        "Genre",
        "mt_col_genre_new":    "Genre (new)",
        "mt_col_comment":      "Comments",
        "mt_col_comment_new":  "Comments (new)",
        "mt_more_columns_title": "More Columns",
        "mt_search_columns":     "Search columns…",
        "mt_size_all_to_fit":    "Size all columns to fit",
        "mt_more_columns":       "More…",
        "mt_file_tooltip_path":      "Path: {path}",
        "mt_file_tooltip_type":      "Type: {type}",
        "mt_file_tooltip_status":    "Status: {status}",
        "mt_file_tooltip_new_name":  "New name: {name}",
        "mt_file_type_audio":        "{ext} audio file",
        "mt_file_type_unknown":      "File",
        "mt_status_error":           "Error",
        "meta_a11y_file_tree":       "Files and folders",
        "meta_a11y_file_tree_desc":  "Tree of scanned folders and checked audio files.",
        "meta_a11y_details_table":   "Tag editor file list",
        "meta_a11y_details_table_desc": "Explorer-style table of audio files and proposed metadata.",
        "meta_a11y_table_header":    "File list columns",
        "meta_a11y_zoom_out":        "Zoom out file list",
        "meta_a11y_zoom_value":      "File list zoom percentage",
        "meta_a11y_zoom_in":         "Zoom in file list",
        "meta_a11y_excluded_filter_desc": "Show only changed files you excluded from Apply",
        "meta_a11y_external_filter_desc": "Show only files changed outside this app",
        "meta_a11y_clear_named_field": "Clear {field}",
        "meta_a11y_about_action":    "About {action}",
        "meta_a11y_configure_action": "Configure {action}",
        "meta_a11y_scan_progress":   "Folder scan progress",
        "meta_a11y_clear_search":    "Clear search",
        "meta_file_op_failed":       "Could not complete the operation on “{name}”.",
        "meta_file_op_missing":      "“{name}” no longer exists.",
        "meta_file_op_destination_exists": "Something with that name already exists, so “{name}” was left alone.",
        "meta_file_op_root_escape":  "“{name}” is outside this folder, so it was not changed.",
        "meta_file_op_root_operation": "The folder you opened cannot be changed from here.",
        "meta_file_op_invalid_name": "That name cannot be used on Windows.",
        "meta_file_op_invalid_root": "That folder cannot be opened as a workspace.",
        "meta_file_op_cloud_placeholder": "“{name}” is stored online. Make it available on this device first.",
        "meta_file_op_recursive_move": "A folder cannot be moved into itself.",
        "meta_file_op_missing_parent": "The destination folder no longer exists.",
        "meta_file_op_not_a_folder": "“{name}” is not a folder.",
        "meta_file_op_not_a_file":   "“{name}” is a folder, not a file.",
        "meta_file_op_unsupported_platform": "This action is available on Windows only.",
        "meta_file_op_rename_failed": "“{name}” could not be renamed. It may be open in another program.",
        "meta_file_op_move_failed":  "“{name}” could not be moved. It may be open in another program.",
        "meta_file_op_recycle_failed": "“{name}” could not be sent to the Recycle Bin. It may be open in another program.",
        "meta_file_op_create_folder_failed": "The folder could not be created.",
        "meta_file_op_properties_failed": "The details of “{name}” could not be read.",

        # ── Metadata controller status messages ────────────────────────────────
        "md_scanning_folder": "Scanning: {folder}…",
        "md_auto_changes_proposed": "Auto-Order: {n} changes proposed",
        "md_auto_no_changes": "Auto-Order: all files are already organised",
        "md_artist_applied": "Artist '{artist}' applied to {n} file(s)",
        "md_album_applied": "Album '{album}' applied to {n} file(s)",
        "md_no_changes_to_apply": "No changes to apply to the selected files",
        "md_writing_tags_to_n": "Writing tags to {n} file(s)…",
        "md_replaygain_no_supported_files": "None of the selected files can safely store ReplayGain tags.",
        "md_replaygain_analysis_started": "Analysing loudness for {n} file(s)…",
        "md_replaygain_analysis_complete": "ReplayGain proposals are ready for {n} file(s). Press Apply to write them.",
        "md_replaygain_analysis_partial": "ReplayGain proposals ready for {done} file(s); {fail} analysis failure(s).",
        "md_replaygain_analysis_cancelled": "ReplayGain analysis cancelled after {n} file(s). Completed proposals were kept.",
        "md_replaygain_stale_results": "ReplayGain results belonged to an older selection and were ignored.",
        "md_album_artist_copied": "Album artist copied from artist ({n} file(s))",
        "md_artist_title_split_done": "Artist-title split completed ({n} file(s))",
        "md_year_cleared": "Year cleared",
        "md_genre_cleared": "Genre cleared",
        "md_track_num_cleared": "Track number cleared",
        "md_title_cleared": "Title cleared",
        "md_artist_cleared": "Artist cleared",
        "md_album_cleared": "Album cleared",
        "md_album_artist_cleared": "Album artist cleared",
        "md_spaces_normalised": "Spaces normalised in {n} title(s)",
        "md_clean_settings_empty": "Clean-up settings are empty — no changes made",
        "md_junk_removed": "Junk removed from {n} title(s)",
        "md_filename_cleaned": "Physical filename cleaned for {n} file(s)",
        "md_filename_numbering_removed": "Numbering removed from filenames for {n} file(s)",
        "md_filename_from_title": "Filename set from title for {n} file(s)",
        "md_searching_duplicates_in": "Searching for duplicates in {folder}…",
        "md_duplicates_deleted": "Deleted {success} duplicate file(s){note}",
        "md_duplicates_deleted_errors_suffix": ", {fail} errors",
        "md_all_changes_reverted": "All changes reverted",
        "md_scan_done": "Scanned {n} files in {folders} folder(s)",
        "md_scan_error": "Scan error: {msg}",
        "md_writing_tags_progress": "Writing tags… {done}/{total}",
        "md_apply_done": "Done — {success} succeeded, {fail} failed, {skip} skipped{bp_note}",
        "md_apply_done_backup_note": " (backup: {name})",
        "md_apply_backup_aborted": "Apply blocked — the backup could not be created, so no files were changed.",
        "md_busy_disk_op": "A Tag Editor write is already in progress — please wait for it to finish.",
        "md_recovery_no_backup": "Recovery: no backup was recorded for the interrupted operation.",
        "md_recovery_failed": "Recovery failed: {detail}",
        "md_recovery_prompt_title": "Recover interrupted Tag Editor operation?",
        "md_recovery_prompt_msg": "A previous Tag Editor apply did not finish ({n} file(s) unresolved). Restore the original tags (and filenames) from its backup?",
        "md_recovery_restore_btn": "Restore from backup",
        "md_recovery_notnow_btn": "Not now",
        "md_recovery_forget_btn": "Forget recovery",
        "md_recovery_forget_title": "Forget recovery permanently?",
        "md_recovery_forget_msg": "This permanently deletes the recovery journal. The interrupted operation can no longer be recovered automatically. Continue?",
        "md_recovery_running": "Recovering interrupted operation…",
        "md_recovery_preflight_failed": "Recovery blocked: the backup is missing or invalid ({code}). Nothing was changed.",
        "md_recovery_done": "Recovery complete — {restored} file(s) restored.",
        "md_recovery_incomplete": "Recovery incomplete — {failed} failed, {missing} missing. The journal was kept so you can retry.",
        "md_recovery_forgotten": "Recovery journal deleted.",
        "md_shutdown_still_finishing": "A Tag Editor write is still finishing safely — the app will stay open until it completes.",
        "md_restoring_tags": "Restoring tags to {n} files…",
        "md_restoring_progress": "Restoring tags… {done}/{total}",
        "md_restore_done": (
            "Restore finished — {restored} restored, {unchanged} already matched, "
            "{missing} missing, {fail} failed"
        ),
        "md_restore_summary_title": "Restore complete",
        "md_restore_pick_title": "Choose a tag backup to restore",
        "md_restore_invalid_title": "Not a valid backup file",
        "md_restore_invalid_msg": (
            "This file is not a tag backup created by BananaFlow, or it could not be read."
        ),
        "md_restore_empty_msg": "This backup file contains no tracks.",
        "md_restore_all_missing_msg": (
            "None of the files recorded in this backup exist on disk anymore, "
            "so there is nothing to restore."
        ),
        "md_restore_confirm_title": "Restore tags from backup?",
        "md_restore_confirm_msg": (
            "The tags saved in {backup} will be written back to {n} files.\n"
            "Only the tags change — no file is deleted, moved, or renamed."
        ),
        "md_restore_missing_note": "{n} files from the backup no longer exist and will be skipped.",
        "md_restore_more_files": "…and {n} more files",
        "md_restore_confirm_btn": "Restore tags",
        "md_duplicates_found_summary": "Found {n_files} duplicates in {n_groups} groups ({strat}, {elapsed:.1f}s)",
        "md_strategy_size": "file size",
        "md_strategy_md5": "MD5",

        # ── Folder names (channel output structure) ─────────────────────────────
        "folder_videos": "Videos",
        "folder_shorts": "Shorts",
        "folder_live": "Live Streams",
        "folder_playlists": "Playlists",
        "folder_releases": "Releases",
        "folder_podcasts": "Podcasts",
        "folder_singles_eps": "Singles & EPs",
        "folder_singles_eps_variants": "Singles & EPs",
        "folder_albums": "Albums",
        "folder_live_performances": "Live Performances",

        # ── About ───────────────────────────────────────────────────────────────
        "about_app": "About",
    },

    "he": {
        # ── Navigation ──────────────────────────────────────────────────────────
        "app_name": "BananaFlow מנהל הורדות",
        "queue": "תור",
        "search": "חיפוש",
        "history": "היסטוריה",
        "settings": "הגדרות",
        "tag_editor": "עורך תגיות",
        "converter": "ממיר",

        # ── Download bar ────────────────────────────────────────────────────────
        "no_tracks_selected": "לא נבחרו שירים",
        "download_selected": "הורד פריטים שנבחרו",
        "download_downloading": " מוריד…",
        "downloads_header_title": "מה נוריד היום?",
        "downloads_header_subtitle": "הזן קישור מ-YouTube או Spotify (פלייליסטים, אלבומים, שירים ואמנים) – המערכת תאתר ותוריד אוטומטית את קטעי השמע התואמים.",
        "downloads_empty_title": "מוכן כשהתוכן יגיע",
        "downloads_empty_subtitle": "הדבק קישור למעלה כדי לטעון מידע, תצוגה מקדימה ופעולות.",
        "search_empty_title": "חפש מוזיקה, סרטונים או פלייליסטים",
        "search_empty_subtitle": "בחר פלטפורמה והקלד שאילתה למעלה כדי להתחיל.",
        "history_header_title": "היסטוריית הורדות",
        "history_header_subtitle": "הורדות שהושלמו נשמרות כאן אוטומטית.",
        "history_empty_title": "אין עדיין הורדות",
        "history_empty_subtitle": "כל הורדה שתסתיים תופיע כאן לגישה מהירה.",
        "converter_drop_title": "גרור קבצים לכאן",
        "converter_drop_subtitle": "או לחץ על הוסף קבצים כדי לבחור קבצי שמע מהמחשב.",

        # ── Error dialogs ───────────────────────────────────────────────────────



        "cannot_write_output_title": "לא ניתן לכתוב לתיקיית הפלט",
        "cannot_write_output_detail": "לא ניתן ליצור את התיקייה:\n{path}\n\nשגיאה: {exc}",

        # ── Status bar ──────────────────────────────────────────────────────────
        "ready": "מוכן.",
        "cancel": "ביטול",
        "status_starting": "מתכונן להורדה…",
        "status_downloading_progress": "מוריד {done} מתוך {total} · {pct}%",
        "status_paused": "ההורדות מושהות",
        "status_cancelling": "מבטל…",
        "status_completed_summary": "{n} הורדות הושלמו.",
        "status_completed_with_preexisting": "{completed} מתוך {total} הושלמו — {downloaded} הורדו, {preexisting} כבר היו קיימים.",
        "status_completed_with_errors": "{ok} הושלמו, {failed} נכשלו.",
        "status_stopped_summary": "ההורדות נעצרו — {done} מתוך {total} הושלמו.",
        "status_stopped_error": "ההורדות נעצרו עקב שגיאה.",
        "phase_matching":    "מחפש התאמה…",
        "phase_waiting":     "ממתין לתור…",
        "phase_starting":    "מתחיל…",
        "phase_downloading": "מוריד",
        "phase_processing":  "מסיים…",
        "spotify_metadata_invalid_card": "פרטי הרצועה מ-Spotify אינם תקינים — הרצועה לא תורד.",
        "eta_calculating": "מחשב…",
        "eta_about_left": "בערך {time} נותרו",
        "eta_range_left": "נותרו בערך {low}–{high}",
        "eta_current_speed_left": "בערך {time} בקצב הנוכחי",
        "eta_tooltip": "הערכת הזמן הנותר לאצווה כולה",
        "starting_downloads": "מתחיל הורדה של {n} פריטים…",
        "download_progress_count": "מוריד {current} מתוך {total}…",
        "cancelling": "מבטל…",
        "status_offline": "אין חיבור לאינטרנט.",
        "status_online": "חיבור האינטרנט חזר.",
        "status_batch_done": "כל ההורדות הסתיימו.",
        "status_batch_cancelled": "ההורדות בוטלו.",

        # ── Offline banner ──────────────────────────────────────────────────────
        "offline_banner_msg": "אין חיבור לאינטרנט — חיפוש והורדות מושהים עד שהחיבור יחזור.",
        "offline_banner_close": "סגור",

        # ── Toasts / small notifications ────────────────────────────────────────
        "download_toast_title": "ההורדה הושלמה",
        "download_toast_fallback": "השיר נשמר.",
        "clipboard_toast_title": "לוח ההעתקה",
        "clipboard_toast_detected": "זוהה קישור: {url}",

        # ── Duplicate-file confirmation (pre-download check) ────────────────────
        "duplicate_detected_title": "זוהה קובץ כפול",
        "duplicate_detected_msg": (
            "\"{title}\" כבר קיים:\n{path}\n\n"
            "להוריד שוב ולהחליף את הקובץ הקיים?"
        ),

        # ── Batched duplicate confirmation (one dialog for the whole batch) ─────
        "batch_duplicates_title": "נמצאו קבצים כפולים",
        "batch_duplicates_subtitle_one": "קובץ אחד כבר קיים בתיקיית היעד.",
        "batch_duplicates_subtitle_many": "{n} קבצים כבר קיימים בתיקיית היעד.",
        "batch_duplicates_skip_all_btn": "דלג על הכל",
        "batch_duplicates_replace_all_btn": "החלף הכל",

        # ── File dialogs ────────────────────────────────────────────────────────
        "choose_download_folder": "בחר תיקיית הורדה",

        # ── URL bar ─────────────────────────────────────────────────────────────
        "fetching_button": "טוען…",
        "fetch_info_button": "הצג מידע",
        "paste_tooltip": "הדבק מהלוח",
        "batch_import_tooltip": "ייבא כתובות URL מקובץ .txt",
        "scrape_tooltip": "סרוק דף זה למציאת קישורי מדיה",
        "clipboard_on_tooltip": "ניטור הלוח פעיל — זיהוי אוטומטי של קישורי מדיה",
        "clipboard_off_tooltip": "ניטור הלוח כבוי — הפעל בהגדרות",
        "url_placeholder": "הדבק קישור מ-YouTube או Spotify (שיר, אלבום, אמן, פלייליסט) או רשום שאילתת חיפוש…",
        "invalid_url_title": "כתובת URL לא תקינה",
        "invalid_url_detail": "זה נראה כמו כתובת URL שבורה או לא שלמה. בדוק את הכתובת ונסה שוב.",

        # ── Batch import ────────────────────────────────────────────────────────
        "batch_import_failed": "ייבוא אצווה נכשל",
        "no_urls_found": "לא נמצאו קישורים נתמכים בקובץ {filename}",
        "batch_import_progress": "מייבא קישור {current} מתוך {total}…",
        "batch_import_complete": "הייבוא הושלם: {success} הצליחו, {failed} נכשלו, {skipped} דולגו.",
        "batch_import_cancelled": "הייבוא בוטל: {success} הצליחו, {failed} נכשלו, {skipped} דולגו, {remaining} לא עובדו.",

        # ── Scraper ─────────────────────────────────────────────────────────────
        "scrape_multi_found": "נמצאו {count} קישורי מדיה. הקישור הראשון נטען — לחץ הצג מידע להתחיל.",
        "scraping": "סורק את הדף לאיתור מדיה…",
        "scrape_no_urls": "לא נמצאו קישורי מדיה בדף.",

        # ── Fetch status ────────────────────────────────────────────────────────
        "fetching": "טוען מידע…",
        "fetching_progress": "טוען רצועה {n} מתוך {total}…",
        "fetching_single": "טוען: {title}",
        "fetch_done": "נטענו {n} רצועות — בחר ולחץ הורדה.",
        "collecting_catalog": "אוסף קטלוג…",
        "found_n_tracks": "נמצאו {n} שירים",

        # ── Settings panel ──────────────────────────────────────────────────────
        "settings_section_basic": "בסיסי",
        "settings_section_advanced": "מתקדם",
        "settings_section_expert": "מומחה ואבחון",
        "signin_group": "התחברות",
        "appearance": "מראה",
        "theme": "ערכת נושא",
        "switch_theme": "החלף ערכת נושא",
        "language": "שפה",
        "select_language": "בחר שפת ממשק",

        "downloads_group": "הורדות",
        "embed_thumbnail": "הטמע תמונה ממוזערת",
        "embed_thumbnail_desc": "כתוב עטיפה לתוך מטא-דאטה של הקובץ (ID3 / MP4)",
        "embed_metadata": "הטמע מטא-דאטה",
        "embed_metadata_desc": "כתוב כותרת, אמן, אלבום ושנה לתוך הקובץ",

        "features": "תכונות",
        "clipboard_monitor": "ניטור לוח",
        "clipboard_monitor_desc": (
            "זיהוי אוטומטי של קישורי YouTube / Spotify שהועתקו ללוח "
            "ומילוי שורת ה-URL באופן אוטומטי"
        ),
        "check_updates": "בדוק עדכונים בהפעלה",
        "check_updates_desc": (
            "בדוק בשקט את GitHub Releases ואת PyPI בעת ההפעלה והצג "
            "התראה כשהאפליקציה או הרכיבים שלה מיושנים"
        ),

        "search_group": "חיפוש",
        "max_youtube_results": "מקסימום תוצאות YouTube",
        "max_youtube_results_desc": "מספר מקסימלי של תוצאות לחיפושי YouTube (1 – 100)",
        "max_spotify_results": "מקסימום תוצאות Spotify",
        "max_spotify_results_desc": "מספר מקסימלי של תוצאות לחיפושי Spotify (1 – 100)",
        "spotify_proxy": "כתובת שרת פרוקסי ל-Spotify",
        "spotify_proxy_desc": "כתובת ה-URL של שרת הפרוקסי ל-Spotify (למשל http://localhost:8000)",

        "spotify_group": "Spotify",
        "spotify_proxy_api_key": "מפתח API לאפליקציה (App API Key)",
        "spotify_proxy_api_key_desc": "טוקן אבטחה לשרת הפרוקסי (נשלח כ-X-App-Token)",

        "authentication": "אימות",
        "cookies_file": "קובץ עוגיות",
        "cookies_file_unset": "לא הוגדר — לחץ עיון לבחירת קובץ cookies.txt",
        "cookies_file_configured": "מוגן עבור חשבון Windows שלך בתיקיית הנתונים הפרטית של BananaFlow — לחץ עיון כדי לעדכן",
        "cookies_updated_title": "קובץ העוגיות עודכן",
        "cookies_updated_msg": "קובץ העוגיות השמור עודכן בהצלחה.",
        "cookies_store_failed_title": "העוגיות לא נשמרו",
        "cookies_store_failed_msg": "BananaFlow לא הצליח לכתוב בבטחה או להגביל את הגישה לקובץ העוגיות הפרטי. הקובץ המיובא לא הוגדר.",
        "browse": "עיון…",
        "clear_cookies": "מחק נתוני התחברות שמורים",
        "clear_cookies_title": "מחיקת נתוני אימות שמורים",
        "clear_cookies_desc": "מחק את קובץ העוגיות של BananaFlow ואת פרופיל דפדפן ההתחברות הייעודי",
        "clear_cookies_confirm": (
            "עוגיות עשויות להעניק גישה לחשבון ויש להתייחס אליהן כמו לסיסמה.\n\n"
            "למחוק לצמיתות את העוגיות השמורות של BananaFlow ואת פרופיל דפדפן ההתחברות הייעודי? "
            "הפעולה לא משנה את הדפדפנים הרגילים, ההורדות, ההיסטוריה או הגדרות אחרות."
        ),
        "clear_cookies_confirm_yes": "מחק נתוני אימות",
        "clear_cookies_confirm_no": "השאר אותם",
        "clear_cookies_failed_title": "נתוני האימות לא נמחקו במלואם",
        "clear_cookies_failed_msg": "חלק מנתוני האימות של BananaFlow נעולים או שלא ניתן היה למחוק אותם. סגור חלונות דפדפן התחברות ונסה שוב.",
        "clear_cookies_success_title": "נתוני האימות נמחקו",
        "clear_cookies_success_msg": "העוגיות השמורות של BananaFlow ופרופיל דפדפן ההתחברות הייעודי הוסרו.",

        "about": "אודות",

        # ── History panel ───────────────────────────────────────────────────────
        "search_history_placeholder": "חפש בהיסטוריה לפי כותרת או אמן…",
        "export_csv": "ייצא CSV",
        "clear_history": "נקה היסטוריה",
        "records_count": "{n} רשומות",
        "col_date": "תאריך",
        "col_title_artist": "כותרת / אמן",
        "col_platform": "פלטפורמה",
        "col_type": "סוג",
        "col_duration": "משך",
        "col_size": "גודל",
        "col_actions": "פעולות",
        "history_empty_hint": (
            "היסטוריית ההורדות תופיע כאן.\n"
            "הורדות שהושלמו נרשמות אוטומטית."
        ),
        "export_dialog_title": "ייצא היסטוריה כ-CSV",
        "export_complete": "הייצוא הושלם",
        "export_complete_msg": "יוצאו {count} רשומות אל:\n{path}",
        "export_failed": "הייצוא נכשל",
        "export_failed_msg": "לא ניתן לכתוב קובץ CSV:\n{error}",
        "clear_history_title": "נקה היסטוריית הורדות",
        "clear_history_confirm": "פעולה זו תמחק לצמיתות את כל הרשומות.\n\nהאם אתה בטוח?",

        # ── Search panel ────────────────────────────────────────────────────────
        "search_placeholder": "חפש שירים, אלבומים, אמנים…",
        "searching": "מחפש…",
        "no_results": "לא נמצאו תוצאות.",
        "results_count": "{n} תוצאות",
        "clear_results": "נקה תוצאות",
        "search_empty_hint": (
            "חפש מוזיקה, סרטונים או פלייליסטים.\n"
            "בחר פלטפורמה והקלד שאילתה למעלה."
        ),
        "platform_youtube": "YouTube",
        "platform_ytmusic": "YouTube Music",
        "platform_spotify": "Spotify",
        "platform_both": "הכל",

        "search_filter_all": "הכל",
        "search_filter_tracks": "שירים",
        "search_filter_albums": "אלבומים",
        "search_filter_artists": "אמנים",
        "search_filter_playlists": "פלייליסטים",
        "search_filter_channels": "ערוצים",

        # ── Queue panel ─────────────────────────────────────────────────────────
        "queue_label": "תור הורדה",
        "no_tracks_loaded": "אין פריטים בתור",
        "select_deselect_all": "בחר / בטל הכל",
        "clear_completed": "נקה שהושלמו",
        "clear_selected": "נקה שנבחרו",
        "clear_all": "נקה הכל",
        "clear_options": "נקה…",
        "pause_all": "השהה הכל",
        "resume_all": "המשך הכל",
        "sel_of_n": "{sel} / {n} נבחרו",
        "queue_stats_done": "· {done}/{total} הושלמו",
        "queue_empty_hint": (
            "הדבק קישור YouTube או Spotify למעלה\n"
            "ולחץ  הצג מידע  לטעינת פריטים."
        ),

        # ── Update system ───────────────────────────────────────────────────────
        "updates_group": "עדכונים",
        "update_check_btn": "בדוק עכשיו",
        "check_app_updates_title": "בדוק עדכוני אפליקציה",
        "check_app_updates_desc": (
            "מומלץ: עדכון BananaFlow הוא מסלול העדכון הראשי — כל גרסה כוללת "
            "את רכיבי ההורדה הבדוקים"
        ),
        "check_component_updates_title": "בדוק עדכוני רכיבים (מתקדם)",
        "check_component_updates_desc": (
            "למשתמשים מתקדמים ולהתקנות מקוד מקור. משתמשים רגילים מקבלים את "
            "הרכיבים האלה (yt-dlp, yt-dlp-ejs) אוטומטית עם עדכוני BananaFlow"
        ),
        "update_check_failed_title": "בדיקת העדכון נכשלה",
        "update_check_failed_msg": (
            "לא ניתן להגיע לשרת העדכונים. בדוק את חיבור האינטרנט ונסה שוב."
        ),
        "up_to_date_title": "הכל מעודכן",
        "app_up_to_date_msg": "BananaFlow {version} היא הגרסה האחרונה שפורסמה.",
        "components_up_to_date_msg": "כל הרכיבים מעודכנים:  {versions}",

        "update_prompt_title": "עדכונים זמינים",
        "update_prompt_subtitle": "שום דבר לא מותקן ללא אישורך.",
        "update_prompt_app_line": "גרסה חדשה של BananaFlow זמינה: {new}  (ברשותך v{cur}).",
        "update_prompt_app_note": (
            "בחירה ב“פתח עמוד הורדה” תפתח את עמוד הגרסה של BananaFlow בדפדפן, שם "
            "תוכל להוריד ולהתקין את הגרסה החדשה. BananaFlow אינו מתעדכן אוטומטית."
        ),
        "update_prompt_app_includes_components": (
            "עדכון זה כולל גם רכיבי הורדה מעודכנים ({names}) — עדכון BananaFlow "
            "הוא כל מה שצריך לעשות."
        ),
        "update_prompt_components_heading": "רכיבי הורדה מיושנים:",
        "update_prompt_component_line": "{name}:  {cur}  →  {new}",
        "update_prompt_component_note": (
            "אתה מריץ את BananaFlow מקוד מקור, ולכן ניתן לעדכן את הרכיבים "
            "במקום: בחירה ב“עדכן רכיבים” מריצה 'pip install --upgrade "
            "yt-dlp[default]' בסביבה של BananaFlow. לאחר מכן נדרשת הפעלה מחדש של "
            "BananaFlow כדי שהגרסאות החדשות ייכנסו לתוקף. מומלץ מאוד לשמור על "
            "yt-dlp מעודכן — גרסאות מיושנות הן הסיבה הנפוצה ביותר להורדות "
            "YouTube שנכשלות."
        ),
        "update_prompt_frozen_note": (
            "רכיבי ההורדה ארוזים בתוך גרסת ההתקנה הזו של BananaFlow ומתעדכנים "
            "יחד עם האפליקציה, כך שאין צורך להתקין דבר בנפרד. כאשר יתפרסם "
            "עדכון BananaFlow שכולל רכיבים חדשים יותר, ההתראה הזו תפנה אליו. "
            "“פתח עמוד הורדה” מציג את הגרסה האחרונה הזמינה — אם אין עדיין "
            "גרסה חדשה יותר, בדוק שוב מאוחר יותר."
        ),
        "update_get_app_btn": "פתח עמוד הורדה",
        "update_components_btn": "עדכן רכיבים",
        "update_open_releases_btn": "פתח עמוד הורדה",
        "update_remind_btn": "הזכר לי מאוחר יותר",
        "update_remind_next_launch": "בהפעלה הבאה",
        "update_remind_1_day": "בעוד יום",
        "update_remind_3_days": "בעוד 3 ימים",
        "update_remind_7_days": "בעוד שבוע",
        "update_skip_btn": "דלג על גרסה זו",
        "component_install_running": "מעדכן רכיבים… זה עשוי לקחת דקה.",
        "component_install_ok_msg": (
            "הרכיבים עודכנו בהצלחה. הפעל מחדש את BananaFlow כדי שהגרסאות "
            "החדשות ייכנסו לתוקף."
        ),
        "component_install_failed_msg": (
            "עדכון הרכיבים נכשל. אפשר לנסות שוב, או להריץ ידנית "
            "'pip install --upgrade yt-dlp[default]'.\n\nפרטים: {detail}"
        ),

        # ── Browser cookies ─────────────────────────────────────────────────────
        "browser_cookies":      "מקור עוגיות דפדפן",
        "browser_cookies_desc": "קרא קובצי עוגיות מהדפדפן שלך כדי לאמת גישה לתוכן המוגבל בגיל או שמיועד רק לחברים",
        "browser_cookie_migrated_title": "הגדרת עוגיות הדפדפן עודכנה",
        "browser_cookie_migrated_msg": "BananaFlow הסיר הגדרה ישנה לקריאת עוגיות ישירות מ־Chrome, Edge, Brave או Chromium, משום ש-Windows אינו מאפשר עוד גישה כזו באופן בטוח ואמין. יש להשתמש בעוזר ההתחברות המבודד של BananaFlow או לייבא cookies.txt. התמיכה ב-Firefox נשארה זמינה.",
        "disabled":             "מושבת",

        # ── Release types ───────────────────────────────────────────────────────
        "release_album":        "אלבום",
        "release_single":       "סינגל",
        "release_ep":           "EP",
        "release_playlist":     "פלייליסט",
        "release_compilation":  "אוסף",
        "tracks":               "שירים",
        "items":                "פריטים",

        # ── System tray ─────────────────────────────────────────────────────────
        "tray_tooltip": "BananaFlow",
        "tray_open": "פתח",
        "tray_cancel_all": "בטל את כל ההורדות",
        "tray_quit": "יציאה",
        "tray_all_done": "כל ההורדות הושלמו!",

        # ── Auth / cookie wizard ────────────────────────────────────────────────
        "auth_wizard_open_btn": "🔑 תיקון ההתחברות",
        "auth_wizard_close_btn": "סגור",
        "auth_wizard_manual_btn": "🔧 תיקון ידני בדפדפן",
        "preflight_warning_title": "אזהרת בדיקת מערכת",

        # ── Preflight (startup checks) — keys defined in
        #    error_handler.PREFLIGHT_TEXTS_EN; English side injected above ──────
        "preflight_ffmpeg_missing": (
            "⚠  לא נמצא FFmpeg ב-PATH של המערכת.\n\n"
            "המרת אודיו/וידאו והטמעת תמונות עטיפה לא יעבדו.\n\n"
            "אם התקנת את BananaFlow דרך קובץ ההתקנה הרשמי, FFmpeg אמור להיות\n"
            "ארוז בתוך תיקיית האפליקציה. אם אתה מריץ מקוד מקור:\n"
            "  Windows : winget install Gyan.FFmpeg\n"
            "  macOS   : brew install ffmpeg\n"
            "  Linux   : sudo apt install ffmpeg\n\n"
            "לאחר מכן הפעל מחדש את BananaFlow."
        ),
        "preflight_no_internet": (
            "⚠  לא זוהה חיבור לאינטרנט.\n\n"
            "טעינת מידע והורדה ייכשלו עד לשחזור החיבור."
        ),
        "preflight_output_dir_not_writable": (
            "⚠  לא ניתן לכתוב לתיקיית ההורדות המוגדרת:\n{detail}\n\n"
            "בחר תיקייה אחרת בהגדרות או בדוק הרשאות."
        ),
        "preflight_cookies_invalid": (
            "⚠  קובץ ה-cookies.txt המוגדר אינו תקין או שלא ניתן לקוראו:\n{detail}\n\n"
            "ייצא עוגיות מחדש או נקה את קובץ העוגיות בהגדרות."
        ),
        "preflight_playwright_missing": (
            "ℹ  Playwright Chromium אינו מותקן.\n\n"
            "רוב ההורדות עובדות בלעדיו, אך התכונות הבאות מושבתות:\n"
            "  • סריקת ערוצים ודיסקוגרפיה של אמנים\n"
            "  • אשף ההתחברות באמצעות עוגיות\n"
            "  • מחלץ הזרמות אוניברסלי (אתרי וידאו כלליים)\n\n"
            "הרץ `python -m playwright install chromium` מתיקיית ההתקנה\n"
            "(או השתמש בסקריפט המצורף `scripts/install_playwright.ps1`) כדי להפעיל אותן."
        ),

        "auth_wizard_title": "התחברות בדפדפן",
        "auth_wizard_url_prompt": "הזן את כתובת האתר שברצונך להתחבר אליו:",
        "auth_wizard_success_title": "ההתחברות הצליחה",
        "auth_wizard_success_msg": "פרטי ההתחברות נשמרו בתיקיית נתוני המשתמש הפרטית של BananaFlow. ניתן להתחיל להוריד מחדש.",
        "auth_wizard_aborted_title": "האשף נסגר ללא שמירה",
        "auth_wizard_aborted_msg": "לא נשמרו עוגיות. ייתכן שהאשף נסגר לפני ההתחברות.",
        "cookie_auth_choice_title": "התחברות להורדות",
        "cookie_auth_choice_body": (
            "ההורדה הזו דורשת חשבון YouTube מחובר. "
            "עוגיות עשויות להעניק גישה לחשבון: התייחס אליהן כמו לסיסמה ושקול להשתמש בחשבון ייעודי. "
            "בחר כיצד לספק אותן:"
        ),
        "cookie_auth_choice_app_browser_btn": "🔑 התחברות דרך דפדפן האפליקציה",
        "cookie_auth_choice_manual_btn": "📁 ייבוא עוגיות ידני",
        "manual_cookie_import_title": "ייבוא עוגיות ידני",
        "manual_cookie_import_instructions": (
            "1. אם עדיין לא הותקן — התקן את התוסף \"Get cookies.txt LOCALLY\" "
            "(בטוח, קוד פתוח — הקישור למטה).\n"
            "2. התחבר לאתר בדפדפן הרגיל שלך.\n"
            "3. לחץ על התוסף וייצא את cookies.txt.\n"
            "4. חזור לכאן ובחר את הקובץ שיוצא.\n\n"
            "עוגיות עשויות להעניק גישה לחשבון. התייחס לקובץ כמו לסיסמה ומחק את קובץ הייצוא המקורי לאחר הייבוא."
        ),
        "manual_cookie_import_open_extension_btn": "🔗 פתח את דף התוסף",
        "manual_cookie_import_choose_file_btn": "📁 בחר קובץ cookies.txt…",
        "resume_downloads_title": "להמשיך הורדות?",
        "resume_downloads_msg": (
            "לתוכנה יש {count} הורדות שלא הסתיימו מהפעלה קודמת.\n"
            "האם לשחזר אותן ולהמשיך?"
        ),
        "signin_required_title": "נדרשת התחברות",
        "signin_required_detail": (
            "הסרטון הזה דורש חשבון YouTube מחובר.\n\n"
            "פתח את הדפדפן הרגיל שלך, ודא שאתה מחובר ל-YouTube, ואז נסה להוריד שוב.\n"
            "אם Windows חוסם קריאת עוגיות מהדפדפן, ייצא עוגיות עם תוסף דפדפן ובחר את הקובץ בהגדרות."
        ),
        "browser_cookie_read_failed_title": "לא ניתן לקרוא עוגיות מהדפדפן",
        "browser_cookie_read_failed_detail": (
            "Windows מגן ונועל את פרופיל הדפדפן הרגיל, ולכן BananaFlow לא ינסה לעקוף זאת.\n\n"
            "יש להשתמש בדפדפן ההתחברות הנפרד של BananaFlow, או לייבא קובץ cookies.txt שיוצא. "
            "אין צורך לסגור, לפתוח נעילה או להחליש את הדפדפן הרגיל."
        ),
        "cancel_btn": "ביטול",
        "details_show_btn": "הצגת פרטים",
        "details_hide_btn": "הסתרת פרטים",

        # ── Track card tooltips ─────────────────────────────────────────────────
        "card_remove_tooltip": "הסר מהתור",
        "card_pause_tooltip": "השהה הורדה",
        "card_resume_tooltip": "המשך הורדה",

        # ── Options bar labels ──────────────────────────────────────────────────
        "options_type_label": "סוג:",
        "options_type_audio": "שמע",
        "options_type_video": "וידאו",
        "options_video_format_note": "וידאו נשמר כ-MP4 לתאימות מרבית.",
        "options_format_label": "פורמט:",
        "options_quality_label": "איכות:",
        "options_save_label": "שמור אל:",
        "options_clipboard_label": "ניטור לוח:",

        # ── Quality selector labels ─────────────────────────────────────────────
        "quality_best": "הכי טובה",
        "quality_high": "גבוהה",
        "quality_balanced": "מאוזנת",
        "quality_economical": "חסכונית",
        "quality_small_file": "קובץ קטן",
        "quality_best_available": "הכי טובה זמינה",
        "quality_source_quality": "איכות המקור",
        "quality_no_additional_lossy_compression": "ללא דחיסה מאבדת נוספת",
        "quality_auto": "אוטומטי",
        "quality_smallest_file": "הקובץ הקטן ביותר",
        "quality_audio_320": "320 kbps",
        "quality_audio_256": "256 kbps",
        "quality_audio_192": "192 kbps",
        "quality_audio_160": "160 kbps",
        "quality_audio_128": "128 kbps",
        "quality_audio_96": "96 kbps",
        "quality_video_4k": "4K",
        "quality_video_2k": "2K",
        "quality_video_full_hd": "Full HD",
        "quality_video_hd": "HD",
        "quality_video_sd": "SD",
        "quality_video_2160": "2160p",
        "quality_video_1440": "1440p",
        "quality_video_1080": "1080p",
        "quality_video_720": "720p",
        "quality_video_480": "480p",
        "quality_video_360": "360p",
        "quality_tooltip_audio_bitrate": (
            "שולט בקצב הסיביות של קובץ הפלט. ערך גבוה יוצר קובץ גדול יותר, "
            "אך לא יכול לשפר מקור שהיה באיכות נמוכה."
        ),
        "quality_tooltip_video_auto": (
            "מוריד את האיכות הגבוהה ביותר הזמינה. כשיש 4K או 8K הקובץ עלול להיות גדול מאוד."
        ),
        "quality_tooltip_flac": (
            "FLAC מונע דחיסה מאבדת נוספת, אך לא משחזר איכות שכבר אבדה במקור."
        ),

        # ── Converter panel ─────────────────────────────────────────────────────
        "converter_cancel_btn": "⏹  ביטול",
        "converter_convert_all_btn": "המר הכל",

        # ── Duplicate files dialog ──────────────────────────────────────────────
        "duplicates_manage_title": "🔍 ניהול קבצים כפולים",
        "duplicates_strategy_size": "לפי גודל קובץ (מהיר)",
        "duplicates_strategy_md5": "לפי תוכן MD5 (מדויק)",
        "duplicates_confidence_same_audio": "ביטחון גבוה — אותו תוכן שמע שחולץ",
        "duplicates_confidence_same_file": "ביטחון גבוה — אותם בתים של הקובץ המלא",
        "duplicates_confidence_possible": "כפילות אפשרית — אותו גודל בלבד; תוכן השמע לא הושווה",
        "duplicates_partial_warning": "הסריקה הסתיימה עם אזהרות: לא ניתן היה לבדוק {n} קבצים. ייתכן שהתוצאות אינן מלאות.",
        "duplicates_partial_detail": "{path}: {reason}",
        "duplicates_possible_confirm_msg": "{n} קבצים יועברו לסל המיחזור. אלו כפילויות אפשריות על פי גודל זהה בלבד; לא בוצעה השוואת תוכן.",
        "duplicate_stat_failed": "לא ניתן לקרוא את פרטי הקובץ",
        "duplicate_read_failed": "לא ניתן לקרוא את תוכן הקובץ",
        "duplicates_header": (
            "נמצאו <b>{n_files}</b> קבצים כפולים ב-<b>{n_groups}</b> "
            "קבוצות (אסטרטגיה: {strat}) | זמן סריקה: {elapsed:.1f}s"
        ),
        "duplicates_hint": "☑ מסומן = שמור קובץ    ☐ לא מסומן = מחק קובץ",
        "duplicates_keep_all_btn": "✅ שמור את כולם",
        "duplicates_keep_all_tooltip": "סמן את כל הקבצים בכל הקבוצות לשמירה",
        "duplicates_group_label": "קבוצה {n}  —  {count} קבצים כפולים",
        "duplicates_apply_btn": "🗑 בצע מחיקה וניקוי",
        "duplicates_nothing_title": "אין מה למחוק",
        "duplicates_nothing_msg": (
            "כל הקבצים מסומנים לשמירה.\n"
            "בטל סימון של קבצים שברצונך למחוק."
        ),
        "duplicates_confirm_title": "אישור מחיקה סופי",
        "duplicates_confirm_msg": (
            "אזהרה: פעולה זו תמחק לצמיתות {n} קבצים מסומנים מהדיסק.\n\n"
            "האם אתה בטוח?"
        ),
        "duplicates_confirm_yes": "כן, מחק",
        "duplicates_confirm_no": "לא, חזור",

        # ── Conflict resolution dialog ──────────────────────────────────────────
        "conflict_sources_count": "{n} מקורות",
        "conflict_dialog_title": "ניהול כפילויות",
        "conflict_dialog_subtitle": "ניהול כפילויות — {n} סרטונים חופפים",
        "conflict_videos_header": "📹 סרטונים / קצרים / שידורים",
        "conflict_playlists_header": "📋 פלייליסטים",
        "conflict_explanation": (
            "הסרטונים הבאים נמצאו ביותר ממקור אחד. "
            "סמן ✓ את העותקים שברצונך להוריד.\n"
            "עותקים שונים יישמרו לתיקיות שונות."
        ),
        "conflict_ok_btn": "אישור — הורד את כל מה שסומן",
        "conflict_keep_videos_btn": "✓ שמור בסרטונים",
        "conflict_keep_playlists_btn": "✓ שמור בפלייליסטים",
        "conflict_keep_both_btn": "✓ שמור שניהם",
        "conflict_clear_all_btn": "✗ נקה הכל",

        # ── Restart prompt ──────────────────────────────────────────────────────
        "restart_required_title": "נדרשת הפעלה מחדש",
        "restart_required_msg": (
            "שינוי השפה ייכנס לתוקף לאחר הפעלה מחדש.\n"
            "להפעיל מחדש כעת?"
        ),
        "restart_now_btn": "הפעל מחדש",
        "restart_later_btn": "מאוחר יותר",

        # ── Tray notifications ──────────────────────────────────────────────────
        "tray_minimized_title": "BananaFlow מנהל הורדות",
        "tray_minimized_message": "פועל ברקע. לחץ פעמיים על אייקון המגש כדי לשחזר.",

        # ── Converter panel (extended) ──────────────────────────────────────────
        "converter_header_title": "ממיר קבצים מקומי",
        "converter_subtitle": (
            "המר קבצי שמע שכבר נמצאים בדיסק לפורמט אחר. "
            "גרור קבצים לכאן או השתמש בכפתור הוספה — לא נדרש חיבור לאינטרנט."
        ),
        "converter_drop_hint": "⬆  גרור קבצי שמע לכאן או לחץ על הוסף קבצים",
        "converter_add_files": "הוסף קבצים",
        "converter_clear_all": "נקה הכל",
        "converter_output_format": "פורמט:",
        "converter_bitrate": "קצב סיביות:",
        "converter_same_folder": "אותה תיקייה כמו המקור",
        "converter_output_folder": "תיקיית פלט",
        "converter_select_output_dialog": "בחר תיקיית פלט",
        "converter_select_files_dialog": "בחר קבצי שמע",
        "converter_audio_files_filter": "קבצי שמע",
        "converter_all_files_filter": "כל הקבצים",
        "converter_file_x_of_y": "קובץ {x} מתוך {y}",
        "converter_summary": "הסתיים: {done} הומרו · {failed} נכשלו · {skipped} דולגו · {cancelled} בוטלו",
        "converter_collision_title": "קבצי פלט כבר קיימים",
        "converter_collision_msg": "{n} קבצי פלט כבר קיימים בתיקיית היעד. מה לעשות איתם?",
        "converter_collision_skip": "דלג על הקיימים",
        "converter_collision_unique": "שמור את שניהם (שם חדש)",
        "converter_collision_overwrite": "החלף את הקיימים",
        "converter_collision_abort": "בטל המרה",
        "converter_wav_warning_title": "תגיות ועטיפה יאבדו",
        "converter_wav_warning_msg": "קבצי WAV אינם יכולים לשמור את כל התגיות או עטיפת אלבום מוטמעת. העותקים המומרים יאבדו מידע זה (קבצי המקור אינם משתנים). להמשיך?",
        "converter_continue_btn": "המשך",
        "converter_abort_btn": "ביטול",
        "converter_status_skipped": "דולג — קובץ היעד כבר קיים",
        "converter_status_cancelled": "בוטל",

        # ── Settings panel (extended) ───────────────────────────────────────────
        "clear": "נקה",
        "select_cookies_file": "בחר קובץ עוגיות (cookies.txt)",
        "accessibility_mode": "מצב נגישות",
        "accessibility_mode_desc": "צבעים בניגודיות גבוהה וסימוני מיקוד בולטים — נכנס לתוקף מיד",
        "concurrent_downloads": "הורדות מקבילות",
        "concurrent_downloads_desc": "מספר השירים שיורדים במקביל (1 – 6)",
        "playlist_behaviour": "התנהגות פלייליסט",
        "playlist_subfolders": "תת-תיקיות פלייליסט",
        "playlist_subfolders_desc": "צור תת-תיקייה בעלת שם לכל הורדת פלייליסט",
        "singles_subfolder": "תת-תיקיית סינגלים ומיני אלבומים",
        "singles_subfolder_desc": "שמור סינגלים ומיני אלבומים בתוך תיקיית קטגוריה ייעודית (אחרת יישמרו ישירות תחת תיקיית האמן)",
        "track_index_prefix": "תחילית מספור רצועה",
        "track_index_prefix_desc": "הוסף לקבצים את הקידומת 01-, 02- … כדי לשמור את סדר הפלייליסט",
        "duplicate_detection": "זיהוי כפילויות",
        "duplicate_detection_desc": "פעולה כאשר קובץ הפלט כבר קיים",
        "duplicate_skip": "דלג בשקט",
        "duplicate_warn": "הצג דיאלוג אזהרה",
        "duplicate_overwrite": "החלף תמיד",
        "system_integration": "אינטגרציה עם המערכת",
        "minimise_to_tray": "מזער למגש המערכת",
        "minimise_to_tray_desc": "השאר את האפליקציה פועלת ברקע כאשר החלון נסגר",
        "global_hotkeys": "קיצורי מקלדת גלובליים",
        "global_hotkeys_desc": "הירשם לקיצורי מקלדת ברמת המערכת (נדרש הפעלה מחדש)",
        "advanced_audio_processing": "⚙  עיבוד שמע מתקדם",
        "sponsorblock_title": "SponsorBlock – הסר קטעים שאינם מוזיקה",
        "sponsorblock_desc": "חתוך אוטומטית קריינויות חסות, פתיחים וסיומות מסרטוני מוזיקה ב-YouTube באמצעות SponsorBlock API",
        "musicbrainz_title": "העשרת מטא-דאטה מ-MusicBrainz",
        "musicbrainz_desc": "לאחר ההורדה, שאל את MusicBrainz לקבלת ז'אנר, לייבל, ISRC, שנת הוצאה ומדינה",
        "lyrics_title": "מוריד מילות שיר  [מתקדם]",
        "lyrics_desc": "הורד מילות שיר אוטומטית והטמע אותן בתגיות הקובץ (נדרש: pip install syncedlyrics)",
        "replay_gain_title": "ניתוח Replay Gain  [מתקדם]",
        "replay_gain_desc": "נתח עוצמת שמע ושמור מטא־דאטה להתאמת השמעה בנגנים תואמים. דגימות השמע אינן מנורמלות או משתנות (נדרש: rsgain או pip install pyloudnorm soundfile)",
        "square_thumbnails_title": "חיתוך תמונה ממוזערת לריבוע  [מתקדם]",
        "square_thumbnails_desc": "חתוך את התמונה הממוזערת של YouTube מ-16:9 לריבוע 1:1 לפני הטמעה — אידיאלי לנגני מוזיקה רגילים (נדרש: pip install Pillow)",
        "youtube_proxy_title": "פרוקסי YouTube",
        "youtube_proxy_desc": "פרוקסי HTTP/HTTPS/SOCKS להורדות YouTube (למשל http://127.0.0.1:7890). השאר ריק להתחברות ישירה.",
        "accent_color": "צבע הדגשה",
        "expand_square_to_rectangle_title": "הרחב תמונות מרובעות למלבן עבור וידאו (MP4)",
        "expand_square_to_rectangle_desc": (
            "כאשר מורידים קובץ וידאו עם תמונה מרובעת במקור (כמו Spotify), "
            "התמונה תורחב למלבן 16:9 על ידי יצירת רקע מטושטש ואלגנטי."
        ),
        "external_login_title": "אימות גישה להורדות מוגבלות",
        "external_login_desc": (
            "אם הורדה מבקשת התחברות או אימות, בצע כאן התחברות פעם אחת "
            "ואז נסה להוריד שוב."
        ),
        "external_login_now_btn": "התחברות…",

        # ── YouTube Doctor (אבחון) ───────────────────────────────────────────────
        "youtube_doctor_group": "אבחון",
        "youtube_doctor_card_title": "אבחון YouTube",
        "youtube_doctor_card_desc": "בדיקת מצב yt-dlp, סביבת JavaScript, עוגיות וספק PO Token להורדות YouTube אמינות.",
        "youtube_doctor_run_btn": "הפעל",
        "youtube_fast_mode_title": "מצב מהיר ל-YouTube",
        "youtube_fast_mode_desc": (
            "הורדת כמה סרטוני YouTube בבת אחת במקום אחד אחרי השני. מהיר "
            "יותר, אך מעלה את הסיכוי לשגיאות או לבקשות התחברות."
        ),
        "youtube_doctor_dialog_title": "אבחון YouTube",
        "youtube_doctor_dialog_subtitle": "בדיקה לא מקוונת — בודקת רק את ההגדרות המקומיות שלך. שום מידע לא נשלח החוצה.",
        "youtube_doctor_cat_yt_dlp_version": "גרסת yt-dlp",
        "youtube_doctor_cat_yt_dlp_ejs": "yt-dlp-ejs",
        "youtube_doctor_cat_js_runtime": "סביבת JavaScript",
        "youtube_doctor_cat_cookies": "עוגיות",
        "youtube_doctor_cat_po_token_provider": "ספק PO Token",
        "youtube_doctor_cat_reliability_mode": "מצב אמינות YouTube",
        "youtube_doctor_ready_label": "מוכן להורדות YouTube ציבוריות",
        "youtube_doctor_cookies_label": "עוגיות זמינות לסרטונים חסומים",
        "youtube_doctor_po_label": "ספק PO Token מוכן",
        "youtube_doctor_yes": "כן",
        "youtube_doctor_maybe": "אולי",
        "youtube_doctor_no": "לא",
        "youtube_doctor_actions_title": "פעולות מומלצות",

        # ── Channel import (tab selection dialog) ───────────────────────────────
        "import_channel_title": "ייבוא ערוץ YouTube",
        "import_channel_discovering": "מאתר לשוניות זמינות…",
        "import_channel_cancel": "ביטול",
        "import_channel_scan_selected": "סרוק לשוניות נבחרות",
        "import_channel_items_count": "{n:,} פריטים",
        "import_channel_error_prefix": "שגיאה באיתור הלשוניות: {error}",
        "import_channel_degraded_warning": "לא הצלחנו לקרוא את רשימת הלשוניות האמיתית של הערוץ — מוצגות הלשוניות הרגילות כניחוש. ייתכן שחלקן ריקות או חסרות.",
        "import_channel_scan_complete": "הסריקה הושלמה — {n:,} פריטים",
        "import_channel_with_name": "ייבוא: {name}",
        "import_channel_tabs_found": "נמצאו {n} לשוניות — בחר מה לסרוק:",
        "import_channel_scanning_selected": "סורק לשוניות נבחרות…",
        "import_channel_scanning_tab": "סורק: {tab}…",
        "import_channel_expanding_playlists": "מרחיב פלייליסטים: {current}/{total}",
        "import_channel_scrape_error": "שגיאת סריקה: {msg}",

        # ── Search result card ──────────────────────────────────────────────────
        "search_card_add_btn": "＋  הוסף",
        "search_card_browse_btn": "עיון  ←",

        # ── Tag Editor: dialogs / headers ───────────────────────────────────────
        "meta_auto_settings_title": "הגדרות סדר אוטומטי",
        "meta_clean_settings_title": "הגדרות ניקוי (אגרסיביות)",
        "meta_auto_header": "בחר אילו פעולות יבצע כפתור 'סדר אוטומטי':",
        "meta_auto_album_note": "לתשומת לבך: מלבד מה שתבחרו למטה, 'סדר אוטומטי' תמיד גם קובע לכל קובץ את שדה האלבום לפי שם התיקייה שלו.",
        "meta_clean_title_group": "ניקוי כותרת (Title)",
        "meta_clean_filename_group": "ניקוי שם קובץ פיזי (Filename)",

        # ── Tag Editor: auto-order operations ───────────────────────────────────
        "meta_op_title_strip_label": "העתק שם קובץ לכותרת (ללא מספר)",
        "meta_op_title_strip_desc": "לוקח את שם הקובץ הקיים ומעתיק אותו לתוך שדה 'כותרת', תוך הסרת מספרים בתחילת השם (למשל '01 שיר' יהפוך ל-'שיר').",
        "meta_op_title_full_label": "העתק שם קובץ לכותרת (כולל מספר)",
        "meta_op_title_full_desc": "לוקח את שם הקובץ הקיים ומעתיק אותו לתוך שדה 'כותרת' בדיוק כפי שהוא.",
        "meta_op_normalize_spaces_label": "מחק רווחים כפולים וקווים תחתונים מהכותרת",
        "meta_op_normalize_spaces_desc": "סורק את הכותרת, מחליף קווים תחתונים (_) ברווחים, ומוחק רווחים כפולים או מיותרים מהכותרת.",
        "meta_op_track_num_label": "חלץ מספר רצועה משם הקובץ",
        "meta_op_track_num_desc": "מחפש מספר בתחילת שם הקובץ (למשל '03') ושומר אותו בתור מספר הרצועה.",
        "meta_op_split_at_label": "פצל שם קובץ ל'אמן' ו'כותרת'",
        "meta_op_split_at_desc": "מזהה מקף (-) בשם הקובץ. מה שלפני המקף הופך ל'אמן', ומה שאחריו ל'כותרת'.",
        "meta_op_album_artist_label": "העתק 'אמן' ל'אמן אלבום'",
        "meta_op_album_artist_desc": "מעתיק את שם ה'אמן' של כל שיר ושם אותו גם בשדה 'אמן אלבום' (חשוב לסידור נכון של אלבומים בנגנים).",
        "meta_op_strip_junk_label": "נקה מילים מיותרות מהכותרת",
        "meta_op_strip_junk_desc": "מנקה מהכותרת תוספות שכיחות מ-YouTube כמו '(Official Video)', '[HD]', או 'Lyrics'.",
        "meta_op_clear_comments_label": "מחק תוכן מתגית 'הערות'",
        "meta_op_clear_comments_desc": "מוחק לחלוטין את כל מה שכתוב בשדה ההערות של השיר.",
        "meta_op_clear_track_num_label": "מחק תוכן מתגית 'מספר רצועה'",
        "meta_op_clear_track_num_desc": "מוחק לחלוטין את מספר הרצועה של השיר.",
        "meta_op_clear_year_label": "מחק תוכן מתגית 'שנה'",
        "meta_op_clear_year_desc": "מוחק את שנת ההוצאה מהתגיות.",
        "meta_op_clear_genre_label": "מחק תוכן מתגית 'ז'אנר'",
        "meta_op_clear_genre_desc": "מוחק את סגנון המוזיקה (ז'אנר) מהתגיות.",
        "meta_op_clear_title_label": "מחק תוכן מתגית 'כותרת'",
        "meta_op_clear_title_desc": "מוחק לחלוטין את הכותרת של השיר.",
        "meta_op_clear_artist_label": "מחק תוכן מתגית 'אמן'",
        "meta_op_clear_artist_desc": "מוחק לחלוטין את שם האמן של השיר.",
        "meta_op_clear_album_label": "מחק תוכן מתגית 'אלבום'",
        "meta_op_clear_album_desc": "מוחק לחלוטין את שם האלבום של השיר.",
        "meta_op_clear_album_artist_label": "מחק תוכן מתגית 'אמן אלבום'",
        "meta_op_clear_album_artist_desc": "מוחק לחלוטין את שם אמן האלבום של השיר.",
        "meta_op_clean_filename_label": "נקה שם קובץ פיזי",
        "meta_op_clean_filename_desc": "מנקה את שם הקובץ עצמו: מסיר קווים תחתונים, מוחק כל מה שבתוך סוגריים () או [], ומסדר רווחים כפולים.",
        "meta_op_strip_filename_numbering_label": "הסר מספור משם הקובץ הפיזי",
        "meta_op_strip_filename_numbering_desc": "מוחק משם הקובץ הפיזי מספור בתחילתו (כמו '01-', '01 -', או '01_').",

        # ── Tag Editor: inspector rail group titles ─────────────────────────────
        "meta_group_from_filename": "לפי שם הקובץ",
        "meta_group_cleanup": "ניקוי ומחיקת תגיות",
        "meta_section_text_cleanup": "ניקוי טקסט",
        "meta_section_clear_fields": "מחיקת שדות",

        # ── Tag Editor: buttons / labels ────────────────────────────────────────
        "meta_cancel": "ביטול",
        "meta_ok": "אישור",
        "meta_save_ok": "אישור שמירה",
        "meta_browse_folder": "  בחר תיקייה",
        "meta_change_folder": "  החלף תיקייה",
        "meta_no_folder_selected": "לא נבחרה תיקייה",
        "meta_include_subdirs": "כלול תתי-תיקיות",
        "meta_auto_btn": "  סדר אוטומטי",
        "meta_apply_changes": "  החל שינויים",
        "meta_revert_changes": "  בטל שינויים",
        "meta_undo_changes": "בטל",
        "meta_redo_changes": "בצע שוב",
        "meta_review_changes": "סקירת שינויים",
        "meta_pending_changes": "שינויים ממתינים",
        "meta_stored_value": "ערך שמור",
        "meta_proposed_value": "ערך מוצע",
        "meta_change_source": "מקור השינוי",
        "meta_change_file": "קובץ",
        "meta_change_field": "שדה",
        "meta_change_included": "נכלל בהחלה",
        "meta_change_excluded": "מוחרג מההחלה",
        "meta_change_summary": "{files} קבצים, {fields} שינויים; {included} נכללים, {excluded} מוחרגים",
        "meta_change_origin_manual": "נערך ידנית",
        "meta_change_origin_auto_arrange": "סודר אוטומטית",
        "meta_change_origin_cleanup": "פעולת ניקוי",
        "meta_change_origin_filename": "עריכת שם קובץ",
        "meta_change_origin_lyrics": "עריכת מילים",
        "meta_change_origin_replaygain": "ReplayGain חושב",
        "meta_change_origin_artwork_add": "נוספה עטיפה",
        "meta_change_origin_artwork_replace": "הוחלפה עטיפה",
        "meta_change_origin_artwork_remove": "הוסרה עטיפה",
        "meta_change_origin_restore": "שוחזר מגיבוי",
        "meta_change_origin_online_metadata": "מטא־דאטה מקוון",
        "meta_online_title": "מטא־דאטה מקוון",
        "meta_online_open": "פתיחת מטא־דאטה מקוון",
        "meta_online_explicit_search_hint": "דבר אינו נשלח אוטומטית. יש לבחור קבצים, לבדוק את מונחי החיפוש ואז ללחוץ על חיפוש.",
        "meta_online_scope": "טווח החיפוש הנוכחי: {n} קבצים",
        "meta_online_scope_label": "טווח חיפוש",
        "meta_online_select_files": "יש לבחור קובץ אחד או יותר לפני פתיחת מטא־דאטה מקוון.",
        "meta_online_single_track": "רצועה יחידה",
        "meta_online_selected_album": "קבצים נבחרים / אלבום",
        "meta_online_search_title": "כותרת",
        "meta_online_search_artist": "אמן",
        "meta_online_search_album": "אלבום",
        "meta_online_search_musicbrainz": "חיפוש ב־MusicBrainz",
        "meta_online_retry": "ניסיון חוזר",
        "meta_online_cancel_lookup": "ביטול החיפוש",
        "meta_online_searching": "מחפש ב־MusicBrainz…",
        "meta_online_candidates": "מועמדים",
        "meta_online_candidates_count": "נמצאו {n} מועמדים. יש לבחור מועמד להשוואה.",
        "meta_online_candidate_label": "{title} — {artist} · ביטחון {score}%",
        "meta_online_comparison": "השוואה בין מטא־דאטה מקומי למקוון",
        "meta_online_use_online": "שימוש במקוון",
        "meta_online_keep_local": "השארת הערך המקומי",
        "meta_online_field": "שדה",
        "meta_online_local_value": "ערך מקומי",
        "meta_online_online_value": "ערך מקוון",
        "meta_online_status": "מצב",
        "meta_online_select_recommended": "בחירת שדות מומלצים",
        "meta_online_clear_selection": "ניקוי בחירת השדות",
        "meta_online_artwork_preview": "תצוגה מקדימה של עטיפה",
        "meta_online_use_artwork": "שימוש בעטיפה זו",
        "meta_online_artwork_loading": "טוען תצוגה מקדימה של עטיפה…",
        "meta_online_release_detail_loading": "טוען את רצועות המהדורה שנבחרה…",
        "meta_online_artwork_final_loading": "מוריד ומאמת את קובץ העטיפה המלא…",
        "meta_online_artwork_none": "אין עטיפה זמינה למהדורה זו.",
        "meta_online_artwork_invalid_mime": "ספק העטיפה החזיר סוג תמונה שאינו נתמך.",
        "meta_online_artwork_too_large": "קובץ העטיפה המלא גדול מכדי להשתמש בו בבטחה.",
        "meta_online_artwork_invalid": "קובץ העטיפה פגום או אינו תמונה תקינה.",
        "meta_online_artwork_unsupported": "לא ניתן לכתוב עטיפה לסוגי הקבצים שנבחרו.",
        "meta_online_artwork_not_selected": "לא נבחרה עטיפה מקוונת",
        "meta_online_artwork_ready": "תצוגת העטיפה מוכנה. יש לבחור בה במפורש כדי להשתמש בה.",
        "meta_online_artwork_unavailable": "אין תצוגת עטיפה תקינה זמינה.",
        "meta_online_add_pending": "הוספה לשינויים הממתינים",
        "meta_online_attribution": "ייחוס לספק",
        "meta_online_attribution_value": "מקור: {provider} · {url}",
        "meta_online_confidence_evidence": "רמת ביטחון: {score}%. ראיות: {evidence}",
        "meta_online_evidence_component": "ראיה: {component} {score}%",
        "meta_online_evidence_unavailable": "אין די ראיות להשוואה",
        "meta_online_no_results": "לא נמצאו תוצאות",
        "meta_online_offline": "אין חיבור לרשת. יש לבדוק את החיבור ולנסות שוב.",
        "meta_online_rate_limited": "MusicBrainz מגביל כרגע את קצב הבקשות. יש להמתין ולנסות שוב.",
        "meta_online_timeout": "הספק לא השיב בזמן.",
        "meta_online_provider_unavailable": "הספק אינו זמין כרגע.",
        "meta_online_provider_error": "ספק המטא־דאטה לא הצליח להשלים את הבקשה.",
        "meta_online_cancelled": "החיפוש בוטל",
        "meta_online_partial_results": "תוצאות חלקיות — יש לבדוק אותן לפני המשך.",
        "meta_online_stale_result": "התוצאה התיישנה כי הבחירה או השינויים הממתינים השתנו. יש לחפש שוב.",
        "meta_online_album_mapping_state": "מיפוי האלבום דורש בדיקה: {unmatched} לא הותאמו, {ambiguous} עמומים.",
        "meta_online_provider_musicbrainz": "MusicBrainz",
        "meta_online_provider_caa": "ארכיון העטיפות Cover Art Archive",
        "meta_online_difference_change": "שונה",
        "meta_online_difference_no_op": "ללא שינוי",
        "meta_online_difference_empty": "לא סופק",
        "meta_online_difference_unsupported": "לא נתמך",
        "meta_online_difference_ambiguous": "עמום",
        "meta_online_field_title": "כותרת",
        "meta_online_field_artist": "אמן",
        "meta_online_field_album": "אלבום",
        "meta_online_field_album_artist": "אמן האלבום",
        "meta_online_field_track_num": "מספר רצועה",
        "meta_online_field_track_total": "מספר רצועות כולל",
        "meta_online_field_disc_num": "מספר דיסק",
        "meta_online_field_disc_total": "מספר דיסקים כולל",
        "meta_online_field_year": "תאריך",
        "meta_online_field_genre": "ז׳אנר",
        "meta_online_field_isrc": "ISRC",
        "meta_online_field_publisher": "לייבל / מוציא לאור",
        "meta_review_all_files": "כל הקבצים",
        "meta_review_all_types": "כל סוגי השינויים",
        "meta_review_all_categories": "כל הקטגוריות",
        "meta_review_all_origins": "כל המקורות",
        "meta_review_all_states": "כל המצבים",
        "meta_review_category_metadata": "מטא-נתונים",
        "meta_review_category_filename": "שם קובץ",
        "meta_review_category_artwork": "עטיפה",
        "meta_review_category_lyrics": "מילים",
        "meta_review_category_replaygain": "ReplayGain",
        "meta_review_warning": "אזהרה",
        "meta_review_warnings": "אזהרות",
        "meta_review_blocked": "חסום",
        "meta_review_counts": "{total} שינויים; {included} קבצים נכללים, {excluded} מוחרגים, {blocked} חסומים; {pending} קבצים יישארו ממתינים לאחר ההחלה",
        "meta_review_revert_entries": "בטל ערכים שנבחרו",
        "meta_review_revert_files": "בטל קבצים שנבחרו",
        "meta_review_revert_filename": "בטל שם קובץ",
        "meta_review_revert_artwork": "בטל עטיפה",
        "meta_review_revert_lyrics": "בטל מילים",
        "meta_review_revert_replaygain": "בטל ReplayGain",
        "meta_review_revert_all": "בטל הכל",
        "meta_review_blocker_details": "פרטי חסימה",
        "meta_review_missing_target": "פריט סביבת העבודה שנבדק אינו זמין עוד.",
        "meta_review_stale_target": "הקובץ השתנה לאחר שנבדק.",
        "meta_restore_btn": "  שחזור מגיבוי",
        "meta_draft_available_title": "לשחזר הצעות שלא נשמרו בעורך התגיות?",
        "meta_draft_available_message": "טיוטת הצעות שמורה משפיעה על {n} קבצים ב-{root}. נוצרה: {age}. השחזור יסרוק את התיקייה וישחזר הצעות בלבד; קבצי מדיה לא ייכתבו.",
        "meta_draft_restore": "שחזר טיוטה",
        "meta_draft_discard": "השלך טיוטה",
        "meta_draft_keep": "שמור לפעם אחרת",
        "meta_draft_unavailable": "תיקיית השורש של הטיוטה אינה זמינה.",
        "meta_draft_legacy_conflict": "נמצאה טיוטה שנייה, שונה, מגרסה קודמת של האפליקציה. לא בוצע מיזוג ולא נמחק דבר. הטיוטה שלמעלה היא הנוכחית; העותק הישן נשמר כאן למקרה שתזדקק לו:\n{path}",
        "meta_draft_migration_failed": "לא ניתן היה להעביר טיוטה מגרסה קודמת של האפליקציה למיקומה החדש. היא נותרה ללא שינוי ולא אבד דבר.",
        "meta_draft_restored": "ההצעות השמורות שוחזרו. יש לסקור אותן לפני החלה.",
        "meta_draft_incompatible": "הטיוטה השמורה אינה תואמת לסביבת עבודה זו ונשמרה לבדיקה.",
        "meta_draft_unsaved_title": "לשינויים הממתינים נדרשת החלטה",
        "meta_draft_unsaved_message": "{operation} אינו יכול להחליף את סביבת העבודה כאשר יש הצעות ממתינות. החל, בטל, או שמור את הטיוטה לשחזור לפני ההמשך.",
        "meta_draft_apply": "סקור והחל",
        "meta_draft_keep_action": "שמור טיוטה ניתנת לשחזור",
        "meta_draft_review_required": "יש לסקור את הטיוטה המשוחזרת בחלון סקירת שינויים לפני ההחלה.",
        "md_recovery_unresolved_cannot_discard": "רשומת השחזור עדיין אינה פתורה ולא ניתן להשליך אותה.",
        "md_recovery_reconciled": "תוצאת הדיסק שכבר הושלמה הותאמה; לא בוצעה כתיבה חוזרת למדיה.",
        "md_recovery_reconcile_btn": "התאם תוצאה שהושלמה",
        "meta_backup_manager": "מנהל גיבויים",
        "meta_backup_manager_note": "מוצגים רק גיבויים מתוך תיקיית הגיבויים של BananaFlow. גיבויים שמקושרים ליומן מוגנים.",
        "meta_backup_created": "נוצר",
        "meta_backup_operation": "פעולה",
        "meta_backup_files": "קבצים",
        "meta_backup_schema": "סכימה",
        "meta_backup_app_version": "גרסת יישום",
        "meta_backup_root": "תיקיית מקור",
        "meta_backup_status": "מזהה פעולה",
        "meta_backup_size": "גודל",
        "meta_backup_validity": "תקינות",
        "meta_backup_location": "מיקום",
        "meta_backup_valid": "תקין",
        "meta_backup_invalid": "לא תקין או פגום",
        "meta_backup_journal_referenced": "מוגן על ידי יומן לא פתור",
        "meta_backup_preview_restore": "תצוגה מקדימה לשחזור",
        "meta_backup_restore": "שחזור",
        "meta_backup_undo_batch": "בטל החלת אצווה",
        "meta_backup_details": "פרטים",
        "meta_backup_export": "ייצוא/העתקה",
        "meta_backup_delete": "מחיקה",
        "meta_backup_refresh": "רענון",
        "meta_backup_preview_message": "המטא-נתונים של {n} קבצים ישוחזרו; בתצוגה מקדימה זו לא נכתבים קבצים.",
        "meta_backup_more_files": "… ועוד {n} קבצים",
        "meta_backup_restore_confirm": "לשחזר מטא-נתונים עבור {n} קבצים מהגיבוי המאומת הזה?",
        "meta_backup_undo_confirm": "שחזר את המטא-נתונים המאומתים שלפני הפעולה. היפוך נתיב אינו נכלל ודורש אישור מפורש נפרד.",
        "meta_backup_delete_protected": "גיבוי זה מוגן על ידי פעולה פעילה או לא פתורה ולא ניתן למחוק אותו.",
        "meta_backup_delete_confirm": "למחוק את הגיבוי לצמיתות? אי אפשר לבטל פעולה זו.",
        "meta_restore_tooltip": (
            "כתיבה חוזרת של התגיות שנשמרו בגיבוי קודם — כל 'החל שינויים' "
            "יוצר גיבוי כזה אוטומטית"
        ),
        "meta_find_duplicates": "  חפש כפילויות",
        "meta_duplicates_tools_title": "זיהוי וניקוי כפילויות",
        "meta_problems_cancelled": "האימות בוטל.",
        "meta_problems_title": "בעיות", "meta_problems_empty": "לא נמצאו בעיות.",
        "meta_problems_validating": "מתבצע אימות בעיות…",
        "meta_problems_error": "לא ניתן לאמת את הבעיות.", "meta_problems_stale": "רשימת הבעיות אינה עדכנית; יש לאמת מחדש לפני תיקון.",
        "meta_problems_all": "כל דרגות החומרה", "meta_problems_search": "חיפוש בעיות",
        "meta_problems_severity": "חומרה", "meta_problems_problem": "בעיה",
        "meta_problems_file": "קובץ", "meta_problems_fixable": "ניתן לתיקון",
        "meta_problems_yes": "כן", "meta_problems_no": "לא", "meta_problems_count": "{n} בעיות",
        "meta_problems_revalidate": "אמת מחדש", "meta_problems_fix_selected": "תקן נבחרים",
        "meta_problems_no_safe_fix": "לבעיות שנבחרו אין תיקון משותף ובטוח.",
        "meta_problems_value": "הזן ערך להוספה לשינויים ממתינים:",
        "meta_problems_preview_title": "תצוגה מקדימה לתיקונים",
        "meta_problems_preview_body": "להוסיף את הערך שהוזן ל-{n} בעיות נבחרות? אף קובץ לא ייכתב.",
        "meta_problems_preview_summary": "תצוגה מקדימה עבור {n} קבצים: {changed} שינויים, ערך חדש: {value}. {results}",
        "meta_problems_old_value": "ערך נוכחי", "meta_problems_new_value": "ערך מוצע", "meta_problems_return_parameters": "חזרה לפרמטרים",
        "meta_problems_result": "תוצאה", "meta_problems_details": "פרטים",
        "meta_problems_add_pending": "הוסף לשינויים ממתינים",
        "meta_problems_severity_information": "מידע", "meta_problems_severity_warning": "אזהרה",
        "meta_problems_severity_error": "שגיאה", "meta_problems_severity_blocker": "חסם",
        "meta_problems_all_categories": "כל הקטגוריות", "meta_problems_all_states": "כל המצבים", "meta_problems_category": "קטגוריה", "meta_problems_state": "מצב", "meta_problems_field": "שדה",
        "meta_problems_select_all": "בחר את כל המסוננים", "meta_problems_clear_selection": "נקה בחירה",
        "meta_problems_category_basic_metadata": "מטא-נתונים בסיסיים", "meta_problems_category_numbering": "מספור", "meta_problems_category_format_capability": "פורמט/יכולת", "meta_problems_category_pending_changes": "שינויים ממתינים", "meta_problems_category_artwork": "עטיפה", "meta_problems_category_filename_path": "שם קובץ/נתיב",
        "meta_problems_state_present": "קיים בדיסק", "meta_problems_state_present_on_disk": "קיים בדיסק", "meta_problems_state_resolved_by_pending": "נפתר על ידי שינויים ממתינים", "meta_problems_state_introduced_by_pending": "נוצר על ידי שינויים ממתינים", "meta_problems_state_pending_blocker": "שינוי ממתין חסום", "meta_problems_state_changed_excluded": "שונה אך לא נכלל",
        "meta_problem_title": "כותרת חסרה", "meta_problem_title_body": "חסרה כותרת.",
        "meta_problem_artist": "אמן חסר", "meta_problem_artist_body": "חסר אמן.",
        "meta_problem_track": "מספור רצועה לא תקין", "meta_problem_track_body": "מספר הרצועה והסך הכולל אינם עקביים.",
        "meta_problem_disc": "מספור דיסק לא תקין", "meta_problem_disc_body": "מספר הדיסק והסך הכולל אינם עקביים.",
        "meta_problem_excluded": "שונה אך לא נכלל", "meta_problem_excluded_body": "קובץ זה שונה אך אינו נכלל בהחלה.",
        "meta_problem_capability": "שינוי ממתין חסום", "meta_problem_capability_body": "שינוי ממתין אינו נתמך או חסום.",
        "meta_problem_artwork": "לא ניתן לקרוא עטיפה", "meta_problem_artwork_body": "העטיפה המוטמעת אינה תקינה או אינה קריאה.",
        "meta_problem_missing_title": "נדרשת כותרת לקובץ הניתן לעריכה.",
        "meta_problem_missing_artist": "נדרש אמן לקובץ הניתן לעריכה.",
        "meta_problem_numbering_invalid": "המספר חייב להיות חיובי ולא לעלות על הסך הכולל הידוע.",
        "meta_problem_changed_excluded": "לקובץ יש שינויים ממתינים אך הוא אינו נכלל בהחלה.",
        "meta_problem_proposal_blocked": "שדה ממתין אינו נתמך או חסום על ידי ראיית בטיחות קיימת.",
        "meta_problem_artwork_invalid": "העטיפה אינה תקינה או שלא ניתן לקרוא אותה.",
        "meta_no_folder_scanned": "לא נסרקה תיקייה",
        "meta_files_folders_header": "קבצים ותיקיות",
        "meta_auto_cfg_tooltip": "הגדר מה סדר אוטומטי יבצע",
        "meta_dupes_tooltip": "סרוק את התיקייה לאיתור קבצי מוזיקה כפולים",
        "meta_clean_cfg_tooltip": "הגדרות ניקוי",
        "meta_empty_title": "אין קבצים להצגה",
        "meta_empty_body": "בחר תיקייה ו-BananaFlow יטען כאן את קבצי המוזיקה.",
        "meta_loading_scanning_title": "טוען קבצים",
        "meta_loading_scanning_body": "סורק את התיקייה שנבחרה ברקע...",
        "meta_loading_apply_title": "מחיל שינויים",
        "meta_loading_apply_body": "כותב את התגיות המעודכנות בבטחה...",
        "meta_loading_restore_title": "משחזר מגיבוי",
        "meta_loading_restore_body": "כותב בחזרה את התגיות שנשמרו בגיבוי...",

        # ── Tag Editor: inspector ──────────────────────────────────────────────
        "meta_select_files_prompt": "בחר קבצים\nאו תיקייה\nלעריכה",
        "meta_all_checked_files": "כל הקבצים המסומנים",
        "meta_apply_artist_group": "החל אמן",
        "meta_artist_placeholder": "שם האמן…",
        "meta_apply_artist_btn": "  החל אמן על המסומנים",
        "meta_apply_album_group": "החל אלבום",
        "meta_album_placeholder": "שם האלבום…",
        "meta_apply_album_btn": "  החל אלבום על המסומנים",
        "meta_tracks_selected_count": "{n} שירים נבחרו",
        "meta_edit_tags_group": "עריכת תגיות",
        "meta_inspector_no_selection_title": "בחר שורות לעריכה",
        "meta_inspector_no_selection_body": "המפקח עורך רק שורות שנבחרו. סינון, ניווט בתיקיות והשורות המוצגות אינם משנים את היקף ההחלה הממתין.",
        "meta_inspector_metadata_section": "מטא־דאטה",
        "meta_inspector_lyrics_section": "מילות שיר מוטמעות",
        "meta_inspector_artwork_section": "עטיפה",
        "meta_artwork_add": "הוספה",
        "meta_artwork_replace": "החלפה",
        "meta_artwork_remove": "הסרה",
        "meta_artwork_remove_all": "הסרת העטיפה הראשית מכולם",
        "meta_artwork_paste": "הדבקה",
        "meta_artwork_export": "ייצוא עטיפה שמורה",
        "meta_artwork_current": "עטיפה שמורה כעת",
        "meta_artwork_proposed": "עטיפה מוצעת",
        "meta_artwork_pending_removal": "הסרה ממתינה",
        "meta_artwork_revert": "ביטול שינוי עטיפה ממתין",
        "meta_artwork_none": "אין עטיפה מוטמעת.",
        "meta_artwork_present": "{n} תמונות מוטמעות.",
        "meta_artwork_mixed": "עטיפות שונות — לא מוצגת עטיפה של קובץ יחיד לכל הבחירה.",
        "meta_artwork_pending": "שינוי עטיפה ממתין. לחצו על החל כדי לכתוב אותו.",
        "meta_artwork_loading": "טוען תצוגה מקדימה של עטיפה…",
        "meta_artwork_read_only": "אפשר לצפות בעטיפה, אך לא בטוח לערוך אותה בפורמט זה.",
        "meta_artwork_invalid_image": "בחרו תמונת JPEG או PNG תקינה.",
        "meta_artwork_unsupported_image": "נתמכות רק תמונות JPEG ו־PNG.",
        "meta_artwork_file_too_large": "קובץ התמונה גדול מדי.",
        "meta_artwork_dimensions_too_large": "ממדי התמונה גדולים מדי.",
        "meta_artwork_animated": "אי אפשר להטמיע תמונות מונפשות.",
        "meta_artwork_export_title": "ייצוא עטיפה",
        "meta_artwork_export_collision": "קובץ עטיפה לייצוא כבר קיים.",
        "meta_artwork_export_invalid_destination": "בחרו תיקיית ייצוא תקינה.",
        "meta_artwork_choose_title": "בחירת תמונת עטיפה",
        "meta_inspector_replaygain_section": "ReplayGain",
        "meta_inspector_file_properties_section": "מאפייני קובץ (לקריאה בלבד)",
        "meta_inspector_clear_short": "נקה",
        "meta_inspector_clear_field": "הצע לנקות שדה זה",
        "meta_inspector_empty_value": "לא הוגדר",
        "meta_inspector_mixed_value": "ערכים שונים",
        "meta_inspector_pending_marker": "(ממתין)",
        "meta_inspector_capability_all": "ניתן לערוך מטא־דאטה בכל {n} הקבצים שנבחרו.",
        "meta_inspector_capability_some": "ניתן לערוך מטא־דאטה ב-{supported} מתוך {total} הקבצים שנבחרו.",
        "meta_inspector_capability_none": "הקבצים שנבחרו הם לקריאה בלבד או שפורמט המטא־דאטה שלהם אינו נתמך.",
        "meta_inspector_pending_files": "ל-{n} קבצים שנבחרו יש שינויים ממתינים.",
        "meta_inspector_field_partial_tooltip": "ניתן לשנות שדה זה ב-{supported} מתוך {total} קבצים; קבצים שאינם נתמכים ידווחו ויישארו ללא שינוי.",
        "meta_inspector_field_unsupported_tooltip": "לא ניתן לייצג שדה זה בבטחה בפורמט שנבחר.",
        "meta_inspector_field_pending_tooltip": "לשדה זה יש הצעה ממתינה.",
        "meta_inspector_partial_scope": "נוצרה הצעה עבור {affected} יעדי שדה/קובץ נתמכים; {unsupported} יעדים שאינם נתמכים נשארו ללא שינוי.",
        "meta_inspector_invalid_value_title": "ערך מטא־דאטה לא תקין",
        "meta_inspector_invalid_value_body": "יש לבדוק את הערך המספרי עבור: {fields}.",
        "meta_apply_confirm_title": "החלת שינויים ממתינים",
        "meta_apply_confirm_body": "לכתוב ולאמת שינויים ממתינים עבור {n} קבצים בדיוק? תחילה ייווצר גיבוי תגיות.",
        "meta_apply_confirm_button": "החל שינויים",
        "meta_lyrics_language": "שפה (לדוגמה eng או heb)",
        "meta_lyrics_description": "תיאור",
        "meta_lyrics_propose_replace": "הצע החלפה",
        "meta_lyrics_propose_clear": "הצע ניקוי",
        "meta_lyrics_revert_pending": "בטל הצעת מילות שיר",
        "meta_lyrics_none": "אין מילות שיר לא־מסונכרנות מוטמעות.",
        "meta_lyrics_present": "קיימות מילות שיר מוטמעות.",
        "meta_lyrics_mixed": "בקבצים שנבחרו יש מילות שיר שונות. עורך ריק אינו אומר ניקוי.",
        "meta_lyrics_pending": "שינוי במילות השיר ממתין להחלה.",
        "meta_lyrics_secondary_preserved": "יישמרו {n} גרסאות נוספות של מילות השיר.",
        "meta_lyrics_synchronized_read_only": "קיימות מילות שיר מתוזמנות והן יישארו לקריאה בלבד.",
        "meta_lyrics_language_not_supported": "פורמט זה שומר את טקסט מילות השיר, אך לא שפה או תיאור.",
        "meta_replaygain_plain_explanation": "ReplayGain שומר מידע להתאמת עוצמת ההשמעה בנגנים תואמים. הוא אינו מנרמל, ממיר או משנה את דגימות השמע.",
        "meta_replaygain_track_gain": "הגבר רצועה",
        "meta_replaygain_track_peak": "שיא רצועה",
        "meta_replaygain_album_gain": "הגבר אלבום",
        "meta_replaygain_album_peak": "שיא אלבום",
        "meta_replaygain_reference_loudness": "עוצמת ייחוס",
        "meta_replaygain_analyze_track": "נתח רצועות",
        "meta_replaygain_analyze_album": "נתח קבוצות אלבום",
        "meta_replaygain_cancel": "בטל ניתוח",
        "meta_replaygain_clear_track": "נקה ערכי רצועה",
        "meta_replaygain_clear_album": "נקה ערכי אלבום",
        "meta_replaygain_revert": "בטל הצעות ReplayGain",
        "meta_replaygain_album_confirm_title": "ניתוח ReplayGain לאלבום",
        "meta_replaygain_album_confirm_body": "לנתח {files} קבצים שנבחרו בתור {groups} קבוצות אלבום שנקבעו באופן עקבי? ל־{ambiguous} קבצים אין זהות אלבום מספקת, ולכן הם יקבלו ערכי רצועה בלבד ולעולם לא ערכי אלבום שקטים. רשימת הקבצים המדויקת מופיעה בפרטים. הניתוח יוצר הצעות בלבד.",
        "meta_replaygain_album_group_safe": "קבוצת אלבום:",
        "meta_replaygain_album_group_ambiguous": "עמום (ערכי רצועה בלבד):",
        "meta_property_filename": "שם קובץ",
        "meta_property_path": "נתיב",
        "meta_property_format": "פורמט",
        "meta_property_duration": "משך",
        "meta_property_bitrate": "קצב סיביות",
        "meta_property_sample_rate": "קצב דגימה",
        "meta_property_channels": "ערוצים",
        "meta_property_size": "גודל קובץ",
        "meta_property_modified": "שונה לאחרונה",
        "meta_property_unavailable": "המאפיינים אינם זמינים.",
        "meta_property_single_selection_only": "בחר קובץ אחד כדי לראות את המאפיינים הטכניים שלו.",
        "meta_mixed_placeholder": "ריק / מעורב",
        "meta_field_title": "כותרת:",
        "meta_field_artist": "אמן:",
        "meta_field_album": "אלבום:",
        "meta_field_album_artist": "אמן אלבום:",
        "meta_field_track": "רצועה:",
        "meta_apply_to_selection": "  החל על הבחירה",
        "meta_rename_group": "שינוי שם קובץ",
        "meta_rename_note": "שנה את שם הקובץ הפיזי לפי הכותרת החדשה",
        "meta_rename_btn": "  שנה שם קובץ לפי כותרת",

        # ── Tag Editor: clean-up checkboxes ────────────────────────────────────
        "meta_clean_brackets": "נקה סוגריים עם זבל (כמו [HD] וכו')",
        "meta_clean_english_junk": "נקה מילות זבל באנגלית (Official, Audio, 4K, Prod...)",
        "meta_clean_hebrew_junk": "נקה מילות זבל בעברית (קאבר, רמיקס, הופעה חיה...)",
        "meta_clean_punctuation": "תקן רווחים, מקפים מיותרים וקווים מפרידים (|)",
        "meta_clean_filename_brackets": "מחיקת סוגריים חכמה (למחוק זבל, להשאיר feat. וכו')",
        "meta_clean_filename_brackets_tooltip": "אם כבוי, ימחק בצורה 'עיוורת' את כל הסוגריים כולל התוכן שלהם.",
        "meta_clean_filename_domains": "נקה שאריות אתרי הורדות (y2mate, yt1s, SPOTIFY-DL...)",
        "meta_clean_filename_emojis": "נקה אימוג'י וסימנים מיוחדים בעייתיים (!@#$)",
        "meta_clean_filename_spaces": "תקן מקפים ורווחים כפולים ( - - )",

        # ── Tag Editor: status / progress / errors ─────────────────────────────
        "meta_choose_music_folder": "בחר תיקיית מוזיקה",
        "meta_delete_to_trash_title": "העברה לסל המחזור",
        "meta_delete_to_trash_body": "{n} קבצים יועברו לסל המחזור. להמשיך?",
        "meta_delete_to_trash_confirm": "העבר לסל המחזור",
        "meta_scanning": "סורק…",
        "meta_scanning_progress": "סורק תגיות… {done}/{total}",
        "meta_searching_duplicates": "מחפש כפילויות…",
        "meta_searching_duplicates_progress": "מחפש כפילויות… {done}/{total}  ({eta})",
        "meta_writing_tags_progress": "כותב תגיות… {done}/{total}",
        "meta_done_success_base": "הושלם: {success} הצליחו",
        "meta_done_failed_suffix": ", {fail} נכשלו",
        "meta_done_partial_suffix": ", {partial} חלקי (שינוי שם ממתין)",
        "meta_done_skipped_suffix": ", {skip} דולגו",
        "meta_done_summary_title": "הצלחה",
        "meta_done_with_errors_title": "הושלם עם שגיאות",
        # שלב 1 — בטיחות החלה (TE-SAFE-*)
        "meta_apply_blocked_title": "ההחלה נחסמה",
        "meta_backup_target_failed": "ההחלה נחסמה: תיקיית הגיבוי אינה שמישה, ולכן שום קובץ לא שונה.",
        "meta_backup_write_failed": "ההחלה נחסמה: לא ניתן היה לכתוב את גיבוי התגיות, ולכן שום קובץ לא שונה.",
        "meta_apply_cancelled": "בוטל לפני שקובץ זה נכתב.",
        "meta_apply_write_failed": "כתיבת התגיות נכשלה — הקובץ נותר ללא שינוי.",
        "meta_rename_blocked": "התגיות נכתבו, אך שינוי השם נחסם (נשאר ממתין).",
        "meta_rename_failed": "התגיות נכתבו, אך שינוי השם נכשל (נשאר ממתין).",
        "meta_rename_rollback_failed": "לא ניתן היה לבטל שינוי שם — נדרש שחזור.",
        "meta_journal_init_failed": "ההחלה נחסמה: לא ניתן היה ליצור את יומן הפעולה, ולכן שום קובץ לא שונה.",
        "meta_journal_transition_failed": "ההחלה נעצרה בבטחה: לא ניתן היה לעדכן את יומן הפעולה — נדרש שחזור.",
        "meta_no_duplicates_found": "לא נמצאו כפילויות ({elapsed:.1f}s)",
        "meta_duplicate_search_error": "שגיאה בחיפוש כפילויות: {msg}",
        "meta_files_deleted": "נמחקו {success} קבצים כפולים{note}",
        "meta_files_deleted_errors_suffix": " ({fail} שגיאות)",
        "meta_files_count": "{n} קבצים",
        "meta_folders_count": "{n} תיקיות",
        "meta_changes_proposed": "{n} שינויים מוצעים",
        "meta_warnings_count": "{n} אזהרות",
        "meta_total_files": "{total} קבצים",
        "meta_showing_filtered": "מציג {checked} מסומנים מתוך {total}",
        "meta_showing_visible": "מציג {visible} מתוך {total}",
        "meta_n_files_checked": "{n} קבצים מסומנים",
        "meta_n_files_visible": "{n} קבצים מוצגים",
        "meta_apply_scope_label": "{n} קבצים יוחלו",
        "meta_exclude_from_apply": "החרג מהחלה",
        "meta_include_in_apply": "כלול בהחלה",
        "meta_excluded_filter_chip": "שינויים שהוחרגו ({n})",
        "meta_tracks_selected_summary": "נבחרו {n} שירים",
        "meta_nav_back": "חזרה",
        "meta_nav_forward": "קדימה",
        "meta_nav_up": "למעלה",
        "meta_search_tracks": "חפש קבצים, כותרות ואמנים…",

        # ── Tag Editor: context menu / dialogs ─────────────────────────────────
        "meta_add_folder": "הוסף תיקייה",
        "meta_open_file": "פתח",
        "meta_reveal_in_explorer": "הצג בסייר הקבצים",
        "meta_copy_path": "העתק נתיב",
        "meta_move_menu": "העבר אל…",
        "meta_move_choose_folder": "בחר תיקיית יעד",
        "meta_properties": "מאפיינים",
        "meta_properties_item": "{name}\nנתיב: {path}\nגודל: {size} בתים\nשונה: {modified}",
        "meta_rename_menu": "שנה שם",
        "meta_delete_menu": "מחק",
        "meta_new_folder_dialog_title": "הוסף תיקייה",
        "meta_new_folder_prompt": "שם התיקייה החדשה:",
        "meta_new_folder_default": "תיקייה חדשה",
        "meta_invalid_folder_name": "שם התיקייה אינו חוקי.",
        "meta_folder_exists": "תיקייה בשם הזה כבר קיימת.",
        "meta_create_folder_failed": "כשל ביצירת התיקייה:\n{error}",
        "meta_rename_dialog_title": "שנה שם",
        "meta_rename_prompt": "הכנס שם חדש:",
        "meta_target_name_exists": "שם היעד כבר קיים בתיקייה זו.",
        "meta_rename_failed": "כשל בשינוי השם:\n{error}",
        "meta_delete_file_title": "מחיקת קובץ",
        "meta_delete_folder_title": "מחיקת תיקייה",
        "meta_delete_confirm": "להעביר לסל המחזור את:\n{name}?",
        "meta_delete_recursive_note": "\n(כל הקבצים בתוך התיקייה יועברו גם הם)",
        "meta_delete_failed": "כשל במחיקה:\n{error}",
        "meta_move_target_exists": "היעד כבר קיים:\n{name}",
        "meta_move_failed": "כשל בהעברת הקובץ:\n{error}",
        "meta_error_title": "שגיאה",
        "meta_unsupported_format_tooltip": "פורמט לא נתמך",

        # ── Downloader hints (authentication / browser / cookies / 403) ────────
        "downloader_auth_required_hint": (
            "💡 YouTube דורש אימות (חשבון Google) כדי להמשיך בהורדה.\n\n"
            "יש לך שתי אפשרויות:\n"
            "1. התחברות מהירה: לחץ על 'תיקון ההתחברות' כדי להתחבר לחשבון Google ישירות מהתוכנה (הכי פשוט).\n"
            "2. ייצוא עוגיות: השתמש בתוסף הדפדפן 'Get cookies.txt LOCALLY' כדי לייצא קובץ טקסט ולבחור אותו בהגדרות.\n"
            "קישור לתוסף: https://chromewebstore.google.com/detail/get-cookiestxt-locally/ccmgnabidkenghhcidlkgeimdbgefecl\n"
        ),
        "downloader_chrome_locked_hint": (
            "💡 טיפ: דפדפן Chrome נעול או מוצפן. סגור את הדפדפן לגמרי ונסה שוב.\n"
            "אם זה לא עוזר, השתמש בלחצן 'תיקון ההתחברות' כדי לקרוא את קובצי העוגיות המוצפנים של Chrome."
        ),
        "downloader_node_missing_hint": (
            "💡 טיפ: חסר רכיב להרצת JavaScript (נחוץ לפתרון ה'חידות' של YouTube).\n"
            "יש להריץ בטרמינל את הפקודות הבאות:\n"
        ),
        "downloader_po_token_hint": (
            "💡 טיפ: YouTube דורש רכיב אימות נוסף (PO Token) או התחברות לחשבון.\n"
            "ייתכן שתצטרך לעדכן את קובץ העוגיות שלך דרך 'תיקון ההתחברות' או להשתמש בלחצן 'תיקון ידני בדפדפן'."
        ),
        "downloader_403_hint": "💡 טיפ: שגיאת גישה (403). ייתכן שצריך לעדכן את קובץ העוגיות או להחליף כתובת IP.",

        # ── Cookie validator ────────────────────────────────────────────────────
        "cookies_file_not_found": "קובץ העוגיות לא נמצא: {path}",
        "cookies_read_error": "שגיאה בקריאת קובץ העוגיות: {exc}",
        "cookies_empty_or_invalid": "קובץ העוגיות ריק או לא תקין.",
        "cookies_missing_login_info": (
            "⚠️ בקובץ הזה אין חיבור פעיל ל-YouTube (חסר LOGIN_INFO).\n"
            "היכנס בעצמך ל-youtube.com, ודא שאתה רואה שם את תמונת החשבון שלך, "
            "ורק אז ייצא מחדש את העוגיות כשהלשונית הזו פתוחה."
        ),
        "cookies_all_expired": (
            "⚠️ תוקף כל העוגיות פג! ייתכן שתקבל שגיאת 403.\n"
            "מומלץ להתחבר מחדש דרך 'תיקון ההתחברות'."
        ),

        # ── Playwright check ────────────────────────────────────────────────────

        # ── Channel flow status ─────────────────────────────────────────────────
        "channel_discovering_tabs": "מאתר לשוניות…",
        "channel_import_cancelled": "ייבוא ערוץ בוטל.",
        "channel_items_found": "נמצאו {n:,} פריטים — בודק כפילויות…",
        "channel_duplicates_found": "נמצאו {n} כפילויות — ממתין להחלטת המשתמש…",
        "channel_adding_to_queue": "מוסיף {n:,} פריטים לתור…",

        # ── Duplicate detector worker ───────────────────────────────────────────
        "dup_calculating": "מחשב…",

        # ── Metadata table headers & row statuses ──────────────────────────────
        "mt_col_filename":     "שם קובץ",
        "mt_col_title":        "כותרת",
        "mt_col_title_new":    "כותרת (חדש)",
        "mt_col_artist":       "אמן",
        "mt_col_artist_new":   "אמן (חדש)",
        "mt_col_album":        "אלבום",
        "mt_col_album_new":    "אלבום (חדש)",
        "mt_col_track":        "רצועה",
        "mt_col_track_new":    "רצועה (חדש)",
        "mt_col_filename_new": "שם קובץ חדש",
        "mt_col_genre":        "ז'אנר",
        "mt_col_genre_new":    "ז'אנר (חדש)",
        "mt_col_comment":      "הערות",
        "mt_col_comment_new":  "הערות (חדש)",
        "mt_more_columns_title": "עוד עמודות",
        "mt_search_columns":     "חפש עמודות…",
        "mt_size_all_to_fit":    "התאם כל העמודות לתוכן",
        "mt_more_columns":       "עוד…",
        "mt_file_tooltip_path":      "נתיב: {path}",
        "mt_file_tooltip_type":      "סוג: {type}",
        "mt_file_tooltip_status":    "מצב: {status}",
        "mt_file_tooltip_new_name":  "שם חדש: {name}",
        "mt_file_type_audio":        "קובץ שמע {ext}",
        "mt_file_type_unknown":      "קובץ",
        "mt_status_error":           "שגיאה",
        "meta_a11y_file_tree":       "קבצים ותיקיות",
        "meta_a11y_file_tree_desc":  "עץ תיקיות שנסרקו וקבצי שמע מסומנים.",
        "meta_a11y_details_table":   "רשימת הקבצים של עורך התגיות",
        "meta_a11y_details_table_desc": "טבלה בסגנון סייר הקבצים של קבצי שמע ומטא-דאטה מוצעת.",
        "meta_a11y_table_header":    "עמודות רשימת הקבצים",
        "meta_a11y_zoom_out":        "הקטן תצוגת רשימת קבצים",
        "meta_a11y_zoom_value":      "אחוז זום של רשימת הקבצים",
        "meta_a11y_zoom_in":         "הגדל תצוגת רשימת קבצים",
        "meta_a11y_excluded_filter_desc": "הצג רק קבצים ששונו והוחרגו מהחלה",
        "meta_a11y_external_filter_desc": "הצג רק קבצים ששונו מחוץ ליישום",
        "meta_a11y_clear_named_field": "נקה {field}",
        "meta_a11y_about_action":    "אודות {action}",
        "meta_a11y_configure_action": "הגדרות {action}",
        "meta_a11y_scan_progress":   "התקדמות סריקת התיקייה",
        "meta_a11y_clear_search":    "נקה חיפוש",
        "meta_file_op_failed":       "לא ניתן היה להשלים את הפעולה על „{name}”.",
        "meta_file_op_missing":      "„{name}” כבר לא קיים.",
        "meta_file_op_destination_exists": "כבר קיים פריט בשם הזה, ולכן „{name}” לא שונה.",
        "meta_file_op_root_escape":  "„{name}” נמצא מחוץ לתיקייה הזו, ולכן לא שונה.",
        "meta_file_op_root_operation": "לא ניתן לשנות מכאן את התיקייה שנפתחה.",
        "meta_file_op_invalid_name": "לא ניתן להשתמש בשם הזה ב‑Windows.",
        "meta_file_op_invalid_root": "לא ניתן לפתוח את התיקייה הזו כסביבת עבודה.",
        "meta_file_op_cloud_placeholder": "„{name}” שמור בענן. יש להפוך אותו לזמין במכשיר הזה תחילה.",
        "meta_file_op_recursive_move": "לא ניתן להעביר תיקייה לתוך עצמה.",
        "meta_file_op_missing_parent": "תיקיית היעד כבר לא קיימת.",
        "meta_file_op_not_a_folder": "„{name}” אינו תיקייה.",
        "meta_file_op_not_a_file":   "„{name}” הוא תיקייה, לא קובץ.",
        "meta_file_op_unsupported_platform": "הפעולה הזו זמינה ב‑Windows בלבד.",
        "meta_file_op_rename_failed": "לא ניתן היה לשנות את שם „{name}”. ייתכן שהוא פתוח בתוכנה אחרת.",
        "meta_file_op_move_failed":  "לא ניתן היה להעביר את „{name}”. ייתכן שהוא פתוח בתוכנה אחרת.",
        "meta_file_op_recycle_failed": "לא ניתן היה לשלוח את „{name}” לסל המיחזור. ייתכן שהוא פתוח בתוכנה אחרת.",
        "meta_file_op_create_folder_failed": "לא ניתן היה ליצור את התיקייה.",
        "meta_file_op_properties_failed": "לא ניתן היה לקרוא את הפרטים של „{name}”.",

        # ── Metadata controller status messages ────────────────────────────────
        "md_scanning_folder": "סורק: {folder}…",
        "md_auto_changes_proposed": "סדר אוטומטי: {n} שינויים הוצעו",
        "md_auto_no_changes": "סדר אוטומטי: כל הקבצים כבר מסודרים",
        "md_artist_applied": "אמן '{artist}' הוחל על {n} קבצים",
        "md_album_applied": "אלבום '{album}' הוחל על {n} קבצים",
        "md_no_changes_to_apply": "אין שינויים להחלה בקבצים הנבחרים",
        "md_writing_tags_to_n": "כותב תגיות ל-{n} קבצים…",
        "md_replaygain_no_supported_files": "אף אחד מהקבצים שנבחרו אינו יכול לשמור תגיות ReplayGain בבטחה.",
        "md_replaygain_analysis_started": "מנתח עוצמת שמע עבור {n} קבצים…",
        "md_replaygain_analysis_complete": "הצעות ReplayGain מוכנות עבור {n} קבצים. יש ללחוץ על החלה כדי לכתוב אותן.",
        "md_replaygain_analysis_partial": "הצעות ReplayGain מוכנות עבור {done} קבצים; הניתוח נכשל עבור {fail} קבצים.",
        "md_replaygain_analysis_cancelled": "ניתוח ReplayGain בוטל לאחר {n} קבצים. ההצעות שהושלמו נשמרו.",
        "md_replaygain_stale_results": "תוצאות ReplayGain השתייכו לבחירה קודמת ולכן לא הוחלו.",
        "md_album_artist_copied": "אמן אלבום הועתק מ-אמן ({n} קבצים)",
        "md_artist_title_split_done": "פיצול אמן-כותרת הושלם ({n} קבצים)",
        "md_year_cleared": "שנה נוקתה",
        "md_genre_cleared": "ז'אנר נוקה",
        "md_track_num_cleared": "מספר רצועה נוקה",
        "md_title_cleared": "כותרת נוקתה",
        "md_artist_cleared": "אמן נוקה",
        "md_album_cleared": "אלבום נוקה",
        "md_album_artist_cleared": "אמן אלבום נוקה",
        "md_spaces_normalised": "נוקה רווחים ב-{n} כותרות",
        "md_clean_settings_empty": "הגדרות הניקוי ריקות - לא בוצע שינוי",
        "md_junk_removed": "זבל הוסר מ-{n} כותרות",
        "md_filename_cleaned": "שם קובץ פיזי נוקה עבור {n} קבצים",
        "md_filename_numbering_removed": "מספור הוסר משם הקובץ עבור {n} קבצים",
        "md_filename_from_title": "שם קובץ נקבע לפי כותרת עבור {n} קבצים",
        "md_searching_duplicates_in": "מחפש כפילויות ב-{folder}…",
        "md_duplicates_deleted": "נמחקו {success} קבצים כפולים{note}",
        "md_duplicates_deleted_errors_suffix": ", {fail} שגיאות",
        "md_all_changes_reverted": "כל השינויים בוטלו",
        "md_scan_done": "נסרקו {n} קבצים ב-{folders} תיקיות",
        "md_scan_error": "שגיאה בסריקה: {msg}",
        "md_writing_tags_progress": "כותב תגיות… {done}/{total}",
        "md_apply_done": "הושלם — {success} הצליחו, {fail} נכשלו, {skip} דולגו{bp_note}",
        "md_apply_done_backup_note": " (גיבוי: {name})",
        "md_apply_backup_aborted": "ההחלה נחסמה — לא ניתן היה ליצור גיבוי, ולכן שום קובץ לא שונה.",
        "md_busy_disk_op": "כתיבת עורך התגיות כבר מתבצעת — יש להמתין לסיומה.",
        "md_recovery_no_backup": "שחזור: לא נרשם גיבוי עבור הפעולה שהופסקה.",
        "md_recovery_failed": "השחזור נכשל: {detail}",
        "md_recovery_prompt_title": "לשחזר פעולת עורך תגיות שהופסקה?",
        "md_recovery_prompt_msg": "פעולת החלה קודמת של עורך התגיות לא הסתיימה ({n} קבצים לא נפתרו). לשחזר את התגיות (ושמות הקבצים) המקוריים מהגיבוי?",
        "md_recovery_restore_btn": "שחזר מגיבוי",
        "md_recovery_notnow_btn": "לא עכשיו",
        "md_recovery_forget_btn": "שכח שחזור",
        "md_recovery_forget_title": "לשכוח את השחזור לצמיתות?",
        "md_recovery_forget_msg": "פעולה זו מוחקת לצמיתות את יומן השחזור. לא ניתן יהיה עוד לשחזר אוטומטית את הפעולה שהופסקה. להמשיך?",
        "md_recovery_running": "משחזר פעולה שהופסקה…",
        "md_recovery_preflight_failed": "השחזור נחסם: הגיבוי חסר או לא תקין ({code}). דבר לא שונה.",
        "md_recovery_done": "השחזור הושלם — {restored} קבצים שוחזרו.",
        "md_recovery_incomplete": "השחזור לא הושלם — {failed} נכשלו, {missing} חסרים. היומן נשמר כדי לאפשר ניסיון חוזר.",
        "md_recovery_forgotten": "יומן השחזור נמחק.",
        "md_shutdown_still_finishing": "כתיבת עורך התגיות עדיין מסתיימת בבטחה — היישום יישאר פתוח עד לסיומה.",
        "md_restoring_tags": "משחזר תגיות ל-{n} קבצים…",
        "md_restoring_progress": "משחזר תגיות… {done}/{total}",
        "md_restore_done": (
            "השחזור הסתיים — {restored} שוחזרו, {unchanged} כבר תואמים, "
            "{missing} חסרים, {fail} נכשלו"
        ),
        "md_restore_summary_title": "השחזור הושלם",
        "md_restore_pick_title": "בחר קובץ גיבוי תגיות לשחזור",
        "md_restore_invalid_title": "קובץ הגיבוי אינו תקין",
        "md_restore_invalid_msg": (
            "הקובץ אינו גיבוי תגיות שנוצר על ידי BananaFlow, או שלא ניתן לקרוא אותו."
        ),
        "md_restore_empty_msg": "קובץ הגיבוי אינו מכיל שירים.",
        "md_restore_all_missing_msg": (
            "אף אחד מהקבצים שנרשמו בגיבוי הזה כבר לא קיים בדיסק, "
            "כך שאין מה לשחזר."
        ),
        "md_restore_confirm_title": "לשחזר תגיות מגיבוי?",
        "md_restore_confirm_msg": (
            "התגיות שנשמרו בקובץ {backup} ייכתבו מחדש אל {n} קבצים.\n"
            "רק התגיות משתנות — אף קובץ לא נמחק, לא מועבר ולא משנה את שמו."
        ),
        "md_restore_missing_note": "{n} קבצים מהגיבוי כבר לא קיימים וידולגו.",
        "md_restore_more_files": "…ועוד {n} קבצים",
        "md_restore_confirm_btn": "שחזר תגיות",
        "md_duplicates_found_summary": "נמצאו {n_files} כפילויות ב-{n_groups} קבוצות ({strat}, {elapsed:.1f}s)",
        "md_strategy_size": "גודל קובץ",
        "md_strategy_md5": "MD5",

        # ── Folder names (channel output structure) ─────────────────────────────
        "folder_videos": "סרטונים",
        "folder_shorts": "קצרים",
        "folder_live": "שידורים חיים",
        "folder_playlists": "פלייליסטים",
        "folder_releases": "פריטי תוכן",
        "folder_podcasts": "פודקאסטים",
        "folder_singles_eps": "סינגלים ומיני אלבומים",
        "folder_singles_eps_variants": "סינגלים ומיני אלבומים",
        "folder_albums": "אלבומים",
        "folder_live_performances": "הופעות חיות",

        # ── About ───────────────────────────────────────────────────────────────
        "about_app": "אודות",

        # ── YouTube Doctor messages (keys defined in core.youtube_doctor.
        #    DOCTOR_TEXTS_EN; the English side is injected below) ────────────────
        "doctor_yt_dlp_missing": "yt-dlp אינו ניתן לטעינה.",
        "doctor_yt_dlp_missing_action": "התקן את yt-dlp:‏ pip install -U yt-dlp[default] ({exc})",
        "doctor_yt_dlp_ok": "yt-dlp {installed} עומד בגרסת המינימום הנדרשת ({minimum}).",
        "doctor_yt_dlp_outdated": (
            "yt-dlp {installed} ישן מהמינימום המומלץ ({minimum}). גרסאות "
            "ישנות חשופות יותר לכשלי PO Token ופענוח חתימות ש-YouTube שינה מאז."
        ),
        "doctor_yt_dlp_outdated_action": "עדכן את yt-dlp:‏ pip install -U \"yt-dlp[default]>={minimum}\"",
        "doctor_ejs_ok": "yt-dlp-ejs מותקן — פענוח JS/EJS של נגן YouTube זמין.",
        "doctor_ejs_missing": (
            "yt-dlp-ejs אינו מותקן. בלעדיו, חלק מהפורמטים ופענוח החתימות "
            "של YouTube עלולים להיות לא זמינים."
        ),
        "doctor_ejs_missing_action": "התקן את yt-dlp-ejs:‏ pip install -U yt-dlp[default] (כולל את yt-dlp-ejs)",
        "doctor_js_ok": "סביבת ריצה JS נבחרת: {selected} ({details}).",
        "doctor_js_ok_bundled": "סביבת ריצה JS נבחרת: {selected} — מגיעה ארוזה עם BananaFlow ({details}).",
        "doctor_js_node_too_old": (
            "נמצא Node {version} אך נדרשת גרסה 22 ומעלה. "
            "פענוח חתימות/נגן של YouTube עלול להיכשל."
        ),
        "doctor_js_none": (
            "לא נמצאה סביבת ריצה JS נתמכת ב-PATH. "
            "פענוח חתימות/נגן של YouTube עלול להיכשל."
        ),
        "doctor_js_action": "התקן Deno (מומלץ), Node 22 ומעלה, או QuickJS.",
        "doctor_cookies_browser": (
            "מוגדר שימוש בעוגיות חיות מהדפדפן '{browser}'. לא ניתן לאמת "
            "קיום או מצב התחברות במצב לא-מקוון; אם החילוץ ייכשל, yt-dlp "
            "ידווח על שגיאה בזמן ההורדה."
        ),
        "doctor_cookies_browser_windows_unsupported": (
            "לא ניתן לקרוא בבטחה פרופיל חי של '{browser}' ב-Windows. יש להשתמש "
            "בדפדפן ההתחברות המבודד של BananaFlow או לייבא cookies.txt; היישום "
            "לא יעקוף נעילות פרופיל או הצפנת App-Bound."
        ),
        "doctor_cookies_none": (
            "לא הוגדרו עוגיות. סרטונים ציבוריים ימשיכו לעבוד; סרטונים "
            "מוגבלי גיל, פרטיים או לחברים בלבד ייכשלו ללא עוגיות."
        ),
        "doctor_cookies_file_missing": "קובץ העוגיות המוגדר '{name}' אינו קיים.",
        "doctor_cookies_file_unreadable": "לא ניתן לקרוא את קובץ העוגיות המוגדר '{name}'.",
        "doctor_cookies_file_empty": "קובץ העוגיות '{name}' ריק. ייתכן שיש לייצא את העוגיות מחדש.",
        "doctor_cookies_not_youtube": (
            "בקובץ העוגיות '{name}' יש רשומות, אך אף אחת מהן לא נראית "
            "שייכת ל-YouTube. ייתכן שיש לייצא את העוגיות מחדש."
        ),
        "doctor_cookies_no_login": (
            "נראה שקיימות עוגיות YouTube בקובץ '{name}', אך לא נמצאו עוגיות "
            "התחברות (למשל LOGIN_INFO/SID) — ייתכן שזהו סשן אנונימי בלבד. "
            "ייתכן שיש לייצא את העוגיות מחדש."
        ),
        "doctor_cookies_login_ok": (
            "נראה שקיימות עוגיות התחברות בקובץ '{name}'. אין בכך ערובה שלא "
            "פג תוקפן — ייצא מחדש אם הורדות מוגבלות נכשלות."
        ),
        "doctor_cookies_reexport": "ייצא מחדש עוגיות מסשן YouTube מחובר.",
        "doctor_cookies_permissions": "בדוק את הרשאות הקובץ, או ייצא את העוגיות מחדש.",
        "doctor_cookies_reexport_on_youtube": "ייצא עוגיות מחדש בזמן ביקור ב-youtube.com כשאתה מחובר.",
        "doctor_cookies_reexport_signed_in": "ייצא עוגיות מחדש כשאתה מחובר לחשבון YouTube/Google.",
        "doctor_pot_ready": (
            "ספק PO Token מוכן: תוסף bgutil זמין, Deno הארוז נבחר, "
            "backend מסוג Deno script קיים, ובדיקת הבריאות עברה "
            "(גרסת script {version}). yt-dlp ישתמש במנגנון הספק הרשמי "
            "עם server_home הארוז של BananaFlow; BananaFlow לא מייצרת, שומרת "
            "או מזריקה PO Tokens בעצמה."
        ),
        "doctor_pot_plugin_only": (
            "תוסף PO Token Provider ארוז, אבל סביבת JavaScript וה-backend "
            "של הספק אינם זמינים יחד. זהו מצב של תוסף בלבד, ולא מצב PO מוכן."
        ),
        "doctor_pot_plugin_runtime_no_backend": (
            "תוסף PO Token Provider וסביבת JavaScript זמינים, אבל backend "
            "ה-Deno script של bgutil חסר או לא שלם. זה עדיין לא PO מוכן עד "
            "שקובצי backend/server ו-node_modules ייארזו."
        ),
        "doctor_pot_backend_unhealthy": (
            "ה-backend של ספק PO Token קיים, אבל נכשל בבדיקת הבריאות של "
            "Deno script: {reason}. זה עדיין לא PO מוכן."
        ),
        "doctor_pot_script_provider_missing": (
            "ה-backend מסוג Deno script עבר בדיקת בריאות, אבל מודול "
            "getpot_bgutil_script של ספק PO Token לא זוהה. זה עדיין לא PO מוכן."
        ),
        "doctor_pot_installed_no_backend": (
            "נראה שספק PO Token מותקן{name_note} דרך {methods}. בהרצה מקוד "
            "מקור אפשר להגדיר ספק ידנית, אבל בבנייה הזו של BananaFlow ה-backend "
            "הארוז של הספק אינו מוכן."
        ),
        "doctor_pot_bundled": (
            "תוסף PO Token Provider ארוז. YouTube Doctor ידווח על מוכנות מלאה רק אם Deno "
            "וה-backend הארוז של הספק קיימים ועוברים בדיקת בריאות."
        ),
        "doctor_pot_installed": (
            "נראה שספק PO Token מותקן{name_note} דרך {methods}. כדי לדווח על מוכנות מלאה "
            "נדרש backend ארוז ובריא של הספק."
        ),
        "doctor_pot_missing": (
            "לא זוהה תוסף PO Token Provider. בניית BananaFlow ציבורית אמורה לכלול אותו; חלק "
            "מסרטוני YouTube עלולים להיכשל בשגיאת PO Token עד שקובצי הספק הארוזים יהיו "
            "זמינים ל-yt-dlp."
        ),
        "doctor_pot_missing_action": (
            "עדכן או התקן מחדש את BananaFlow כדי שקובצי PO Token Provider הארוזים יהיו קיימים. "
            "בהתקנת מקור, התקן את תוספת po-token והריץ את כלי staging של הספק."
        ),
        "doctor_reliability_conservative": (
            "מצב שמרני של YouTube פעיל (ברירת מחדל): מספר הורדות YouTube "
            "באצווה מבוצעות אחת-אחת עם המתנה של 5–10 שניות ביניהן, "
            "ומקביליות המקטעים מוגבלת ל-1. הורדות שאינן מ-YouTube אינן מושפעות."
        ),
        "doctor_reliability_fast": (
            "מצב שמרני של YouTube כבוי (מצב 'מהיר' הוא בחירה מפורשת). "
            "הורדות YouTube רצות במקביליות רגילה, מה שמעלה את הסיכון "
            "לשגיאות 403, הגבלת קצב ואתגרי PO Token/בוט."
        ),

        # ── Error dialogs (keys defined in error_handler.ERROR_TEXTS_EN;
        #    the English side is injected below) ─────────────────────────────────
        "err_spotify_metadata_invalid_title": "פרטי רצועת Spotify אינם תקינים",
        "err_spotify_metadata_invalid_detail": (
            "Spotify החזיר פרטי רצועה חסרים, פגומים או מזוהמים בתוכן מהעמוד. "
            "הרצועה נשארה ללא פתרון ולא נוספה לתור ההורדה."
        ),
        "err_browser_cookie_access_title": "לא ניתן לקרוא עוגיות דפדפן בבטחה",
        "err_browser_cookie_access_detail": (
            "Windows מגן ונועל פרופילים חיים של Chrome, Edge ו-Brave. "
            "BananaFlow לא יעקוף את ההגנות. ניתן לפתוח את עוזר ההתחברות עם "
            "פרופיל נפרד של BananaFlow, או לייבא קובץ cookies.txt."
        ),
        "err_bot_challenge_title": "YouTube ביקש אימות אנושי",
        "err_bot_challenge_detail": (
            "YouTube הציג אתגר נגד בוטים. יש לעצור ניסיונות חוזרים ולהמתין לפני "
            "ניסיון נוסף. אם התוכן דורש חשבון, ניתן להשתמש בעוזר ההתחברות; "
            "החלפת סרטון לא תפתור את האתגר."
        ),
        "err_po_token_title": "נדרש PO Token",
        "err_po_token_detail": (
            "YouTube דורש PO Token עבור הסרטון הזה. הפעל את YouTube Doctor "
            "ועדכן או התקן מחדש את BananaFlow אם חבילת PO Token Provider הארוזה "
            "אינה מוכנה. בהתקנת מקור, התקן את תוספת po-token והריץ את כלי "
            "staging של הספק."
        ),
        "err_cookies_expired_title": "פג תוקף עוגיות YouTube",
        "err_cookies_expired_detail": (
            "נראה שעוגיות ה-YouTube שלך פגו תוקף או אינן תקינות. ייצא "
            "עוגיות מחדש ונסה שוב."
        ),
        "err_js_runtime_title": "לא נמצאה סביבת ריצה JavaScript",
        "err_js_runtime_detail": (
            "לא נמצאה סביבת ריצה JavaScript נתמכת. התקן Deno או Node 22 "
            "ומעלה, ואז הפעל שוב את YouTube Doctor."
        ),
        "err_signin_required_title": "נדרשת התחברות",
        "err_signin_required_detail": (
            "נראה שהסרטון דורש התחברות (מוגבל גיל או דורש חשבון). הגדר "
            "עוגיות YouTube אם יש לך גישה לתוכן.\n\n"
            "השתמש בעוזר ההתחברות המבודד של BananaFlow, או ייבא קובץ "
            "cookies.txt בהגדרות. אין צורך בגישה לפרופיל הדפדפן הרגיל."
        ),
        "err_video_unavailable_title": "הסרטון אינו זמין",
        "err_video_unavailable_detail": "הסרטון פרטי, נמחק, או אינו זמין באזור שלך.",
        "err_geo_restricted_title": "תוכן מוגבל גיאוגרפית",
        "err_geo_restricted_detail": (
            "התוכן אינו זמין במדינה שלך.\n\n"
            "שקול שימוש ב-VPN או בקובץ עוגיות מאזור מתאים."
        ),
        "err_rate_limited_title": "‏YouTube הגביל את קצב הבקשות",
        "err_rate_limited_detail": (
            "YouTube חסם את הבקשה או הגביל את הקצב שלה.\n\n"
            "השאר את המצב השמרני פעיל, המתן כמה דקות, והימנע מניסיונות "
            "חוזרים — ניסיון מיידי מחדש נוטה להחמיר את הגבלת הקצב."
        ),
        "err_403_title": "הגישה נדחתה (403)",
        "err_403_detail": (
            "YouTube חסם את הבקשה או הגביל את הקצב שלה.\n\n"
            "בדרך כלל זה אומר שהסרטון דורש אימות, או שבקשות אוטומטיות "
            "נחסמות. השאר את המצב השמרני פעיל, המתן, והימנע מניסיונות "
            "חוזרים. נסה להוסיף קובץ עוגיות בהגדרות אם יש לך גישה לתוכן."
        ),
        "err_copyright_title": "התוכן נחסם עקב זכויות יוצרים",
        "err_copyright_detail": "הסרטון הוגבל עקב תביעת זכויות יוצרים ולא ניתן להוריד אותו.",
        "err_unsupported_url_title": "כתובת לא נתמכת",
        "err_unsupported_url_detail": (
            "yt-dlp לא מצא מחלץ מתאים לכתובת הזו.\n\n"
            "ודא שהכתובת היא קישור ישיר לסרטון, לפלייליסט או לאלבום."
        ),
        "err_truncated_url_title": "קישור לסרטון חסר",
        "err_truncated_url_detail": (
            "לקישור ה-YouTube הזה חסרים תווים ממזהה הסרטון, ולכן הוא לא "
            "מצביע על סרטון אמיתי.\n\n"
            "העתק שוב את הקישור המלא (משורת הכתובת או מכפתור השיתוף) ונסה שוב."
        ),
        "err_network_title": "שגיאת רשת",
        "err_network_detail": (
            "אירעה שגיאת רשת בתקשורת עם השרת.\n\n"
            "בדוק את חיבור האינטרנט ונסה שוב."
        ),
        "err_ssl_title": "שגיאת SSL / אישור אבטחה",
        "err_ssl_detail": (
            "לא ניתן היה ליצור חיבור מאובטח.\n\n"
            "ייתכן ששעון המערכת שגוי, או שחומת אש מיירטת תעבורת HTTPS."
        ),
        "err_ffmpeg_missing_title": "‏FFmpeg לא נמצא",
        "err_ffmpeg_missing_detail": (
            "yt-dlp זקוק ל-FFmpeg כדי למזג או להמיר אודיו/וידאו.\n\n"
            "התקן FFmpeg וודא שהוא זמין ב-PATH של המערכת.\n\n"
            "  Windows : winget install Gyan.FFmpeg\n"
            "  macOS   : brew install ffmpeg\n"
            "  Linux   : sudo apt install ffmpeg"
        ),
        "err_disk_permissions_title": "שגיאת דיסק / הרשאות",
        "err_disk_permissions_detail": (
            "לא ניתן לכתוב את הקובץ שהורד.\n\n"
            "הדיסק מלא, או שאין לך הרשאת כתיבה לתיקיית הפלט. בחר תיקייה "
            "אחרת בהגדרות."
        ),
        "err_no_internet_title": "אין חיבור לאינטרנט",
        "err_no_internet_detail": (
            "לא ניתן להגיע לאינטרנט.\n\n"
            "בדוק את חיבור הרשת ונסה שוב."
        ),
        "err_connection_failed_title": "החיבור נכשל",
        "err_connection_failed_detail": (
            "לא ניתן להתחבר לשרת.\n\n"
            "ייתכן שהשירות אינו זמין באופן זמני."
        ),
        "err_timeout_title": "תם הזמן לבקשה",
        "err_timeout_detail": "השרת לא הגיב בזמן.\n\nנסה שוב בעוד רגע.",
        "err_permission_denied_title": "ההרשאה נדחתה",
        "err_permission_denied_detail": (
            "לא ניתן לכתוב לתיקיית הפלט.\n\n"
            "בחר תיקייה אחרת בהגדרות."
        ),
        "err_generic_title": "ההורדה נכשלה",
        "err_generic_detail": (
            "אירעה שגיאה בלתי צפויה:\n\n{short}\n\n"
            "אם הבעיה נמשכת, בדוק את חיבור האינטרנט ונסה שוב."
        ),
        "err_doctor_prefix": "YouTube Doctor: ",
        "meta_format_supported": "ניתן לערוך מטא־דאטה בפורמט זה.",
        "meta_format_wav_limited": "עריכת מטא־דאטה מוגבלת: BananaFlow כותב תגיות ID3 בקבצי WAV; מטא־דאטה קיים מסוג RIFF INFO/BWF נשאר ללא שינוי.",
        "meta_format_read_only": "אפשר להציג את הפורמט, אך BananaFlow אינו יכול לערוך את המטא־דאטה שלו בבטחה.",
        "meta_format_future": "הפורמט זוהה, אך עריכת מטא־דאטה עבורו מתוכננת לעדכון עתידי.",
        "meta_field_track_total": "סך רצועות:",
        "meta_field_disc": "דיסק:",
        "meta_field_disc_total": "סך דיסקים:",
        "meta_field_date": "תאריך:",
        "meta_field_genre": "סוגה:",
        "meta_field_comment": "הערה:",
        "meta_field_composer": "מלחין:",
        "meta_field_publisher": "מוציא לאור:",
        "meta_field_copyright": "זכויות יוצרים:",
        "meta_field_bpm": "BPM:",
        "meta_field_isrc": "ISRC:",
        "meta_field_grouping": "קיבוץ:",
        "meta_field_sort_title": "מיון כותרת:",
        "meta_field_sort_artist": "מיון אמן:",
        "meta_field_sort_album": "מיון אלבום:",
        "meta_field_sort_album_artist": "מיון אמן אלבום:",
    },
}


# ── Core-produced diagnostic/error texts ─────────────────────────────────────
# core.youtube_doctor and error_handler build their English messages from
# module-level template dicts and attach the template key + params to each
# result. Injecting those dicts here (instead of copying the strings) makes
# the "en" table physically incapable of drifting from what core renders;
# the Hebrew entries for the same keys live in the "he" dict above and are
# parity-checked by tests/test_i18n_coverage.py.
from core.youtube_doctor import DOCTOR_TEXTS_EN as _DOCTOR_TEXTS_EN       # noqa: E402
from error_handler import ERROR_TEXTS_EN as _ERROR_TEXTS_EN               # noqa: E402
from error_handler import PREFLIGHT_TEXTS_EN as _PREFLIGHT_TEXTS_EN       # noqa: E402

TRANSLATIONS["en"].update(_DOCTOR_TEXTS_EN)
TRANSLATIONS["en"].update(_ERROR_TEXTS_EN)
TRANSLATIONS["en"].update(_PREFLIGHT_TEXTS_EN)

# Phase 9 Action & Template Engine.  Kept together so every production label
# has an English/Hebrew pair and internal stable IDs never leak into the UI.
_TAG_ACTION_TEXTS_EN = {
    "meta_action_engine_title": "Actions, Templates & Presets",
    "meta_action_engine_subtitle": "Preview safe metadata and filename changes, then add them to Pending Changes.",
    "meta_action_engine_page_title": "Build repeatable tag and filename changes",
    "meta_action_engine_page_body": "Choose an action, a filename template, or a saved workflow. Nothing is written to disk from this preview.",
    "meta_action_engine_open": "Open Actions, Templates & Presets",
    "meta_action_kind_accessible": "Action type",
    "meta_actions_tab": "Actions",
    "meta_templates_tab": "Templates",
    "meta_presets_tab": "Presets",
    "meta_action_select": "Action",
    "meta_template_select": "Template direction",
    "meta_preset_select": "Saved workflow",
    "meta_action_scope": "Scope",
    "meta_scope_current": "Current row",
    "meta_scope_selected": "Selected rows",
    "meta_scope_visible": "Visible rows",
    "meta_scope_active_folder": "Active folder",
    "meta_action_parameters": "Parameters",
    "meta_action_targets": "Targets: {n}",
    "meta_action_supported": "Supported: {n}",
    "meta_action_expected_changes": "Changes: {n}",
    "meta_action_skipped": "Skipped / no-op: {n}",
    "meta_action_blockers": "Warnings / blockers: {n}",
    "meta_action_changed_only": "Changed only",
    "meta_action_col_file": "File",
    "meta_action_col_field": "Field",
    "meta_action_col_old": "Current",
    "meta_action_col_new": "Proposed",
    "meta_action_col_status": "Status",
    "meta_action_col_details": "Details",
    "meta_action_preview_accessible": "Action preview results",
    "meta_action_preview": "Refresh preview",
    "meta_action_back_parameters": "Back to parameters",
    "meta_action_add_pending": "Add to Pending Changes",
    "meta_action_status_changed": "Changed",
    "meta_action_status_no_op": "No change",
    "meta_action_status_skipped": "Skipped",
    "meta_action_status_unsupported": "Unsupported",
    "meta_action_status_warning": "Warning",
    "meta_action_status_blocker": "Blocked",
    "meta_action_status_collision": "Collision",
    "meta_action_stale_title": "Preview is out of date",
    "meta_action_stale_body": "The selection or Pending Changes changed. Review the refreshed preview before adding it.",
    "meta_action_auto_arrange": "Auto Arrange",
    "meta_action_auto_arrange_desc": "Derive title, track number and album from the filename and folder.",
    "meta_action_filename_from_title": "Rename file from title",
    "meta_action_filename_from_title_desc": "Build a safe filename from the current title while preserving the extension.",
    "meta_action_filename_to_tags": "Parse filename into tags",
    "meta_action_filename_to_tags_desc": "Extract named tag fields from a filename using a constrained template.",
    "meta_action_tags_to_filename": "Build filename from tags",
    "meta_action_tags_to_filename_desc": "Render a safe filename from tag fields and preview rename collisions.",
    "meta_action_set_field": "Set a tag field",
    "meta_action_set_field_desc": "Set one metadata field to the same value across the chosen scope.",
    "meta_action_set_artist": "Set artist and missing album artist",
    "meta_action_set_artist_desc": "Set artist and fill album artist only when it is empty.",
    "meta_action_replace": "Find and replace text",
    "meta_action_replace_desc": "Replace literal text in a chosen metadata field.",
    "meta_action_case": "Change text case",
    "meta_action_case_desc": "Convert a chosen field to upper, lower, title or sentence case.",
    "meta_action_number": "Number tracks",
    "meta_action_number_desc": "Assign sequential track numbers in stable scope order.",
    "meta_action_param_template": "Template",
    "meta_action_param_overwrite": "Overwrite existing values",
    "meta_action_param_sanitize": "Sanitize Windows filename",
    "meta_action_param_strip_numbering": "Remove leading numbering",
    "meta_action_param_field": "Field",
    "meta_action_param_value": "Value",
    "meta_action_param_find": "Find",
    "meta_action_param_replace": "Replace with",
    "meta_action_param_case_sensitive": "Case-sensitive",
    "meta_action_param_mode": "Case style",
    "meta_action_param_start": "Start number",
    "meta_action_param_step": "Increment",
    "meta_action_param_smart_brackets": "Remove known bracketed junk",
    "meta_action_param_remove_domains": "Remove downloader domains",
    "meta_action_param_remove_emojis": "Remove emoji",
    "meta_action_param_fix_spaces": "Normalize spaces",
    "meta_action_param_remove_web_junk": "Remove web additions",
    "meta_action_param_remove_hebrew": "Remove Hebrew cleanup terms",
    "meta_action_param_fix_punctuation": "Fix punctuation and spaces",
    "meta_action_diag_missing_value": "The {field} value is missing.",
    "meta_action_diag_unknown_field": "The placeholder {token} is not supported.",
    "meta_action_diag_invalid_numeric": "The {field} value must be a number.",
    "meta_action_diag_unknown_parameter": "The {parameter} parameter is not supported for this action.",
    "meta_action_diag_parameter_required": "Enter a value for {parameter}.",
    "meta_action_diag_invalid_parameter": "The {parameter} parameter is invalid.",
    "meta_action_diag_optional_parse": "Optional template segments cannot be used when reading tags from a filename.",
    "meta_action_diag_template_no_match": "The filename does not match this template.",
    "meta_action_diag_invalid_template": "The template direction is invalid.",
    "meta_action_diag_empty_template": "Enter a template before previewing.",
    "meta_action_diag_invalid_optional": "The optional template segment is invalid.",
    "meta_action_diag_template_no_fields": "The template must contain at least one placeholder.",
    "meta_action_diag_repeated_field": "A field cannot appear more than once when reading a filename.",
    "meta_action_diag_adjacent_field": "Put text between adjacent template fields so they can be read safely.",
    "meta_action_diag_invalid_placeholder": "The template contains an invalid placeholder.",
    "meta_action_diag_invalid_filename_chars": "The proposed filename contains invalid Windows characters.",
    "meta_action_diag_empty_filename": "The proposed filename would be empty.",
    "meta_action_diag_reserved_filename": "The proposed filename uses a reserved Windows device name.",
    "meta_action_diag_invalid_extension": "The file extension is invalid.",
    "meta_action_diag_filename_too_long": "The proposed filename is too long.",
    "meta_action_diag_unsupported_item": "This item cannot be edited.",
    "meta_action_diag_unsupported_format": "This file format is not supported by this action.",
    "meta_action_diag_unsupported": "This action is not supported for the selected item.",
    "meta_action_diag_number_not_found": "No track number was found in the filename.",
    "meta_action_diag_filename_pattern": "The filename does not contain an artist and title separator.",
    "meta_action_diag_artist_missing": "The Artist value is missing.",
    "meta_action_diag_title_missing": "The Title value is missing.",
    "meta_action_diag_cleanup_empty_title": "Cleaning would remove the entire title.",
    "meta_action_diag_invalid_track_num": "The track number in the filename is invalid.",
    "meta_action_diag_invalid_disc_num": "The disc number in the filename is invalid.",
    "meta_action_diag_rename_collision": "The proposed destination already exists or conflicts with another proposed filename.",
    "meta_action_diag_rename_reserved": "The proposed filename uses a reserved Windows device name.",
    "meta_action_diag_rename_invalid": "The proposed filename is invalid on Windows.",
    "meta_action_diag_rename_escape": "The proposed filename must stay within the current folder.",
    "meta_action_diag_rename_locked": "The proposed destination is currently unavailable.",
    "meta_action_diag_rename_failed": "The rename plan cannot be completed safely.",
    "meta_action_diag_rename_blocked_sibling": "A related rename prevents this filename from being used.",
    "meta_action_diag_unknown": "The preview found an issue. Review the action parameters and try again.",
    "meta_action_choice_title": "Title",
    "meta_action_choice_artist": "Artist",
    "meta_action_choice_album": "Album",
    "meta_action_choice_album_artist": "Album artist",
    "meta_action_choice_genre": "Genre",
    "meta_action_choice_year": "Year",
    "meta_action_choice_comment": "Comment",
    "meta_action_choice_upper": "UPPER CASE",
    "meta_action_choice_lower": "lower case",
    "meta_action_choice_sentence": "Sentence case",
    "meta_template_example": "Example: {track_num:02} - {artist} - {title}[ - {album}]",
    "meta_preset_sequence_note": "This saved workflow contains an ordered action sequence. Preview evaluates the whole sequence as one Pending Changes command.",
    "meta_preset_save_as": "Save as preset",
    "meta_preset_update": "Update",
    "meta_preset_rename": "Rename",
    "meta_preset_duplicate": "Duplicate",
    "meta_preset_delete": "Delete",
    "meta_preset_reset_builtins": "Reset built-ins",
    "meta_preset_name_prompt": "Preset name",
    "meta_preset_copy_name": "{name} copy",
    "meta_preset_delete_confirm": "Delete the custom preset “{name}”?",
    "meta_preset_saved": "Custom presets saved.",
    "meta_preset_store_corrupt": "The custom preset file is damaged. Built-in presets are still available.",
    "meta_preset_store_unsupported": "This preset file uses an unsupported schema. Built-in presets are still available.",
    "meta_preset_store_migrated": "Older custom presets were loaded and will be upgraded on the next save.",
    "meta_preset_builtin_artist_title": "Artist - Title",
    "meta_preset_builtin_track_artist_title": "Track - Artist - Title",
    "meta_preset_builtin_parse_artist_title": "Artist - Title to tags",
    "meta_field_track_num": "Track number",
    "meta_field_disc_num": "Disc number",
    "meta_field_year": "Year",
    "meta_field_filename": "Filename",
    "meta_field_value": "Value",
}

_TAG_ACTION_TEXTS_HE = {
    "meta_action_engine_title": "פעולות, תבניות והגדרות שמורות",
    "meta_action_engine_subtitle": "הצגה מקדימה של שינויים בטוחים בתגיות ובשמות קבצים, ואז הוספתם לשינויים הממתינים.",
    "meta_action_engine_page_title": "יצירת שינויים חוזרים לתגיות ולשמות קבצים",
    "meta_action_engine_page_body": "בחרו פעולה, תבנית שם קובץ או תהליך שמור. דבר אינו נכתב לדיסק מתוך התצוגה המקדימה.",
    "meta_action_engine_open": "פתיחת פעולות, תבניות והגדרות שמורות",
    "meta_action_kind_accessible": "סוג פעולה",
    "meta_actions_tab": "פעולות",
    "meta_templates_tab": "תבניות",
    "meta_presets_tab": "הגדרות שמורות",
    "meta_action_select": "פעולה",
    "meta_template_select": "כיוון התבנית",
    "meta_preset_select": "תהליך שמור",
    "meta_action_scope": "טווח",
    "meta_scope_current": "השורה הנוכחית",
    "meta_scope_selected": "השורות שנבחרו",
    "meta_scope_visible": "השורות המוצגות",
    "meta_scope_active_folder": "התיקייה הפעילה",
    "meta_action_parameters": "פרמטרים",
    "meta_action_targets": "יעדים: {n}",
    "meta_action_supported": "נתמכים: {n}",
    "meta_action_expected_changes": "שינויים: {n}",
    "meta_action_skipped": "דילוג / ללא שינוי: {n}",
    "meta_action_blockers": "אזהרות / חסימות: {n}",
    "meta_action_changed_only": "שורות שהשתנו בלבד",
    "meta_action_col_file": "קובץ",
    "meta_action_col_field": "שדה",
    "meta_action_col_old": "נוכחי",
    "meta_action_col_new": "מוצע",
    "meta_action_col_status": "מצב",
    "meta_action_col_details": "פרטים",
    "meta_action_preview_accessible": "תוצאות תצוגה מקדימה של פעולה",
    "meta_action_preview": "רענון תצוגה מקדימה",
    "meta_action_back_parameters": "חזרה לפרמטרים",
    "meta_action_add_pending": "הוספה לשינויים הממתינים",
    "meta_action_status_changed": "השתנה",
    "meta_action_status_no_op": "ללא שינוי",
    "meta_action_status_skipped": "דולג",
    "meta_action_status_unsupported": "לא נתמך",
    "meta_action_status_warning": "אזהרה",
    "meta_action_status_blocker": "חסום",
    "meta_action_status_collision": "התנגשות",
    "meta_action_stale_title": "התצוגה המקדימה אינה עדכנית",
    "meta_action_stale_body": "הבחירה או השינויים הממתינים השתנו. יש לבדוק את התצוגה המרועננת לפני ההוספה.",
    "meta_action_auto_arrange": "סידור אוטומטי",
    "meta_action_auto_arrange_desc": "הפקת כותרת, מספר רצועה ואלבום משם הקובץ ומהתיקייה.",
    "meta_action_filename_from_title": "שינוי שם קובץ לפי הכותרת",
    "meta_action_filename_from_title_desc": "יצירת שם קובץ בטוח מהכותרת הנוכחית תוך שמירת הסיומת.",
    "meta_action_filename_to_tags": "פענוח שם קובץ לתגיות",
    "meta_action_filename_to_tags_desc": "חילוץ שדות תגית משם קובץ באמצעות תבנית מוגבלת ובטוחה.",
    "meta_action_tags_to_filename": "יצירת שם קובץ מתגיות",
    "meta_action_tags_to_filename_desc": "יצירת שם קובץ בטוח משדות תגית והצגה מקדימה של התנגשויות.",
    "meta_action_set_field": "הגדרת שדה תגית",
    "meta_action_set_field_desc": "הגדרת שדה מטא-נתונים אחד לאותו ערך בכל הטווח שנבחר.",
    "meta_action_set_artist": "הגדרת אמן ואמן אלבום חסר",
    "meta_action_set_artist_desc": "הגדרת האמן ומילוי אמן האלבום רק כאשר הוא ריק.",
    "meta_action_replace": "חיפוש והחלפת טקסט",
    "meta_action_replace_desc": "החלפת טקסט מילולי בשדה מטא-נתונים נבחר.",
    "meta_action_case": "שינוי אותיות הטקסט",
    "meta_action_case_desc": "המרת שדה נבחר לאותיות גדולות, קטנות, כותרת או משפט.",
    "meta_action_number": "מספור רצועות",
    "meta_action_number_desc": "הקצאת מספרי רצועות עוקבים לפי סדר הטווח היציב.",
    "meta_action_param_template": "תבנית",
    "meta_action_param_overwrite": "דריסת ערכים קיימים",
    "meta_action_param_sanitize": "ניקוי שם קובץ עבור Windows",
    "meta_action_param_strip_numbering": "הסרת מספור מוביל",
    "meta_action_param_field": "שדה",
    "meta_action_param_value": "ערך",
    "meta_action_param_find": "חיפוש",
    "meta_action_param_replace": "החלפה ב-",
    "meta_action_param_case_sensitive": "התאמת רישיות",
    "meta_action_param_mode": "סגנון אותיות",
    "meta_action_param_start": "מספר התחלתי",
    "meta_action_param_step": "תוספת",
    "meta_action_param_smart_brackets": "הסרת טקסט מיותר מוכר בסוגריים",
    "meta_action_param_remove_domains": "הסרת כתובות של אתרי הורדה",
    "meta_action_param_remove_emojis": "הסרת אימוג׳י",
    "meta_action_param_fix_spaces": "סידור רווחים",
    "meta_action_param_remove_web_junk": "הסרת תוספות רשת",
    "meta_action_param_remove_hebrew": "הסרת מונחי ניקוי בעברית",
    "meta_action_param_fix_punctuation": "תיקון פיסוק ורווחים",
    "meta_action_diag_missing_value": "הערך עבור {field} חסר.",
    "meta_action_diag_unknown_field": "מציין המיקום {token} אינו נתמך.",
    "meta_action_diag_invalid_numeric": "הערך עבור {field} חייב להיות מספר.",
    "meta_action_diag_unknown_parameter": "הפרמטר {parameter} אינו נתמך עבור פעולה זו.",
    "meta_action_diag_parameter_required": "יש להזין ערך עבור {parameter}.",
    "meta_action_diag_invalid_parameter": "הפרמטר {parameter} אינו תקין.",
    "meta_action_diag_optional_parse": "אי אפשר להשתמש במקטעי תבנית אופציונליים בעת קריאת תגיות משם קובץ.",
    "meta_action_diag_template_no_match": "שם הקובץ אינו תואם לתבנית זו.",
    "meta_action_diag_invalid_template": "כיוון התבנית אינו תקין.",
    "meta_action_diag_empty_template": "יש להזין תבנית לפני הצגה מקדימה.",
    "meta_action_diag_invalid_optional": "מקטע התבנית האופציונלי אינו תקין.",
    "meta_action_diag_template_no_fields": "התבנית חייבת לכלול לפחות מציין מיקום אחד.",
    "meta_action_diag_repeated_field": "אי אפשר להשתמש באותו שדה יותר מפעם אחת בעת קריאת שם קובץ.",
    "meta_action_diag_adjacent_field": "יש להוסיף טקסט בין שדות תבנית צמודים כדי לקרוא אותם בבטחה.",
    "meta_action_diag_invalid_placeholder": "התבנית כוללת מציין מיקום לא תקין.",
    "meta_action_diag_invalid_filename_chars": "שם הקובץ המוצע כולל תווים שאינם תקינים ב-Windows.",
    "meta_action_diag_empty_filename": "שם הקובץ המוצע יהיה ריק.",
    "meta_action_diag_reserved_filename": "שם הקובץ המוצע משתמש בשם התקן שמור של Windows.",
    "meta_action_diag_invalid_extension": "סיומת הקובץ אינה תקינה.",
    "meta_action_diag_filename_too_long": "שם הקובץ המוצע ארוך מדי.",
    "meta_action_diag_unsupported_item": "אי אפשר לערוך פריט זה.",
    "meta_action_diag_unsupported_format": "תבנית קובץ זו אינה נתמכת עבור פעולה זו.",
    "meta_action_diag_unsupported": "פעולה זו אינה נתמכת עבור הפריט שנבחר.",
    "meta_action_diag_number_not_found": "לא נמצא מספר רצועה בשם הקובץ.",
    "meta_action_diag_filename_pattern": "שם הקובץ אינו כולל מפריד בין אמן לכותרת.",
    "meta_action_diag_artist_missing": "הערך עבור אמן חסר.",
    "meta_action_diag_title_missing": "הערך עבור כותרת חסר.",
    "meta_action_diag_cleanup_empty_title": "הניקוי ימחק את כל הכותרת.",
    "meta_action_diag_invalid_track_num": "מספר הרצועה בשם הקובץ אינו תקין.",
    "meta_action_diag_invalid_disc_num": "מספר הדיסק בשם הקובץ אינו תקין.",
    "meta_action_diag_rename_collision": "יעד השינוי המוצע כבר קיים או מתנגש בשם קובץ מוצע אחר.",
    "meta_action_diag_rename_reserved": "שם הקובץ המוצע משתמש בשם התקן שמור של Windows.",
    "meta_action_diag_rename_invalid": "שם הקובץ המוצע אינו תקין ב-Windows.",
    "meta_action_diag_rename_escape": "שם הקובץ המוצע חייב להישאר בתוך התיקייה הנוכחית.",
    "meta_action_diag_rename_locked": "היעד המוצע אינו זמין כרגע.",
    "meta_action_diag_rename_failed": "לא ניתן להשלים את תוכנית שינוי השם בבטחה.",
    "meta_action_diag_rename_blocked_sibling": "שינוי שם קשור מונע שימוש בשם קובץ זה.",
    "meta_action_diag_unknown": "התצוגה המקדימה מצאה בעיה. יש לבדוק את פרמטרי הפעולה ולנסות שוב.",
    "meta_action_choice_title": "כותרת",
    "meta_action_choice_artist": "אמן",
    "meta_action_choice_album": "אלבום",
    "meta_action_choice_album_artist": "אמן אלבום",
    "meta_action_choice_genre": "סוגה",
    "meta_action_choice_year": "שנה",
    "meta_action_choice_comment": "הערה",
    "meta_action_choice_upper": "אותיות גדולות",
    "meta_action_choice_lower": "אותיות קטנות",
    "meta_action_choice_sentence": "אות גדולה בתחילת משפט",
    "meta_template_example": "דוגמה: {track_num:02} - {artist} - {title}[ - {album}]",
    "meta_preset_sequence_note": "התהליך השמור מכיל רצף פעולות מסודר. התצוגה המקדימה מחשבת את כולו כפקודה אחת בשינויים הממתינים.",
    "meta_preset_save_as": "שמירה כהגדרה",
    "meta_preset_update": "עדכון",
    "meta_preset_rename": "שינוי שם",
    "meta_preset_duplicate": "שכפול",
    "meta_preset_delete": "מחיקה",
    "meta_preset_reset_builtins": "איפוס המובנות",
    "meta_preset_name_prompt": "שם ההגדרה",
    "meta_preset_copy_name": "עותק של {name}",
    "meta_preset_delete_confirm": "למחוק את ההגדרה המותאמת „{name}” ?",
    "meta_preset_saved": "ההגדרות המותאמות נשמרו.",
    "meta_preset_store_corrupt": "קובץ ההגדרות המותאמות פגום. ההגדרות המובנות עדיין זמינות.",
    "meta_preset_store_unsupported": "קובץ ההגדרות משתמש בסכימה שאינה נתמכת. ההגדרות המובנות עדיין זמינות.",
    "meta_preset_store_migrated": "הגדרות ישנות נטענו וישודרגו בשמירה הבאה.",
    "meta_preset_builtin_artist_title": "אמן - כותרת",
    "meta_preset_builtin_track_artist_title": "רצועה - אמן - כותרת",
    "meta_preset_builtin_parse_artist_title": "אמן - כותרת לתגיות",
    "meta_field_track_num": "מספר רצועה",
    "meta_field_disc_num": "מספר דיסק",
    "meta_field_year": "שנה",
    "meta_field_filename": "שם קובץ",
    "meta_field_value": "ערך",
}

TRANSLATIONS["en"].update(_TAG_ACTION_TEXTS_EN)
TRANSLATIONS["he"].update(_TAG_ACTION_TEXTS_HE)

# Phase 12 Import / Export. Core carries only stable values and these keys;
# filenames, paths, encodings, URLs, ISRCs and field IDs remain LTR in widgets.
_METADATA_IO_TEXTS_EN = {
    "meta_io_title": "Import / Export",
    "meta_io_toolbar": "Import /\nExport",
    "meta_io_subtitle": "Exchange metadata, reports, playlists, and custom presets through explicit previews.",
    "meta_io_operation_accessible": "Import or export operation",
    "meta_io_export_metadata": "Export Metadata CSV",
    "meta_io_import_metadata": "Import Metadata CSV",
    "meta_io_export_change_report": "Export Change Report",
    "meta_io_export_problems_report": "Export Problems Report",
    "meta_io_export_playlist": "Export Playlist",
    "meta_io_export_presets": "Export Presets",
    "meta_io_import_presets": "Import Presets",
    "meta_io_export_options": "Scope, values, fields, and format",
    "meta_io_preview_destination": "Preview, destination, and export",
    "meta_io_source_format": "Source file, encoding, and delimiter",
    "meta_io_mapping": "Identity and field mapping",
    "meta_io_results": "Dry-run results and pending changes",
    "meta_io_report_options": "Report scope, format, language, and destination",
    "meta_io_playlist_options": "Playlist scope, order, paths, and format",
    "meta_io_preset_package": "Portable custom-preset package",
    "meta_io_scope": "Scope",
    "meta_io_scope_selected": "Selected files",
    "meta_io_scope_visible": "Current visible view",
    "meta_io_scope_changed": "Changed files",
    "meta_io_scope_all": "All loaded files",
    "meta_io_value_source": "Metadata values",
    "meta_io_effective_values": "Effective values (including pending changes)",
    "meta_io_original_values": "Original values on disk",
    "meta_io_encoding": "Encoding",
    "meta_io_delimiter": "Delimiter",
    "meta_io_delimiter_comma": "Comma",
    "meta_io_delimiter_semicolon": "Semicolon",
    "meta_io_delimiter_tab": "Tab",
    "meta_io_include_absolute_paths": "Include absolute paths (privacy-sensitive)",
    "meta_io_absolute_paths_warning": "Privacy warning: absolute paths can reveal your user name and private folder structure. Leave this off for shared reports.",
    "meta_io_destination": "Destination",
    "meta_io_choose_destination": "Choose destination",
    "meta_io_choose_source": "Choose source file",
    "meta_io_overwrite": "Overwrite the existing destination",
    "meta_io_preview": "Preview",
    "meta_io_preview_not_ready": "Choose the options and destination to create the immutable export plan.",
    "meta_io_preview_rows": "{n} rows · {source}",
    "meta_io_export": "Export",
    "meta_io_import": "Import",
    "meta_io_source_file": "Source file",
    "meta_io_auto_detect": "Auto-detect",
    "meta_io_load_headers": "Load Header Preview",
    "meta_io_field_mapping": "CSV column mapping",
    "meta_io_csv_column": "CSV column",
    "meta_io_target": "Identity / metadata target",
    "meta_io_ignore": "Ignore",
    "meta_io_identity_relative": "Identity: root-relative path",
    "meta_io_identity_absolute": "Identity: absolute path (explicit)",
    "meta_io_identity_filename": "Identity: unique filename",
    "meta_io_mapping_for": "Mapping for {column}",
    "meta_io_blank_clears": "Blank cells clear mapped fields (off by default)",
    "meta_io_dry_run": "Run Dry Run",
    "meta_io_select_safe": "Select Safe Changes",
    "meta_io_clear_selection": "Clear Selection",
    "meta_io_dry_run_results": "Metadata import dry-run results",
    "meta_io_selected": "Selected",
    "meta_io_row": "Row",
    "meta_io_field": "Field",
    "meta_io_imported": "Imported value",
    "meta_io_state": "State",
    "meta_io_add_pending": "Add to Pending Changes",
    "meta_io_headers_loaded": "Loaded {n} stable CSV headers. Review identity and field mapping.",
    "meta_io_load_headers_first": "Load the header preview before running the dry run.",
    "meta_io_identity_required": "Choose exactly one identity column.",
    "meta_io_dry_run_ready": "Dry run ready: {changes} safe field changes across {rows} rows.",
    "meta_io_dry_run_first": "Run and review the dry run first.",
    "meta_io_report_format": "Report format",
    "meta_io_language": "Report language",
    "meta_io_include_technical_ids": "Include technical IDs in machine-readable output",
    "meta_io_spreadsheet_safe": "Spreadsheet-safe report CSV",
    "meta_io_order": "Order",
    "meta_io_order_current": "Current Details-view order",
    "meta_io_order_natural": "Natural path order",
    "meta_io_order_track": "Disc and track order",
    "meta_io_path_mode": "Path mode",
    "meta_io_path_auto": "Auto (relative when safe)",
    "meta_io_path_relative": "Relative",
    "meta_io_path_absolute": "Absolute",
    "meta_io_playlist_format": "Playlist format",
    "meta_io_conflict_policy": "Conflict policy",
    "meta_io_conflict_skip": "Skip conflicts",
    "meta_io_conflict_keep_both": "Keep both with a new ID",
    "meta_io_conflict_replace": "Replace the same custom preset",
    "meta_io_conflict_rename": "Import as a renamed custom preset",
    "meta_io_preset_name": "Preset name",
    "meta_io_fields": "Metadata fields", "meta_io_preview_ready": "Preview is ready. Review it before choosing the destination.",
    "meta_io_preview_first": "Create and review a current preview before exporting.",
    "meta_io_result_filter": "Dry-run result filter", "meta_io_filter_all": "All results",
    "meta_io_filter_selected": "Selected only", "meta_io_report_preview_ready": "Report preview contains {n} rows.",
    "meta_report_preview_absolute_on": "Absolute paths are included.",
    "meta_report_preview_absolute_off": "Only the root display name and relative paths are included.",
    "meta_io_playlist_preview_ready": "Playlist preview contains {n} entries and {warnings} warnings.",
    "meta_io_preset_selection": "Custom presets to export", "meta_io_preset_transfer": "Import / Export Presets",
    "meta_io_rename_to": "Rename to", "meta_io_validate_package": "Validate Package",
    "meta_io_accept_preset_import": "Import Valid Presets", "meta_io_preset_preview_ready": "Validated {n} preset entries. Review each conflict before import.",
    "meta_io_details": "Details",
    "meta_io_preset_preview": "Preset transfer validation preview",
    "meta_io_state_initial": "Choose an operation. Nothing starts automatically.",
    "meta_io_state_loading": "Working… You can cancel safely.",
    "meta_io_empty_scope": "This explicit scope contains no files.",
    "meta_io_no_rows": "There are no rows to export for this scope.",
    "meta_io_source_missing": "The selected source file is missing or unavailable.",
    "meta_io_destination_invalid": "Choose a destination inside an existing writable folder.",
    "meta_io_destination_exists": "The destination exists. Enable overwrite explicitly to replace it.",
    "meta_io_export_success": "Export completed safely ({n}).",
    "meta_io_import_success": "Added {fields} field changes for {files} files to Pending Changes.",
    "meta_io_import_failed": "The import preview is stale or the selected changes are no longer valid.",
    "meta_io_preset_import_success": "Imported {n} custom presets; skipped {skipped}.",
    "meta_io_state_matched": "Matched", "meta_io_state_change": "Change",
    "meta_io_state_no_op": "No-op", "meta_io_state_unmatched": "Unmatched",
    "meta_io_state_ambiguous": "Ambiguous target", "meta_io_state_duplicate_target": "Duplicate CSV target",
    "meta_io_state_invalid": "Invalid value", "meta_io_state_unsupported": "Unsupported field",
    "meta_io_state_read_only": "Read-only target", "meta_io_state_stale_identity": "Stale identity",
    "meta_io_state_blocked": "Blocked", "meta_io_state_skipped": "Skipped",
    "meta_io_preset_state_valid": "Valid", "meta_io_preset_state_unknown_action": "Unknown action",
    "meta_io_preset_state_invalid_parameters": "Invalid parameters",
    "meta_io_preset_state_duplicate_package_id": "Duplicate package ID",
    "meta_io_preset_state_existing_custom_conflict": "Existing custom conflict",
    "meta_io_preset_state_builtin_conflict": "Built-in conflict",
    "meta_io_preset_state_unsupported_schema": "Unsupported schema",
    "meta_io_preset_state_skipped": "Skipped",
    "meta_io_error_cancelled": "The operation was cancelled; no partial output was published.",
    "meta_io_error_empty_scope": "The selected scope is empty.",
    "meta_io_error_source_missing": "The source file is missing or unreadable.",
    "meta_io_error_source_changed": "The source file changed after the dry run. Load it again.",
    "meta_io_error_source_too_large": "The source file exceeds the safe import size limit.",
    "meta_io_error_invalid_encoding": "The file cannot be decoded strictly with this encoding.",
    "meta_io_error_invalid_format": "The file structure or quoting is invalid.",
    "meta_io_error_resource_limit": "The file exceeds a safe row, column, or cell limit.",
    "meta_io_error_invalid_mapping": "The identity or field mapping is invalid.",
    "meta_io_error_stale_preview": "The workspace or Pending Changes changed. Run the preview again.",
    "meta_io_error_destination_exists": "The destination already exists.",
    "meta_io_error_destination_invalid": "The destination folder is invalid.",
    "meta_io_error_permission_denied": "Permission to write the destination was denied.",
    "meta_io_error_write_failed": "The destination could not be written safely.",
    "meta_io_error_readback_failed": "The written output failed readback validation.",
    "meta_io_error_unsupported_publication": "This destination cannot safely create a new file without replacing an existing one.",
    "meta_io_error_unsupported_schema": "This transfer schema is not supported.",
    "meta_io_warning_empty_scope": "The selected scope is empty.",
    "meta_io_warning_stale_identity": "File identity evidence is stale.",
    "meta_io_warning_pending_rename": "The playlist uses the current disk filename; export again after Apply.",
    "meta_io_warning_missing_file": "A missing file was excluded from the playlist.",
    "meta_io_warning_absolute_path": "Absolute paths may reveal private folder names.",
    "meta_io_warning_partial": "The operation completed with warnings.",
    "meta_report_change_title": "Pending Change Report",
    "meta_report_problems_title": "Problems Report",
    "meta_report_change_summary": "{files} changed files · {fields} fields · {included} included · {excluded} excluded",
    "meta_report_problems_summary": "{count} current problems",
    "meta_report_context": "Root: {root} · Scope: {scope} · Generated: {generated}",
    "meta_report_scope_all_issues": "All current issues",
    "meta_report_scope_filtered_issues": "Current Problems filters",
    "meta_report_warning_present": "Review warning",
    "meta_report_value_unknown": "Unknown value", "meta_report_value_not_applicable": "Not applicable",
    "meta_report_capability_full": "Fully supported", "meta_report_capability_limited": "Limited support",
    "meta_report_capability_read_only": "Read-only", "meta_report_capability_unsupported": "Unsupported",
    "meta_report_capability_future": "Planned support",
    "meta_report_source_validation": "Validation", "meta_report_source_pending": "Pending changes",
    "meta_report_source_artwork": "Artwork", "meta_report_source_musicbrainz": "MusicBrainz",
    "meta_report_source_cover_art_archive": "Cover Art Archive", "meta_report_source_csv_import": "CSV import",
    "meta_report_source_disk": "Disk metadata", "meta_report_source_online_metadata": "Online metadata",
    "meta_report_field_lyrics": "Lyrics", "meta_report_field_artwork": "Artwork",
    "meta_report_field_replaygain_track_gain": "ReplayGain track gain",
    "meta_report_field_replaygain_track_peak": "ReplayGain track peak",
    "meta_report_field_replaygain_album_gain": "ReplayGain album gain",
    "meta_report_field_replaygain_album_peak": "ReplayGain album peak",
    "meta_report_field_replaygain_reference_loudness": "ReplayGain reference loudness",
    "meta_report_header_item_id": "Technical item ID", "meta_report_header_issue_id": "Technical issue ID",
    "meta_report_header_filename": "Filename", "meta_report_header_relative_path": "Relative path",
    "meta_report_header_absolute_path": "Absolute path",
    "meta_report_header_field": "Field", "meta_report_header_original": "Original value",
    "meta_report_header_previous": "Previous pending value", "meta_report_header_proposed": "Proposed value",
    "meta_report_header_effective": "Effective value", "meta_report_header_operation": "Operation",
    "meta_report_header_origin": "Origin", "meta_report_header_included": "Included in Apply",
    "meta_report_header_capability": "Capability", "meta_report_header_warning": "Warning",
    "meta_report_header_source": "Source", "meta_report_header_source_url": "Source URL",
    "meta_report_header_title": "Title", "meta_report_header_explanation": "Explanation",
    "meta_report_header_severity": "Severity", "meta_report_header_category": "Category",
    "meta_report_header_state": "Issue state", "meta_report_header_fixable": "Fixable",
    "meta_report_header_changed_excluded": "Changed but excluded",
    "meta_report_header_operation_id": "Technical operation value", "meta_report_header_origin_id": "Technical origin value",
    "meta_report_header_field_id": "Technical field value",
    "meta_report_header_capability_id": "Technical capability value", "meta_report_header_source_id": "Technical source value",
    "meta_report_header_severity_id": "Technical severity value", "meta_report_header_category_id": "Technical category value",
    "meta_report_header_state_id": "Technical issue-state value",
    "meta_change_origin_recovery": "Recovered draft", "meta_change_origin_template": "Action template",
    "meta_change_origin_import": "Imported metadata", "meta_problems_category_duplicates": "Duplicates",
    "meta_change_operation_set": "Set", "meta_change_operation_clear": "Clear",
    "meta_change_operation_add": "Add", "meta_change_operation_replace": "Replace",
    "meta_change_operation_remove": "Remove", "meta_change_operation_rename": "Rename",
    "meta_change_operation_move": "Move", "yes": "Yes", "no": "No", "close": "Close",
}

_METADATA_IO_TEXTS_HE = {
    **_METADATA_IO_TEXTS_EN,
    "meta_io_title": "ייבוא / ייצוא", "meta_io_toolbar": "ייבוא /\nייצוא",
    "meta_io_subtitle": "החלפת מטא־דאטה, דוחות, רשימות השמעה והגדרות שמורות מותאמות — רק לאחר תצוגה מקדימה מפורשת.",
    "meta_io_operation_accessible": "פעולת ייבוא או ייצוא",
    "meta_io_export_metadata": "ייצוא מטא־דאטה ל־CSV", "meta_io_import_metadata": "ייבוא מטא־דאטה מ־CSV",
    "meta_io_export_change_report": "ייצוא דוח שינויים", "meta_io_export_problems_report": "ייצוא דוח בעיות",
    "meta_io_export_playlist": "ייצוא רשימת השמעה", "meta_io_export_presets": "ייצוא הגדרות שמורות",
    "meta_io_import_presets": "ייבוא הגדרות שמורות",
    "meta_io_export_options": "טווח, ערכים, שדות ותבנית", "meta_io_preview_destination": "תצוגה מקדימה, יעד וייצוא",
    "meta_io_source_format": "קובץ מקור, קידוד ומפריד", "meta_io_mapping": "מיפוי זהות ושדות",
    "meta_io_results": "תוצאות הרצה יבשה ושינויים ממתינים", "meta_io_report_options": "טווח הדוח, תבנית, שפה ויעד",
    "meta_io_playlist_options": "טווח רשימת ההשמעה, סדר, נתיבים ותבנית", "meta_io_preset_package": "חבילת הגדרות שמורות מותאמות וניידת",
    "meta_io_scope": "טווח", "meta_io_scope_selected": "קבצים שנבחרו", "meta_io_scope_visible": "התצוגה הנוכחית",
    "meta_io_scope_changed": "קבצים ששונו", "meta_io_scope_all": "כל הקבצים שנטענו",
    "meta_io_value_source": "ערכי מטא־דאטה", "meta_io_effective_values": "ערכים בפועל (כולל שינויים ממתינים)",
    "meta_io_original_values": "ערכים מקוריים בדיסק", "meta_io_encoding": "קידוד", "meta_io_delimiter": "מפריד",
    "meta_io_delimiter_comma": "פסיק", "meta_io_delimiter_semicolon": "נקודה־פסיק", "meta_io_delimiter_tab": "טאב",
    "meta_io_include_absolute_paths": "לכלול נתיבים מוחלטים (רגיש לפרטיות)", "meta_io_destination": "יעד",
    "meta_io_absolute_paths_warning": "אזהרת פרטיות: נתיבים מוחלטים עלולים לחשוף את שם המשתמש ומבנה התיקיות הפרטי. מומלץ להשאיר אפשרות זו כבויה בדוחות משותפים.",
    "meta_io_choose_destination": "בחירת יעד", "meta_io_choose_source": "בחירת קובץ מקור",
    "meta_io_overwrite": "להחליף את קובץ היעד הקיים", "meta_io_preview": "תצוגה מקדימה",
    "meta_io_preview_not_ready": "יש לבחור אפשרויות ויעד כדי ליצור תוכנית ייצוא קבועה.",
    "meta_io_preview_rows": "{n} שורות · {source}", "meta_io_export": "ייצוא", "meta_io_import": "ייבוא",
    "meta_io_source_file": "קובץ מקור", "meta_io_auto_detect": "זיהוי אוטומטי", "meta_io_load_headers": "טעינת תצוגת כותרות",
    "meta_io_field_mapping": "מיפוי עמודות CSV", "meta_io_csv_column": "עמודת CSV", "meta_io_target": "זהות / שדה מטא־דאטה",
    "meta_io_ignore": "התעלמות", "meta_io_identity_relative": "זהות: נתיב יחסי לתיקיית הבסיס",
    "meta_io_identity_absolute": "זהות: נתיב מוחלט (במפורש)", "meta_io_identity_filename": "זהות: שם קובץ ייחודי",
    "meta_io_mapping_for": "מיפוי עבור {column}", "meta_io_blank_clears": "תאים ריקים מנקים שדות ממופים (כבוי כברירת מחדל)",
    "meta_io_dry_run": "הרצה יבשה", "meta_io_select_safe": "בחירת שינויים בטוחים", "meta_io_clear_selection": "ניקוי הבחירה",
    "meta_io_dry_run_results": "תוצאות הרצה יבשה של ייבוא מטא־דאטה", "meta_io_selected": "נבחר",
    "meta_io_row": "שורה", "meta_io_field": "שדה", "meta_io_imported": "ערך מיובא", "meta_io_state": "מצב",
    "meta_io_add_pending": "הוספה לשינויים הממתינים", "meta_io_headers_loaded": "נטענו {n} כותרות CSV. יש לבדוק את מיפוי הזהות והשדות.",
    "meta_io_load_headers_first": "יש לטעון תחילה את תצוגת הכותרות.", "meta_io_identity_required": "יש לבחור עמודת זהות אחת בלבד.",
    "meta_io_dry_run_ready": "ההרצה היבשה מוכנה: {changes} שינויי שדה בטוחים ב־{rows} שורות.",
    "meta_io_dry_run_first": "יש להריץ ולבדוק תחילה הרצה יבשה.", "meta_io_report_format": "תבנית הדוח",
    "meta_io_language": "שפת הדוח", "meta_io_include_technical_ids": "לכלול מזהים טכניים בפלט קריא למכונה",
    "meta_io_spreadsheet_safe": "CSV דוח בטוח לגיליון אלקטרוני", "meta_io_order": "סדר",
    "meta_io_order_current": "הסדר הנוכחי בתצוגת הפרטים", "meta_io_order_natural": "סדר נתיבים טבעי",
    "meta_io_order_track": "סדר דיסק ורצועה", "meta_io_path_mode": "מצב נתיב", "meta_io_path_auto": "אוטומטי (יחסי כשבטוח)",
    "meta_io_path_relative": "יחסי", "meta_io_path_absolute": "מוחלט", "meta_io_playlist_format": "תבנית רשימת ההשמעה",
    "meta_io_conflict_policy": "מדיניות התנגשות", "meta_io_conflict_skip": "דילוג על התנגשויות",
    "meta_io_conflict_keep_both": "שמירת שניהם עם מזהה חדש", "meta_io_conflict_replace": "החלפת אותה הגדרה מותאמת",
    "meta_io_conflict_rename": "ייבוא כהגדרה מותאמת בשם אחר", "meta_io_preset_name": "שם ההגדרה",
    "meta_io_fields": "שדות מטא־נתונים", "meta_io_preview_ready": "התצוגה המקדימה מוכנה. יש לבדוק אותה לפני בחירת היעד.",
    "meta_io_preview_first": "יש ליצור ולבדוק תצוגה מקדימה עדכנית לפני הייצוא.",
    "meta_io_result_filter": "מסנן תוצאות ההרצה היבשה", "meta_io_filter_all": "כל התוצאות",
    "meta_io_filter_selected": "נבחרים בלבד", "meta_io_report_preview_ready": "תצוגת הדוח כוללת {n} שורות.",
    "meta_report_preview_absolute_on": "נתיבים מוחלטים כלולים.",
    "meta_report_preview_absolute_off": "כלולים רק שם השורש לתצוגה ונתיבים יחסיים.",
    "meta_io_playlist_preview_ready": "תצוגת רשימת ההשמעה כוללת {n} רשומות ו־{warnings} אזהרות.",
    "meta_io_preset_selection": "הגדרות מותאמות לייצוא", "meta_io_preset_transfer": "ייבוא / ייצוא הגדרות",
    "meta_io_rename_to": "שינוי שם אל", "meta_io_validate_package": "אימות החבילה",
    "meta_io_accept_preset_import": "ייבוא הגדרות תקינות", "meta_io_preset_preview_ready": "אומתו {n} הגדרות. יש לבדוק כל התנגשות לפני הייבוא.",
    "meta_io_details": "פרטים", "meta_io_preset_preview": "תצוגת אימות להעברת הגדרות שמורות",
    "meta_io_state_initial": "יש לבחור פעולה. דבר אינו מתחיל אוטומטית.", "meta_io_state_loading": "מתבצע… ניתן לבטל בבטחה.",
    "meta_io_empty_scope": "אין קבצים בטווח המפורש שנבחר.", "meta_io_no_rows": "אין שורות לייצוא בטווח זה.",
    "meta_io_source_missing": "קובץ המקור חסר או אינו זמין.", "meta_io_destination_invalid": "יש לבחור יעד בתוך תיקייה קיימת שניתן לכתוב אליה.",
    "meta_io_destination_exists": "קובץ היעד קיים. יש לאפשר החלפה במפורש.", "meta_io_export_success": "הייצוא הושלם בבטחה ({n}).",
    "meta_io_import_success": "נוספו {fields} שינויי שדה עבור {files} קבצים לשינויים הממתינים.",
    "meta_io_import_failed": "התצוגה המקדימה מיושנת או שהשינויים שנבחרו אינם תקפים עוד.",
    "meta_io_preset_import_success": "יובאו {n} הגדרות מותאמות; דולגו {skipped}.",
    "meta_io_state_matched": "מותאם", "meta_io_state_change": "שינוי", "meta_io_state_no_op": "ללא שינוי",
    "meta_io_state_unmatched": "ללא התאמה", "meta_io_state_ambiguous": "יעד דו־משמעי",
    "meta_io_state_duplicate_target": "יעד CSV כפול", "meta_io_state_invalid": "ערך לא תקין",
    "meta_io_state_unsupported": "שדה לא נתמך", "meta_io_state_read_only": "יעד לקריאה בלבד",
    "meta_io_state_stale_identity": "זהות מיושנת", "meta_io_state_blocked": "חסום", "meta_io_state_skipped": "דולג",
    "meta_io_preset_state_valid": "תקין", "meta_io_preset_state_unknown_action": "פעולה לא מוכרת",
    "meta_io_preset_state_invalid_parameters": "פרמטרים לא תקינים", "meta_io_preset_state_duplicate_package_id": "מזהה כפול בחבילה",
    "meta_io_preset_state_existing_custom_conflict": "התנגשות עם הגדרה מותאמת קיימת",
    "meta_io_preset_state_builtin_conflict": "התנגשות עם הגדרה מובנית", "meta_io_preset_state_unsupported_schema": "סכימה לא נתמכת",
    "meta_io_preset_state_skipped": "דולג", "meta_io_error_cancelled": "הפעולה בוטלה; לא פורסם קובץ חלקי.",
    "meta_io_error_source_changed": "קובץ המקור השתנה לאחר ההרצה היבשה. יש לטעון אותו מחדש.",
    "meta_io_error_invalid_encoding": "לא ניתן לפענח את הקובץ בקפדנות באמצעות קידוד זה.",
    "meta_io_error_invalid_format": "מבנה הקובץ או סימני הציטוט אינם תקינים.",
    "meta_io_error_resource_limit": "הקובץ חורג ממגבלת שורות, עמודות או גודל תא בטוחה.",
    "meta_io_error_invalid_mapping": "מיפוי הזהות או השדות אינו תקין.",
    "meta_io_error_stale_preview": "סביבת העבודה או השינויים הממתינים השתנו. יש להריץ שוב תצוגה מקדימה.",
    "meta_io_error_empty_scope": "אין קבצים בטווח שנבחר.",
    "meta_io_error_source_missing": "קובץ המקור חסר או אינו זמין.",
    "meta_io_error_source_too_large": "קובץ המקור חורג ממגבלת הגודל הבטוחה.",
    "meta_io_error_destination_exists": "קובץ היעד כבר קיים ונדרשת הסכמה מפורשת להחלפה.",
    "meta_io_error_destination_invalid": "תיקיית היעד חסרה או אינה תקינה.",
    "meta_io_error_unsupported_schema": "סכמת הקובץ אינה נתמכת.",
    "meta_io_error_permission_denied": "ההרשאה לכתיבת קובץ היעד נדחתה.",
    "meta_io_error_write_failed": "לא ניתן לכתוב את קובץ היעד בבטחה.",
    "meta_io_error_readback_failed": "הפלט שנכתב נכשל באימות הקריאה החוזרת.",
    "meta_io_error_unsupported_publication": "ביעד זה לא ניתן ליצור קובץ חדש בבטחה בלי להחליף קובץ קיים.",
    "meta_io_warning_pending_rename": "רשימת ההשמעה משתמשת בשם הקובץ הנוכחי; יש לייצא שוב לאחר ההחלה.",
    "meta_io_warning_missing_file": "קובץ חסר הושמט מרשימת ההשמעה.", "meta_io_warning_absolute_path": "נתיבים מוחלטים עלולים לחשוף שמות תיקיות פרטיים.",
    "meta_io_warning_empty_scope": "הטווח שנבחר ריק.", "meta_io_warning_stale_identity": "פרטי זהות הקובץ אינם עדכניים.",
    "meta_io_warning_partial": "הפעולה הושלמה באופן חלקי; יש לבדוק את הפרטים.",
    "meta_report_change_title": "דוח שינויים ממתינים", "meta_report_problems_title": "דוח בעיות",
    "meta_report_change_summary": "{files} קבצים ששונו · {fields} שדות · {included} כלולים · {excluded} מוחרגים",
    "meta_report_problems_summary": "{count} בעיות נוכחיות", "meta_report_context": "שורש: {root} · טווח: {scope} · הופק: {generated}",
    "meta_report_scope_all_issues": "כל הבעיות הנוכחיות",
    "meta_report_scope_filtered_issues": "המסננים הנוכחיים במרכז הבעיות", "meta_report_warning_present": "אזהרה לבדיקה",
    "meta_report_value_unknown": "ערך לא ידוע", "meta_report_value_not_applicable": "לא רלוונטי",
    "meta_report_capability_full": "תמיכה מלאה", "meta_report_capability_limited": "תמיכה מוגבלת",
    "meta_report_capability_read_only": "קריאה בלבד", "meta_report_capability_unsupported": "לא נתמך",
    "meta_report_capability_future": "תמיכה מתוכננת",
    "meta_report_source_validation": "אימות", "meta_report_source_pending": "שינויים ממתינים",
    "meta_report_source_artwork": "עטיפה", "meta_report_source_musicbrainz": "MusicBrainz",
    "meta_report_source_cover_art_archive": "ארכיון עטיפות", "meta_report_source_csv_import": "ייבוא CSV",
    "meta_report_source_disk": "מטא־דאטה בדיסק", "meta_report_source_online_metadata": "מטא־דאטה מקוון",
    "meta_report_field_lyrics": "מילות השיר", "meta_report_field_artwork": "עטיפה",
    "meta_report_field_replaygain_track_gain": "הגבר ReplayGain לרצועה",
    "meta_report_field_replaygain_track_peak": "שיא ReplayGain לרצועה",
    "meta_report_field_replaygain_album_gain": "הגבר ReplayGain לאלבום",
    "meta_report_field_replaygain_album_peak": "שיא ReplayGain לאלבום",
    "meta_report_field_replaygain_reference_loudness": "עוצמת ייחוס של ReplayGain",
    "meta_report_header_item_id": "מזהה פריט טכני", "meta_report_header_issue_id": "מזהה בעיה טכני",
    "meta_report_header_filename": "שם קובץ", "meta_report_header_relative_path": "נתיב יחסי",
    "meta_report_header_absolute_path": "נתיב מוחלט", "meta_report_header_field": "שדה",
    "meta_report_header_original": "ערך מקורי", "meta_report_header_previous": "ערך ממתין קודם",
    "meta_report_header_proposed": "ערך מוצע", "meta_report_header_effective": "ערך בפועל",
    "meta_report_header_operation": "פעולה", "meta_report_header_origin": "מקור", "meta_report_header_included": "כלול בהחלה",
    "meta_report_header_warning": "אזהרה", "meta_report_header_source": "מקור", "meta_report_header_source_url": "כתובת המקור",
    "meta_report_header_title": "כותרת", "meta_report_header_explanation": "הסבר", "meta_report_header_severity": "חומרה",
    "meta_report_header_category": "קטגוריה", "meta_report_header_state": "מצב הבעיה", "meta_report_header_fixable": "ניתן לתיקון",
    "meta_report_header_changed_excluded": "שונה אך מוחרג", "meta_change_operation_set": "הגדרה",
    "meta_report_header_operation_id": "ערך פעולה טכני", "meta_report_header_origin_id": "ערך מקור טכני",
    "meta_report_header_field_id": "ערך שדה טכני",
    "meta_report_header_capability_id": "ערך יכולת טכני", "meta_report_header_source_id": "ערך מקור טכני",
    "meta_report_header_severity_id": "ערך חומרה טכני", "meta_report_header_category_id": "ערך קטגוריה טכני",
    "meta_report_header_state_id": "ערך מצב בעיה טכני",
    "meta_change_origin_recovery": "טיוטה ששוחזרה", "meta_change_origin_template": "תבנית פעולה",
    "meta_change_origin_import": "מטא־דאטה מיובא", "meta_problems_category_duplicates": "כפילויות",
    "meta_change_operation_clear": "ניקוי", "meta_change_operation_add": "הוספה", "meta_change_operation_replace": "החלפה",
    "meta_change_operation_remove": "הסרה", "meta_change_operation_rename": "שינוי שם", "meta_change_operation_move": "העברה",
    "yes": "כן", "no": "לא", "close": "סגירה",
}

_FILESYSTEM_MONITORING_TEXTS_EN = {
    "meta_monitoring_status": "Filesystem monitoring status",
    "meta_monitoring_status_tooltip": "External file changes are validated before the workspace is updated.",
    "meta_monitoring_diagnostic": "Monitoring detail: {detail}",
    "meta_monitoring_disabled": "Monitoring off",
    "meta_monitoring_active": "Monitoring active",
    "meta_monitoring_refreshing": "Refreshing files",
    "meta_monitoring_paused": "Monitoring paused",
    "meta_monitoring_degraded": "Monitoring degraded",
    "meta_monitoring_root_lost": "Folder unavailable",
    "meta_manual_refresh": "Refresh files",
    "meta_external_filter": "External changes ({n})",
    "meta_external_state_current": "Current",
    "meta_external_state_refreshing": "Refreshing",
    "meta_external_state_changed_on_disk": "Changed on disk",
    "meta_external_state_stale_with_proposals": "Stale with local proposals",
    "meta_external_state_missing": "File was removed",
    "meta_external_state_moved": "File moved externally",
    "meta_external_state_replaced": "Replaced by a different file",
    "meta_external_state_unreadable": "File is unreadable",
    "meta_external_state_cloud_placeholder": "Cloud placeholder",
    "meta_external_state_conflict": "External-change conflict",
    "meta_external_state_ignored_own_operation": "Validated application change",
    "meta_external_state_root_unavailable": "Folder temporarily unavailable",
    "meta_external_inspector_status": "External state: {state}",
    "meta_external_multiple_states": "The selection contains {n} external states.",
    "meta_external_review_action": "Review differences",
    "meta_external_review_title": "Review external file changes",
    "meta_external_review_body": "Compare the previous baseline, current disk values, and pending local values for {path}.",
    "meta_external_safe_rebase": "Safe rebase: local and external changes do not overlap.",
    "meta_external_unsafe_rebase": "Conflict: at least one field changed both locally and externally.",
    "meta_external_differences_table": "External change field differences",
    "meta_external_differences_table_desc": "Each row compares the previous value, the value now on disk, and your pending value, and states whether they overlap.",
    "meta_external_field": "Field",
    "meta_external_previous": "Previous baseline",
    "meta_external_disk": "Current disk value",
    "meta_external_local": "Pending local value",
    "meta_external_overlap": "Overlap",
    "meta_external_reload": "Reload from disk",
    "meta_external_keep_local": "Keep local proposals",
    "meta_external_remove_missing": "Remove missing file",
    "meta_external_locate_moved": "Locate moved file",
    "meta_external_resolution_failed_title": "The conflict was not resolved",
    "meta_external_resolution_failed": "The reviewed evidence is no longer current ({detail}). Refresh and review again.",
    "meta_external_apply_blocked": "{n} changed files have unresolved external changes. Review them before Apply.",
    "meta_external_review_blocker": "External state blocks Apply: {state}",
    "meta_problem_external_change": "External file change",
    "meta_problem_external_change_body": "This file changed outside the Tag Editor and requires review or refresh.",
    "meta_audio_files": "Audio files",
}

_FILESYSTEM_MONITORING_TEXTS_HE = {
    "meta_monitoring_status": "מצב ניטור מערכת הקבצים",
    "meta_monitoring_status_tooltip": "שינויים חיצוניים בקבצים מאומתים לפני עדכון סביבת העבודה.",
    "meta_monitoring_diagnostic": "פרטי ניטור: {detail}",
    "meta_monitoring_disabled": "הניטור כבוי",
    "meta_monitoring_active": "הניטור פעיל",
    "meta_monitoring_refreshing": "הקבצים מתרעננים",
    "meta_monitoring_paused": "הניטור מושהה",
    "meta_monitoring_degraded": "הניטור מוגבל",
    "meta_monitoring_root_lost": "התיקייה אינה זמינה",
    "meta_manual_refresh": "רענון קבצים",
    "meta_external_filter": "שינויים חיצוניים ({n})",
    "meta_external_state_current": "עדכני",
    "meta_external_state_refreshing": "מתרענן",
    "meta_external_state_changed_on_disk": "השתנה בדיסק",
    "meta_external_state_stale_with_proposals": "מיושן עם הצעות מקומיות",
    "meta_external_state_missing": "הקובץ הוסר",
    "meta_external_state_moved": "הקובץ הועבר מחוץ ליישום",
    "meta_external_state_replaced": "הוחלף בקובץ אחר",
    "meta_external_state_unreadable": "לא ניתן לקרוא את הקובץ",
    "meta_external_state_cloud_placeholder": "מציין מיקום בענן",
    "meta_external_state_conflict": "התנגשות עם שינוי חיצוני",
    "meta_external_state_ignored_own_operation": "שינוי של היישום אומת",
    "meta_external_state_root_unavailable": "התיקייה אינה זמינה זמנית",
    "meta_external_inspector_status": "מצב חיצוני: {state}",
    "meta_external_multiple_states": "הבחירה כוללת {n} מצבים חיצוניים.",
    "meta_external_review_action": "בדיקת ההבדלים",
    "meta_external_review_title": "בדיקת שינויים חיצוניים בקובץ",
    "meta_external_review_body": "השוואה בין ערכי הבסיס הקודמים, ערכי הדיסק הנוכחיים והערכים המקומיים הממתינים עבור {path}.",
    "meta_external_safe_rebase": "ניתן לבסס מחדש בבטחה: השינויים המקומיים והחיצוניים אינם חופפים.",
    "meta_external_unsafe_rebase": "התנגשות: לפחות שדה אחד השתנה גם מקומית וגם חיצונית.",
    "meta_external_differences_table": "הבדלי שדות בעקבות שינוי חיצוני",
    "meta_external_differences_table_desc": "כל שורה משווה בין הערך הקודם, הערך שנמצא כעת בדיסק והערך הממתין שלך, ומציינת אם יש ביניהם התנגשות.",
    "meta_external_field": "שדה",
    "meta_external_previous": "בסיס קודם",
    "meta_external_disk": "ערך נוכחי בדיסק",
    "meta_external_local": "ערך מקומי ממתין",
    "meta_external_overlap": "חפיפה",
    "meta_external_reload": "טעינה מחדש מהדיסק",
    "meta_external_keep_local": "שמירת ההצעות המקומיות",
    "meta_external_remove_missing": "הסרת הקובץ החסר",
    "meta_external_locate_moved": "איתור הקובץ שהועבר",
    "meta_external_resolution_failed_title": "ההתנגשות לא נפתרה",
    "meta_external_resolution_failed": "המידע שנבדק אינו עדכני עוד ({detail}). יש לרענן ולבדוק שוב.",
    "meta_external_apply_blocked": "ל־{n} קבצים ששונו יש שינויים חיצוניים שלא נפתרו. יש לבדוק אותם לפני ההחלה.",
    "meta_external_review_blocker": "מצב חיצוני חוסם את ההחלה: {state}",
    "meta_problem_external_change": "שינוי חיצוני בקובץ",
    "meta_problem_external_change_body": "הקובץ השתנה מחוץ לעורך התגיות ונדרשת בדיקה או רענון.",
    "meta_audio_files": "קובצי שמע",
}

TRANSLATIONS["en"].update(_METADATA_IO_TEXTS_EN)
TRANSLATIONS["he"].update(_METADATA_IO_TEXTS_HE)
TRANSLATIONS["en"].update(_FILESYSTEM_MONITORING_TEXTS_EN)
TRANSLATIONS["he"].update(_FILESYSTEM_MONITORING_TEXTS_HE)


def render_preflight_warnings(warnings) -> str:
    """Render startup preflight warnings in the active UI language.

    ``warnings`` is a list of error_handler.PreflightWarning (or anything
    exposing ``.key``/``.params``) — duck-typed so this module never
    needs to import error_handler. Joined the same way
    PreflightResult.warning_text() joins the canonical English originals.
    """
    return "\n\n".join(t(warning.key, **warning.params) for warning in warnings)


def set_language(lang: str) -> None:
    """Set the active language code (falls back to English).

    This only updates the in-memory translation state. Use
    :func:`apply_language` instead when you also need to update the
    application's layout direction and notify language-aware widgets.
    """
    global _current
    if lang not in TRANSLATIONS:
        lang = "en"
    _current = lang


def current_language() -> str:
    return _current


def _warn_missing(key: str, lang: str, also_missing_in_en: bool = False) -> None:
    """Log a translation key that's not present in the active language.

    Each (key, lang) pair is logged at most once per session to avoid
    flooding the log from paint events. Uses DEBUG level so it's silent
    in normal use but visible when devs raise the log level.
    """
    marker = (key, lang)
    if marker in _warned_keys:
        return
    _warned_keys.add(marker)
    if also_missing_in_en:
        _log.debug("i18n: key %r missing in %r and in English fallback", key, lang)
    else:
        _log.debug("i18n: key %r missing in %r (using English fallback)", key, lang)


def t(key: str, **kwargs) -> str:
    """Translate `key` using the active language and format with kwargs."""
    d = TRANSLATIONS.get(_current, TRANSLATIONS["en"])
    if key in d:
        s = d[key]
    elif key in TRANSLATIONS["en"]:
        s = TRANSLATIONS["en"][key]
        if _current != "en":
            _warn_missing(key, _current)
    else:
        _warn_missing(key, _current, also_missing_in_en=True)
        s = key
    try:
        return s.format(**kwargs)
    except Exception:
        return s


# ─── Language coordinator ────────────────────────────────────────────────────
#
# The LanguageManager singleton exposes a ``language_changed(str)`` Qt signal
# that widgets can connect to in order to live-update their text when the
# user changes language. Today the application uses restart-based language
# switching (see ``request_language_restart`` below), so no widget connects
# to the signal — but the plumbing is in place so a future live-retranslate
# phase can wire each widget's ``_retranslate()`` method without an
# architectural change.

_language_manager: Optional["LanguageManager"] = None


def language_manager() -> "LanguageManager":
    """Return the singleton LanguageManager, creating it on first call.

    Requires a QApplication to exist. Import-deferred so plain ``import ui.i18n``
    does not pull in Qt symbols.
    """
    global _language_manager
    if _language_manager is None:
        # Local import: keeps Qt out of the import path for non-UI consumers.
        from PySide6.QtCore import QObject, Signal

        class LanguageManager(QObject):
            language_changed = Signal(str)

        _language_manager = LanguageManager()
    return _language_manager


def apply_language(app, lang: str) -> None:
    """Apply ``lang`` as the active language + layout direction in one call.

    Use at startup (after QApplication is constructed) and whenever the user
    changes language in Settings. ``app`` must be a ``QApplication`` instance.
    """
    set_language(lang)
    # Local imports keep this module importable without Qt for tooling.
    from ui.direction import apply_app_direction
    apply_app_direction(app, _current)
    language_manager().language_changed.emit(_current)


# Map the Hebrew category labels used internally (matching YouTube's Hebrew UI)
# to translation keys. Used by ``localized_folder_name`` to produce the correct
# folder-name string when writing scraped channel content to disk.
_FOLDER_KEY_MAP: Dict[str, str] = {
    "סרטונים":              "folder_videos",
    "קצרים":                "folder_shorts",
    "שידורים חיים":         "folder_live",
    "פלייליסטים":           "folder_playlists",
    "פריטי תוכן":           "folder_releases",
    "פודקאסטים":            "folder_podcasts",
    "סינגלים ומיני אלבומים": "folder_singles_eps",
    "סינגלים ו-EP":         "folder_singles_eps",
    "סינגלים וגרסאות EP":   "folder_singles_eps",
    "אלבומים":              "folder_albums",
    "הופעות חיות":          "folder_live_performances",
}


def localized_folder_name(category: str) -> str:
    """Translate an internal Hebrew category label to the current language.

    Internal scraping/identification code uses Hebrew labels because they
    match YouTube's Hebrew UI. When those labels are written out as folder
    names, they should be in the user's selected language. Returns the
    original ``category`` unchanged if no mapping is registered.
    """
    key = _FOLDER_KEY_MAP.get(category)
    return t(key) if key else category


def request_language_restart(app, lang: str) -> None:
    """Persist ``lang`` to in-memory state then restart the app process.

    The new process re-reads ``cfg.language`` at startup and renders the
    entire UI cleanly in the new language with the correct RTL direction
    from frame one — no partial-refresh edge cases.
    """
    apply_language(app, lang)
    from PySide6.QtCore import QProcess

    program = sys.executable
    # Re-launch with the same argv. When frozen (PyInstaller), sys.argv[0]
    # is the bundled executable; sys.executable points to the same binary,
    # so passing sys.argv[1:] as arguments preserves any user-supplied flags.
    arguments = sys.argv[1:] if getattr(sys, "frozen", False) else list(sys.argv)
    workdir = os.getcwd()
    QProcess.startDetached(program, arguments, workdir)
    app.quit()
