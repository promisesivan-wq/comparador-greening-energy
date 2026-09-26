# ⚡ Comparador de Facturas de Luz — Greening Energy

Aplicación Streamlit para analizar facturas eléctricas en PDF o fotografías con la API de OpenAI, calcular una oferta y generar un PDF comercial.

## Incluye

- PDF y múltiples imágenes.
- Lectura mediante visión/IA.
- Extracción de cliente, CUPS, tarifa, periodo, total, energía P1-P6 y potencia P1-P6.
- Edición manual de los datos extraídos.
- Precios de oferta editables.
- IVA e impuesto eléctrico configurables.
- Reactiva y servicios configurables.
- Ahorro mensual y anual.
- PDF comercial descargable.
- Logo opcional.

## Publicarlo

Streamlit Community Cloud permite desplegar una app desde GitHub. Necesitas una cuenta de GitHub y una clave de API de OpenAI.

1. Sube esta carpeta a un repositorio de GitHub.
2. Entra en https://share.streamlit.io/
3. Crea una app y selecciona `app.py`.
4. En Advanced settings > Secrets añade:

```toml
OPENAI_API_KEY = "TU_CLAVE_DE_OPENAI"
```

No subas nunca la clave al repositorio.

## Ejecutarlo en local

```bash
pip install -r requirements.txt
streamlit run app.py
```

También puedes introducir la API key directamente en la barra lateral durante la sesión.

## Importante

La IA no debe considerarse infalible: la aplicación muestra el nivel de confianza y permite editar los datos antes de generar el PDF.
