# -*- coding: utf-8 -*-
import io
from datetime import datetime
import numpy as np
import pandas as pd
import streamlit as st

# Mapa
import folium
from folium.plugins import MarkerCluster, HeatMap, MeasureControl
from streamlit_folium import st_folium
from folium import Element, MacroElement
from jinja2 import Template

# PDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader

# ===================== CONFIG =====================
st.set_page_config(page_title="Dashboard de Avances – Encuestas", layout="wide")
st.title("📈 Dashboard de Avances – Encuestas")
st.caption("Conteos grandes, mapa, descarga de PNG y PDF de avance.")

# ======== Utilidades ========
META_COLS = {"ObjectID", "GlobalID", "instance_id", "CreationDate", "EditDate", "Creator", "Editor"}

def normalize_string(s: str) -> str:
    if pd.isna(s): return ""
    s = str(s).strip().lower()
    return " ".join(s.split())

def normalize_factors(s: str) -> str:
    if pd.isna(s): return ""
    parts = [normalize_string(p) for p in str(s).replace(";", ",").split(",")]
    parts = [p for p in parts if p]
    parts.sort()
    return ",".join(parts)

def detect_duplicates(df, time_col: str, window_minutes: int, content_cols: list):
    """Grupos de duplicados exactos (mismo contenido normalizado) en ventana corta."""
    if df.empty or not content_cols: return pd.DataFrame()
    tmp = df.copy()

    # normalizar contenido
    normalized = {}
    for c in content_cols:
        normalized[c] = normalize_factors(tmp[c]) if "factor" in c.lower() else tmp[c].apply(normalize_string)
    norm_df = pd.DataFrame(normalized)

    key = pd.util.hash_pandas_object(norm_df, index=False)
    tmp["_hash_content"] = key
    tmp[time_col] = pd.to_datetime(tmp[time_col], errors="coerce")
    tmp["_row_i"] = tmp.index
    tmp = tmp.sort_values(time_col).reset_index(drop=True)

    win = pd.Timedelta(minutes=window_minutes)
    results = []
    for h, g in tmp.groupby("_hash_content", dropna=False):
        g = g.copy().sort_values(time_col)
        if len(g) < 2: continue
        g["time_diff_prev"] = g[time_col].diff()
        block_id = (g["time_diff_prev"].isna() | (g["time_diff_prev"] > win)).cumsum()
        for _, gb in g.groupby(block_id):
            if len(gb) >= 2:
                row = {
                    "conteo_duplicados": len(gb),
                    "primero": gb[time_col].min(),
                    "ultimo": gb[time_col].max(),
                    "indices": gb["_row_i"].tolist()
                }
                results.append(row)
    return pd.DataFrame(results)

def center_from_points(df, lon_col, lat_col):
    if df.empty or lon_col not in df.columns or lat_col not in df.columns:
        return (10.0, -84.0)
    mlat = df[lat_col].mean(skipna=True); mlon = df[lon_col].mean(skipna=True)
    if np.isnan(mlat) or np.isnan(mlon): return (10.0, -84.0)
    return (float(mlat), float(mlon))

