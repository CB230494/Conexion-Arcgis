# -*- coding: utf-8 -*-
import io, os, time, tempfile
from datetime import datetime
import numpy as np
import pandas as pd
import streamlit as st

# Mapa
import folium
from folium.plugins import MarkerCluster, HeatMap, MeasureControl
from streamlit_folium import st_folium
from folium import Element

# PDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader

# ====== (Opcional) HTML -> PNG con Selenium headless ======
SELENIUM_AVAILABLE = True
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
except Exception:
    SELENIUM_AVAILABLE = False

# ===================== CONFIG =====================
st.set_page_config(page_title="Dashboard de Avances – Encuestas", layout="wide")
st.title("📈 Dashboard de Avances – Encuestas")
st.caption("Conteos grandes, mapa (duplicadas en rojo), PDF con detalle y evidencias.")

# ======== Utilidades ========
META_COLS = {"ObjectID", "GlobalID", "instance_id", "CreationDate", "EditDate", "Creator", "Editor"}

def normalize_string(x) -> str:
    if pd.isna(x): return ""
    s = str(x).strip().lower()
    return " ".join(s.split())

def normalize_factors(x) -> str:
    if pd.isna(x): return ""
    parts = str(x).replace(";", ",").split(",")
    parts = [normalize_string(p) for p in parts if normalize_string(p)]
    parts.sort()
    return ",".join(parts)

def detect_duplicates(df, time_col: str, window_minutes: int, content_cols: list):
    """Identifica grupos con MISMO contenido exacto normalizado en una ventana <= window_minutes."""
    if df.empty or not content_cols: return pd.DataFrame()
    tmp = df.copy()
    normalized = {}
    for c in content_cols:
        normalized[c] = tmp[c].apply(normalize_factors) if "factor" in c.lower() else tmp[c].apply(normalize_string)
    norm_df = pd.DataFrame(normalized)
    key = pd.util.hash_pandas_object(norm_df, index=False)
    tmp["_hash_content"] = key
    tmp[time_col] = pd.to_datetime(tmp[time_col], errors="coerce")
    tmp["_row_i"] = tmp.index
    tmp = tmp.sort_values(time_col).reset_index(drop=True)

    win = pd.Timedelta(minutes=window_minutes)
    out = []
    for h, g in tmp.groupby("_hash_content", dropna=False):
        g = g.copy().sort_values(time_col)
        if len(g) < 2: 
            continue
        g["time_diff_prev"] = g[time_col].diff()
        block_id = (g["time_diff_prev"].isna() | (g["time_diff_prev"] > win)).cumsum()
        for _, gb in g.groupby(block_id):
            if len(gb) >= 2:
                out.append({
                    "conteo_duplicados": len(gb),
                    "primero": gb[time_col].min(),
                    "ultimo": gb[time_col].max(),
                    "indices": gb["_row_i"].tolist()
                })
    dupes = pd.DataFrame(out)
    if dupes.empty:
        return dupes
    return dupes.sort_values(["conteo_duplicados","ultimo"], ascending=[False, False]).reset_index(drop=True)

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

def html_to_png(html_bytes: bytes, width: int = 1280, height: int = 800, wait_sec: float = 2.8) -> bytes | None:
    """Convierte HTML a PNG con Selenium headless. Devuelve bytes PNG o None si falla."""
    if not SELENIUM_AVAILABLE:
        return None
    tmp_html = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
    try:
        tmp_html.write(html_bytes); tmp_html.flush(); tmp_html.close()
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument(f"--window-size={width},{height}")
        driver = webdriver.Chrome(ChromeDriverManager().install(), options=options)
        try:
            driver.get("file://" + tmp_html.name)
            time.sleep(wait_sec)  # dar tiempo a Leaflet para renderizar
            png = driver.get_screenshot_as_png()
            return png
        finally:
            driver.quit()
    except Exception:
        return None
    finally:
        try: os.unlink(tmp_html.name)
        except Exception: pass

def to_reason_string(df, idxs, time_col, lon_col, lat_col, window_minutes):
    """Construye texto explicativo del porqué del duplicado para un grupo."""
    sub = df.loc[idxs].copy()
    # misma ubicación exacta (coordenadas idénticas) – tolerancia por redondeo
    same_place = False
    if lon_col in sub.columns and lat_col in sub.columns:
        coords = sub[[lon_col, lat_col]].round(6).dropna()
        same_place = (len(coords.drop_duplicates()) == 1) and (len(coords) == len(sub))
    rango = ""
    try:
        primero = pd.to_datetime(sub[time_col], errors="coerce").min()
        ultimo = pd.to_datetime(sub[time_col], errors="coerce").max()
        if pd.notna(primero) and pd.notna(ultimo):
            rango = f"{primero.strftime('%d/%m/%Y %H:%M')} → {ultimo.strftime('%d/%m/%Y %H:%M')}"
    except Exception:
        pass
    motivo = (f"Se detectó el mismo contenido en un lapso ≤ {window_minutes} minutos"
              + (", en la **misma ubicación**" if same_place else "")
              + ". Se conservará **1** respuesta y se eliminarán las demás del grupo.")
    return motivo, rango, same_place

