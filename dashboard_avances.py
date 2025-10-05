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

# PDF (Platypus: envuelve texto largo)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# ===================== CONFIG =====================
st.set_page_config(page_title="Dashboard de Avances – Encuestas", layout="wide")

# ---- Título personalizable ----
BASE_TITLE = "Informe de Avance – Encuestas"
suffix = st.text_input("Añadir al título del informe (opcional)", placeholder="Ej.: Distrito Norte – Semana 40")
REPORT_TITLE = BASE_TITLE if not suffix.strip() else f"{BASE_TITLE} – {suffix.strip()}"
st.title(REPORT_TITLE)
st.caption("Conteos grandes, limpieza de duplicados, mapa (duplicadas en rojo) y PDF con detalles y evidencias.")

# ======== Utilidades ========
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
    """Identifica grupos con MISMO contenido exacto normalizado en ventana <= window_minutes."""
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
    if df.empty or lon_col not in df.columns or lat_col not in df.columns:
        return (10.0, -84.0)
    mlat = df[lat_col].mean(skipna=True); mlon = df[lon_col].mean(skipna=True)
    if np.isnan(mlat) or np.isnan(mlon): return (10.0, -84.0)
    return (float(mlat), float(mlon))

def big_number(label: str, value: str):
    st.markdown(
        f"""
        <div style="padding:12px 14px;border-radius:12px;background:rgba(255,255,255,0.05);">
          <div style="font-size:46px;font-weight:800;line-height:1;margin-bottom:2px;">{value}</div>
          <div style="font-size:13px;opacity:0.75;margin-top:-2px;">{label}</div>
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
    """Texto explicativo del porqué del duplicado para un grupo."""
    sub = df.loc[idxs].copy()
    same_place = False
    if lon_col in sub.columns and lat_col in sub.columns:
        coords = sub[[lon_col, lat_col]].round(6).dropna()
        same_place = (len(coords.drop_duplicates()) == 1) and (len(coords) == len(sub))
    rango = ""
    try:
        primero = pd.to_datetime(sub[time_col], errors="coerce").min()
        ultimo = pd.to_datetime(sub[time_col], errors="coerce").max()
        if pd.notna(primero) and pd.notna(ultimo):
            rango = f"{primero.strftime('%d/%m/%Y %H:%M')} → {ultimo.strftime('%d/%m/%Y %H:%M')}"
    except Exception:
        pass
    motivo = (f"Se detectó el mismo contenido en un lapso ≤ {window_minutes} minutos"
              + (", en la misma ubicación" if same_place else "")
              + ". En la limpieza se conservará 1 y se eliminarán las demás.")
    return motivo, rango

# ======== Sidebar ========
st.sidebar.header("Cargar Excel")
uploaded = st.sidebar.file_uploader("Sube un archivo .xlsx", type=["xlsx"])

st.sidebar.markdown("---")
st.sidebar.subheader("Evidencias para PDF (opcional)")
map_dup_png = st.sidebar.file_uploader("Mapa con duplicadas (PNG)", type=["png"])
map_final_png = st.sidebar.file_uploader("Mapa final validado (PNG)", type=["png"])

# ======== Carga de datos ========
@st.cache_data(show_spinner=False)
def load_excel_first_sheet(file_like):
    if hasattr(file_like, "read"):
        data = file_like.read(); bio = io.BytesIO(data)
    else:
        bio = file_like
    xls = pd.ExcelFile(bio, engine="openpyxl")
    first_sheet = xls.sheet_names[0]
    return pd.read_excel(xls, sheet_name=first_sheet), first_sheet

if not uploaded:
    st.info("Sube un Excel (.xlsx) en la barra lateral para comenzar."); st.stop()

df_raw, sheet_name = load_excel_first_sheet(uploaded)

# Mantener un DF que podamos limpiar
if "df_clean" not in st.session_state:
    st.session_state.df_clean = df_raw.copy()

df = st.session_state.df_clean  # trabajaremos sobre df_clean

# Fechas & columnas clave
for c in ["CreationDate", "EditDate", "¿Cuándo fue el último incidente?"]:
    if c in df.columns: df[c] = pd.to_datetime(df[c], errors="coerce")

lon_col, lat_col = "x", "y"
time_col = "CreationDate" if "CreationDate" in df.columns else ("EditDate" if "EditDate" in df.columns else "¿Cuándo fue el último incidente?")
window_minutes = 10

# Duplicados (sobre df_clean actual)
content_cols = [c for c in df.columns if c not in META_COLS | {lon_col, lat_col}]
dupes = detect_duplicates(df, time_col=time_col, window_minutes=window_minutes, content_cols=content_cols)

# KPIs
total = len(df)
duplicadas = int(dupes["conteo_duplicados"].sum()) if not dupes.empty else 0
eliminar_por_grupo = int(sum(max(0, n-1) for n in (dupes["conteo_duplicados"] if not dupes.empty else [])))
validadas = total - eliminar_por_grupo

c1, c2, c3, c4 = st.columns([1.1, 1, 1, 1])
with c1: big_number("Respuestas totales", f"{total}")
with c2: big_number("Duplicadas detectadas", f"{duplicadas}")
with c3: big_number("Se eliminarán (1 por grupo)", f"{eliminar_por_grupo}")
with c4: big_number("Quedarán validadas", f"{validadas}")

st.info(
    f"Un **duplicado** es un grupo de respuestas con **exactamente la misma información** "
    f"registradas en un lapso **≤ {window_minutes} minutos**. En la limpieza, de cada grupo se "
    f"**mantiene 1** y se **eliminan** las demás; por eso, de {total} pasarían a **{validadas}** respuestas validadas."
)

# ======== Limpieza de duplicados ========
if dupes.empty:
    st.success("✅ No se detectaron duplicados. No hay nada que limpiar.")
else:
    with st.expander("🧹 Limpiar duplicados (mantener 1 por grupo)"):
        # ---- MOSTRAR BONITO (Streamlit) ----
        filas = []
        explicaciones = []
        for i, row in dupes.iterrows():
            idxs = row["indices"]
            motivo, rango = reason_for_group(df, idxs, time_col, lon_col, lat_col, window_minutes)
            filas.append({
                "Grupo": f"Grupo {i+1}",
                "Respuestas": int(row["conteo_duplicados"]),
                "Se eliminarán": max(0, int(row["conteo_duplicados"])-1),
                "Rango": rango or "-",
                "Motivo": motivo
            })
            explicaciones.append(
                f"**Grupo {i+1}**: {int(row['conteo_duplicados'])} respuestas → "
                f"se eliminarán {max(0, int(row['conteo_duplicados'])-1)} y **se conservará 1**. "
                f"Rango: {rango or '-'} · Motivo: {motivo}"
            )
        df_vista = pd.DataFrame(filas)
        st.dataframe(df_vista, use_container_width=True)
        st.markdown("—")
        for txt in explicaciones:
            st.markdown(f"- {txt}")

        # ---- Controles de limpieza ----
        criterio = st.radio("¿Cuál conservar en cada grupo?",
                            ["Mantener el más reciente", "Mantener el más antiguo"], horizontal=True)

        def limpiar(df_in: pd.DataFrame, dupes_df: pd.DataFrame, criterio_txt: str):
            df_out = df_in.copy()
            for _, r in dupes_df.iterrows():
                idxs = r["indices"]
                vivos = df_out.index.intersection(idxs)
                if len(vivos) <= 1:
                    continue
                sub = df_out.loc[vivos]
                if time_col in df_out.columns:
                    ts = pd.to_datetime(sub[time_col], errors="coerce").dropna()
                    if not ts.empty:
                        keep = ts.idxmax() if criterio_txt.startswith("Mantener el más reciente") else ts.idxmin()
                    else:
                        keep = sub.index[0]
                else:
                    keep = sub.index[0]
                drop_ids = [i for i in sub.index if i != keep]
                df_out = df_out.drop(index=drop_ids, errors="ignore")
            return df_out

        cbtn1, cbtn2 = st.columns([1,1])
        with cbtn1:
            if st.button("🧹 Ejecutar limpieza ahora"):
                st.session_state.df_clean = limpiar(st.session_state.df_clean, dupes, criterio)
                st.success("Limpieza realizada. KPIs, mapa y tabla se han actualizado.")
                st.rerun()
        with cbtn2:
            to_excel_download(df, filename="datos_limpios.xlsx", key="dl_excel_limpio")

# ======== Mapa (duplicadas en ROJO) ========
st.markdown("### 🗺️ Mapa (duplicadas en rojo)")

# recomputar dup_set contra df actual (pos-limpieza)
dupes_now = detect_duplicates(df, time_col=time_col, window_minutes=window_minutes, content_cols=content_cols)
dup_set = set()
if not dupes_now.empty:
    for lst in dupes_now["indices"]:
        dup_set.update(lst)

valid_points = df.dropna(subset=[lat_col, lon_col]).copy()
for c in [lat_col, lon_col]:
    if c in valid_points.columns:
        valid_points[c] = pd.to_numeric(valid_points[c], errors="coerce")
valid_points = valid_points.dropna(subset=[lat_col, lon_col])

center_lat, center_lon = center_from_points(valid_points, lon_col, lat_col)
m = folium.Map(location=[center_lat, center_lon], zoom_start=13, control_scale=True)

# Capas base
folium.TileLayer(tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
                 attr="© CARTO", name="CartoDB Positron (gris)").add_to(m)
folium.TileLayer("OpenStreetMap", name="OpenStreetMap (callejero)").add_to(m)
folium.TileLayer(tiles="https://{s}.tile.stamen.com/terrain/{z}/{x}/{y}.png",
                 attr="© Stamen/OSM", name="Stamen Terrain (relieve)").add_to(m)
folium.TileLayer(tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                 attr="Tiles © Esri", name="Esri WorldImagery (satelital)").add_to(m)

# Marcadores
if not valid_points.empty:
    mc = MarkerCluster(name="Clúster de puntos"); mc.add_to(m)
    for idx, r in valid_points.iterrows():
        lat, lon = float(r[lat_col]), float(r[lon_col])
        popup_fields = []
        for c in df.columns:
            val = r[c]
            if pd.isna(val): continue
            if isinstance(val, (pd.Timestamp, np.datetime64)):
                try: val = pd.to_datetime(val).strftime("%d/%m/%Y %H:%M")
                except: pass
            popup_fields.append(f"<b>{c}:</b> {val}")
        is_dup = idx in dup_set
        icon = folium.Icon(color="red" if is_dup else "blue", icon="info-sign")
        folium.Marker((lat, lon), popup=folium.Popup("<br>".join(popup_fields), max_width=420), icon=icon).add_to(mc)

# Heatmap
if len(valid_points) >= 2:
    HeatMap(
        valid_points[[lat_col, lon_col]].values.tolist(),
        radius=20, blur=25,
        gradient={0.2: "#ffffb2", 0.4: "#fecc5c", 0.6: "#fd8d3c", 0.8: "#f03b20", 1.0: "#bd0026"},
        name="Mapa de calor"
    ).add_to(m)

folium.LayerControl(collapsed=False).add_to(m)
MeasureControl(position='topright', primary_length_unit='meters',
               secondary_length_unit='kilometers',
               primary_area_unit='sqmeters',
               secondary_area_unit='hectares').add_to(m)

# Traducir popup medición
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

# Render
st_folium(m, use_container_width=True, returned_objects=[])

# Descargar mapa HTML para evidencias
st.markdown("**Exportar mapa para evidencias**")
map_html_bytes = m.get_root().render().encode("utf-8")
st.download_button("⬇️ Descargar mapa (HTML)", data=map_html_bytes,
                   file_name="mapa_encuestas.html", mime="text/html", key="dl_html_mapa")

# ======== Preparar detalle para PDF ========
detalle_dupes = []
if not dupes_now.empty:
    for i, row in dupes_now.iterrows():
        idxs = row["indices"]
        motivo, rango = reason_for_group(df, idxs, time_col, lon_col, lat_col, window_minutes)
        detalle_dupes.append({
            "conteo": int(row["conteo_duplicados"]),
            "eliminar": max(0, int(row["conteo_duplicados"])-1),
            "indices": idxs,
            "rango": rango,
            "motivo": motivo
        })

# ======== PDF con Platypus (ajuste de texto) ========
def build_pdf(report_title: str, conteos: dict, detalle_dupes: list,
              mapa_dup_png_bytes: bytes | None,
              mapa_final_png_bytes: bytes | None) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H1b", parent=styles["Heading1"], fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="H2b", parent=styles["Heading2"], fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="Body", parent=styles["BodyText"], leading=14))

    flow = []
    flow.append(Paragraph(report_title, styles["H1b"]))
    flow.append(Paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["Body"]))
    flow.append(Spacer(1, 10))

    # ---- Resumen en CUADRO ----
    flow.append(Paragraph("Resumen", styles["H2b"]))
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
    flow.append(table)
    flow.append(Spacer(1, 10))

    # ---- Detalle duplicados (IDs con misma fuente que el cuerpo) ----
    if not detalle_dupes:
        flow.append(Paragraph("No se detectaron respuestas duplicadas. Todas las respuestas cargadas están validadas.", styles["Body"]))
        flow.append(Spacer(1, 12))
    else:
        flow.append(Paragraph("Detalle de respuestas duplicadas", styles["H2b"]))
        for i, d in enumerate(detalle_dupes, start=1):
            flow.append(Paragraph(
                f"<b>Grupo {i}</b> – {d['conteo']} respuestas (se eliminarán {d['eliminar']}, quedará 1).",
                styles["Body"]
            ))
            flow.append(Paragraph(f"Rango temporal: {d['rango'] or '-'}", styles["Body"]))
            # IDs en estilo Body (no monoespaciado)
            flow.append(Paragraph(f"Índices/IDs: {', '.join(map(str, d['indices']))}", styles["Body"]))
            flow.append(Paragraph(f"Motivo: {d['motivo']}", styles["Body"]))
            flow.append(Spacer(1, 8))

    # ---- Evidencias ----
    if (mapa_dup_png_bytes is not None) or (mapa_final_png_bytes is not None):
        flow.append(Paragraph("Evidencias visuales", styles["H2b"]))

    def add_img(png_bytes, caption):
        if not png_bytes:
            return
        img = ImageReader(io.BytesIO(png_bytes))
        iw, ih = img.getSize()
        max_w = A4[0] - 4*cm
        max_h = A4[1] / 2.2
        ratio = min(max_w/iw, max_h/ih)
        w, h = iw*ratio, ih*ratio
        flow.append(Image(io.BytesIO(png_bytes), width=w, height=h))
        flow.append(Paragraph(caption, styles["Body"]))
        flow.append(Spacer(1, 8))

    add_img(mapa_dup_png_bytes, "Mapa con duplicadas (marcadas en rojo).")
    add_img(mapa_final_png_bytes, "Mapa final (respuestas validadas).")

    doc.build(flow)
    buffer.seek(0)
    return buffer.getvalue()

# ======== Generar PDF ========
st.markdown("### 📄 Generar PDF de avance")
conteos_pdf = {
    "Respuestas totales": total,
    "Duplicadas detectadas": duplicadas if not dupes.empty else 0,
    "Se eliminarán (1 por grupo)": eliminar_por_grupo if not dupes.empty else 0,
    "Quedarán validadas": validadas,
    "Última respuesta": "-" if time_col not in df.columns or pd.isna(df[time_col].max()) else pd.to_datetime(df[time_col].max()).strftime("%d/%m/%Y"),
}
dup_png_bytes = map_dup_png.read() if map_dup_png else None
final_png_bytes = map_final_png.read() if map_final_png else None
pdf_bytes = build_pdf(REPORT_TITLE, conteos_pdf, detalle_dupes, dup_png_bytes, final_png_bytes)
st.download_button(
    "⬇️ Descargar PDF de avance",
    data=pdf_bytes,
    file_name=f"informe_avance_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    mime="application/pdf",
    key="dl_pdf_avance"
)

# ======== Tabla rápida ========
st.markdown("### 📄 Datos (primeras filas)")
st.dataframe(df.head(1000), use_container_width=True)

