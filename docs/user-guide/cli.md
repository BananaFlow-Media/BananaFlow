# BananaFlow CLI reference

Status: **Current / normative user reference**

The packaged/source CLI uses the same backend engines as the GUI without requiring Qt UI interaction.

## Entry points

From a source checkout:

```bash
python cli.py --help
```

After package installation:

```bash
bananaflow-cli --help
```

## Common operations

```bash
# Inspect environment/reliability components
bananaflow-cli --doctor

# Show version
bananaflow-cli --version

# List content without downloading
bananaflow-cli URL --list

# Download audio
bananaflow-cli URL --media-type audio --audio-format mp3

# Download video with a quality preset
bananaflow-cli URL --media-type video --quality video_1080
```

The CLI's `--help` output is authoritative for the exact option set and accepted values. When a CLI flag/default/meaning changes, update this file and the relevant user-manual examples in the same PR.

## Output and scripting

List-mode output is designed to remain useful for piping. Progress/diagnostic output should not silently corrupt machine-consumed standard output. Use `--quiet` for reduced progress output and `--debug` when collecting troubleshooting evidence.

## Authentication

When an operation genuinely requires authenticated YouTube access, provide the documented cookie/sign-in mechanism. Treat cookie files as credentials: do not paste their values into issues or logs. `--doctor` reports readiness without exposing cookie values.

## Spotify

Spotify URLs are metadata/resolution inputs; BananaFlow does not download Spotify audio streams. Spotify **search** is a separate feature that can use the optional proxy API documented in [`spotify-proxy-api.md`](spotify-proxy-api.md).

## Full option reference

Do not duplicate a hand-maintained exhaustive parser table here: run `bananaflow-cli --help`. This prevents the reference from drifting when argparse changes. The user manual explains stable concepts/presets and examples.
