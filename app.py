import base64
import json
import re
from decimal import Decimal, InvalidOperation
from io import BytesIO

import streamlit as st
from openai import OpenAI

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

st.set_page_config(
    page_title="Comparador de Facturas de Luz | Greening Energy",
    page_icon="⚡",
    layout="wide",
)

# Modelo de OpenAI
MODEL = "gpt-5.6-luna"


# ============================================================
# PRECIOS POR DEFECTO
# ============================================================

DEFAULT_ENERGY = {
    "P1": 0.095010,
    "P2": 0.090360,
    "P3": 0.092514,
    "P4": 0.0,
    "P5": 0.0,
    "P6": 0.0,
}

DEFAULT_POWER = {
    "P1": 0.055827,
    "P2": 0.029089,
    "P3": 0.012278,
    "P4": 0.010647,
    "P5": 0.006887,
    "P6": 0.003951,
}


# ============================================================
# PROMPT DE ANÁLISIS DE FACTURA
# ============================================================

SYSTEM_PROMPT = """
Eres un especialista español en facturas eléctricas.

Tu trabajo es analizar TODOS los documentos adjuntos
(PDF o fotografías) como una única factura eléctrica.

Debes leer visualmente las páginas y extraer únicamente
los datos que sean visibles o claramente deducibles.

NO INVENTES VALORES.

Devuelve EXCLUSIVAMENTE JSON válido.

ESTRUCTURA EXACTA:

{
  "cliente": null,
  "comercializadora": null,
  "cups": null,
  "tarifa": null,
  "fecha_emision": null,
  "periodo_inicio": null,
  "periodo_fin": null,
  "dias_facturados": null,
  "importe_total": null,
  "base_imponible": null,
  "iva_porcentaje": null,
  "iva_importe": null,
  "impuesto_electrico_importe": null,
  "alquiler_contador": null,
  "otros_conceptos": null,

  "energia": {
    "P1": {
      "kwh": null,
      "precio": null,
      "importe": null
    },
    "P2": {
      "kwh": null,
      "precio": null,
      "importe": null
    },
    "P3": {
      "kwh": null,
      "precio": null,
      "importe": null
    },
    "P4": {
      "kwh": null,
      "precio": null,
      "importe": null
    },
    "P5": {
      "kwh": null,
      "precio": null,
      "importe": null
    },
    "P6": {
      "kwh": null,
      "precio": null,
      "importe": null
    }
  },

  "potencia": {
    "P1": {
      "kw": null,
      "precio": null,
      "importe": null
    },
    "P2": {
      "kw": null,
      "precio": null,
      "importe": null
    },
    "P3": {
      "kw": null,
      "precio": null,
      "importe": null
    },
    "P4": {
      "kw": null,
      "precio": null,
      "importe": null
    },
    "P5": {
      "kw": null,
      "precio": null,
      "importe": null
    },
    "P6": {
      "kw": null,
      "precio": null,
      "importe": null
    }
  },

  "reactiva_importe": null,
  "descuentos_importe": null,
  "servicios_importe": null,

  "confianza": 0,

  "observaciones": []
}

REGLAS IMPORTANTES:

1. Usa números con punto decimal.
2. Si un dato no aparece claramente, utiliza null.
3. NO INVENTES DATOS.
4. No confundas kW con kWh.
5. Respeta siempre los periodos P1-P6.
6. En tarifas 2.0TD normalmente existen energía P1-P3
   y potencia P1-P2.
7. En tarifas 3.0TD o superiores pueden existir P1-P6.
8. Si hay varias páginas o fotografías, fusiona la información.
9. No dupliques importes que aparezcan repetidos en distintas páginas.
10. "importe_total" debe ser el TOTAL FINAL que paga el cliente.
11. Los descuentos deben conservarse como número positivo.
12. "confianza" debe ser un número entre 0 y 100.
13. "observaciones" debe indicar cualquier dato dudoso,
    ilegible o que deba ser revisado manualmente.
14. Presta especial atención a:
    - total de factura
    - consumo por periodo
    - potencia contratada
    - precio de energía
    - precio de potencia
    - impuestos
    - descuentos
    - servicios
    - energía reactiva
    - alquiler de contador
    - fechas
    - número de días facturados
15. Si un importe aparece con formato español,
    por ejemplo 1.234,56 €, conviértelo a 1234.56.
"""


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def num(x):
    """
    Convierte un valor a float de forma segura.
    """
    try:
        if x is None or x == "":
            return 0.0

        value = str(x).strip()

        # Formato español: 1.234,56
        if "," in value and "." in value:
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", ".")

        return float(Decimal(value))

    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def money(x):
    """
    Formatea un número como moneda española.
    """
    return (
        f"{num(x):,.2f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
        + " €"
    )


