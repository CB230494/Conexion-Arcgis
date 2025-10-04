# -*- coding: utf-8 -*-
# app.py — Lector de encuestas ArcGIS / Survey123 (solo lectura)

import re
import json
from io import BytesIO
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import pandas as pd
import streamlit as st

import folium
from streamlit_folium import st_folium


# =========================
# Config UI
# =========================
st.set_page_config(page_title="Lectura de encuestas ArcGIS (Solo lectura)", layout="wide")
st.title("📥 Lectura de encuestas ArcGIS / Survey123 (solo lectura)")
st.caption("Compatible con ArcGIS Online y Enterprise. No escribe ni borra nada — solo lectura.")

# =========================
# Utilidades HTTP
# =========================
def _http_get(url: str, params: dict | None = None, timeout: int = 60) -> requests.Response:
    r = requests.get(url, params=params, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r

def _http_post(url: str, data: dict | None = None, timeout: int = 60) -> requests.Response:
    r = requests.post(url, data=data, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r

def _json_or_raise_text(resp: requests.Response) -> dict:
    """
    ArcGIS devuelve HTML cuando no hay permisos o cuando la URL no es de REST.
    Este helper evita 'Expecting value: line 1 column 1 (char 0)' y muestra el
    texto crudo si no es JSON.
    """
    ctype = resp.headers.get("Content-Type", "")
    txt = resp.text.strip()
    try:
        if "application/json" in ctype or txt.startswith("{") or txt.startswith("["):
            j = resp.json()
        else:
            raise ValueError("Respuesta no es JSON")
    except Exception:
        # Devolver un error legible con un preview del contenido
        preview = txt[:300].replace("\n", " ")
        raise RuntimeError(f"La URL no devolvió JSON (posible falta de permisos o URL incorrecta). "
                           f"Respuesta inicial: {preview}")
    if isinstance(j, dict) and "error" in j:
        raise RuntimeError(j["error"])
    return j


# =========================
# Detectores y parsers
# =========================
FEATURE_RE = re.compile(r"/FeatureServer(?:/\d+)?/?$", re.IGNORECASE)

def is_feature_layer_url(url: str) -> bool:
    return bool(FEATURE_RE.search(url.strip()))

def clean_url(u: str) -> str:
    return u.strip().rstrip("/")

def expand_arcgis_short(short_url: str) -> str:
    """
    Expande un arcg.is/xxxxx para obtener la URL final (generalmente item.html?id=...).
    """
    resp = _http_get(short_url)
    return resp.url  # requests siguió la redirección

def extract_item_id_from_url(url: str) -> Optional[str]:
    """
    Toma un URL tipo .../home/item.html?id=XXXXXXXX y devuelve el ID.
    """
    try:
        q = parse_qs(urlparse(url).query)
        return q.get("id", [None])[0]
    except Exception:
        return None

def extract_item_id_any(s: str) -> Optional[str]:
    """
    Acepta:
      - ID de 32 chars (hex)
      - URL de item (home/item.html?id=...)
      - Enlace corto arcg.is/xxxxx (lo expande y toma el item)
    """
    s = s.strip()
    # ID directo
    if len(s) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return s
    # arcg.is corto
    if "arcg.is/" in s:
        expanded = expand_arcgis_short(s)
        return extract_item_id_from_url(expanded)
    # item largo
    if "item.html" in s:
        return extract_item_id_from_url(s)
    return None


# =========================
# Token / Autenticación
# =========================
def guess_generate_token_endpoint(portal_base: str) -> str:
    """
    ArcGIS Online: token siempre en https://www.arcgis.com/sharing/rest/generateToken
    Enterprise:    portal/sharing/rest/generateToken
    """
    portal = clean_url(portal_base)
    # Si es *.arcgis.com asumimos Online
    if "arcgis.com" in urlparse(portal).netloc:
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
        "expiration": 60  # minutos
    }
    r = _http_post(ep, data=data, timeout=30)
    j = _json_or_raise_text(r)
    token = j.get("token")
    if not token:
        raise RuntimeError("No se obtuvo 'token' en la respuesta.")
    return token


# =========================
# REST helpers
# =========================
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


# =========================
# Resolución de capa desde Item
# =========================
def resolve_featurelayer_from_item(portal_base: str, item_id: str, token: Optional[str]) -> str:
    """
    Dado un ítem (Survey o Feature Service), devuelve una URL válida de FeatureLayer.
    - Si el item ya es Feature Service → su URL.
    - Si es Survey123 → relación Survey2Data (capa de respuestas).
    """
    portal = clean_url(portal_base)
    item_url = f"{portal}/sharing/rest/content/items/{item_id}"
    meta = get_json(item_url, token=token)

    # 1) Feature Service directo
    t = (meta.get("type") or "").lower()
    svc_url = meta.get("url", "") or ""

    if t in ["feature service", "feature layer"]:
        if is_feature_layer_url(svc_url):
            return svc_url if "/FeatureServer/" in svc_url else f"{svc_url}/0"
        if svc_url.endswith("/FeatureServer"):
            return f"{svc_url}/0"

    # 2) Survey → relación Survey2Data
    rel = get_json(f"{item_url}/relatedItems",
                   token=token,
                   params={"relationshipType": "Survey2Data", "direction": "forward"})
    for it in rel.get("relatedItems", []):
        rel_url = it.get("url", "")
        if rel_url:
            if is_feature_layer_url(rel_url):
                return rel_url if "/FeatureServer/" in rel_url else f"{rel_url}/0"
            if rel_url.endswith("/FeatureServer"):
                return f"{rel_url}/0"

    # 3) Última oportunidad con 'url' del propio ítem
    if svc_url:
        if is_feature_layer_url(svc_url):
            return svc_url if "/FeatureServer/" in svc_url else f"{svc_url}/0"
        if svc_url.endswith("/FeatureServer"):
            return f"{svc_url}/0"

    raise RuntimeError("No se pudo resolver la URL de FeatureLayer desde el ítem. "
                       "Verifica permisos o comparte la URL del FeatureServer directamente.")


# =========================
# Downloader de registros
# =========================
def fetch_all_features(layer_url: str, token: Optional[str]) -> Tuple[pd.DataFrame, dict]:
    layer_url = clean_url(layer_url)

    # Metadatos de la capa
    meta = get_json(layer_url, token=token)
    max_count = meta.get("maxRecordCount", 2000)
    geom_type = meta.get("geometryType", "")
    fields = meta.get("fields", [])
    oid_field = next((f["name"] for f in fields if f.get("type") == "esriFieldTypeOID"), "OBJECTID")

    # Conteo total
    count = post_json(f"{layer_url}/query", token=token, data={
        "where": "1=1",
        "returnCountOnly": "true",
        "outFields": "*",
        "returnGeometry": "false"
    }).get("count", 0)

    rows = []
    fetched = 0
    while fetched < count:
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

    df = pd.DataFrame(rows)
    return df, meta


# =========================
# UI – Autenticación
# =========================
with st.expander("🔐 Acceso (marca solo si la capa es privada)", expanded=False):
    c1, c2 = st.columns(2)
    with c1:
        portal_base = st.text_input(
            "Portal de ArcGIS (Online o Enterprise)",
            value="https://sembremos-seg.maps.arcgis.com",
            help="Para ArcGIS Online se usará https://www.arcgis.com para generar el token automáticamente."
        )
        privado = st.checkbox("La capa es privada (requiere iniciar sesión)", value=False)
    with c2:
        user = st.text_input("Usuario", value="", disabled=not privado, placeholder="tu_usuario")
        pwd = st.text_input("Contraseña", value="", disabled=not privado, type="password")

# =========================
# UI – Origen
# =========================
with st.expander("🧩 Origen de datos", expanded=True):
    origen = st.radio("¿Qué vas a pegar?",
                      ["URL de Feature Layer (/FeatureServer/0)",
                       "URL/ID de Ítem (Survey123 o Feature Service)",
                       "Enlace corto arcg.is/xxxxx"],
                      horizontal=False)

    feature_url = ""
    item_input = ""

    if origen == "URL de Feature Layer (/FeatureServer/0)":
        feature_url = st.text_input("Pega la URL del FeatureLayer", value="")
    elif origen == "URL/ID de Ítem (Survey123 o Feature Service)":
        item_input = st.text_input("Pega la URL del ítem (home/item.html?id=...) o el ID de 32 caracteres", value="")
    else:
        item_input = st.text_input("Pega el enlace corto arcg.is/xxxxx", value="")

ok = st.button("Cargar datos", type="primary")

# =========================
# Lógica principal
# =========================
if ok:
    try:
        # 1) Token si es privado
        token = None
        if privado:
            if not (portal_base and user and pwd):
                st.error("Para capas privadas, completa portal, usuario y contraseña.")
                st.stop()
            with st.spinner("Generando token..."):
                token = generate_token(portal_base, user, pwd, referer=portal_base)

        # 2) Resolver URL del FeatureLayer según origen
        if origen == "URL de Feature Layer (/FeatureServer/0)":
            if not feature_url:
                st.error("Pega la URL del FeatureLayer (debe terminar en /FeatureServer/0).")
                st.stop()
            layer_url = clean_url(feature_url)

        else:
            item_id = extract_item_id_any(item_input)
            if not item_id:
                st.error("No pude identificar el ID del ítem. "
                         "Si pegaste arcg.is, asegúrate de que redirige a un item. "
                         "Sino, pega el enlace largo (home/item.html?id=...) o el ID de 32 caracteres.")
                st.stop()
            with st.spinner("Resolviendo URL de capa de datos (FeatureLayer)..."):
                layer_url = resolve_featurelayer_from_item(portal_base, item_id, token)

        st.info(f"Usando capa: `{layer_url}`")

        # 3) Descargar registros
        with st.spinner("Descargando registros..."):
            df, meta = fetch_all_features(layer_url, token)

        if df.empty:
            st.warning("La capa no tiene registros o no tienes permisos para verlos.")
            st.stop()

        # 4) Metadatos + Tabla
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
                st.download_button("⬇️ Descargar CSV",
                                   df.to_csv(index=False).encode("utf-8"),
                                   file_name="encuestas_arcgis.csv",
                                   mime="text/csv")
            with c2:
                bio = BytesIO()
                with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
                    df.to_excel(writer, index=False, sheet_name="datos")
                st.download_button("⬇️ Descargar Excel",
                                   bio.getvalue(),
                                   file_name="encuestas_arcgis.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        # 5) Mapa (si hay puntos)
        if {"_lat", "_lon"}.issubset(df.columns):
            st.subheader("🗺️ Mapa (puntos)")
            lat = pd.to_numeric(df["_lat"], errors="coerce").dropna()
            lon = pd.to_numeric(df["_lon"], errors="coerce").dropna()
            if not lat.empty and not lon.empty:
                center = (lat.mean(), lon.mean())
                m = folium.Map(location=center, zoom_start=11, control_scale=True)
                for _, r in df.iterrows():
                    if pd.notna(r.get("_lat")) and pd.notna(r.get("_lon")):
                        folium.CircleMarker(
                            location=(float(r["_lat"]), float(r["_lon"])),
                            radius=4, fill=True
                        ).add_to(m)
                st_folium(m, height=480, use_container_width=True)
        else:
            st.info("La capa no es de puntos o no trae geometría; se muestra solo la tabla.")

    except Exception as e:
        st.error(f"Error: {e}")
        st.toast("Verifica: (1) URL del FeatureLayer o Ítem, (2) permisos, (3) si la capa es privada, usa usuario/contraseña correctos.", icon="⚠️")
