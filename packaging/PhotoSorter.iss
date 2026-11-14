; Inno Setup script for PhotoSorterSetup.exe (built by .github/workflows/release.yml).
; Per-user install (no admin prompt), so in-app updates can run the next installer silently.
; Your photos, index (<output>\_index), settings and models (%LOCALAPPDATA%\PhotoSorter) are outside {app}
; and are never touched by installing, upgrading or uninstalling.
#ifndef AppVer
  #define AppVer "0.0.0"
#endif

[Setup]
AppId={{6B1E7C1A-3F2D-4E8B-9C55-0A7D2F4B8E61}
AppName=Photo Sorter
AppVersion={#AppVer}
AppVerName=Photo Sorter {#AppVer}
AppPublisher=Photo Sorter contributors
AppPublisherURL=https://github.com/apm84/photo-sorter
AppSupportURL=https://github.com/apm84/photo-sorter/issues
AppUpdatesURL=https://github.com/apm84/photo-sorter/releases
DefaultDirName={localappdata}\Programs\PhotoSorter
DefaultGroupName=Photo Sorter
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=PhotoSorterSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
UninstallDisplayIcon={app}\PhotoSorter.exe
UninstallDisplayName=Photo Sorter
LicenseFile=..\LICENSE
SetupLogging=yes
VersionInfoVersion={#AppVer}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; PyInstaller libraries change between versions: clear the old ones before copying the new build.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\PhotoSorter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Photo Sorter"; Filename: "{app}\PhotoSorter.exe"
Name: "{autodesktop}\Photo Sorter"; Filename: "{app}\PhotoSorter.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PhotoSorter.exe"; Description: "Start Photo Sorter"; Flags: nowait postinstall skipifsilent
Filename: "{app}\PhotoSorter.exe"; Flags: nowait; Check: ShouldRelaunch

[UninstallDelete]
; The optional "start family sync at sign-in" file points at this install.
Type: files; Name: "{userstartup}\PhotoSorter-FamilySync.cmd"

[Code]
function ShouldRelaunch: Boolean;
begin
  { in-app updates pass /RELAUNCH=1 so the app opens again after a silent upgrade }
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
