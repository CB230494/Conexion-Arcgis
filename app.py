# -*- coding: utf-8 -*-
from datetime import datetime
import io
import numpy as np
import pandas as pd
import streamlit as st

import folium
from folium.plugins import MarkerCluster, HeatMap, MeasureControl
from streamlit_folium import st_folium

# ===================== CONFIG =====================
st.set_page_config(page_title="Encuestas – Seguridad", layout="wide")
st.title("📊 Dashboard de Encuestas – Seguridad")
st.caption("Carga un Excel y visualiza conteos, duplicados exactos y mapa (clúster + calor + distancias).")

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

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = phi2 - phi1
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi/2.0)**2 + np.cos(phi1)*np.cos(phi2)*np.sin(dlambda/2.0)**2
    c = 2*np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    return R * c

def detect_duplicates(df, time_col: str, window_minutes: int, content_cols: list):
    if df.empty or not content_cols: return pd.DataFrame()
    tmp = df.copy()

    normalized = {}
    for c in content_cols:
        if "factor" in c.lower():
            normalized[c] = tmp[c].apply(normalize_factors)
        else:
            normalized[c] = tmp[c].apply(normalize_string)
    norm_df = pd.DataFrame(normalized)

    key = pd.util.hash_pandas_object(norm_df, index=False)
    tmp["_hash_content"] = key
    tmp[time_col] = pd.to_datetime(tmp[time_col], errors="coerce")
    tmp = tmp.sort_values(time_col).reset_index(drop=True)

    win = pd.Timedelta(minutes=window_minutes)
    results = []
    for h, g in tmp.groupby("_hash_content", dropna=False):
        g = g.copy().sort_values(time_col)
        if g.shape[0] < 2: continue
        g["time_diff_prev"] = g[time_col].diff()
        block_id = (g["time_diff_prev"].isna() | (g["time_diff_prev"] > win)).cumsum()
        for _, gb in g.groupby(block_id):
            if gb.shape[0] >= 2:
                row = {
                    "conteo_duplicados": gb.shape[0],
                    "primero": gb[time_col].min(),
                    "ultimo": gb[time_col].max(),
                    "ventana_min": window_minutes,
                    "hash": h,
                    "indices": gb.index.tolist()
                }
                for c in content_cols:
                    row[c] = norm_df.loc[gb.index[0], c]
                results.append(row)
    if not results: return pd.DataFrame()
    return pd.DataFrame(results).sort_values(["conteo_duplicados","ultimo"], ascending=[False, False])

def center_from_points(df, lon_col, lat_col):
    if df.empty or lon_col not in df.columns or lat_col not in df.columns:
        return (10.0, -84.0)
    mlat = df[lat_col].mean(skipna=True); mlon = df[lon_col].mean(skipna=True)
    if np.isnan(mlat) or np.isnan(mlon): return (10.0, -84.0)
    return (float(mlat), float(mlon))

# ======== Sidebar mínimo (solo carga) ========
st.sidebar.header("Cargar Excel")
uploaded = st.sidebar.file_uploader("Sube un archivo .xlsx", type=["xlsx"])

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

# coerción de fechas si están
for c in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
    if c in df.columns:
        df[c] = pd.to_datetime(df[c], errors="coerce")

# ======== Parámetros fijos (sin mostrar en UI) ========
lon_col, lat_col = "x", "y"
time_col = "CreationDate" if "CreationDate" in df.columns else (
    "EditDate" if "EditDate" in df.columns else "¿Cuándo fue el último incidente?"
)
window_minutes = 10
distance_threshold_m = 200

# ======== Métricas ========
c1, c2, c3, c4 = st.columns(4)
with c1: st.metric("Total de respuestas", int(df.shape[0]))
with c2: st.metric("Hoja detectada", sheet_name)
if time_col in df.columns:
    ult = df[time_col].max()
    with c3: st.metric("Última respuesta", "-" if pd.isna(ult) else ult.strftime("%Y-%m-%d %H:%M"))
if "¿Cómo califica la seguridad en su zona?" in df.columns:
    seg_counts = df["¿Cómo califica la seguridad en su zona?"].astype(str).str.lower().value_counts()
    top_seg = seg_counts.index[0] if not seg_counts.empty else "-"
    with c4: st.metric("Calificación más frecuente", top_seg)

