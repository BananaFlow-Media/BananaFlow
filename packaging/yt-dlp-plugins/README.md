# Bundled yt-dlp plugins

This directory is the build-time staging slot for yt-dlp plugins that must
ship inside the packaged BananaFlow build.

For the public Windows package, `scripts/build_windows.ps1` installs the
pinned `bgutil-ytdlp-pot-provider==1.3.1` package and then runs
`python packaging/stage_pot_provider.py`. That script copies the provider
plugin source into this folder in yt-dlp's expected plugin layout:

```text
packaging/yt-dlp-plugins/bgutil-ytdlp-pot-provider/
└── yt_dlp_plugins/extractor/
    ├── getpot_bgutil.py
    └── getpot_bgutil_script.py
```

The HTTP provider module is intentionally not staged for the public
package. BananaFlow uses the provider's Deno script mode so the default build
does not probe `http://127.0.0.1:4416/ping` or require a local server.

The same staging script also downloads the matching upstream source archive
and prepares the Deno script backend under `packaging/pot-provider-backend/`.
PyInstaller bundles both folders. At runtime, BananaFlow activates the bundled
Deno runtime and passes yt-dlp the provider's official extractor argument:

```text
youtubepot-bgutilscript:server_home=<bundled bgutil server/>
```

BananaFlow does not generate, scrape, store, or inject PO Tokens. yt-dlp obtains
tokens through its official PO Token Provider mechanism.

Only this README is tracked. Staged plugin files are regenerated for release
builds and remain gitignored. Keep `THIRD_PARTY_NOTICES.md`,
`SOURCE_OFFER.md`, and the release checklist in sync with the exact staged
provider version.
