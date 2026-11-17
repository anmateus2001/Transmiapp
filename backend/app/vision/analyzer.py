"""Reglas de evasión por puertas laterales.

Configuración por cámara (coordenadas normalizadas 0..1):
  exterior  → polígonos del carril exclusivo («Fuera», rojo)
  interior  → polígonos de la plataforma («Dentro», verde)
  bahia     → (opcional) dónde se detiene el bus; si no existe se usa el carril
  puertas   → (recomendado) líneas sobre el umbral de cada puerta: {"a": [x, y], "b": [x, y]}.
              El sentido de INGRESO apunta a la derecha del trazo a→b tal como se ve en pantalla
              (y hacia abajo); se dibuja como flecha sobre el video y se invierte intercambiando a y b.

Referencia de cada persona: sus pies (centro inferior de la caja).

Dos detectores de cruce, complementarios (un cruce se cuenta una sola vez por persona):
  • Por zonas: la persona se confirma en el carril y luego en la plataforma.
  • Por línea: los pies quedan a un lado de la puerta y luego al otro, con una profundidad mínima
    relativa a la altura de la persona. Detecta cruces rápidos vistos en muy pocos cuadros.

Robustez:
  • Empalme de pistas: si el tracker cambia el ID de una persona al pasar el marco de la puerta,
    la pista nueva hereda el historial de la que se perdió cerca, hace poco y con altura parecida.
  • Bus en la puerta o recién salido (gracia) → el cruce es un pasajero normal y solo se cuenta.
  • Ruido: un solo cuadro al otro lado no confirma nada.

Eventos: INGRESO_LATERAL (crítico), SALIDA_LATERAL, PERSONA_CARRIL, AGLOMERACION.
"""
import time
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

TIPOS_ZONA = ("exterior", "interior", "bahia")
NUNCA = -1e9


class Puerta:
    """Segmento sobre el umbral de una puerta. Profundidad > 0 = lado de la plataforma."""

    def __init__(self, a, b):
        self.a = np.asarray(a, dtype=float)
        self.b = np.asarray(b, dtype=float)
        d = self.b - self.a
        self.largo = max(float(np.hypot(*d)), 1.0)
        self.dir = d / self.largo
        self.normal = np.array([-self.dir[1], self.dir[0]])     # apunta hacia la plataforma

    @property
    def centro(self):
        return (self.a + self.b) / 2

    def medir(self, punto, alto):
        """(profundidad en alturas de persona, posición a lo largo de la puerta 0..1)."""
        v = np.asarray(punto, dtype=float) - self.a
        prof = (self.dir[0] * v[1] - self.dir[1] * v[0]) / max(alto, 1.0)
        return prof, float(v @ self.dir) / self.largo


