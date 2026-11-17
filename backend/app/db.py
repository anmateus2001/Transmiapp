"""Modelo relacional de TransmiEye (sección 9.2.7 del documento) sobre SQLAlchemy.

Funciona con SQLite (por defecto) y con PostgreSQL cambiando TRANSMIEYE_DB.
"""
import datetime as dt
import logging

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON,
                        SmallInteger, String, Text, create_engine, event)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .settings import AJUSTES, DATABASE_URL

log = logging.getLogger("transmieye")

_es_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15} if _es_sqlite else {},
    pool_pre_ping=True,
)

if _es_sqlite:
    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=8000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
BigId = BigInteger().with_variant(Integer, "sqlite")   # autoincremento en SQLite


def ahora() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


class Estacion(Base):
    __tablename__ = "estacion"
    estacion_id = Column(Integer, primary_key=True)
    nombre = Column(String(100), nullable=False, unique=True)
    linea = Column(String(50), nullable=False)
    localidad = Column(String(50), nullable=False, default="")
    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)
    activa = Column(Boolean, default=True)
    fecha_registro = Column(DateTime, default=ahora)
    camaras = relationship("Camara", back_populates="estacion")


class Camara(Base):
    __tablename__ = "camara"
    camara_id = Column(Integer, primary_key=True)
    estacion_id = Column(Integer, ForeignKey("estacion.estacion_id"), nullable=False)
    codigo_camara = Column(String(50), nullable=False, unique=True)
    fuente = Column(String(500), nullable=False)          # índice webcam, rtsp://..., http://..., o archivo
    ubicacion = Column(String(200), nullable=False, default="")
    tipo_camara = Column(String(50), nullable=False, default="fija")
    vista = Column(String(20), nullable=False, default="interior")   # desde dónde mira: interior | exterior
    resolucion = Column(String(20), nullable=True)
    fps = Column(SmallInteger, nullable=True)
    roi_config = Column(JSON, nullable=True)              # {"exterior": [[[x,y],..]], "interior": [...], "bahia": [...]}
    activa = Column(Boolean, default=True)
    fecha_registro = Column(DateTime, default=ahora)
    estacion = relationship("Estacion", back_populates="camaras")


class TipoEvasion(Base):
    __tablename__ = "tipo_evasion"
    tipo_id = Column(Integer, primary_key=True)
    codigo = Column(String(40), nullable=False, unique=True)
    nombre = Column(String(100), nullable=False, unique=True)
    descripcion = Column(Text, nullable=True)
    nivel_riesgo = Column(SmallInteger, nullable=False)   # 1 (bajo) a 5 (alto)


