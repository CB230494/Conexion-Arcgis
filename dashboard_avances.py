# -*- coding: utf-8 -*-
import io
from datetime import datetime
import numpy as np
import pandas as pd
import streamlit as st
import uuid

# Mapa
import folium
from folium.plugins import MarkerCluster, HeatMap, MeasureControl
from streamlit_folium import st_folium
from folium import Element

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# ===================== CONFIG =====================
st.set_page_config(page_title="Dashboard de Avance – Encuestas", layout="wide")

# ---- Título personalizable (también para el PDF) ----
BASE_TITLE = "Informe de Avance – Encuestas"
suffix = st.text_input("Añadir al título del informe (opcional)", placeholder="Ej.: Distrito Norte – Semana 40")
REPORT_TITLE = BASE_TITLE if not suffix.strip() else f"{BASE_TITLE} – {suffix.strip()}"
st.title(REPORT_TITLE)
st.caption("Dashboard con conteos, limpieza de duplicados, mapa (duplicadas en rojo) y PDF con evidencias.")

# ======== Funciones base ========
META_COLS = {"ObjectID", "GlobalID", "instance_id", "CreationDate", "EditDate", "Creator", "Editor"}

def normalize_string(x) -> str:
    if pd.isna(x): return ""
    s = str(x).strip().lower()
    return " ".join(s.split())

def normalize_factors(x) -> str:
    if pd.isna(x): return ""
    parts = str(x).replace(";", ",").split(",")
    parts = [normalize_string(p) for p in parts if normalize_string(p)]
    parts.sort()
    return ",".join(parts)

def detect_duplicates(df, time_col: str, window_minutes: int, content_cols: list):
    """Detecta grupos con mismo contenido dentro de una ventana temporal."""
    if df.empty or not content_cols: return pd.DataFrame()
    tmp = df.copy()
    normalized = {}
    for c in content_cols:
        normalized[c] = tmp[c].apply(normalize_factors) if "factor" in c.lower() else tmp[c].apply(normalize_string)
    norm_df = pd.DataFrame(normalized)
    key = pd.util.hash_pandas_object(norm_df, index=False)
    tmp["_hash_content"] = key
    tmp[time_col] = pd.to_datetime(tmp[time_col], errors="coerce")
    tmp["_row_i"] = tmp.index
    tmp = tmp.sort_values(time_col).reset_index(drop=True)

    win = pd.Timedelta(minutes=window_minutes)
    out = []
    for _, g in tmp.groupby("_hash_content", dropna=False):
        g = g.copy().sort_values(time_col)
        if len(g) < 2:
            continue
        g["time_diff_prev"] = g[time_col].diff()
        block_id = (g["time_diff_prev"].isna() | (g["time_diff_prev"] > win)).cumsum()
        for _, gb in g.groupby(block_id):
            if len(gb) >= 2:
                out.append({
                    "conteo_duplicados": len(gb),
                    "primero": gb[time_col].min(),
                    "ultimo": gb[time_col].max(),
                    "indices": gb["_row_i"].tolist()
                })
    dupes = pd.DataFrame(out)
    if dupes.empty:
        return dupes
    return dupes.sort_values(["conteo_duplicados","ultimo"], ascending=[False, False]).reset_index(drop=True)

def center_from_points(df, lon_col, lat_col):
    if df.empty: return (10.0, -84.0)
    return (float(df[lat_col].mean()), float(df[lon_col].mean()))

