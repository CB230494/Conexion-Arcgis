# -*- coding: utf-8 -*-
# app.py — Lector de encuestas ArcGIS / Survey123 (solo lectura) con autodetección de 403

import re
from io import BytesIO
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="Lectura de encuestas ArcGIS (Solo lectura)", layout="wide")
st.title("📥 Lectura de encuestas ArcGIS / Survey123 (solo lectura)")
st.caption("Solo lectura. Soporta ArcGIS Online y Enterprise, items de Survey123 y Feature Services.")

# --------------------------- Helpers HTTP/JSON ---------------------------
def _http_get(url: str, params: dict | None = None, timeout: int = 60) -> requests.Response:
    r = requests.get(url, params=params, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r

def _http_post(url: str, data: dict | None = None, timeout: int = 60) -> requests.Response:
    r = requests.post(url, data=data, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r

def _json_or_raise_text(resp: requests.Response) -> dict:
    ctype = resp.headers.get("Content-Type", "")
    txt = resp.text.strip()
    try:
        if "application/json" in ctype or txt.startswith("{") or txt.startswith("["):
            j = resp.json()
        else:
            raise ValueError("Respuesta no es JSON")
    except Exception:
        preview = txt[:300].replace("\n", " ")
        raise RuntimeError(f"La URL no devolvió JSON (posible falta de permisos/login o URL no-REST). "
                           f"Respuesta inicial: {preview}")
    if isinstance(j, dict) and "error" in j:
        # Errores REST de ArcGIS vienen bajo 'error'
        err = j["error"]
        code = err.get("code")
        message = err.get("message") or err.get("messageCode") or "Error REST"
        details = err.get("details") or []
        raise RuntimeError(f"{code or ''} {message}. details={details}")
    return j

def _is_403_message(msg: str) -> bool:
    return "403" in msg or "GWM_0003" in msg or "do not have permissions" in msg.lower()

# --------------------------- Detectores y parsing ---------------------------
FEATURE_RE = re.compile(r"/FeatureServer(?:/\d+)?/?$", re.IGNORECASE)

def is_featurelayer_url(url: str) -> bool:
    return bool(FEATURE_RE.search(url.strip()))

def clean_url(u: str) -> str:
    return u.strip().rstrip("/")

def expand_arcgis_short(short_url: str) -> str:
    r = _http_get(short_url)
    return r.url  # URL final (item.html?id=...)

def extract_item_id_from_url(url: str) -> Optional[str]:
    q = parse_qs(urlparse(url).query)
    return q.get("id", [None])[0]

def extract_item_id_any(s: str) -> Optional[str]:
    s = s.strip()
    if len(s) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return s
    if "arcg.is/" in s:
        expanded = expand_arcgis_short(s)
        return extract_item_id_from_url(expanded)
    if "item.html" in s:
        return extract_item_id_from_url(s)
    return None

# --------------------------- Token / autenticación ---------------------------
def guess_generate_token_endpoint(portal_base: str) -> str:
    portal = clean_url(portal_base)
    netloc = urlparse(portal).netloc
    if "arcgis.com" in netloc:  # ArcGIS Online
        return "https://www.arcgis.com/sharing/rest/generateToken"
    return f"{portal}/sharing/rest/generateToken"

def generate_token(portal_base: str, username: str, password: str, referer: Optional[str] = None) -> str:
    ep = guess_generate_token_endpoint(portal_base)
    data = {
        "username": username,
        "password": password,
        "client": "referer",
        "referer": referer or portal_base,
        "f": "json",
        "expiration": 60
    }
    r = _http_post(ep, data=data, timeout=30)
    j = _json_or_raise_text(r)
    token = j.get("token")
    if not token:
        raise RuntimeError("No se obtuvo 'token' en la respuesta al generar token.")
    return token

# --------------------------- REST helpers ---------------------------
def get_json(url: str, token: Optional[str] = None, params: dict | None = None) -> dict:
    p = {"f": "json"}
    if params: p.update(params)
    if token:  p["token"] = token
    r = _http_get(url, params=p)
    return _json_or_raise_text(r)

def post_json(url: str, token: Optional[str] = None, data: dict | None = None) -> dict:
    d = {"f": "json"}
    if data:  d.update(data)
    if token: d["token"] = token
    r = _http_post(url, data=d)
    return _json_or_raise_text(r)

# --------------------------- Resolver capa desde Item ---------------------------
def resolve_featurelayer_from_item(portal_base: str, item_id: str, token: Optional[str]) -> str:
    portal = clean_url(portal_base)
    item_url = f"{portal}/sharing/rest/content/items/{item_id}"

    meta = get_json(item_url, token=token)
    t = (meta.get("type") or "").lower()
    svc_url = meta.get("url", "") or ""

    # Caso 1: Item ya es un Feature Service/Layer
    if t in ["feature service", "feature layer", "feature service item"]:
        if is_featurelayer_url(svc_url):
            return svc_url if "/FeatureServer/" in svc_url else f"{svc_url}/0"
        if svc_url.endswith("/FeatureServer"):
            return f"{svc_url}/0"

    # Caso 2: Survey123 → relación Survey2Data (datos)
    rel = get_json(f"{item_url}/relatedItems",
                   token=token,
                   params={"relationshipType": "Survey2Data", "direction": "forward"})
    for it in rel.get("relatedItems", []):
        rel_url = it.get("url", "")
        if rel_url:
            if is_featurelayer_url(rel_url):
                return rel_url if "/FeatureServer/" in rel_url else f"{rel_url}/0"
            if rel_url.endswith("/FeatureServer"):
                return f"{rel_url}/0"

    # Caso 3: último intento con la 'url' del propio ítem
    if svc_url:
        if is_featurelayer_url(svc_url):
            return svc_url if "/FeatureServer/" in svc_url else f"{svc_url}/0"
        if svc_url.endswith("/FeatureServer"):
            return f"{svc_url}/0"

    raise RuntimeError("No se pudo resolver la URL de FeatureLayer desde el ítem (revisa permisos).")

# --------------------------- Descarga de features ---------------------------
def fetch_all_features(layer_url: str, token: Optional[str]) -> Tuple[pd.DataFrame, dict]:
    layer_url = clean_url(layer_url)
    meta = get_json(layer_url, token=token)
    max_count = meta.get("maxRecordCount", 2000)
    geom_type = meta.get("geometryType", "")
    fields = meta.get("fields", [])
    oid_field = next((f["name"] for f in fields if f.get("type") == "esriFieldTypeOID"), "OBJECTID")

    cnt = post_json(f"{layer_url}/query", token=token, data={
        "where": "1=1", "returnCountOnly": "true", "outFields": "*", "returnGeometry": "false"
    }).get("count", 0)

    rows, fetched = [], 0
    while fetched < cnt:
        page = post_json(f"{layer_url}/query", token=token, data={
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "resultOffset": fetched,
            "resultRecordCount": max_count,
            "orderByFields": oid_field,
            "outSR": 4326
        })
        feats = page.get("features", [])
        if not feats:
            break
        for f in feats:
            attrs = f.get("attributes", {}) or {}
            geom = f.get("geometry")
            if geom_type == "esriGeometryPoint" and geom:
                attrs["_lon"] = geom.get("x")
                attrs["_lat"] = geom.get("y")
            rows.append(attrs)
        fetched += len(feats)

    return pd.DataFrame(rows), meta

# --------------------------- UI: Autenticación ---------------------------
with st.expander("🔐 Acceso (marca si la capa/ítem es privado)", expanded=False):
    c1, c2 = st.columns(2)
    with c1:
        portal_base = st.text_input(
            "Portal (Online o Enterprise)",
            value="https://sembremos-seg.maps.arcgis.com",
            help="Si es *.arcgis.com, el token se genera en https://www.arcgis.com automáticamente."
        )
        privado = st.checkbox("La capa/ítem es privado (requiere login)", value=False)
    with c2:
        user = st.text_input("Usuario", value="", disabled=not privado)
        pwd = st.text_input("Contraseña", value="", disabled=not privado, type="password")

# --------------------------- UI: Origen ---------------------------
with st.expander("🧩 Origen de datos", expanded=True):
    origen = st.radio(
        "¿Qué vas a pegar?",
        ["URL de Feature Layer (/FeatureServer/0)",
         "URL/ID de Ítem (Survey123 o Feature Service)",
         "Enlace corto arcg.is/xxxxx"],
        horizontal=False
    )
    feature_url, item_input = "", ""
    if origen == "URL de Feature Layer (/FeatureServer/0)":
        feature_url = st.text_input("Pega la URL del FeatureLayer", value="")
    elif origen == "URL/ID de Ítem (Survey123 o Feature Service)":
        item_input = st.text_input("Pega la URL del ítem (home/item.html?id=...) o el ID", value="")
    else:
        item_input = st.text_input("Pega el enlace corto arcg.is/xxxxx", value="")

ok = st.button("Cargar datos", type="primary")

# --------------------------- Lógica principal ---------------------------
def cargar(token: Optional[str]) -> None:
    # Resolver URL de capa
    if origen == "URL de Feature Layer (/FeatureServer/0)":
        if not feature_url:
            st.error("Pega la URL del FeatureLayer (debe terminar en /FeatureServer/0).")
            st.stop()
        layer_url = clean_url(feature_url)
    else:
        item_id = extract_item_id_any(item_input)
        if not item_id:
            st.error("No pude identificar el ID del ítem. Si pegaste arcg.is, verifica que apunte a un item.")
            st.stop()
        with st.spinner("Resolviendo URL de FeatureLayer..."):
            layer_url = resolve_featurelayer_from_item(portal_base, item_id, token)

    st.info(f"Usando capa: `{layer_url}`")

    # Descargar registros
    with st.spinner("Descargando registros..."):
        df, meta = fetch_all_features(layer_url, token)

    if df.empty:
        st.warning("La capa no tiene registros o no tienes permisos para verlos.")
        st.stop()

    left, right = st.columns([2, 1])
    with right:
        st.subheader("ℹ️ Metadatos")
        st.write({
            "name": meta.get("name"),
            "geometryType": meta.get("geometryType"),
            "maxRecordCount": meta.get("maxRecordCount"),
            "fields": len(meta.get("fields", []))
        })

    with left:
        st.subheader("📄 Tabla de respuestas")
        st.dataframe(df, use_container_width=True, hide_index=True)

        c1, c2 = st.columns(2)
        with c1:
            st.download_button("⬇️ CSV", df.to_csv(index=False).encode("utf-8"),
                               file_name="encuestas_arcgis.csv", mime="text/csv")
        with c2:
            bio = BytesIO()
            with pd.ExcelWriter(bio, engine="xlsxwriter") as w:
                df.to_excel(w, index=False, sheet_name="datos")
            st.download_button("⬇️ Excel", bio.getvalue(),
                               file_name="encuestas_arcgis.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # Mapa si son puntos
    if {"_lat", "_lon"}.issubset(df.columns):
        st.subheader("🗺️ Mapa (puntos)")
        lat = pd.to_numeric(df["_lat"], errors="coerce").dropna()
        lon = pd.to_numeric(df["_lon"], errors="coerce").dropna()
        if not lat.empty and not lon.empty:
            m = folium.Map(location=(lat.mean(), lon.mean()), zoom_start=11, control_scale=True)
            for _, r in df.iterrows():
                if pd.notna(r.get("_lat")) and pd.notna(r.get("_lon")):
                    folium.CircleMarker(location=(float(r["_lat"]), float(r["_lon"])),
                                        radius=4, fill=True).add_to(m)
            st_folium(m, height=480, use_container_width=True)
    else:
        st.info("La capa no es de puntos o no trae geometría; se muestra solo la tabla.")

if ok:
    try:
        token = None
        # Intento 1: si marcaste privado, genero token primero
        if privado:
            if not (portal_base and user and pwd):
                st.error("Para privado: completa portal, usuario y contraseña.")
                st.stop()
            with st.spinner("Generando token..."):
                token = generate_token(portal_base, user, pwd, referer=portal_base)
            cargar(token)
        else:
            # Intento 2: probar sin token; si da 403, te pido login automáticamente
            try:
                cargar(token=None)
            except Exception as e:
                msg = str(e)
                if _is_403_message(msg):
                    st.warning("🔒 El recurso parece privado. Activa 'La capa/ítem es privado' y coloca usuario/contraseña.")
                raise
    except Exception as e:
        st.error(f"Error: {e}")
        st.toast("Verifica: (1) URL del FeatureLayer o Ítem, (2) permisos, (3) si es privado, inicia sesión.", icon="⚠️")
