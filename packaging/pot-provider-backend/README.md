# PO Token Provider backend staging

This directory is a build-time staging slot for the upstream
`bgutil-ytdlp-pot-provider` server/script backend.

`packaging/stage_pot_provider.py` downloads the matching upstream source
archive, copies the `server/` tree here, and uses build-time npm to install
production `node_modules` from upstream `package-lock.json`. On Windows,
PyInstaller bundles the upstream provider and its license under `pot-provider/`
inside the active runtime directory. The shorter path gives Inno Setup room for
deeply nested modules during a maintenance upgrade. macOS retains the
`pot-provider-backend/` layout. BananaFlow uses Deno when yt-dlp invokes the
script provider.

Only this README is tracked. The staged provider source, `node_modules/`,
and download/cache files are regenerated for release builds and remain
gitignored. Keep `THIRD_PARTY_NOTICES.md` and `SOURCE_OFFER.md` in sync
with the exact provider version staged for a release.
