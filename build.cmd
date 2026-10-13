@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name TellMe_v7 --hidden-import _cffi_backend app.py
) else (
    python -m PyInstaller --noconfirm --clean --onefile --windowed --name TellMe_v7 --hidden-import _cffi_backend app.py
)
