# PO Token Provider backend staging

This directory is a build-time staging slot for the upstream
`bgutil-ytdlp-pot-provider` server/script backend.

`packaging/stage_pot_provider.py` downloads the matching upstream source
archive, copies the `server/` tree here, and uses build-time npm to install
production `node_modules` from upstream `package-lock.json`. PyInstaller then
bundles this folder as `pot-provider-backend/` next to the app's internal
runtime files. Normal packaged users do not need Node or npm; BananaFlow uses the
bundled Deno runtime when yt-dlp invokes the script provider.

Only this README is tracked. The staged provider source, `node_modules/`,
and download/cache files are regenerated for release builds and remain
gitignored. Keep `THIRD_PARTY_NOTICES.md` and `SOURCE_OFFER.md` in sync
with the exact provider version staged for a release.
