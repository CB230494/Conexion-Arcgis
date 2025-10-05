# -*- coding: utf-8 -*-
import io
from datetime import datetime
import numpy as np
import pandas as pd
import streamlit as st

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
st.set_page_config(page_title="Dashboard de Avances – Encuestas", layout="wide")

# ---- Título personalizable ----
BASE_TITLE = "Informe de Avance – Encuestas"
suffix = st.text_input("Añadir al título del informe (opcional)", placeholder="Ej.: Distrito Norte – Semana 40")
REPORT_TITLE = BASE_TITLE if not suffix.strip() else f"{BASE_TITLE} – {suffix.strip()}"
st.title(REPORT_TITLE)
st.caption("Dashboard con conteos, limpieza de duplicados, mapa y generación de PDF con evidencias.")

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
    return (df[lat_col].mean(), df[lon_col].mean())

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
st.sidebar.subheader("Evidencias para PDF (puedes subir varias PNG)")
evidence_png_files = st.sidebar.file_uploader("Sube 1 o más imágenes (PNG)", type=["png"], accept_multiple_files=True)

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
time_col = "CreationDate" if "CreationDate" in df.columns else "EditDate"
window_minutes = 10

content_cols = [c for c in df.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df, time_col, window_minutes, content_cols)

# ---- KPIs ----
total = len(df)
duplicadas = int(dupes["conteo_duplicados"].sum()) if not dupes.empty else 0
eliminar_por_grupo = int(sum(max(0, n-1) for n in dupes["conteo_duplicados"])) if not dupes.empty else 0
validadas = total - eliminar_por_grupo
ultima_fecha = df[time_col].max().strftime("%d/%m/%Y") if time_col in df.columns else "-"

c1, c2, c3, c4 = st.columns(4)
with c1: big_number("Respuestas totales", total)
with c2: big_number("Duplicadas detectadas", duplicadas)
with c3: big_number("Se eliminarán", eliminar_por_grupo)
with c4: big_number("Validadas", validadas)

# ---- Cuadro resumen ----
st.markdown(
    f"""
    <div style="border:1px solid rgba(255,255,255,0.2);background:rgba(255,255,255,0.05);
                border-radius:10px;padding:15px 18px;margin:8px 0;">
      <div style="font-weight:700;font-size:18px;margin-bottom:8px;">Resumen</div>
      <div style="font-size:15px;line-height:1.6;">
        <b>Respuestas totales:</b> {total}<br>
        <b>Duplicadas detectadas:</b> {duplicadas}<br>
        <b>Se eliminarán:</b> {eliminar_por_grupo}<br>
        <b>Quedarán validadas:</b> {validadas}<br>
        <b>Última respuesta:</b> {ultima_fecha}
      </div>
    </div>
    """, unsafe_allow_html=True
)

# ======== LIMPIEZA ========
if dupes.empty:
    st.success("✅ No se detectaron duplicados.")
else:
    with st.expander("🧹 Limpiar duplicados (mantener 1 por grupo)"):
        filas = []
        for i, row in dupes.iterrows():
            idxs = row["indices"]
            motivo, rango = reason_for_group(df, idxs, time_col, lon_col, lat_col, window_minutes)
            filas.append({
                "Grupo": f"Grupo {i+1}",
                "Respuestas": row["conteo_duplicados"],
                "Se eliminarán": max(0, row["conteo_duplicados"]-1),
                "Rango": rango, "Motivo": motivo
            })
        st.dataframe(pd.DataFrame(filas), use_container_width=True)
        criterio = st.radio("¿Cuál conservar?", ["Más reciente", "Más antiguo"], horizontal=True)

        def limpiar(df_in, dupes_df, criterio):
            df_out = df_in.copy()
            for _, r in dupes_df.iterrows():
                idxs = r["indices"]
                vivos = df_out.index.intersection(idxs)
                if len(vivos) <= 1: continue
                sub = df_out.loc[vivos]
                ts = pd.to_datetime(sub[time_col], errors="coerce").dropna()
                keep = ts.idxmax() if criterio == "Más reciente" else ts.idxmin()
                drop = [i for i in sub.index if i != keep]
                df_out = df_out.drop(index=drop)
            return df_out

        if st.button("🧹 Ejecutar limpieza ahora"):
            st.session_state.df_clean = limpiar(df, dupes, criterio)
            st.success("Limpieza realizada correctamente.")
            st.rerun()

# ======== MAPA ========
st.markdown("### 🗺️ Mapa (duplicadas en rojo)")

# puntos válidos
valid_points = df.copy()
for c in [lat_col, lon_col]:
    if c in valid_points.columns:
        valid_points[c] = pd.to_numeric(valid_points[c], errors="coerce")
valid_points = valid_points.dropna(subset=[lat_col, lon_col]).copy()