def big_number(label: str, value: str):
    st.markdown(
        f"""
        <div style="padding:12px;border-radius:12px;background:rgba(255,255,255,0.05);">
          <div style="font-size:44px;font-weight:800;">{value}</div>
          <div style="font-size:13px;opacity:0.75;">{label}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

def to_excel_download(df: pd.DataFrame, filename: str = "datos_limpios.xlsx", key: str = "dl_excel"):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="datos")
    bio.seek(0)
    st.download_button("⬇️ Descargar Excel limpio", data=bio, file_name=filename,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=key)

def reason_for_group(df, idxs, time_col, lon_col, lat_col, window_minutes):
    """Explicación del duplicado."""
    sub = df.loc[idxs].copy()
    same_place = False
    if lon_col in sub.columns and lat_col in sub.columns:
        coords = sub[[lon_col, lat_col]].round(6).dropna()
        same_place = (len(coords.drop_duplicates()) == 1)
    rango = ""
    try:
        primero = pd.to_datetime(sub[time_col], errors="coerce").min()
        ultimo = pd.to_datetime(sub[time_col], errors="coerce").max()
        rango = f"{primero.strftime('%d/%m/%Y %H:%M')} → {ultimo.strftime('%d/%m/%Y %H:%M')}"
    except Exception:
        pass
    motivo = (f"Se detectó el mismo contenido en un lapso ≤ {window_minutes} minutos"
              + (", en la misma ubicación" if same_place else "")
              + ". Se conservará 1 y se eliminarán las demás.")
    return motivo, rango

# ======== Sidebar ========
st.sidebar.header("Cargar Excel")
uploaded = st.sidebar.file_uploader("Sube un archivo .xlsx", type=["xlsx"])

st.sidebar.markdown("---")
st.sidebar.subheader("Evidencias para PDF")
map_dup_png = st.sidebar.file_uploader("🟥 Mapa con duplicadas (PNG)", type=["png"])
map_final_png = st.sidebar.file_uploader("✅ Mapa final validado (PNG)", type=["png"])

# ======== Cargar datos ========
@st.cache_data(show_spinner=False)
def load_excel_first_sheet(file_like):
    data = file_like.read()
    bio = io.BytesIO(data)
    xls = pd.ExcelFile(bio, engine="openpyxl")
    first_sheet = xls.sheet_names[0]
    return pd.read_excel(xls, sheet_name=first_sheet), first_sheet

if not uploaded:
    st.info("Sube un Excel (.xlsx) para comenzar.")
    st.stop()

df_raw, sheet_name = load_excel_first_sheet(uploaded)

if "df_clean" not in st.session_state:
    st.session_state.df_clean = df_raw.copy()

df = st.session_state.df_clean

# Campos
for c in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
    if c in df.columns:
        df[c] = pd.to_datetime(df[c], errors="coerce")

lon_col, lat_col = "x", "y"
time_col = "CreationDate" if "CreationDate" in df.columns else ("EditDate" if "EditDate" in df.columns else "¿Cuándo fue el último incidente?")
window_minutes = 10

content_cols = [c for c in df.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df, time_col, window_minutes, content_cols)

# ---- KPIs ----
total = len(df)
duplicadas = int(dupes["conteo_duplicados"].sum()) if not dupes.empty else 0
eliminar_por_grupo = int(sum(max(0, n-1) for n in (dupes["conteo_duplicados"] if not dupes.empty else [])))
validadas = total - eliminar_por_grupo
ultima_fecha = "-" if time_col not in df.columns or pd.isna(df[time_col].max()) else pd.to_datetime(df[time_col].max()).strftime("%d/%m/%Y")

c1, c2, c3, c4 = st.columns(4)
with c1: big_number("Respuestas totales", total)
with c2: big_number("Duplicadas detectadas", duplicadas)
with c3: big_number("Se eliminarán (1 por grupo)", eliminar_por_grupo)
with c4: big_number("Quedarán validadas", validadas)

# ---- Cuadro resumen en la app ----
st.markdown(
    f"""
    <div style="border:1px solid rgba(255,255,255,0.2);background:rgba(255,255,255,0.05);
                border-radius:10px;padding:15px 18px;margin:8px 0;">
      <div style="font-weight:700;font-size:18px;margin-bottom:8px;">Resumen</div>
      <div style="font-size:15px;line-height:1.6;">
        <b>Respuestas totales:</b> {total}<br>
        <b>Duplicadas detectadas:</b> {duplicadas}<br>
        <b>Se eliminarán (1 por grupo):</b> {eliminar_por_grupo}<br>
        <b>Quedarán validadas:</b> {validadas}<br>
        <b>Última respuesta:</b> {ultima_fecha}
      </div>
    </div>
    """, unsafe_allow_html=True
)

st.info(
    f"Un **duplicado** es un grupo de respuestas con **exactamente la misma información** "
    f"registradas en un lapso **≤ {window_minutes} minutos**. En la limpieza, de cada grupo se "
    f"**mantiene 1** y se **eliminan** las demás; por eso, de {total} pasarían a **{validadas}** respuestas validadas."
)

# ======== LIMPIEZA ========
if dupes.empty:
    st.success("✅ No se detectaron duplicados.")
else:
    with st.expander("🧹 Limpiar duplicados (mantener 1 por grupo)"):
        filas = []
        explicaciones = []
        for i, row in dupes.iterrows():
            idxs = row["indices"]
            motivo, rango = reason_for_group(df, idxs, time_col, lon_col, lat_col, window_minutes)
            filas.append({
                "Grupo": f"Grupo {i+1}",
                "Respuestas": int(row["conteo_duplicados"]),
                "Se eliminarán": max(0, int(row["conteo_duplicados"]) - 1),
                "Rango": rango or "-",
                "Motivo": motivo
            })
            explicaciones.append(
                f"**Grupo {i+1}**: {int(row['conteo_duplicados'])} respuestas → "
                f"se eliminarán {max(0, int(row['conteo_duplicados'])-1)} y **se conservará 1**. "
                f"Rango: {rango or '-'} · Motivo: {motivo}"
            )
        st.dataframe(pd.DataFrame(filas), use_container_width=True)
        st.markdown("—")
        for txt in explicaciones:
            st.markdown(f"- {txt}")

        criterio = st.radio("¿Cuál conservar?", ["Más reciente", "Más antiguo"], horizontal=True)

        def limpiar(df_in, dupes_df, criterio_txt):
            df_out = df_in.copy()
            for _, r in dupes_df.iterrows():
                idxs = r["indices"]
                vivos = df_out.index.intersection(idxs)
                if len(vivos) <= 1: 
                    continue
                sub = df_out.loc[vivos]
                ts = pd.to_datetime(sub[time_col], errors="coerce").dropna()
                if not ts.empty:
                    keep = ts.idxmax() if criterio_txt.startswith("Más reciente") else ts.idxmin()
                else:
                    keep = sub.index[0]
                drop_ids = [i for i in sub.index if i != keep]
                df_out = df_out.drop(index=drop_ids)
            return df_out

        if st.button("🧹 Ejecutar limpieza ahora"):
            st.session_state.df_clean = limpiar(df, dupes, criterio)
            st.success("Limpieza realizada correctamente.")
            st.rerun()

        to_excel_download(df, filename="datos_limpios.xlsx", key="dl_excel_limpio")

# ======== Mapa ========
st.markdown("### 🗺️ Mapa (duplicadas en rojo)")

# Puntos válidos
valid_points = df.copy()
for c in [lat_col, lon_col]:
    if c in valid_points.columns:
        valid_points[c] = pd.to_numeric(valid_points[c], errors="coerce")
valid_points = valid_points.dropna(subset=[lat_col, lon_col]).copy()

dupes_now = detect_duplicates(df, time_col, window_minutes, content_cols)
dup_set = {i for lst in dupes_now["indices"] for i in lst} if not dupes_now.empty else set()

center_lat, center_lon = center_from_points(valid_points, lon_col, lat_col)
m = folium.Map(location=[center_lat, center_lon], zoom_start=13, control_scale=True)

# Capas base
folium.TileLayer(
    tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    name="OpenStreetMap",
    attr="© OpenStreetMap contributors"
).add_to(m)

folium.TileLayer(
    tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    name="CartoDB Positron (gris)",
    attr="© OpenStreetMap contributors © CARTO"
).add_to(m)

folium.TileLayer(
    tiles="https://stamen-tiles.a.ssl.fastly.net/terrain/{z}/{x}/{y}.png",
    name="Stamen Terrain (relieve)",
    attr="Map tiles by Stamen Design, CC BY 3.0 — Map data © OpenStreetMap"
).add_to(m)

folium.TileLayer(
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    name="Esri WorldImagery (satelital)",
    attr="Tiles © Esri — Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community"
).add_to(m)

# Marcadores
if not valid_points.empty:
    mc = MarkerCluster(name="Clúster de puntos").add_to(m)
    for idx, r in valid_points.iterrows():
        color = "red" if idx in dup_set else "blue"
        popup_items = []
        for c in df.columns:
            val = r[c]
            if pd.isna(val): 
                continue
            if isinstance(val, (pd.Timestamp, np.datetime64)):
                try: val = pd.to_datetime(val).strftime("%d/%m/%Y %H:%M")
                except: pass
            popup_items.append(f"<b>{c}:</b> {val}")
        folium.Marker(
            [r[lat_col], r[lon_col]],
            icon=folium.Icon(color=color, icon="info-sign"),
            popup=folium.Popup("<br>".join(popup_items), max_width=420)
        ).add_to(mc)
    if len(valid_points) > 1:
        HeatMap(valid_points[[lat_col, lon_col]].values.tolist(), name="Mapa de calor",
                radius=20, blur=25,
                gradient={0.2:"#ffffb2",0.4:"#fecc5c",0.6:"#fd8d3c",0.8:"#f03b20",1.0:"#bd0026"}).add_to(m)

folium.LayerControl(collapsed=False).add_to(m)
MeasureControl(position="topright", primary_length_unit="meters",
               secondary_length_unit="kilometers",
               primary_area_unit="sqmeters", secondary_area_unit="hectares").add_to(m)

# Traducir popup de medición
script_trad = """
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
m.get_root().html.add_child(Element(f"<script>{script_trad}</script>"))
st_folium(m, use_container_width=True, returned_objects=[])

# Exportar mapa HTML
st.markdown("**Exportar mapa para evidencias**")
map_html_bytes = m.get_root().render().encode("utf-8")
st.download_button("⬇️ Descargar mapa (HTML)", data=map_html_bytes,
                   file_name="mapa_encuestas.html", mime="text/html", key="dl_html_mapa")

# ======== Preparar detalle para PDF ========
detalle_dupes_list = []
if not dupes_now.empty:
    for _, row in dupes_now.iterrows():
        motivo, rango = reason_for_group(df, row["indices"], time_col, lon_col, lat_col, window_minutes)
        detalle_dupes_list.append({
            "conteo": int(row["conteo_duplicados"]),
            "eliminar": max(0, int(row["conteo_duplicados"]) - 1),
            "rango": rango,
            "motivo": motivo
        })

# ======== PDF (cuadro resumen + evidencias) ========
def build_pdf(title, conteos, detalle_dupes, dup_png_bytes, final_png_bytes):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Body", parent=styles["BodyText"], leading=14))

    flow = []
    flow.append(Paragraph(title, styles["Heading1"])))
    # ^^^ pequeña corrección de paréntesis más abajo (línea correcta):
    flow = []
    flow.append(Paragraph(title, styles["Heading1"]))
    flow.append(Paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["Body"]))
    flow.append(Spacer(1, 10))

    # Cuadro resumen
    rows = [[Paragraph(f"<b>{k}</b>", styles["Body"]), Paragraph(str(v), styles["Body"])] for k, v in conteos.items()]
    table = Table(rows, colWidths=[7.5*cm, None])
    table.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 0.8, colors.black),
        ("INNERGRID", (0,0), (-1,-1), 0.3, colors.grey),
        ("BACKGROUND", (0,0), (-1,-1), colors.whitesmoke),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    flow.append(Paragraph("Resumen", styles["Heading2"]))
    flow.append(table)
    flow.append(Spacer(1, 12))

    # Detalle duplicados
    if detalle_dupes:
        flow.append(Paragraph("Detalle de respuestas duplicadas", styles["Heading2"]))
        for i, d in enumerate(detalle_dupes, 1):
            flow.append(Paragraph(f"<b>Grupo {i}</b>: {d['conteo']} respuestas "
                                  f"(se eliminarán {d['eliminar']}, quedará 1).", styles["Body"]))
            flow.append(Paragraph(f"Rango temporal: {d['rango'] or '-'}", styles["Body"]))
            flow.append(Paragraph(f"Motivo: {d['motivo']}", styles["Body"]))
            flow.append(Spacer(1, 6))
    else:
        flow.append(Paragraph("No se detectaron respuestas duplicadas. Todas están validadas.", styles["Body"]))

    # Evidencias
    if dup_png_bytes or final_png_bytes:
        flow.append(PageBreak())
        flow.append(Paragraph("Evidencias visuales", styles["Heading2"]))
        flow.append(Spacer(1, 6))

        max_w = A4[0] - 4*cm
        max_h = A4[1] / 2.0

        def add_scaled_img(png_bytes, caption):
            imgR = ImageReader(io.BytesIO(png_bytes))
            iw, ih = imgR.getSize()
            ratio = min(max_w/iw, max_h/ih)
            w, h = iw*ratio, ih*ratio
            flow.append(Image(io.BytesIO(png_bytes), width=w, height=h))
            flow.append(Spacer(1, 6))
            flow.append(Paragraph(caption, styles["Body"]))

        if dup_png_bytes:
            add_scaled_img(dup_png_bytes, "Mapa con duplicadas (marcadas en rojo).")

        if final_png_bytes:
            flow.append(PageBreak())
            add_scaled_img(final_png_bytes, "Mapa final (respuestas validadas).")

    doc.build(flow)
    buffer.seek(0)
    return buffer.getvalue()

