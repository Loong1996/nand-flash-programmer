; Inno Setup script for the nsprog Windows installer.
; Built by host/packaging/build_app.py --installer, which passes
;   /DAppVersion=x.y.z /DSourceExe=<path to nsprog-windows-x86_64.exe> /DIconFile=<.ico>

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef IconFile
  #define IconFile ""
#endif
#ifndef SourceExe
  #define SourceExe "..\dist\nsprog-windows-x86_64.exe"
#endif

[Setup]
AppId={{6E7F1A52-3C1B-4F0E-9E5B-6E53B7A1C0D9}
AppName=nsprog
AppVersion={#AppVersion}
AppVerName=nsprog {#AppVersion}
AppPublisher=nsprog
AppPublisherURL=https://github.com/Loong1996/nand-flash-programmer
DefaultDirName={autopf}\nsprog
DefaultGroupName=nsprog
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=nsprog-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\nsprog.exe
ChangesEnvironment=yes
#if IconFile != ""
SetupIconFile={#IconFile}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(CompilerPath + "Languages\ChineseSimplified.isl")
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "addtopath"; Description: "Add nsprog to PATH (use it from a terminal)"; GroupDescription: "Command line:"

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "nsprog.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\nsprog"; Filename: "{app}\nsprog.exe"; Comment: "NAND / SPI flash programmer (web UI)"
Name: "{group}\Uninstall nsprog"; Filename: "{uninstallexe}"
Name: "{autodesktop}\nsprog"; Filename: "{app}\nsprog.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\nsprog.exe"; Description: "{cm:LaunchProgram,nsprog}"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;
