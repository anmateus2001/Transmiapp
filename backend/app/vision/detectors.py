"""Detección + seguimiento de personas y buses.

YoloDetector: YOLO (Ultralytics) + ByteTrack. Cada cámara tiene su propia instancia porque
el tracker guarda estado entre frames.
MotionDetector: respaldo sin PyTorch (MOG2 + tracker por IoU). Menos preciso, pero permite
que el sistema funcione en equipos donde no se puede instalar Ultralytics.
"""
from dataclasses import dataclass

import cv2
import numpy as np

from ..settings import DATA_DIR, MODELS_DIR

CLASE_PERSONA = 0
NOMBRES_VEHICULO = {2: "carro", 5: "bus", 6: "tren", 7: "camion"}


@dataclass
class Deteccion:
    box: np.ndarray          # x1, y1, x2, y2 en píxeles
    conf: float
    clase: int
    tid: int = -1            # id de seguimiento (solo personas)


def resolver_dispositivo(pref: str):
    try:
        import torch
        cuda = torch.cuda.is_available()
    except Exception:
        cuda = False
    if pref == "cuda" and not cuda:
        raise RuntimeError("Se pidió CUDA pero PyTorch no detecta una GPU NVIDIA.")
    if pref == "cpu" or not cuda:
        return "cpu", False
    return 0, True


def config_tracker(cfg) -> str:
    """ByteTrack ajustado: las detecciones débiles (≥0.1) se usan para *mantener* pistas y solo las
    fuertes (≥ conf_persona) crean pistas nuevas. match_thresh alto tolera más desplazamiento entre
    cuadros procesados, que es lo normal cuando la inferencia corre a 5–15 FPS."""
    ruta = DATA_DIR / "bytetrack_transmieye.yaml"
    alta = cfg["conf_persona"]
    ruta.write_text(
        "tracker_type: bytetrack\n"
        f"track_high_thresh: {alta:.2f}\n"
        "track_low_thresh: 0.1\n"
        f"new_track_thresh: {min(alta + 0.05, 0.9):.2f}\n"
        "track_buffer: 45\n"
        "match_thresh: 0.9\n"
        "fuse_score: True\n", encoding="utf-8")
    return str(ruta)


class YoloDetector:
    def __init__(self, modelo: str, dispositivo: str, cfg: dict):
        from ultralytics import YOLO   # import tardío: el respaldo funciona sin Ultralytics
        ruta = MODELS_DIR / modelo
        # Si el modelo no existe, Ultralytics lo descarga a esta misma ruta
        self.model = YOLO(str(ruta))
        self.device, gpu = resolver_dispositivo(dispositivo)
        self.modo = cfg["tracker"]
        self.bytetrack = config_tracker(cfg) if self.modo == "bytetrack" else None
        self.propio = TrackerCentroides()
        self.nombre = f"YOLO {modelo} · {'GPU' if gpu else 'CPU'} · {'ByteTrack' if self.bytetrack else 'tracker FPS-bajo'}"

    def detectar(self, frame, cfg):
        clases = [CLASE_PERSONA] + list(cfg["clases_vehiculo"])
        if self.bytetrack is None:
            conf = min(cfg["conf_persona"], cfg["conf_vehiculo"])
            r = self.model.predict(frame, classes=clases, conf=conf, iou=0.5, imgsz=cfg["imgsz"],
                                   device=self.device, verbose=False)[0]
            personas, vehiculos = [], []
            if r.boxes is not None and len(r.boxes):
                cajas, confs = r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()
                clss = r.boxes.cls.cpu().numpy().astype(int)
                for i in range(len(cajas)):
                    if clss[i] == CLASE_PERSONA and confs[i] >= cfg["conf_persona"]:
                        personas.append(Deteccion(cajas[i], float(confs[i]), CLASE_PERSONA))
                    elif clss[i] != CLASE_PERSONA and confs[i] >= cfg["conf_vehiculo"]:
                        vehiculos.append(Deteccion(cajas[i], float(confs[i]), int(clss[i])))
            return self.propio.actualizar(personas), vehiculos
        r = self.model.track(frame, persist=True, classes=clases, conf=0.1, iou=0.5,
                             imgsz=cfg["imgsz"], device=self.device,
                             verbose=False, tracker=self.bytetrack)[0]
        personas, vehiculos = [], []
        if r.boxes is None or len(r.boxes) == 0:
            return personas, vehiculos
        cajas = r.boxes.xyxy.cpu().numpy()
        confs = r.boxes.conf.cpu().numpy()
        clss = r.boxes.cls.cpu().numpy().astype(int)
        ids = r.boxes.id.cpu().numpy().astype(int) if r.boxes.id is not None else None
        for i in range(len(cajas)):
            if clss[i] == CLASE_PERSONA:
                if ids is None:          # ByteTrack ya filtró por confianza
                    continue
                personas.append(Deteccion(cajas[i], float(confs[i]), CLASE_PERSONA, int(ids[i])))
            elif confs[i] >= cfg["conf_vehiculo"]:
                vehiculos.append(Deteccion(cajas[i], float(confs[i]), int(clss[i])))
        return personas, vehiculos


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


