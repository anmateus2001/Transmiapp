"""Purga automática de imágenes con más de N días (Resolución 5858 de 2019 · HU19 · RNF13).

Solo borra los archivos de imagen; los metadatos del evento se conservan para las estadísticas.
"""
import datetime as dt
import shutil
import threading

from .db import Configuracion, EventoEvasion, SessionLocal, registrar_log
from .settings import AJUSTES, EVID_DIR


def purgar(origen="automática") -> dict:
    dias = AJUSTES.get()["retencion_dias"]
    limite = dt.datetime.now() - dt.timedelta(days=dias)
    carpetas = 0
    for d in EVID_DIR.iterdir() if EVID_DIR.exists() else []:
        try:
            fecha = dt.datetime.strptime(d.name, "%Y-%m-%d")
        except ValueError:
            continue
        if fecha.date() < limite.date():
            shutil.rmtree(d, ignore_errors=True)
            carpetas += 1
    with SessionLocal() as s:
        n = (s.query(EventoEvasion)
             .filter(EventoEvasion.timestamp_evento < limite.replace(hour=0, minute=0, second=0),
                     EventoEvasion.purgado.is_(False))
             .update({EventoEvasion.purgado: True, EventoEvasion.clip_frames: 0}, synchronize_session=False))
        fila = s.get(Configuracion, "ultima_purga")
        valor = dt.datetime.now().isoformat(timespec="seconds")
        if fila is None:
            s.add(Configuracion(clave="ultima_purga", valor=valor))
        else:
            fila.valor = valor
        s.commit()
    registrar_log("info", "purga", f"Purga {origen}: {carpetas} día(s) de imágenes eliminados, "
                                   f"{n} evento(s) marcados como purgados (retención {dias} días).")
    return {"dias_eliminados": carpetas, "eventos_purgados": n, "retencion_dias": dias}


class PurgaAutomatica(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="purga")
        self._parar = threading.Event()

    def run(self):
        self._parar.wait(30)
        while not self._parar.is_set():
            try:
                with SessionLocal() as s:
                    fila = s.get(Configuracion, "ultima_purga")
                    ultima = dt.datetime.fromisoformat(fila.valor) if fila and fila.valor else None
                if ultima is None or dt.datetime.now() - ultima >= dt.timedelta(hours=24):
                    purgar()
            except Exception as e:
                registrar_log("error", "purga", f"Fallo en la purga automática: {e}")
            self._parar.wait(3600)

    def detener(self):
        self._parar.set()
