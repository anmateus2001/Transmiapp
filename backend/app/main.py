"""TransmiEye · API REST (FastAPI), WebSocket de alertas, streaming MJPEG e interfaz web."""
import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import shutil
import time
import zipfile
from contextlib import asynccontextmanager

import cv2
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func

from . import auth
from .auth import Usuario, requiere
from .db import (Alerta, Camara, Configuracion, Estacion, EventoEvasion, LogAcceso, LogTecnico, Operador, Reporte,
                 SessionLocal, TipoEvasion, ahora, guardar_ajustes, inicializar_bd, registrar_acceso, registrar_log)
from .events import HubAlertas, ServicioEventos
from .maintenance import PurgaAutomatica, purgar
from .reports import generar_reporte
from .settings import (AJUSTES, CLAVES_REINICIO, DATABASE_URL, EVID_DIR, FRONTEND_DIR, MODELS_DIR, REPORT_DIR,
                       VERSION, VIDEOS_DIR)
from .stats import filas_eventos, por_estacion_detalle, resumen
from .vision.analyzer import TIPOS_ZONA
from .vision.manager import GestorCamaras
from .vision.render import placeholder
from .vision.source import FuenteVideo, probar_fuente

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

hub = HubAlertas()
servicio = ServicioEventos(hub)
gestor = GestorCamaras(servicio)
purga = PurgaAutomatica()
INICIO = time.time()


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    inicializar_bd()
    hub.loop = asyncio.get_running_loop()
    servicio.start()
    purga.start()
    registrar_log("info", "sistema", f"TransmiEye v{VERSION} iniciado.")
    await asyncio.to_thread(gestor.iniciar_todas)
    yield
    purga.detener()
    await asyncio.to_thread(gestor.detener_todas)
    registrar_log("info", "sistema", "TransmiEye detenido.")


app = FastAPI(title="TransmiEye API", version=VERSION, lifespan=ciclo_vida)


# ═════════════════════════════════════════════════════════════════════════════
# Esquemas
# ═════════════════════════════════════════════════════════════════════════════
class LoginIn(BaseModel):
    usuario: str
    password: str


class CambioPassIn(BaseModel):
    actual: str
    nueva: str = Field(min_length=8)


class UsuarioIn(BaseModel):
    nombre: str
    usuario: str
    correo: str
    rol: str
    password: str | None = None
    estacion_asignada: int | None = None
    activo: bool = True


class EstacionIn(BaseModel):
    nombre: str
    linea: str
    localidad: str = ""
    latitud: float | None = None
    longitud: float | None = None
    activa: bool = True


class CamaraIn(BaseModel):
    codigo_camara: str
    estacion_id: int
    fuente: str
    ubicacion: str = ""
    tipo_camara: str = "fija"
    vista: str = "interior"
    activa: bool = True


class ZonasIn(BaseModel):
    roi: dict


class FuenteIn(BaseModel):
    fuente: str


class RevisionIn(BaseModel):
    decision: str           # confirmar | rechazar | pendiente
    notas: str | None = None


class ReporteIn(BaseModel):
    desde: dt.datetime
    hasta: dt.datetime
    estacion_id: int | None = None


def _ip(req: Request):
    return req.client.host if req.client else None


def _404(obj, msg="No encontrado."):
    if obj is None:
        raise HTTPException(404, msg)
    return obj


# ═════════════════════════════════════════════════════════════════════════════
# Autenticación y usuarios
# ═════════════════════════════════════════════════════════════════════════════
def _op_dict(o: Operador):
    return {"id": o.operador_id, "nombre": o.nombre, "usuario": o.usuario, "correo": o.correo, "rol": o.rol,
            "estacion_asignada": o.estacion_asignada, "activo": o.activo,
            "ultimo_acceso": o.ultimo_acceso.isoformat() if o.ultimo_acceso else None}


@app.post("/api/auth/login")
def login(datos: LoginIn, request: Request):
    with SessionLocal() as s:
        op = s.query(Operador).filter(func.lower(Operador.usuario) == datos.usuario.strip().lower()).first()
        if op is None or not op.activo or not auth.verificar_password(datos.password, op.password_hash):
            registrar_acceso(op.operador_id if op else None, datos.usuario[:50], "login_fallido", ip=_ip(request))
            raise HTTPException(401, "Usuario o contraseña incorrectos.")
        op.ultimo_acceso = ahora()
        s.commit()
        registrar_acceso(op.operador_id, op.usuario, "login", ip=_ip(request))
        por_defecto = op.usuario == "admin" and auth.verificar_password("admin123", op.password_hash)
        return {"token": auth.crear_token(op), "usuario": _op_dict(op), "password_por_defecto": por_defecto}


@app.get("/api/auth/yo")
def yo(u: Usuario = requiere()):
    with SessionLocal() as s:
        return _op_dict(_404(s.get(Operador, u.id)))


