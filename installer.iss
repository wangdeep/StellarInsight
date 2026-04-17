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
#define AppVersion   "1.0.0"
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
// Check for Edge WebView2 Runtime — required by pywebview
function IsWebView2Installed: Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(
    HKLM,
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'pv',
    Version
  ) and (Version <> '') and (Version <> '0.0.0.0');
  if not Result then
    Result := RegQueryStringValue(
      HKCU,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv',
      Version
    ) and (Version <> '') and (Version <> '0.0.0.0');
end;

procedure InitializeWizard;
begin
  if not IsWebView2Installed then
    MsgBox(
      'Microsoft Edge WebView2 Runtime was not detected on this PC.' + #13#10 + #13#10 +
      'Stellar Insight needs it to display its window.' + #13#10 +
      'After installation, download and run the WebView2 installer from:' + #13#10 +
      'https://developer.microsoft.com/en-us/microsoft-edge/webview2/' + #13#10 + #13#10 +
      '(Most Windows 10/11 PCs already have it via Windows Update.)',
      mbInformation,
      MB_OK
    );
end;
