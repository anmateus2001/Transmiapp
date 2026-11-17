"""Un hilo por cámara: captura → detección/seguimiento → reglas → video anotado → eventos."""
import threading
import time
import uuid
from collections import deque

from ..db import registrar_log
from ..settings import AJUSTES
from .analyzer import AnalizadorEvasion, Zonas
from .detectors import crear_detector
from .render import dibujar, escalar_ancho, jpeg, placeholder
from .source import FuenteVideo


class CamaraWorker(threading.Thread):
    def __init__(self, cam: dict, servicio_eventos):
        super().__init__(daemon=True, name=f"camara-{cam['id']}")
        self.cam = cam
        self.eventos = servicio_eventos
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._roi = cam.get("roi") or {}
        self._roi_cambiado = True
        self._jpeg = placeholder("Conectando con la camara...")
        self._raw = None
        self.estado = "iniciando"
        self.error = None
        self.motor = None
        self.resolucion = None
        self.fps = 0.0
        self.latencia_ms = 0.0
        self.frames = 0
        self.eventos_sesion = 0
        self.inicio = time.time()
        self.analisis = {}
        self._ultimo_log = None

    # ── interfaz pública ────────────────────────────────────────────────────
    def detener(self):
        self._parar.set()

    def actualizar_roi(self, roi):
        with self._lock:
            self._roi = roi or {}
            self._roi_cambiado = True

    def jpeg_actual(self) -> bytes:
        with self._lock:
            return self._jpeg

    def snapshot(self) -> bytes | None:
        with self._lock:
            return jpeg(self._raw, 90) if self._raw is not None else None

    def info(self) -> dict:
        return {"estado": self.estado, "error": self.error, "motor": self.motor,
                "resolucion": self.resolucion, "fps": round(self.fps, 1),
                "latencia_ms": round(self.latencia_ms, 1), "frames": self.frames,
                "eventos_sesion": self.eventos_sesion, "analisis": self.analisis,
                "activa_desde": self.inicio}

    def _log(self, nivel, msg):
        if msg != self._ultimo_log:                 # evita inundar el log con el mismo error
            self._ultimo_log = msg
            registrar_log(nivel, f"camara:{self.cam['codigo']}", msg)

    # ── bucle principal ─────────────────────────────────────────────────────
    def run(self):
        cfg = AJUSTES.get()
        detector, aviso = crear_detector(cfg)
        self.motor = detector.nombre
        if aviso:
            self._log("aviso", aviso)
        analizador = AnalizadorEvasion()
        zonas, tam = None, None
        fuente, espera = None, 1.0
        buffer, pendientes = deque(), []
        ultimo_muestreo = 0.0
        t_fps, n_fps = time.monotonic(), 0
        errores_det = 0

        while not self._parar.is_set():
            if fuente is None:
                self.estado = "reconectando" if self.frames else "conectando"
                try:
                    fuente = FuenteVideo(self.cam["fuente"])
                    fuente.abrir()
                    self.resolucion = fuente.resolucion
                    self.estado, self.error, espera = "en_linea", None, 1.0
                    self._log("info", f"Conectada a {self.cam['fuente']} ({fuente.tipo}, {fuente.resolucion}, {fuente.fps:.0f} fps) · {self.motor}")
                except Exception as e:
                    fuente = None
                    self.error = str(e)
                    with self._lock:
                        self._jpeg = placeholder(f"Sin senal: {self.cam['codigo']}\nReintentando...")
                    self._log("error", f"No se pudo conectar: {e}")
                    self._parar.wait(espera)
                    espera = min(espera * 2, 10.0)        # RNF11: reintento ≤ 10 s
                    continue

            frame = fuente.leer()
            if frame is None:
                self._log("aviso", "Se perdió la señal de video; reconectando.")
                fuente.cerrar()
                fuente = None
                continue

            cfg = AJUSTES.get()
            frame = escalar_ancho(frame, cfg["ancho_proceso"])
            h, w = frame.shape[:2]
            with self._lock:
                if self._roi_cambiado or tam != (w, h):
                    zonas, tam = Zonas(self._roi, w, h), (w, h)
                    self._roi_cambiado = False

            t0 = time.perf_counter()
            try:
                personas, vehiculos = detector.detectar(frame, cfg)
                errores_det = 0
            except Exception as e:
                errores_det += 1
                personas, vehiculos = [], []
                if errores_det in (1, 50):
                    self._log("error", f"Fallo de detección: {e}")
            ahora = time.monotonic()
            nuevos = analizador.actualizar(personas, vehiculos, zonas, cfg, ahora)
            dur = (time.perf_counter() - t0) * 1000
            self.latencia_ms = dur if not self.latencia_ms else self.latencia_ms * 0.9 + dur * 0.1

            meta = {"codigo": self.cam["codigo"], "estacion": self.cam["estacion"], "fps": self.fps}
            vista = dibujar(frame, zonas, personas, vehiculos, analizador, meta,
                            anonimizar_caras=cfg["anonimizar_en_vivo"], ahora=ahora)
            with self._lock:
                self._jpeg = jpeg(vista, 80)
                self._raw = frame
            self.analisis = dict(analizador.estado, zonas=zonas.configuradas)

            # Búfer circular para el clip de evidencia (siempre anonimizado)
            if cfg["guardar_clips"] and ahora - ultimo_muestreo >= 1.0 / cfg["clip_fps"]:
                ultimo_muestreo = ahora
                anon = vista if cfg["anonimizar_en_vivo"] else dibujar(
                    frame, zonas, personas, vehiculos, analizador, meta, anonimizar_caras=True, ahora=ahora)
                cuadro = jpeg(escalar_ancho(anon, 640), 72)
                buffer.append(cuadro)
                while len(buffer) > max(cfg["clip_pre_s"] * cfg["clip_fps"], 1):
                    buffer.popleft()
                for p in pendientes:
                    p["frames"].append(cuadro)
                    p["faltan"] -= 1
                for p in [p for p in pendientes if p["faltan"] <= 0]:
                    self.eventos.registrar_clip(p["uid"], p["frames"])
                    pendientes.remove(p)

            for ev in nuevos:
                self._emitir(ev, frame, zonas, personas, vehiculos, analizador, meta, cfg, buffer, pendientes, ahora)

            n_fps += 1
            self.frames += 1
            if ahora - t_fps >= 1.0:
                self.fps = n_fps / (ahora - t_fps)
                t_fps, n_fps = ahora, 0

        for p in pendientes:
            self.eventos.registrar_clip(p["uid"], p["frames"])
        if fuente is not None:
            fuente.cerrar()
        self.estado = "detenida"
        with self._lock:
            self._jpeg = placeholder(f"{self.cam['codigo']} detenida")

    def _emitir(self, ev, frame, zonas, personas, vehiculos, analizador, meta, cfg, buffer, pendientes, ahora):
        uid = uuid.uuid4().hex[:16]
        evidencia = dibujar(frame, zonas, personas, vehiculos, analizador, meta, anonimizar_caras=True,
                            resaltar_tid=ev["track_id"], buscar_rostros=True, ahora=ahora)
        h, w = frame.shape[:2]
        box = ev.get("box")
        box_norm = [round(box[0] / w, 4), round(box[1] / h, 4), round(box[2] / w, 4), round(box[3] / h, 4)] if box else None
        self.eventos.registrar_evento({
            "uid": uid, "camara_id": self.cam["id"], "tipo": ev["tipo"], "confianza": ev["confianza"],
            "track_id": ev["track_id"], "box": box_norm, "detalle": ev["detalle"], "jpeg": jpeg(evidencia, 90),
        })
        self.eventos_sesion += 1
        if cfg["guardar_clips"]:
            if cfg["clip_post_s"] > 0:
                pendientes.append({"uid": uid, "frames": list(buffer), "faltan": cfg["clip_post_s"] * cfg["clip_fps"]})
            elif buffer:
                self.eventos.registrar_clip(uid, list(buffer))
