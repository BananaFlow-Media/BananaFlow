# BananaFlow package/runtime profile — historical snapshot

Status: **Historical measurement evidence**  
Measurement date: 2026-07-17  
Measured build: pre-release `0.2.0` packaging architecture

These numbers informed the browser-packaging ADR. They are intentionally retained for engineering history and **must not be presented as current Stable package sizes or current performance measurements**. Re-measure a current release before making a new size/performance claim.

## Package-size snapshot

| Artifact | Exact bytes | MiB |
|---|---:|---:|
| Portable folder | 1,503,279,008 | 1,433.9 |
| Portable ZIP | 698,434,614 | 666.1 |
| Inno Setup installer | 451,431,218 | 430.5 |

## `_internal` component snapshot

| Component | MiB | Share at the time |
|---|---:|---:|
| Chromium variants | 688.4 | 49.6% |
| FFmpeg payload | 192.3 | 13.9% |
| PySide6 / Qt | 113.7 | 8.2% |
| Playwright Python/driver | 100.3 | 7.2% |
| Deno runtime | 94.6 | 6.8% |
| bgutil provider backend | 80.4 | 5.8% |
| numpy/scipy native libs | 45.3 | 3.3% |
| Python runtime | 15.1 | 1.1% |
| Other | 56.9 | 4.1% |

The Chromium payload contained both a headed-capable full browser (needed by Cookie Wizard) and a headless shell for scraping/extraction paths. Playwright's small screen-recording FFmpeg payload was unused by BananaFlow media logic and was noted as non-material trim potential.

## Startup/idle snapshot

Measured time to a valid Qt main-window handle:

- cold: ~11.9 s;
- warm: ~8.9 s.

Idle process state after launch was one application process at roughly 242 MB working set. Chromium was not launched at startup, so the browser's disk footprint was not the same thing as an idle-memory/startup cost.

## Browser-session snapshot

Representative transient browser trees measured at the time:

| Scenario | Additional process/RAM observation |
|---|---|
| light headless page | ~4 processes / 184 MB |
| YouTube channel headless page | ~4 / 248 MB |
| Spotify headless page | ~4 / 549 MB |
| headed persistent Cookie Wizard-style session | ~9 / 650 MB |

These numbers varied by page/content and were released after the browser/context closed.

## Engineering conclusion at the time

Chromium was the largest installed-size lever but not an idle-runtime cost. Several product features genuinely needed browser JavaScript/DOM execution, so the project retained bundled Chromium and pursued cheaper non-browser probes only where they could preserve correctness with browser fallback.

See [`../architecture/browser-component-decision.md`](../architecture/browser-component-decision.md).

## How to use this file

Use it for historical comparison and decision rationale only. A future optimization proposal should publish a new dated profile against the exact current release artifact/hardware/methodology rather than editing these old numbers until they look current.