class Operador(Base):
    __tablename__ = "operador"
    operador_id = Column(Integer, primary_key=True)
    nombre = Column(String(100), nullable=False)
    usuario = Column(String(50), nullable=False, unique=True)
    correo = Column(String(150), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    rol = Column(String(30), nullable=False)              # admin | supervisor | visor
    estacion_asignada = Column(Integer, ForeignKey("estacion.estacion_id", ondelete="SET NULL"), nullable=True)
    activo = Column(Boolean, default=True)
    ultimo_acceso = Column(DateTime, nullable=True)


class EventoEvasion(Base):
    __tablename__ = "evento_evasion"
    evento_id = Column(BigId, primary_key=True)
    uid = Column(String(32), nullable=False, unique=True)
    camara_id = Column(Integer, ForeignKey("camara.camara_id", ondelete="CASCADE"), nullable=False)
    tipo_id = Column(Integer, ForeignKey("tipo_evasion.tipo_id"), nullable=False)
    timestamp_evento = Column(DateTime, nullable=False, index=True)
    confianza = Column(Float, nullable=False)
    track_id = Column(Integer, nullable=True)
    frame_referencia = Column(String(300), nullable=True)  # carpeta relativa de la evidencia
    hash_sha256 = Column(String(64), nullable=True)
    clip_frames = Column(Integer, default=0)
    coordenadas_bbox = Column(JSON, nullable=True)        # [x1,y1,x2,y2] normalizado
    detalle = Column(Text, nullable=True)
    confirmado = Column(Boolean, nullable=True, default=None)   # NULL = pendiente
    operador_confirmacion = Column(Integer, ForeignKey("operador.operador_id", ondelete="SET NULL"), nullable=True)
    fecha_confirmacion = Column(DateTime, nullable=True)
    notas = Column(Text, nullable=True)
    purgado = Column(Boolean, default=False)
    camara = relationship("Camara")
    tipo = relationship("TipoEvasion")
    revisor = relationship("Operador")


class Alerta(Base):
    __tablename__ = "alerta"
    alerta_id = Column(BigId, primary_key=True)
    evento_id = Column(BigInteger, ForeignKey("evento_evasion.evento_id", ondelete="CASCADE"), nullable=False, unique=True)
    canal = Column(String(30), nullable=False, default="pantalla")
    nivel = Column(String(10), nullable=False)            # critica | media | baja
    timestamp_alerta = Column(DateTime, default=ahora, index=True)
    estado = Column(String(20), default="enviada")        # enviada | atendida | descartada
    operador_receptor = Column(Integer, ForeignKey("operador.operador_id", ondelete="SET NULL"), nullable=True)
    fecha_atencion = Column(DateTime, nullable=True)
    evento = relationship("EventoEvasion")
    receptor = relationship("Operador")


class Reporte(Base):
    __tablename__ = "reporte"
    reporte_id = Column(Integer, primary_key=True)
    operador_id = Column(Integer, ForeignKey("operador.operador_id", ondelete="SET NULL"), nullable=True)
    estacion_id = Column(Integer, ForeignKey("estacion.estacion_id", ondelete="SET NULL"), nullable=True)
    fecha_inicio = Column(DateTime, nullable=False)
    fecha_fin = Column(DateTime, nullable=False)
    total_eventos = Column(Integer, default=0)
    ruta_archivo = Column(String(300), nullable=True)
    fecha_generacion = Column(DateTime, default=ahora)
    operador = relationship("Operador")
    estacion = relationship("Estacion")


class LogTecnico(Base):
    __tablename__ = "log_tecnico"
    log_id = Column(BigId, primary_key=True)
    timestamp = Column(DateTime, default=ahora, index=True)
    nivel = Column(String(10), nullable=False)            # info | aviso | error
    origen = Column(String(60), nullable=False)
    mensaje = Column(Text, nullable=False)


class LogAcceso(Base):
    __tablename__ = "log_acceso"
    log_id = Column(BigId, primary_key=True)
    timestamp = Column(DateTime, default=ahora, index=True)
    operador_id = Column(Integer, ForeignKey("operador.operador_id", ondelete="SET NULL"), nullable=True)
    usuario = Column(String(50), nullable=True)
    accion = Column(String(60), nullable=False)
    detalle = Column(Text, nullable=True)
    ip = Column(String(60), nullable=True)


class Configuracion(Base):
    __tablename__ = "configuracion"
    clave = Column(String(60), primary_key=True)
    valor = Column(JSON, nullable=True)


# ── utilidades ───────────────────────────────────────────────────────────────
def registrar_log(nivel: str, origen: str, mensaje: str):
    getattr(log, {"error": "error", "aviso": "warning"}.get(nivel, "info"))(f"[{origen}] {mensaje}")
    try:
        with SessionLocal() as s:
            s.add(LogTecnico(nivel=nivel, origen=origen[:60], mensaje=mensaje))
            s.commit()
    except Exception as e:  # el log nunca debe tumbar al llamador
        log.error(f"No se pudo guardar log técnico: {e}")


def registrar_acceso(operador_id, usuario, accion, detalle=None, ip=None):
    try:
        with SessionLocal() as s:
            s.add(LogAcceso(operador_id=operador_id, usuario=usuario, accion=accion, detalle=detalle, ip=ip))
            s.commit()
    except Exception as e:
        log.error(f"No se pudo guardar log de acceso: {e}")


def guardar_ajustes(datos: dict):
    with SessionLocal() as s:
        fila = s.get(Configuracion, "deteccion")
        if fila is None:
            s.add(Configuracion(clave="deteccion", valor=datos))
        else:
            fila.valor = datos
        s.commit()


TIPOS_INICIALES = [
    ("INGRESO_LATERAL", "Ingreso por puerta lateral",
     "Persona que pasa del carril exclusivo a la plataforma por una puerta de abordaje sin que haya un bus detenido.", 5),
    ("PERSONA_CARRIL", "Persona en el carril exclusivo",
     "Persona que permanece en la vía frente a las puertas de la estación sin bus presente. Suele preceder a un ingreso irregular.", 4),
    ("SALIDA_LATERAL", "Salida por puerta lateral",
     "Persona que sale de la plataforma hacia el carril por una puerta de abordaje sin bus.", 3),
    ("AGLOMERACION", "Aglomeración en puertas",
     "Número de personas en la zona de puertas por encima del aforo configurado durante al menos 5 segundos.", 2),
]

# Coordenadas aproximadas; edítalas desde la página Estaciones.
ESTACIONES_INICIALES = [
    ("Héroes", "Autonorte (B)", "Chapinero", 4.6667, -74.0573),
    ("Calle 72", "Caracas (A)", "Chapinero", 4.6578, -74.0617),
    ("Marly", "Caracas (A)", "Chapinero", 4.6367, -74.0660),
    ("Calle 100", "Autonorte (B)", "Usaquén", 4.6853, -74.0567),
    ("Portal Norte", "Autonorte (B)", "Usaquén", 4.7544, -74.0458),
    ("Ricaurte", "NQS Central (E)", "Los Mártires", 4.6139, -74.0897),
    ("Portal Sur", "NQS Sur (G)", "Bosa", 4.5961, -74.1720),
]


def inicializar_bd():
    from .auth import hash_password   # import tardío para evitar ciclos
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        if not s.query(TipoEvasion).count():
            for codigo, nombre, desc, riesgo in TIPOS_INICIALES:
                s.add(TipoEvasion(codigo=codigo, nombre=nombre, descripcion=desc, nivel_riesgo=riesgo))
        if not s.query(Estacion).count():
            for nombre, linea, loc, lat, lon in ESTACIONES_INICIALES:
                s.add(Estacion(nombre=nombre, linea=linea, localidad=loc, latitud=lat, longitud=lon))
        if not s.query(Operador).count():
            s.add(Operador(nombre="Administrador", usuario="admin", correo="admin@transmieye.local",
                           password_hash=hash_password("admin123"), rol="admin"))
        fila = s.get(Configuracion, "deteccion")
        if fila is not None:
            AJUSTES.cargar(fila.valor)
        s.commit()
