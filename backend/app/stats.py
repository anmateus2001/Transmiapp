"""Agregaciones de eventos usadas por el panel, la página de estaciones y los reportes PDF."""
import datetime as dt
from collections import Counter, defaultdict

from .db import Camara, Estacion, EventoEvasion, TipoEvasion


def filas_eventos(s, desde: dt.datetime, hasta: dt.datetime, estacion_id=None, camara_id=None):
    q = (s.query(EventoEvasion.timestamp_evento, Estacion.nombre, Camara.codigo_camara, TipoEvasion.codigo,
                 EventoEvasion.confianza, EventoEvasion.confirmado, Estacion.estacion_id)
         .join(Camara, EventoEvasion.camara_id == Camara.camara_id)
         .join(Estacion, Camara.estacion_id == Estacion.estacion_id)
         .join(TipoEvasion, EventoEvasion.tipo_id == TipoEvasion.tipo_id)
         .filter(EventoEvasion.timestamp_evento >= desde, EventoEvasion.timestamp_evento < hasta))
    if estacion_id:
        q = q.filter(Estacion.estacion_id == estacion_id)
    if camara_id:
        q = q.filter(Camara.camara_id == camara_id)
    return q.all()


def resumen(filas) -> dict:
    por_tipo = Counter(f[3] for f in filas)
    por_hora = [0] * 24
    por_estacion = Counter()
    por_dia = Counter()
    for f in filas:
        por_hora[f[0].hour] += 1
        por_estacion[f[1]] += 1
        por_dia[f[0].date().isoformat()] += 1
    confirmados = sum(1 for f in filas if f[5] is True)
    rechazados = sum(1 for f in filas if f[5] is False)
    revisados = confirmados + rechazados
    hora_pico = max(range(24), key=lambda h: por_hora[h]) if filas else None
    return {
        "total": len(filas),
        "por_tipo": dict(por_tipo),
        "por_hora": por_hora,
        "por_estacion": dict(por_estacion.most_common()),
        "por_dia": dict(sorted(por_dia.items())),
        "hora_pico": hora_pico,
        "estacion_critica": por_estacion.most_common(1)[0][0] if por_estacion else None,
        "confirmados": confirmados,
        "rechazados": rechazados,
        "pendientes": len(filas) - revisados,
        "precision": round(confirmados / revisados, 4) if revisados else None,
        "confianza_media": round(sum(f[4] for f in filas) / len(filas), 4) if filas else None,
    }


def por_estacion_detalle(filas) -> dict:
    d = defaultdict(lambda: {"total": 0, "ingresos": 0, "confirmados": 0})
    for f in filas:
        e = d[f[6]]
        e["total"] += 1
        e["ingresos"] += f[3] == "INGRESO_LATERAL"
        e["confirmados"] += f[5] is True
    return d
