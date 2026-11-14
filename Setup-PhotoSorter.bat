@echo off
setlocal
cd /d "%~dp0"
echo === Photo Sorter setup ===
set PY=
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY (
  py -3.13 --version >nul 2>nul && set "PY=py -3.13"
)
if not defined PY (
  py -3.14 --version >nul 2>nul && set "PY=py -3.14"
)
if not defined PY set PY=python
%PY% -c "import sys; raise SystemExit(0 if (3,12)<=sys.version_info[:2]<=(3,14) else 1)" || (echo Python 3.12, 3.13, or 3.14 is required: https://www.python.org/downloads/windows/ & pause & exit /b 1)
if not exist .venv (%PY% -m venv .venv || (echo Could not create .venv & pause & exit /b 1))
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip uninstall -y onnxruntime >nul 2>nul
pip install -r requirements-windows.txt || (echo Install failed & pause & exit /b 1)
python -m photosorter providers
echo.
echo Downloading face models (YuNet + SFace, about 40 MB)...
python -m photosorter download-models --embedder none --faces yunet_sface || (pause & exit /b 1)
python -c "from photosorter.embedders import embedder_available as a; raise SystemExit(0 if a('siglip2') else 1)"
if errorlevel 1 (
  echo.
  echo The image AI model ^(SigLIP 2^) is not installed yet.
  python -m photosorter download-models --embedder siglip2 --faces none 2>nul || (
    echo It will now be converted on this PC once ^(downloads about 1.5 GB of tools, takes ~10 minutes^).
    call "%~dp0Export-Models.bat"
  )
)
echo.
echo Setup done. Start the app with Run-PhotoSorter.bat
pause
