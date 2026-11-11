; Inno Setup recipe (UNTESTED: needs Windows + Inno Setup 6). Build the one-folder app first:
;   pyinstaller desktop\walter-desktop.spec      -> dist\walter-desktop\
;   iscc scripts\desktop\windows\walter-desktop.iss
; Unsigned: SmartScreen will warn until it's code-signed (a separate, paid step).
#define AppVersion "0.1.0"
[Setup]
AppId={{6E2B7C1A-5D7E-4C8B-9E31-0B7A9C1D2E3F}
AppName=Walter Desktop
AppVersion={#AppVersion}
AppPublisher=Qiiver LLC
DefaultDirName={localappdata}\Programs\Walter Desktop
PrivilegesRequired=lowest
OutputDir=..\..\..\dist
OutputBaseFilename=walter-desktop-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
[Files]
Source: "..\..\..\dist\walter-desktop\*"; DestDir: "{app}"; Flags: recursesubdirs
[Icons]
Name: "{userprograms}\Walter Desktop"; Filename: "{app}\walter-desktop.exe"; Parameters: "run"
Name: "{userstartup}\Walter Desktop"; Filename: "{app}\walter-desktop.exe"; Parameters: "run"; Tasks: autostart
[Tasks]
Name: autostart; Description: "Start the Walter face when I log in"
[Run]
Filename: "{app}\walter-desktop.exe"; Parameters: "doctor"; Flags: postinstall shellexec skipifsilent; Description: "Check this computer (doctor)"
