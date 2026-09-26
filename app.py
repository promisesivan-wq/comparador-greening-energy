
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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

st.set_page_config(
    page_title="Comparador de Facturas de Luz | Greening Energy",
    page_icon="⚡",
    layout="wide",
)

MODEL = "gpt-5.6-luna"

DEFAULT_ENERGY = {"P1": 0.095010, "P2": 0.090360, "P3": 0.092514, "P4": 0.0, "P5": 0.0, "P6": 0.0}
DEFAULT_POWER = {
    "P1": 0.055827, "P2": 0.029089, "P3": 0.012278,
    "P4": 0.010647, "P5": 0.006887, "P6": 0.003951,
}

EMPTY_PERIODS = {
    f"P{i}": {"kwh": None, "precio": None, "importe": None}
    for i in range(1, 7)
}
EMPTY_POWER = {
    f"P{i}": {"kw": None, "precio": None, "importe": None}
    for i in range(1, 7)
}

SYSTEM_PROMPT = """
Eres un especialista español en facturas eléctricas. Analiza TODOS los documentos
adjuntos (PDF o fotografías) como una única factura. Extrae solamente datos que
sean visibles o claramente deducibles. NO inventes valores.

Devuelve EXCLUSIVAMENTE JSON válido con esta estructura:
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
    "P1": {"kwh": null, "precio": null, "importe": null},
    "P2": {"kwh": null, "precio": null, "importe": null},
    "P3": {"kwh": null, "precio": null, "importe": null},
    "P4": {"kwh": null, "precio": null, "importe": null},
    "P5": {"kwh": null, "precio": null, "importe": null},
    "P6": {"kwh": null, "precio": null, "importe": null}
  },
  "potencia": {
    "P1": {"kw": null, "precio": null, "importe": null},
    "P2": {"kw": null, "precio": null, "importe": null},
    "P3": {"kw": null, "precio": null, "importe": null},
    "P4": {"kw": null, "precio": null, "importe": null},
    "P5": {"kw": null, "precio": null, "importe": null},
    "P6": {"kw": null, "precio": null, "importe": null}
  },
  "reactiva_importe": null,
  "descuentos_importe": null,
  "servicios_importe": null,
  "confianza": 0,
  "observaciones": []
}

REGLAS:
- Usa números con punto decimal.
- Si no aparece un dato, usa null.
- No confundas kW con kWh.
- Respeta P1-P6.
- En 2.0TD normalmente hay energía P1-P3 y potencia P1-P2.
- Si hay varias páginas/fotos, fusiona los datos y no los dupliques.
- importe_total debe ser el TOTAL FINAL DE LA FACTURA.
- Los descuentos deben conservar su importe como valor positivo en la extracción.
- confianza debe ser 0-100.
- observaciones debe contener cualquier dato que pueda requerir revisión manual.
"""

