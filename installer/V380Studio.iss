#define MyAppName "V380 Studio"
#define MyAppVersion "1.0"
#define MyAppExeName "V380Studio.exe"

[Setup]
AppId={{8F3B2A71-6C19-4E5A-9D40-7A1C2E9B4F08}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=V380 Studio
DefaultDirName={localappdata}\V380Studio
DefaultGroupName=V380 Studio
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=V380StudioSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "..\dist\V380Studio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\V380 Studio"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\V380 Studio"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch V380 Studio"; Flags: nowait postinstall skipifsilent
