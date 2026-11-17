"""Anonimización (Ley 1581 de 2012) y superposición de zonas/detecciones sobre el frame."""
import datetime as dt
import unicodedata

import cv2
import numpy as np

_cascada = None


def _detector_rostros():
    global _cascada
    if _cascada is None:
        try:
            ruta = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            c = cv2.CascadeClassifier(ruta)
            _cascada = c if not c.empty() else False
        except Exception:
            _cascada = False
    return _cascada or None


def _pixelar(img, x1, y1, x2, y2, bloques=7):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = max(0, int(x1)), max(0, int(y1)), min(w, int(x2)), min(h, int(y2))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return
    roi = img[y1:y2, x1:x2]
    peq = cv2.resize(roi, (bloques, max(1, int(bloques * (y2 - y1) / max(x2 - x1, 1)))),
                     interpolation=cv2.INTER_LINEAR)
    img[y1:y2, x1:x2] = cv2.resize(peq, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST)


def anonimizar(img, cajas_personas, buscar_rostros=False):
    """Pixela la cabeza de cada persona detectada (parte superior de su caja).

    Con buscar_rostros=True también aplica un detector Haar para cubrir rostros de personas
    que el modelo no detectó (se usa al guardar evidencia, no en cada frame en vivo).
    """
    for b in cajas_personas:
        x1, y1, x2, y2 = b[:4]
        alto, ancho = y2 - y1, x2 - x1
        cab = max(alto * 0.26, 14)
        _pixelar(img, x1 + ancho * 0.08, y1 - alto * 0.02, x2 - ancho * 0.08, y1 + cab)
    if buscar_rostros and (det := _detector_rostros()) is not None:
        gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for (x, y, w, h) in det.detectMultiScale(gris, 1.15, 5, minSize=(18, 18)):
            _pixelar(img, x - w * 0.15, y - h * 0.15, x + w * 1.15, y + h * 1.15)
    return img


def ascii(texto: str) -> str:
    """cv2.putText no soporta tildes."""
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()


# Colores BGR
C_EXT = (48, 40, 214)      # rojo carril
C_INT = (64, 170, 110)     # verde plataforma
C_BAHIA = (200, 140, 30)   # azul bahía
C_BUS = (0, 200, 255)      # amarillo
C_NEUTRO = (235, 235, 235)
C_EVASION = (0, 0, 255)


def _texto(img, txt, org, escala=0.5, color=(255, 255, 255), grosor=1, fondo=None):
    (tw, th), base = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, escala, grosor)
    x, y = int(org[0]), int(org[1])
    if fondo is not None:
        cv2.rectangle(img, (x - 3, y - th - 4), (x + tw + 3, y + base + 1), fondo, -1)
    cv2.putText(img, txt, (x, y), cv2.FONT_HERSHEY_SIMPLEX, escala, color, grosor, cv2.LINE_AA)


