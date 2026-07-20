# Bundled JavaScript runtime (Deno)

This folder ships a standalone **JavaScript runtime** binary *inside* the
packaged BananaFlow EXE. yt-dlp needs a JS runtime to run YouTube's player
logic (via yt-dlp-ejs) and a PO Token Provider needs one to generate
tokens. Bundling one means a clean Windows machine with no Node/Deno
installed still gets reliable downloads out of the box.

`packaging/bananaflow.spec` copies everything here into a `runtime` folder
next to `bananaflow.exe`. `core.runtime_components.activate_bundled_components`
prepends that folder to `PATH` at startup, so yt-dlp's runtime detection
(`utils.yt_dlp_opts._detect_js_runtimes`) selects it automatically —
before it, the same machine's own system PATH is used, exactly as before
this feature existed.

## Staging it

Everything under this folder *except this README* is gitignored — a
~90 MB binary has no business in git history, exactly like
`packaging/ffmpeg/`. Stage it with:

```powershell
pwsh scripts\fetch_deno_runtime.ps1
```

This downloads the official Deno release zip for win-x64 from
`github.com/denoland/deno/releases`, **verifies its published SHA-256
checksum** before extracting, and copies `deno.exe` here. Re-run it
whenever you want to bump the bundled Deno version (`-Version` param).

`scripts/build_windows.ps1` now requires `deno.exe` for the public
Windows package because the bundled bgutil PO Token Provider stack uses
the official Deno script mode. If `deno.exe` is missing, the build fails
with a clear message pointing at this script.

## Required layout

```
packaging/runtime/
└── deno.exe          (Windows)
```

The name must match yt-dlp's expected `deno` executable. Deno is a single
self-contained MIT-licensed binary and is the runtime used by the bundled
bgutil script backend.

## Before shipping

- Deno's license (MIT) is covered in `THIRD_PARTY_NOTICES.md`; re-verify
  the version and checksum before each binary release. If you bundle
  Node instead, add Node's full license bundle and notices first.
- Confirm the binary is the correct OS/architecture for the build
  (`fetch_deno_runtime.ps1` only targets win-x64 today).
- Run `bananaflow-cli --doctor` in the built EXE and confirm the JS runtime
  check reports **deno — bundled with BananaFlow** and the PO Token Provider
  check reports the full stack ready.
