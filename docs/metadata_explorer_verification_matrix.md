# Metadata Explorer Verification Matrix

This matrix makes the Tag Editor Explorer imitation measurable. It is not a
claim of 100% Windows Explorer parity; it is the checklist used to compare the
Qt implementation against Windows File Explorer details view.

## Fixture Folder

Create or select a temporary music folder with:
- `01 - English Song.mp3`
- `02 - שיר בעברית.flac`
- `03 - Mixed עברית English 123.m4a`
- `very very very long filename with artist - album - title.mp3`
- `unsupported.wav`
- one subfolder containing one supported file
- one duplicate filename target for move/rename failure checks

Do not use a personal music folder for destructive checks. Delete/move checks
must use disposable files only.

## GUI Workflow

Use the project GUI safety flow:
- Start the app from this workspace.
- Use `list_windows` and activate only the `bananaflow` app window.
- Use screenshot/OCR before clicking.
- Test only the Tag Editor Explorer centre pane.
- If the app window is not clearly identified, stop.

## Comparison States

| Area | States to capture | Expected evidence |
| --- | --- | --- |
| Empty | no scan, empty folder | empty state visible; table not focused |
| Small rows | 1 row, 3 rows | row height, icon gap, checkbox gutter |
| Large rows | 1,000+ rows | scroll remains smooth; no full-row repaint spikes |
| Selection | click, Ctrl+click, Shift+click, Ctrl+A, Escape | selected/current rows match Explorer behavior |
| Focus | active selection, inactive selection | inactive selection remains visible and subdued |
| Hover | row hover, checkbox hover, header hover | hover fill and hit zones align with Explorer |
| Keyboard | F2, Enter, Delete, Shift+Delete, Space, Menu, Shift+F10 | actions route through view signals or safe panel flow |
| Header | resize, double-click best fit, reorder, sort, context menu | widths/order/sort persist and fixed gutter stays pinned |
| Icons | supported audio, unsupported, error, folder | Shell/QFileIconProvider icon or explicit status icon shown |
| Drag/drop | file to folder, file to file parent, invalid descendant, existing target | only valid move emits; invalid drops rejected |
| Accessibility | keyboard-only navigation, screen-reader labels | tree/table/header/zoom controls expose accessible names |
| RTL/LTR | Hebrew UI, mixed Hebrew/English filenames | UI is RTL; filename text and rename editor stay LTR |
| Themes | dark, light, accent change | selection and rubber band use active accent |
| DPI | 100%, 125%, 150%, 200% | row/header/checkbox geometry scales without clipping |
| High contrast | Windows high contrast where available | information is not conveyed by color alone |

## Automated Coverage

Run before claiming a category complete:

```powershell
venv\Scripts\python.exe -m pytest tests/test_explorer_details_view.py tests/test_explorer_tree_widget.py tests/test_metadata_table_model.py tests/test_metadata_accessibility.py tests/test_metadata_recycle_bin.py tests/test_i18n_coverage.py tests/test_hardcoded_string_scanner.py -q
```

## Screenshot Naming

When GUI screenshots are taken, store names outside the repo or in an explicit
throwaway artifacts folder:
- `explorer-empty-light-100.png`
- `explorer-selected-dark-150.png`
- `explorer-rtl-mixed-filenames.png`
- `explorer-header-resize-sort.png`
- `explorer-drag-invalid-target.png`
