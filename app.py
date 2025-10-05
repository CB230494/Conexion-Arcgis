# -*- coding: utf-8 -*-
import io
import numpy as np
import pandas as pd
import streamlit as st
from datetime import datetime

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
    """Devuelve DataFrame con grupos de duplicados exactos (mismo contenido) en ventana corta."""
    if df.empty or not content_cols: return pd.DataFrame()
    tmp = df.copy()

    # normalización de columnas de contenido
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
    tmp["_row_i"] = tmp.index  # conservar índice original
    tmp = tmp.sort_values(time_col).reset_index(drop=True)

    win = pd.Timedelta(minutes=window_minutes)
    results = []
    for h, g in tmp.groupby("_hash_content", dropna=False):
        g = g.copy().sort_values(time_col)
        if g.shape[0] < 2:
            continue
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
                    "indices": gb["_row_i"].tolist()
                }
                for c in content_cols:
                    row[c] = norm_df.loc[gb.index[0], c]
                results.append(row)
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(["conteo_duplicados","ultimo"], ascending=[False, False])

def center_from_points(df, lon_col, lat_col):
    if df.empty or lon_col not in df.columns or lat_col not in df.columns:
        return (10.0, -84.0)
    mlat = df[lat_col].mean(skipna=True); mlon = df[lon_col].mean(skipna=True)
    if np.isnan(mlat) or np.isnan(mlon): return (10.0, -84.0)
    return (float(mlat), float(mlon))

def to_excel_download(df: pd.DataFrame, filename: str = "datos_limpios.xlsx", key: str = "dl_limpio_footer"):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="datos")
    bio.seek(0)
    st.download_button(
        label="⬇️ Descargar Excel limpio",
        data=bio,
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=key,
    )

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

df_raw, sheet_name = load_excel_first_sheet(uploaded)

# mantener df limpio en sesión para permitir eliminaciones
if "df_clean" not in st.session_state:
    st.session_state.df_clean = df_raw.copy()
df = st.session_state.df_clean

# coerción de fechas si están
for c in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
    if c in df.columns:
        df[c] = pd.to_datetime(df[c], errors="coerce")

# ======== Parámetros fijos ========
lon_col, lat_col = "x", "y"
time_col = "CreationDate" if "CreationDate" in df.columns else (
    "EditDate" if "EditDate" in df.columns else "¿Cuándo fue el último incidente?"
)
window_minutes = 10
distance_threshold_m = 200

# ======== Métricas (conteo más grande + fecha dd/mm/aaaa) ========
c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1])

with c1:
    total = int(df.shape[0])
    st.markdown(
        f"""
        <div style="font-size:56px; font-weight:800; line-height:1; margin-bottom:2px;">{total}</div>
        <div style="font-size:14px; opacity:0.75; margin-top:-2px;">Respuestas</div>
        """,
        unsafe_allow_html=True
    )

with c2:
    # Hoja detectada como metric normal
    st.metric("Hoja detectada", sheet_name)

with c3:
    if time_col in df.columns:
        ult = df[time_col].max()
        fecha_txt = "-" if pd.isna(ult) else pd.to_datetime(ult).strftime("%d/%m/%Y")
        st.metric("Última respuesta", fecha_txt)

with c4:
    if "¿Cómo califica la seguridad en su zona?" in df.columns:
        seg_counts = df["¿Cómo califica la seguridad en su zona?"].astype(str).str.lower().value_counts()
        top_seg = seg_counts.index[0] if not seg_counts.empty else "-"
        st.metric("Calificación más frecuente", top_seg)

# ======== Duplicados exactos (ventana 10 min) ========
st.markdown("### 🧪 Duplicados exactos en ≤ 10 min")
content_cols = [c for c in df.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df, time_col=time_col, window_minutes=window_minutes, content_cols=content_cols)

dup_set = set()
if dupes.empty:
    st.success("No se detectaron grupos de respuestas EXACTAMENTE iguales en la ventana indicada.")
