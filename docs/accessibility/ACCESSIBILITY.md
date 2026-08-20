# BananaFlow accessibility contract

Status: **Current / normative**

Accessibility is a product requirement, not a final polish pass.

## Keyboard

- All interactive functionality must be reachable without a mouse.
- Tab order follows visual/logical order in both LTR and RTL layouts.
- Context-menu actions have keyboard equivalents where a right click would otherwise be required.
- Focus must remain visible; custom styling must not remove the focus indication.
- Long-running operations must not trap focus or block the GUI event loop.

## Screen readers / semantics

- Interactive widgets, including icon-only controls, expose meaningful accessible names and roles.
- Status is not conveyed by color alone; provide text/icon/state semantics.
- Progress and errors have textual representations.

## RTL and mixed-direction content

Hebrew UI uses RTL layout. Technical values that become confusing when mirrored — paths, URLs, filenames where appropriate, codecs, identifiers and similar tokens — stay readable in their natural LTR direction. Back/forward semantics remain logical, not visually inverted in a way that changes meaning.

See [`../i18n/TRANSLATING.md`](../i18n/TRANSLATING.md).

## Scaling and contrast

- Layout must remain usable across supported Windows display scaling ranges, including 100–200% checks for complex screens.
- Text/control clipping is a defect.
- High-contrast/accessibility modes must retain state distinctions without relying on subtle color differences.

## Touch

Touch support must not remove mouse/keyboard behavior. Scrolling, press-and-hold/context behavior, hit target sizing and Tag Editor touch selection/zoom should remain discoverable and non-destructive.

## Testing

Automated accessibility/RTL/DPI tests live in `tests/` and manual matrices under `docs/qa/`. A visible UI change must review accessibility impact and include screenshots/recording in the PR where practical.

## Change rule

New custom widgets, navigation models, modal flows, gesture behavior, layout changes or new status presentations must update tests and this contract/user documentation when the user-visible rules change.
