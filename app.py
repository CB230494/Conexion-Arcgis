# -*- coding: utf-8 -*-
import json
import math
import re
from urllib.parse import urlparse, parse_qs

import streamlit as st
import pandas as pd
import requests

# Mapa
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="Lectura de Encuestas ArcGIS (Solo lectura)", layout="wide")
st.title("📥 Lectura de encuestas ArcGIS / Survey123 (solo lectura)")

st.caption("Funciona con capas públicas o privadas (con token). No edita ni borra nada.")

# ----------------- Utilidades -----------------
def is_feature_layer_url(url: str) -> bool:
    return bool(re.search(r"/FeatureServer(/\d+)?/?$", url))

def clean_url(u: str) -> str:
    return u.strip().rstrip("/")

def extract_item_id(item_or_short_url: str) -> str | None:
    """
    Acepta:
      - URL de item: .../home/item.html?id=XXXXXXXX...
      - URL corta arcg.is/xxxxx (no resolvemos redirección desde aquí)
      - ID directo de 32 caracteres
    """
    s = item_or_short_url.strip()
    if len(s) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return s
    if "item.html" in s:
        q = parse_qs(urlparse(s).query)
        return q.get("id", [None])[0]
    if "arcg.is" in s:
        # El corto redirige a un item; no podemos seguir redirección sin navegador.
        # Pedimos que pegue el enlace largo de item, pero devolvemos None aquí.
        return None
    return None

def gen_token(portal_base: str, username: str, password: str) -> str:
    """
    Genera token con generateToken (referer). Soporta ArcGIS Online/Enterprise.
    """
    portal = clean_url(portal_base)
    if "arcgis.com" in portal:
    url = "https://www.arcgis.com/sharing/rest/generateToken"
else:
    url = f"{portal}/sharing/rest/generateToken"
    data = {
        "username": username,
        "password": password,
        "client": "referer",
        "referer": portal,
        "f": "json",
        "expiration": 60  # minutos
    }
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["token"]

def get_json(url: str, token: str | None = None, params: dict | None = None) -> dict:
    p = {"f": "json"}
    if params: p.update(params)
    if token: p["token"] = token
    r = requests.get(url, params=p, timeout=60)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j

def post_json(url: str, token: str | None = None, data: dict | None = None) -> dict:
    d = {"f": "json"}
    if data: d.update(data)
    if token: d["token"] = token
    r = requests.post(url, data=d, timeout=60)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j

def resolve_featurelayer_from_item(portal_base: str, item_id: str, token: str | None) -> str:
    """
    Dado un item (Survey o Feature Service), encontramos la URL de FeatureServer/0
    - Si el item YA es un Feature Service, devuelve su URL.
    - Si es Survey123, buscamos la capa de datos relacionada (relationship: Survey2Data).
    """
    portal = clean_url(portal_base)
    item_url = f"{portal}/sharing/rest/content/items/{item_id}"
    meta = get_json(item_url, token=token)

    # Caso 1: Item tipo "Feature Service"
    if meta.get("type", "").lower() in ["feature service", "feature layer"]:
        svc_url = meta.get("url", "")
        if is_feature_layer_url(svc_url):
            return svc_url
        # Si es un FeatureServer sin /0, agregamos /0 por defecto
        if svc_url.endswith("/FeatureServer"):
            return svc_url + "/0"

    # Caso 2: Survey -> buscar relación Survey2Data
    rel_url = f"{item_url}/relatedItems"
    rel = get_json(rel_url, token=token, params={
        "relationshipType": "Survey2Data",
        "direction": "forward"
    })
    related = rel.get("relatedItems", [])
    if related:
        data_item = related[0]
        svc_url = data_item.get("url", "")
        if svc_url:
            if is_feature_layer_url(svc_url):
                return svc_url
            if svc_url.endswith("/FeatureServer"):
                return svc_url + "/0"

    # Último intento: si el propio item tiene 'url' que parece FeatureServer
    if "url" in meta:
        svc_url = meta["url"]
        if is_feature_layer_url(svc_url):
            return svc_url
        if svc_url.endswith("/FeatureServer"):
            return svc_url + "/0"

    raise RuntimeError("No se pudo resolver la URL de FeatureLayer desde el item. "
                       "Verifica permisos o comparte la URL del FeatureServer directamente.")

def fetch_all_features(layer_url: str, token: str | None) -> tuple[pd.DataFrame, dict]:
    """
    Descarga todos los registros en páginas según maxRecordCount.
    Devuelve DataFrame (atributos + lon/lat si es punto) y metadata de la capa.
    """
    layer_url = clean_url(layer_url)
    meta = get_json(layer_url, token=token)
    max_count = meta.get("maxRecordCount", 2000)
    geom_type = meta.get("geometryType", "")
    fields = meta.get("fields", [])
    objectid_field = next((f["name"] for f in fields if f.get("type") == "esriFieldTypeOID"), "OBJECTID")

    # Primero contemos
    count_json = post_json(layer_url + "/query", token=token, data={
        "where": "1=1",
        "returnCountOnly": "true",
        "outFields": "*",
        "returnGeometry": "true"
    })
    total = count_json.get("count", 0)

    rows = []
    fetched = 0
    while fetched < total:
        page = post_json(layer_url + "/query", token=token, data={
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "resultOffset": fetched,
            "resultRecordCount": max_count,
            "orderByFields": objectid_field,
            "outSR": 4326
        })
        feats = page.get("features", [])
        for f in feats:
            attrs = f.get("attributes", {}) or {}
            geom = f.get("geometry", None)
            # Extraer lon/lat si es punto
            if geom_type == "esriGeometryPoint" and geom:
                attrs["_lon"] = geom.get("x")
                attrs["_lat"] = geom.get("y")
            rows.append(attrs)
        fetched += len(feats)
        if len(feats) == 0:
            break

    df = pd.DataFrame(rows)
    return df, meta