# ======== Duplicados exactos (ventana corta) ========
st.markdown("### 🧪 Duplicados exactos en ≤ 10 min")
content_cols = [c for c in df.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df, time_col=time_col, window_minutes=window_minutes, content_cols=content_cols)
if dupes.empty:
    st.success("No se detectaron grupos de respuestas EXACTAMENTE iguales en la ventana indicada.")
else:
    st.warning(f"Se detectaron {dupes.shape[0]} grupo(s) de posibles duplicados exactos.")
    with st.expander("Ver detalle de duplicados"):
        show_cols = ["conteo_duplicados", "primero", "ultimo", "ventana_min"] + content_cols + ["indices"]
        st.dataframe(dupes[show_cols], use_container_width=True)

# ======== Mapa ========
st.markdown("### 🗺️ Mapa de respuestas")
valid_points = df.dropna(subset=[lat_col, lon_col]).copy()
for c in [lat_col, lon_col]:
    if c in valid_points.columns:
        valid_points[c] = pd.to_numeric(valid_points[c], errors="coerce")
valid_points = valid_points.dropna(subset=[lat_col, lon_col])
center_lat, center_lon = center_from_points(valid_points, lon_col, lat_col)

m = folium.Map(location=[center_lat, center_lon], zoom_start=13, control_scale=True)

# capas base
folium.TileLayer(tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
                 attr="© CARTO", name="CartoDB Positron (gris)").add_to(m)
folium.TileLayer("OpenStreetMap", name="OpenStreetMap (callejero)").add_to(m)
folium.TileLayer(tiles="https://{s}.tile.stamen.com/terrain/{z}/{x}/{y}.png",
                 attr="© Stamen/OSM", name="Stamen Terrain (relieve)").add_to(m)
folium.TileLayer(tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                 attr="Tiles © Esri", name="Esri WorldImagery (satelital)").add_to(m)

# marcadores + clúster
if not valid_points.empty:
    mc = MarkerCluster(name="Clúster de puntos")
    mc.add_to(m)
    for _, r in valid_points.iterrows():
        lat, lon = float(r[lat_col]), float(r[lon_col])
        popup_fields = []
        for c in df.columns:
            val = r[c]
            if pd.isna(val): continue
            if isinstance(val, (pd.Timestamp, np.datetime64)):
                try: val = pd.to_datetime(val).strftime("%Y-%m-%d %H:%M")
                except: pass
            popup_fields.append(f"<b>{c}:</b> {val}")
        folium.Marker([lat, lon], popup=folium.Popup("<br>".join(popup_fields), max_width=400)).add_to(mc)

# heatmap rojo
if not valid_points.empty and len(valid_points) >= 2:
    heat_data = valid_points[[lat_col, lon_col]].values.tolist()
    HeatMap(
        heat_data,
        radius=20, blur=25,
        gradient={0.2: "#ffffb2", 0.4: "#fecc5c", 0.6: "#fd8d3c", 0.8: "#f03b20", 1.0: "#bd0026"},
        name="Mapa de calor"
    ).add_to(m)

# pares cercanos (<= 200 m) y líneas
pairs = []
if not valid_points.empty and len(valid_points) >= 2:
    pts = valid_points[[lat_col, lon_col]].to_numpy(dtype=float)
    idx = valid_points.index.to_list()
    for a in range(len(pts)):
        for b in range(a + 1, len(pts)):
            lat1, lon1 = pts[a]; lat2, lon2 = pts[b]
            d = haversine_m(lat1, lon1, lat2, lon2)
            if d <= 200:
                pairs.append((idx[a], idx[b], float(d), lat1, lon1, lat2, lon2))
if pairs:
    for _, _, d, la, lo, lb, lob in sorted(pairs, key=lambda x: x[2]):
        folium.PolyLine([(la, lo), (lb, lob)], color="#d62728", weight=3, opacity=0.8).add_to(m)

# control de capas
folium.LayerControl(collapsed=False).add_to(m)

# control de medición (unidades en metros) + traducción al español del popup
MeasureControl(position='topright',
               primary_length_unit='meters',
               secondary_length_unit='kilometers',
               primary_area_unit='sqmeters',
               secondary_area_unit='hectares').add_to(m)

# inyectar JS para traducir textos del popup del control de medida
from folium import Element
script = """
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
m.get_root().html.add_child(Element(f"<script>{script}</script>"))

# render
st_folium(m, use_container_width=True, returned_objects=[])

# tabla final
st.markdown("### 📄 Datos (primeras filas)")
st.dataframe(df.head(1000), use_container_width=True)
