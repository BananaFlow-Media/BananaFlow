# Metadata Explorer manual verification matrix

Status: **Reusable manual QA checklist**

This matrix compares the Tag Editor Explorer-like center pane against its intended Windows File Explorer-style behavior. It is not a claim of complete Windows Explorer parity. Record the tested BananaFlow version/commit, Windows version, DPI/theme/language and date in PR/release evidence when executed.

See [`qa/README.md`](qa/README.md) for manual-QA rules.

## Disposable fixture folder

Create a temporary music folder containing:

- English, Hebrew and mixed-direction **supported audio** filenames;
- a very long filename;
- at least one **unsupported** or read-only format relevant to the current product;
- a subfolder with a supported file;
- disposable collision targets for move/rename checks.

Never perform destructive QA on a personal library.

## Verification matrix

| Area | States/actions | Expected evidence |
|---|---|---|
| Empty | **empty** / no scan / empty folder | clear empty state; no stale selection |
| Small list | exactly **1 row** and a 2–3 row folder | geometry and single-row selection remain correct |
| Large list | **1,000+ rows** | scrolling, virtualization/refresh and selection remain usable |
| Selection | click, **Ctrl+Click**, **Shift+Click**, **Ctrl+A**, **Escape** | current/selected state matches documented behavior |
| Focus | active window and **inactive selection** | focus/selection remains distinguishable without color-only meaning |
| Hover | row hover and **checkbox hover** | hover affordance is visible and does not change selection by itself |
| Keyboard | **F2**, Enter, **Delete**, **Shift+Delete**, Space, Menu/Shift+F10 as supported | actions route through safe product flows and confirmations |
| Header | resize, **double-click best fit**, reorder, sort and **context menu** | order/visibility/width persistence behaves correctly |
| Icons/status | **supported audio**, unsupported/read-only/error/folder | appropriate icon/text semantics |
| **drag/drop** | valid move plus invalid/collision/descendant targets | valid operations only; unsafe targets refused |
| **accessibility** | keyboard + screen-reader names | tree/table/header/zoom/action controls expose semantics |
| **RTL/LTR** | Hebrew UI + mixed filenames/paths | shell layout RTL; technical filename/path editing remains readable |
| Theme | **dark**, **light**, accent | selection/focus/hover remain visible |
| **DPI** | 100/125/150/200% | no clipped rows/header/checkbox/text |
| **high contrast** | Windows high-contrast/accessibility mode | state is not color-only |
| Touch | scroll/tap/press-and-hold/selection/zoom where supported | touch does not trigger destructive action accidentally |

## GUI safety workflow

When visual verification is driven by automation or a remote-control tool, preserve this exact safety order:

1. `list_windows` and identify the BananaFlow window from title/process/context.
2. **activate only** the clearly identified BananaFlow window; do not click through by guessed coordinates.
3. Capture **screenshot/OCR** evidence only after the correct window is active.
4. **If the app window is not clearly identified, stop.** Do not continue with destructive or state-changing input.

This workflow is intentionally explicit because the Explorer checks include Delete/Shift+Delete, drag/drop and rename paths.

## Automated coverage to run first

Use the focused Explorer/metadata/i18n/accessibility tests relevant to the changed area, then the full isolated gate. `docs/testing/TESTING.md` is the source of truth for commands.

## Screenshot evidence

Store screenshots outside tracked source or in an explicit throwaway CI/PR artifact location. Name them with state/theme/DPI/language so reviewers can identify what was actually verified.