def big_number(label: str, value: str):
    st.markdown(
        f"""
        <div style="padding:12px 14px;border-radius:12px;background:rgba(255,255,255,0.05);">
          <div style="font-size:46px;font-weight:800;line-height:1;margin-bottom:2px;">{value}</div>
          <div style="font-size:13px;opacity:0.75;margin-top:-2px;">{label}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

def build_pdf(conteos: dict, mapa_png_bytes: bytes | None) -> bytes:
    """Genera PDF en memoria con conteos y (opcional) imagen del mapa."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    W, H = A4

    # Encabezado
    c.setFont("Helvetica-Bold", 16)
    c.drawString(2*cm, H-2*cm, "Informe de Avance – Encuestas")
    c.setFont("Helvetica", 10)
    c.drawString(2*cm, H-2.6*cm, f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    # Conteos
    y = H - 4.0*cm
    c.setFont("Helvetica-Bold", 13)
    c.drawString(2*cm, y, "Resumen")
    y -= 0.8*cm
    c.setFont("Helvetica", 11)
    for k, v in conteos.items():
        c.drawString(2*cm, y, f"- {k}: {v}")
        y -= 0.6*cm

    # Imagen del mapa (si hay)
    if mapa_png_bytes:
        try:
            img = ImageReader(io.BytesIO(mapa_png_bytes))
            # Dimensiones aprox dentro de la página
            max_w = W - 4*cm
            max_h = H/2.0
            iw, ih = img.getSize()
            ratio = min(max_w/iw, max_h/ih)
            w, h = iw*ratio, ih*ratio
            c.drawImage(img, (W - w)/2, 3*cm, width=w, height=h, preserveAspectRatio=True, anchor='c')
            c.setFont("Helvetica-Oblique", 9)
            c.drawCentredString(W/2, 3*cm - 0.4*cm, "Mapa al momento de generar el informe")
        except Exception:
            c.setFont("Helvetica-Oblique", 9)
            c.drawString(2*cm, 3*cm, "Nota: no se pudo incrustar la imagen del mapa.")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()

# ======== Sidebar mínimo ========
st.sidebar.header("Cargar Excel")
uploaded = st.sidebar.file_uploader("Sube un archivo .xlsx", type=["xlsx"])

# (Opcional) subir PNG capturado del mapa para incrustarlo en el PDF
st.sidebar.markdown("---")
map_png = st.sidebar.file_uploader("Opcional: sube el PNG del mapa (para el PDF)", type=["png"])

# ======== Carga de datos ========
@st.cache_data(show_spinner=False)
def load_excel_first_sheet(file_like):
    if hasattr(file_like, "read"):
        data = file_like.read()
        bio = io.BytesIO(data)
    else:
        bio = file_like
    xls = pd.ExcelFile(bio, engine="openpyxl")
    first_sheet = xls.sheet_names[0]
    return pd.read_excel(xls, sheet_name=first_sheet), first_sheet

if not uploaded:
    st.info("Sube un Excel (.xlsx) en la barra lateral para comenzar.")
    st.stop()

df, sheet_name = load_excel_first_sheet(uploaded)

# Fechas & columnas clave
for c in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
    if c in df.columns:
        df[c] = pd.to_datetime(df[c], errors="coerce")

lon_col, lat_col = "x", "y"
time_col = "CreationDate" if "CreationDate" in df.columns else ("EditDate" if "EditDate" in df.columns else "¿Cuándo fue el último incidente?")
window_minutes = 10

# Duplicados exactos
content_cols = [c for c in df.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df, time_col=time_col, window_minutes=window_minutes, content_cols=content_cols)

# Conteos
total = len(df)
duplicadas = int(dupes["conteo_duplicados"].sum()) if not dupes.empty else 0
eliminadas_si_limpio = int(sum(max(0, n-1) for n in dupes["conteo_duplicados"])) if not dupes.empty else 0

# Métricas grandes
c1, c2, c3, c4 = st.columns([1.1, 1, 1, 1])
with c1: big_number("Respuestas totales", f"{total}")
with c2: big_number("Duplicadas detectadas", f"{duplicadas}")
with c3: big_number("Se eliminarían (dejando 1/grupo)", f"{eliminadas_si_limpio}")
with c4:
    ult = df[time_col].max() if time_col in df.columns else pd.NaT
    fecha_txt = "-" if pd.isna(ult) else pd.to_datetime(ult).strftime("%d/%m/%Y")
    big_number("Última respuesta", fecha_txt)

# ============= MAPA =============
st.markdown("### 🗺️ Mapa (con descarga PNG)")

valid_points = df.dropna(subset=[lat_col, lon_col]).copy()
for c in [lat_col, lon_col]:
    if c in valid_points.columns:
        valid_points[c] = pd.to_numeric(valid_points[c], errors="coerce")
valid_points = valid_points.dropna(subset=[lat_col, lon_col])

center_lat, center_lon = center_from_points(valid_points, lon_col, lat_col)
m = folium.Map(location=[center_lat, center_lon], zoom_start=13, control_scale=True)

# Capas base
folium.TileLayer(tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
                 attr="© CARTO", name="CartoDB Positron (gris)").add_to(m)
folium.TileLayer("OpenStreetMap", name="OpenStreetMap (callejero)").add_to(m)
folium.TileLayer(tiles="https://{s}.tile.stamen.com/terrain/{z}/{x}/{y}.png",
                 attr="© Stamen/OSM", name="Stamen Terrain (relieve)").add_to(m)
folium.TileLayer(tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                 attr="Tiles © Esri", name="Esri WorldImagery (satelital)").add_to(m)

# Clúster + marcadores
if not valid_points.empty:
    mc = MarkerCluster(name="Clúster de puntos"); mc.add_to(m)
    for _, r in valid_points.iterrows():
        lat, lon = float(r[lat_col]), float(r[lon_col])
        popup_fields = []
        for c in df.columns:
            val = r[c]
            if pd.isna(val): continue
            if isinstance(val, (pd.Timestamp, np.datetime64)):
                try: val = pd.to_datetime(val).strftime("%d/%m/%Y %H:%M")
                except: pass
            popup_fields.append(f"<b>{c}:</b> {val}")
        folium.Marker((lat, lon), popup=folium.Popup("<br>".join(popup_fields), max_width=420)).add_to(mc)

# Heatmap
if len(valid_points) >= 2:
    HeatMap(
        valid_points[[lat_col, lon_col]].values.tolist(),
        radius=20, blur=25,
        gradient={0.2: "#ffffb2", 0.4: "#fecc5c", 0.6: "#fd8d3c", 0.8: "#f03b20", 1.0: "#bd0026"},
        name="Mapa de calor"
    ).add_to(m)

folium.LayerControl(collapsed=False).add_to(m)
MeasureControl(position='topright', primary_length_unit='meters',
               secondary_length_unit='kilometers',
               primary_area_unit='sqmeters',
               secondary_area_unit='hectares').add_to(m)

# Traducción popup medición
script_trad = """
function traducirPopupMedida(){
  document.querySelectorAll('.leaflet-popup-content').forEach(function(el){
    el.innerHTML = el.innerHTML
      .replace(/Linear measurement/gi, 'Medición lineal')
      .replace(/Meters/gi, 'Metros')
      .replace(/Miles/gi, 'Millas')
      .replace(/Center on this line/gi, 'Centrar en esta línea')
      .replace(/Delete/gi, 'Eliminar');
  });
}
document.addEventListener('click', function(){ setTimeout(traducirPopupMedida, 120); });
"""
m.get_root().html.add_child(Element(f"<script>{script_trad}</script>"))

# ===== Botón para descargar PNG dentro del mapa (leaflet-easyPrint) =====
class EasyPrint(MacroElement):
    _template = Template(u"""
        {% macro script(this, kwargs) %}
            L.easyPrint({
              title: 'Descargar PNG',
              position: 'topleft',
              sizeModes: ['Current'],
              exportOnly: true,
              filename: 'mapa_encuestas'
            }).addTo({{this._parent.get_name()}});
        {% endmacro %}
    """)
    def render(self, **kwargs):
        super().render(**kwargs)

# Cargar el plugin desde CDN
cdn = """
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.easyprint/2.1.9/bundle.min.js"
        integrity="sha512-8c4Yk9b2oM1vGm7lC1b2f1H7zj5XvY3oFq9n0gN8gq1Wq+8VRbGx9bO5o5nDNxNUGf0Qp3qz3b3yQj6mvx8XgA=="
        crossorigin="anonymous"></script>
"""
m.get_root().html.add_child(Element(cdn))
m.add_child(EasyPrint())

# Render Folium
st_folium(m, use_container_width=True, returned_objects=[])

st.info("Usa el botón **Descargar PNG** sobre el mapa. Luego súbelo en la barra lateral para incluirlo en el PDF.")

# ============= PDF =============
st.markdown("### 📄 Generar PDF de avance")
conteos = {
    "Respuestas totales": total,
    "Duplicadas detectadas": duplicadas,
    "Se eliminarían (dejando 1 por grupo)": eliminadas_si_limpio,
    "Última respuesta": "-" if pd.isna(df[time_col].max()) else pd.to_datetime(df[time_col].max()).strftime("%d/%m/%Y")
}

png_bytes = map_png.read() if map_png is not None else None
pdf_bytes = build_pdf(conteos, png_bytes)
st.download_button(
    "⬇️ Descargar PDF de avance",
    data=pdf_bytes,
    file_name=f"informe_avance_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    mime="application/pdf"
)

# Tabla rápida
st.markdown("### 📄 Datos (primeras filas)")
st.dataframe(df.head(1000), use_container_width=True)

