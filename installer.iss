; ─────────────────────────────────────────────────────────────────────────────
; Stellar Insight — Inno Setup installer script
;
; Prerequisites:
;   1. Build the exe first:  pyinstaller xylon_eve.spec
;   2. Install Inno Setup 6: https://jrsoftware.org/isdl.php
;   3. Open this file in Inno Setup and click Compile  (or: iscc installer.iss)
;
; Output: installer/StellarInsight_Setup.exe
; ─────────────────────────────────────────────────────────────────────────────

#define AppName      "Stellar Insight"
#define AppVersion      "1.0.0"
#define AppPublisher "Stellarforge"
#define AppURL       "https://insight.stellarforge.nexus"
#define AppExeName   "StellarInsight.exe"
#define AppIcon      "static\icon.ico"

[Setup]
AppId={{A3F7C2D1-88BE-4E5A-B6F0-2D3C9E1A7B45}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; Single-file exe — no uninstaller entry needed in Programs & Features if preferred,
; but having one is friendlier:
UninstallDisplayIcon={app}\{#AppExeName}
; Compress well
Compression=lzma2/ultra64
SolidCompression=yes
; Output location (relative to this .iss file)
OutputDir=installer
OutputBaseFilename=StellarInsight_Setup
; Require admin for Program Files install
PrivilegesRequired=admin
; Suppress the HKCU-while-admin warning — the startup reg entry is intentionally
; written per-user so it only affects the installing user, not all accounts.
UsedUserAreasWarning=no
; Minimum Windows version: Windows 10 (for Edge WebView2)
MinVersion=10.0
WizardStyle=modern
; Show a licence page if you add a LICENSE.txt
; LicenseFile=LICENSE.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";    Description: "{cm:CreateDesktopIcon}";    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon";    Description: "Launch {#AppName} at Windows startup"; GroupDescription: "Startup:"; Flags: unchecked

[Dirs]
; Pre-create the user data directory and subfolders so the app can write
; to them immediately on first launch without needing elevated rights.
; {userappdata} resolves to C:\Users\<user>\AppData\Roaming
Name: "{userappdata}\{#AppName}";           Flags: uninsneveruninstall
Name: "{userappdata}\{#AppName}\nebulae";   Flags: uninsneveruninstall

[Files]
; The compiled single-file executable from PyInstaller
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; Drop the VERSION file into the user data dir so the app and update
; checker can always find it, regardless of the install path.
Source: "VERSION"; DestDir: "{userappdata}\{#AppName}"; Flags: ignoreversion

[Icons]
; Start Menu shortcut
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; Optional desktop shortcut (only if task selected)
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Optional: launch at Windows startup (only if task selected)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: startupicon

[Run]
; Offer to launch the app after install finishes
Filename: "{app}\{#AppExeName}"; \
  Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Kill the running process before uninstalling
Filename: "taskkill.exe"; Parameters: "/F /IM {#AppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
// ─────────────────────────────────────────────────────────────────────────────
// Edge WebView2 Runtime detection + silent bootstrap install
// Required by pywebview on Windows. Most Win10/11 PCs already have it via
// Windows Update, but clean installs may not.
// ─────────────────────────────────────────────────────────────────────────────

function IsWebView2Installed: Boolean;
var
  Version: String;
begin
  // Check machine-wide (typical Windows Update install)
  Result := RegQueryStringValue(
    HKLM,
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'pv', Version
  ) and (Version <> '') and (Version <> '0.0.0.0');

  // Also check per-user install
  if not Result then
    Result := RegQueryStringValue(
      HKCU,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', Version
    ) and (Version <> '') and (Version <> '0.0.0.0');
end;

// Download a file from URL to a local path using WinInet (no external tools)
function DownloadFile(URL, DestPath: String): Boolean;
var
  WinHttpReq: Variant;
  FileStream:  Variant;
begin
  Result := False;
  try
    WinHttpReq := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    WinHttpReq.Open('GET', URL, False);
    WinHttpReq.Send('');
    if WinHttpReq.Status = 200 then
    begin
      FileStream := CreateOleObject('ADODB.Stream');
      FileStream.Type_    := 1;  // adTypeBinary
      FileStream.Open;
      FileStream.Write(WinHttpReq.ResponseBody);
      FileStream.SaveToFile(DestPath, 2);  // adSaveCreateOverWrite
      FileStream.Close;
      Result := True;
    end;
  except
    // Download failed — caller handles the fallback
  end;
end;

procedure InstallWebView2;
var
  TempDir:    String;
  Bootstrapper: String;
  ResultCode: Integer;
begin
  TempDir      := ExpandConstant('{tmp}');
  Bootstrapper := TempDir + '\MicrosoftEdgeWebview2Setup.exe';

  MsgBox(
    'Stellar Insight needs the Microsoft Edge WebView2 Runtime to display its window.' + #13#10 +
    #13#10 +
    'It will now be downloaded and installed silently (~2 MB).' + #13#10 +
    'An internet connection is required.',
    mbInformation, MB_OK
  );

  // Microsoft's evergreen bootstrapper — tiny (~2 MB), installs the latest Runtime
  if DownloadFile(
    'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
    Bootstrapper
  ) then
  begin
    if Exec(Bootstrapper, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    begin
      if IsWebView2Installed then
        MsgBox('WebView2 Runtime installed successfully.', mbInformation, MB_OK)
      else
        MsgBox(
          'WebView2 installation finished but could not be verified.' + #13#10 +
          'If Stellar Insight does not open, please install WebView2 manually from:' + #13#10 +
          'https://developer.microsoft.com/en-us/microsoft-edge/webview2/',
          mbError, MB_OK
        );
    end else
      MsgBox(
        'WebView2 bootstrapper failed to run (error ' + IntToStr(ResultCode) + ').' + #13#10 +
        'Please install it manually from:' + #13#10 +
        'https://developer.microsoft.com/en-us/microsoft-edge/webview2/',
        mbError, MB_OK
      );
  end else
    MsgBox(
      'Could not download the WebView2 bootstrapper.' + #13#10 +
      'Please install it manually from:' + #13#10 +
      'https://developer.microsoft.com/en-us/microsoft-edge/webview2/',
      mbError, MB_OK
    );
end;

procedure InitializeWizard;
begin
  // Nothing to show here — WebView2 check happens at install time
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not IsWebView2Installed then
    InstallWebView2;
end;

// Offer a clean exit if WebView2 still missing after the install attempt
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and not IsWebView2Installed then
  begin
    if MsgBox(
      'Edge WebView2 Runtime is still not detected.' + #13#10 +
      'Stellar Insight may not start correctly.' + #13#10 + #13#10 +
      'Continue with installation anyway?',
      mbConfirmation, MB_YESNO
    ) = IDNO then
      Result := False;
  end;
en