def dibujar(frame, zonas, personas, vehiculos, analizador, meta, anonimizar_caras=False,
            resaltar_tid=None, buscar_rostros=False, ahora=0.0):
    out = frame.copy()
    if anonimizar_caras:
        anonimizar(out, [d.box for d in personas], buscar_rostros)

    # Zonas semitransparentes
    if zonas is not None:
        capa = out.copy()
        for tipo, color in (("bahia", C_BAHIA), ("exterior", C_EXT), ("interior", C_INT)):
            for poli in zonas.poligonos[tipo]:
                cv2.fillPoly(capa, [poli], color)
        cv2.addWeighted(capa, 0.16, out, 0.84, 0, out)
        for tipo, color in (("bahia", C_BAHIA), ("exterior", C_EXT), ("interior", C_INT)):
            for poli in zonas.poligonos[tipo]:
                cv2.polylines(out, [poli], True, color, 2, cv2.LINE_AA)

    if zonas is not None:
        _dibujar_sentido(out, zonas, analizador, ahora)

    for v in vehiculos:
        x1, y1, x2, y2 = map(int, v.box)
        cv2.rectangle(out, (x1, y1), (x2, y2), C_BUS, 2)
        _texto(out, f"BUS {v.conf:.2f}", (x1 + 4, y1 + 18), 0.55, (0, 0, 0), 1, C_BUS)

    pistas = analizador.pistas if analizador else {}
    for d in personas:
        x1, y1, x2, y2 = map(int, d.box)
        p = pistas.get(d.tid)
        z = p.zona_confirmada if p else None
        color = C_EXT if z == "exterior" else C_INT if z == "interior" else C_NEUTRO
        grosor = 2
        etiqueta = f"#{p.alias if p else d.tid}"
        if p and (p.marcada_hasta > ahora or p.alias == resaltar_tid):
            color, grosor = C_EVASION, 4
            etiqueta += f" {p.marca}"
        cv2.rectangle(out, (x1, y1), (x2, y2), color, grosor)
        cv2.circle(out, ((x1 + x2) // 2, y2), 4, color, -1)          # punto de los pies
        tinta = (20, 20, 20) if color == C_NEUTRO else (255, 255, 255)
        _texto(out, etiqueta, (x1 + 2, max(y1 - 6, 14)), 0.5, tinta, 1, color)

    # Barra superior de estado
    h, w = out.shape[:2]
    cv2.rectangle(out, (0, 0), (w, 34), (30, 26, 22), -1)
    _texto(out, ascii(f"{meta.get('codigo', '')}  {meta.get('estacion', '')}"), (10, 23), 0.6)
    hora = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (tw, _), _ = cv2.getTextSize(hora, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    _texto(out, hora, (w - tw - 10, 22), 0.5, (200, 200, 200))
    est = analizador.estado if analizador else {}
    info = f"Personas {est.get('personas', len(personas))}   FPS {meta.get('fps', 0):.1f}"
    _texto(out, info, (10, h - 12), 0.55, (255, 255, 255), 1, (30, 26, 22))
    if est.get("bus_presente"):
        _texto(out, "BUS EN LA PUERTA", (w - 190, h - 12), 0.55, (0, 0, 0), 1, C_BUS)
    elif est.get("bus_reciente"):
        _texto(out, "BUS SALIENDO", (w - 160, h - 12), 0.55, (0, 0, 0), 1, (120, 200, 255))
    if zonas is not None and not zonas.configuradas:
        _texto(out, "ZONAS NO CONFIGURADAS - solo deteccion", (10, 60), 0.6, (255, 255, 255), 1, (0, 120, 220))
    elif analizador is not None:
        c = analizador.contadores
        _texto(out, f"Ingresos irregulares {c['ingresos']}   Salidas {c['salidas']}   Cruces con bus {c['con_bus']}",
               (10, 58), 0.52, (255, 255, 255), 1, (30, 26, 22))
    return out


def _flecha(img, p1, p2, color, grosor=3):
    p1, p2 = tuple(int(v) for v in p1), tuple(int(v) for v in p2)
    cv2.arrowedLine(img, p1, p2, (25, 25, 25), grosor + 3, cv2.LINE_AA, tipLength=0.32)
    cv2.arrowedLine(img, p1, p2, color, grosor, cv2.LINE_AA, tipLength=0.32)


def _dibujar_sentido(img, zonas, analizador, ahora):
    """Líneas de puerta con flecha de sentido de ingreso (carril → plataforma)."""
    destellos = analizador.destello if analizador else {}
    for i, pu in enumerate(zonas.puertas):
        rojo = destellos.get(i, 0) > ahora
        a, b = tuple(int(v) for v in pu.a), tuple(int(v) for v in pu.b)
        cv2.line(img, a, b, (25, 25, 25), 7, cv2.LINE_AA)
        cv2.line(img, a, b, C_EVASION if rojo else (255, 255, 255), 4 if rojo else 3, cv2.LINE_AA)
        largo = float(np.clip(pu.largo * 0.35, 28, 80))
        c = pu.centro
        cola, punta = c - pu.normal * largo * 0.7, c + pu.normal * largo
        cv2.line(img, tuple(int(v) for v in cola), tuple(int(v) for v in c), (25, 25, 25), 6, cv2.LINE_AA)
        cv2.line(img, tuple(int(v) for v in cola), tuple(int(v) for v in c), C_EXT, 3, cv2.LINE_AA)
        _flecha(img, c, punta, C_INT)
        _texto(img, f"P{i + 1} INGRESO", (punta[0] + 6, punta[1] + 5), 0.45, (255, 255, 255), 1,
               C_EVASION if rojo else (40, 110, 60))
    if not zonas.puertas and (f := zonas.flecha_auto()) is not None:
        _flecha(img, f[0], f[1], (255, 255, 255), 2)
        _texto(img, "SENTIDO DE INGRESO", (f[1][0] + 8, f[1][1] + 5), 0.45, (255, 255, 255), 1, (40, 110, 60))


def jpeg(img, calidad=80) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, calidad])
    return buf.tobytes() if ok else b""


def escalar_ancho(frame, ancho_max):
    h, w = frame.shape[:2]
    if w <= ancho_max:
        return frame
    f = ancho_max / w
    return cv2.resize(frame, (ancho_max, int(round(h * f))), interpolation=cv2.INTER_AREA)


def placeholder(texto, ancho=960, alto=540) -> bytes:
    img = np.full((alto, ancho, 3), (46, 40, 34), dtype=np.uint8)
    for i, linea in enumerate(ascii(texto).split("\n")):
        (tw, _), _ = cv2.getTextSize(linea, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        _texto(img, linea, ((ancho - tw) // 2, alto // 2 + i * 36), 0.8, (210, 210, 210), 2)
    return jpeg(img)