dupes_now = detect_duplicates(df, time_col, window_minutes, content_cols)
dup_set = {i for lst in dupes_now["indices"] for i in lst} if not dupes_now.empty else set()

center_lat, center_lon = center_from_points(valid_points, lon_col, lat_col)
m = folium.Map(location=[center_lat, center_lon], zoom_start=13, control_scale=True)
folium.TileLayer("OpenStreetMap").add_to(m)
folium.TileLayer("Stamen Terrain").add_to(m)
folium.TileLayer("CartoDB Positron").add_to(m)
folium.TileLayer("Esri WorldImagery").add_to(m)

if not valid_points.empty:
    mc = MarkerCluster().add_to(m)
    for idx, r in valid_points.iterrows():
        color = "red" if idx in dup_set else "blue"
        folium.Marker([r[lat_col], r[lon_col]],
                      icon=folium.Icon(color=color),
                      popup=f"ID {idx}").add_to(mc)
    if len(valid_points) > 1:
        HeatMap(valid_points[[lat_col, lon_col]].values.tolist(),
                name="Mapa de calor").add_to(m)

folium.LayerControl().add_to(m)
MeasureControl(primary_length_unit="meters", secondary_length_unit="kilometers").add_to(m)
st_folium(m, use_container_width=True, returned_objects=[])

# ======== PDF ========
def build_pdf(title, conteos, detalle_dupes, evidencias):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Body", parent=styles["BodyText"], leading=14))
    flow = []
    flow.append(Paragraph(title, styles["Heading1"]))
    flow.append(Paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["Body"]))
    flow.append(Spacer(1, 10))

    # --- Cuadro resumen ---
    rows = [[Paragraph(f"<b>{k}</b>", styles["Body"]), Paragraph(str(v), styles["Body"])] for k, v in conteos.items()]
    table = Table(rows, colWidths=[7*cm, None])
    table.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 0.8, colors.black),
        ("BACKGROUND", (0,0), (-1,-1), colors.whitesmoke)
    ]))
    flow.append(Paragraph("Resumen", styles["Heading2"]))
    flow.append(table)
    flow.append(Spacer(1, 12))

    # --- Detalle duplicados ---
    if detalle_dupes:
        flow.append(Paragraph("Detalle de respuestas duplicadas", styles["Heading2"]))
        for i, d in enumerate(detalle_dupes, 1):
            flow.append(Paragraph(f"<b>Grupo {i}</b>: {d['conteo']} respuestas, "
                                  f"se eliminarán {d['eliminar']} y quedará 1.", styles["Body"]))
            flow.append(Paragraph(f"Rango: {d['rango']}", styles["Body"]))
            flow.append(Paragraph(f"Motivo: {d['motivo']}", styles["Body"]))
            flow.append(Spacer(1, 6))
    else:
        flow.append(Paragraph("No se detectaron duplicados.", styles["Body"]))

    # --- Evidencias ---
    if evidencias:
        flow.append(PageBreak())
        flow.append(Paragraph("Evidencias visuales", styles["Heading2"]))
        max_w = A4[0] - 4*cm
        max_h = A4[1]/2.4
        for i, ev in enumerate(evidencias):
            img = ImageReader(io.BytesIO(ev))
            iw, ih = img.getSize()
            ratio = min(max_w/iw, max_h/ih)
            w, h = iw*ratio, ih*ratio
            flow.append(Image(io.BytesIO(ev), width=w, height=h))
            flow.append(Spacer(1, 8))
            if (i+1) % 2 == 0:
                flow.append(PageBreak())

    doc.build(flow)
    buffer.seek(0)
    return buffer.getvalue()

# --- Construir PDF ---
conteos = {
    "Respuestas totales": total,
    "Duplicadas detectadas": duplicadas,
    "Se eliminarán": eliminar_por_grupo,
    "Quedarán validadas": validadas,
    "Última respuesta": ultima_fecha
}
detalle_dupes = []
if not dupes_now.empty:
    for i, row in dupes_now.iterrows():
        motivo, rango = reason_for_group(df, row["indices"], time_col, lon_col, lat_col, window_minutes)
        detalle_dupes.append({
            "conteo": row["conteo_duplicados"],
            "eliminar": max(0, row["conteo_duplicados"]-1),
            "rango": rango,
            "motivo": motivo
        })

evidences_bytes = [f.read() for f in evidence_png_files] if evidence_png_files else []
pdf_bytes = build_pdf(REPORT_TITLE, conteos, detalle_dupes, evidences_bytes)

st.download_button(
    "⬇️ Descargar PDF de avance",
    data=pdf_bytes,
    file_name=f"informe_avance_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    mime="application/pdf"
)

st.markdown("### 📋 Vista previa de datos")
st.dataframe(df.head(100), use_container_width=True)
