# -*- coding: utf-8 -*-
import io
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st

import folium
from folium.plugins import MarkerCluster, HeatMap, MeasureControl
from streamlit_folium import st_folium

# ===================== CONFIG =====================
st.set_page_config(page_title="Dashboard de Encuestas – Seguridad", layout="wide")

st.title("📊 Dashboard de Encuestas – Seguridad")
st.caption("Conteos, detección de duplicados en ventana corta, y mapa con clúster/heatmap/distancias.")

# ===================== UTILIDADES =====================

META_COLS = {"ObjectID", "GlobalID", "instance_id", "CreationDate", "EditDate", "Creator", "Editor"}

def parse_datetime_safe(x):
    """Convierte a datetime si es posible; devuelve NaT si no."""
    try:
        return pd.to_datetime(x)
    except Exception:
        return pd.NaT

def normalize_string(s: str) -> str:
    if pd.isna(s):
        return ""
    s = str(s).strip().lower()
    # normalizar separadores múltiples y espacios
    s = " ".join(s.split())
    return s

def normalize_factors(s: str) -> str:
    """Normaliza '¿Cuáles factores influyen?' separando por coma y ordenando para evitar desajustes por orden."""
    if pd.isna(s):
        return ""
    parts = [normalize_string(p) for p in str(s).replace(";", ",").split(",")]
    parts = [p for p in parts if p]
    parts.sort()
    return ",".join(parts)

def haversine_m(lat1, lon1, lat2, lon2):
    """Distancia Haversine en metros."""
    R = 6371000.0
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = phi2 - phi1
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi/2.0)**2 + np.cos(phi1)*np.cos(phi2)*np.sin(dlambda/2.0)**2
    c = 2*np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    return R * c

def detect_duplicates(df, time_col: str, window_minutes: int, content_cols: list):
    """
    Detecta grupos de filas con contenido exactamente igual (en content_cols),
    cuya diferencia de tiempo entre valores consecutivos sea <= ventana especificada.
    Devuelve un DataFrame con grupo, tamaño del grupo y rango temporal.
    """
    if df.empty or not content_cols:
        return pd.DataFrame()

    tmp = df.copy()

    # normalizar columnas de contenido
    normalized = {}
    for c in content_cols:
        if "factor" in c.lower():
            normalized[c] = tmp[c].apply(normalize_factors)
        else:
            normalized[c] = tmp[c].apply(normalize_string)
    norm_df = pd.DataFrame(normalized)

    key = pd.util.hash_pandas_object(norm_df, index=False)  # hash estable por fila contenido
    tmp["_hash_content"] = key

    # asegurar datetime en time_col
    tmp[time_col] = pd.to_datetime(tmp[time_col], errors="coerce")

    # ordenar por tiempo para comparar ventanas
    tmp = tmp.sort_values(time_col).reset_index(drop=True)

    # por cada hash, detectar si hay eventos cercanos en el tiempo
    results = []
    win = pd.Timedelta(minutes=window_minutes)

    for h, g in tmp.groupby("_hash_content", dropna=False):
        g = g.copy().sort_values(time_col)
        if g.shape[0] < 2:
            continue

        # Ventanas: marcamos pares cercanos y luego agrupamos consecutivos
        g["time_diff_prev"] = g[time_col].diff()
        # comenzamos un "bloque" cuando la diferencia es > ventana o NaT
        block_id = (g["time_diff_prev"].isna() | (g["time_diff_prev"] > win)).cumsum()
        for b, gb in g.groupby(block_id):
            if gb.shape[0] >= 2:
                row = {
                    "conteo_duplicados": gb.shape[0],
                    "primero": gb[time_col].min(),
                    "ultimo": gb[time_col].max(),
                    "ventana_min": window_minutes,
                    "hash": h,
                    "indices": gb.index.tolist()
                }
                # También agregamos una vista de las columnas de contenido para inspección
                for c in content_cols:
                    row[c] = norm_df.loc[gb.index[0], c]
                results.append(row)

    if not results:
        return pd.DataFrame()

    out = pd.DataFrame(results).sort_values(["conteo_duplicados", "ultimo"], ascending=[False, False])
    return out

