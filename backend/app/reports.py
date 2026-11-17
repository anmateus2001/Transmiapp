"""Reportes PDF (RF12 · HU17) con ReportLab."""
import datetime as dt

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

from .db import Camara, Estacion, EventoEvasion, Reporte, TipoEvasion
from .settings import EVID_DIR, REPORT_DIR, VERSION
from .stats import filas_eventos, resumen

ROJO = colors.HexColor("#B3202A")
TINTA = colors.HexColor("#1F2B37")
GRIS = colors.HexColor("#E6E9EC")
NOMBRES = {"INGRESO_LATERAL": "Ingreso por puerta lateral", "PERSONA_CARRIL": "Persona en el carril",
           "SALIDA_LATERAL": "Salida por puerta lateral", "AGLOMERACION": "Aglomeración en puertas"}


def _tabla(datos, anchos, encabezado=True):
    t = Table(datos, colWidths=anchos, repeatRows=1 if encabezado else 0)
    estilo = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F7")]),
              ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRIS),
              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    if encabezado:
        estilo += [("BACKGROUND", (0, 0), (-1, 0), TINTA), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                   ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5)]
    t.setStyle(TableStyle(estilo))
    return t


def _grafica_barras(valores, etiquetas, titulo, ancho=17 * cm, alto=5.5 * cm):
    d = Drawing(ancho, alto)
    g = VerticalBarChart()
    g.x, g.y, g.width, g.height = 30, 30, ancho - 45, alto - 55
    g.data = [valores or [0]]
    g.categoryAxis.categoryNames = etiquetas or [""]
    g.categoryAxis.labels.fontSize = 6.5
    g.categoryAxis.labels.angle = 0 if len(etiquetas) <= 24 else 45
    g.valueAxis.valueMin = 0
    g.valueAxis.labels.fontSize = 7
    g.bars[0].fillColor = ROJO
    g.bars[0].strokeColor = None
    d.add(g)
    d.add(String(30, alto - 12, titulo, fontName="Helvetica-Bold", fontSize=9, fillColor=TINTA))
    return d


