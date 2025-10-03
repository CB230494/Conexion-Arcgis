# -*- coding: utf-8 -*-
import json, math, hashlib, time
import requests
import pandas as pd
import streamlit as st
import pydeck as pdk
import folium
from streamlit_folium import st_folium

# =========================
# Lectura de secrets (modo password)
# =========================
st.set_page_config(page_title="Dashboard Encuesta Seguridad", layout="wide")

AGOL = st.secrets.get("agol", {})
PORTAL      = AGOL.get("org_url", "https://www.arcgis.com").rstrip("/")
USER        = AGOL.get("username", "")
PWD         = AGOL.get("password", "")
ITEM_ID     = AGOL.get("item_id", "")
LAYER_INDEX = int(AGOL.get("layer_index", 0))

if not (USER and PWD and ITEM_ID):
    st.error("Faltan claves en secrets.toml → [agol] org_url, username, password, item_id, layer_index")
    st.stop()

st.sidebar.success(f"Portal: {PORTAL}")
st.sidebar.caption(f"ItemID: {ITEM_ID} | Layer: {LAYER_INDEX}")
st.sidebar.caption(f"Usuario: {USER}")

# =========================
# Helpers AGO REST
# =========================
def get_token(portal=PORTAL, username=USER, password=PWD, referer="https://streamlit.io"):
    """Genera token de AGO usando username/password.
       Requiere client=referer + referer."""
    url = f"{portal}/sharing/rest/generateToken"
    data = {
        "f": "json",
        "username": username,
        "password": password,
        "client": "referer",
        "referer": referer,
        "expiration": 60
    }
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    js = r.json()
    if "token" in js:
        return js["token"]
    # Muestra error legible
    err = js.get("error", js)
    st.error(f"Token error: {err}")
    raise RuntimeError(f"No se pudo generar token: {js}")

def get_item_info(portal, item_id, token):
    url = f"{portal}/sharing/rest/content/items/{item_id}"
    r = requests.get(url, params={"f":"json","token":token}, timeout=30)
    r.raise_for_status()
    return r.json()

def get_layer_url_from_item(portal, item_id, layer_index, token):
    """Devuelve .../FeatureServer/{layer_index} a partir del item_id."""
    info = get_item_info(portal, item_id, token)
    service_url = info.get("url")
    if not service_url:
        # último recurso: /data
        r = requests.get(f"{portal}/sharing/rest/content/items/{item_id}/data",
                         params={"f":"json","token":token}, timeout=30)
        if r.ok:
            data = r.json()
            service_url = data.get("url") or data.get("serviceItemId")
    if not service_url:
        raise RuntimeError("No se pudo resolver la URL del servicio desde el item.")
    return f"{service_url.rstrip('/')}/{layer_index}"

def layer_metadata(layer_url, token):
    r = requests.get(layer_url, params={"f":"json","token":token}, timeout=30)
    r.raise_for_status()
    return r.json()

def query_layer(layer_url, token, where="1=1", out_fields="*", return_geom=True):
    params = {
        "f":"json",
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,
        "returnGeometry": str(return_geom).lower(),
        "token": token
    }
    r = requests.get(f"{layer_url}/query", params=params, timeout=90)
    r.raise_for_status()
    return r.json()

def apply_updates(layer_url, token, updates):
    # updates = [{"attributes": {OID: 1, "campo": "valor", ...}}, ...]
    data = {"f":"json", "updates": json.dumps(updates), "token": token}
    r = requests.post(f"{layer_url}/applyEdits", data=data, timeout=90)
    r.raise_for_status()
    return r.json()

def delete_features(layer_url, token, object_ids):
    data = {"f":"json", "objectIds": ",".join(map(str, object_ids)), "token": token}
    r = requests.post(f"{layer_url}/deleteFeatures", data=data, timeout=90)
    r.raise_for_status()
    return r.json()

# =========================
# Utilidades duplicados
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
# Conexión y datos
# =========================
with st.spinner("Conectando a ArcGIS..."):
    TOKEN = get_token()
    LAYER_URL = get_layer_url_from_item(PORTAL, ITEM_ID, LAYER_INDEX, TOKEN)
    meta = layer_metadata(LAYER_URL, TOKEN)
    OID = meta.get("objectIdField", "OBJECTID")
    item_title = get_item_info(PORTAL, ITEM_ID, TOKEN).get("title", "Capa")

st.success(f"Conectado a: {item_title}")
st.sidebar.info(f"OID: {OID}")

resp = query_layer(LAYER_URL, TOKEN, where="1=1", out_fields="*", return_geom=True)
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
st.sidebar.header("Duplicados")
base_fields = ["seguridad_general","tipo_incidente","factores","frecuencia","contacto"]
default_list = [c for c in base_fields if c in df.columns]
campos = st.sidebar.multiselect("Campos clave", options=sorted(df.columns), default=default_list)
usar_dia = st.sidebar.toggle("Usar día (CreationDate)", value=True)
redondeo = st.sidebar.slider("Redondeo coords", 0, 7, 5)

# Clave dup (si capa no trae dup_key)
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
c1.metric("Total", f"{total}")
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
# Tabla + Edición
# =========================
st.subheader("Tabla")
show_cols = [OID,"CreationDate","Creator","seguridad_general","tipo_incidente","factores","frecuencia","contacto", dup_key_col, "dup_is_dup_calc","dup_group_calc"]
show_cols = [c for c in show_cols if c in df.columns]
edit_cols = [c for c in ["seguridad_general","tipo_incidente","factores","frecuencia","contacto"] if c in df.columns]

edited = st.data_editor(
    df[show_cols], num_rows="dynamic", use_container_width=True,
    disabled=[c for c in show_cols if c not in edit_cols], key="editor"
)

st.markdown("### Guardar ediciones")
if st.button("Aplicar cambios a la capa", type="primary"):
    try:
        merged = edited.merge(df[[OID]+edit_cols], on=OID, suffixes=("_new","_old"))
        ups = []
        for _, r in merged.iterrows():
            attrs = {OID: int(r[OID])}; changed = False
            for c in edit_cols:
                if r.get(f"{c}_new") != r.get(f"{c}_old"):
                    attrs[c] = r.get(f"{c}_new"); changed = True
            if changed:
                ups.append({"attributes": attrs})
        if ups:
            res = apply_updates(LAYER_URL, TOKEN, ups)
            st.success(f"Actualizados {len(ups)} registros.")
        else:
            st.info("No hay cambios para aplicar.")
    except Exception as e:
        st.error(f"Error al actualizar: {e}")

st.markdown("### Eliminar por OID")
oids_txt = st.text_input("OBJECTIDs (separados por coma)", value="")
if st.button("Eliminar OIDs indicados", type="secondary"):
    try:
        ids = [int(x.strip()) for x in oids_txt.split(",") if x.strip().isdigit()]
        if not ids:
            st.warning("No ingresaste OIDs válidos.")
        else:
            res = delete_features(LAYER_URL, TOKEN, ids)
            st.success(f"Eliminados {len(ids)} registros.")
    except Exception as e:
        st.error(f"Error al eliminar: {e}")

# Exportar duplicados
st.markdown("### Exportar duplicados a Excel")
dups = df[df["dup_is_dup_calc"]==1].copy()
if not dups.empty:
    st.download_button("Descargar duplicados.xlsx", data=dups.to_excel(index=False), file_name="duplicados.xlsx")
else:
    st.info("No hay duplicados con la lógica actual.")


