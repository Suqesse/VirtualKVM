; Compile with official Inno Setup 6 (ISCC.exe).
; All media tools, runtime libraries and notices must already be in the payload.
; Keep AppId stable across releases so upgrades use the existing installation.
#ifndef AppVersion
  #error AppVersion must be provided by scripts/installers/windows.py
#endif
#ifndef AppSourceDir
  #error AppSourceDir must be provided by scripts/installers/windows.py
#endif
#ifndef SetupOutputDir
  #error SetupOutputDir must be provided by scripts/installers/windows.py
#endif

[Setup]
AppId={{D7A9C831-9513-4B4F-B9C1-65AE1EC77B3F}
AppName=VirtualKVM
AppVersion={#AppVersion}
AppPublisher=VirtualKVM
AppCopyright=Copyright (c) 2026 VirtualKVM contributors
DefaultDirName={localappdata}\Programs\VirtualKVM
DefaultGroupName=VirtualKVM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
OutputDir={#SetupOutputDir}
OutputBaseFilename=VirtualKVM-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
DisableFinishedPage=no
LicenseFile={#AppSourceDir}\_internal\licenses\VirtualKVM-MIT.txt
Uninstallable=yes
CreateUninstallRegKey=yes
UninstallDisplayIcon={app}\VirtualKVM.exe
CloseApplications=yes
CloseApplicationsFilter=VirtualKVM.exe
RestartApplications=no
ChangesAssociations=no
ChangesEnvironment=no
#ifdef AppIconFile
SetupIconFile={#AppIconFile}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\VirtualKVM"; Filename: "{app}\VirtualKVM.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\VirtualKVM"; Filename: "{app}\VirtualKVM.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\VirtualKVM.exe"; Description: "{cm:LaunchProgram,VirtualKVM}"; Flags: nowait postinstall skipifsilent unchecked

; No startup tasks, firewall/network changes, downloads, or user-data deletes.
; Inno's file log removes installed application files on uninstall. Preferences,
; paired identity state, downloads and user-selected shared folders are external
; to this payload and must remain untouched by install/upgrade/uninstall.
