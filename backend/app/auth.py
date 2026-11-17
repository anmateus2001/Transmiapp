"""Autenticación con contraseñas bcrypt y tokens JWT; roles admin > supervisor > visor."""
import datetime as dt

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

from .settings import JWT_HORAS, SECRET_KEY

ROLES = ("admin", "supervisor", "visor")
_JERARQUIA = {"visor": 1, "supervisor": 2, "admin": 3}


def hash_password(clave: str) -> str:
    return bcrypt.hashpw(clave.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verificar_password(clave: str, hash_: str) -> bool:
    try:
        return bcrypt.checkpw(clave.encode("utf-8"), hash_.encode())
    except ValueError:
        return False


def crear_token(op) -> str:
    payload = {
        "sub": str(op.operador_id),
        "usr": op.usuario,
        "rol": op.rol,
        "nom": op.nombre,
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=JWT_HORAS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decodificar(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "La sesión expiró. Ingresa de nuevo.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido.")


def _token_de(request: Request) -> str:
    cab = request.headers.get("Authorization", "")
    if cab.startswith("Bearer "):
        return cab[7:]
    # <img> y WebSocket no pueden enviar cabeceras: se acepta ?token=
    t = request.query_params.get("token")
    if t:
        return t
    raise HTTPException(401, "Se requiere iniciar sesión.")


class Usuario:
    def __init__(self, d: dict):
        self.id = int(d["sub"])
        self.usuario = d["usr"]
        self.rol = d["rol"]
        self.nombre = d.get("nom", d["usr"])

    def puede(self, rol_minimo: str) -> bool:
        return _JERARQUIA[self.rol] >= _JERARQUIA[rol_minimo]


def requiere(rol_minimo: str = "visor"):
    def dep(request: Request) -> Usuario:
        u = Usuario(decodificar(_token_de(request)))
        if not u.puede(rol_minimo):
            raise HTTPException(403, f"Esta acción requiere rol {rol_minimo} o superior.")
        return u
    return Depends(dep)