def num(x):
    try:
        if x is None or x == "":
            return 0.0
        return float(Decimal(str(x).replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0

def money(x):
    return f"{num(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"

def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

def normalize(data):
    data = data or {}
    data.setdefault("energia", {})
    data.setdefault("potencia", {})
    for p in range(1, 7):
        data["energia"].setdefault(f"P{p}", {"kwh": None, "precio": None, "importe": None})
        data["potencia"].setdefault(f"P{p}", {"kw": None, "precio": None, "importe": None})
    data.setdefault("observaciones", [])
    return data

def get_api_key():
    try:
        key = st.secrets.get("OPENAI_API_KEY", "")
    except Exception:
        key = ""
    if key:
        return key
    return st.session_state.get("api_key", "")

def get_client():
    key = get_api_key()
    return OpenAI(api_key=key) if key else None

def image_data_url(uploaded):
    raw = uploaded.getvalue()
    mime = uploaded.type or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"

def analyze_files(files):
    client = get_client()
    if not client:
        raise RuntimeError("Introduce la API key de OpenAI en la barra lateral o configúrala como secreto OPENAI_API_KEY.")

    content = [{"type": "input_text", "text": SYSTEM_PROMPT + "\nAnaliza ahora la factura."}]
    for f in files:
        if f.name.lower().endswith(".pdf"):
            uploaded = client.files.create(
                file=(f.name, f.getvalue(), f.type or "application/pdf"),
                purpose="user_data",
            )
            content.append({"type": "input_file", "file_id": uploaded.id})
        else:
            content.append({
                "type": "input_image",
                "image_url": image_data_url(f),
                "detail": "high",
            })

    response = client.responses.create(
        model=MODEL,
        input=[{"role": "user", "content": content}],
    )
    return normalize(parse_json(response.output_text))

def edit_number(label, value, key, step=0.000001, decimals=6):
    v = num(value)
    return st.number_input(label, value=v, step=step, format=f"%.{decimals}f", key=key)

def calculate(data, energy_prices, power_prices, iva_rate, iee_rate,
              include_rental, include_other, include_reactive,
              include_services, include_discounts):
    days = max(1, int(num(data.get("dias_facturados")) or 30))

    energy_rows = []
    energy_base = 0.0
    for i in range(1, 7):
        p = f"P{i}"
        row = data["energia"].get(p, {})
        kwh = num(row.get("kwh"))
        price = num(energy_prices.get(p))
        cost = kwh * price
        if kwh:
            energy_rows.append((p, kwh, price, cost))
        energy_base += cost

    power_rows = []
    power_base = 0.0
    for i in range(1, 7):
        p = f"P{i}"
        row = data["potencia"].get(p, {})
        kw = num(row.get("kw"))
        price = num(power_prices.get(p))
        cost = kw * price * days
        if kw:
            power_rows.append((p, kw, price, days, cost))
        power_base += cost

    taxable_base = energy_base + power_base
    extras = 0.0
    if include_rental:
        extras += num(data.get("alquiler_contador"))
    if include_other:
        extras += num(data.get("otros_conceptos"))
    if include_reactive:
        extras += num(data.get("reactiva_importe"))
    if include_services:
        extras += num(data.get("servicios_importe"))
    if include_discounts:
        extras -= num(data.get("descuentos_importe"))

    pre_tax = taxable_base + extras
    iee = pre_tax * iee_rate / 100.0
    base_iva = pre_tax + iee
    iva = base_iva * iva_rate / 100.0
    new_invoice = base_iva + iva

    current_invoice = num(data.get("importe_total"))
    current_month = current_invoice * 30 / days
    new_month = new_invoice * 30 / days
    saving_month = current_month - new_month
    saving_year = saving_month * 12

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

def build_pdf(data, result, energy_prices, power_prices, company_name, logo_bytes=None):
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=15*mm, leftMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm,
        title="Comparativa de ahorro energético", author=company_name
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title2", parent=styles["Title"], fontSize=21, leading=25, alignment=TA_CENTER)
    sub = ParagraphStyle("sub2", parent=styles["Normal"], fontSize=9, alignment=TA_CENTER, textColor=colors.HexColor("#555555"))
    h2 = ParagraphStyle("h22", parent=styles["Heading2"], fontSize=13, leading=16, spaceBefore=9, spaceAfter=6)
    normal = ParagraphStyle("normal2", parent=styles["Normal"], fontSize=8.5, leading=11)
    story = []

    story.append(Paragraph("COMPARATIVA DE AHORRO ENERGÉTICO", title))
    story.append(Paragraph(company_name, sub))
    story.append(Spacer(1, 6*mm))

    info = [
        ["Cliente", data.get("cliente") or "—", "Comercializadora", data.get("comercializadora") or "—"],
        ["Tarifa", data.get("tarifa") or "—", "CUPS", data.get("cups") or "—"],
        ["Periodo", f'{data.get("periodo_inicio") or "—"} → {data.get("periodo_fin") or "—"}', "Días", str(result["days"])],
    ]
    t = Table(info, colWidths=[28*mm, 62*mm, 32*mm, 60*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(0,-1),colors.HexColor("#F1F3F5")),
        ("BACKGROUND",(2,0),(2,-1),colors.HexColor("#F1F3F5")),
        ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),
        ("FONTNAME",(2,0),(2,-1),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),8),
        ("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D5D9DD")),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),5),
        ("BOTTOMPADDING",(0,0),(-1,-1),5),
    ]))
    story.append(t)

    story.append(Spacer(1, 7*mm))
    story.append(Paragraph("AHORRO ESTIMADO", h2))
    savings = [
        ["Factura actual normalizada a 30 días", money(result["current_month"])],
        ["Nueva oferta normalizada a 30 días", money(result["new_month"])],
        ["AHORRO MENSUAL", money(result["saving_month"])],
        ["AHORRO ANUAL ESTIMADO", money(result["saving_year"])],
    ]
    stt = Table(savings, colWidths=[110*mm, 68*mm])
    stt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,1),colors.HexColor("#F7F8F9")),
        ("BACKGROUND",(0,2),(-1,3),colors.HexColor("#E8F5EE")),
        ("FONTNAME",(0,2),(-1,3),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),9.5),
        ("FONTSIZE",(1,3),(1,3),16),
        ("TEXTCOLOR",(1,2),(1,3),colors.HexColor("#0B7A43")),
        ("ALIGN",(1,0),(1,-1),"RIGHT"),
        ("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D5D9DD")),
        ("TOPPADDING",(0,0),(-1,-1),6),
        ("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story.append(stt)

    story.append(Paragraph("Coste de energía", h2))
    erows = [["Periodo", "Consumo", "Precio oferta", "Coste"]]
    for p, kwh, price, cost in result["energy_rows"]:
        erows.append([p, f"{kwh:,.2f} kWh", f"{price:.6f} €/kWh", money(cost)])
    erows.append(["TOTAL", f'{sum(x[1] for x in result["energy_rows"]):,.2f} kWh', "", money(result["energy_base"])])
    et = Table(erows, colWidths=[25*mm, 50*mm, 52*mm, 51*mm])
    et.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#F1F3F5")),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),
        ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#D5D9DD")),
        ("FONTSIZE",(0,0),(-1,-1),8),
        ("ALIGN",(3,1),(3,-1),"RIGHT"),
    ]))
    story.append(et)

    story.append(Paragraph("Coste de potencia", h2))
    prows = [["Periodo", "Potencia", "Precio oferta", "Días", "Coste"]]
    for p, kw, price, days, cost in result["power_rows"]:
        prows.append([p, f"{kw:,.2f} kW", f"{price:.6f} €/kW/día", str(days), money(cost)])
    prows.append(["TOTAL", "", "", "", money(result["power_base"])])
    pt = Table(prows, colWidths=[20*mm, 39*mm, 55*mm, 20*mm, 44*mm])
    pt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#F1F3F5")),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),
        ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#D5D9DD")),
        ("FONTSIZE",(0,0),(-1,-1),8),
        ("ALIGN",(4,1),(4,-1),"RIGHT"),
    ]))
    story.append(pt)

    story.append(Paragraph("Fiscalidad y otros conceptos", h2))
    tax = [
        ["Base antes de impuestos", money(result["pre_tax"])],
        ["Impuesto eléctrico", money(result["iee"])],
        ["IVA", money(result["iva"])],
        ["Total nueva oferta", money(result["new_invoice"])],
    ]
    xt = Table(tax, colWidths=[110*mm, 68*mm])
    xt.setStyle(TableStyle([
        ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#D5D9DD")),
        ("FONTSIZE",(0,0),(-1,-1),8.5),
        ("ALIGN",(1,0),(1,-1),"RIGHT"),
        ("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),
    ]))
    story.append(xt)
    story.append(Spacer(1, 5*mm))
    story.append(Paragraph(
        "La comparativa es una estimación basada en los datos extraídos de la factura y en los precios configurados en la oferta. "
        "Conviene revisar los campos marcados para verificación antes de presentar el documento al cliente.",
        normal
    ))

    doc.build(story)
    return buf.getvalue()

st.title("⚡ Comparador de Facturas de Luz")
st.caption("Lectura de factura con IA · Comparativa de oferta · Ahorro mensual y anual · PDF comercial")

with st.sidebar:
    st.header("⚙️ Configuración")
    st.text_input("API key de OpenAI", type="password", key="api_key_input",
                  help="Solo se usa durante esta sesión. Para publicar la app, es mejor usar OPENAI_API_KEY en Secrets.")
    if st.session_state.get("api_key_input"):
        st.session_state["api_key"] = st.session_state["api_key_input"]

    company_name = st.text_input("Nombre de la empresa", "Greening Energy")
    logo = st.file_uploader("Logo (opcional)", type=["png", "jpg", "jpeg"])

    st.subheader("Impuestos")
    iva_rate = st.number_input("IVA (%)", min_value=0.0, max_value=30.0, value=21.0, step=0.5)
    iee_rate = st.number_input("Impuesto eléctrico (%)", min_value=0.0, max_value=20.0, value=5.1127, step=0.0001)

    st.subheader("Precios de energía (€/kWh)")
    energy_prices = {}
    for p in range(1, 7):
        energy_prices[f"P{p}"] = st.number_input(
            p, value=float(DEFAULT_ENERGY[f"P{p}"]), step=0.000001, format="%.6f", key=f"ep_{p}"
        )

    st.subheader("Precios de potencia (€/kW/día)")
    power_prices = {}
    for p in range(1, 7):
        power_prices[f"P{p}"] = st.number_input(
            p, value=float(DEFAULT_POWER[f"P{p}"]), step=0.000001, format="%.6f", key=f"pp_{p}"
        )

    st.subheader("Conceptos")
    include_rental = st.checkbox("Incluir alquiler de contador", value=True)
    include_other = st.checkbox("Incluir otros conceptos", value=False)
    include_reactive = st.checkbox("Incluir energía reactiva", value=False)
    include_services = st.checkbox("Incluir servicios extra", value=False)
    include_discounts = st.checkbox("Aplicar descuentos de la factura", value=True)

st.info("Sube una factura PDF o varias fotografías. La IA leerá los documentos y después podrás revisar/corregir los datos antes de calcular.")

files = st.file_uploader(
    "📎 Adjunta la factura",
    type=["pdf", "png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
)

if files and st.button("🔎 Analizar factura con IA", type="primary", use_container_width=True):
    with st.spinner("Leyendo y comprobando la factura..."):
        try:
            st.session_state["bill_data"] = analyze_files(files)
            st.success("Factura analizada. Revisa los datos antes de generar la comparativa.")
        except Exception as e:
            st.error(f"No se ha podido analizar la factura: {e}")

data = st.session_state.get("bill_data")

if data:
    st.subheader("1. Datos extraídos")
    confidence = num(data.get("confianza"))
    if confidence < 70:
        st.warning(f"Nivel de confianza de lectura: {confidence:.0f}%. Revisa especialmente consumos, potencia y total.")
    else:
        st.success(f"Nivel de confianza de lectura: {confidence:.0f}%.")

    c1, c2, c3, c4 = st.columns(4)
    data["cliente"] = c1.text_input("Cliente", data.get("cliente") or "")
    data["comercializadora"] = c2.text_input("Comercializadora", data.get("comercializadora") or "")
    data["tarifa"] = c3.text_input("Tarifa", data.get("tarifa") or "")
    data["cups"] = c4.text_input("CUPS", data.get("cups") or "")

    c1, c2, c3, c4 = st.columns(4)
    data["periodo_inicio"] = c1.text_input("Inicio", data.get("periodo_inicio") or "")
    data["periodo_fin"] = c2.text_input("Fin", data.get("periodo_fin") or "")
    data["dias_facturados"] = c3.number_input("Días facturados", min_value=1, value=max(1, int(num(data.get("dias_facturados")) or 30)))
    data["importe_total"] = c4.number_input("Total factura (€)", min_value=0.0, value=num(data.get("importe_total")), step=0.01)

    st.markdown("### Energía")
    ecols = st.columns(6)
    for i in range(1, 7):
        p = f"P{i}"
        row = data["energia"][p]
        with ecols[i-1]:
            row["kwh"] = st.number_input(f"{p} kWh", min_value=0.0, value=num(row.get("kwh")), step=1.0, key=f"kwh_{p}")
            row["precio"] = st.number_input(f"{p} precio", min_value=0.0, value=num(row.get("precio")), step=0.000001, format="%.6f", key=f"bill_ep_{p}")
            row["importe"] = st.number_input(f"{p} importe", min_value=0.0, value=num(row.get("importe")), step=0.01, key=f"bill_ei_{p}")

    st.markdown("### Potencia")
    pcols = st.columns(6)
    for i in range(1, 7):
        p = f"P{i}"
        row = data["potencia"][p]
        with pcols[i-1]:
            row["kw"] = st.number_input(f"{p} kW", min_value=0.0, value=num(row.get("kw")), step=0.1, key=f"kw_{p}")
            row["precio"] = st.number_input(f"{p} precio", min_value=0.0, value=num(row.get("precio")), step=0.000001, format="%.6f", key=f"bill_pp_{p}")
            row["importe"] = st.number_input(f"{p} importe", min_value=0.0, value=num(row.get("importe")), step=0.01, key=f"bill_pi_{p}")

    with st.expander("Otros datos detectados / revisión"):
        cc = st.columns(5)
        data["alquiler_contador"] = cc[0].number_input("Alquiler contador", min_value=0.0, value=num(data.get("alquiler_contador")), step=0.01)
        data["otros_conceptos"] = cc[1].number_input("Otros conceptos", min_value=0.0, value=num(data.get("otros_conceptos")), step=0.01)
        data["reactiva_importe"] = cc[2].number_input("Reactiva", min_value=0.0, value=num(data.get("reactiva_importe")), step=0.01)
        data["servicios_importe"] = cc[3].number_input("Servicios", min_value=0.0, value=num(data.get("servicios_importe")), step=0.01)
        data["descuentos_importe"] = cc[4].number_input("Descuentos", min_value=0.0, value=num(data.get("descuentos_importe")), step=0.01)
        if data.get("observaciones"):
            st.write("Observaciones de la IA:", data["observaciones"])

    st.subheader("2. Comparativa")
    result = calculate(
        data, energy_prices, power_prices, iva_rate, iee_rate,
        include_rental, include_other, include_reactive, include_services, include_discounts
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Factura actual / 30 días", money(result["current_month"]))
    m2.metric("Nueva oferta / 30 días", money(result["new_month"]))
    m3.metric("Ahorro mensual", money(result["saving_month"]))
    m4.metric("Ahorro anual", money(result["saving_year"]))

    if result["saving_month"] > 0:
        st.success(f"💰 Ahorro estimado: {money(result['saving_month'])} al mes · {money(result['saving_year'])} al año.")
    elif result["saving_month"] < 0:
        st.warning(f"La oferta configurada resulta {money(abs(result['saving_month']))} más cara al mes con estos datos.")
    else:
        st.info("Con los precios configurados, el ahorro estimado es 0 €.")

    st.markdown("### Desglose de la nueva oferta")
    rows = []
    for p, kwh, price, cost in result["energy_rows"]:
        rows.append({"Tipo": "Energía", "Periodo": p, "Cantidad": f"{kwh:.2f} kWh", "Precio": f"{price:.6f}", "Coste": money(cost)})
    for p, kw, price, days, cost in result["power_rows"]:
        rows.append({"Tipo": "Potencia", "Periodo": p, "Cantidad": f"{kw:.2f} kW", "Precio": f"{price:.6f}", "Coste": money(cost)})
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.markdown("### 3. Generar PDF comercial")
    pdf = build_pdf(
        data, result, energy_prices, power_prices, company_name,
        logo.getvalue() if logo else None
    )
    filename_client = re.sub(r"[^A-Za-z0-9_-]+", "_", data.get("cliente") or "cliente")
    st.download_button(
        "📥 Descargar comparativa en PDF",
        data=pdf,
        file_name=f"Comparativa_{filename_client}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )
else:
    st.markdown("""
### Cómo funciona

1. **Adjunta** la factura en PDF o fotos.
2. **Analiza con IA**: extrae cliente, CUPS, tarifa, consumo, potencia, precios, impuestos y total.
3. **Revisa** los datos detectados y corrige cualquier campo si fuera necesario.
4. **Configura** tus precios de energía, potencia e impuestos.
5. Obtén automáticamente **ahorro mensual y anual**.
6. **Descarga un PDF profesional** para presentárselo al cliente.
""")