class TrackerCentroides:
    """Seguimiento robusto a FPS bajos (3–15 FPS de inferencia, lo normal en CPU).

    - Toda detección recibe un ID en el mismo cuadro (ByteTrack exige dos cuadros seguidos para
      confirmar una pista y, con saltos grandes entre cuadros, muchas personas nunca la obtienen).
    - Predice la posición con la velocidad de cada pista.
    - Asocia por IoU o por distancia entre pies relativa a la altura de la persona.
    """

    def __init__(self, max_perdidos=8, dist_rel=0.9):
        self.max_perdidos = max_perdidos
        self.dist_rel = dist_rel
        self.pistas = {}       # id -> {"box", "vel", "perdidos"}
        self.sig_id = 1

    def _prediccion(self, p):
        return p["box"] + p["vel"] * (p["perdidos"] + 1)

    def actualizar(self, dets):
        pares = []
        for tid, p in self.pistas.items():
            pb = self._prediccion(p)
            alto = max(pb[3] - pb[1], 1.0)
            pie_p = np.array([(pb[0] + pb[2]) / 2, pb[3]])
            for j, d in enumerate(dets):
                s = iou(pb, d.box)
                pie_d = np.array([(d.box[0] + d.box[2]) / 2, d.box[3]])
                dist = np.linalg.norm(pie_p - pie_d) / alto
                alto_d = max(d.box[3] - d.box[1], 1.0)
                if dist > self.dist_rel or not 0.5 < alto_d / alto < 2.0:
                    continue
                pares.append((s + (1 - dist / self.dist_rel) * 0.5, tid, j))
        pares.sort(reverse=True)
        usados_t, usados_d = set(), set()
        for _, tid, j in pares:
            if tid in usados_t or j in usados_d:
                continue
            usados_t.add(tid)
            usados_d.add(j)
            p = self.pistas[tid]
            nueva_vel = (dets[j].box - p["box"]) / (p["perdidos"] + 1)
            p["vel"] = 0.6 * p["vel"] + 0.4 * nueva_vel
            p["box"], p["perdidos"] = dets[j].box, 0
            dets[j].tid = tid
        for j, d in enumerate(dets):
            if j not in usados_d:
                d.tid = self.sig_id
                self.pistas[self.sig_id] = {"box": d.box, "vel": np.zeros(4), "perdidos": 0}
                self.sig_id += 1
        vistos = {d.tid for d in dets}
        for tid in list(self.pistas):
            if tid not in vistos:
                self.pistas[tid]["perdidos"] += 1
                if self.pistas[tid]["perdidos"] > self.max_perdidos:
                    del self.pistas[tid]
        return dets


class MotionDetector:
    nombre = "Movimiento (OpenCV MOG2)"

    def __init__(self):
        self.fg = cv2.createBackgroundSubtractorMOG2(history=400, varThreshold=32, detectShadows=True)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.tracker = TrackerCentroides()

    def detectar(self, frame, cfg):
        h, w = frame.shape[:2]
        escala = 480.0 / w if w > 480 else 1.0
        peq = cv2.resize(frame, None, fx=escala, fy=escala) if escala != 1.0 else frame
        mask = self.fg.apply(peq)
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)      # descarta sombras (127)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel, iterations=2)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area_min = cfg["area_min_movimiento"] * escala * escala
        area_frame = peq.shape[0] * peq.shape[1]
        personas, vehiculos = [], []
        for c in cnts:
            a = cv2.contourArea(c)
            if a < area_min:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            box = np.array([x, y, x + bw, y + bh], dtype=np.float32) / escala
            if a > 0.12 * area_frame and bw > bh:
                vehiculos.append(Deteccion(box, 0.5, 5))
            elif bh >= 0.8 * bw:
                personas.append(Deteccion(box, 0.5, CLASE_PERSONA))
        return self.tracker.actualizar(personas), vehiculos


def crear_detector(cfg):
    """Devuelve (detector, aviso). Si YOLO no está disponible, cae al detector de movimiento."""
    if cfg["motor"] == "yolo":
        try:
            return YoloDetector(cfg["modelo"], cfg["dispositivo"], cfg), None
        except ImportError:
            return MotionDetector(), "Ultralytics/PyTorch no está instalado: se usa detección por movimiento."
        except Exception as e:
            return MotionDetector(), f"No se pudo cargar YOLO ({e}); se usa detección por movimiento."
    return MotionDetector(), None
