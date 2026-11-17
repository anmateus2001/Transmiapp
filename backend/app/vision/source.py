"""Captura de video desde webcam (índice), stream (rtsp/http) o archivo.

- Streams y webcams: un hilo lee continuamente y solo se procesa el frame más reciente,
  así el retraso no se acumula si la detección es más lenta que la cámara.
- Archivos: se reproducen al ritmo real (saltando frames si el procesamiento va atrasado)
  y se repiten en bucle, de modo que se comportan como una cámara en vivo.
"""
import os
import threading
import time
from pathlib import Path

import cv2

from ..settings import BASE_DIR, VIDEOS_DIR

# RTSP por TCP es mucho más estable a través de redes con pérdida
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")


def resolver_fuente(fuente: str):
    f = str(fuente).strip()
    if f.isdigit():
        return int(f), "webcam"
    if "://" in f:
        return f, "stream"
    for base in (Path(f), BASE_DIR / f, VIDEOS_DIR / f):
        if base.is_file():
            return str(base.resolve()), "archivo"
    raise FileNotFoundError(f"No se encontró el archivo de video «{f}». Ponlo en la carpeta videos/ o usa una ruta completa.")


class FuenteVideo:
    def __init__(self, fuente: str):
        self.fuente = fuente
        self.cap = None
        self.tipo = None
        self.fps = 25.0
        self.resolucion = None
        self._frame = None
        self._seq = 0
        self._leido = 0
        self._cond = threading.Condition()
        self._activo = False
        self._fallo = False
        self._hilo = None

    # ── apertura ────────────────────────────────────────────────────────────
    def abrir(self):
        src, self.tipo = resolver_fuente(self.fuente)
        if self.tipo == "webcam" and os.name == "nt":
            cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)
        elif self.tipo == "stream":
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        else:
            cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            cap.release()
            raise ConnectionError(f"No se pudo abrir la fuente «{self.fuente}».")
        if self.tipo != "archivo":
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        fps = cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if 1 < fps < 121 else 25.0
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.resolucion = f"{w}x{h}" if w and h else None
        self.cap = cap
        self._fallo = False
        if self.tipo == "archivo":
            self._t0 = time.monotonic()
            self._idx = 0
        else:
            self._activo = True
            self._hilo = threading.Thread(target=self._bucle_lectura, daemon=True, name=f"grab-{self.fuente}")
            self._hilo.start()

    def _bucle_lectura(self):
        fallos = 0
        while self._activo:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                fallos += 1
                if fallos >= 25:
                    self._fallo = True
                    with self._cond:
                        self._cond.notify_all()
                    return
                time.sleep(0.04)
                continue
            fallos = 0
            with self._cond:
                self._frame = frame
                self._seq += 1
                self._cond.notify_all()

    # ── lectura ─────────────────────────────────────────────────────────────
    def leer(self, timeout: float = 3.0):
        """Devuelve el siguiente frame o None si la fuente falló."""
        if self.cap is None:
            return None
        if self.tipo == "archivo":
            return self._leer_archivo()
        with self._cond:
            fin = time.monotonic() + timeout
            while self._seq == self._leido and not self._fallo:
                resto = fin - time.monotonic()
                if resto <= 0:
                    return None
                self._cond.wait(resto)
            if self._fallo:
                return None
            self._leido = self._seq
            return self._frame

    def _leer_archivo(self):
        objetivo = int((time.monotonic() - self._t0) * self.fps)
        if objetivo < self._idx:                       # vamos adelantados: esperar al ritmo real
            time.sleep((self._idx - objetivo) / self.fps)
        else:
            saltos = min(objetivo - self._idx, int(self.fps * 2))
            for _ in range(saltos):                    # vamos atrasados: descartar frames viejos
                if not self.cap.grab():
                    break
                self._idx += 1
        ok, frame = self.cap.read()
        if not ok or frame is None:                    # fin del archivo: volver a empezar
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._t0 = time.monotonic()
            self._idx = 0
            ok, frame = self.cap.read()
            if not ok:
                return None
        self._idx += 1
        return frame

    def cerrar(self):
        self._activo = False
        if self._hilo is not None:
            self._hilo.join(timeout=2)
        if self.cap is not None:
            self.cap.release()
        self.cap = None


def probar_fuente(fuente: str) -> dict:
    """Abre la fuente, toma un frame y devuelve sus datos (para el formulario de cámaras)."""
    f = FuenteVideo(fuente)
    f.abrir()
    try:
        frame = None
        for _ in range(3):
            frame = f.leer(timeout=5)
            if frame is not None:
                break
        if frame is None:
            raise ConnectionError("La fuente abrió pero no entregó imágenes.")
        h, w = frame.shape[:2]
        return {"tipo": f.tipo, "resolucion": f"{w}x{h}", "fps": round(f.fps, 1)}
    finally:
        f.cerrar()