class Zonas:
    def __init__(self, roi: dict | None, ancho: int, alto: int):
        self.w, self.h = ancho, alto
        self.poligonos = {t: [] for t in TIPOS_ZONA}
        self.mascaras, self.areas = {}, {}
        roi = roi or {}
        for tipo in TIPOS_ZONA:
            for poli in roi.get(tipo, []) or []:
                if len(poli) >= 3:
                    self.poligonos[tipo].append(np.array([[p[0] * ancho, p[1] * alto] for p in poli], dtype=np.int32))
            if self.poligonos[tipo]:
                m = np.zeros((alto, ancho), dtype=np.uint8)
                cv2.fillPoly(m, self.poligonos[tipo], 1)
                self.mascaras[tipo] = m
                self.areas[tipo] = max(int(m.sum()), 1)
        self.puertas = [Puerta((p["a"][0] * ancho, p["a"][1] * alto), (p["b"][0] * ancho, p["b"][1] * alto))
                        for p in roi.get("puertas", []) or [] if p.get("a") and p.get("b")]

    @property
    def tiene_zonas(self) -> bool:
        return bool(self.poligonos["exterior"] and self.poligonos["interior"])

    @property
    def configuradas(self) -> bool:
        return self.tiene_zonas or bool(self.puertas)

    def zona_de(self, punto):
        x, y = int(min(max(punto[0], 0), self.w - 1)), int(min(max(punto[1], 0), self.h - 1))
        for tipo in ("exterior", "interior"):
            m = self.mascaras.get(tipo)
            if m is not None and m[y, x]:
                return tipo
        return None

    def cobertura(self, box, tipo):
        """Fracción de la zona `tipo` cubierta por la caja."""
        m = self.mascaras.get(tipo)
        if m is None:
            return 0.0
        x1, y1 = max(int(box[0]), 0), max(int(box[1]), 0)
        x2, y2 = min(int(box[2]), self.w), min(int(box[3]), self.h)
        if x2 <= x1 or y2 <= y1:
            return 0.0
        return float(m[y1:y2, x1:x2].sum()) / self.areas[tipo]

    def bus_en_puertas(self, box, solape):
        """Sin bahía ni carril dibujados: el bus cubre la franja exterior pegada a alguna puerta."""
        franja = 0.08 * self.h
        for pu in self.puertas:
            dentro = 0
            for t in np.linspace(0.1, 0.9, 9):
                x, y = pu.a + (pu.b - pu.a) * t - pu.normal * franja
                dentro += box[0] <= x <= box[2] and box[1] <= y <= box[3]
            if dentro / 9 >= max(solape, 0.3):
                return True
        return False

    def flecha_auto(self):
        """Sentido de ingreso inferido de las zonas, para dibujarlo cuando no hay líneas de puerta."""
        if not self.tiene_zonas:
            return None
        ce = np.vstack(self.poligonos["exterior"]).mean(axis=0)
        ci = np.vstack(self.poligonos["interior"]).mean(axis=0)
        return ce + (ci - ce) * 0.25, ce + (ci - ce) * 0.75


@dataclass
class Pista:
    tid: int
    primera: float
    ultima: float
    alias: int = 0                                  # ID que se muestra (se conserva al empalmar)
    pie: np.ndarray | None = None
    alto: float = 1.0
    box: np.ndarray | None = None
    confs: deque = field(default_factory=lambda: deque(maxlen=20))
    zona_actual: str | None = None
    racha: int = 0
    zona_confirmada: str | None = None
    desde: float = 0.0
    ultimo_ext: float = NUNCA                       # última vez confirmada en el carril
    vio_bus_en_exterior: bool = False
    puertas: dict = field(default_factory=dict)     # estado por línea de puerta
    reportados: set = field(default_factory=set)
    normales: set = field(default_factory=set)      # cruces con bus ya contados
    marcada_hasta: float = 0.0
    marca: str = ""
    empalmes: int = 0


HEREDABLE = ("primera", "alias", "zona_confirmada", "desde", "ultimo_ext", "vio_bus_en_exterior", "puertas",
             "reportados", "normales", "marcada_hasta", "marca")


