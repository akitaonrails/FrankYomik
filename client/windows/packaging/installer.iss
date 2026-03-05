#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{B7A3F2E1-4D5C-4E8F-9A1B-2C3D4E5F6A7B}
AppName=Frank Yomik
AppVersion={#AppVersion}
AppPublisher=Frank Manga
DefaultDirName={autopf}\FrankYomik
DefaultGroupName=Frank Yomik
UninstallDisplayIcon={app}\frank_client.exe
OutputBaseFilename=FrankYomik-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\build\windows\x64\runner\Release\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Frank Yomik"; Filename: "{app}\frank_client.exe"
Name: "{autodesktop}\Frank Yomik"; Filename: "{app}\frank_client.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\frank_client.exe"; Description: "{cm:LaunchProgram,Frank Yomik}"; Flags: nowait postinstall skipifsilent
