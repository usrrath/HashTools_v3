#define MyAppName "HashTools v3"
#define MyAppVersion "3.0"
#define MyAppPublisher "HashTools"
#define MyAppExeName "HashTools_v3.exe"

[Setup]
AppId={{E84E4A9D-7F2A-48F1-B72D-BC238BE231A7}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\HashTools
DefaultGroupName=HashTools
OutputDir=..\installer
OutputBaseFilename=HashTools_v3_Setup_Windows_x64
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "..\dist\HashTools_v3\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\HashTools v3"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\HashTools v3"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch HashTools v3"; Flags: nowait postinstall skipifsilent