def parse_json(text):
    """
    Limpia posibles bloques Markdown y convierte a JSON.
    """
    if not text:
        raise ValueError("La IA no devolvió ningún texto.")

    text = text.strip()

    # Quitar ```json ... ```
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
    )

    return json.loads(text)


def normalize(data):
    """
    Garantiza que todos los campos necesarios existen.
    """

    data = data or {}

    # Campos generales
    defaults = {
        "cliente": None,
        "comercializadora": None,
        "cups": None,
        "tarifa": None,
        "fecha_emision": None,
        "periodo_inicio": None,
        "periodo_fin": None,
        "dias_facturados": None,
        "importe_total": None,
        "base_imponible": None,
        "iva_porcentaje": None,
        "iva_importe": None,
        "impuesto_electrico_importe": None,
        "alquiler_contador": None,
        "otros_conceptos": None,
        "reactiva_importe": None,
        "descuentos_importe": None,
        "servicios_importe": None,
        "confianza": 0,
        "observaciones": [],
    }

    for key, default in defaults.items():
        if key not in data:
            data[key] = default

    # Energía
    data.setdefault("energia", {})

    for i in range(1, 7):
        p = f"P{i}"

        data["energia"].setdefault(
            p,
            {
                "kwh": None,
                "precio": None,
                "importe": None,
            },
        )

        for field in ["kwh", "precio", "importe"]:
            data["energia"][p].setdefault(field, None)

    # Potencia
    data.setdefault("potencia", {})

    for i in range(1, 7):
        p = f"P{i}"

        data["potencia"].setdefault(
            p,
            {
                "kw": None,
                "precio": None,
                "importe": None,
            },
        )

        for field in ["kw", "precio", "importe"]:
            data["potencia"][p].setdefault(field, None)

    if not isinstance(data["observaciones"], list):
        data["observaciones"] = [str(data["observaciones"])]

    return data


# ============================================================
# API OPENAI
# ============================================================

def get_api_key():
    """
    Primero busca OPENAI_API_KEY en Streamlit Secrets.
    Si no existe, utiliza la clave introducida manualmente.
    """

    try:
        key = st.secrets.get("OPENAI_API_KEY", "")
    except Exception:
        key = ""

    if key:
        return key

    return st.session_state.get("api_key", "")


def get_client():
    """
    Crea el cliente OpenAI con timeout y reintentos.
    """

    key = get_api_key()

    if not key:
        return None

    return OpenAI(
        api_key=key,
        timeout=600.0,
        max_retries=3,
    )


def image_data_url(uploaded):
    """
    Convierte una imagen subida en Data URL.
    """

    raw = uploaded.getvalue()

    mime = uploaded.type or "image/jpeg"

    encoded = base64.b64encode(raw).decode("utf-8")

    return f"data:{mime};base64,{encoded}"


# ============================================================
# ANÁLISIS DE FACTURAS
# ============================================================

