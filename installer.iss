#define MyAppName "Stellar Insight"
#define MyAppVersion "1.0"
#define MyAppPublisher "Stellar Insight"
#define MyAppExeName "StellarInsight.exe"

[Setup]
AppId={{A1E6F7C2-8F6C-4B9E-9D2A-123456789ABC}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={commonpf}\Stellar Insight
DefaultGroupName=Stellar Insight
OutputDir=installer
OutputBaseFilename=StellarInsight_Setup
Compression=lzma
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\StellarInsight.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Stellar Insight"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Stellar Insight"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Stellar Insight"; Flags: nowait postinstall skipifsilent

[Code]

function IsWebView2Installed: Boolean;
begin
  Result :=
    RegKeyExists(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F1F4B53A-4F1F-4E35-A9D8-4D1A7A2F4B4F}') or
    RegKeyExists(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F1F4B53A-4F1F-4E35-A9D8-4D1A7A2F4B4F}');
end;

procedure InstallWebView2;
var
  TempFile: string;
  ResultCode: Integer;
begin
  if IsWebView2Installed then
    Exit;

  MsgBox(
    'Stellar Insight needs the Microsoft Edge WebView2 Runtime to display its window.' + #13#10 + #13#10 +
    'It will now be downloaded and installed silently (~2 MB).' + #13#10 +
    'An internet connection is required.',
    mbInformation, MB_OK
  );

  TempFile := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');

  try
    DownloadTemporaryFile(
      'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
      'MicrosoftEdgeWebview2Setup.exe',
      '',
      nil
    );
  except
    MsgBox('Failed to download WebView2 installer: ' + GetExceptionMessage, mbError, MB_OK);
    Abort;
  end;

  if not Exec(TempFile, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('Failed to launch WebView2 installer.', mbError, MB_OK);
    Abort;
  end;
end;

procedure InitializeWizard;
begin
  InstallWebView2;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;

  if CurPageID = wpReady then
  begin
    if not IsWebView2Installed then
    begin
      if MsgBox(
        'WebView2 still does not appear to be installed.' + #13#10 + #13#10 +
        'Continue installation anyway?',
        mbConfirmation, MB_YESNO
      ) = IDNO then
      begin
        Result := False;
      end;
    end;
  end;
end;