@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
pip install -r requirements-dev.txt
set PHOTOSORTER_TEST_MODELS=%LOCALAPPDATA%\PhotoSorter\models
python -m pytest -q
pause