def ensure_lon_lat(df, lon_col="x", lat_col="y"):
    """Valida que existan columnas de coordenadas y corrige tipos."""
    if lon_col not in df.columns or lat_col not in df.columns:
        st.warning("No se encontraron columnas de coordenadas 'x' (longitud) y 'y' (latitud). Ajusta en la barra lateral.")
    # convertir a numéricos
    for c in [lon_col, lat_col]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def center_from_points(df, lon_col, lat_col):
    if df.empty or lon_col not in df.columns or lat_col not in df.columns:
        return (10.0, -84.0)  # CR por defecto aproximado
    mlat = df[lat_col].mean(skipna=True)
    mlon = df[lon_col].mean(skipna=True)
    if np.isnan(mlat) or np.isnan(mlon):
        return (10.0, -84.0)
    return (float(mlat), float(mlon))

# ===================== SIDEBAR =====================

st.sidebar.header("⚙️ Configuración")

uploaded = st.sidebar.file_uploader("Sube un Excel (.xlsx)", type=["xlsx"])

default_path = st.sidebar.text_input(
    "O usa una ruta/archivo por defecto (opcional):",
    value="/mnt/data/S123_709e5cc4d53d419083caa06f401bc335_EXCEL (1).xlsx"
)

sheet_name = st.sidebar.text_input("Nombre de la hoja", value="_1_de_formulario_0")

lon_col = st.sidebar.text_input("Columna de Longitud (x)", value="x")
lat_col = st.sidebar.text_input("Columna de Latitud (y)", value="y")

date_col_candidates = ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]
time_col = st.sidebar.selectbox("Columna de tiempo para ventanas cortas", options=date_col_candidates, index=0)

window_minutes = st.sidebar.number_input("Ventana de tiempo (minutos) para detectar duplicados exactos", min_value=1, max_value=240, value=10)

date_filter_enable = st.sidebar.checkbox("Filtrar por rango de fechas (según CreationDate)", value=True)
today = pd.Timestamp.today().normalize()
date_min = st.sidebar.date_input("Fecha inicial", value=today - pd.Timedelta(days=30))
date_max = st.sidebar.date_input("Fecha final", value=pd.Timestamp.today())

st.sidebar.markdown("---")
st.sidebar.subheader("🗺️ Opciones de mapa")
show_markers = st.sidebar.checkbox("Mostrar marcadores", value=True)
use_cluster = st.sidebar.checkbox("Agrupar en clúster", value=True)
show_heatmap = st.sidebar.checkbox("Mostrar Heatmap", value=True)
heat_radius = st.sidebar.slider("Radio Heatmap (px)", min_value=5, max_value=50, value=20)
heat_blur = st.sidebar.slider("Desenfoque Heatmap", min_value=5, max_value=50, value=25)

base_map = st.sidebar.selectbox(
    "Estilo de mapa base",
    options=[
        "CartoDB Positron (gris)",
        "OpenStreetMap (callejero)",
        "Stamen Toner (alto contraste)",
        "Stamen Terrain (relieve)",
        "Esri WorldImagery (satelital)",
    ],
    index=0
)

st.sidebar.markdown("---")
st.sidebar.subheader("📏 Distancias entre puntos")
distance_enable = st.sidebar.checkbox("Calcular pares dentro de una distancia", value=False)
distance_threshold_m = st.sidebar.number_input("Umbral de distancia (metros)", min_value=10, max_value=5000, value=200)

# ===================== CARGA DE DATOS =====================

@st.cache_data(show_spinner=False)
def load_excel(file_like, sheet):
    return pd.read_excel(file_like, sheet_name=sheet)

def get_dataframe():
    if uploaded is not None:
        return load_excel(uploaded, sheet_name)
    else:
        # intentar con ruta por defecto
        try:
            return load_excel(default_path, sheet_name)
        except Exception as e:
            st.error(f"No se pudo abrir el Excel. Sube un archivo o verifica la ruta. Detalle: {e}")
            return pd.DataFrame()