def analyze_files(files):

    client = get_client()

    if not client:
        raise RuntimeError(
            "No se ha encontrado la API key de OpenAI. "
            "Comprueba el secreto OPENAI_API_KEY en Streamlit."
        )

    # Mensaje inicial
    content = [
        {
            "type": "input_text",
            "text": SYSTEM_PROMPT
            + """

ANALIZA AHORA TODOS LOS DOCUMENTOS ADJUNTOS.

IMPORTANTE:

- Lee visualmente todas las páginas.
- Comprueba especialmente el TOTAL FINAL.
- Comprueba los consumos P1-P6.
- Comprueba la potencia contratada P1-P6.
- Comprueba los precios.
- Comprueba los importes.
- Comprueba impuestos.
- Comprueba descuentos.
- Comprueba servicios.
- Comprueba energía reactiva.
- Comprueba alquiler de contador.
- Comprueba las fechas.
- Comprueba los días facturados.

No inventes ningún dato.

Devuelve exclusivamente el JSON solicitado.
""",
        }
    ]

    # ========================================================
    # AÑADIR TODOS LOS ARCHIVOS
    # ========================================================

    for f in files:

        raw = f.getvalue()

        filename = f.name.lower()

        # ----------------------------------------------------
        # PDF
        # ----------------------------------------------------

        if filename.endswith(".pdf"):

            encoded_pdf = base64.b64encode(raw).decode("utf-8")

            content.append(
                {
                    "type": "input_file",
                    "filename": f.name,
                    "file_data": encoded_pdf,
                }
            )

        # ----------------------------------------------------
        # IMÁGENES
        # ----------------------------------------------------

        else:

            content.append(
                {
                    "type": "input_image",
                    "image_url": image_data_url(f),
                    "detail": "high",
                }
            )

    # ========================================================
    # LLAMADA A OPENAI
    # ========================================================

    try:

        response = client.responses.create(
            model=MODEL,
            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            timeout=600,
        )

    except Exception as e:

        raise RuntimeError(
            f"Error de conexión con OpenAI: "
            f"{type(e).__name__}: {e}"
        ) from e

    # ========================================================
    # OBTENER TEXTO
    # ========================================================

    output_text = getattr(
        response,
        "output_text",
        None,
    )

    if not output_text:

        raise RuntimeError(
            "OpenAI recibió la factura pero no devolvió "
            "ningún resultado de texto."
        )

    # ========================================================
    # CONVERTIR JSON
    # ========================================================

    try:

        extracted = parse_json(output_text)

    except Exception as e:

        raise RuntimeError(
            "La IA respondió, pero la respuesta no tenía "
            "un JSON válido.\n\n"
            f"Respuesta recibida:\n{output_text[:2000]}"
        ) from e

    return normalize(extracted)


# ============================================================
# CÁLCULO DE COMPARATIVA
# ============================================================

def calculate(
    data,
    energy_prices,
    power_prices,
    iva_rate,
    iee_rate,
    include_rental,
    include_other,
    include_reactive,
    include_services,
    include_discounts,
):

    days = max(
        1,
        int(
            num(data.get("dias_facturados"))
            or 30
        ),
    )

    # ========================================================
    # ENERGÍA
    # ========================================================

    energy_rows = []

    energy_base = 0.0

    for i in range(1, 7):

        p = f"P{i}"

        row = data["energia"].get(p, {})

        kwh = num(row.get("kwh"))

        price = num(
            energy_prices.get(p)
        )

        cost = kwh * price

        if kwh:

            energy_rows.append(
                (
                    p,
                    kwh,
                    price,
                    cost,
                )
            )

        energy_base += cost

    # ========================================================
    # POTENCIA
    # ========================================================

    power_rows = []

    power_base = 0.0

    for i in range(1, 7):

        p = f"P{i}"

        row = data["potencia"].get(p, {})

        kw = num(row.get("kw"))

        price = num(
            power_prices.get(p)
        )

        cost = (
            kw
            * price
            * days
        )

        if kw:

            power_rows.append(
                (
                    p,
                    kw,
                    price,
                    days,
                    cost,
                )
            )

        power_base += cost

    # ========================================================
    # EXTRAS
    # ========================================================

    taxable_base = (
        energy_base
        + power_base
    )

    extras = 0.0

    if include_rental:

        extras += num(
            data.get("alquiler_contador")
        )

    if include_other:

        extras += num(
            data.get("otros_conceptos")
        )

    if include_reactive:

        extras += num(
            data.get("reactiva_importe")
        )

    if include_services:

        extras += num(
            data.get("servicios_importe")
        )

    if include_discounts:

        extras -= num(
            data.get("descuentos_importe")
        )

    # ========================================================
    # IMPUESTOS
    # ========================================================

    pre_tax = (
        taxable_base
        + extras
    )

    iee = (
        pre_tax
        * iee_rate
        / 100.0
    )

    base_iva = (
        pre_tax
        + iee
    )

    iva = (
        base_iva
        * iva_rate
        / 100.0
    )

    new_invoice = (
        base_iva
        + iva
    )

    # ========================================================
    # COMPARACIÓN CON FACTURA ACTUAL
    # ========================================================

    current_invoice = num(
        data.get("importe_total")
    )

    current_month = (
        current_invoice
        * 30
        / days
    )

    new_month = (
        new_invoice
        * 30
        / days
    )

    saving_month = (
        current_month
        - new_month
    )

    saving_year = (
        saving_month
        * 12
    )

    return {
        "days": days,

        "energy_rows": energy_rows,
        "power_rows": power_rows,

        "energy_base": energy_base,
        "power_base": power_base,

        "extras": extras,
        "pre_tax": pre_tax,

        "iee": iee,
        "iva": iva,

        "new_invoice": new_invoice,

        "current_month": current_month,
        "new_month": new_month,

        "saving_month": saving_month,
        "saving_year": saving_year,
    }


