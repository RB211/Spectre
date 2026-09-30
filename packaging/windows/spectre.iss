; The Windows installer, built by .github/workflows/windows-release.yml with
; Inno Setup from the PyInstaller folder in dist\Spectre:
;
;     iscc /DAppVersion=1.0.0 packaging\windows\spectre.iss
;
; It installs for the current user alone -- no administrator prompt -- into
; %LOCALAPPDATA%\Programs\Spectre, with Start menu entries for Spectre and
; Spectre VR (the same program with --vr), an optional desktop shortcut, and
; an uninstaller in Settings > Apps.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0B7C1A-3F52-4E8C-9A4D-5C1B2F7E9D30}
AppName=Spectre
AppVersion={#AppVersion}
AppVerName=Spectre {#AppVersion}
AppPublisher=William Holland
AppPublisherURL=https://github.com/RB211/Spectre
AppSupportURL=https://github.com/RB211/Spectre/issues
AppUpdatesURL=https://github.com/RB211/Spectre/releases
DefaultDirName={localappdata}\Programs\Spectre
DefaultGroupName=Spectre
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
LicenseFile=..\..\LICENSE
SetupIconFile=..\..\build\icons\spectre.ico
UninstallDisplayIcon={app}\Spectre.exe
OutputDir=..\..\dist
OutputBaseFilename=Spectre-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "desktopvricon"; Description: "Create a desktop shortcut for Spectre VR"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\..\dist\Spectre\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\build\icons\spectre-vr.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\Spectre"; Filename: "{app}\Spectre.exe"
Name: "{group}\Spectre VR"; Filename: "{app}\Spectre.exe"; Parameters: "--vr"; IconFilename: "{app}\spectre-vr.ico"; Comment: "Spectre in a headset (untested on Windows): start Quest Link, Air Link or SteamVR first"
Name: "{group}\Uninstall Spectre"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Spectre"; Filename: "{app}\Spectre.exe"; Tasks: desktopicon
Name: "{autodesktop}\Spectre VR"; Filename: "{app}\Spectre.exe"; Parameters: "--vr"; IconFilename: "{app}\spectre-vr.ico"; Tasks: desktopvricon

[Run]
Filename: "{app}\Spectre.exe"; Description: "Play Spectre now"; Flags: nowait postinstall skipifsilent