# --- Construir PDF y botón de descarga ---
conteos = {
    "Respuestas totales": total,
    "Duplicadas detectadas": duplicadas,
    "Se eliminarán (1 por grupo)": eliminar_por_grupo,
    "Quedarán validadas": validadas,
    "Última respuesta": ultima_fecha
}
dup_png_bytes = map_dup_png.read() if map_dup_png else None
final_png_bytes = map_final_png.read() if map_final_png else None
pdf_bytes = build_pdf(REPORT_TITLE, conteos, detalle_dupes_list, dup_png_bytes, final_png_bytes)

st.download_button(
    "⬇️ Descargar PDF de avance",
    data=pdf_bytes,
    file_name=f"informe_avance_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    mime="application/pdf"
)

# Tabla
st.markdown("### 📋 Vista previa de datos")
st.dataframe(df.head(1000), use_container_width=True)

# ===================== ENCUESTA CON CONDICIONALES (DEMO) =====================
st.markdown("---")
st.markdown("## 🧩 Encuesta con condicionales (demo)")

# Mapeos de Canton → Distritos (puedes ampliar libremente)
CANTON_DISTRICTS = {
    "Alajuela (Central)": [
        "Alajuela", "San José", "Carrizal", "San Antonio", "Guácima",
        "San Isidro", "Sabanilla", "San Rafael", "Río Segundo",
        "Desamparados", "Turrúcares", "Tambor", "Garita", "Sarapiquí"
    ],
    "Sabanilla": [  # si te refieres a distrito/centro poblado independiente
        "Centro", "Este", "Oeste", "Norte", "Sur"
    ],
    "Desamparados": [
        "Desamparados", "San Miguel", "San Juan de Dios", "San Rafael Arriba",
        "San Antonio", "Frailes", "Patarrá", "San Cristóbal",
        "Rosario", "Damas", "San Rafael Abajo", "Gravilias", "Los Guido"
    ],
}

