@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno virtual e instalando dependencias. La primera vez tarda varios minutos...
  python -m venv .venv || (echo No se encontro Python. Instala Python 3.10-3.12 desde python.org y marca "Add to PATH". & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Fallo la instalacion. Revisa tu conexion a internet. & pause & exit /b 1)
)
start "" http://localhost:8000
".venv\Scripts\python.exe" run.py %*
pause
