; Inno Setup script for the Code Lite Windows installer.
;
; Built by .github/workflows/release.yml, which passes the version in:
;   ISCC /DAppVersion=1.2.3 packaging\windows\codelite.iss
;
; The installer wraps the same single-file executable that ships as the
; portable build, so both artifacts are always the same binary.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Code Lite"
#define AppExeName "CodeLite.exe"
#define AppPublisher "Code Lite contributors"
#define AppUrl "https://github.com/pythonIsFast/CodeLite"

[Setup]
; A stable GUID is what lets a later version upgrade this install in place
; instead of appearing as a second program. Never change it.
AppId={{7C4F1E62-9A3B-4D18-B5C6-1F2E8A9D3B47}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
VersionInfoVersion={#AppVersion}

; Per-user install: no admin prompt, and the agent runs as the same user whose
; files and shell it is meant to touch.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto

LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=CodeLite-{#AppVersion}-windows-setup
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\..\NOTICE"; DestDir: "{app}"; DestName: "NOTICE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Conversations, tokens and settings live in the user's own data directory, not
; under {app}. They are deliberately NOT removed on uninstall -- deleting a
; user's chat history behind their back is not the uninstaller's call.
