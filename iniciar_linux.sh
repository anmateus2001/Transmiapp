#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/backend"
if [ ! -x .venv/bin/python ]; then
  echo "Creando entorno virtual e instalando dependencias (la primera vez tarda varios minutos)..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
( sleep 4; xdg-open http://localhost:8000 >/dev/null 2>&1 || open http://localhost:8000 >/dev/null 2>&1 || true ) &
exec .venv/bin/python run.py "$@"
