# Manual QA Checklist — YouTube Doctor (Settings UI)

This exists because the automated test suite verifies the "Run YouTube
Doctor" button and its dialog headlessly (`QT_QPA_PLATFORM=offscreen`,
see `tests/test_youtube_doctor_gui.py`), but nobody has confirmed it
*renders correctly on a real screen* yet. Run this once on a normal
desktop session before considering the feature done.

## 1. Open the app

```powershell
cd path\to\bananaflow
python main.py
```

The main window should appear within a few seconds. If it doesn't
appear at all (no window, no error), stop here and capture:
- Full console output
- `%TEMP%` for any `bananaflow*.log` file

## 2. Navigate to the new card

1. Open **Settings** (left navigation rail).
2. Scroll down. You should see, in this order: Appearance → Downloads
   → Playlist Behaviour → Features → System Integration → Advanced
   Audio Processing → **Authentication** → **Diagnostics** → Search →
   About.
3. The **Diagnostics** group should contain exactly one card:
   **"YouTube Doctor"**, with a **"Run"** button, and a one-line
   description mentioning yt-dlp/JS runtime/cookies/PO Token Provider.

**Failure looks like:** the group is missing entirely, is in the wrong
position, the button label/title is a raw i18n key (e.g. literally
`youtube_doctor_run_btn` instead of "Run"), or the card overlaps/clips
neighboring cards.

## 3. Run the Doctor

1. Click **Run** on the YouTube Doctor card.
2. A dialog titled **"YouTube Doctor"** should open, centered on the
   main window, with:
   - A subtitle line explaining it's a local diagnostic check.
   - Six check lines, each starting with a status icon (✅ / ⚠ / ❌)
     and a label: *yt-dlp version*, *yt-dlp-ejs*, *JavaScript runtime*,
     *Cookies*, *PO Token Provider*, *YouTube reliability mode*.
   - A summary block: *Ready for public YouTube downloads*, *Cookies
     available for gated videos*, *PO Token Provider ready* — each
     followed by Yes / Maybe / No.
   - A *Recommended actions* list (present if any check is ⚠/❌ — on a
     broken packaged build with the bundled provider stack missing or
     unhealthy, expect at least one line here).
   - A single **OK** button that closes the dialog.
3. All text should be fully visible — no line cut off at the dialog's
   right/bottom edge, no horizontal scrollbar, no overlapping text.
   If a line looks clipped, try resizing the dialog before concluding
   it's broken (long yt-dlp version/JS runtime lines can wrap).

**Failure looks like:** dialog doesn't open at all: blank/empty dialog;
any text overlapping or cut off *after* resizing; the window is
unstyled (wrong colors / doesn't match the app's current theme); the
app crashes or freezes.

## 4. Confirm no cookie values ever appear

1. In **Settings → Authentication**, configure a real `cookies.txt`
   file (export one from your browser, or use any Netscape-format
   file with a `LOGIN_INFO` or `SID` entry).
2. Re-run YouTube Doctor (step 3).
3. Read the **Cookies** line and the whole dialog carefully.

**Pass:** the Cookies line says something like "cookies appear
present" / "login cookies appear present" / mentions the cookie
*file's name* at most (e.g. `cookies.txt`) — never a domain+name+value
triplet, never the raw cookie string, never the full absolute file
path (e.g. no `C:\Users\<you>\...`).

**Failure:** any cookie *value* string appears anywhere in the dialog,
or the full file path (with your username in it) is shown.

## 5. Confirm both languages work

1. In **Settings → Appearance**, switch language to **Hebrew**.
2. Repeat steps 2–3.

**Pass:** the Diagnostics group title, card title/description, button
label, and dialog contents are all in Hebrew (except deliberately
untranslated technical terms: "YouTube Doctor", "yt-dlp", "PO Token
Provider", "Deno", "Node", "QuickJS" — these are expected to stay in
English/Latin script, matching how "SponsorBlock"/"MusicBrainz" are
handled elsewhere in the app). Layout should be right-to-left; no
i18n key should appear as raw text (e.g. `youtube_doctor_cat_cookies`).

3. Switch language back to English before finishing.

## 6. If something is wrong

Capture and share:
- A screenshot of the broken state (Settings panel and/or the dialog).
- Full console output from launch to the point of failure.
- `%APPDATA%\.bananaflow\config.json` (redact `cookies_file`/`spotify_client_secret` values if sharing outside your machine).
- The exact step number above where it diverged from "Pass".
