# -*- coding: utf-8 -*-
import math, hashlib, requests
import pandas as pd
import streamlit as st
import pydeck as pdk
import folium
from streamlit_folium import st_folium

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Dashboard Encuesta (Solo lectura)", layout="wide")

# Secrets admitidos:
# [agol_public]
# feature_layer_url = "https://services.arcgis.com/.../arcgis/rest/services/<VISTA>_stakeholder/FeatureServer/0"
# item_id_public    = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # id del item 'Feature layer (vista)' público
# portal            = "https://www.arcgis.com"             # opcional
PUB = st.secrets.get("agol_public", {})
PORTAL = PUB.get("portal", "https://www.arcgis.com").rstrip("/")
FEATURE_LAYER_URL = (PUB.get("feature_layer_url") or "").rstrip("/")
ITEM_ID_PUBLIC = PUB.get("item_id_public", "").strip()

st.sidebar.success("Modo: Solo lectura (sin token)")

# ─────────────────────────────────────────────────────────────
# Helpers REST (sin token)
# ─────────────────────────────────────────────────────────────
def get_public_item_info(item_id, portal=PORTAL):
    url = f"{portal}/sharing/rest/content/items/{item_id}"
    r = requests.get(url, params={"f": "json"}, timeout=30)
    r.raise_for_status()
    return r.json()

def resolve_layer_url(feature_layer_url: str, item_id: str) -> str:
    """
    Regresa la URL de layer (terminando en /FeatureServer/0).
    • Si viene la URL en secrets, la usa.
    • Si viene un item_id público de un 'Feature layer (vista)', obtiene la URL del item.
    """
    if feature_layer_url:
        return feature_layer_url

    if item_id:
        info = get_public_item_info(item_id, PORTAL)
        svc = info.get("url", "")
        if not svc:
            raise RuntimeError("No pude leer la URL del servicio desde el item. ¿El item es público y es 'Feature layer (vista)'?")
        # forzamos layer 0 por defecto
        return f"{svc.rstrip('/')}/0"

    raise RuntimeError("Falta 'feature_layer_url' o 'item_id_public' en secrets [agol_public].")

def layer_metadata(layer_url):
    r = requests.get(layer_url, params={"f": "json"}, timeout=60)
    r.raise_for_status()
    return r.json()

def query_page(layer_url, where="1=1", out_fields="*", return_geom=True, result_offset=0, page_size=2000):
    params = {
        "f": "json",
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,
        "returnGeometry": str(return_geom).lower(),
        "resultOffset": result_offset,
        "resultRecordCount": page_size,
    }
    r = requests.get(f"{layer_url}/query", params=params, timeout=120)
    r.raise_for_status()
    return r.json()

def query_all_features(layer_url, where="1=1", out_fields="*", return_geom=True, page_size=2000):
    """Descarga todas las entidades paginando (sin token)."""
    result_offset = 0
    all_features = []
    while True:
        js = query_page(layer_url, where, out_fields, return_geom, result_offset, page_size)
        feats = js.get("features", [])
        all_features.extend(feats)
        if len(feats) < page_size:
            break
        result_offset += page_size
    return all_features

# ─────────────────────────────────────────────────────────────
# Duplicados helpers
# ─────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────
# Resolver URL de capa y cargar datos
# ─────────────────────────────────────────────────────────────
try:
    LAYER_URL = resolve_layer_url(FEATURE_LAYER_URL, ITEM_ID_PUBLIC)
except Exception as e:
    st.error(f"Config secrets inválida: {e}")
    st.stop()

st.sidebar.caption(LAYER_URL)

try:
    meta = layer_metadata(LAYER_URL)
except Exception as e:
    st.error(f"No pude leer metadatos de la capa.\n{e}")
    st.stop()

OID = meta.get("objectIdField", "OBJECTID")
title = meta.get("name", "Capa")
st.success(f"Conectado a: {title}")
st.sidebar.info(f"OID: {OID}")

try:
    feats = query_all_features(LAYER_URL, out_fields="*", return_geom=True)
except Exception as e:
    st.error(f"No pude consultar la capa.\n{e}")
    st.stop()

if not feats:
    st.warning("No hay registros en la capa.")
    st.stop()

# features -> DataFrame
rows = []
for f in feats:
    atr = f.get("attributes", {})
    geom = f.get("geometry", {})
    atr["x"] = geom.get("x")
    atr["y"] = geom.get("y")
    rows.append(atr)

