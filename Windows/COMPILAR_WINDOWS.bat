@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Compilando MonoPoly.exe
echo.
echo  === MonoPoly: compilando .exe ===
echo.
set PY=python
where python >nul 2>nul || set PY=py
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo  ERROR: no encuentro Python con tkinter.
  echo  Instala Python desde python.org tildando "tcl/tk" y "Add python.exe to PATH".
  pause & exit /b 1
)
echo  Instalando herramientas de compilacion...
%PY% -m pip install --upgrade --quiet pip pyinstaller
%PY% -m pip install --upgrade --quiet tkinterdnd2
set DND=
%PY% -c "import tkinterdnd2" >nul 2>nul && set DND=--collect-all tkinterdnd2
echo  Compilando (tarda 1 o 2 minutos)...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name MonoPoly %DND% wavpoly_gui.py
if errorlevel 1 (
  echo.
  echo  FALLO la compilacion. Copiame el texto de arriba.
  pause & exit /b 1
)
copy /y "dist\MonoPoly.exe" "MonoPoly.exe" >nul
rmdir /s /q build >nul 2>nul
rmdir /s /q dist >nul 2>nul
del /q MonoPoly.spec >nul 2>nul
echo.
echo  LISTO: MonoPoly.exe esta en esta carpeta.
echo.
pause