df = get_dataframe()

if df.empty:
    st.stop()

# Coercionar fechas
for c in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
    if c in df.columns:
        df[c] = pd.to_datetime(df[c], errors="coerce")

# Filtro por fechas (CreationDate)
if date_filter_enable and "CreationDate" in df.columns:
    mask = (df["CreationDate"].dt.date >= pd.to_datetime(date_min).date()) & \
           (df["CreationDate"].dt.date <= pd.to_datetime(date_max).date())
    df_filtered = df.loc[mask].copy()
else:
    df_filtered = df.copy()

# Asegurar coords
df_filtered = ensure_lon_lat(df_filtered, lon_col, lat_col)

# ===================== MÉTRICAS =====================

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total de respuestas (todas)", int(df.shape[0]))
with col2:
    st.metric("Respuestas en rango", int(df_filtered.shape[0]))

if "CreationDate" in df_filtered.columns:
    ult = df_filtered["CreationDate"].max()
    with col3:
        st.metric("Última respuesta (CreationDate)", "-" if pd.isna(ult) else ult.strftime("%Y-%m-%d %H:%M"))
if "¿Cómo califica la seguridad en su zona?" in df_filtered.columns:
    seg_counts = df_filtered["¿Cómo califica la seguridad en su zona?"].astype(str).str.lower().value_counts()
    top_seg = seg_counts.index[0] if not seg_counts.empty else "-"
    with col4:
        st.metric("Calificación más frecuente", top_seg)

st.markdown("### 🧪 Duplicados exactos en ventana corta")
# columnas de contenido = todas menos meta y coords/tiempos evidentes
content_cols = [c for c in df_filtered.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df_filtered, time_col=time_col, window_minutes=window_minutes, content_cols=content_cols)

if dupes.empty:
    st.success("No se detectaron grupos de respuestas EXACTAMENTE iguales dentro de la ventana de tiempo indicada.")
else:
    st.warning(f"Se detectaron {dupes.shape[0]} grupo(s) de posibles duplicados exactos en ≤ {window_minutes} min.")
    # Expand para ver detalle
    with st.expander("Ver detalle de duplicados"):
        # Mostrar columnas clave y los índices involucrados
        show_cols = ["conteo_duplicados", "primero", "ultimo", "ventana_min"] + content_cols + ["indices"]
        st.dataframe(dupes[show_cols], use_container_width=True)

# ===================== MAPA =====================

st.markdown("### 🗺️ Mapa de respuestas")

# Centro del mapa
center_lat, center_lon = center_from_points(df_filtered, lon_col, lat_col)

m = folium.Map(location=[center_lat, center_lon], zoom_start=13, control_scale=True)

# Capa base seleccionada
base_tiles = {
    "CartoDB Positron (gris)": ("CartoDB positron", "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
                                "https://carto.com/attributions"),
    "OpenStreetMap (callejero)": ("OpenStreetMap", None, None),
    "Stamen Toner (alto contraste)": ("Stamen Toner", "https://{s}.tile.stamen.com/toner/{z}/{x}/{y}.png",
                                      "Map tiles by Stamen Design, CC BY 3.0 — Map data © OpenStreetMap contributors"),
    "Stamen Terrain (relieve)": ("Stamen Terrain", "https://{s}.tile.stamen.com/terrain/{z}/{x}/{y}.png",
                                 "Map tiles by Stamen Design, CC BY 3.0 — Map data © OpenStreetMap contributors"),
    "Esri WorldImagery (satelital)": ("Esri WorldImagery", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                                      "Tiles © Esri")
}

# Agregar varias opciones de base para alternar
for name, (label, url, attr) in base_tiles.items():
    if url is None:
        folium.TileLayer(tiles=label, name=name, control=True).add_to(m)
    else:
        folium.TileLayer(tiles=url, name=name, attr=attr, control=True).add_to(m)

