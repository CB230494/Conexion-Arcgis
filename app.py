# -*- coding: utf-8 -*-
import os, math, json, time, base64, hashlib, secrets, urllib.parse
import requests
import pandas as pd
import streamlit as st
import pydeck as pdk
import folium
from streamlit_folium import st_folium

# =========================
# Config
# =========================
st.set_page_config(page_title="Dashboard Encuesta Seguridad (OAuth AGO)", layout="wide")

OAUTH = st.secrets["oauth"]
PORTAL        = OAUTH.get("portal", "https://www.arcgis.com").rstrip("/")
CLIENT_ID     = OAUTH["client_id"]
REDIRECT_URI  = OAUTH["redirect_uri"]

ITEM = st.secrets.get("item", {})
ITEM_ID      = ITEM.get("item_id")
LAYER_INDEX  = int(ITEM.get("layer_index", 0))

AUTH_URL  = f"{PORTAL}/sharing/rest/oauth2/authorize"
TOKEN_URL = f"{PORTAL}/sharing/rest/oauth2/token"

# =========================
# Helpers OAuth2 + PKCE
# =========================
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def create_pkce():
    verifier = _b64url(secrets.token_urlsafe(96).encode())[:128]  # <= 128 chars
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge

def build_auth_url(state):
    code_verifier, code_challenge = create_pkce()
    st.session_state["code_verifier"] = code_verifier
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

def exchange_code_for_token(code):
    data = {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": st.session_state["code_verifier"],
        "f":"json"
    }
    r = requests.post(TOKEN_URL, data=data, timeout=30)
    r.raise_for_status()
    js = r.json()
    if "access_token" not in js:
        raise RuntimeError(f"Token error: {js}")
    return js

def refresh_token(refresh_token):
    data = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "f": "json"
    }
    r = requests.post(TOKEN_URL, data=data, timeout=30)
    r.raise_for_status()
    js = r.json()
    if "access_token" not in js:
        raise RuntimeError(f"Refresh error: {js}")
    return js

def get_user_token():
    # Si ya hay token y no venció, úsalo
    tok = st.session_state.get("oauth_token")
    if tok and time.time() < tok["expires_at"]:
        return tok["access_token"]

    # Si hay refresh_token, intenta refrescar
    if tok and tok.get("refresh_token"):
        js = refresh_token(tok["refresh_token"])
        js["expires_at"] = time.time() + js.get("expires_in", 7200) - 60
        st.session_state["oauth_token"] = js
        return js["access_token"]

    # Primera vez: verificar si hay ?code= en la URL (callback)
    params = st.query_params
    if "code" in params:
        code = params["code"]
        js = exchange_code_for_token(code)
        js["expires_at"] = time.time() + js.get("expires_in", 7200) - 60
        st.session_state["oauth_token"] = js
        # limpiar query params
        st.query_params.clear()
        return js["access_token"]

    # Si no hay code, mostrar botón login
    login_url = build_auth_url(state="s123")
    st.markdown(f"[🔐 Iniciar sesión con ArcGIS Online]({login_url})")
    st.stop()

# =========================
# REST Helpers (igual que antes, usan access_token)
# =========================
def get_item_info(item_id, token):
    url = f"{PORTAL}/sharing/rest/content/items/{item_id}"
    r = requests.get(url, params={"f":"json","token":token}, timeout=30)
    r.raise_for_status()
    return r.json()

def get_layer_url_from_item(item_id, layer_index, token):
    info = get_item_info(item_id, token)
    service_url = info.get("url")
    if not service_url:
        # último recurso: buscar en /data
        r = requests.get(f"{PORTAL}/sharing/rest/content/items/{item_id}/data",
                         params={"f":"json","token":token}, timeout=30)
        if r.ok:
            service_url = r.json().get("url")
    if not service_url:
        raise RuntimeError("No se pudo resolver la URL del servicio desde el item.")
    return f"{service_url.rstrip('/')}/{layer_index}"

def layer_metadata(layer_url, token):
    r = requests.get(layer_url, params={"f":"json","token":token}, timeout=30)
    r.raise_for_status()
    return r.json()