class AnalizadorEvasion:
    def __init__(self):
        self.pistas: dict[int, Pista] = {}
        self.ultimo_bus = NUNCA
        self.ultimo_por_tipo: dict[str, float] = {}
        self.aglom_desde = None
        self.destello: dict[int, float] = {}        # puerta → hasta cuándo resaltarla
        self.contadores = {"ingresos": 0, "salidas": 0, "con_bus": 0}
        self.estado = {"bus_presente": False, "bus_reciente": False, "personas": 0,
                       "en_exterior": 0, "en_interior": 0, "contadores": dict(self.contadores)}

    # ── utilidades ──────────────────────────────────────────────────────────
    def _fuera_cooldown(self, tipo, ahora, cfg, minimo=0):
        return ahora - self.ultimo_por_tipo.get(tipo, NUNCA) >= max(cfg["cooldown_s"], minimo)

    def _evento(self, tipo, pista, ahora, detalle=""):
        conf = float(np.mean(pista.confs)) if pista is not None and pista.confs else 0.5
        if pista is not None and pista.empalmes:
            conf *= 0.85                             # cruce reconstruido tras perder a la persona
        if pista is not None:
            pista.reportados.add(tipo)
            pista.marcada_hasta = ahora + 3.0
            pista.marca = {"INGRESO_LATERAL": "EVASION", "SALIDA_LATERAL": "SALIDA",
                           "PERSONA_CARRIL": "EN CARRIL"}.get(tipo, tipo)
        self.ultimo_por_tipo[tipo] = ahora
        return {"tipo": tipo, "track_id": pista.alias if pista else None,
                "box": pista.box.tolist() if pista is not None and pista.box is not None else None,
                "confianza": round(conf, 4), "detalle": detalle}

    def _cruce(self, p, sentido, con_bus, ahora, puerta=None):
        """Registra un cruce una sola vez por persona y sentido. Devuelve el evento o None."""
        if con_bus:
            if sentido not in p.normales:
                p.normales.add(sentido)
                self.contadores["con_bus"] += 1
            return None
        tipo = "INGRESO_LATERAL" if sentido == "entrada" else "SALIDA_LATERAL"
        if tipo in p.reportados:
            return None
        self.contadores["ingresos" if sentido == "entrada" else "salidas"] += 1
        if puerta is not None:
            self.destello[puerta] = ahora + 2.5
        texto = ("Cruce carril → plataforma sin bus en la puerta" if sentido == "entrada"
                 else "Cruce plataforma → carril sin bus en la puerta")
        notas = [f"puerta {puerta + 1}" if puerta is not None else "por zonas"]
        if p.empalmes:
            notas.append("ID recuperado tras oclusión")
        return self._evento(tipo, p, ahora, f"{texto} ({', '.join(notas)})")

    def _lado(self, zonas, pie, alto):
        """Lado de la puerta para el empalme: ('exterior'|'interior', distancia a la puerta en alturas)
        o None si el punto no está cerca de ninguna puerta."""
        mejor = None
        for pu in zonas.puertas:
            prof, t = pu.medir(pie, alto)
            margen = 0.1 + 0.6 * alto / pu.largo
            if -margen <= t <= 1 + margen and abs(prof) <= 1.5:
                if mejor is None or abs(prof) < abs(mejor[1]):
                    mejor = ("interior" if prof > 0 else "exterior", prof)
        if mejor is not None or not zonas.tiene_zonas:
            return mejor
        # Solo zonas: cerca de la frontera entre carril y plataforma
        pt = (float(pie[0]), float(pie[1]))
        d_ext = max((cv2.pointPolygonTest(p.reshape(-1, 1, 2).astype(np.float32), pt, True)
                     for p in zonas.poligonos["exterior"]), default=-1e9)
        d_int = max((cv2.pointPolygonTest(p.reshape(-1, 1, 2).astype(np.float32), pt, True)
                     for p in zonas.poligonos["interior"]), default=-1e9)
        # pointPolygonTest: > 0 dentro, < 0 fuera (distancia con signo)
        if d_ext >= d_int and -d_int <= 1.5 * alto:
            return ("exterior", -d_int / alto)
        if d_int > d_ext and -d_ext <= 1.5 * alto:
            return ("interior", -d_ext / alto)
        return None

    def _empalmar(self, nueva: Pista, ahora, cfg, zonas):
        """Une una pista nueva con una recién perdida cuando la persona cruzó la puerta tapada por su
        marco y el tracker le cambió el ID. Condiciones (pensadas para no unir personas distintas en
        una multitud):
          • las dos están junto a una puerta y en lados opuestos,
          • la perdida venía confirmada de su lado,
          • distancia y altura compatibles con la misma persona caminando o trotando,
          • hay un único candidato claro (si hay dos parecidos, no se adivina)."""
        ventana = cfg["ventana_reid_s"]
        if ventana <= 0:
            return
        lado_nueva = self._lado(zonas, nueva.pie, nueva.alto)
        if lado_nueva is None:
            return
        candidatos = []
        for p in self.pistas.values():
            if p is nueva or p.ultima >= ahora or p.pie is None:
                continue
            dt = ahora - p.ultima
            if dt > ventana or not 0.75 < nueva.alto / max(p.alto, 1) < 1.35:
                continue
            ancho_n, ancho_p = nueva.box[2] - nueva.box[0], p.box[2] - p.box[0]
            if not 0.6 < ancho_n / max(ancho_p, 1) < 1.65:
                continue                             # forma de caja distinta: otra persona u objeto
            lado_p = self._lado(zonas, p.pie, p.alto)
            if lado_p is None or lado_p[0] == lado_nueva[0]:
                continue
            confirmada = p.zona_confirmada == lado_p[0] or any(
                (e["fuera_ok"] if lado_p[0] == "exterior" else e["adentro"] > NUNCA) for e in p.puertas.values())
            if not confirmada:
                continue
            d = float(np.hypot(*(nueva.pie - p.pie))) / max(p.alto, nueva.alto)
            if d <= 0.4 + 1.2 * dt:                  # velocidad de caminata (~1 altura/s) con holgura
                candidatos.append((d, p))
        if not candidatos:
            return
        candidatos.sort(key=lambda x: x[0])
        if len(candidatos) > 1 and candidatos[1][0] < candidatos[0][0] * 2.0:
            return                                   # ambiguo: no adivinar en una multitud
        mejor = candidatos[0][1]
        for k in HEREDABLE:
            setattr(nueva, k, getattr(mejor, k))
        nueva.confs.extendleft(reversed(mejor.confs))
        nueva.empalmes = mejor.empalmes + 1
        del self.pistas[mejor.tid]

    # ── paso principal ─────────────────────────────────────────────────────
    def actualizar(self, personas, vehiculos, zonas: Zonas, cfg: dict, ahora: float | None = None):
        ahora = ahora if ahora is not None else time.monotonic()
        eventos = []
        n_min = cfg["min_frames_zona"]
        ventana = cfg["ventana_cruce_s"]

        # 1) ¿Hay un bus detenido en la puerta?
        if zonas.mascaras.get("bahia") is not None:
            bus_presente = any(zonas.cobertura(v.box, "bahia") >= cfg["solape_bus"] for v in vehiculos)
        elif zonas.mascaras.get("exterior") is not None:
            bus_presente = any(zonas.cobertura(v.box, "exterior") >= cfg["solape_bus"] for v in vehiculos)
        else:
            bus_presente = any(zonas.bus_en_puertas(v.box, cfg["solape_bus"]) for v in vehiculos)
        if bus_presente:
            self.ultimo_bus = ahora
        bus_reciente = bus_presente or (ahora - self.ultimo_bus) < cfg["gracia_bus_s"]

        # 2) Actualizar pistas y empalmar las nuevas con las recién perdidas
        nuevas = []
        for d in personas:
            p = self.pistas.get(d.tid)
            if p is None:
                p = self.pistas[d.tid] = Pista(d.tid, ahora, ahora, alias=d.tid)
                nuevas.append(p)
            p.ultima, p.box = ahora, d.box
            p.pie = np.array([(d.box[0] + d.box[2]) / 2.0, d.box[3]], dtype=float)
            p.alto = float(max(d.box[3] - d.box[1], 1.0))
            p.confs.append(d.conf)
        if zonas.configuradas:
            for p in nuevas:
                self._empalmar(p, ahora, cfg, zonas)

        # 3) Reglas por persona
        en_ext = en_int = 0
        for d in personas:
            p = self.pistas.get(d.tid)
            if p is None or not zonas.configuradas:
                continue

            # 3a) Cruce por zonas: confirmada en carril → confirmada en plataforma
            if zonas.tiene_zonas:
                z = zonas.zona_de(p.pie)
                if z == p.zona_actual:
                    p.racha += 1
                else:
                    p.zona_actual, p.racha = z, 1
                if z is not None and p.racha >= n_min and z != p.zona_confirmada:
                    previa = p.zona_confirmada
                    p.zona_confirmada, p.desde = z, ahora
                    if z == "interior" and ahora - p.ultimo_ext <= ventana:
                        ev = self._cruce(p, "entrada", bus_reciente or p.vio_bus_en_exterior, ahora)
                        eventos += [ev] if ev else []
                    elif z == "exterior" and previa == "interior":
                        ev = self._cruce(p, "salida", bus_reciente, ahora)
                        eventos += [ev] if ev else []
                    if z == "exterior":
                        p.vio_bus_en_exterior = bus_reciente
                if p.zona_confirmada == "exterior" and z == "exterior":
                    p.ultimo_ext = ahora
                    if bus_reciente:
                        p.vio_bus_en_exterior = True     # probablemente bajó del bus

            # 3b) Cruce por líneas de puerta
            for i, pu in enumerate(zonas.puertas):
                prof, t = pu.medir(p.pie, p.alto)
                e = p.puertas.setdefault(i, {"afuera": NUNCA, "adentro": NUNCA, "r_fuera": 0, "r_dentro": 0,
                                             "bus_afuera": False, "fuera_ok": False})
                margen = 0.1 + 0.6 * p.alto / pu.largo
                if t < -margen or t > 1 + margen:     # fuera del ancho de esta puerta
                    e["r_fuera"] = e["r_dentro"] = 0
                    continue
                u = cfg["profundidad_cruce"]
                if prof <= -u:                         # lado del carril
                    e["r_dentro"] = 0
                    e["r_fuera"] += 1
                    if e["r_fuera"] == 1:
                        e["bus_afuera"] = bus_reciente
                        # Un solo cuadro afuera solo cuenta si la persona no venía confirmada de adentro
                        e["fuera_ok"] = e["adentro"] == NUNCA
                    e["bus_afuera"] = e["bus_afuera"] or bus_reciente
                    if e["r_fuera"] >= n_min:
                        e["fuera_ok"] = True
                        if ahora - e["adentro"] <= ventana:
                            ev = self._cruce(p, "salida", bus_reciente, ahora, i)
                            eventos += [ev] if ev else []
                        e["adentro"] = NUNCA
                    if e["fuera_ok"]:
                        e["afuera"] = ahora
                elif prof >= u:                        # lado de la plataforma
                    e["r_fuera"] = 0
                    e["r_dentro"] += 1
                    if e["r_dentro"] >= n_min:
                        if ahora - e["afuera"] <= ventana:
                            ev = self._cruce(p, "entrada", bus_reciente or e["bus_afuera"], ahora, i)
                            eventos += [ev] if ev else []
                        e["afuera"] = NUNCA
                        e["adentro"] = ahora

            # 3c) Permanencia en el carril (necesita zonas)
            if p.zona_confirmada == "exterior":
                en_ext += 1
                inicio = max(p.desde, self.ultimo_bus + cfg["gracia_bus_s"])
                if (not bus_reciente and ahora - inicio >= cfg["tiempo_carril_s"]
                        and not p.reportados & {"PERSONA_CARRIL", "SALIDA_LATERAL"}
                        and self._fuera_cooldown("PERSONA_CARRIL", ahora, cfg)):
                    eventos.append(self._evento("PERSONA_CARRIL", p, ahora,
                                                f"Más de {cfg['tiempo_carril_s']:.0f} s en el carril sin bus"))
            elif p.zona_confirmada == "interior":
                en_int += 1

        # 4) Aglomeración en la zona de puertas
        if zonas.tiene_zonas and en_int >= cfg["aforo_max"]:
            if self.aglom_desde is None:
                self.aglom_desde = ahora
            elif ahora - self.aglom_desde >= 5 and self._fuera_cooldown("AGLOMERACION", ahora, cfg, minimo=120):
                eventos.append(self._evento("AGLOMERACION", None, ahora,
                                            f"{en_int} personas en la zona de puertas (aforo {cfg['aforo_max']})"))
                eventos[-1]["confianza"] = 0.9
        else:
            self.aglom_desde = None

        # 5) Limpieza
        limite = max(4.0, cfg["ventana_reid_s"] + 1)
        for tid in [t for t, p in self.pistas.items() if ahora - p.ultima > limite]:
            del self.pistas[tid]

        self.estado = {"bus_presente": bus_presente, "bus_reciente": bus_reciente, "personas": len(personas),
                       "en_exterior": en_ext, "en_interior": en_int, "contadores": dict(self.contadores)}
        return eventos
