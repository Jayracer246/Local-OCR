; Optional Inno Setup script for a proper Windows installer (Start Menu
; shortcut, uninstaller entry in "Add or remove programs") instead of
; handing out the plain zip from build_windows.ps1.
;
; Prerequisites: Inno Setup (https://jrsoftware.org/isinfo.php), and
; packaging\build_windows.ps1 must have already been run so dist\LocalOCR\
; exists.
;
; Compile with the Inno Setup Compiler GUI, or from the command line:
;   iscc packaging\windows_installer.iss
;
; NOT VERIFIED — written against documented Inno Setup syntax but never
; compiled, since there is no Windows/Inno Setup in this build environment.
; If it fails to compile, the syntax is the first thing to check against
; the current Inno Setup docs; the intent (install dist\LocalOCR\* to
; Program Files, no admin required, Start Menu shortcut) should carry over
; to a fixed version easily.

#define MyAppName "Local OCR"
#define MyAppVersion "1.0.0"
#define MyAppExeName "LocalOCR.exe"

[Setup]
AppId={{B6C1E9C4-6B1E-4B8B-9C7C-LOCALOCR0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
; Per-user install, no admin prompt — matches Ollama's own installer
; philosophy for the same reason: fewer permission hurdles on a locked-down
; offline machine.
DefaultDirName={userpf}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LocalOCR-windows-setup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes

[Files]
Source: "..\dist\LocalOCR\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
