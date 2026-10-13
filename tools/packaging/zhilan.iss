; =====================================================================================
; 知澜 · Windows 安装程序（阶段 6 · 条目 6.5 · 交付物 1）
; =====================================================================================
; 编译（**一条命令**，本机路径一律走参数/环境变量，不写死进本文件——本仓纪律「本机路径零容忍」）：
;
;   $env:ZL_PKG_VENDOR = '<仓外 vendor 目录>'           # 里面应有 MicrosoftEdgeWebView2RuntimeInstallerX64.exe
;   & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" tools\packaging\zhilan.iss
;
; 前置（两种输入都要先备好，缺任何一个都会在编译期而不是运行时报错）：
;   1) 成品     ：`pwsh -File tools\packaging\build.ps1 -EvidenceTag S6-6.5` 产出的 dist\zhilan\
;                  —— 顶层只有 `_internal\` 与 `zhilan.exe`（纯 Python 模块在 exe 内嵌 PYZ 里，
;                     `_internal` 看不到 `mineru\` 是正常的，别按包名判完整性）；
;   2) 许可暂存 ：`python tools\packaging\collect_licenses.py --site-packages <pkg-venv>\Lib\site-packages
;                    --inno-license "<Inno Setup 6>\license.txt"` ⇒ build\licenses-stage\
;
; ⚠️ 本文件含中文，**必须以 UTF-8 with BOM 保存**（Inno Setup 6 的编译器要求；无 BOM 会变乱码）。
;
; 设计要点（逐条对应条目 6.5 的规格与开工包裁决）：
;   · 规格 2 安装路径：默认 D:\Zhilan，无 D 盘回落 C:\Zhilan；**末级目录名强制 ASCII**、
;     受保护目录：交互态给明确中文提示（Yes/No 原样保留），静默态（/SILENT、/VERYSILENT）
;     fail-closed 并以精确中文拒因中止（181；拒因区分「受保护目录」与「末级非 ASCII」，
;     两项同时违反时固定优先级 = ASCII 先判；静默模式判据 = WizardSilent，⛔ 不是 Interactive=False）；
;   · 规格 3 WebView2：**已装则跳过**、未装则用随包的 Evergreen 完整离线安装器装上；检测读
;     **HKLM\SOFTWARE\WOW6432Node\...**（实测本机是 system-level 安装，只读 HKCU 会误判「未装」）；
;   · 规格 4 许可：`licenses\` 全量随包（由 collect_licenses.py 机械收集）+ 安装目录根 LICENSE / NOTICE；
;   · 规格 5 桌面默认配置：模板写到 `<安装目录>\.env`，**仅当不存在时**（升级不覆盖用户配置）；
;   · 规格 6 升级/卸载：`.env` / `data\` / `models\` 一律保留；默认不删数据，勾选才删；models 永不动；
;     **不写任何机器/用户级环境变量**（五个 MINERU_* 由打包入口在冻结态进程内 setdefault）；
;   · 裁决 16：预建 `<安装目录>\data\`（顺带绕开「首启 data\ 不存在 ⇒ 第一次迁移必失败」）；
;   · A14 变异自证：用 `ISCC /D` 编译期开关产出「变异态」安装包，**默认态完全不含变异逻辑**。
; =====================================================================================

; -------------------------------------------------------------------------------------
; 0 · 常量区（**所有本机路径只从这里或命令行 /D 参数来**）
; -------------------------------------------------------------------------------------
#define MyAppName "知澜"
; ★ 安装器版本的单一来源；发版时需与 frontend/package.json、公开文档和 Git 标签同步。
;   AppVersion / 安装包文件名 / 安装器自身版本资源全部由它派生。
#define MyAppVersion "0.1.5"
#define MyAppPublisher "bbaicao"
#define MyAppExeName "zhilan.exe"

; 成品目录（默认 = 仓库根 dist\zhilan；`SourcePath` 是 .iss 所在目录，永远以反斜杠结尾）
#ifndef MyPayloadDir
  #define MyPayloadDir SourcePath + "..\..\dist\zhilan"
#endif
; 许可暂存目录（collect_licenses.py 的产物）
#ifndef MyLicensesStageDir
  #define MyLicensesStageDir SourcePath + "..\..\build\licenses-stage"
#endif
; 桌面默认配置模板
#ifndef MyEnvTemplate
  #define MyEnvTemplate SourcePath + "zhilan-desktop.env.template"
#endif
; 安装包产出目录
#ifndef MyOutDir
  #define MyOutDir SourcePath + "..\..\dist\installer"
#endif
; 产物名后缀：默认空（交付物恒为 `zhilan-setup-<版本>.exe`）。
; 只有 A14 变异自证那种场合才用 `/DMyOutputSuffix=-mut-xxx` 区分变异包，**不污染交付物名**。
#ifndef MyOutputSuffix
  #define MyOutputSuffix ""
#endif
; WebView2 Evergreen 完整离线安装器所在目录（**仓外 vendor**，二进制不入库；见开工包裁决 20）
#ifndef MyVendorDir
  #define MyVendorDir GetEnv('ZL_PKG_VENDOR')
#endif

; —— 编译期前置断言：输入不全就**在编译期**停下，不留给运行时 ----
#if MyVendorDir == ""
  #error 未指定 WebView2 完整离线安装器目录。请设环境变量 ZL_PKG_VENDOR=<仓外 vendor 目录>，或用 ISCC /DMyVendorDir=<目录> 传入（该目录下应有 MicrosoftEdgeWebView2RuntimeInstallerX64.exe）。
#endif
#if !FileExists(MyVendorDir + "\MicrosoftEdgeWebView2RuntimeInstallerX64.exe")
  #error ZL_PKG_VENDOR 指向的目录里没有 MicrosoftEdgeWebView2RuntimeInstallerX64.exe。它是规格 3「内置 WebView2」的载体，必须随包（安装过程不得联网去下）。
#endif
#if !FileExists(MyPayloadDir + "\" + MyAppExeName)
  #error 找不到成品 exe（{MyPayloadDir}\zhilan.exe）。请先跑 pwsh -File tools\packaging\build.ps1 -EvidenceTag S6-6.5。
#endif
#if !FileExists(MyEnvTemplate)
  #error 找不到桌面默认配置模板（tools\packaging\zhilan-desktop.env.template）。
#endif
#if !FileExists(MyLicensesStageDir + "\licenses\mineru\LICENSE.md")
  #error 找不到随包许可暂存目录（build\licenses-stage\licenses\mineru\LICENSE.md）。请先跑 tools\packaging\collect_licenses.py；规格 4 的判据是「MinerU 的许可全文能在安装目录里逐字找到」。
#endif
#if !FileExists(MyLicensesStageDir + "\INSTALLATION-TERMS.txt")
  #error 缺少 INSTALLATION-TERMS.txt。许可收集需提供 --webview-license，生成包含 Runtime 条款和用户通知的安装许可页。
#endif

; -------------------------------------------------------------------------------------
; 1 · [Setup]
; -------------------------------------------------------------------------------------
[Setup]
; AppId 一经发布不得再改（升级安装靠它认出旧版本）
AppId={{6F9B2C41-8E3D-4A57-9C10-7B5E2A8D4F63}
; 安装包与向导图标（「书页化澜」；exe 自身的图标由 zhilan.spec 的 icon= 提供）
SetupIconFile={#SourcePath}\zhilan.ico
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=本安装程序由 Inno Setup 6 生成（https://jrsoftware.org/isinfo.php）；其许可证全文随包在 <安装目录>\licenses\Inno-Setup-license.txt。
; 默认装 D:\Zhilan；没有 D 盘回落 C:\Zhilan（见 [Code] GetDefaultDir）；安装页允许改。
DefaultDirName={code:GetDefaultDir}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#MyOutDir}
OutputBaseFilename=zhilan-setup-{#MyAppVersion}{#MyOutputSuffix}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile={#MyLicensesStageDir}\INSTALLATION-TERMS.txt
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
; 安装器自身的版本资源（可由 PowerShell 直接读出来核对，见证据 A3）
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoCopyright=Copyright 2026 bbaicao
VersionInfoTextVersion={#MyAppVersion}

; -------------------------------------------------------------------------------------
; 1b · [Messages]：Inno Setup 6.7.3 官方包**不带**简体中文语言文件（Languages\ 里没有
;      ChineseSimplified.isl），故向导框架默认是英文。这里只覆盖**用户真正会读到**的几处，
;      把界面文案中文化——不改任何行为。⚠️ 本条的已知限制见 证据/S6-6.5-安装程序.md「未覆盖项」。
; -------------------------------------------------------------------------------------
[Messages]
SetupAppTitle=知澜安装程序
SetupWindowTitle=知澜安装程序 - %1
WelcomeLabel1=欢迎安装 知澜
WelcomeLabel2=即将在你的电脑上安装 [name/ver]。%n%n安装完成后不需要另外安装 Python、数据库或模型运行库；但问答与建索引需要你在「设置」页填写一把 API Key（或使用本机 Ollama）。
SelectDirDesc=知澜要装到哪里？
SelectDirLabel3=安装程序将把 知澜 安装到下面的文件夹。
SelectDirBrowseLabel=点「下一步」继续；要换个位置，点「浏览」。
DiskSpaceMBLabel=至少需要 [mb] MB 的可用磁盘空间。
ReadyLabel1=安装程序已准备好，点「安装」开始。
InstallingLabel=正在安装 知澜，请稍候……
FinishedHeadingLabel=知澜 安装完成
FinishedLabel=安装完成。从开始菜单或桌面启动「知澜」即可（首次启动会初始化数据库，稍等几秒）。
FinishedLabelNoIcons=安装完成。从开始菜单启动「知澜」即可（首次启动会初始化数据库，稍等几秒）。
ClickNext=下一步(&N) >
ButtonNext=下一步(&N) >
ButtonBack=< 上一步(&B)
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ButtonCancel=取消
ButtonBrowse=浏览(&R)…
ButtonYes=是(&Y)
ButtonNo=否(&N)
ButtonOK=确定

; -------------------------------------------------------------------------------------
; 2 · [Languages]（只有英文语言文件可用；中文文案由上面的 [Messages] 覆盖提供）
; -------------------------------------------------------------------------------------
[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

; -------------------------------------------------------------------------------------
; 3 · [Tasks]
;     数据保留口径（规格 6 / Q3）：**默认不勾** ⇒ 卸载时保留 data\；勾了才删。
;     ⚠️ models\ 永不出现在 [UninstallDelete] 里（它是用户自己下载的资产）。
; -------------------------------------------------------------------------------------
[Tasks]
Name: "deletedata"; Description: "卸载时一并删除我的数据（<安装目录>\data，含会话记录与文档索引；模型目录 models 永不自动删除）"; GroupDescription: "数据保留："; Flags: unchecked

; -------------------------------------------------------------------------------------
; 4 · [Files]
; -------------------------------------------------------------------------------------
[Files]
; WebView2 完整离线安装器放在固实压缩流前部；仅由 PrepareToInstall 解出，退出时自动清理。
Source: "{#MyVendorDir}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy noencryption
; (a) 许可与声明：随包放安装目录根（Apache-2.0 §4(d) 的常规落法）
Source: "{#MyLicensesStageDir}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyLicensesStageDir}\NOTICE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyLicensesStageDir}\INSTALLATION-TERMS.txt"; DestDir: "{app}"; Flags: ignoreversion
; (b) licenses\ 全量随包（含 MinerU 的自定义 Apache 变体全文、Inno Setup 自身许可、机械生成的清单）
Source: "{#MyLicensesStageDir}\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
; (c) 桌面默认配置：**仅当不存在时**写（规格 5 + 规格 6 的升级口径）。
;     模板按字节原样拷贝；需要知道安装目录的那一行由 [Code] 在装完后**追加**（见 AppendModelDirLine）。
#ifdef MutateOverwriteEnv
  ; ⚠️ 变异态（A14 ②）：去掉 onlyifdoesntexist ⇒ 升级安装会**覆盖**用户改过的 .env。
  ;    只由 ISCC /DMutateOverwriteEnv=1 编译产生；默认态不含本分支。
  Source: "{#MyEnvTemplate}"; DestDir: "{app}"; DestName: ".env"; Flags: ignoreversion uninsneveruninstall
#else
  Source: "{#MyEnvTemplate}"; DestDir: "{app}"; DestName: ".env"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall
#endif
; (d) 成品整树原样搬运（顶层只有 _internal\ 与 zhilan.exe；安装器不得重写/压缩 exe 内容）
Source: "{#MyPayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; -------------------------------------------------------------------------------------
; 5 · [Dirs]
;     data\ 预建（裁决 16：绕开「首启 data\ 不存在 ⇒ 第一次 alembic 迁移必失败」的噪音日志）；
;     models\ 预建（Q2-a 的「解压到 <安装目录>\models\」有确定落点）。
;     两者都 uninsneveruninstall：卸载**不动**它们（删 data 只走下面那条勾选项）。
; -------------------------------------------------------------------------------------
[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\models"; Flags: uninsneveruninstall

; -------------------------------------------------------------------------------------
; 6 · [Icons]：开始菜单「知澜」组（规格 4 要求「开始菜单/关于页提供 licenses 的可查入口」）
; -------------------------------------------------------------------------------------
[Icons]
Name: "{group}\知澜"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\许可证与第三方声明"; Filename: "{app}\licenses"
Name: "{group}\卸载 知澜"; Filename: "{uninstallexe}"

; -------------------------------------------------------------------------------------
; 7 · [Run]
; -------------------------------------------------------------------------------------
[Run]
; WebView2 在 PrepareToInstall 中补齐并确认成功，此处不重复安装。
; 装完可选启动（静默安装不弹窗）
Filename: "{app}\{#MyAppExeName}"; Description: "启动 知澜"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

; -------------------------------------------------------------------------------------
; 8 · [UninstallDelete]
;     只有安装时勾了「卸载时一并删除我的数据」才删 data\；models\ **永不在此出现**。
; -------------------------------------------------------------------------------------
[UninstallDelete]
Type: filesandordirs; Name: "{app}\data"; Tasks: deletedata

; =====================================================================================
; 9 · [Code]
; =====================================================================================
[Code]
const
  { WebView2 Evergreen 运行时的安装信息位置（开工包裁决 9）。
    实测（2026-09-20，本机）：只有 HKLM\SOFTWARE\WOW6432Node\... 这一处有 pv
    （system-level 安装）；HKCU 与原生视图 HKLM\SOFTWARE\... 都是空的。
    ⇒ 只读 HKCU 会把「已装」判成「未装」而重复安装，故这里五个位置都探一遍。 }
  WebView2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2RegWow = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2Guid;
  WebView2RegNative = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid;

{ ----------------------------------------------------------------------------------
  工具：小函数
  ---------------------------------------------------------------------------------- }

function TrimTrailingSlashes(const Dir: String): String;
begin
  Result := Dir;
  while (Length(Result) > 3) and ((Result[Length(Result)] = '\') or (Result[Length(Result)] = '/')) do
    Delete(Result, Length(Result), 1);
end;

function IsAsciiOnly(const S: String): Boolean;
var
  I: Integer;
begin
  Result := True;
  for I := 1 to Length(S) do
    if (Ord(S[I]) < 32) or (Ord(S[I]) > 126) then
    begin
      Result := False;
      Exit;
    end;
end;

function IsUnder(const Dir, Parent: String): Boolean;
var
  D, P: String;
begin
  D := Lowercase(TrimTrailingSlashes(Dir));
  P := Lowercase(TrimTrailingSlashes(Parent));
  Result := (P <> '') and ((D = P) or (Copy(D, 1, Length(P) + 1) = P + '\'));
end;

function IsProtectedDir(const Dir: String): Boolean;
begin
  Result := IsUnder(Dir, ExpandConstant('{commonpf}'))
         or IsUnder(Dir, ExpandConstant('{commonpf32}'))
         or IsUnder(Dir, ExpandConstant('{commonpf64}'))
         or IsUnder(Dir, ExpandConstant('{win}'))
         or IsUnder(Dir, ExpandConstant('{sys}'));
end;

{ ----------------------------------------------------------------------------------
  规格 2：默认安装路径 —— D:\Zhilan，无 D 盘回落 C:\Zhilan
  ---------------------------------------------------------------------------------- }

function GetDefaultDir(Param: String): String;
begin
  if DirExists('D:\') then
    Result := 'D:\Zhilan'
  else
    Result := 'C:\Zhilan';
end;

{ ----------------------------------------------------------------------------------
  规格 2：路径校验（A11 / A12）—— 受保护目录给明确提示；非 ASCII 末级目录名**拒绝**
  ---------------------------------------------------------------------------------- }

function ValidateInstallDir(const Dir: String; Interactive: Boolean; var Reason: String): Boolean;
var
  Name, Msg, ProtectedMsg: String;
begin
  Result := True;
  Reason := '';
  Name := ExtractFileName(TrimTrailingSlashes(Dir));

  if not IsAsciiOnly(Name) then
  begin
    Msg := '安装目录的「最后一级目录名」必须是纯 ASCII（英文/数字/下划线等），不能含中文或其它非 ASCII 字符。'
           + #13#10 + #13#10
           + '当前填的是：' + Name + #13#10 + #13#10
           + '原因：程序解包后的路径一旦含非 ASCII 字符，内置的 onnxruntime / 向量库等原生库会出现'
           + '间歇性加载失败（时好时坏，极难排查）。' + #13#10 + #13#10
           + '建议改成：Zhilan';
    { 181：拒因文本只有这一份，交互态（MsgBox）与静默态（PrepareToInstall 的返回值）共用。 }
    Reason := '安装目录的「最后一级目录名」必须是纯 ASCII 目录名，不能含中文或其它非 ASCII 字符。当前是：'
              + Name + '（程序路径含非 ASCII 字符会让内置原生库间歇性加载失败）。请改成 Zhilan 这类纯英文目录名。';
    Log('路径校验失败：末级目录名含非 ASCII 字符 —— ' + Name);
    if Interactive and not WizardSilent then
      MsgBox(Msg, mbError, MB_OK);
    Result := False;
    Exit;
  end;

  if IsProtectedDir(Dir) then
  begin
    ProtectedMsg := '你选择的安装目录在受保护的位置：' + #13#10 + Dir + #13#10 + #13#10
      + '知澜要在该目录里写配置（.env）、数据（data\）与模型（models\），'
      + '而受保护目录默认不可写 ⇒ 装完之后「设置」保存与文件上传都会失败。' + #13#10 + #13#10
      + '建议改用 D:\Zhilan 或 C:\Zhilan 这类可写目录。是否仍然继续？';
    Log('路径校验提示：受保护目录（默认不可写）—— ' + Dir);
    { 181：静默安装（/SILENT、/VERYSILENT）里没有人能回答这个 Yes/No ⇒ fail-closed（拒绝并交出原因）。
      ⚠️ 判据必须是 WizardSilent（真实安装模式），**不能**用 Interactive 参数：
         交互安装里 PrepareToInstall 也传 Interactive=False，拿它当静默判据会把
         「用户已在目录页确认过 Yes」的安装拦死在最后一步（未经授权改变既有产品行为）。 }
    if WizardSilent then
    begin
      Reason := '安装目录位于受保护的位置，静默安装（/SILENT、/VERYSILENT）无法就此征求用户意见，已中止：' + #13#10
                + Dir + #13#10
                + '该位置默认不可写，而知澜要在其中写配置（.env）、数据（data\）与模型（models\），'
                + '装完之后「设置」保存与文件上传都会失败。' + #13#10
                + '请改用 D:\Zhilan 或 C:\Zhilan 这类可写目录；或去掉 /SILENT 、/VERYSILENT 改用交互安装逐项确认。';
      Log('路径校验失败：静默安装命中受保护目录（fail-closed，不弹框）—— ' + Dir);
      Result := False;
      Exit;
    end;
    if Interactive then
    begin
      if MsgBox(ProtectedMsg, mbConfirmation, MB_YESNO) = IDNO then
      begin
        Reason := '用户在目录页选择「否」：不接受把安装目录放在受保护位置。';
        Result := False;
        Exit;
      end;
    end;
  end;
end;

{ 安装向导的「下一步」：越早拦越好（装到一半再报错会留残迹）。
  ⚠️ 静默安装下这个回调也会被模拟调用 ⇒ 弹框一律用 WizardSilent 关掉，
     真正的静默拒绝在 ValidateInstallDir 内完成（两个 caller 共用同一份判据与拒因）。 }
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Reason: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
    Result := ValidateInstallDir(TrimTrailingSlashes(WizardDirValue()), True, Reason);
end;

{ 静默安装（/SILENT、/VERYSILENT）不走向导 ⇒ 校验必须在 PrepareToInstall 里再来一遍，
  否则「安静模式 + 中文目录」会绕过整条闸门。返回非空字符串 = 中止安装。
  181：中止原因**不再在这里另写一份**，而是取 ValidateInstallDir 交回的真实原因
  （受保护目录与末级非 ASCII 各说各的，且与交互态判据同源）。 }
function GetWebView2Version(): String;
var
  V: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM64, WebView2RegWow, 'pv', V) then Result := V
  else if RegQueryStringValue(HKLM32, WebView2RegNative, 'pv', V) then Result := V
  else if RegQueryStringValue(HKLM64, WebView2RegNative, 'pv', V) then Result := V
  else if RegQueryStringValue(HKCU64, WebView2RegWow, 'pv', V) then Result := V
  else if RegQueryStringValue(HKCU, WebView2RegNative, 'pv', V) then Result := V;
  if Result = '0.0.0.0' then Result := '';
#ifdef MutateSkipWebView2Check
  { ⚠️ 变异态（A9 ② / A14 ①）：**整个脚本**都当机器上没装 WebView2——判据、日志、安装前验证一起翻。
    只由 ISCC /DMutateSkipWebView2Check=1 编译产生；默认态不含本分支。
    （只在 `NeedsWebView2` 上翻是不够的：日志行会继续报「已装 ⇒ 跳过」，与真的跑了离线安装器自相矛盾。） }
  Result := '';
#endif
end;

function NeedsWebView2(): Boolean;
begin
  Result := GetWebView2Version() = '';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir, Reason: String;
  ResultCode: Integer;
begin
  Result := '';
  if WizardSilent and (ExpandConstant('{param:ACCEPTTHIRDPARTYLICENSES|0}') <> '1') then
  begin
    Result := '静默安装需要由用户明确接受随包第三方条款。请阅读 INSTALLATION-TERMS.txt，'
              + '确认同意后再添加 /ACCEPTTHIRDPARTYLICENSES=1；或使用交互安装阅读许可页。';
    Log(Result);
    Exit;
  end;
  Dir := TrimTrailingSlashes(ExpandConstant('{app}'));
  Log('安装目录（最终）：' + Dir);
  if not ValidateInstallDir(Dir, False, Reason) then
  begin
    Result := Reason;
    Exit;
  end;
  if not NeedsWebView2() then
  begin
    Log('WebView2 检测：已装（pv=' + GetWebView2Version() + '）⇒ 跳过，不重复安装、不改动既有版本');
    Exit;
  end;
  Log('WebView2 检测：未装 ⇒ 使用随包的完整离线安装器静默安装');
  try
    ExtractTemporaryFile('MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
  except
    Result := '无法解出随包的 WebView2 完整离线安装器，已中止安装。请重新获取完整安装包。';
    Log(Result + ' ' + GetExceptionMessage());
    Exit;
  end;
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'),
    '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '无法启动 WebView2 完整离线安装器，已中止安装：' + SysErrorMessage(ResultCode);
    Log(Result);
    Exit;
  end;
  if ResultCode = 3010 then
  begin
    NeedsRestart := True;
    Result := 'WebView2 安装要求重启 Windows（返回码 3010）。请重启后重新运行知澜安装包。';
    Log(Result);
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := 'WebView2 完整离线安装失败，已中止安装。返回码：' + IntToStr(ResultCode);
    Log(Result);
    Exit;
  end;
  if NeedsWebView2() then
  begin
    Result := 'WebView2 安装器已退出，但仍未检测到可用运行时，已中止安装。请检查系统权限后重试。';
    Log(Result);
    Exit;
  end;
  Log('WebView2 完整离线安装成功（pv=' + GetWebView2Version() + '）');
end;

{ ----------------------------------------------------------------------------------
  规格 5：桌面默认配置的补齐
    ⚠️ 为什么是「追加一行」而不是「整文件读改写」——**这是实测踩出来的坑**：
    `LoadStringsFromFile` 对**无 BOM** 的文件按**系统 ANSI 代码页**解码，而模板是 UTF-8；
    于是模板里的中文注释被逐行解成乱码，`SaveStringsToFile` 再把乱码按 UTF-8 写回 ⇒
    安装出来的 `.env` 中文全废（2026-09-20 首轮实测，见 证据/S6-6.5-安装后核对-默认目录.txt）。
    改成「模板由 [Files] **逐字节原样拷贝**、安装器**只追加一行纯 ASCII**」之后，
    非 ASCII 文本**全程不经过** Inno 的字符串层 —— ⚠️ **但这个结论只在安装根目录（app 常量）全路径纯 ASCII 时成立**：

    ⚠️ **2026-09-28 就地更正（路径编码修复，端到端静默安装实测）：上面「编码风险归零」的前提已被证伪。**
    `SaveStringToFile` 按**系统 ANSI 代码页**写（本机 cp936/GBK），而安装根目录只由 `ValidateInstallDir`
    校验**末级目录名**（:299-301）⇒ 用户把安装目录放成 `D:\示例目录\Zhilan` 这种
    「末级纯 ASCII、父级含中文」的路径时照样通过，追加行里的中文目录就被写成 GBK 字节，
    与模板拷出来的 UTF-8 内容**混编**（实测 2,254 B：「知澜」的 GBK `d6 aa c0 bd` 落在 2,221–2,224）
    ⇒ 装完启动即崩：`utf-8 codec can not decode byte 0xc0 in position 2223`。
    ⇒ 追加前先判**安装根目录全路径**纯 ASCII（复用既有 `IsAsciiOnly`，:247-258）；
    非纯 ASCII 一律 **Log 一行 + Exit、不写 `.env`**（守卫在 `AppendModelDirLine` 内）。
    守卫**功能无损**：打包态权重根由 `zhilan_launcher.py` 在冻结态进程内 `setdefault` 钉住
    （未显式覆盖时 `MINERU_HOME=<exe 目录>`，MinerU 再追加 `models`；
    用户已设置的真实 MINERU_HOME 环境变量优先，见 zhilan_launcher.py 的 setdefault），
    而 `backend/app/pipeline/mineru.py` 只在 `os.environ` **无** `MINERU_HOME` 时才回落
    `MINERU_MODEL_DIR` ⇒ 这一行在打包态本就冗余，跳过不改运行期权重根。

    追加的判据：`MINERU_MODEL_DIR` 这一行**不存在**时才补 ⇒ 用户自己改过 / 上次安装已补过 ⇒ 一个字都不动。
    （读取只为判断「有没有」，不写回，故 ANSI 解码的影响仅限判据本身，而这一行是纯 ASCII。）
  ---------------------------------------------------------------------------------- }

function HasModelDirLine(const EnvPath: String): Boolean;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := False;
  if not LoadStringsFromFile(EnvPath, Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos('MINERU_MODEL_DIR', Lines[I]) > 0 then
    begin
      Result := True;
      Exit;
    end;
end;

procedure AppendModelDirLine();
var
  EnvPath: String;
  Line: String;
  FullDir: String;
begin
  EnvPath := ExpandConstant('{app}\.env');
  if not FileExists(EnvPath) then
  begin
    Log('.env 未写出（模板缺失或未选中）—— 跳过模型目录补齐');
    Exit;
  end;
  if HasModelDirLine(EnvPath) then
  begin
    Log('.env 里已有 MINERU_MODEL_DIR（用户配置或上次安装补过）—— 原样保留，不追加');
    Exit;
  end;
  { 路径编码修复 守卫：整条安装路径纯 ASCII 才允许追加。
    理由：SaveStringToFile 按系统 ANSI 代码页写 ⇒ 非 ASCII 安装路径会让追加行里的中文目录
    变成 GBK 字节，与模板拷出来的 UTF-8 内容混编，装完启动即崩（见上方规格 5 注释的就地更正）。
    跳过不损失功能：打包态权重根由 zhilan_launcher.py 在冻结态 setdefault 钉住 MINERU_HOME，
    本行仅在「进程内没有 MINERU_HOME」时才被 mineru.py 回落使用，属冗余。 }
  FullDir := TrimTrailingSlashes(ExpandConstant('{app}'));
  if not IsAsciiOnly(FullDir) then
  begin
    Log('安装目录含非 ASCII 字符（' + FullDir + '）—— 跳过 .env 的 MINERU_MODEL_DIR 追加：该行会按 ANSI 代码页把中文目录写成 GBK 字节，与 .env 其余 UTF-8 内容混编，导致启动即崩（路径编码修复）。打包态该行冗余（MINERU_HOME 由 zhilan_launcher.py 在冻结态 setdefault 钉住），不影响运行期权重根。');
    Exit;
  end;
  Line := 'MINERU_MODEL_DIR=' + ExpandConstant('{app}') + '\models' + #13#10;
  if SaveStringToFile(EnvPath, Line, True) then
    Log('.env 已补齐模型目录：MINERU_MODEL_DIR=' + ExpandConstant('{app}') + '\models')
  else
    Log('.env 模型目录补齐失败（写回失败）：' + EnvPath);
end;

{ ----------------------------------------------------------------------------------
  规格 3：WebView2 两态
  ---------------------------------------------------------------------------------- }

{ 安装阶段：把 WebView2 的两态**写进安装日志**（A15 要求「WebView2 两态在日志里有显式行」） }
procedure LogWebView2State();
begin
  if GetWebView2Version() = '' then
    Log('WebView2 检测：未就绪（安装前验证应已中止此状态）')
  else
    Log('WebView2 检测：已装（pv=' + GetWebView2Version() + '）⇒ 跳过，不重复安装、不改动既有版本');
end;

function BoolText(const Value: Boolean): String;
begin
  if Value then
    Result := '是'
  else
    Result := '否';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    LogWebView2State();
    Log('数据保留选择：卸载时删除 data\ = ' + BoolText(IsTaskSelected('deletedata')));
    Log('提示：本安装器不写任何机器级/用户级环境变量；五个 MINERU_* 由知澜在启动时于自身进程内设置。');
  end;
  if CurStep = ssPostInstall then
    AppendModelDirLine();
end;

{ ----------------------------------------------------------------------------------
  卸载：把「数据保留选择」也写进卸载日志（A10/A15 要能逐项核对）
  ---------------------------------------------------------------------------------- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    Log('卸载：data\ 是否一并删除由安装时勾选的「数据保留」决定；models\ 一律不动（用户自有资产）');
end;