# ============================================================
# CREACIÓN DEL PDF
# ============================================================

def build_pdf(
    data,
    result,
    energy_prices,
    power_prices,
    company_name,
    logo_bytes=None,
):

    buf = BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Comparativa de ahorro energético",
        author=company_name,
    )

    styles = getSampleStyleSheet()

    title = ParagraphStyle(
        "title2",
        parent=styles["Title"],
        fontSize=21,
        leading=25,
        alignment=TA_CENTER,
    )

    sub = ParagraphStyle(
        "sub2",
        parent=styles["Normal"],
        fontSize=9,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#555555"),
    )

    h2 = ParagraphStyle(
        "h22",
        parent=styles["Heading2"],
        fontSize=13,
        leading=16,
        spaceBefore=9,
        spaceAfter=6,
    )

    normal = ParagraphStyle(
        "normal2",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
    )

    story = []

    # ========================================================
    # CABECERA
    # ========================================================

    story.append(
        Paragraph(
            "COMPARATIVA DE AHORRO ENERGÉTICO",
            title,
        )
    )

    story.append(
        Paragraph(
            company_name,
            sub,
        )
    )

    story.append(
        Spacer(
            1,
            6 * mm,
        )
    )

    # ========================================================
    # DATOS CLIENTE
    # ========================================================

    info = [
        [
            "Cliente",
            data.get("cliente")
            or "—",
            "Comercializadora",
            data.get("comercializadora")
            or "—",
        ],
        [
            "Tarifa",
            data.get("tarifa")
            or "—",
            "CUPS",
            data.get("cups")
            or "—",
        ],
        [
            "Periodo",
            (
                f'{data.get("periodo_inicio") or "—"}'
                f' → '
                f'{data.get("periodo_fin") or "—"}'
            ),
            "Días",
            str(result["days"]),
        ],
    ]

    t = Table(
        info,
        colWidths=[
            28 * mm,
            62 * mm,
            32 * mm,
            60 * mm,
        ],
    )

    t.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (0, -1),
                    colors.HexColor("#F1F3F5"),
                ),
                (
                    "BACKGROUND",
                    (2, 0),
                    (2, -1),
                    colors.HexColor("#F1F3F5"),
                ),
                (
                    "FONTNAME",
                    (0, 0),
                    (0, -1),
                    "Helvetica-Bold",
                ),
                (
                    "FONTNAME",
                    (2, 0),
                    (2, -1),
                    "Helvetica-Bold",
                ),
                (
                    "FONTSIZE",
                    (0, 0),
                    (-1, -1),
                    8,
                ),
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.35,
                    colors.HexColor("#D5D9DD"),
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE",
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    5,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    5,
                ),
            ]
        )
    )

    story.append(t)

    story.append(
        Spacer(
            1,
            7 * mm,
        )
    )

    # ========================================================
    # AHORRO
    # ========================================================

    story.append(
        Paragraph(
            "AHORRO ESTIMADO",
            h2,
        )
    )

    savings = [
        [
            "Factura actual normalizada a 30 días",
            money(result["current_month"]),
        ],
        [
            "Nueva oferta normalizada a 30 días",
            money(result["new_month"]),
        ],
        [
            "AHORRO MENSUAL",
            money(result["saving_month"]),
        ],
        [
            "AHORRO ANUAL ESTIMADO",
            money(result["saving_year"]),
        ],
    ]

    stt = Table(
        savings,
        colWidths=[
            110 * mm,
            68 * mm,
        ],
    )

    stt.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 1),
                    colors.HexColor("#F7F8F9"),
                ),
                (
                    "BACKGROUND",
                    (0, 2),
                    (-1, 3),
                    colors.HexColor("#E8F5EE"),
                ),
                (
                    "FONTNAME",
                    (0, 2),
                    (-1, 3),
                    "Helvetica-Bold",
                ),
                (
                    "FONTSIZE",
                    (0, 0),
                    (-1, -1),
                    9.5,
                ),
                (
                    "FONTSIZE",
                    (1, 3),
                    (1, 3),
                    16,
                ),
                (
                    "TEXTCOLOR",
                    (1, 2),
                    (1, 3),
                    colors.HexColor("#0B7A43"),
                ),
                (
                    "ALIGN",
                    (1, 0),
                    (1, -1),
                    "RIGHT",
                ),
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.35,
                    colors.HexColor("#D5D9DD"),
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    6,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    6,
                ),
            ]
        )
    )

    story.append(stt)

    # ========================================================
    # ENERGÍA
    # ========================================================

    story.append(
        Paragraph(
            "Coste de energía",
            h2,
        )
    )

    erows = [
        [
            "Periodo",
            "Consumo",
            "Precio oferta",
            "Coste",
        ]
    ]

    for (
        p,
        kwh,
        price,
        cost,
    ) in result["energy_rows"]:

        erows.append(
            [
                p,
                f"{kwh:,.2f} kWh",
                f"{price:.6f} €/kWh",
                money(cost),
            ]
        )

    erows.append(
        [
            "TOTAL",
            (
                f'{sum(x[1] for x in result["energy_rows"]):,.2f}'
                " kWh"
            ),
            "",
            money(result["energy_base"]),
        ]
    )

    et = Table(
        erows,
        colWidths=[
            25 * mm,
            50 * mm,
            52 * mm,
            51 * mm,
        ],
    )

    et.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    colors.HexColor("#F1F3F5"),
                ),
                (
                    "FONTNAME",
                    (0, 0),
                    (-1, 0),
                    "Helvetica-Bold",
                ),
                (
                    "FONTNAME",
                    (0, -1),
                    (-1, -1),
                    "Helvetica-Bold",
                ),
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.3,
                    colors.HexColor("#D5D9DD"),
                ),
                (
                    "FONTSIZE",
                    (0, 0),
                    (-1, -1),
                    8,
                ),
                (
                    "ALIGN",
                    (3, 1),
                    (3, -1),
                    "RIGHT",
                ),
            ]
        )
    )

    story.append(et)

    # ========================================================
    # POTENCIA
    # ========================================================

    story.append(
        Paragraph(
            "Coste de potencia",
            h2,
        )
    )

    prows = [
        [
            "Periodo",
            "Potencia",
            "Precio oferta",
            "Días",
            "Coste",
        ]
    ]

    for (
        p,
        kw,
        price,
        days,
        cost,
    ) in result["power_rows"]:

        prows.append(
            [
                p,
                f"{kw:,.2f} kW",
                f"{price:.6f} €/kW/día",
                str(days),
                money(cost),
            ]
        )

    prows.append(
        [
            "TOTAL",
            "",
            "",
            "",
            money(result["power_base"]),
        ]
    )

    pt = Table(
        prows,
        colWidths=[
            20 * mm,
            39 * mm,
            55 * mm,
            20 * mm,
            44 * mm,
        ],
    )

    pt.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    colors.HexColor("#F1F3F5"),
                ),
                (
                    "FONTNAME",
                    (0, 0),
                    (-1, 0),
                    "Helvetica-Bold",
                ),
                (
                    "FONTNAME",
                    (0, -1),
                    (-1, -1),
                    "Helvetica-Bold",
                ),
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.3,
                    colors.HexColor("#D5D9DD"),
                ),
                (
                    "FONTSIZE",
                    (0, 0),
                    (-1, -1),
                    8,
                ),
                (
                    "ALIGN",
                    (4, 1),
                    (4, -1),
                    "RIGHT",
                ),
            ]
        )
    )

    story.append(pt)

    # ========================================================
    # FISCALIDAD
    # ========================================================

    story.append(
        Paragraph(
            "Fiscalidad y otros conceptos",
            h2,
        )
    )

    tax = [
        [
            "Base antes de impuestos",
            money(result["pre_tax"]),
        ],
        [
            "Impuesto eléctrico",
            money(result["iee"]),
        ],
        [
            "IVA",
            money(result["iva"]),
        ],
        [
            "Total nueva oferta",
            money(result["new_invoice"]),
        ],
    ]

    xt = Table(
        tax,
        colWidths=[
            110 * mm,
            68 * mm,
        ],
    )

    xt.setStyle(
        TableStyle(
            [
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.3,
                    colors.HexColor("#D5D9DD"),
                ),
                (
                    "FONTSIZE",
                    (0, 0),
                    (-1, -1),
                    8.5,
                ),
                (
                    "ALIGN",
                    (1, 0),
                    (1, -1),
                    "RIGHT",
                ),
                (
                    "FONTNAME",
                    (0, -1),
                    (-1, -1),
                    "Helvetica-Bold",
                ),
            ]
        )
    )

    story.append(xt)

    story.append(
        Spacer(
            1,
            5 * mm,
        )
    )

    story.append(
        Paragraph(
            "La comparativa es una estimación basada en los "
            "datos extraídos de la factura y en los precios "
            "configurados en la oferta. Conviene revisar los "
            "campos antes de presentar el documento al cliente.",
            normal,
        )
    )

    # ========================================================
    # CREAR PDF
    # ========================================================

    doc.build(story)

    return buf.getvalue()


