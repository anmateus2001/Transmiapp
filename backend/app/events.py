"""Persistencia de eventos (fuera del hilo de video) y difusión de alertas en tiempo real."""
import asyncio
import hashlib
import json
import queue
import threading

from fastapi import WebSocket

from .db import Alerta, Camara, EventoEvasion, SessionLocal, TipoEvasion, ahora, registrar_log
from .settings import AJUSTES, EVID_DIR


def nivel_de(riesgo: int) -> str:
    return "critica" if riesgo >= 5 else "media" if riesgo >= 3 else "baja"


class HubAlertas:
    """Clientes WebSocket conectados; publicar() se puede llamar desde cualquier hilo."""

    def __init__(self):
        self.clientes: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def conectar(self, ws: WebSocket):
        await ws.accept()
        self.clientes.add(ws)

    def desconectar(self, ws: WebSocket):
        self.clientes.discard(ws)

    async def _difundir(self, texto: str):
        for ws in list(self.clientes):
            try:
                await ws.send_text(texto)
            except Exception:
                self.clientes.discard(ws)

    def publicar(self, mensaje: dict):
        if self.loop is not None and self.clientes:
            asyncio.run_coroutine_threadsafe(self._difundir(json.dumps(mensaje, default=str)), self.loop)


class ServicioEventos(threading.Thread):
    def __init__(self, hub: HubAlertas):
        super().__init__(daemon=True, name="servicio-eventos")
        self.hub = hub
        self.cola: queue.Queue = queue.Queue(maxsize=500)
        self._tipos: dict[str, tuple[int, int, str]] = {}

    def registrar_evento(self, datos: dict):
        datos["ts"] = ahora()
        try:
            self.cola.put_nowait(("evento", datos))
        except queue.Full:
            registrar_log("error", "eventos", "Cola de eventos llena: se descartó un evento.")

    def registrar_clip(self, uid: str, frames: list):
        try:
            self.cola.put_nowait(("clip", {"uid": uid, "frames": frames}))
        except queue.Full:
            pass

    def run(self):
        while True:
            tipo, datos = self.cola.get()
            try:
                if tipo == "evento":
                    self._guardar_evento(datos)
                else:
                    self._guardar_clip(datos)
            except Exception as e:
                registrar_log("error", "eventos", f"No se pudo guardar {tipo}: {e}")

    def _tipo(self, s, codigo):
        if codigo not in self._tipos:
            t = s.query(TipoEvasion).filter_by(codigo=codigo).one()
            self._tipos[codigo] = (t.tipo_id, t.nivel_riesgo, t.nombre)
        return self._tipos[codigo]

    def _guardar_evento(self, d):
        ts = d["ts"]
        carpeta_rel = f"{ts:%Y-%m-%d}/{d['uid']}"
        carpeta = EVID_DIR / carpeta_rel
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "evento.jpg").write_bytes(d["jpeg"])
        sha = hashlib.sha256(d["jpeg"]).hexdigest()
        cfg = AJUSTES.get()

        with SessionLocal() as s:
            tipo_id, riesgo, nombre_tipo = self._tipo(s, d["tipo"])
            ev = EventoEvasion(uid=d["uid"], camara_id=d["camara_id"], tipo_id=tipo_id, timestamp_evento=ts,
                               confianza=d["confianza"], track_id=d["track_id"], frame_referencia=carpeta_rel,
                               hash_sha256=sha, coordenadas_bbox=d["box"], detalle=d["detalle"])
            s.add(ev)
            s.flush()
            alerta = None
            if cfg["notificaciones"] and d["confianza"] >= cfg["umbral_alerta"]:
                alerta = Alerta(evento_id=ev.evento_id, canal="pantalla", nivel=nivel_de(riesgo), timestamp_alerta=ts)
                s.add(alerta)
            s.commit()
            cam = s.get(Camara, d["camara_id"])
            payload = {
                "tipo": "evento", "evento_id": ev.evento_id, "timestamp": ts.isoformat(),
                "tipo_evento": d["tipo"], "nombre_tipo": nombre_tipo, "confianza": d["confianza"],
                "camara": cam.codigo_camara if cam else None,
                "estacion": cam.estacion.nombre if cam and cam.estacion else None,
                "alerta_id": alerta.alerta_id if alerta else None,
                "nivel": alerta.nivel if alerta else None,
            }
        (carpeta / "metadata.json").write_text(json.dumps({**payload, "hash_sha256": sha, "detalle": d["detalle"],
                                                           "bbox_normalizada": d["box"]}, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
        self.hub.publicar(payload)

    def _guardar_clip(self, d):
        with SessionLocal() as s:
            ev = s.query(EventoEvasion).filter_by(uid=d["uid"]).first()
            if ev is None or not ev.frame_referencia:
                return
            carpeta = EVID_DIR / ev.frame_referencia / "clip"
            carpeta.mkdir(parents=True, exist_ok=True)
            for i, cuadro in enumerate(d["frames"], start=1):
                (carpeta / f"{i:04d}.jpg").write_bytes(cuadro)
            ev.clip_frames = len(d["frames"])
            s.commit()