def query_layer(layer_url, token, where="1=1", out_fields="*", return_geom=True):
    payload = {
        "f": "json",
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,
        "returnGeometry": str(return_geom).lower(),
        "token": token,
    }
    r = requests.get(f"{layer_url}/query", params=payload, timeout=90)
    r.raise_for_status()
    return r.json()

def apply_updates(layer_url, token, updates):
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
# Auth de usuario
# =========================
st.sidebar.success(f"Portal: {PORTAL}")
token = get_user_token()  # ← abre login si hace falta

# =========================
# Cargar capa y datos
# =========================
if not ITEM_ID:
    st.error("Falta item_id en secrets [item].")
    st.stop()

layer_url = get_layer_url_from_item(ITEM_ID, LAYER_INDEX, token)
meta = layer_metadata(layer_url, token)
OID = meta.get("objectIdField", "OBJECTID")
title = get_item_info(ITEM_ID, token).get("title","Capa")

st.sidebar.info(f"Item: {ITEM_ID} | Layer: {LAYER_INDEX} | OID: {OID}")
st.success(f"Conectado a: {title}")

resp = query_layer(layer_url, token, out_fields="*", return_geom=True)
features = resp.get("features", [])
if not features:
    st.warning("No hay registros en la capa.")
    st.stop()

rows=[]
for f in features:
    atr = f.get("attributes",{})
    g = f.get("geometry",{})
    atr["x"] = g.get("x")
    atr["y"] = g.get("y")
    rows.append(atr)
df = pd.DataFrame(rows)
total = len(df)

# =========================
# UI duplicados
# =========================
st.sidebar.header("Detección de duplicados")
base_fields = ["seguridad_general","tipo_incidente","factores","frecuencia","contacto"]
default_list = [c for c in base_fields if c in df.columns]
campos = st.sidebar.multiselect("Campos clave", options=sorted(df.columns), default=default_list)
usar_dia = st.sidebar.toggle("Usar día (CreationDate)", value=True)
redondeo = st.sidebar.slider("Redondeo coords", 0, 7, 5)

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
    dfm = df.dropna(subset=["y","x"])
    if not dfm.empty:
        layer = pdk.Layer(
            "HeatmapLayer",
            data=dfm.rename(columns={"y":"lat","x":"lon"}),
            get_position='[lon, lat]',
            opacity=0.9
        )
        view = pdk.ViewState(latitude=float(dfm["lat"].mean()), longitude=float(dfm["lon"].mean()), zoom=7)
        st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view))
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
# Tabla + ediciones
# =========================
st.subheader("Tabla")
show_cols = [OID,"CreationDate","Creator","seguridad_general","tipo_incidente","factores","frecuencia","contacto", dup_key_col, "dup_is_dup_calc","dup_group_calc"]
show_cols = [c for c in show_cols if c in df.columns]
edit_cols = [c for c in ["seguridad_general","tipo_incidente","factores","frecuencia","contacto"] if c in df.columns]

edited = st.data_editor(df[show_cols], use_container_width=True, num_rows="dynamic",
                        disabled=[c for c in show_cols if c not in edit_cols], key="editor")

# Guardar cambios
st.markdown("### Guardar ediciones")
if st.button("Aplicar cambios a la capa", type="primary"):
    try:
        merged = edited.merge(df[[OID]+edit_cols], on=OID, suffixes=("_new","_old"))
        ups=[]
        for _, r in merged.iterrows():
            attrs={OID:int(r[OID])}; changed=False
            for c in edit_cols:
                if r.get(f"{c}_new") != r.get(f"{c}_old"):
                    attrs[c]=r.get(f"{c}_new"); changed=True
            if changed: ups.append({"attributes": attrs})
        if ups:
            res = apply_updates(layer_url, token, ups)
            st.success(f"Actualizados {len(ups)} registros.")
        else:
            st.info("No hay cambios para aplicar.")
    except Exception as e:
        st.error(f"Error al actualizar: {e}")

# Eliminar
st.markdown("### Eliminar por OID")
oids_txt = st.text_input("OBJECTIDs (coma separados)", value="")
if st.button("Eliminar OIDs indicados", type="secondary"):
    try:
        ids = [int(x.strip()) for x in oids_txt.split(",") if x.strip().isdigit()]
        if not ids:
            st.warning("No ingresaste OIDs válidos.")
        else:
            res = delete_features(layer_url, token, ids)
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


