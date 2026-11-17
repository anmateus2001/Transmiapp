"""Gestor de hilos de cámara (RNF07: varias cámaras concurrentes)."""
import threading

from ..db import Camara, SessionLocal, registrar_log
from .worker import CamaraWorker


class GestorCamaras:
    def __init__(self, servicio_eventos):
        self.eventos = servicio_eventos
        self.workers: dict[int, CamaraWorker] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _datos(cam: Camara) -> dict:
        return {"id": cam.camara_id, "codigo": cam.codigo_camara, "fuente": cam.fuente,
                "estacion": cam.estacion.nombre if cam.estacion else "", "roi": cam.roi_config or {}}

    def iniciar_todas(self):
        with SessionLocal() as s:
            ids = [c.camara_id for c in s.query(Camara).filter(Camara.activa.is_(True)).all()]
        for cid in ids:
            self.iniciar(cid)

    def iniciar(self, cam_id: int):
        with SessionLocal() as s:
            cam = s.get(Camara, cam_id)
            if cam is None:
                raise KeyError(cam_id)
            datos = self._datos(cam)
        with self._lock:
            self.detener(cam_id, registrar=False)
            w = CamaraWorker(datos, self.eventos)
            self.workers[cam_id] = w
            w.start()
        registrar_log("info", "gestor", f"Cámara {datos['codigo']} iniciada.")

    def detener(self, cam_id: int, registrar=True):
        with self._lock:
            w = self.workers.pop(cam_id, None)
        if w is not None:
            w.detener()
            w.join(timeout=6)
            if registrar:
                registrar_log("info", "gestor", f"Cámara {w.cam['codigo']} detenida.")

    def reiniciar_todas(self):
        for cid in list(self.workers):
            self.iniciar(cid)

    def detener_todas(self):
        for cid in list(self.workers):
            self.detener(cid, registrar=False)

    def actualizar_roi(self, cam_id: int, roi: dict):
        w = self.workers.get(cam_id)
        if w is not None:
            w.actualizar_roi(roi)

    def worker(self, cam_id: int) -> CamaraWorker | None:
        return self.workers.get(cam_id)

    def info(self, cam_id: int) -> dict:
        w = self.workers.get(cam_id)
        return w.info() if w else {"estado": "detenida"}
