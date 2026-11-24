#pragma codepage 65001
#define ProjectRoot AddBackslash(SourcePath) + "..\..\"
#ifndef ReleaseVersion
  #define ReleaseVersion "0.6.3"
#endif
#ifndef ArtifactDir
  #define ArtifactDir ProjectRoot + "artifacts\installer-0.6.3\"
#endif
#ifndef PayloadDir
  #define PayloadDir ProjectRoot + "artifacts\receive-preview-0.6.3\dist\"
#endif
#define WebViewBootstrapper ProjectRoot + "windows\packaging\vendor\MicrosoftEdgeWebview2Setup.exe"
#define FirewallHelper ProjectRoot + "windows\firewall_rule.ps1"
#define DesktopMutex "Local\SendToCopyQ-Desktop-0.6"

[Setup]
AppId={{8D16AF61-0A5F-48C1-B6D0-4CF3A9876E30}
AppName=MoryPaste
AppVersion={#ReleaseVersion}
AppPublisher=MoryPaste
DefaultDirName={localappdata}\Programs\SendToCopyQ
DefaultGroupName=SendToCopyQ
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
OutputDir={#ArtifactDir}
OutputBaseFilename=MoryPaste-{#ReleaseVersion}-Setup
SetupIconFile={#ProjectRoot}windows\desktop_resources\morypaste.ico
UninstallDisplayName=MoryPaste {#ReleaseVersion}
UninstallDisplayIcon={app}\SendToCopyQ.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AppMutex={#DesktopMutex}
DisableProgramGroupPage=yes
ShowLanguageDialog=no
Uninstallable=yes
#ifdef MoryPasteSigned
SignTool=morypaste
SignedUninstaller=yes
SignedUninstallerDir={#ArtifactDir}
#endif

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#PayloadDir}SendToCopyQ\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WebViewBootstrapper}"; DestDir: "{app}\runtime"; Flags: ignoreversion
Source: "{#FirewallHelper}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\MoryPaste"; Filename: "{app}\SendToCopyQ.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\MoryPaste"; Filename: "{app}\SendToCopyQ.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\SendToCopyQ.exe"; Description: "立即启动 MoryPaste"; Flags: postinstall nowait skipifsilent; Check: CanLaunchApp

[Code]
const
  WebView2ClientId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  UninstallRegistryKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8D16AF61-0A5F-48C1-B6D0-4CF3A9876E30}_is1';
  InteractiveUninstallFlag = '/MORYPASTEUSERUNINSTALL';

var
  DeleteUserData: Boolean;
  InteractiveUninstall: Boolean;
  CanLaunchAppValue: Boolean;

function WebView2RuntimeInstalled: Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2ClientId, 'pv', Version) and
     (Version <> '') and (Version <> '0.0.0.0') then Result := True;
  if not Result and RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WebView2ClientId, 'pv', Version) and
     (Version <> '') and (Version <> '0.0.0.0') then Result := True;
  if not Result and RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2ClientId, 'pv', Version) and
     (Version <> '') and (Version <> '0.0.0.0') then Result := True;
end;

function CanLaunchApp: Boolean;
begin
  Result := CanLaunchAppValue and WebView2RuntimeInstalled;
end;

procedure RegisterInteractiveUninstall;
var
  Command: String;
begin
  { Keep Inno's executable path and its separate QuietUninstallString. }
  if RegQueryStringValue(HKA, UninstallRegistryKey, 'UninstallString', Command) and
     (Pos(InteractiveUninstallFlag, Command) = 0) then
    if not RegWriteStringValue(HKA, UninstallRegistryKey, 'UninstallString',
                               Command + ' /SILENT /NORESTART ' + InteractiveUninstallFlag) then
      Log('Could not register the combined uninstall confirmation.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Bootstrapper: String;
begin
  if CurStep = ssPostInstall then
  begin
    RegisterInteractiveUninstall;
    CanLaunchAppValue := WebView2RuntimeInstalled;
    Bootstrapper := ExpandConstant('{app}\runtime\MicrosoftEdgeWebview2Setup.exe');
    while not CanLaunchAppValue do
    begin
      if not FileExists(Bootstrapper) then Break;
      Exec(Bootstrapper, '/silent /install', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
      CanLaunchAppValue := WebView2RuntimeInstalled;
      if CanLaunchAppValue then Break;
      if MsgBox('未能完成 Microsoft WebView2 Runtime 安装。安装 SendToCopyQ 已完成，但桌面窗口暂时无法启动。' + #13#10 + #13#10 +
                '选择“重试”再次运行微软官方安装程序；选择“取消”打开微软官方页面，之后可重新启动 SendToCopyQ。',
                mbError, MB_RETRYCANCEL) <> IDRETRY then
      begin
        ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
        Break;
      end;
    end;
    if not CanLaunchAppValue then
      MsgBox('SendToCopyQ 已安装。WebView2 Runtime 尚未就绪，因此本次不会自动启动客户端。联网后可再次启动客户端并重试，或从微软官方页面安装 WebView2。',
             mbInformation, MB_OK);
  end;
end;

function HasUninstallArgument(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
    begin
      Result := True;
      exit;
    end;
end;

function UninstallDataDir: String;
begin
  Result := ExpandConstant('{localappdata}\SendToCopyQ');
end;

function CanDeleteUninstallData: Boolean;
var
  ConfigPath, ReceiveDir, DataDir: String;
  Raw: AnsiString;
begin
  DataDir := UninstallDataDir;
  ConfigPath := AddBackslash(DataDir) + 'receive-directory.txt';
  Result := True;
  if not FileExists(ConfigPath) then exit;
  Result := False;
  if not LoadStringFromFile(ConfigPath, Raw) then exit;
  ReceiveDir := Trim(Utf8Decode(Raw));
  if not PathIsRooted(ReceiveDir) then exit;
  ReceiveDir := RemoveBackslashUnlessRoot(ExpandFileName(ReceiveDir));
  DataDir := RemoveBackslashUnlessRoot(ExpandFileName(DataDir));
  Result := not PathSame(ReceiveDir, DataDir) and
            not PathStartsWith(ReceiveDir, AddBackslash(DataDir), True);
end;

function ConfirmUninstallOptions: Boolean;
var
  Form: TSetupForm;
  Heading, Note, RetainedHeading, RetainedNote: TNewStaticText;
  DeleteCheck: TNewCheckBox;
  UninstallButton, CancelButton: TNewButton;
begin
  Form := CreateCustomForm(436, 218, True, True);
  try
    Form.Caption := 'MoryPaste 卸载';
    Form.BorderStyle := bsDialog;

    Heading := TNewStaticText.Create(Form);
    Heading.Parent := Form;
    Heading.Caption := '卸载 MoryPaste？';
    Heading.Font.Style := [fsBold];
    Heading.Left := ScaleX(20);
    Heading.Top := ScaleY(18);

    DeleteCheck := TNewCheckBox.Create(Form);
    DeleteCheck.Parent := Form;
    DeleteCheck.SetBounds(ScaleX(20), ScaleY(49), Form.ClientWidth - ScaleX(40), ScaleY(23));
    DeleteCheck.Caption := '同时删除应用数据';
    DeleteCheck.Checked := False;
    DeleteCheck.Enabled := DirExists(UninstallDataDir) and CanDeleteUninstallData;

    Note := TNewStaticText.Create(Form);
    Note.Parent := Form;
    Note.AutoSize := False;
    Note.WordWrap := True;
    Note.SetBounds(ScaleX(40), ScaleY(77), Form.ClientWidth - ScaleX(60), ScaleY(38));
    if DeleteCheck.Enabled then
      Note.Caption := '删除配对、设置、接收记录和日志。重装后需重新配对。'
    else if DirExists(UninstallDataDir) then
      Note.Caption := '接收文件位于应用数据目录内，或接收位置无法确认。本次保留该目录。'
    else
      Note.Caption := '没有需要清理的应用数据。';

    RetainedHeading := TNewStaticText.Create(Form);
    RetainedHeading.Parent := Form;
    RetainedHeading.Caption := '已接收的文件会保留';
    RetainedHeading.Font.Style := [fsBold];
    RetainedHeading.Left := ScaleX(20);
    RetainedHeading.Top := ScaleY(120);

    RetainedNote := TNewStaticText.Create(Form);
    RetainedNote.Parent := Form;
    RetainedNote.Caption := '图片、视频和其他文件仍在原接收目录。';
    RetainedNote.Left := ScaleX(20);
    RetainedNote.Top := ScaleY(143);

    UninstallButton := TNewButton.Create(Form);
    UninstallButton.Parent := Form;
    UninstallButton.Caption := '卸载';
    UninstallButton.SetBounds(Form.ClientWidth - ScaleX(188), ScaleY(174), ScaleX(80), ScaleY(28));
    UninstallButton.ModalResult := mrOk;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := '取消';
    CancelButton.SetBounds(Form.ClientWidth - ScaleX(100), ScaleY(174), ScaleX(80), ScaleY(28));
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    CancelButton.Default := True;
    Form.ActiveControl := CancelButton;

    Result := Form.ShowModal = mrOk;
    DeleteUserData := Result and DeleteCheck.Enabled and DeleteCheck.Checked;
  finally
    Form.Free;
  end;
end;

function InitializeUninstall: Boolean;
begin
  DeleteUserData := False;
  InteractiveUninstall := UninstallSilent and HasUninstallArgument(InteractiveUninstallFlag)
    and not HasUninstallArgument('/VERYSILENT');
  while CheckForMutexes('{#DesktopMutex}') do
  begin
    Result := False;
    if UninstallSilent and not InteractiveUninstall then exit;
    if MsgBox('MoryPaste 仍在运行。请从系统托盘选择「退出程序」，然后点「重试」。',
              mbInformation, MB_RETRYCANCEL) <> IDRETRY then exit;
  end;
  if InteractiveUninstall then
    Result := ConfirmUninstallOptions
  else
    Result := True;
end;

procedure RemoveProductFirewallRules;
var
  ResultCode: Integer;
  PowerShell, Helper, ReceiverPath, Parameters: String;
begin
  Helper := ExpandConstant('{app}\firewall_rule.ps1');
  PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ReceiverPath := ExpandConstant('{app}\receiver\SendToCopyQ-Receiver.exe');
  Parameters := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "' + Helper +
                '" -ReceiverPath "' + ReceiverPath + '" -Action ';
  if FileExists(Helper) then
  begin
    if not Exec(PowerShell, Parameters + 'HasRules',
                ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    begin
      MsgBox('无法检查 MoryPaste 的防火墙规则，本次卸载已取消。', mbError, MB_OK);
      Abort;
    end;
    if ResultCode = 0 then
    begin
      if UninstallSilent and not InteractiveUninstall then
      begin
        MsgBox('SendToCopyQ 的局域网防火墙规则需要管理员授权才能清理。为避免卸载后留下规则，本次静默卸载已取消。请运行交互式卸载。', mbError, MB_OK);
        Abort;
      end;
      if not ShellExec('runas', PowerShell, Parameters + 'Remove', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      begin
        Log('Firewall cleanup could not start. Windows error: ' + IntToStr(ResultCode));
        if ResultCode <> 1223 then
          MsgBox('无法启动防火墙清理。本次卸载已取消。' + #13#10 + SysErrorMessage(ResultCode), mbError, MB_OK);
        Abort;
      end;
      if ResultCode <> 0 then
      begin
        Log('Firewall cleanup failed. Exit code: ' + IntToStr(ResultCode));
        MsgBox('未能清理当前安装的防火墙规则，本次卸载已取消。退出代码：' + IntToStr(ResultCode), mbError, MB_OK);
        Abort;
      end;
    end
    else if ResultCode <> 1 then
    begin
      MsgBox('无法确认 SendToCopyQ 防火墙规则状态。为避免误删其他规则或留下本应用规则，本次卸载已取消。', mbError, MB_OK);
      Abort;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UserDataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { A direct invocation of unins*.exe keeps Inno's standard confirmation. }
    if not UninstallSilent then
      if not ConfirmUninstallOptions then Abort;
    RemoveProductFirewallRules;
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    DeleteFile(ExpandConstant('{userstartup}\SendToCopyQ.lnk'));
    if DeleteUserData then
    begin
      UserDataDir := UninstallDataDir;
      if not CanDeleteUninstallData then
        MsgBox('程序已卸载。为保留已接收的文件，应用数据目录未删除：' + #13#10 + UserDataDir, mbInformation, MB_OK)
      else if DirExists(UserDataDir) and not DelTree(UserDataDir, True, True, True) and DirExists(UserDataDir) then
        MsgBox('应用已卸载，但用户数据目录未能完全删除，仍保留在：' + #13#10 + UserDataDir, mbError, MB_OK);
    end;
  end;
end;
