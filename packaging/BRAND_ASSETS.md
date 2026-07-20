# BananaFlow brand assets — provenance

Every visual asset in this directory is a production derivative taken
from the approved **BananaFlow Brand Asset Pack v2.0** (owner-supplied,
maintained outside this repository). The pack's QA report verifies the
ICO resolution set (16–256 px), the ICNS set, and the wizard bitmaps.

| Repository file | Source asset in the brand pack |
|---|---|
| `packaging/bananaflow.ico` | `05_APP_ICONS/BananaFlow_Primary_App.ico` |
| `packaging/bananaflow.icns` | `05_APP_ICONS/BananaFlow_Primary_App.icns` |
| `packaging/installer/WizardImage.bmp` | `07_WINDOWS/Inno_Setup/WizardImage.bmp` |
| `packaging/installer/WizardSmallImage.bmp` | `07_WINDOWS/Inno_Setup/WizardSmallImage.bmp` |

Usage:

* `bananaflow.ico` — Windows EXE icon (both `bananaflow.exe` and
  `bananaflow-cli.exe`, wired in `packaging/bananaflow.spec`) and the
  installer's `SetupIconFile`.
* `bananaflow.icns` — macOS `.app` bundle icon.
* `Wizard*.bmp` — Inno Setup wizard imagery (`packaging/bananaflow.iss`).

The BananaFlow artwork is supplied for use by the BananaFlow owner; the
brand pack does not grant third parties permission to reuse the mark for
unrelated products. When the brand pack is revised, refresh these
derivatives from the same source paths and update this table.