df = pd.DataFrame(rows)
total = len(df)

# ─────────────────────────────────────────────────────────────
# Parámetros para duplicados
# ─────────────────────────────────────────────────────────────
st.sidebar.header("Detección de duplicados")
base_fields = ["seguridad_general", "tipo_incidente", "factores", "frecuencia", "contacto"]
default_list = [c for c in base_fields if c in df.columns]
campos = st.sidebar.multiselect("Campos clave", options=sorted(df.columns), default=default_list)
usar_dia = st.sidebar.toggle("Usar día (CreationDate)", value=True)
redondeo = st.sidebar.slider("Redondeo coords", 0, 7, 5)

# calcular clave dup (si el servicio no la trae)
if "dup_key" not in df.columns or df["dup_key"].isna().all():
    df["_dup_key_calc"] = df.apply(lambda r: build_dup_key(r, campos, usar_dia, redondeo), axis=1)
    dup_key_col = "_dup_key_calc"
else:
    dup_key_col = "dup_key"

grp = df.groupby(dup_key_col)[OID].transform("count")
df["dup_is_dup_calc"] = ((df[dup_key_col] != "") & (grp > 1)).astype(int)
df["dup_group_calc"] = df[dup_key_col].fillna("").str[:8]

# ─────────────────────────────────────────────────────────────
# KPIs
# ─────────────────────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
c1.metric("Total respuestas", f"{total}")
c2.metric("Duplicadas", f"{int(df['dup_is_dup_calc'].sum())}")
c3.metric("Válidas", f"{int((1 - df['dup_is_dup_calc']).sum())}")

# ─────────────────────────────────────────────────────────────
# Mapas
# ─────────────────────────────────────────────────────────────
st.subheader("Mapas")
tab1, tab2 = st.tabs(["Heatmap (pydeck)", "Puntos (folium)"])

with tab1:
    dfm = df.dropna(subset=["y", "x"]).rename(columns={"y": "lat", "x": "lon"})
    if not dfm.empty:
        heat = pdk.Layer("HeatmapLayer", data=dfm, get_position='[lon, lat]', opacity=0.9)
        view = pdk.ViewState(latitude=float(dfm["lat"].mean()),
                             longitude=float(dfm["lon"].mean()),
                             zoom=7)
        st.pydeck_chart(pdk.Deck(layers=[heat], initial_view_state=view))
    else:
        st.info("No hay coordenadas para heatmap.")

with tab2:
    pts = df.dropna(subset=["y", "x"])
    if not pts.empty:
        m = folium.Map(location=[pts["y"].mean(), pts["x"].mean()], zoom_start=7)
        for _, r in pts.iterrows():
            folium.CircleMarker(
                [r["y"], r["x"]],
                radius=4, fill=True,
                popup=folium.Popup(f"OID: {r[OID]}<br>Seguridad: {r.get('seguridad_general','')}",
                                   max_width=250)
            ).add_to(m)
        st_folium(m, height=450, use_container_width=True)
    else:
        st.info("Sin puntos para mostrar.")

# ─────────────────────────────────────────────────────────────
# Tabla (solo lectura)
# ─────────────────────────────────────────────────────────────
st.subheader("Tabla (solo lectura)")
show_cols = [
    OID, "CreationDate", "Creator",
    "seguridad_general", "tipo_incidente", "factores", "frecuencia", "contacto",
    dup_key_col, "dup_is_dup_calc", "dup_group_calc"
]
show_cols = [c for c in show_cols if c in df.columns]
st.dataframe(df[show_cols], use_container_width=True)

# ─────────────────────────────────────────────────────────────
# Exportar duplicados
# ─────────────────────────────────────────────────────────────
st.markdown("### Exportar duplicados a Excel")
dups = df[df["dup_is_dup_calc"] == 1].copy()
if not dups.empty:
    # to_excel devuelve binario en memoria si usamos BytesIO,
    # pero Streamlit acepta directamente el buffer que genera pandas >= 2.0
    st.download_button(
        "Descargar duplicados.xlsx",
        data=dups.to_excel(index=False),
        file_name="duplicados.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
else:
    st.info("No hay duplicados con la lógica actual.")

st.caption("Modo lectura: edición y eliminación deshabilitadas por diseño.")

