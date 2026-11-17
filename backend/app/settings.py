"""Rutas, valores por defecto y ajustes en caliente de TransmiEye."""
import copy
import os
import secrets
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # carpeta raíz del proyecto
DATA_DIR = BASE_DIR / "data"
EVID_DIR = DATA_DIR / "evidencias"
REPORT_DIR = DATA_DIR / "reportes"
MODELS_DIR = BASE_DIR / "modelos"
VIDEOS_DIR = BASE_DIR / "videos"
FRONTEND_DIR = BASE_DIR / "frontend"

for d in (DATA_DIR, EVID_DIR, REPORT_DIR, MODELS_DIR, VIDEOS_DIR):
    d.mkdir(parents=True, exist_ok=True)

VERSION = "2.0.0"

# SQLite por defecto. Para PostgreSQL:
#   set TRANSMIEYE_DB=postgresql+psycopg2://usuario:clave@localhost:5432/transmieye
DATABASE_URL = os.getenv("TRANSMIEYE_DB", f"sqlite:///{(DATA_DIR / 'transmieye.db').as_posix()}")


def _secret_key() -> str:
    env = os.getenv("TRANSMIEYE_SECRET")
    if env:
        return env
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


SECRET_KEY = _secret_key()
JWT_HORAS = 12

# ── Parámetros de detección (editables desde Configuración) ──────────────────
DEFAULTS = {
    "motor": "yolo",              # yolo | movimiento (respaldo sin PyTorch)
    "modelo": "yolov8n.pt",       # yolov8n/s/m.pt, yolo11n.pt ... o un .pt propio en /modelos
    "dispositivo": "auto",        # auto | cpu | cuda
    "tracker": "propio",          # propio (robusto a FPS bajos) | bytetrack (mejor con GPU y ≥15 FPS)
    "imgsz": 960,              # 640 si el equipo es lento; 960 detecta mejor personas lejanas
    "conf_persona": 0.30,
    "conf_vehiculo": 0.40,
    "clases_vehiculo": [5, 7],    # COCO: 5 bus, 7 truck (6 train confunde la estructura de la estación)
    "solape_bus": 0.25,           # fracción de la bahía que debe cubrir un bus para considerarlo detenido
    "min_frames_zona": 2,         # cuadros seguidos para confirmar de qué lado está una persona
    "profundidad_cruce": 0.15,    # distancia mínima a la línea de puerta, en alturas de persona
    "ventana_cruce_s": 10.0,      # tiempo máximo entre estar en el carril y aparecer en la plataforma
    "ventana_reid_s": 0.8,        # tiempo para recuperar a una persona que cambió de ID en la puerta
    "gracia_bus_s": 4.0,          # segundos tras salir el bus en que los cruces se consideran normales
    "tiempo_carril_s": 3.0,       # permanencia en carril sin bus para alertar
    "aforo_max": 15,              # personas en zona interior para alerta de aglomeración
    "cooldown_s": 20,             # tiempo mínimo entre alertas repetidas del mismo tipo en una cámara
    "umbral_alerta": 0.30,        # confianza mínima de un evento para generar alerta
    "notificaciones": True,
    "sonido_alertas": True,
    "anonimizar_en_vivo": False,  # la evidencia almacenada SIEMPRE se anonimiza (Ley 1581)
    "guardar_clips": True,
    "clip_pre_s": 4,
    "clip_post_s": 2,
    "clip_fps": 6,
    "retencion_dias": 30,
    "ancho_proceso": 960,
    "area_min_movimiento": 1500,
}

_TIPOS = {
    "motor": ("enum", {"yolo", "movimiento"}),
    "modelo": ("str", None),
    "dispositivo": ("enum", {"auto", "cpu", "cuda"}),
    "tracker": ("enum", {"propio", "bytetrack"}),
    "imgsz": ("int", (320, 1920)),
    "conf_persona": ("float", (0.05, 0.95)),
    "conf_vehiculo": ("float", (0.05, 0.95)),
    "clases_vehiculo": ("clases", {2, 5, 6, 7}),
    "solape_bus": ("float", (0.02, 1.0)),
    "min_frames_zona": ("int", (1, 30)),
    "profundidad_cruce": ("float", (0.02, 1.0)),
    "ventana_cruce_s": ("float", (0.5, 60)),
    "ventana_reid_s": ("float", (0, 10)),
    "gracia_bus_s": ("float", (0, 60)),
    "tiempo_carril_s": ("float", (0.5, 120)),
    "aforo_max": ("int", (2, 200)),
    "cooldown_s": ("float", (0, 3600)),
    "umbral_alerta": ("float", (0, 1)),
    "notificaciones": ("bool", None),
    "sonido_alertas": ("bool", None),
    "anonimizar_en_vivo": ("bool", None),
    "guardar_clips": ("bool", None),
    "clip_pre_s": ("int", (0, 15)),
    "clip_post_s": ("int", (0, 15)),
    "clip_fps": ("int", (1, 15)),
    "retencion_dias": ("int", (1, 365)),
    "ancho_proceso": ("int", (320, 1920)),
    "area_min_movimiento": ("int", (100, 50000)),
}

# Cambiar estas claves exige recargar el detector de cada cámara
CLAVES_REINICIO = {"motor", "modelo", "dispositivo", "tracker", "conf_persona"}


def validar(cambios: dict) -> dict:
    limpio = {}
    for k, v in cambios.items():
        if k not in _TIPOS:
            continue
        tipo, regla = _TIPOS[k]
        try:
            if tipo == "int":
                v = int(v)
                if not regla[0] <= v <= regla[1]:
                    raise ValueError
            elif tipo == "float":
                v = float(v)
                if not regla[0] <= v <= regla[1]:
                    raise ValueError
            elif tipo == "bool":
                v = bool(v)
            elif tipo == "enum":
                if v not in regla:
                    raise ValueError
            elif tipo == "str":
                v = str(v).strip()
                if not v or "/" in v or "\\" in v:
                    raise ValueError
            elif tipo == "clases":
                v = sorted({int(x) for x in v})
                if not v or not set(v) <= regla:
                    raise ValueError
        except (ValueError, TypeError):
            raise ValueError(f"Valor inválido para «{k}»: {cambios[k]!r}")
        limpio[k] = v
    return limpio


class AjustesRuntime:
    """Ajustes compartidos entre hilos. Los workers leen una copia en cada frame."""

    def __init__(self):
        self._lock = threading.Lock()
        self._datos = copy.deepcopy(DEFAULTS)

    def cargar(self, guardados: dict):
        with self._lock:
            for k, v in (guardados or {}).items():
                if k in DEFAULTS:
                    self._datos[k] = v

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._datos)

    def actualizar(self, cambios: dict) -> set:
        limpio = validar(cambios)
        with self._lock:
            modificadas = {k for k, v in limpio.items() if self._datos.get(k) != v}
            self._datos.update(limpio)
        return modificadas


AJUSTES = AjustesRuntime()
