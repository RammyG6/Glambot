; Inno Setup script for GlambotSetup.exe.
; Build with: iscc installer.iss /DSourceDataDir="D:\Glambot" /DMyAppVersion="1.10"
; (build_installer.bat wires both of these up automatically - see that file,
; which reads MyAppVersion from the repo-root VERSION file so Windows and
; macOS builds can never drift to different version numbers.)
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
; Fallback only - always overridden by build_installer.bat from the
; repo-root VERSION file. Keep in sync manually if you ever build by
; invoking iscc directly without a /DMyAppVersion override.
#ifndef MyAppVersion
  #define MyAppVersion "1.10"
#endif
#define MyAppPublisher "G6 Moco"

[Setup]
AppId={{6C8B9C2E-6E7C-4A7B-9E2A-6B7B3F6C0B1E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
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

[Dirs]
; Deliberately empty - a general-purpose installer shouldn't silently carry
; over this machine's specific client jobs, overlays, or soundtracks. Copy
; any of these over by hand afterward if you want to clone this machine's
; in-progress work or asset library.
Name: "{code:GetDataDir}\project"
Name: "{code:GetDataDir}\overlays"
Name: "{code:GetDataDir}\soundtracks"
Name: "{code:GetDataDir}\backgrounds"

[Icons]
Name: "{group}\Glambot"; Filename: "{app}\Glambot.exe"
Name: "{autodesktop}\Glambot"; Filename: "{app}\Glambot.exe"; Tasks: desktopicon

[Run]
; Allow inbound connections on the Glambot port so guest downloads / iPad
; control work over the LAN (BIND_HOST=0.0.0.0) without a manual Windows
; Defender Firewall prompt. Harmless when Glambot stays on loopback.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Glambot LAN"" dir=in action=allow protocol=TCP localport=5000"; Flags: runhidden; StatusMsg: "Adding firewall rule for LAN access..."
; Built-in camera FTP import: control port (default 2121) + passive data range.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Glambot FTP import"" dir=in action=allow protocol=TCP localport=2121,50000-50050"; Flags: runhidden; StatusMsg: "Adding firewall rule for camera FTP import..."
Filename: "{app}\Glambot.exe"; Description: "Launch Glambot now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Glambot LAN"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Glambot FTP import"""; Flags: runhidden

; Deliberately no [UninstallDelete] section: Inno's default uninstaller only
; removes what [Files]/[Dirs] installed under {app} (the app code). The data
; folder lives outside {app} and is never referenced there, so it survives
; an uninstall untouched, by construction.

[Code]
var
  DataDirPage: TInputDirWizardPage;
  EnvPreexisted: Boolean;

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

// Strips a leading INBOX_DIR=... line from the just-copied .env, so a
// brand-new data folder falls back to glambot_launcher.py's own default
// (data_dir\project) instead of the build machine's absolute dev path.
// Only touches an .env this install just wrote (see EnvPreexisted below) -
// never a data folder's own already-customized .env.
procedure StripInboxDirFromEnv(const EnvPath: String);
var
  Lines: TStringList;
  I: Integer;
begin
  if not FileExists(EnvPath) then
    exit;
  Lines := TStringList.Create;
  try
    Lines.LoadFromFile(EnvPath);
    for I := Lines.Count - 1 downto 0 do
      if Copy(Lines[I], 1, 10) = 'INBOX_DIR=' then
        Lines.Delete(I);
    Lines.SaveToFile(EnvPath);
  finally
    Lines.Free;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    EnvPreexisted := FileExists(GetDataDir('') + '\.env');
  if CurStep = ssPostInstall then
  begin
    SaveStringToFile(ExpandConstant('{app}\datadir.txt'), GetDataDir(''), False);
    if not EnvPreexisted then
      StripInboxDirFromEnv(GetDataDir('') + '\.env');
  end;
end;