# ============================================================
# INTERFAZ
# ============================================================

st.title(
    "⚡ Comparador de Facturas de Luz"
)

st.caption(
    "Lectura de factura con IA · "
    "Comparativa de oferta · "
    "Ahorro mensual y anual · "
    "PDF comercial"
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header(
        "⚙️ Configuración"
    )

    st.text_input(
        "API key de OpenAI",
        type="password",
        key="api_key_input",
        help=(
            "Solo se utiliza durante esta sesión. "
            "Para publicar la aplicación es mejor utilizar "
            "OPENAI_API_KEY en Secrets."
        ),
    )

    if st.session_state.get(
        "api_key_input"
    ):

        st.session_state[
            "api_key"
        ] = st.session_state[
            "api_key_input"
        ]

    company_name = st.text_input(
        "Nombre de la empresa",
        "Greening Energy",
    )

    logo = st.file_uploader(
        "Logo (opcional)",
        type=[
            "png",
            "jpg",
            "jpeg",
        ],
    )

    # ========================================================
    # IMPUESTOS
    # ========================================================

    st.subheader(
        "Impuestos"
    )

    iva_rate = st.number_input(
        "IVA (%)",
        min_value=0.0,
        max_value=30.0,
        value=21.0,
        step=0.5,
    )

    iee_rate = st.number_input(
        "Impuesto eléctrico (%)",
        min_value=0.0,
        max_value=20.0,
        value=5.1127,
        step=0.0001,
    )

    # ========================================================
    # ENERGÍA
    # ========================================================

    st.subheader(
        "Precios de energía (€/kWh)"
    )

    energy_prices = {}

    for p in range(1, 7):

        energy_prices[f"P{p}"] = st.number_input(
            p,
            value=float(
                DEFAULT_ENERGY[f"P{p}"]
            ),
            step=0.000001,
            format="%.6f",
            key=f"ep_{p}",
        )

    # ========================================================
    # POTENCIA
    # ========================================================

    st.subheader(
        "Precios de potencia (€/kW/día)"
    )

    power_prices = {}

    for p in range(1, 7):

        power_prices[f"P{p}"] = st.number_input(
            p,
            value=float(
                DEFAULT_POWER[f"P{p}"]
            ),
            step=0.000001,
            format="%.6f",
            key=f"pp_{p}",
        )

    # ========================================================
    # CONCEPTOS
    # ========================================================

    st.subheader(
        "Conceptos"
    )

    include_rental = st.checkbox(
        "Incluir alquiler de contador",
        value=True,
    )

    include_other = st.checkbox(
        "Incluir otros conceptos",
        value=False,
    )

    include_reactive = st.checkbox(
        "Incluir energía reactiva",
        value=False,
    )

    include_services = st.checkbox(
        "Incluir servicios extra",
        value=False,
    )

    include_discounts = st.checkbox(
        "Aplicar descuentos de la factura",
        value=True,
    )


# ============================================================
# SUBIR FACTURA
# ============================================================

st.info(
    "Sube una factura PDF o varias fotografías. "
    "La IA leerá los documentos y después podrás "
    "revisar y corregir los datos antes de calcular."
)


files = st.file_uploader(
    "📎 Adjunta la factura",
    type=[
        "pdf",
        "png",
        "jpg",
        "jpeg",
        "webp",
    ],
    accept_multiple_files=True,
)


# ============================================================
# BOTÓN ANALIZAR
# ============================================================

if files and st.button(
    "🔎 Analizar factura con IA",
    type="primary",
    use_container_width=True,
):

    with st.spinner(
        "Leyendo y comprobando la factura..."
    ):

        try:

            bill_data = analyze_files(
                files
            )

            st.session_state[
                "bill_data"
            ] = bill_data

            st.success(
                "Factura analizada correctamente. "
                "Revisa los datos antes de generar "
                "la comparativa."
            )

        except Exception as e:

            st.error(
                "❌ No se ha podido analizar la factura."
            )

            st.code(
                f"{type(e).__name__}: {e}",
                language="text",
            )

            st.info(
                "Si el error vuelve a aparecer, "
                "este detalle técnico permitirá "
                "identificar exactamente el problema."
            )


# ============================================================
# DATOS EXTRAÍDOS
# ============================================================

data = st.session_state.get(
    "bill_data"
)


if data:

    st.subheader(
        "1. Datos extraídos"
    )

    confidence = num(
        data.get("confianza")
    )

    if confidence < 70:

        st.warning(
            f"Nivel de confianza de lectura: "
            f"{confidence:.0f}%. "
            "Revisa especialmente consumos, "
            "potencia y total."
        )

    else:

        st.success(
            f"Nivel de confianza de lectura: "
            f"{confidence:.0f}%."
        )

    # ========================================================
    # DATOS GENERALES
    # ========================================================

    c1, c2, c3, c4 = st.columns(4)

    data["cliente"] = c1.text_input(
        "Cliente",
        data.get("cliente") or "",
    )

    data["comercializadora"] = c2.text_input(
        "Comercializadora",
        data.get("comercializadora") or "",
    )

    data["tarifa"] = c3.text_input(
        "Tarifa",
        data.get("tarifa") or "",
    )

    data["cups"] = c4.text_input(
        "CUPS",
        data.get("cups") or "",
    )

    c1, c2, c3, c4 = st.columns(4)

    data["periodo_inicio"] = c1.text_input(
        "Inicio",
        data.get("periodo_inicio") or "",
    )

    data["periodo_fin"] = c2.text_input(
        "Fin",
        data.get("periodo_fin") or "",
    )

    data["dias_facturados"] = c3.number_input(
        "Días facturados",
        min_value=1,
        value=max(
            1,
            int(
                num(
                    data.get(
                        "dias_facturados"
                    )
                )
                or 30
            ),
        ),
    )

    data["importe_total"] = c4.number_input(
        "Total factura (€)",
        min_value=0.0,
        value=num(
            data.get(
                "importe_total"
            )
        ),
        step=0.01,
    )

    # ========================================================
    # ENERGÍA
    # ========================================================

    st.markdown(
        "### Energía"
    )

    ecols = st.columns(6)

    for i in range(1, 7):

        p = f"P{i}"

        row = data[
            "energia"
        ][p]

        with ecols[i - 1]:

            row["kwh"] = st.number_input(
                f"{p} kWh",
                min_value=0.0,
                value=num(
                    row.get("kwh")
                ),
                step=1.0,
                key=f"kwh_{p}",
            )

            row["precio"] = st.number_input(
                f"{p} precio",
                min_value=0.0,
                value=num(
                    row.get("precio")
                ),
                step=0.000001,
                format="%.6f",
                key=f"bill_ep_{p}",
            )

            row["importe"] = st.number_input(
                f"{p} importe",
                min_value=0.0,
                value=num(
                    row.get("importe")
                ),
                step=0.01,
                key=f"bill_ei_{p}",
            )

    # ========================================================
    # POTENCIA
    # ========================================================

    st.markdown(
        "### Potencia"
    )

    pcols = st.columns(6)

    for i in range(1, 7):

        p = f"P{i}"

        row = data[
            "potencia"
        ][p]

        with pcols[i - 1]:

            row["kw"] = st.number_input(
                f"{p} kW",
                min_value=0.0,
                value=num(
                    row.get("kw")
                ),
                step=0.1,
                key=f"kw_{p}",
            )

            row["precio"] = st.number_input(
                f"{p} precio",
                min_value=0.0,
                value=num(
                    row.get("precio")
                ),
                step=0.000001,
                format="%.6f",
                key=f"bill_pp_{p}",
            )

            row["importe"] = st.number_input(
                f"{p} importe",
                min_value=0.0,
                value=num(
                    row.get("importe")
                ),
                step=0.01,
                key=f"bill_pi_{p}",
            )

    # ========================================================
    # OTROS DATOS
    # ========================================================

    with st.expander(
        "Otros datos detectados / revisión"
    ):

        cc = st.columns(5)

        data["alquiler_contador"] = cc[0].number_input(
            "Alquiler contador",
            min_value=0.0,
            value=num(
                data.get(
                    "alquiler_contador"
                )
            ),
            step=0.01,
        )

        data["otros_conceptos"] = cc[1].number_input(
            "Otros conceptos",
            min_value=0.0,
            value=num(
                data.get(
                    "otros_conceptos"
                )
            ),
            step=0.01,
        )

        data["reactiva_importe"] = cc[2].number_input(
            "Reactiva",
            min_value=0.0,
            value=num(
                data.get(
                    "reactiva_importe"
                )
            ),
            step=0.01,
        )

        data["servicios_importe"] = cc[3].number_input(
            "Servicios",
            min_value=0.0,
            value=num(
                data.get(
                    "servicios_importe"
                )
            ),
            step=0.01,
        )

        data["descuentos_importe"] = cc[4].number_input(
            "Descuentos",
            min_value=0.0,
            value=num(
                data.get(
                    "descuentos_importe"
                )
            ),
            step=0.01,
        )

        if data.get(
            "observaciones"
        ):

            st.write(
                "Observaciones de la IA:",
                data[
                    "observaciones"
                ],
            )


    # ========================================================
    # COMPARATIVA
    # ========================================================

    st.subheader(
        "2. Comparativa"
    )

    result = calculate(
        data,
        energy_prices,
        power_prices,
        iva_rate,
        iee_rate,
        include_rental,
        include_other,
        include_reactive,
        include_services,
        include_discounts,
    )

    # ========================================================
    # MÉTRICAS
    # ========================================================

    m1, m2, m3, m4 = st.columns(4)

    m1.metric(
        "Factura actual / 30 días",
        money(
            result["current_month"]
        ),
    )

    m2.metric(
        "Nueva oferta / 30 días",
        money(
            result["new_month"]
        ),
    )

    m3.metric(
        "Ahorro mensual",
        money(
            result["saving_month"]
        ),
    )

    m4.metric(
        "Ahorro anual",
        money(
            result["saving_year"]
        ),
    )

    # ========================================================
    # MENSAJE AHORRO
    # ========================================================

    if result["saving_month"] > 0:

        st.success(
            f"💰 Ahorro estimado: "
            f"{money(result['saving_month'])} "
            f"al mes · "
            f"{money(result['saving_year'])} "
            f"al año."
        )

    elif result["saving_month"] < 0:

        st.warning(
            f"La oferta configurada resulta "
            f"{money(abs(result['saving_month']))} "
            "más cara al mes con estos datos."
        )

    else:

        st.info(
            "Con los precios configurados, "
            "el ahorro estimado es 0 €."
        )

    # ========================================================
    # DESGLOSE
    # ========================================================

    st.markdown(
        "### Desglose de la nueva oferta"
    )

    rows = []

    for (
        p,
        kwh,
        price,
        cost,
    ) in result["energy_rows"]:

        rows.append(
            {
                "Tipo": "Energía",
                "Periodo": p,
                "Cantidad": f"{kwh:.2f} kWh",
                "Precio": f"{price:.6f}",
                "Coste": money(cost),
            }
        )

    for (
        p,
        kw,
        price,
        days,
        cost,
    ) in result["power_rows"]:

        rows.append(
            {
                "Tipo": "Potencia",
                "Periodo": p,
                "Cantidad": f"{kw:.2f} kW",
                "Precio": f"{price:.6f}",
                "Coste": money(cost),
            }
        )

    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
    )

    # ========================================================
    # PDF
    # ========================================================

    st.markdown(
        "### 3. Generar PDF comercial"
    )

    pdf = build_pdf(
        data,
        result,
        energy_prices,
        power_prices,
        company_name,
        logo.getvalue()
        if logo
        else None,
    )

    filename_client = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        data.get("cliente")
        or "cliente",
    )

    st.download_button(
        "📥 Descargar comparativa en PDF",
        data=pdf,
        file_name=(
            f"Comparativa_"
            f"{filename_client}.pdf"
        ),
        mime="application/pdf",
        use_container_width=True,
    )


# ============================================================
# PANTALLA INICIAL
# ============================================================

else:

    st.markdown(
        """
### Cómo funciona

1. **Adjunta** la factura en PDF o fotos.
2. **Analiza con IA**.
3. La IA extrae:
   - Cliente
   - CUPS
   - Comercializadora
   - Tarifa
   - Fechas
   - Días facturados
   - Consumo P1-P6
   - Potencia P1-P6
   - Precios
   - Impuestos
   - Descuentos
   - Servicios
   - Reactiva
   - Total de factura
4. **Revisa y corrige** los datos detectados.
5. Configura tus precios de **Greening Energy**.
6. Obtén automáticamente:
   - Coste actual
   - Coste con tu oferta
   - Ahorro mensual
   - Ahorro anual
7. **Descarga un PDF profesional** para presentárselo al cliente.
"""
    )
