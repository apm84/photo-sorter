@echo off
setlocal
cd /d "%~dp0"
rem One-time: convert google/siglip2-base-patch16-224 (Apache-2.0) to ONNX in %LOCALAPPDATA%\PhotoSorter\models\siglip2
where py >nul 2>nul && (set PY=py -3.12) || (set PY=python)
if not exist .exportenv (%PY% -m venv .exportenv || (echo Could not create .exportenv & pause & exit /b 1))
call .exportenv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install torch --index-url https://download.pytorch.org/whl/cpu || (pause & exit /b 1)
pip install -r requirements-export.txt || (pause & exit /b 1)
python tools\export_siglip2.py || (echo Export failed & pause & exit /b 1)
call deactivate
echo.
echo Done. You can delete the .exportenv folder now to free about 1.5 GB.
