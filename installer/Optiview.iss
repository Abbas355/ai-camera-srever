#define MyAppName "Optiview Backend"
#define MyAppVersion "1.0"
#define MyAppExeName "OptiviewBackend.exe"

[Setup]
AppId={{A7E4C1D2-9B38-4F6A-8E01-2D5C7A9B0E14}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Optiview
DefaultDirName={localappdata}\Optiview
DefaultGroupName=Optiview
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=OptiviewSetup
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
Source: "..\dist\OptiviewBackend\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Optiview Backend"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Optiview Backend"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Optiview Backend"; Flags: nowait postinstall skipifsilent
