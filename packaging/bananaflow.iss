; Inno Setup script for BananaFlow
; Compile with: iscc packaging\bananaflow.iss
; (Inno Setup 6 is free: https://jrsoftware.org/isdl.php)
;
; Run scripts/build_windows.ps1 BEFORE compiling this installer — it
; produces the dist/bananaflow/ folder that the installer packages.

#define AppName        "BananaFlow"
#define AppPublisher   "BananaFlow Media"
#define AppURL         "https://github.com/BananaFlow-Media/BananaFlow"
; BananaFlow's official website. This is what Windows shows as the
; publisher/help link in Apps & features, and what the Start-menu
; "Official website" shortcut opens. Kept in sync with
; utils/website.py::WEBSITE_URL by
; tests/test_p0_gates.py::TestOfficialWebsiteURLConsistency.
#define WebsiteURL     "https://bananaflow.bananaflow-media.workers.dev/"
; The installer offers Hebrew and English, but a shortcut is a single
; static URL, so it points at the site's Hebrew entry page — the same
; page a bare visit to the site redirects to (/ -> 308 -> /he/).
#define WebsiteHomeURL "https://bananaflow.bananaflow-media.workers.dev/he/"
#define WebsiteHelpURL "https://bananaflow.bananaflow-media.workers.dev/he/help/"
#define AppExeName     "bananaflow.exe"
#define AppCliExeName  "bananaflow-cli.exe"
; A brand-new installation identity: BananaFlow installs side-by-side
; with any earlier product and never upgrades or removes it (D-02).
#define AppId          "{{225AE960-D765-430C-BD88-5DED07F420E4}}"

; ── Normalized source roots (finding F-12) ───────────────────────────────────
; Every path below is composed from these two, which are absolute and contain
; no parent traversal.
;
; Writing "..\dist\bananaflow\*" instead makes ISCC compose
; <repo>\packaging\..\dist\... literally, WITHOUT normalizing it, and then hand
; that string to the Win32 file APIs. The dead "\packaging\.." segment costs 13
; characters of MAX_PATH for nothing. Measured during the Phase 15 audit: the
; deepest bundled file (node_modules nesting inside the PO Token Provider
; backend) reached 262 characters as ISCC composed it versus 249 normalized,
; against a 260 limit — so the installer failed to compile from any checkout
; whose path was more than ~70 characters long. It failed with "The system
; cannot find the path specified", naming a node_modules file and never
; mentioning length, which is why it cost an audit to diagnose.
;
; ExtractFilePath(RemoveBackslash(SourcePath)) is the parent of the directory
; holding this script: SourcePath is "<repo>\packaging\", so this yields
; "<repo>\" with a trailing backslash, resolved once at compile time.
; LongPathsEnabled=1 is NOT a fix here: ISCC is not long-path aware (verified,
; it was already 1 on the audit machine).
#define RepoRoot   ExtractFilePath(RemoveBackslash(SourcePath))
#define DistDir    RepoRoot + "dist\bananaflow"

; Read the version from the EXE we just built so a single bump in version.py
; propagates here too. ISPP supports GetStringFileInfo — that is the most
; robust path.
#define AppVersion GetStringFileInfo(DistDir + "\bananaflow.exe", "ProductVersion")

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#WebsiteURL}
AppSupportURL={#WebsiteHelpURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#RepoRoot}dist
OutputBaseFilename=BananaFlow-v{#AppVersion}-windows-x64-setup
SetupIconFile={#SourcePath}bananaflow.ico
WizardImageFile={#SourcePath}installer\WizardImage.bmp
WizardSmallImageFile={#SourcePath}installer\WizardSmallImage.bmp
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#RepoRoot}LICENSES.md

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "hebrew";  MessagesFile: "compiler:Languages\Hebrew.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; Name: "installplaywright"; Description: "Install Playwright Chromium (~300 MB) for channel scraping and sign-in wizard"; Flags: unchecked

[Files]
; Bundle the entire one-folder PyInstaller dist.
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Helper scripts so the user can re-run them post-install.
Source: "{#RepoRoot}scripts\install_playwright.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
; License, source availability, notices, and release docs.
Source: "{#RepoRoot}LICENSE";               DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}LICENSES.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}NOTICE";                DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}SOURCE_OFFER.md";       DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}CONTRIBUTING.md";       DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}README.md";             DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{#AppName} (CLI)"; Filename: "{app}\{#AppCliExeName}"
; Internet shortcut: Inno writes a .url file when Filename is a URL.
; The label stays ASCII on purpose — this script has no BOM, so ISCC
; would read a Hebrew {cm:} custom message as ANSI and garble it in the
; Start menu. The page it opens is Hebrew regardless.
Name: "{group}\{#AppName} Website"; Filename: "{#WebsiteHomeURL}"
; Name: "{group}\Install Playwright"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\install_playwright.ps1"""
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; ; Optional: install Playwright at the end of setup if the user ticked it.
; Filename: "powershell.exe"; \
;     Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\install_playwright.ps1"""; \
;     StatusMsg: "Installing Playwright Chromium..."; \
;     Tasks: installplaywright; Flags: runhidden
; Launch the app at the end of setup if the user wants to.
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Do not delete the user's downloads or their ~/.bananaflow config — only
; remove files we installed. ~/.bananaflow lives under %APPDATA% which is
; out of {app} so it survives uninstall by default.
Type: filesandordirs; Name: "{app}\scripts"
