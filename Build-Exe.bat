@echo off
rem Optional: one-folder Windows build in dist\PhotoSorter\. Excludes pillow-heif (GPL-licensed libheif/x265
rem binaries), so the .exe cannot read HEIC; use Run-PhotoSorter.bat for HEIC libraries.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
pip install "pyinstaller>=6.10"
pyinstaller --noconfirm packaging\photosorter.spec
echo Built dist\PhotoSorter\PhotoSorter.exe
pause
