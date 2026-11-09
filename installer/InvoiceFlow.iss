#define AppName "InvoiceFlow"
#define AppVersion "5.1.1"
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
Source: "..\.build\dist\InvoiceFlow\tesseract-ocr-setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not IsTesseractInstalled

[Icons]
Name: "{group}\InvoiceFlow"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\InvoiceFlow"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Run the bundled engine only on first install or when no usable executable is
; found. Reinstalling/upgrading InvoiceFlow must not launch Tesseract's own
; installer over an existing installation (which can prompt to uninstall it).
Filename: "{tmp}\tesseract-ocr-setup.exe"; Parameters: "/VERYSILENT /NORESTART /SP-"; StatusMsg: "Installing Tesseract OCR engine..."; Flags: waituntilterminated runhidden; Check: not IsTesseractInstalled
Filename: "{app}\{#AppExeName}"; Description: "Launch InvoiceFlow"; Flags: postinstall nowait skipifsilent

[Code]
function TesseractExeAt(const Candidate: String): Boolean;
begin
  Result := (Candidate <> '') and FileExists(Candidate);
end;

function IsTesseractInstalled(): Boolean;
var
  Candidate, InstallDir, PathValue: String;
  Separator: Integer;
begin
  Result := False;

  // Honour a user-configured executable first.
  Candidate := GetEnv('TESSERACT_CMD');
  if TesseractExeAt(Candidate) then begin Result := True; Exit; end;

  // Standard per-machine and per-user install locations.
  if TesseractExeAt(ExpandConstant('{autopf}\Tesseract-OCR\tesseract.exe')) then begin Result := True; Exit; end;
  if TesseractExeAt(ExpandConstant('{autopf32}\Tesseract-OCR\tesseract.exe')) then begin Result := True; Exit; end;
  if TesseractExeAt(ExpandConstant('{localappdata}\Programs\Tesseract-OCR\tesseract.exe')) then begin Result := True; Exit; end;

  // Also detect custom installations already available on PATH.
  PathValue := GetEnv('PATH');
  while PathValue <> '' do begin
    Separator := Pos(';', PathValue);
    if Separator > 0 then begin
      InstallDir := Trim(Copy(PathValue, 1, Separator - 1));
      Delete(PathValue, 1, Separator);
    end else begin
      InstallDir := Trim(PathValue);
      PathValue := '';
    end;
    if (Length(InstallDir) >= 2) and (InstallDir[1] = '"') and (InstallDir[Length(InstallDir)] = '"') then
      InstallDir := Copy(InstallDir, 2, Length(InstallDir) - 2);
    if TesseractExeAt(AddBackslash(InstallDir) + 'tesseract.exe') then begin Result := True; Exit; end;
  end;

  // Some Tesseract installers publish only the install directory in registry.
  if RegQueryStringValue(HKLM64, 'SOFTWARE\Tesseract-OCR', 'InstallDir', InstallDir) then
    if TesseractExeAt(AddBackslash(InstallDir) + 'tesseract.exe') then begin Result := True; Exit; end;
  if RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Tesseract-OCR_is1', 'InstallLocation', InstallDir) then
    if TesseractExeAt(AddBackslash(InstallDir) + 'tesseract.exe') then begin Result := True; Exit; end;
  if RegQueryStringValue(HKCU, 'SOFTWARE\Tesseract-OCR', 'InstallDir', InstallDir) then
    if TesseractExeAt(AddBackslash(InstallDir) + 'tesseract.exe') then begin Result := True; Exit; end;
  if RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Tesseract-OCR_is1', 'InstallLocation', InstallDir) then
    if TesseractExeAt(AddBackslash(InstallDir) + 'tesseract.exe') then begin Result := True; Exit; end;
  if RegQueryStringValue(HKLM32, 'SOFTWARE\Tesseract-OCR', 'InstallDir', InstallDir) then
    if TesseractExeAt(AddBackslash(InstallDir) + 'tesseract.exe') then begin Result := True; Exit; end;
  if RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Tesseract-OCR_is1', 'InstallLocation', InstallDir) then
    if TesseractExeAt(AddBackslash(InstallDir) + 'tesseract.exe') then begin Result := True; Exit; end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox('InvoiceFlow requires 64-bit Windows.', mbError, MB_OK);
    Result := False;
  end;
end;
