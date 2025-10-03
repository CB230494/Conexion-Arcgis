import os, math, hashlib, time
import pandas as pd
import streamlit as st
import pydeck as pdk
from streamlit_folium import st_folium
import folium

from arcgis.gis import GIS
from arcgis.features import FeatureLayer

# =========================
# Config y conexión
# =========================
st.set_page_config(page_title="Dashboard Encuesta Seguridad", layout="wide")

ORG_URL   = st.secrets["agol"]["org_url"]
USER      = st.secrets["agol"]["username"]
PWD       = st.secrets["agol"]["password"]
ITEM_ID   = st.secrets["agol"]["item_id"]
LAYER_IDX = int(st.secrets["agol"]["layer_index"])

@st.cache_resource(show_spinner=False)
def get_gis():
    return GIS(ORG_URL, USER, PWD)

gis = get_gis()
item = gis.content.get(ITEM_ID)
layer: FeatureLayer = item.layers[LAYER_IDX]

st.sidebar.success(f"Conectado a: {item.title}")
st.sidebar.caption(f"Usuario: {gis.users.me.username}")

# =========================
# Utilidades
# =========================
def norm(v):
    if v is None: return ""
    if isinstance(v, float) and math.isnan(v): return ""
    return str(v).strip().upper()

def day_from_ms(ms):
    if ms in (None, ""): return ""
    try:
        return pd.to_datetime(int(ms), unit="ms").strftime("%Y%m%d")
    except Exception:
        return ""

def build_key(row, campos_clave, usar_dia=True, x=None, y=None, round_coords=5):
    partes = [norm(row.get(c)) for c in campos_clave if c in row]
    if usar_dia and "CreationDate" in row:
        partes.append(day_from_ms(row.get("CreationDate")))
    if x is not None and y is not None:
        partes += [str(round(x, round_coords)), str(round(y, round_coords))]
    raw = "|".join(partes)
    return hashlib.md5(raw.encode("utf-8")).hexdigest() if raw else ""

@st.cache_data(show_spinner=True, ttl=60)
def load_df():
    fs = layer.query(out_sr=4326)
    sdf = fs.sdf  # Spatially enabled DF (arcgis)
    # lat/lon desde geometría
    if "SHAPE" in sdf.columns:
        sdf["lon"] = sdf["SHAPE"].apply(lambda g: g.x if g is not None else None)
        sdf["lat"] = sdf["SHAPE"].apply(lambda g: g.y if g is not None else None)
    df = pd.DataFrame(sdf.drop(columns=["SHAPE"], errors="ignore"))
    return df

def detect_oid_col(df):
    candidates = [getattr(layer.properties, "objectIdField", None), "OBJECTID","ObjectID","objectid","FID","fid"]
    for c in candidates:
        if c and c in df.columns:
            return c
    raise RuntimeError("No se encontró la columna OID en la capa.")

# =========================
# Parámetros de duplicado
# =========================
st.sidebar.header("Duplicados")
campos_default = ["seguridad_general","tipo_incidente","factores","frecuencia","contacto"]
campos_clave = st.sidebar.multiselect("Campos clave", options=campos_default + [c for c in campos_default if c not in campos_default], default=campos_default)
usar_dia = st.sidebar.toggle("Usar día (CreationDate) en la clave", value=True)
redondeo = st.sidebar.slider("Redondeo coords (decimales)", 0, 7, 5)

# =========================
# Cargar datos
# =========================
df = load_df()
OID_COL = detect_oid_col(df)
total = len(df)

# Calcular clave si no existe dup_key
if "dup_key" not in df.columns or df["dup_key"].isna().all():
    df["_dup_key_calc"] = df.apply(lambda r: build_key(r, campos_clave, usar_dia, r.get("lon"), r.get("lat"), redondeo), axis=1)
    dup_key_col = "_dup_key_calc"
else:
    dup_key_col = "dup_key"

# Flag duplicados
grp = df.groupby(dup_key_col)[OID_COL].transform("count")
df["dup_is_dup_calc"] = ((df[dup_key_col]!="") & (grp>1)).astype(int)
df["dup_group_calc"] = df[dup_key_col].fillna("").str[:8]

# KPIs
col1, col2, col3 = st.columns(3)
col1.metric("Total respuestas", f"{total}")
col2.metric("Duplicadas", f"{int(df['dup_is_dup_calc'].sum())}")
col3.metric("Válidas", f"{int((1-df['dup_is_dup_calc']).sum())}")

# =========================
# Mapas
# =========================
st.subheader("Mapa")
tab_map1, tab_map2 = st.tabs(["Heatmap (pydeck)", "Mapa base (folium)"])

