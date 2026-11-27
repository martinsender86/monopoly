#!/bin/bash
# MonoPoly — compila MonoPoly.app en esta carpeta
cd "$(dirname "$0")" || exit 1
echo ""
echo " === MonoPoly: compilando app para Mac ==="
echo ""
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter,sys; sys.exit(0 if tkinter.TkVersion>=8.6 else 1)" 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo " ERROR: no encuentro un Python 3 con Tk 8.6."
  echo " Instala Python desde https://www.python.org/downloads/macos/ y volve a correr esto."
  echo " (El python3 que trae macOS usa un Tk viejo que no sirve.)"
  read -r -p " Enter para salir"; exit 1
fi
echo " Usando $($PY --version) ($(command -v $PY))"
rm -rf .venv_build
"$PY" -m venv .venv_build || { echo " ERROR creando el entorno"; read -r -p " Enter"; exit 1; }
VP=".venv_build/bin/python"
echo " Instalando herramientas de compilacion..."
"$VP" -m pip install --quiet --upgrade pip pyinstaller || { echo " ERROR instalando PyInstaller"; read -r -p " Enter"; exit 1; }
"$VP" -m pip install --quiet tkinterdnd2 >/dev/null 2>&1
DND=""
"$VP" -c "import tkinterdnd2" >/dev/null 2>&1 && DND="--collect-all tkinterdnd2"
echo " Compilando (1 o 2 minutos)..."
"$VP" -m PyInstaller --noconfirm --clean --windowed --name MonoPoly \
  --osx-bundle-identifier ar.monopoly.app $DND wavpoly_gui.py
if [ ! -d "dist/MonoPoly.app" ]; then
  echo ""; echo " FALLO la compilacion. Copiame el texto de arriba."
  read -r -p " Enter para salir"; exit 1
fi
rm -rf MonoPoly.app
cp -R dist/MonoPoly.app ./MonoPoly.app
rm -rf build dist MonoPoly.spec .venv_build
echo ""
echo " LISTO: MonoPoly.app esta en esta carpeta. Podes moverla a Aplicaciones."
echo ""
read -r -p " Enter para cerrar"
