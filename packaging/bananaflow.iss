; Inno Setup script for BananaFlow
; Compile with: iscc packaging\bananaflow.iss
; (Inno Setup 6 is free: https://jrsoftware.org/isdl.php)
;
; Run scripts/build_windows.ps1 BEFORE compiling this installer — it
; produces the dist/bananaflow/ folder that the installer packages.

#define AppName        "BananaFlow"
#define AppPublisher   "BananaFlow Media"
#define AppURL         "https://github.com/BananaFlow-Media/BananaFlow"
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
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
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