INCIDENTES = [
    "Robo", "Violencia intrafamiliar", "Venta de drogas",
    "Disturbios", "Sin novedad", "Otro"
]

# Estado de respuestas almacenadas en sesión
if "survey_responses" not in st.session_state:
    st.session_state.survey_responses = []

with st.form("form_encuesta_condicional"):
    st.markdown("#### 1) Ubicación")
    canton = st.selectbox("Cantón", list(CANTON_DISTRICTS.keys()))
    distrito = None
    if canton:
        distrito = st.selectbox("Distrito", CANTON_DISTRICTS.get(canton, []))

    st.markdown("#### 2) Incidente")
    tiene_incidente = st.radio("¿El reporte tiene incidente?", ["Sí", "No"], horizontal=True)

    finalizar_ya = False
    tipo_incidente = None
    incidente_otro = ""
    requiere_seguimiento = None
    observ = ""

    # Si no hay incidente, ofrecemos finalizar en ese punto
    if tiene_incidente == "No":
        st.info("Si no hay incidente, puedes finalizar y enviar ahora.")
        finalizar_ya = st.checkbox("Finalizar y enviar ahora")

    else:
        # Sí hay incidente → mostramos más campos
        tipo_incidente = st.selectbox("Tipo de incidente", INCIDENTES)
        if tipo_incidente == "Otro":
            incidente_otro = st.text_input("Especifica el incidente")

        if tipo_incidente == "Sin novedad":
            st.info("Seleccionaste *Sin novedad*. Puedes finalizar y enviar ahora.")
            finalizar_ya = st.checkbox("Finalizar y enviar ahora")

        st.markdown("#### 3) Seguimiento")
        requiere_seguimiento = st.radio("¿Requiere seguimiento?", ["Sí", "No"], horizontal=True)
        if requiere_seguimiento == "No":
            st.info("No requiere seguimiento. Puedes finalizar y enviar en este punto si lo deseas.")
            finalizar_ya = st.checkbox("Finalizar y enviar ahora", value=finalizar_ya)

        st.markdown("#### 4) Observaciones")
        observ = st.text_area("Observaciones (opcional)", placeholder="Notas breves...")

    enviado = st.form_submit_button("Enviar respuesta")

