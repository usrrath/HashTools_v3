
#define MyAppName "HashTools v3"
#define MyAppVersion "3.0"
#define MyAppPublisher "HashTools"
#define MyAppExeName "HashTools_v3.exe"

; Paths relative to packaging/HashTools_Setup.iss
#define MyAppSourceDir "..\dist\HashTools_v3"
#define MyAppOutputDir "..\installer"

; Stop compilation if the executable is missing
#if !FileExists(MyAppSourceDir + "\" + MyAppExeName)
  #error "HashTools_v3.exe not found. Build with PyInstaller first."
#endif

[Setup]
AppId={{E84E4A9D-7F2A-48F1-B72D-BC238BE231A7}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}

DefaultDirName={autopf}\HashTools
DefaultGroupName={#MyAppName}

OutputDir={#MyAppOutputDir}
OutputBaseFilename=HashTools_v3_Setup_Windows_x64

Compression=lzma2
SolidCompression=yes
WizardStyle=modern

PrivilegesRequired=admin

; x64 installer: Windows x64 only
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os

UninstallDisplayIcon={app}\{#MyAppExeName}

DisableProgramGroupPage=yes
AllowNoIcons=yes
CloseApplications=yes
RestartApplications=no

SetupLogging=yes
VersionInfoVersion=3.0.0.0
VersionInfoDescription=HashTools v3 Installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; \
    Description: "Create a desktop shortcut"; \
    GroupDescription: "Additional icons:"; \
    Flags: unchecked

[Files]
; Include all PyInstaller onedir output,
; including _internal, Python libraries,
; services.memory and required resources.
Source: "{#MyAppSourceDir}\*"; \
    DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; \
    Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"

Name: "{autodesktop}\{#MyAppName}"; \
    Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Launch {#MyAppName}"; \
    WorkingDir: "{app}"; \
    Flags: nowait postinstall skipifsilent