@app.post("/api/auth/cambiar-password")
def cambiar_password(datos: CambioPassIn, request: Request, u: Usuario = requiere()):
    with SessionLocal() as s:
        op = _404(s.get(Operador, u.id))
        if not auth.verificar_password(datos.actual, op.password_hash):
            raise HTTPException(400, "La contraseña actual no es correcta.")
        op.password_hash = auth.hash_password(datos.nueva)
        s.commit()
    registrar_acceso(u.id, u.usuario, "cambio_password", ip=_ip(request))
    return {"ok": True}


@app.get("/api/usuarios")
def listar_usuarios(u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        return [_op_dict(o) for o in s.query(Operador).order_by(Operador.nombre).all()]


def _validar_usuario(d: UsuarioIn):
    if d.rol not in auth.ROLES:
        raise HTTPException(400, "Rol inválido.")
    if "@" not in d.correo:
        raise HTTPException(400, "Correo inválido.")


@app.post("/api/usuarios")
def crear_usuario(d: UsuarioIn, request: Request, u: Usuario = requiere("admin")):
    _validar_usuario(d)
    if not d.password or len(d.password) < 8:
        raise HTTPException(400, "La contraseña debe tener al menos 8 caracteres.")
    with SessionLocal() as s:
        if s.query(Operador).filter((Operador.usuario == d.usuario) | (Operador.correo == d.correo)).first():
            raise HTTPException(409, "Ya existe un usuario con ese nombre de usuario o correo.")
        op = Operador(nombre=d.nombre, usuario=d.usuario.strip(), correo=d.correo.strip(), rol=d.rol,
                      password_hash=auth.hash_password(d.password), estacion_asignada=d.estacion_asignada,
                      activo=d.activo)
        s.add(op)
        s.commit()
        registrar_acceso(u.id, u.usuario, "crear_usuario", op.usuario, _ip(request))
        return _op_dict(op)


@app.put("/api/usuarios/{oid}")
def editar_usuario(oid: int, d: UsuarioIn, request: Request, u: Usuario = requiere("admin")):
    _validar_usuario(d)
    with SessionLocal() as s:
        op = _404(s.get(Operador, oid))
        if oid == u.id and (not d.activo or d.rol != "admin"):
            raise HTTPException(400, "No puedes desactivar tu propia cuenta ni quitarte el rol de administrador.")
        op.nombre, op.correo, op.rol, op.activo = d.nombre, d.correo, d.rol, d.activo
        op.estacion_asignada = d.estacion_asignada
        if d.password:
            if len(d.password) < 8:
                raise HTTPException(400, "La contraseña debe tener al menos 8 caracteres.")
            op.password_hash = auth.hash_password(d.password)
        s.commit()
        registrar_acceso(u.id, u.usuario, "editar_usuario", op.usuario, _ip(request))
        return _op_dict(op)


# ═════════════════════════════════════════════════════════════════════════════
# Estaciones
# ═════════════════════════════════════════════════════════════════════════════
def _hoy():
    h = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return h, h + dt.timedelta(days=1)


@app.get("/api/estaciones")
def listar_estaciones(u: Usuario = requiere()):
    desde, hasta = _hoy()
    with SessionLocal() as s:
        det = por_estacion_detalle(filas_eventos(s, desde, hasta))
        res = []
        for e in s.query(Estacion).order_by(Estacion.nombre).all():
            cams = e.camaras
            en_linea = sum(1 for c in cams if gestor.info(c.camara_id)["estado"] == "en_linea")
            d = det.get(e.estacion_id, {"total": 0, "ingresos": 0, "confirmados": 0})
            res.append({"id": e.estacion_id, "nombre": e.nombre, "linea": e.linea, "localidad": e.localidad,
                        "latitud": e.latitud, "longitud": e.longitud, "activa": e.activa,
                        "camaras": len(cams), "camaras_en_linea": en_linea,
                        "eventos_hoy": d["total"], "ingresos_hoy": d["ingresos"], "confirmados_hoy": d["confirmados"]})
        return res


@app.post("/api/estaciones")
def crear_estacion(d: EstacionIn, u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        if s.query(Estacion).filter_by(nombre=d.nombre.strip()).first():
            raise HTTPException(409, "Ya existe una estación con ese nombre.")
        e = Estacion(**{**d.model_dump(), "nombre": d.nombre.strip()})
        s.add(e)
        s.commit()
        return {"id": e.estacion_id}


@app.put("/api/estaciones/{eid}")
def editar_estacion(eid: int, d: EstacionIn, u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        e = _404(s.get(Estacion, eid))
        for k, v in d.model_dump().items():
            setattr(e, k, v)
        s.commit()
        return {"ok": True}


@app.delete("/api/estaciones/{eid}")
def borrar_estacion(eid: int, u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        e = _404(s.get(Estacion, eid))
        if e.camaras:
            raise HTTPException(409, "La estación tiene cámaras registradas. Muévelas o elimínalas primero.")
        s.delete(e)
        s.commit()
        return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════
# Cámaras
# ═════════════════════════════════════════════════════════════════════════════
def _cam_dict(c: Camara):
    return {"id": c.camara_id, "codigo": c.codigo_camara, "estacion_id": c.estacion_id,
            "estacion": c.estacion.nombre if c.estacion else None, "fuente": c.fuente, "ubicacion": c.ubicacion,
            "tipo_camara": c.tipo_camara, "vista": c.vista, "resolucion": c.resolucion, "fps": c.fps,
            "activa": c.activa, "roi": c.roi_config or {}, "runtime": gestor.info(c.camara_id)}


@app.get("/api/camaras")
def listar_camaras(u: Usuario = requiere()):
    with SessionLocal() as s:
        return [_cam_dict(c) for c in s.query(Camara).order_by(Camara.codigo_camara).all()]


@app.get("/api/camaras/{cid}")
def ver_camara(cid: int, u: Usuario = requiere()):
    with SessionLocal() as s:
        return _cam_dict(_404(s.get(Camara, cid)))


@app.post("/api/camaras/probar-fuente")
def probar(d: FuenteIn, u: Usuario = requiere("admin")):
    try:
        return {"ok": True, **probar_fuente(d.fuente)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _guardar_camara(s, c: Camara, d: CamaraIn):
    if d.vista not in ("interior", "exterior"):
        raise HTTPException(400, "La vista debe ser «interior» o «exterior».")
    _404(s.get(Estacion, d.estacion_id), "La estación no existe.")
    otra = s.query(Camara).filter(Camara.codigo_camara == d.codigo_camara.strip(), Camara.camara_id != (c.camara_id or 0)).first()
    if otra:
        raise HTTPException(409, "Ya existe una cámara con ese código.")
    for k, v in d.model_dump().items():
        setattr(c, k, v.strip() if isinstance(v, str) else v)


@app.post("/api/camaras")
def crear_camara(d: CamaraIn, request: Request, u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        c = Camara()
        _guardar_camara(s, c, d)
        try:
            info = probar_fuente(c.fuente)
            c.resolucion, c.fps = info["resolucion"], int(info["fps"])
        except Exception:
            pass   # se permite registrar cámaras que aún no están conectadas
        s.add(c)
        s.commit()
        cid, activa = c.camara_id, c.activa
    registrar_acceso(u.id, u.usuario, "crear_camara", d.codigo_camara, _ip(request))
    if activa:
        gestor.iniciar(cid)
    return {"id": cid}


@app.put("/api/camaras/{cid}")
def editar_camara(cid: int, d: CamaraIn, request: Request, u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        c = _404(s.get(Camara, cid))
        fuente_antes = c.fuente
        _guardar_camara(s, c, d)
        s.commit()
        activa = c.activa
    registrar_acceso(u.id, u.usuario, "editar_camara", d.codigo_camara, _ip(request))
    if activa:
        gestor.iniciar(cid)     # recarga fuente, código y estación
    else:
        gestor.detener(cid)
    return {"ok": True, "fuente_cambiada": fuente_antes != d.fuente}


@app.delete("/api/camaras/{cid}")
def borrar_camara(cid: int, request: Request, u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        c = _404(s.get(Camara, cid))
        if s.query(EventoEvasion).filter_by(camara_id=cid).count():
            raise HTTPException(409, "La cámara tiene eventos registrados (evidencia). Desactívala en lugar de eliminarla.")
        gestor.detener(cid)
        codigo = c.codigo_camara
        s.delete(c)
        s.commit()
    registrar_acceso(u.id, u.usuario, "eliminar_camara", codigo, _ip(request))
    return {"ok": True}


@app.post("/api/camaras/{cid}/iniciar")
def iniciar_camara(cid: int, u: Usuario = requiere("supervisor")):
    with SessionLocal() as s:
        c = _404(s.get(Camara, cid))
        c.activa = True
        s.commit()
    gestor.iniciar(cid)
    return {"ok": True}


@app.post("/api/camaras/{cid}/detener")
def detener_camara(cid: int, u: Usuario = requiere("supervisor")):
    with SessionLocal() as s:
        c = _404(s.get(Camara, cid))
        c.activa = False
        s.commit()
    gestor.detener(cid)
    return {"ok": True}


@app.put("/api/camaras/{cid}/zonas")
def guardar_zonas(cid: int, d: ZonasIn, request: Request, u: Usuario = requiere("admin")):
    roi = {}
    for tipo in TIPOS_ZONA:
        polis = []
        for poli in d.roi.get(tipo, []) or []:
            pts = [[round(min(max(float(p[0]), 0), 1), 4), round(min(max(float(p[1]), 0), 1), 4)] for p in poli]
            if len(pts) >= 3:
                polis.append(pts)
        roi[tipo] = polis
    puertas = []
    for pu in d.roi.get("puertas", []) or []:
        try:
            a = [round(min(max(float(pu["a"][0]), 0), 1), 4), round(min(max(float(pu["a"][1]), 0), 1), 4)]
            b = [round(min(max(float(pu["b"][0]), 0), 1), 4), round(min(max(float(pu["b"][1]), 0), 1), 4)]
        except (KeyError, TypeError, ValueError, IndexError):
            raise HTTPException(400, "Línea de puerta inválida.")
        if abs(a[0] - b[0]) + abs(a[1] - b[1]) < 0.01:
            raise HTTPException(400, "Una línea de puerta es demasiado corta.")
        puertas.append({"a": a, "b": b})
    roi["puertas"] = puertas
    with SessionLocal() as s:
        c = _404(s.get(Camara, cid))
        c.roi_config = roi
        s.commit()
        codigo = c.codigo_camara
    gestor.actualizar_roi(cid, roi)
    registrar_acceso(u.id, u.usuario, "configurar_zonas", codigo, _ip(request))
    return {"ok": True, "roi": roi}


_NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/api/camaras/{cid}/frame.jpg")
def frame_camara(cid: int, u: Usuario = requiere()):
    w = gestor.worker(cid)
    datos = w.jpeg_actual() if w else placeholder("Camara detenida")
    return Response(datos, media_type="image/jpeg", headers=_NO_CACHE)


@app.get("/api/camaras/{cid}/snapshot.jpg")
def snapshot_camara(cid: int, u: Usuario = requiere()):
    """Imagen limpia (sin anotaciones) para dibujar las zonas."""
    w = gestor.worker(cid)
    datos = w.snapshot() if w else None
    if datos is None:
        with SessionLocal() as s:
            c = _404(s.get(Camara, cid))
            fuente = c.fuente
        f = FuenteVideo(fuente)
        try:
            f.abrir()
            frame = f.leer(timeout=5)
        except Exception as e:
            raise HTTPException(409, f"No se pudo obtener imagen de la cámara: {e}")
        finally:
            f.cerrar()
        if frame is None:
            raise HTTPException(409, "La cámara no entregó imagen.")
        from .vision.render import escalar_ancho, jpeg
        datos = jpeg(escalar_ancho(frame, AJUSTES.get()["ancho_proceso"]), 90)
    return Response(datos, media_type="image/jpeg", headers=_NO_CACHE)


@app.get("/api/camaras/{cid}/stream")
def stream_camara(cid: int, u: Usuario = requiere()):
    """MJPEG para la vista ampliada (para mosaicos usar frame.jpg y no agotar conexiones del navegador)."""
    def generar():
        ultimo = None
        while True:
            w = gestor.worker(cid)
            datos = w.jpeg_actual() if w else placeholder("Camara detenida")
            if datos is not ultimo:
                ultimo = datos
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + datos + b"\r\n"
            time.sleep(0.05)
    return StreamingResponse(generar(), media_type="multipart/x-mixed-replace; boundary=frame", headers=_NO_CACHE)


@app.get("/api/videos")
def listar_videos(u: Usuario = requiere()):
    ext = {".mp4", ".avi", ".mkv", ".mov", ".m4v", ".webm"}
    return [{"nombre": f.name, "mb": round(f.stat().st_size / 1e6, 1)}
            for f in sorted(VIDEOS_DIR.iterdir()) if f.suffix.lower() in ext]


@app.post("/api/videos")
def subir_video(archivo: UploadFile = File(...), u: Usuario = requiere("admin")):
    nombre = "".join(ch for ch in (archivo.filename or "video.mp4") if ch.isalnum() or ch in "._- ").strip()
    if not nombre.lower().endswith((".mp4", ".avi", ".mkv", ".mov", ".m4v", ".webm")):
        raise HTTPException(400, "Formato no soportado. Usa mp4, avi, mkv, mov o webm.")
    destino = VIDEOS_DIR / nombre
    with destino.open("wb") as f:
        shutil.copyfileobj(archivo.file, f)
    registrar_log("info", "videos", f"Video subido: {nombre} por {u.usuario}")
    return {"nombre": nombre, "fuente": f"videos/{nombre}"}


# ═════════════════════════════════════════════════════════════════════════════
# Eventos (historial, revisión, evidencia)
# ═════════════════════════════════════════════════════════════════════════════
def _ev_dict(e: EventoEvasion, alerta: Alerta | None = None):
    tiene = bool(e.frame_referencia) and not e.purgado
    return {"id": e.evento_id, "uid": e.uid, "timestamp": e.timestamp_evento.isoformat(),
            "camara_id": e.camara_id, "camara": e.camara.codigo_camara,
            "estacion_id": e.camara.estacion_id, "estacion": e.camara.estacion.nombre,
            "tipo": e.tipo.codigo, "tipo_nombre": e.tipo.nombre, "nivel_riesgo": e.tipo.nivel_riesgo,
            "confianza": e.confianza, "track_id": e.track_id, "detalle": e.detalle, "bbox": e.coordenadas_bbox,
            "confirmado": e.confirmado, "revisor": e.revisor.nombre if e.revisor else None,
            "fecha_confirmacion": e.fecha_confirmacion.isoformat() if e.fecha_confirmacion else None,
            "notas": e.notas, "tiene_imagen": tiene, "clip_frames": e.clip_frames if tiene else 0,
            "hash_sha256": e.hash_sha256, "purgado": e.purgado}


def _fecha(txt, fin=False):
    if not txt:
        return None
    try:
        d = dt.datetime.fromisoformat(txt)
    except ValueError:
        raise HTTPException(400, f"Fecha inválida: {txt}")
    if fin and len(txt) <= 10:
        d += dt.timedelta(days=1)
    return d


def _consulta_eventos(s, desde, hasta, estacion_id, camara_id, tipo, estado):
    q = s.query(EventoEvasion).join(Camara).join(TipoEvasion)
    if d := _fecha(desde):
        q = q.filter(EventoEvasion.timestamp_evento >= d)
    if h := _fecha(hasta, fin=True):
        q = q.filter(EventoEvasion.timestamp_evento < h)
    if estacion_id:
        q = q.filter(Camara.estacion_id == estacion_id)
    if camara_id:
        q = q.filter(EventoEvasion.camara_id == camara_id)
    if tipo:
        q = q.filter(TipoEvasion.codigo == tipo)
    if estado == "pendiente":
        q = q.filter(EventoEvasion.confirmado.is_(None))
    elif estado == "confirmado":
        q = q.filter(EventoEvasion.confirmado.is_(True))
    elif estado == "rechazado":
        q = q.filter(EventoEvasion.confirmado.is_(False))
    return q.order_by(EventoEvasion.timestamp_evento.desc(), EventoEvasion.evento_id.desc())


@app.get("/api/eventos")
def listar_eventos(desde: str | None = None, hasta: str | None = None, estacion_id: int | None = None,
                   camara_id: int | None = None, tipo: str | None = None, estado: str | None = None,
                   pagina: int = Query(1, ge=1), tam: int = Query(25, ge=1, le=200), u: Usuario = requiere()):
    with SessionLocal() as s:
        q = _consulta_eventos(s, desde, hasta, estacion_id, camara_id, tipo, estado)
        total = q.count()
        items = [_ev_dict(e) for e in q.offset((pagina - 1) * tam).limit(tam).all()]
        return {"total": total, "pagina": pagina, "tam": tam, "items": items}


@app.get("/api/eventos/export.csv")
def exportar_csv(request: Request, desde: str | None = None, hasta: str | None = None,
                 estacion_id: int | None = None, camara_id: int | None = None, tipo: str | None = None,
                 estado: str | None = None, u: Usuario = requiere()):
    with SessionLocal() as s:
        filas = _consulta_eventos(s, desde, hasta, estacion_id, camara_id, tipo, estado).all()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["id", "fecha_hora", "estacion", "camara", "tipo", "confianza", "estado", "revisor", "detalle", "sha256"])
        est = {True: "confirmado", False: "rechazado", None: "pendiente"}
        for e in filas:
            w.writerow([e.evento_id, e.timestamp_evento.isoformat(sep=" "), e.camara.estacion.nombre,
                        e.camara.codigo_camara, e.tipo.nombre, f"{e.confianza:.4f}", est[e.confirmado],
                        e.revisor.nombre if e.revisor else "", e.detalle or "", e.hash_sha256 or ""])
    registrar_acceso(u.id, u.usuario, "exportar_csv", f"{len(filas)} filas", _ip(request))
    nombre = f"transmieye_eventos_{dt.datetime.now():%Y%m%d_%H%M}.csv"
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@app.get("/api/eventos/{eid}")
def ver_evento(eid: int, u: Usuario = requiere()):
    with SessionLocal() as s:
        return _ev_dict(_404(s.get(EventoEvasion, eid)))


def _carpeta_evento(eid):
    with SessionLocal() as s:
        e = _404(s.get(EventoEvasion, eid))
        if e.purgado or not e.frame_referencia:
            raise HTTPException(410, "La imagen fue eliminada por la política de retención.")
        return EVID_DIR / e.frame_referencia


@app.get("/api/eventos/{eid}/imagen")
def imagen_evento(eid: int, u: Usuario = requiere()):
    f = _carpeta_evento(eid) / "evento.jpg"
    if not f.exists():
        raise HTTPException(404, "Imagen no disponible.")
    return FileResponse(f, media_type="image/jpeg")


@app.get("/api/eventos/{eid}/clip/{n}")
def cuadro_clip(eid: int, n: int, u: Usuario = requiere()):
    f = _carpeta_evento(eid) / "clip" / f"{n:04d}.jpg"
    if not f.exists():
        raise HTTPException(404, "Cuadro no disponible.")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.post("/api/eventos/{eid}/revision")
def revisar_evento(eid: int, d: RevisionIn, request: Request, u: Usuario = requiere("supervisor")):
    valores = {"confirmar": True, "rechazar": False, "pendiente": None}
    if d.decision not in valores:
        raise HTTPException(400, "Decisión inválida.")
    with SessionLocal() as s:
        e = _404(s.get(EventoEvasion, eid))
        e.confirmado = valores[d.decision]
        e.operador_confirmacion = u.id if d.decision != "pendiente" else None
        e.fecha_confirmacion = ahora() if d.decision != "pendiente" else None
        e.notas = d.notas
        a = s.query(Alerta).filter_by(evento_id=eid).first()
        if a and a.estado == "enviada" and d.decision != "pendiente":
            a.estado = "atendida" if d.decision == "confirmar" else "descartada"
            a.operador_receptor, a.fecha_atencion = u.id, ahora()
        s.commit()
        r = _ev_dict(e)
    registrar_acceso(u.id, u.usuario, f"revision_{d.decision}", f"evento {eid}", _ip(request))
    hub.publicar({"tipo": "revision", "evento_id": eid})
    return r


@app.get("/api/eventos/{eid}/evidencia.zip")
def exportar_evidencia(eid: int, request: Request, u: Usuario = requiere("supervisor")):
    carpeta = _carpeta_evento(eid)
    with SessionLocal() as s:
        e = _404(s.get(EventoEvasion, eid))
        meta = _ev_dict(e)
    imagen = (carpeta / "evento.jpg").read_bytes() if (carpeta / "evento.jpg").exists() else b""
    sha_actual = hashlib.sha256(imagen).hexdigest()
    integra = sha_actual == meta["hash_sha256"]
    buf = io.BytesIO()
    lineas = [f"Cadena de custodia · Evento #{eid}",
              f"Exportado por: {u.nombre} ({u.usuario}) el {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
              f"Estación: {meta['estacion']} · Cámara: {meta['camara']}",
              f"Fecha del evento: {meta['timestamp']}",
              f"SHA-256 registrado al detectar: {meta['hash_sha256']}",
              f"SHA-256 al exportar:            {sha_actual}",
              f"Integridad de la imagen: {'VERIFICADA' if integra else 'NO COINCIDE (posible alteración)'}",
              "", "Archivos incluidos (SHA-256):"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        archivos = [("evento.jpg", imagen)]
        clip = carpeta / "clip"
        if clip.exists():
            archivos += [(f"clip/{f.name}", f.read_bytes()) for f in sorted(clip.glob("*.jpg"))]
        for nombre, contenido in archivos:
            z.writestr(nombre, contenido)
            lineas.append(f"  {hashlib.sha256(contenido).hexdigest()}  {nombre}")
        z.writestr("metadata.json", json.dumps({**meta, "integridad_verificada": integra}, ensure_ascii=False, indent=2))
        z.writestr("cadena_custodia.txt", "\n".join(lineas))
    registrar_acceso(u.id, u.usuario, "exportar_evidencia", f"evento {eid}", _ip(request))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="evidencia_evento_{eid}.zip"'})


# ═════════════════════════════════════════════════════════════════════════════
# Alertas
# ═════════════════════════════════════════════════════════════════════════════
def _alerta_dict(a: Alerta):
    return {"id": a.alerta_id, "nivel": a.nivel, "estado": a.estado, "timestamp": a.timestamp_alerta.isoformat(),
            "receptor": a.receptor.nombre if a.receptor else None,
            "fecha_atencion": a.fecha_atencion.isoformat() if a.fecha_atencion else None,
            "evento": _ev_dict(a.evento)}


@app.get("/api/alertas")
def listar_alertas(estado: str = "activas", limite: int = Query(100, ge=1, le=500), u: Usuario = requiere()):
    with SessionLocal() as s:
        q = s.query(Alerta)
        if estado == "activas":
            q = q.filter(Alerta.estado == "enviada")
        elif estado in ("atendida", "descartada"):
            q = q.filter(Alerta.estado == estado)
        items = [_alerta_dict(a) for a in q.order_by(Alerta.timestamp_alerta.desc(), Alerta.alerta_id.desc()).limit(limite)]
        conteo = dict(s.query(Alerta.nivel, func.count()).filter(Alerta.estado == "enviada").group_by(Alerta.nivel).all())
        desde, _ = _hoy()
        atendidas_hoy = s.query(Alerta).filter(Alerta.estado != "enviada", Alerta.fecha_atencion >= desde).count()
        return {"items": items, "activas": conteo, "atendidas_hoy": atendidas_hoy}


def _cerrar_alerta(aid, estado, u, request):
    with SessionLocal() as s:
        a = _404(s.get(Alerta, aid))
        a.estado, a.operador_receptor, a.fecha_atencion = estado, u.id, ahora()
        s.commit()
    registrar_acceso(u.id, u.usuario, f"alerta_{estado}", f"alerta {aid}", _ip(request))
    hub.publicar({"tipo": "alerta_cerrada", "alerta_id": aid})
    return {"ok": True}


@app.post("/api/alertas/{aid}/atender")
def atender_alerta(aid: int, request: Request, u: Usuario = requiere("supervisor")):
    return _cerrar_alerta(aid, "atendida", u, request)


@app.post("/api/alertas/{aid}/descartar")
def descartar_alerta(aid: int, request: Request, u: Usuario = requiere("supervisor")):
    return _cerrar_alerta(aid, "descartada", u, request)


# ═════════════════════════════════════════════════════════════════════════════
# Estadísticas
# ═════════════════════════════════════════════════════════════════════════════
@app.get("/api/stats/resumen")
def stats_resumen(u: Usuario = requiere()):
    desde, hasta = _hoy()
    with SessionLocal() as s:
        r = resumen(filas_eventos(s, desde, hasta))
        rev = dict(s.query(EventoEvasion.confirmado, func.count()).filter(EventoEvasion.confirmado.isnot(None))
                   .group_by(EventoEvasion.confirmado).all())
        n_cams = s.query(Camara).count()
        alertas = s.query(Alerta).filter(Alerta.estado == "enviada").count()
    conf, rech = rev.get(True, 0), rev.get(False, 0)
    infos = [w.info() for w in list(gestor.workers.values())]
    return {**r, "camaras_total": n_cams, "camaras_en_linea": sum(1 for i in infos if i["estado"] == "en_linea"),
            "alertas_activas": alertas,
            "precision_global": round(conf / (conf + rech), 4) if conf + rech else None,
            "revisados_global": conf + rech,
            "fps_promedio": round(sum(i["fps"] for i in infos) / len(infos), 1) if infos else None,
            "latencia_promedio_ms": round(sum(i["latencia_ms"] for i in infos) / len(infos), 1) if infos else None}


@app.get("/api/stats/diario")
def stats_diario(dias: int = Query(14, ge=1, le=120), u: Usuario = requiere()):
    _, hasta = _hoy()
    desde = hasta - dt.timedelta(days=dias)
    with SessionLocal() as s:
        r = resumen(filas_eventos(s, desde, hasta))
    serie = []
    for i in range(dias):
        d = (desde + dt.timedelta(days=i)).date().isoformat()
        serie.append({"fecha": d, "total": r["por_dia"].get(d, 0)})
    return serie


@app.get("/api/stats/horario")
def stats_horario(dias: int = Query(1, ge=1, le=120), u: Usuario = requiere()):
    _, hasta = _hoy()
    with SessionLocal() as s:
        r = resumen(filas_eventos(s, hasta - dt.timedelta(days=dias), hasta))
    return {"por_hora": r["por_hora"], "dias": dias}


# ═════════════════════════════════════════════════════════════════════════════
# Reportes
# ═════════════════════════════════════════════════════════════════════════════
def _rep_dict(r: Reporte):
    return {"id": r.reporte_id, "desde": r.fecha_inicio.isoformat(), "hasta": r.fecha_fin.isoformat(),
            "estacion": r.estacion.nombre if r.estacion else "Todas", "total_eventos": r.total_eventos,
            "generado": r.fecha_generacion.isoformat(), "operador": r.operador.nombre if r.operador else None,
            "disponible": bool(r.ruta_archivo) and (REPORT_DIR / r.ruta_archivo).exists()}


@app.get("/api/reportes")
def listar_reportes(u: Usuario = requiere()):
    with SessionLocal() as s:
        return [_rep_dict(r) for r in s.query(Reporte).order_by(Reporte.fecha_generacion.desc()).limit(100)]


@app.post("/api/reportes")
def crear_reporte(d: ReporteIn, request: Request, u: Usuario = requiere("supervisor")):
    if d.hasta <= d.desde:
        raise HTTPException(400, "La fecha final debe ser posterior a la inicial.")
    with SessionLocal() as s:
        r = generar_reporte(s, d.desde, d.hasta, d.estacion_id, u)
        res = _rep_dict(r)
    registrar_acceso(u.id, u.usuario, "generar_reporte", f"reporte {res['id']}", _ip(request))
    return res


@app.get("/api/reportes/{rid}/pdf")
def descargar_reporte(rid: int, u: Usuario = requiere()):
    with SessionLocal() as s:
        r = _404(s.get(Reporte, rid))
        f = REPORT_DIR / (r.ruta_archivo or "")
    if not f.is_file():
        raise HTTPException(404, "El archivo del reporte ya no existe.")
    return FileResponse(f, media_type="application/pdf", filename=f.name)


# ═════════════════════════════════════════════════════════════════════════════
# Configuración, sistema y registros
# ═════════════════════════════════════════════════════════════════════════════
MODELOS_CONOCIDOS = ["yolov8n.pt", "yolov8s.pt", "yolov8m.pt", "yolo11n.pt", "yolo11s.pt", "yolo11m.pt"]


@app.get("/api/config")
def ver_config(u: Usuario = requiere()):
    locales = sorted(f.name for f in MODELS_DIR.glob("*.pt"))
    return {"ajustes": AJUSTES.get(), "modelos": sorted(set(MODELOS_CONOCIDOS) | set(locales)),
            "modelos_descargados": locales}


@app.put("/api/config")
def editar_config(cambios: dict, request: Request, u: Usuario = requiere("admin")):
    try:
        modificadas = AJUSTES.actualizar(cambios)
    except ValueError as e:
        raise HTTPException(400, str(e))
    guardar_ajustes(AJUSTES.get())
    if modificadas:
        registrar_acceso(u.id, u.usuario, "editar_configuracion", ", ".join(sorted(modificadas)), _ip(request))
    reinicio = bool(modificadas & CLAVES_REINICIO)
    if reinicio:
        gestor.reiniciar_todas()
    return {"ajustes": AJUSTES.get(), "modificadas": sorted(modificadas), "camaras_reiniciadas": reinicio}


@app.get("/api/sistema")
def sistema(u: Usuario = requiere()):
    info = {"version": VERSION, "opencv": cv2.__version__, "base_datos": DATABASE_URL.split(":")[0],
            "activo_desde": INICIO, "camaras_activas": len(gestor.workers), "clientes_ws": len(hub.clientes)}
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda"] = torch.cuda.is_available()
        info["gpu"] = torch.cuda.get_device_name(0) if info["cuda"] else None
    except Exception:
        info.update(torch=None, cuda=False, gpu=None)
    try:
        import ultralytics
        info["ultralytics"] = ultralytics.__version__
    except Exception:
        info["ultralytics"] = None
    tam = sum(f.stat().st_size for f in EVID_DIR.rglob("*") if f.is_file())
    info["evidencias_mb"] = round(tam / 1e6, 1)
    info["disco_libre_gb"] = round(shutil.disk_usage(EVID_DIR).free / 1e9, 1)
    with SessionLocal() as s:
        fila = s.get(Configuracion, "ultima_purga")
        info["ultima_purga"] = fila.valor if fila else None
    return info


@app.post("/api/mantenimiento/purgar")
def purgar_ahora(u: Usuario = requiere("admin")):
    return purgar(origen=f"manual ({u.usuario})")


@app.get("/api/logs/tecnicos")
def logs_tecnicos(nivel: str | None = None, limite: int = Query(200, le=1000), u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        q = s.query(LogTecnico)
        if nivel:
            q = q.filter(LogTecnico.nivel == nivel)
        return [{"timestamp": l.timestamp.isoformat(), "nivel": l.nivel, "origen": l.origen, "mensaje": l.mensaje}
                for l in q.order_by(LogTecnico.log_id.desc()).limit(limite)]


@app.get("/api/logs/accesos")
def logs_accesos(limite: int = Query(200, le=1000), u: Usuario = requiere("admin")):
    with SessionLocal() as s:
        return [{"timestamp": l.timestamp.isoformat(), "usuario": l.usuario, "accion": l.accion,
                 "detalle": l.detalle, "ip": l.ip}
                for l in s.query(LogAcceso).order_by(LogAcceso.log_id.desc()).limit(limite)]


@app.get("/api/tipos-evento")
def tipos_evento(u: Usuario = requiere()):
    with SessionLocal() as s:
        return [{"codigo": t.codigo, "nombre": t.nombre, "descripcion": t.descripcion, "nivel_riesgo": t.nivel_riesgo}
                for t in s.query(TipoEvasion).order_by(TipoEvasion.nivel_riesgo.desc())]


# ═════════════════════════════════════════════════════════════════════════════
# WebSocket de alertas en tiempo real
# ═════════════════════════════════════════════════════════════════════════════
@app.websocket("/ws")
async def ws_alertas(ws: WebSocket):
    try:
        auth.decodificar(ws.query_params.get("token", ""))
    except HTTPException:
        await ws.close(code=4401)
        return
    await hub.conectar(ws)
    try:
        while True:
            await ws.receive_text()      # el cliente envía «ping» periódicamente
    except WebSocketDisconnect:
        pass
    finally:
        hub.desconectar(ws)


# Interfaz web (debe montarse al final)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
