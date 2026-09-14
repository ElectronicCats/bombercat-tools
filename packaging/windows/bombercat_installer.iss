; BomberCat — Inno Setup installer script.
;
; AppId identifies the application across versions: Inno Setup uses it to find
; a previous install and to register the uninstaller. It was generated once and
; MUST NEVER CHANGE — a new GUID makes Windows treat an upgrade as a second,
; independent product. @VERSION@ is substituted from the VERSION file at build
; time (see packaging/ and .github/workflows/build-windows.yml).

#define MyAppName "BomberCat"
#define MyAppExeName "bombercat.exe"
#define MyAppPublisher "Electronic Cats"
#define MyAppURL "https://github.com/ElectronicCats/bombercat-tools"

[Setup]
AppId={{B2123304-531C-477C-B378-46EE726970B3}
AppName={#MyAppName}
AppVersion=@VERSION@
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=BomberCat-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Add bombercat to the system PATH"; GroupDescription: "Integration:"

[Files]
; PyInstaller --onedir output: the executable plus its whole runtime tree.
Source: "..\..\dist\bombercat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
    ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
    Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE,
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
    'Path', OrigPath)
  then begin
    Result := True;
    exit;
  end;
  { Look for the directory with leading and trailing semicolons so a path that
    merely starts with the same characters is not mistaken for a match. }
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