else:
    st.warning(f"Se detectaron {dupes.shape[0]} grupo(s) de posibles duplicados exactos.")
    for lst in dupes["indices"]:
        dup_set.update(lst)

    with st.expander("🧹 Limpiar duplicados (mantener 1 por grupo)"):
        dupes_show = dupes[["conteo_duplicados","primero","ultimo","ventana_min","indices"]].copy()

        def fmt_dt(x):
            try:
                return pd.to_datetime(x).strftime("%d/%m/%Y")
            except Exception:
                return "-"

        dupes_show["rango"] = dupes_show["primero"].apply(fmt_dt) + " → " + dupes_show["ultimo"].apply(fmt_dt)
        st.dataframe(dupes_show.drop(columns=["primero","ultimo"]), use_container_width=True)

        opciones = [
            f"Grupo {i+1} – {row['conteo_duplicados']} elementos – {row['rango']}"
            for i, row in dupes_show.reset_index(drop=True).iterrows()
        ]
        seleccion = st.multiselect("Selecciona grupos a limpiar (o deja vacío para todos):", opciones)

        criterio = st.radio("Criterio de conservación (¿cuál se queda en cada grupo?)",
                            ["Mantener el más reciente", "Mantener el más antiguo"], horizontal=True)

        def limpiar(df_in: pd.DataFrame, dupes_df: pd.DataFrame, seleccion_opciones: list, opciones_txt: list, criterio_txt: str):
            df_out = df_in.copy()
            selected_rows = list(range(len(dupes_df)))
            if seleccion_opciones:
                selected_rows = [opciones_txt.index(s) for s in seleccion_opciones]

            for pos in selected_rows:
                idxs = dupes_df.iloc[pos]["indices"]
                vivos = df_out.index.intersection(idxs)
                if len(vivos) <= 1:
                    continue

                sub = df_out.loc[vivos]

                if time_col in df_out.columns:
                    ts = pd.to_datetime(sub[time_col], errors="coerce")
                    ts_non = ts.dropna()
                    if criterio_txt.startswith("Mantener el más reciente"):
                        keep = ts_non.idxmax() if not ts_non.empty else sub.index[0]
                    else:
                        keep = ts_non.idxmin() if not ts_non.empty else sub.index[0]
                else:
                    keep = sub.index[0]

                drop_ids = [i for i in sub.index if i != keep]
                df_out = df_out.drop(index=drop_ids, errors="ignore")

            return df_out

        colb1, colb2 = st.columns([1,1])
        with colb1:
            if st.button("🧹 Limpiar seleccionados / todos"):
                st.session_state.df_clean = limpiar(
                    st.session_state.df_clean, dupes, seleccion, opciones, criterio
                )
                st.success("Limpieza realizada. Actualizando tabla y mapa…")
                st.rerun()
        with colb2:
            to_excel_download(st.session_state.df_clean, filename="datos_limpios.xlsx", key="dl_limpio_expander")

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

# marcadores: duplicados en rojo, resto azul
if not valid_points.empty:
    mc = MarkerCluster(name="Clúster de puntos")
    mc.add_to(m)
    dup_set_local = set()
    if not dupes.empty:
        for lst in dupes["indices"]:
            dup_set_local.update(lst)
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
        is_dup = idx in dup_set_local
        icon = folium.Icon(color="red" if is_dup else "blue", icon="info-sign")
        folium.Marker([lat, lon], popup=folium.Popup("<br>".join(popup_fields), max_width=420), icon=icon).add_to(mc)

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
    idxs = valid_points.index.to_list()
    for a in range(len(pts)):
        for b in range(a + 1, len(pts)):
            lat1, lon1 = pts[a]; lat2, lon2 = pts[b]
            d = haversine_m(lat1, lon1, lat2, lon2)
            if d <= distance_threshold_m:
                pairs.append((idxs[a], idxs[b], float(d), lat1, lon1, lat2, lon2))
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

# ======== Descarga & Tabla ========
st.markdown("### ⬇️ Exportar datos limpios")
to_excel_download(st.session_state.df_clean, filename="datos_limpios.xlsx", key="dl_limpio_footer")

st.markdown("### 📄 Datos (primeras filas)")
st.dataframe(st.session_state.df_clean.head(1000), use_container_width=True)



