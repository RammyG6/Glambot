; Inno Setup script for GlambotSetup.exe.
; Build with: iscc installer.iss /DSourceDataDir="D:\Glambot"
; (build_installer.bat wires this up automatically - see that file.)
;
; NOTE: this script only ever *references* paths on the build machine. It
; contains no secrets itself and is safe to commit. The COMPILED
; GlambotSetup.exe it produces is what embeds whatever live .env/
; credentials.json/token.json exist at #SourceDataDir when you run this -
; treat that .exe as sensitive, same as .env itself (see windows_app's
; README section in the repo README / the plan this was built from).

#ifndef SourceDataDir
  #define SourceDataDir "D:\Glambot"
#endif

#define MyAppName "Glambot"
#define MyAppVersion "1.0"
#define MyAppPublisher "G6 Moco"

[Setup]
AppId={{6C8B9C2E-6E7C-4A7B-9E2A-6B7B3F6C0B1E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Glambot
DefaultGroupName=Glambot
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=.
OutputBaseFilename=GlambotSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; App code - built by `pyinstaller glambot.spec` into windows_app\dist\Glambot
; before this script runs (see build_installer.bat).
Source: "dist\Glambot\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

; This machine's real secrets/config. `onlyifdoesntexist` so re-running the
; installer on a machine that already has these never overwrites them.
; `skipifsourcedoesntexist` so the script still compiles/runs if one of
; these happens to be missing on the build machine (e.g. token.json before
; the first Drive consent) instead of hard-failing the build.
Source: "{#SourceDataDir}\.env"; DestDir: "{code:GetDataDir}"; Flags: onlyifdoesntexist skipifsourcedoesntexist
Source: "{#SourceDataDir}\.env.example"; DestDir: "{code:GetDataDir}"; Flags: onlyifdoesntexist skipifsourcedoesntexist
Source: "{#SourceDataDir}\credentials.json"; DestDir: "{code:GetDataDir}"; Flags: onlyifdoesntexist skipifsourcedoesntexist
Source: "{#SourceDataDir}\token.json"; DestDir: "{code:GetDataDir}"; Flags: onlyifdoesntexist skipifsourcedoesntexist
Source: "{#SourceDataDir}\overlays\*"; DestDir: "{code:GetDataDir}\overlays"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist skipifsourcedoesntexist
Source: "{#SourceDataDir}\soundtracks\*"; DestDir: "{code:GetDataDir}\soundtracks"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist skipifsourcedoesntexist
Source: "{#SourceDataDir}\backgrounds\*"; DestDir: "{code:GetDataDir}\backgrounds"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist skipifsourcedoesntexist

[Dirs]
; Deliberately empty - a general-purpose installer shouldn't silently carry
; over this machine's specific client jobs. Copy project\ over by hand
; afterward if you want to clone this machine's in-progress work.
Name: "{code:GetDataDir}\project"

[Icons]
Name: "{group}\Glambot"; Filename: "{app}\Glambot.exe"
Name: "{autodesktop}\Glambot"; Filename: "{app}\Glambot.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Glambot.exe"; Description: "Launch Glambot now"; Flags: nowait postinstall skipifsilent

; Deliberately no [UninstallDelete] section: Inno's default uninstaller only
; removes what [Files]/[Dirs] installed under {app} (the app code). The data
; folder lives outside {app} and is never referenced there, so it survives
; an uninstall untouched, by construction.

[Code]
var
  DataDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(
    wpSelectDir,
    'Select Data Folder',
    'Where should Glambot store your projects, footage, and settings?',
    'This is kept separate from the app itself, in {app}, so it survives ' +
      'upgrades and uninstalls untouched. Choose an existing folder or a ' +
      'new one - it will be created if needed.',
    False,
    ''
  );
  DataDirPage.Add('');
  DataDirPage.Values[0] := ExpandConstant('{sd}\GlambotData');
end;

function GetDataDir(Param: String): String;
begin
  Result := DataDirPage.Values[0];
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\datadir.txt'), GetDataDir(''), False);
end;