def generar_reporte(s, desde: dt.datetime, hasta: dt.datetime, estacion_id, operador) -> Reporte:
    filas = filas_eventos(s, desde, hasta, estacion_id=estacion_id)
    r = resumen(filas)
    est = s.get(Estacion, estacion_id) if estacion_id else None
    nombre = f"reporte_{dt.datetime.now():%Y%m%d_%H%M%S}.pdf"
    ruta = REPORT_DIR / nombre

    estilos = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=estilos["Title"], fontName="Helvetica-Bold", fontSize=18,
                        textColor=TINTA, alignment=0, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=estilos["Heading2"], fontName="Helvetica-Bold", fontSize=11.5,
                        textColor=TINTA, spaceBefore=12, spaceAfter=6)
    txt = ParagraphStyle("txt", parent=estilos["Normal"], fontName="Helvetica", fontSize=9, leading=12)

    doc = SimpleDocTemplate(str(ruta), pagesize=letter, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                            title="Reporte de evasión TransmiEye", author="TransmiEye")
    el = [Paragraph("Reporte de evasión por puertas laterales", h1),
          Paragraph(f"Periodo: <b>{desde:%d/%m/%Y %H:%M}</b> a <b>{hasta:%d/%m/%Y %H:%M}</b><br/>"
                    f"Estación: <b>{est.nombre if est else 'Todas'}</b><br/>"
                    f"Generado por {operador.nombre} el {dt.datetime.now():%d/%m/%Y %H:%M} · TransmiEye v{VERSION}", txt),
          Spacer(1, 8)]

    prec = f"{r['precision'] * 100:.1f} %" if r["precision"] is not None else "Sin revisiones"
    el.append(Paragraph("Resumen", h2))
    datos = [["Indicador", "Valor"], ["Eventos detectados", r["total"]]]
    datos += [[NOMBRES.get(k, k), r["por_tipo"].get(k, 0)] for k in NOMBRES]
    datos += [["Confirmados por operador", r["confirmados"]], ["Rechazados (falsos positivos)", r["rechazados"]],
              ["Pendientes de revisión", r["pendientes"]], ["Precisión (confirmados / revisados)", prec],
              ["Hora pico", f"{r['hora_pico']:02d}:00" if r["hora_pico"] is not None else "–"],
              ["Estación con más eventos", r["estacion_critica"] or "–"]]
    el.append(_tabla(datos, [11 * cm, 6 * cm]))

    el.append(Spacer(1, 10))
    el.append(_grafica_barras(r["por_hora"], [f"{h:02d}" for h in range(24)], "Eventos por hora del día"))
    if len(r["por_dia"]) > 1:
        dias = list(r["por_dia"].items())[-31:]
        el.append(_grafica_barras([v for _, v in dias], [k[5:] for k, _ in dias], "Eventos por día"))

    if r["por_estacion"]:
        el.append(Paragraph("Eventos por estación", h2))
        el.append(_tabla([["Estación", "Eventos"]] + [[k, v] for k, v in r["por_estacion"].items()],
                         [11 * cm, 6 * cm]))

    ultimos = (s.query(EventoEvasion).join(Camara).join(TipoEvasion)
               .filter(EventoEvasion.timestamp_evento >= desde, EventoEvasion.timestamp_evento < hasta))
    if estacion_id:
        ultimos = ultimos.filter(Camara.estacion_id == estacion_id)
    ultimos = ultimos.order_by(EventoEvasion.timestamp_evento.desc()).limit(30).all()
    if ultimos:
        el.append(Paragraph("Últimos eventos del periodo", h2))
        estado = {True: "Confirmado", False: "Rechazado", None: "Pendiente"}
        el.append(_tabla([["#", "Fecha y hora", "Estación", "Cámara", "Tipo", "Conf.", "Estado"]] +
                         [[e.evento_id, f"{e.timestamp_evento:%d/%m %H:%M:%S}", e.camara.estacion.nombre,
                           e.camara.codigo_camara, NOMBRES.get(e.tipo.codigo, e.tipo.nombre),
                           f"{e.confianza * 100:.0f}%", estado[e.confirmado]] for e in ultimos],
                         [1.1 * cm, 2.6 * cm, 2.8 * cm, 2 * cm, 4.6 * cm, 1.3 * cm, 2.2 * cm]))

        imgs = [e for e in ultimos if e.tipo.codigo == "INGRESO_LATERAL" and not e.purgado and e.frame_referencia
                and (EVID_DIR / e.frame_referencia / "evento.jpg").exists()][:4]
        if imgs:
            el.append(Paragraph("Evidencia de ingresos laterales (rostros anonimizados)", h2))
            celdas = []
            for e in imgs:
                im = Image(str(EVID_DIR / e.frame_referencia / "evento.jpg"))
                f = (8.2 * cm) / im.imageWidth
                im.drawWidth, im.drawHeight = im.imageWidth * f, im.imageHeight * f
                celdas.append([im, Paragraph(f"#{e.evento_id} · {e.timestamp_evento:%d/%m/%Y %H:%M:%S}<br/>"
                                             f"SHA-256 {e.hash_sha256[:20]}…", txt)])
            filas_img = [[celdas[i][0], celdas[i + 1][0] if i + 1 < len(celdas) else ""] for i in range(0, len(celdas), 2)]
            pies = [[celdas[i][1], celdas[i + 1][1] if i + 1 < len(celdas) else ""] for i in range(0, len(celdas), 2)]
            mezcla = []
            for a, b in zip(filas_img, pies):
                mezcla += [a, b]
            el.append(_tabla(mezcla, [8.5 * cm, 8.5 * cm], encabezado=False))

    el.append(Spacer(1, 14))
    el.append(Paragraph("Las imágenes de este reporte fueron anonimizadas antes de almacenarse (Ley 1581 de 2012) "
                        "y se conservan por el periodo de retención configurado.", txt))

    def pie(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(2 * cm, 1.1 * cm, "TransmiEye · Sistema de detección de evasión en TransMilenio")
        canvas.drawRightString(letter[0] - 2 * cm, 1.1 * cm, f"Página {d.page}")
        canvas.restoreState()

    doc.build(el, onFirstPage=pie, onLaterPages=pie)
    rep = Reporte(operador_id=operador.id, estacion_id=estacion_id, fecha_inicio=desde, fecha_fin=hasta,
                  total_eventos=r["total"], ruta_archivo=nombre)
    s.add(rep)
    s.commit()
    return rep