# Marcadores
valid_points = df_filtered.dropna(subset=[lat_col, lon_col]).copy()

if show_markers and not valid_points.empty:
    if use_cluster:
        mc = MarkerCluster(name="Clúster de puntos")
        mc.add_to(m)
    for i, r in valid_points.iterrows():
        lat, lon = float(r[lat_col]), float(r[lon_col])
        popup_fields = []
        for c in df_filtered.columns:
            val = r[c]
            if pd.isna(val):
                continue
            # mostrar fechas legibles
            if isinstance(val, (pd.Timestamp, np.datetime64)):
                try:
                    val = pd.to_datetime(val).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
            popup_fields.append(f"<b>{c}:</b> {val}")
        popup_html = "<br>".join(popup_fields)
        marker = folium.Marker(location=[lat, lon], popup=folium.Popup(popup_html, max_width=400))
        if use_cluster:
            marker.add_to(mc)
        else:
            marker.add_to(m)

# Heatmap (con gradiente más rojizo)
if show_heatmap and not valid_points.empty:
    heat_data = valid_points[[lat_col, lon_col]].dropna().values.tolist()
    if len(heat_data) >= 2:  # HeatMap requiere al menos 2 puntos para ser útil
        HeatMap(
            heat_data,
            radius=heat_radius,
            blur=heat_blur,
            gradient={0.2: "#ffffb2", 0.4: "#fecc5c", 0.6: "#fd8d3c", 0.8: "#f03b20", 1.0: "#bd0026"},
            name="Mapa de calor"
        ).add_to(m)

# Distancias: pares dentro de umbral y líneas
pairs_df = pd.DataFrame()
if distance_enable and not valid_points.empty and len(valid_points) >= 2:
    pts = valid_points[[lat_col, lon_col]].to_numpy(dtype=float)
    idx = valid_points.index.to_list()

    rows = []
    for a in range(len(pts)):
        for b in range(a + 1, len(pts)):
            lat1, lon1 = pts[a]
            lat2, lon2 = pts[b]
            d = haversine_m(lat1, lon1, lat2, lon2)
            if d <= distance_threshold_m:
                rows.append({
                    "idx_1": idx[a],
                    "idx_2": idx[b],
                    "lat1": lat1, "lon1": lon1,
                    "lat2": lat2, "lon2": lon2,
                    "dist_m": round(float(d), 2)
                })
    if rows:
        pairs_df = pd.DataFrame(rows).sort_values("dist_m")
        # Dibujar líneas
        for _, rr in pairs_df.iterrows():
            folium.PolyLine(
                locations=[(rr["lat1"], rr["lon1"]), (rr["lat2"], rr["lon2"])],
                color="#d62728", weight=3, opacity=0.8
            ).add_to(m)

# Controles extra
folium.LayerControl(collapsed=False).add_to(m)
MeasureControl(position='topright', primary_length_unit='meters').add_to(m)

# Render mapa
map_out = st_folium(m, use_container_width=True, returned_objects=[])

# ===================== TABLAS DE APOYO =====================

st.markdown("### 📄 Datos filtrados")
st.dataframe(df_filtered, use_container_width=True)

if distance_enable and not pairs_df.empty:
    st.markdown(f"### 📏 Pares de puntos a ≤ {distance_threshold_m} m")
    # Agregar columnas con tiempos si existen
    for tcol in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
        if tcol in df_filtered.columns:
            # traer tiempos de los índices de cada par
            pairs_df[f"{tcol}_1"] = df_filtered.loc[pairs_df["idx_1"], tcol].values
            pairs_df[f"{tcol}_2"] = df_filtered.loc[pairs_df["idx_2"], tcol].values
    st.dataframe(pairs_df.drop(columns=["lat1","lon1","lat2","lon2"]), use_container_width=True)

st.info(
    "Tip: Ajusta la **ventana de tiempo** en minutos para detectar posibles respuestas duplicadas exactas, "
    "y el **umbral de distancia** para ver pares de puntos cercanos unidos por líneas."
)
