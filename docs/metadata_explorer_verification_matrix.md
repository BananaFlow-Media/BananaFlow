# Metadata Explorer manual verification matrix

Status: **Reusable manual QA checklist**

This matrix compares the Tag Editor Explorer-like center pane against its intended Windows File Explorer-style behavior. It is not a claim of complete Windows Explorer parity. Record the tested BananaFlow version/commit, Windows version, DPI/theme/language and date in PR/release evidence when executed.

See [`qa/README.md`](qa/README.md) for manual-QA rules.

## Disposable fixture folder

Create a temporary music folder containing:

- English, Hebrew and mixed-direction supported audio filenames;
- a very long filename;
- at least one unsupported/read-only format relevant to the current product;
- a subfolder with a supported file;
- disposable collision targets for move/rename checks.

Never perform destructive QA on a personal library.

## Verification matrix

| Area | States/actions | Expected evidence |
|---|---|---|
| Empty | no scan / empty folder | clear empty state; no stale selection |
| Small/large lists | 1–3 rows and 1,000+ rows | geometry remains stable; scrolling stays usable |
| Selection | click, Ctrl/Shift selection, Ctrl+A, Escape | current/selected state matches documented behavior |
| Focus | active/inactive window | focus/selection remains distinguishable without color-only meaning |
| Keyboard | F2, Enter, Delete, Space, Menu/Shift+F10 as supported | actions route through safe product flows |
| Header | resize/best-fit/reorder/sort/context | order/visibility/width persistence behaves correctly |
| Icons/status | supported/read-only/error/folder | appropriate icon/text semantics |
| Drag/drop | valid move plus invalid/collision/descendant targets | valid operations only; unsafe targets refused |
| Accessibility | keyboard + screen-reader names | tree/table/header/zoom/action controls expose semantics |
| RTL/LTR | Hebrew UI + mixed filenames/paths | shell layout RTL; technical filename/path editing remains readable |
| Theme | light/dark/accent | selection/focus/hover remain visible |
| DPI | 100/125/150/200% | no clipped rows/header/checkbox/text |
| High contrast | Windows high-contrast/accessibility mode | state is not color-only |
| Touch | scroll/tap/press-and-hold/selection/zoom where supported | touch does not trigger destructive action accidentally |

## Automated coverage to run first

Use the focused Explorer/metadata/i18n/accessibility tests relevant to the changed area, then the full isolated gate. `docs/testing/TESTING.md` is the source of truth for commands.

## Screenshot evidence

Store screenshots outside tracked source or in an explicit throwaway CI/PR artifact location. Name them with state/theme/DPI/language so reviewers can identify what was actually verified.
