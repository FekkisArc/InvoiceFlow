#define AppName "InvoiceFlow"
#define AppVersion "4.5.0"
#define AppPublisher "InvoiceFlow"
#define AppExeName "InvoiceFlow.exe"

[Setup]
AppId={{B83F1F70-54E9-4D4D-90B1-6C7B10A64220}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\InvoiceFlow
DefaultGroupName=InvoiceFlow
OutputDir=..\installer-output
OutputBaseFilename=InvoiceFlow_Setup_{#AppVersion}
SetupIconFile=
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
UninstallDisplayName=InvoiceFlow
Uninstallable=yes
DisableProgramGroupPage=yes
CloseApplications=force
RestartApplications=no
LicenseFile=..\LICENSE.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Dirs]
Name: "{localappdata}\InvoiceOCR"; Flags: uninsneveruninstall
Name: "{localappdata}\InvoiceOCR\Originals"; Flags: uninsneveruninstall
Name: "{localappdata}\InvoiceOCR\Exports"; Flags: uninsneveruninstall

[Files]
Source: "..\.build\dist\InvoiceFlow\*"; DestDir: "{app}"; Excludes: "tesseract-ocr-setup.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\.build\dist\InvoiceFlow\tesseract-ocr-setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\InvoiceFlow"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\InvoiceFlow"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\tesseract-ocr-setup.exe"; Parameters: "/S"; StatusMsg: "Installing Tesseract OCR engine..."; Flags: waituntilterminated
Filename: "{app}\{#AppExeName}"; Description: "Launch InvoiceFlow"; Flags: postinstall nowait skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox('InvoiceFlow requires 64-bit Windows.', mbError, MB_OK);
    Result := False;
  end;
end;
