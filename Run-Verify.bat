@echo off
cd /d "%~dp0"
set /p OUT=Output folder that holds _index (e.g. D:\Sorted Photos): 
.venv\Scripts\python.exe -m photosorter web --out "%OUT%" --page verify
pause
