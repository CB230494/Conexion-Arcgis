# -*- coding: utf-8 -*-
import requests, math, hashlib
import pandas as pd
import streamlit as st
import pydeck as pdk
import folium
from streamlit_folium import st_folium

# =========================
# Config
# =========================
st.set_page_config(page_title="Dashboard Encuesta Seguridad (Solo lectura)", layout="wide")

PUB = st.secrets.get("agol_public", {})
LAYER_URL = PUB.get("feature_layer_url", "").rstrip("/")

if not LAYER_URL.endswith("/FeatureServer/0") and "/FeatureServer/" not in LAYER_URL:
    st.error("Revisa secrets: [agol_public] feature_layer_url debe ser la URL del servicio y terminar en /FeatureServer/0")
    st.stop()

st.sidebar.success("Modo: Solo lectura (sin token)")
st.sidebar.caption(LAYER_URL)

# =========================
# REST helpers (sin token)
# =========================
def layer_metadata(layer_url):
    r = requests.get(layer_url, params={"f":"json"}, timeout=60)
    r.raise_for_status()
    return r.json()

def query_layer(layer_url, where="1=1", out_fields="*", return_geom=True):
    params = {
        "f":"json",
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,
        "returnGeometry": str(return_geom).lower()
    }
    r = requests.get(f"{layer_url}/query", params=params, timeout=90)
    r.raise_for_status()
    return r.json()

# =========================
# Duplicados helpers
# =========================
def norm(v):
    if v is None: return ""
    if isinstance(v, float) and math.isnan(v): return ""
    return str(v).strip().upper()

def day_from_ms(ms):
    if ms in (None, "", float("nan")): return ""
    try:
        return pd.to_datetime(int(ms), unit="ms").strftime("%Y%m%d")
    except Exception:
        return ""

def build_dup_key(row, campos, usar_dia=True, round_coords=5):
    parts = [norm(row.get(c)) for c in campos]
    if usar_dia and "CreationDate" in row:
        parts.append(day_from_ms(row.get("CreationDate")))
    x, y = row.get("x"), row.get("y")
    if x is not None and y is not None:
        parts += [str(round(x, round_coords)), str(round(y, round_coords))]
    raw = "|".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest() if raw else ""

# =========================
# Cargar metadatos y datos
# =========================
meta = layer_metadata(LAYER_URL)
OID = meta.get("objectIdField", "OBJECTID")
title = meta.get("name", "Capa")

st.success(f"Conectado a: {title}")
st.sidebar.info(f"OID: {OID}")

resp = query_layer(LAYER_URL, out_fields="*", return_geom=True)
features = resp.get("features", [])

if not features:
    st.warning("No hay registros en la capa.")
    st.stop()

rows = []
for f in features:
    atr = f.get("attributes", {})
    geo = f.get("geometry", {})
    atr["x"] = geo.get("x")
    atr["y"] = geo.get("y")
    rows.append(atr)

df = pd.DataFrame(rows)
total = len(df)

# =========================
# Parámetros de duplicados (UI)
# =========================
st.sidebar.header("Detección de duplicados")
base_fields = ["seguridad_general","tipo_incidente","factores","frecuencia","contacto"]
default_list = [c for c in base_fields if c in df.columns]
campos = st.sidebar.multiselect("Campos clave", options=sorted(df.columns), default=default_list)
usar_dia = st.sidebar.toggle("Usar día (CreationDate)", value=True)
redondeo = st.sidebar.slider("Redondeo coords", 0, 7, 5)

# Calcular clave si la capa no trae dup_key
if "dup_key" not in df.columns or df["dup_key"].isna().all():
    df["_dup_key_calc"] = df.apply(lambda r: build_dup_key(r, campos, usar_dia, redondeo), axis=1)
    dup_key_col = "_dup_key_calc"
else:
    dup_key_col = "dup_key"

grp = df.groupby(dup_key_col)[OID].transform("count")
df["dup_is_dup_calc"] = ((df[dup_key_col]!="") & (grp>1)).astype(int)
df["dup_group_calc"] = df[dup_key_col].fillna("").str[:8]

# =========================
# KPIs
# =========================
c1,c2,c3 = st.columns(3)
c1.metric("Total respuestas", f"{total}")
c2.metric("Duplicadas", f"{int(df['dup_is_dup_calc'].sum())}")
c3.metric("Válidas", f"{int((1-df['dup_is_dup_calc']).sum())}")

# =========================
# Mapas
# =========================
st.subheader("Mapas")
tab1, tab2 = st.tabs(["Heatmap (pydeck)", "Puntos (folium)"])

with tab1:
    dfm = df.dropna(subset=["y","x"]).rename(columns={"y":"lat","x":"lon"})
    if not dfm.empty:
        heat = pdk.Layer("HeatmapLayer", data=dfm, get_position='[lon, lat]', opacity=0.9)
        view = pdk.ViewState(latitude=float(dfm["lat"].mean()), longitude=float(dfm["lon"].mean()), zoom=7)
        st.pydeck_chart(pdk.Deck(layers=[heat], initial_view_state=view))
    else:
        st.info("No hay coordenadas para heatmap.")

with tab2:
    pts = df.dropna(subset=["y","x"])
    if not pts.empty:
        m = folium.Map(location=[pts["y"].mean(), pts["x"].mean()], zoom_start=7)
        for _, r in pts.iterrows():
            folium.CircleMarker([r["y"], r["x"]], radius=4, fill=True,
                popup=folium.Popup(f"OID: {r[OID]}<br>Seguridad: {r.get('seguridad_general','')}", max_width=250)
            ).add_to(m)
        st_folium(m, height=450, use_container_width=True)
    else:
        st.info("Sin puntos para mostrar.")

# =========================
# Tabla (solo lectura)
# =========================
st.subheader("Tabla (solo lectura)")
show_cols = [OID,"CreationDate","Creator","seguridad_general","tipo_incidente","factores","frecuencia","contacto", dup_key_col, "dup_is_dup_calc","dup_group_calc"]
show_cols = [c for c in show_cols if c in df.columns]

st.dataframe(df[show_cols], use_container_width=True)

# =========================
# Exportar duplicados
# =========================
st.markdown("### Exportar duplicados a Excel")
dups = df[df["dup_is_dup_calc"]==1].copy()
if not dups.empty:
    st.download_button("Descargar duplicados.xlsx", data=dups.to_excel(index=False), file_name="duplicados.xlsx")
else:
    st.info("No hay duplicados con la lógica actual.")

st.caption("Modo lectura: edición y eliminación deshabilitadas por diseño.")


