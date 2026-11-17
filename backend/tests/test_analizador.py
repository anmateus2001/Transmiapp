"""Pruebas del motor de reglas con trayectorias sintéticas.

Ejecutar desde backend/:  python tests/test_analizador.py     (o: python -m pytest tests -q)

Escena: imagen de 1000x600 vista desde la plataforma. Arriba el carril (rojo, y < 240),
la puerta en y = 270 y abajo la plataforma (verde, y > 300). Cada paso son 0.25 s (4 FPS,
lo normal en CPU). Una persona mide 160 px.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.settings import DEFAULTS                                  # noqa: E402
from app.vision.analyzer import AnalizadorEvasion, Zonas           # noqa: E402
from app.vision.detectors import Deteccion                         # noqa: E402

W, H = 1000, 600
CARRIL = [[[0, 0], [1, 0], [1, 0.4], [0, 0.4]]]
PLATAFORMA = [[[0, 0.5], [1, 0.5], [1, 1], [0, 1]]]
BAHIA = [[[0, 0], [1, 0], [1, 0.35], [0, 0.35]]]
# Puerta horizontal en y=0.45 trazada de izquierda a derecha: la flecha de ingreso apunta a la
# derecha del trazo, que en pantalla es hacia abajo (la plataforma)
PUERTA = [{"a": [0.35, 0.45], "b": [0.65, 0.45]}]

SOLO_ZONAS = {"exterior": CARRIL, "interior": PLATAFORMA, "bahia": BAHIA}
SOLO_LINEA = {"puertas": PUERTA}
COMPLETO = {"exterior": CARRIL, "interior": PLATAFORMA, "bahia": BAHIA, "puertas": PUERTA}

CFG = dict(DEFAULTS, cooldown_s=0, aforo_max=4)
BUS = Deteccion(np.array([0, 0, 1000, 250], dtype=float), 0.9, 5)
PASO = 0.25

AFUERA, ADENTRO, UMBRAL = 150, 420, 285   # y de los pies


def persona(tid, y_pies, x=500):
    return Deteccion(np.array([x - 30, y_pies - 160, x + 30, y_pies], dtype=float), 0.8, 0, tid)


def correr(cuadros, roi=COMPLETO, con_bus=lambda i: False, cfg=None):
    """cuadros: lista de listas de detecciones (o de y para la persona 1)."""
    a, z = AnalizadorEvasion(), Zonas(roi, W, H)
    tipos = []
    for i, c in enumerate(cuadros):
        dets = [persona(1, c)] if isinstance(c, (int, float)) else (c or [])
        tipos += [e["tipo"] for e in a.actualizar(dets, [BUS] if con_bus(i) else [], z, cfg or CFG, 100 + i * PASO)]
    return tipos, a.contadores


# ── Ingresos (carril → plataforma) ─────────────────────────────────────────
def test_ingreso_sin_bus_es_evasion():
    for roi in (COMPLETO, SOLO_ZONAS, SOLO_LINEA):
        tipos, c = correr([AFUERA] * 4 + [ADENTRO] * 4, roi)
        assert tipos == ["INGRESO_LATERAL"], roi
        assert c["ingresos"] == 1


def test_cruce_rapido_un_solo_cuadro_en_el_carril():
    # A 4 FPS quien cruza corriendo puede verse una sola vez del lado del carril
    tipos, _ = correr([AFUERA] + [ADENTRO] * 4, SOLO_LINEA)
    assert tipos == ["INGRESO_LATERAL"]
    tipos, _ = correr([AFUERA] + [ADENTRO] * 4, COMPLETO)
    assert tipos == ["INGRESO_LATERAL"]


def test_cambio_de_id_al_cruzar_la_puerta():
    # Camina hacia la puerta, el marco la tapa 2 cuadros (0.5 s) y reaparece adentro con otro ID
    cuadros = [[persona(1, y)] for y in (150, 180, 210, 240)] + [[]] * 2 + \
              [[persona(7, y)] for y in (330, 360, 390, 420, 420)]
    for roi in (COMPLETO, SOLO_ZONAS, SOLO_LINEA):
        tipos, _ = correr(cuadros, roi)
        assert tipos == ["INGRESO_LATERAL"], roi


def test_cambio_de_id_no_empalma_con_alguien_lejano():
    # La persona del carril desaparece al otro lado de la imagen: el nuevo ID no la hereda
    cuadros = [[persona(1, AFUERA, x=100)]] * 6 + [[]] * 2 + [[persona(7, ADENTRO, x=900)]] * 6
    tipos, _ = correr(cuadros, SOLO_ZONAS)
    assert "INGRESO_LATERAL" not in tipos


def test_multitud_ambigua_no_se_empalma():
    # Dos personas se pierden junto a la puerta en el carril y aparece una nueva en la plataforma:
    # no se sabe quién es, así que no se inventa un cruce
    cuadros = [[persona(1, AFUERA + 80, 470), persona(2, AFUERA + 80, 530)]] * 5 + [[]] * 2 + \
              [[persona(9, ADENTRO - 80, 500)]] * 6
    tipos, _ = correr(cuadros, SOLO_LINEA)
    assert "INGRESO_LATERAL" not in tipos


def test_no_empalma_si_aparece_del_mismo_lado():
    # Alguien se pierde en el carril y otro aparece también en el carril: no hay cruce
    cuadros = [[persona(1, AFUERA)]] * 5 + [[]] * 2 + [[persona(9, AFUERA + 40)]] * 6 + [[persona(9, AFUERA + 40)]] * 2
    tipos, _ = correr(cuadros, SOLO_LINEA)
    assert tipos == []


def test_caminando_por_la_puerta_en_diagonal():
    ys = np.linspace(AFUERA, ADENTRO, 8)
    xs = np.linspace(380, 620, 8)
    tipos, _ = correr([[persona(1, y, x)] for y, x in zip(ys, xs)] + [[persona(1, ADENTRO, 620)]] * 3)
    assert tipos == ["INGRESO_LATERAL"]


def test_cruce_por_fuera_de_la_puerta_no_cuenta_por_linea():
    # La línea solo cubre su puerta: alguien que pasa lejos de ella no se cuenta por línea
    tipos, _ = correr([[persona(1, AFUERA, x=60)]] * 3 + [[persona(1, ADENTRO, x=60)]] * 3, SOLO_LINEA)
    assert tipos == []


def test_dos_personas_entran_juntas():
    cuadros = [[persona(1, AFUERA, 420), persona(2, AFUERA, 580)]] * 3 + \
              [[persona(1, ADENTRO, 420), persona(2, ADENTRO, 580)]] * 3
    tipos, c = correr(cuadros)
    assert tipos == ["INGRESO_LATERAL", "INGRESO_LATERAL"] and c["ingresos"] == 2


# ── Bus en la puerta: pasajeros normales ───────────────────────────────────
def test_ingreso_con_bus_es_normal_y_se_cuenta():
    for roi in (COMPLETO, SOLO_ZONAS, SOLO_LINEA):
        tipos, c = correr([AFUERA] * 4 + [ADENTRO] * 4, roi, con_bus=lambda i: True)
        assert tipos == [] and c["con_bus"] == 1, roi


def test_bajarse_del_bus_y_entrar_despues_no_alerta():
    # El bus se va en el cuadro 6; la persona entra 2 s después (fuera de la gracia)
    tipos, _ = correr([AFUERA] * 14 + [ADENTRO] * 4, con_bus=lambda i: i < 6)
    assert tipos == []


def test_ingreso_cuando_el_bus_ya_se_fue_hace_rato():
    tipos, _ = correr([None] * 30 + [AFUERA] * 4 + [ADENTRO] * 4, con_bus=lambda i: i < 4)
    assert tipos == ["INGRESO_LATERAL"]


# ── Ruido ──────────────────────────────────────────────────────────────────
def test_un_cuadro_de_ruido_no_cuenta():
    for roi in (COMPLETO, SOLO_ZONAS, SOLO_LINEA):
        tipos, _ = correr([ADENTRO] * 6 + [AFUERA] + [ADENTRO] * 6, roi)
        assert tipos == [], roi


def test_parado_sobre_la_puerta_no_cuenta():
    # Pies oscilando sobre la línea, a menos de la profundidad mínima
    tipos, _ = correr([UMBRAL - 12, UMBRAL + 12] * 10, SOLO_LINEA)
    assert tipos == []


# ── Salidas (plataforma → carril) ──────────────────────────────────────────
def test_salida_sin_bus():
    for roi in (COMPLETO, SOLO_ZONAS, SOLO_LINEA):
        tipos, c = correr([ADENTRO] * 4 + [AFUERA] * 4, roi)
        assert tipos == ["SALIDA_LATERAL"] and c["salidas"] == 1, roi


def test_varias_personas_salen_y_todas_cuentan():
    cfg = dict(CFG, cooldown_s=60)
    xs = (420, 500, 580)
    cuadros = [[persona(i, ADENTRO, x) for i, x in enumerate(xs, 1)]] * 4 + \
              [[persona(i, AFUERA, x) for i, x in enumerate(xs, 1)]] * 4
    tipos, c = correr(cuadros, SOLO_LINEA, cfg=cfg)
    assert tipos.count("SALIDA_LATERAL") == 3 and c["salidas"] == 3


def test_salir_para_subir_al_bus_es_normal():
    tipos, c = correr([ADENTRO] * 4 + [AFUERA] * 4, con_bus=lambda i: True)
    assert tipos == [] and c["con_bus"] == 1


def test_quien_sale_al_carril_no_genera_ademas_persona_en_carril():
    tipos, _ = correr([ADENTRO] * 4 + [AFUERA] * 30, SOLO_ZONAS)
    assert tipos == ["SALIDA_LATERAL"]


# ── Otros eventos ──────────────────────────────────────────────────────────
def test_permanencia_en_carril():
    tipos, _ = correr([AFUERA] * 16, SOLO_ZONAS)     # 4 s sin bus
    assert tipos == ["PERSONA_CARRIL"]


def test_permanencia_y_luego_ingreso():
    tipos, _ = correr([AFUERA] * 16 + [ADENTRO] * 4)
    assert tipos == ["PERSONA_CARRIL", "INGRESO_LATERAL"]


def test_aglomeracion():
    a, z = AnalizadorEvasion(), Zonas(SOLO_ZONAS, W, H)
    tipos = []
    for i in range(40):
        ps = [persona(tid, 450, x=100 + tid * 150) for tid in range(1, 6)]
        tipos += [e["tipo"] for e in a.actualizar(ps, [], z, CFG, 100 + i * PASO)]
    assert tipos == ["AGLOMERACION"]


if __name__ == "__main__":
    fallas = 0
    for nombre, f in list(globals().items()):
        if nombre.startswith("test_"):
            try:
                f()
                print("ok   ", nombre)
            except AssertionError as e:
                fallas += 1
                print("FALLA", nombre, e)
    print(f"\n{'Todas las pruebas pasaron' if not fallas else f'{fallas} prueba(s) fallaron'}")
    sys.exit(1 if fallas else 0)