def build_pdf(conteos: dict, detalle_dupes: list, mapa_dup_png: bytes | None, mapa_final_png: bytes | None) -> bytes:
    """Genera PDF con resumen, detalle de duplicados y hasta 2 imágenes."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    W, H = A4

    # Encabezado
    c.setFont("Helvetica-Bold", 16)
    c.drawString(2*cm, H-2*cm, "Informe de Avance – Encuestas")
    c.setFont("Helvetica", 10)
    c.drawString(2*cm, H-2.6*cm, f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    # Resumen
    y = H - 4.2*cm
    c.setFont("Helvetica-Bold", 13)
    c.drawString(2*cm, y, "Resumen")
    y -= 0.8*cm
    c.setFont("Helvetica", 11)
    for k, v in conteos.items():
        c.drawString(2*cm, y, f"- {k}: {v}"); y -= 0.6*cm

    # Detalle duplicados
    if detalle_dupes:
        y -= 0.2*cm
        c.setFont("Helvetica-Bold", 12)
        c.drawString(2*cm, y, "Detalle de respuestas duplicadas")
        y -= 0.6*cm
        c.setFont("Helvetica", 10)
        for i, d in enumerate(detalle_dupes, start=1):
            lineas = [
                f"Grupo {i}: {d['conteo']} respuestas (se eliminarán {d['eliminar']}, quedará 1).",
                f"Índices/IDs: {', '.join(map(str, d['indices']))}",
                f"Rango temporal: {d['rango'] or '-'}",
                f"Motivo: {d['motivo']}",
            ]
            for ln in lineas:
                if y < 3*cm:
                    c.showPage(); y = H - 2.5*cm
                    c.setFont("Helvetica-Bold", 12); c.drawString(2*cm, y, "Detalle de respuestas duplicadas (cont.)")
                    y -= 0.6*cm; c.setFont("Helvetica", 10)
                c.drawString(2*cm, y, f"- {ln}")
                y -= 0.5*cm

    # Imágenes
    def draw_img(png_bytes, caption):
        nonlocal y
        if not png_bytes: return
        img = ImageReader(io.BytesIO(png_bytes))
        max_w = W - 4*cm; max_h = H/2.2
        iw, ih = img.getSize(); ratio = min(max_w/iw, max_h/ih)
        w, h = iw*ratio, ih*ratio
        if y < h + 3*cm:
            c.showPage(); y = H - 2.5*cm
        c.drawImage(img, (W - w)/2, y - h, width=w, height=h)
        y = y - h - 0.4*cm
        c.setFont("Helvetica-Oblique", 9)
        c.drawCentredString(W/2, y, caption)
        y -= 0.6*cm

    y -= 0.2*cm
    c.setFont("Helvetica-Bold", 12)
    if mapa_dup_png or mapa_final_png:
        c.drawString(2*cm, y, "Evidencias visuales"); y -= 0.6*cm
        c.setFont("Helvetica", 10)
        if mapa_dup_png:
            draw_img(mapa_dup_png, "Mapa con duplicadas (marcadas en rojo)")
        if mapa_final_png:
            draw_img(mapa_final_png, "Mapa final (respuestas validadas)")
    c.showPage(); c.save(); buffer.seek(0)
    return buffer.getvalue()

def map_to_html_download(m: folium.Map, filename: str = "mapa_encuestas.html", key: str = "dl_html"):
    html = m.get_root().render().encode("utf-8")
    st.download_button("⬇️ Descargar mapa (HTML)", data=html, file_name=filename,
                       mime="text/html", key=key)

# ======== Sidebar ========
st.sidebar.header("Cargar Excel")
uploaded = st.sidebar.file_uploader("Sube un archivo .xlsx", type=["xlsx"])

st.sidebar.markdown("---")
st.sidebar.subheader("Evidencias para PDF (opcional)")
map_dup_any = st.sidebar.file_uploader("Mapa con duplicadas (PNG o HTML)", type=["png","html"])
map_final_any = st.sidebar.file_uploader("Mapa final validado (PNG o HTML)", type=["png","html"])

# ======== Carga de datos ========
@st.cache_data(show_spinner=False)
def load_excel_first_sheet(file_like):
    if hasattr(file_like, "read"):
        data = file_like.read(); bio = io.BytesIO(data)
    else:
        bio = file_like
    xls = pd.ExcelFile(bio, engine="openpyxl")
    first_sheet = xls.sheet_names[0]
    return pd.read_excel(xls, sheet_name=first_sheet), first_sheet

if not uploaded:
    st.info("Sube un Excel (.xlsx) en la barra lateral para comenzar."); st.stop()

df, sheet_name = load_excel_first_sheet(uploaded)

# Fechas & columnas clave
for c in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
    if c in df.columns: df[c] = pd.to_datetime(df[c], errors="coerce")

lon_col, lat_col = "x", "y"
time_col = "CreationDate" if "CreationDate" in df.columns else ("EditDate" if "EditDate" in df.columns else "¿Cuándo fue el último incidente?")
window_minutes = 10

# Duplicados
content_cols = [c for c in df.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df, time_col=time_col, window_minutes=window_minutes, content_cols=content_cols)

# Conjunto de índices duplicados (para marcar en mapa)
dup_set = set()
if not dupes.empty:
    for lst in dupes["indices"]:
        dup_set.update(lst)

# Conteos
total = len(df)
duplicadas = int(dupes["conteo_duplicados"].sum()) if not dupes.empty else 0
eliminar_por_grupo = int(sum(max(0, n-1) for n in (dupes["conteo_duplicados"] if not dupes.empty else [])))
validadas = total - eliminar_por_grupo

# Métricas
c1, c2, c3, c4 = st.columns([1.1, 1, 1, 1])
with c1: big_number("Respuestas totales", f"{total}")
with c2: big_number("Duplicadas detectadas", f"{duplicadas}")
with c3: big_number("Se eliminarán (1 por grupo)", f"{eliminar_por_grupo}")
with c4: big_number("Quedarán validadas", f"{validadas}")

st.info(
    f"Un **duplicado** es un grupo de respuestas con **exactamente la misma información** "
    f"registradas en un lapso **≤ {window_minutes} minutos**. En la limpieza, de cada grupo se "
    f"**mantiene 1** y se **eliminan** las demás; por eso, de {total} pasarían a **{validadas}** respuestas validadas."
)

# ============= MAPA (duplicadas en ROJO) =============
st.markdown("### 🗺️ Mapa (duplicadas en rojo)")

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

# Clúster + marcadores (rojo si es duplicado)
if not valid_points.empty:
    mc = MarkerCluster(name="Clúster de puntos"); mc.add_to(m)
    for idx, r in valid_points.iterrows():
        lat, lon = float(r[lat_col]), float(r[lon_col])
        popup_fields = []
        for c in df.columns:
            val = r[c]
            if pd.isna(val): continue
            if isinstance(val, (pd.Timestamp, np.datetime64)):
                try: val = pd.to_datetime(val).strftime("%d/%m/%Y %H:%M")
                except: pass
            popup_fields.append(f"<b>{c}:</b> {val}")
        is_dup = idx in dup_set
        icon = folium.Icon(color="red" if is_dup else "blue", icon="info-sign")
        folium.Marker((lat, lon), popup=folium.Popup("<br>".join(popup_fields), max_width=420), icon=icon).add_to(mc)

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

# Traducir popup medición
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

# Render del mapa
st_folium(m, use_container_width=True, returned_objects=[])

# Descarga del mapa como HTML (siempre funciona)
st.markdown("**Exportar mapa para evidencias**")
map_html_bytes = m.get_root().render().encode("utf-8")
st.download_button("⬇️ Descargar mapa (HTML)", data=map_html_bytes,
                   file_name="mapa_encuestas.html", mime="text/html", key="dl_html_mapa")

# ============= Preparar detalle para PDF =============
detalle_dupes = []
if not dupes.empty:
    for _, row in dupes.iterrows():
        idxs = row["indices"]
        motivo, rango, same_place = to_reason_string(df, idxs, time_col, lon_col, lat_col, window_minutes)
        detalle_dupes.append({
            "conteo": int(row["conteo_duplicados"]),
            "eliminar": max(0, int(row["conteo_duplicados"]) - 1),
            "indices": idxs,
            "rango": rango,
            "motivo": motivo
        })

# ============= Cargar evidencias para PDF (PNG o HTML -> PNG) =============
def any_to_png_bytes(uploaded_file):
    if uploaded_file is None: 
        return None
    name = uploaded_file.name.lower()
    if name.endswith(".png"):
        return uploaded_file.read()
    if name.endswith(".html"):
        html_b = uploaded_file.read()
        st.info(f"Convirtiendo **{uploaded_file.name}** a PNG…")
        png_b = html_to_png(html_b)
        if png_b:
            st.success("Conversión exitosa.")
            return png_b
        st.error("No fue posible convertir HTML a PNG en este entorno. Sube un PNG (captura).")
        return None
    st.error("Formato no soportado. Sube PNG o HTML.")
    return None

map_dup_png = any_to_png_bytes(map_dup_any)
map_final_png = any_to_png_bytes(map_final_any)

# ============= PDF =============
st.markdown("### 📄 Generar PDF de avance")
conteos = {
    "Respuestas totales": total,
    "Duplicadas detectadas": duplicadas,
    "Se eliminarán (1 por grupo)": eliminar_por_grupo,
    "Quedarán validadas": validadas,
    "Última respuesta": "-" if pd.isna(df[time_col].max()) else pd.to_datetime(df[time_col].max()).strftime("%d/%m/%Y")
}
pdf_bytes = build_pdf(conteos, detalle_dupes, map_dup_png, map_final_png)
st.download_button(
    "⬇️ Descargar PDF de avance",
    data=pdf_bytes,
    file_name=f"informe_avance_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    mime="application/pdf"
)

# Tabla rápida
st.markdown("### 📄 Datos (primeras filas)")
st.dataframe(df.head(1000), use_container_width=True)