with tab_map1:
    # Heatmap con pydeck
    df_map = df.dropna(subset=["lat","lon"])
    if not df_map.empty:
        layer_heat = pdk.Layer(
            "HeatmapLayer",
            data=df_map[["lat","lon"]],
            get_position='[lon, lat]',
            aggregation='"MEAN"',
            opacity=0.9
        )
        r = pdk.Deck(
            initial_view_state=pdk.ViewState(latitude=float(df_map["lat"].mean()), longitude=float(df_map["lon"].mean()), zoom=7),
            layers=[layer_heat],
            tooltip={"text": "Heatmap de respuestas"}
        )
        st.pydeck_chart(r)
    else:
        st.info("No hay coordenadas para mostrar.")

with tab_map2:
    # Puntos + popup
    df_pts = df.dropna(subset=["lat","lon"]).copy()
    if not df_pts.empty:
        m = folium.Map(location=[df_pts["lat"].mean(), df_pts["lon"].mean()], zoom_start=7, tiles="OpenStreetMap")
        for _, r in df_pts.iterrows():
            popup = folium.Popup(f"OID: {r[OID_COL]}<br>Seguridad: {r.get('seguridad_general','')}<br>Tipo: {r.get('tipo_incidente','')}", max_width=250)
            folium.CircleMarker([r["lat"], r["lon"]], radius=4, fill=True, popup=popup).add_to(m)
        st_folium(m, height=450, use_container_width=True)
    else:
        st.info("No hay coordenadas para mostrar.")

# =========================
# Tabla editable / limpieza
# =========================
st.subheader("Tabla")
mostrar_cols = [OID_COL, "CreationDate", "Creator", "seguridad_general","tipo_incidente","factores","frecuencia","contacto", dup_key_col, "dup_is_dup_calc","dup_group_calc"]
mostrar_cols = [c for c in mostrar_cols if c in df.columns]
edit_cols = [c for c in ["seguridad_general","tipo_incidente","factores","frecuencia","contacto"] if c in df.columns]

edited = st.data_editor(
    df[mostrar_cols],
    num_rows="dynamic",
    use_container_width=True,
    disabled=[c for c in mostrar_cols if c not in edit_cols],  # solo editables esos campos
    key="editor"
)

st.caption("Consejo: filtra por 'dup_is_dup_calc == 1' para revisar duplicados.")

# =========================
# Acciones: Guardar ediciones / eliminar
# =========================
st.divider()
colA, colB = st.columns([1,1])

with colA:
    st.markdown("### Guardar ediciones")
    if st.button("Aplicar cambios seleccionados a la capa", type="primary"):
        try:
            # comparar con original
            merged = edited.merge(df[[OID_COL]+edit_cols], on=OID_COL, suffixes=("_new","_old"))
            updates = []
            for _, r in merged.iterrows():
                attrs = {"attributes": {OID_COL: int(r[OID_COL])}}
                changed = False
                for c in edit_cols:
                    new = r.get(f"{c}_new")
                    old = r.get(f"{c}_old")
                    if new != old:
                        attrs["attributes"][c] = new
                        changed = True
                if changed:
                    updates.append(attrs)
            if updates:
                res = layer.edit_features(updates=updates)
                st.success(f"Actualizados {len(updates)} registros.")
                st.cache_data.clear()  # refrescar cache
            else:
                st.info("No hay cambios para aplicar.")
        except Exception as e:
            st.error(f"Error al actualizar: {e}")

with colB:
    st.markdown("### Eliminar duplicados")
    st.caption("Ingresa una lista de OIDs a eliminar (separados por coma). Revisa antes de ejecutar.")
    oids_text = st.text_input("OBJECTIDs a eliminar", value="")
    if st.button("Eliminar OIDs indicados", type="secondary"):
        try:
            oids = [int(x.strip()) for x in oids_text.split(",") if x.strip().isdigit()]
            if not oids:
                st.warning("No ingresaste OIDs válidos.")
            else:
                where = f"{OID_COL} IN ({','.join(map(str, oids))})"
                layer.delete_features(where=where)
                st.success(f"Eliminados {len(oids)} registros.")
                st.cache_data.clear()
        except Exception as e:
            st.error(f"Error al eliminar: {e}")

# =========================
# Exportaciones
# =========================
st.divider()
st.markdown("### Exportar duplicados a Excel")
dups = df[df["dup_is_dup_calc"] == 1].copy()
if not dups.empty:
    xls = dups.to_excel(index=False, sheet_name="duplicados")
    st.download_button("Descargar duplicados.xlsx", data=dups.to_excel(index=False), file_name="duplicados.xlsx")
else:
    st.info("No hay duplicados detectados con la lógica actual.")

st.caption("© Tu organización – Usa con permisos de edición. Acciones se aplican directo a la capa.")