# ----------------- UI -----------------
with st.expander("🔐 Acceso (solo si la capa es privada)"):
    c1, c2 = st.columns([1, 1])
    with c1:
        portal_base = st.text_input(
            "Portal de ArcGIS (ej. https://sembremos-seg.maps.arcgis.com)",
            value="https://sembremos-seg.maps.arcgis.com"
        )
        modo_privado = st.checkbox("La capa es privada (requiere iniciar sesión)", value=False)
    with c2:
        user = st.text_input("Usuario", value="", disabled=not modo_privado, placeholder="tu_usuario")
        pwd = st.text_input("Contraseña", value="", disabled=not modo_privado, type="password")

with st.expander("🧩 Origen de datos"):
    origen = st.radio("¿Qué vas a pegar?", ["URL de FeatureLayer", "URL/ID de Ítem (Survey123 o Feature Service)"], horizontal=True)
    if origen == "URL de FeatureLayer":
        feature_url = st.text_input(
            "Pega la URL del FeatureLayer (ej. .../FeatureServer/0)",
            value=""
        )
        item_input = ""
    else:
        item_input = st.text_input(
            "Pega la URL del ítem (home/item.html?id=...) o el ID de 32 caracteres",
            value=""
        )
        feature_url = ""

    st.caption("Tip: si tienes un enlace corto arcg.is/xxxxx, abrelo en el navegador y copia el enlace largo del ítem.")

ok = st.button("Cargar datos", type="primary")

# ----------------- Lógica -----------------
if ok:
    try:
        token = None
        if modo_privado:
            if not (portal_base and user and pwd):
                st.error("Para capas privadas, completa portal, usuario y contraseña.")
                st.stop()
            with st.spinner("Generando token..."):
                token = gen_token(portal_base, user, pwd)

        # Resolver URL del FeatureLayer si vino un ítem
        if origen == "URL/ID de Ítem (Survey123 o Feature Service)":
            item_id = extract_item_id(item_input)
            if not item_id:
                st.error("No pude identificar el ID del ítem. Pega el enlace largo (home/item.html?id=...) o el ID de 32 caracteres.")
                st.stop()
            with st.spinner("Resolviendo URL de capa de datos..."):
                layer_url = resolve_featurelayer_from_item(portal_base, item_id, token)
        else:
            if not feature_url:
                st.error("Pega la URL del FeatureLayer.")
                st.stop()
            layer_url = feature_url

        st.info(f"Usando capa: `{layer_url}`")

        with st.spinner("Descargando registros..."):
            df, meta = fetch_all_features(layer_url, token)

        if df.empty:
            st.warning("La capa no tiene registros (o no tienes permisos para verlos).")
            st.stop()

        # Mostrar algunos metadatos útiles
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

            # Descargas
            c1, c2 = st.columns(2)
            with c1:
                st.download_button(
                    "⬇️ Descargar CSV",
                    df.to_csv(index=False).encode("utf-8"),
                    file_name="encuestas_arcgis.csv",
                    mime="text/csv"
                )
            with c2:
                # Excel en memoria
                from io import BytesIO
                bio = BytesIO()
                with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
                    df.to_excel(writer, index=False, sheet_name="datos")
                st.download_button(
                    "⬇️ Descargar Excel",
                    bio.getvalue(),
                    file_name="encuestas_arcgis.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        # Mapa si hay puntos
        if "_lat" in df.columns and "_lon" in df.columns:
            st.subheader("🗺️ Mapa (puntos)")
            # Centro aproximado
            lat0 = df["_lat"].dropna().astype(float)
            lon0 = df["_lon"].dropna().astype(float)
            if not lat0.empty and not lon0.empty:
                center = (lat0.mean(), lon0.mean())
                m = folium.Map(location=center, zoom_start=11, control_scale=True)
                # Marcadores
                for _, r in df.iterrows():
                    if pd.notna(r.get("_lat")) and pd.notna(r.get("_lon")):
                        folium.CircleMarker(
                            location=(float(r["_lat"]), float(r["_lon"])),
                            radius=4,
                            fill=True
                        ).add_to(m)
                st_folium(m, height=480, use_container_width=True)
        else:
            st.info("La capa no es de puntos o no trae geometría; se muestra solo la tabla.")

    except Exception as e:
        st.error(f"Error: {e}")
        st.toast("Revisa si la capa es pública o si el usuario/contraseña/portal son correctos.", icon="⚠️")



