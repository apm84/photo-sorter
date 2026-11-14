@echo off
cd /d "%~dp0"
rem Photo Sorter v0.4: start the family sync to the calendar Pi when you sign in to Windows.
rem Run again with  Install-Sync-Autostart.bat remove  to stop that.
set /p OUT=Output folder that holds _index (e.g. D:\Sorted Photos): 
if /i "%1"=="remove" (
  .venv\Scripts\python.exe -m photosorter sync-autostart --out "%OUT%" --remove
) else (
  .venv\Scripts\python.exe -m photosorter sync-autostart --out "%OUT%"
)
pause