if enviado:
    # Construimos el registro respetando lo visible/oculto
    registro = {
        "id": str(uuid.uuid4())[:8],
        "fecha_envio": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "canton": canton,
        "distrito": distrito,
        "tiene_incidente": tiene_incidente,
        "tipo_incidente": tipo_incidente if tiene_incidente == "Sí" else None,
        "incidente_otro": incidente_otro if (tiene_incidente == "Sí" and tipo_incidente == "Otro") else None,
        "requiere_seguimiento": requiere_seguimiento if tiene_incidente == "Sí" else None,
        "observaciones": observ if tiene_incidente == "Sí" else None,
        "finalizado_temprano": finalizar_ya or (tiene_incidente == "No") or (tipo_incidente == "Sin novedad" if tipo_incidente else False),
    }
    st.session_state.survey_responses.append(registro)
    st.success("✅ Respuesta registrada (demo).")

# Mostrar y descargar respuestas capturadas (demo)
if st.session_state.survey_responses:
    st.markdown("#### Respuestas capturadas (demo)")
    demo_df = pd.DataFrame(st.session_state.survey_responses)
    st.dataframe(demo_df, use_container_width=True)

    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        demo_df.to_excel(writer, index=False, sheet_name="respuestas_encuesta")
    bio.seek(0)
    st.download_button("⬇️ Descargar respuestas de encuesta (Excel)", data=bio,
                       file_name="respuestas_condicionales_demo.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


