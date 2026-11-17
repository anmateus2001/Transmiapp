"""Arranque de TransmiEye.

    python run.py                 → solo este equipo: http://127.0.0.1:8000
    python run.py --red           → accesible desde otros equipos de la red local
    python run.py --puerto 8080
"""
import argparse
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Servidor TransmiEye")
    p.add_argument("--red", action="store_true", help="escuchar en 0.0.0.0 (intranet)")
    p.add_argument("--puerto", type=int, default=8000)
    a = p.parse_args()
    host = "0.0.0.0" if a.red else "127.0.0.1"
    print(f"\n  TransmiEye en http://{'localhost' if not a.red else '<ip-de-este-equipo>'}:{a.puerto}\n")
    uvicorn.run("app.main:app", host=host, port=a.puerto, log_level="warning")
