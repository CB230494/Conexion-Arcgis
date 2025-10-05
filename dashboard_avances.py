# ---------------------------------------------
# 📋 SISTEMA DE CONTROL PARA BARBERÍA - STREAMLIT
# Pestaña 1: ✂️ Registro de Cortes
# ---------------------------------------------

import streamlit as st
import pandas as pd
import io
from datetime import datetime, date, time

from database import (
    insertar_corte,
    obtener_cortes,
    eliminar_corte,
    actualizar_corte
)

# -----------------------------
# 🎛️ Configuración de la app
# -----------------------------
st.set_page_config(
    page_title="Barbería - Registro de Cortes",
    layout="wide",
    initial_sidebar_state="expanded"
)

# -----------------------------
# 📌 Menú lateral
# -----------------------------
menu = st.sidebar.radio(
    "Selecciona una sección",
    [
        "✂️ Registro de Cortes",
        "📦 Inventario",
        "📅 Citas",
        "💵 Finanzas",
        "📊 Reporte General"
    ]
)

# ---------------------------------------------
# ✂️ PESTAÑA 1: Registro de Cortes
# ---------------------------------------------
if menu == "✂️ Registro de Cortes":
    st.title("✂️ Registro de Cortes Realizados")
    st.markdown("Agrega, consulta o elimina cortes realizados por los barberos.")

    # ---------- FORMULARIO NUEVO CORTE ----------
    st.subheader("➕ Agregar nuevo corte")

    with st.form("form_nuevo_corte"):
        col1, col2, col3 = st.columns(3)
        with col1:
            fecha = st.date_input("Fecha", value=date.today())
        with col2:
            barbero = st.text_input("Nombre del barbero")
        with col3:
            cliente = st.text_input("Nombre del cliente")

        tipo_corte = st.selectbox("Tipo de corte", ["Clásico", "Fade", "Diseño", "Barba", "Otro"])
        precio = st.number_input("Precio (₡)", min_value=0.0, step=500.0, format="%.2f")
        observacion = st.text_area("Observaciones (opcional)")
        submitted = st.form_submit_button("💾 Guardar")

        if submitted:
            if not barbero.strip() or not cliente.strip():
                st.warning("⚠️ Barbero y Cliente son campos obligatorios.")
            else:
                insertar_corte(str(fecha), barbero.strip(), cliente.strip(), tipo_corte, precio, observacion.strip())
                st.success("✅ Corte registrado correctamente")
                st.rerun()

    st.divider()

    # ---------- LISTADO DE CORTES REGISTRADOS ----------
    st.subheader("📋 Historial de cortes")

    cortes = obtener_cortes()
    if cortes:
        df = pd.DataFrame(cortes)
        df["fecha"] = pd.to_datetime(df["fecha"]).dt.strftime("%d/%m/%Y")
        df["precio"] = df["precio"].map(lambda x: round(x, 2))

        # Botón para descargar respaldo en Excel
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name="Cortes")
        st.download_button(
            label="⬇️ Descargar respaldo en Excel",
            data=output.getvalue(),
            file_name="cortes_registrados.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        # Mostrar los cortes en tarjetas editables
        for corte in cortes:
            with st.container():
                id_corte = corte["id"]
                editar = st.session_state.get(f"edit_{id_corte}", False)

                if editar:
                    st.markdown(f"### ✏️ Editando corte ID {id_corte}")
                    f = st.date_input("Fecha", value=pd.to_datetime(corte["fecha"]), key=f"fecha_{id_corte}")
                    b = st.text_input("Barbero", value=corte["barbero"], key=f"barbero_{id_corte}")
                    c = st.text_input("Cliente", value=corte["cliente"], key=f"cliente_{id_corte}")
                    t = st.selectbox("Tipo de corte", ["Clásico", "Fade", "Diseño", "Barba", "Otro"], index=0, key=f"tipo_{id_corte}")
                    p = st.number_input("Precio (₡)", value=float(corte["precio"]), step=500.0, format="%.2f", key=f"precio_{id_corte}")
                    o = st.text_area("Observación", value=corte["observacion"] or "", key=f"obs_{id_corte}")

                    col1, col2 = st.columns(2)
                    if col1.button("💾 Guardar", key=f"guardar_{id_corte}"):
                        actualizar_corte(id_corte, {
                            "fecha": str(f),
                            "barbero": b,
                            "cliente": c,
                            "tipo_corte": t,
                            "precio": p,
                            "observacion": o
                        })
                        st.session_state[f"edit_{id_corte}"] = False
                        st.success("✅ Corte actualizado")
                        st.rerun()
                    if col2.button("❌ Cancelar", key=f"cancelar_{id_corte}"):
                        st.session_state[f"edit_{id_corte}"] = False
                        st.rerun()
                else:
                    cols = st.columns([1.5, 2, 2, 2, 1.5, 3, 1, 1])
                    cols[0].markdown(f"🗓️ **{corte['fecha']}**")
                    cols[1].markdown(f"💈 **{corte['barbero']}**")
                    cols[2].markdown(f"👤 {corte['cliente']}")
                    cols[3].markdown(f"✂️ {corte['tipo_corte']}")
                    cols[4].markdown(f"💰 ₡{corte['precio']:,.2f}")
                    cols[5].markdown(f"📝 {corte['observacion'] or '—'}")
                    if cols[6].button("✏️", key=f"edit_{id_corte}"):
                        st.session_state[f"edit_{id_corte}"] = True
                        st.rerun()
                    if cols[7].button("🗑️", key=f"delete_{id_corte}"):
                        eliminar_corte(id_corte)
                        st.success("✅ Corte eliminado")
                        st.rerun()
    else:
        st.info("Aún no se han registrado cortes.")
# ---------------------------------------------
# 📦 PESTAÑA 2: Inventario
# ---------------------------------------------
elif menu == "📦 Inventario":
    from database import (
        insertar_producto,
        obtener_productos,
        actualizar_producto,
        eliminar_producto
    )

    st.title("📦 Inventario de Productos")
    st.markdown("Administra los productos disponibles y su stock.")

    # ---------- AGREGAR PRODUCTO ----------
    st.subheader("➕ Agregar nuevo producto")
    with st.form("form_nuevo_producto"):
        col1, col2 = st.columns(2)
        nombre = col1.text_input("Nombre del producto")
        precio_unitario = col2.number_input("Precio unitario (₡)", min_value=0.0, step=100.0, format="%.2f")
        descripcion = st.text_input("Descripción (opcional)")
        stock = st.number_input("Stock inicial", min_value=0, step=1)
        enviado = st.form_submit_button("💾 Guardar producto")

        if enviado:
            if not nombre.strip():
                st.warning("⚠️ El nombre del producto es obligatorio.")
            else:
                insertar_producto(nombre.strip(), descripcion.strip(), stock, precio_unitario)
                st.success("✅ Producto registrado correctamente")
                st.rerun()

    st.divider()

    # ---------- LISTADO DE PRODUCTOS ----------
    st.subheader("📋 Productos en inventario")
    productos = obtener_productos()

    if productos:
        df = pd.DataFrame(productos)
        df["precio_unitario"] = df["precio_unitario"].map(lambda x: round(x, 2))

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Productos")
        st.download_button(
            label="⬇️ Descargar inventario en Excel",
            data=output.getvalue(),
            file_name="inventario_productos.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        for producto in productos:
            id_producto = producto["id"]
            editando = st.session_state.get(f"edit_prod_{id_producto}", False)

            if editando:
                st.markdown(f"### ✏️ Editando producto ID {id_producto}")
                col1, col2 = st.columns(2)
                nombre_edit = col1.text_input("Nombre", value=producto["nombre"], key=f"nombre_{id_producto}")
                precio_edit = col2.number_input("Precio (₡)", value=float(producto["precio_unitario"]), step=100.0, format="%.2f", key=f"precio_{id_producto}")
                descripcion_edit = st.text_input("Descripción", value=producto["descripcion"] or "", key=f"desc_{id_producto}")
                stock_edit = st.number_input("Stock", value=int(producto["stock"]), step=1, key=f"stock_{id_producto}")
                col1, col2 = st.columns(2)
                if col1.button("💾 Guardar", key=f"guardar_{id_producto}"):
                    actualizar_producto(id_producto, {
                        "nombre": nombre_edit,
                        "precio_unitario": precio_edit,
                        "descripcion": descripcion_edit,
                        "stock": stock_edit
                    })
                    st.session_state[f"edit_prod_{id_producto}"] = False
                    st.success("✅ Producto actualizado")
                    st.rerun()
                if col2.button("❌ Cancelar", key=f"cancelar_{id_producto}"):
                    st.session_state[f"edit_prod_{id_producto}"] = False
                    st.rerun()
            else:
                cols = st.columns([2, 2, 2, 2, 1, 1])
                cols[0].markdown(f"📦 **{producto['nombre']}**")
                cols[1].markdown(f"🧾 {producto['descripcion'] or '—'}")
                cols[2].markdown(f"💰 ₡{producto['precio_unitario']:,.2f}")
                cols[3].markdown(f"📦 Stock: {producto['stock']}")
                if cols[4].button("✏️", key=f"edit_{id_producto}"):
                    st.session_state[f"edit_prod_{id_producto}"] = True
                    st.rerun()
                if cols[5].button("🗑️", key=f"del_{id_producto}"):
                    eliminar_producto(id_producto)
                    st.success("✅ Producto eliminado")
                    st.rerun()
    else:
        st.info("No hay productos registrados todavía.")
# ---------------------------------------------
# 📅 PESTAÑA: Gestión de Citas
# ---------------------------------------------
elif menu == "📅 Citas":
    from database import obtener_citas, actualizar_estado_cita, actualizar_cita, eliminar_cita
    from datetime import datetime, date, time
    import pandas as pd

    st.title("📅 Gestión de Citas")
    st.markdown("Revisa y administra las citas solicitadas por los clientes.")

    citas = obtener_citas()
    df = pd.DataFrame(citas)

    if df.empty:
        st.info("No hay citas registradas.")
    else:
        estados = ["todas", "pendiente", "aceptada", "rechazada"]
        estado_filtro = st.selectbox("🔍 Filtrar por estado", estados)

        if estado_filtro != "todas":
            df = df[df["estado"] == estado_filtro]

        for cita in df.itertuples():
            with st.container():
                st.markdown(f"### 🧾 Cita ID {cita.id}")
                col1, col2, col3 = st.columns(3)
                fecha_str = cita.fecha.strftime("%d/%m/%Y") if not isinstance(cita.fecha, str) else cita.fecha
                col1.markdown(f"**📅 Fecha:** {fecha_str}")
                col2.markdown(f"**🕒 Hora:** {cita.hora}")
                col3.markdown(f"**🧴 Servicio:** {cita.servicio}")
                st.markdown(f"**👤 Cliente:** {cita.cliente_nombre}")
                st.markdown(f"**✂️ Barbero asignado:** {cita.barbero or 'Sin asignar'}")
                st.markdown(f"**📌 Estado actual:** `{cita.estado}`")

                with st.expander("✏️ Editar cita"):
                    # Convertir fecha a formato compatible
                    if isinstance(cita.fecha, str):
                        try:
                            valor_fecha = datetime.strptime(cita.fecha, "%d/%m/%Y").date()
                        except ValueError:
                            valor_fecha = datetime.strptime(cita.fecha, "%Y-%m-%d").date()
                    else:
                        valor_fecha = cita.fecha

                    nueva_fecha = st.date_input("📅 Nueva fecha", value=valor_fecha, key=f"fecha_{cita.id}")

                    # Convertir hora a formato time
                    try:
                        hora_original = datetime.strptime(cita.hora, "%H:%M").time()
                    except ValueError:
                        hora_original = datetime.strptime(cita.hora, "%H:%M:%S").time()

                    nueva_hora = st.time_input("🕒 Nueva hora", value=hora_original, key=f"hora_{cita.id}")
                    nuevo_barbero = st.text_input("✂️ Asignar barbero", value=cita.barbero or "", key=f"barbero_{cita.id}")
                    nueva_fecha_str = nueva_fecha.strftime("%Y-%m-%d")
                    nueva_hora_str = nueva_hora.strftime("%H:%M")

                    col_e1, col_e2 = st.columns(2)
                    if col_e1.button("💾 Guardar cambios", key=f"guardar_cita_{cita.id}"):
                        actualizar_cita(cita.id, {
                            "fecha": nueva_fecha_str,
                            "hora": nueva_hora_str,
                            "barbero": nuevo_barbero
                        })
                        st.success("✅ Cita actualizada")
                        st.rerun()

                    if col_e2.button("🗑️ Eliminar cita", key=f"eliminar_cita_{cita.id}"):
                        eliminar_cita(cita.id)
                        st.success("✅ Cita eliminada")
                        st.rerun()

                col_a1, col_a2 = st.columns(2)
                if cita.estado == "pendiente":
                    if col_a1.button("✅ Aceptar", key=f"aceptar_{cita.id}"):
                        actualizar_estado_cita(cita.id, "aceptada")
                        st.success("📬 Cita aceptada")
                        st.rerun()
                    if col_a2.button("❌ Rechazar", key=f"rechazar_{cita.id}"):
                        actualizar_estado_cita(cita.id, "rechazada")
                        st.warning("📭 Cita rechazada")
                        st.rerun()


# ---------------------------------------------
# 💵 PESTAÑA 4: Finanzas
# ---------------------------------------------
elif menu == "💵 Finanzas":
    from database import (
        insertar_ingreso,
        obtener_ingresos,
        actualizar_ingreso,
        eliminar_ingreso,
        insertar_gasto,
        obtener_gastos,
        actualizar_gasto,
        eliminar_gasto
    )

    st.title("💵 Control de Finanzas")
    st.markdown("Registra ingresos y gastos de la barbería, y consulta el balance general.")

    # ----------- FORMULARIO INGRESO -----------
    st.subheader("➕ Agregar Ingreso")
    with st.form("form_ingreso"):
        col1, col2 = st.columns(2)
        fecha_i = col1.date_input("Fecha del ingreso", value=date.today())
        concepto_i = col2.text_input("Concepto del ingreso")
        monto_i = st.number_input("Monto (₡)", min_value=0.0, step=500.0, format="%.2f", key="monto_ingreso")
        observacion_i = st.text_area("Observación (opcional)")
        enviar_i = st.form_submit_button("💾 Guardar ingreso")
        if enviar_i:
            if not concepto_i.strip():
                st.warning("⚠️ El concepto es obligatorio.")
            else:
                insertar_ingreso(str(fecha_i), concepto_i.strip(), monto_i, observacion_i.strip())
                st.success("✅ Ingreso registrado")
                st.rerun()

    # ----------- FORMULARIO GASTO -----------
    st.subheader("➖ Agregar Gasto")
    with st.form("form_gasto"):
        col1, col2 = st.columns(2)
        fecha_g = col1.date_input("Fecha del gasto", value=date.today())
        concepto_g = col2.text_input("Concepto del gasto")
        monto_g = st.number_input("Monto (₡)", min_value=0.0, step=500.0, format="%.2f", key="monto_gasto")
        observacion_g = st.text_area("Observación (opcional)", key="obs_gasto")
        enviar_g = st.form_submit_button("💾 Guardar gasto")
        if enviar_g:
            if not concepto_g.strip():
                st.warning("⚠️ El concepto es obligatorio.")
            else:
                insertar_gasto(str(fecha_g), concepto_g.strip(), monto_g, observacion_g.strip())
                st.success("✅ Gasto registrado")
                st.rerun()

    st.divider()

    # ----------- HISTORIAL Y BALANCE -----------
    st.subheader("📊 Resumen de movimientos")

    ingresos = obtener_ingresos()
    gastos = obtener_gastos()

    df_i = pd.DataFrame(ingresos) if ingresos else pd.DataFrame()
    df_g = pd.DataFrame(gastos) if gastos else pd.DataFrame()

    total_i = sum(i["monto"] for i in ingresos)
    total_g = sum(g["monto"] for g in gastos)
    balance = total_i - total_g
    color = "green" if balance >= 0 else "red"

    st.markdown(f"**💰 Total Ingresos:** ₡{total_i:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    st.markdown(f"**💸 Total Gastos:** ₡{total_g:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    st.markdown(
        f"<strong>🧾 Balance general:</strong> <span style='color:{color}; font-weight:bold;'>₡{balance:,.2f}</span>"
        .replace(",", "X").replace(".", ",").replace("X", "."), unsafe_allow_html=True
    )

    st.divider()

    # ----------- LISTADOS Y DESCARGA -----------
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("### 📋 Ingresos")
        if not df_i.empty:
            df_i["fecha"] = pd.to_datetime(df_i["fecha"]).dt.strftime("%d/%m/%Y")
            df_i["monto"] = df_i["monto"].map(lambda x: round(x, 2))
            for ingreso in ingresos:
                id = ingreso["id"]
                editando = st.session_state.get(f"edit_ingreso_{id}", False)

                if editando:
                    st.markdown(f"#### ✏️ Editando ingreso ID {id}")
                    f = st.date_input("Fecha", value=pd.to_datetime(ingreso["fecha"]), key=f"fecha_i_{id}")
                    c = st.text_input("Concepto", value=ingreso["concepto"], key=f"concepto_i_{id}")
                    m = st.number_input("Monto (₡)", value=float(ingreso["monto"]), key=f"monto_i_{id}", step=500.0)
                    o = st.text_input("Observación", value=ingreso["observacion"] or "", key=f"obs_i_{id}")
                    col1a, col2a = st.columns(2)
                    if col1a.button("💾 Guardar", key=f"guardar_i_{id}"):
                        actualizar_ingreso(id, {"fecha": str(f), "concepto": c, "monto": m, "observacion": o})
                        st.session_state[f"edit_ingreso_{id}"] = False
                        st.rerun()
                    if col2a.button("❌ Cancelar", key=f"cancelar_i_{id}"):
                        st.session_state[f"edit_ingreso_{id}"] = False
                        st.rerun()
                else:
                    st.markdown(f"📅 {ingreso['fecha']} | 💰 ₡{ingreso['monto']:,.2f} | 📄 {ingreso['concepto']}")
                    st.markdown(f"📝 {ingreso['observacion'] or '—'}")
                    col1b, col2b = st.columns(2)
                    if col1b.button("✏️ Editar", key=f"editar_i_{id}"):
                        st.session_state[f"edit_ingreso_{id}"] = True
                        st.rerun()
                    if col2b.button("🗑️ Eliminar", key=f"eliminar_i_{id}"):
                        eliminar_ingreso(id)
                        st.success("✅ Ingreso eliminado")
                        st.rerun()
        else:
            st.info("No hay ingresos registrados.")

    with col2:
        st.markdown("### 📋 Gastos")
        if not df_g.empty:
            df_g["fecha"] = pd.to_datetime(df_g["fecha"]).dt.strftime("%d/%m/%Y")
            df_g["monto"] = df_g["monto"].map(lambda x: round(x, 2))
            for gasto in gastos:
                id = gasto["id"]
                editando = st.session_state.get(f"edit_gasto_{id}", False)

                if editando:
                    st.markdown(f"#### ✏️ Editando gasto ID {id}")
                    f = st.date_input("Fecha", value=pd.to_datetime(gasto["fecha"]), key=f"fecha_g_{id}")
                    c = st.text_input("Concepto", value=gasto["concepto"], key=f"concepto_g_{id}")
                    m = st.number_input("Monto (₡)", value=float(gasto["monto"]), key=f"monto_g_{id}", step=500.0)
                    o = st.text_input("Observación", value=gasto["observacion"] or "", key=f"obs_g_{id}")
                    col1a, col2a = st.columns(2)
                    if col1a.button("💾 Guardar", key=f"guardar_g_{id}"):
                        actualizar_gasto(id, {"fecha": str(f), "concepto": c, "monto": m, "observacion": o})
                        st.session_state[f"edit_gasto_{id}"] = False
                        st.rerun()
                    if col2a.button("❌ Cancelar", key=f"cancelar_g_{id}"):
                        st.session_state[f"edit_gasto_{id}"] = False
                        st.rerun()
                else:
                    st.markdown(f"📅 {gasto['fecha']} | 💸 ₡{gasto['monto']:,.2f} | 📄 {gasto['concepto']}")
                    st.markdown(f"📝 {gasto['observacion'] or '—'}")
                    col1b, col2b = st.columns(2)
                    if col1b.button("✏️ Editar", key=f"editar_g_{id}"):
                        st.session_state[f"edit_gasto_{id}"] = True
                        st.rerun()
                    if col2b.button("🗑️ Eliminar", key=f"eliminar_g_{id}"):
                        eliminar_gasto(id)
                        st.success("✅ Gasto eliminado")
                        st.rerun()
        else:
            st.info("No hay gastos registrados.")
# ---------------------------------------------
# 📊 PESTAÑA 5: Reporte General
# ---------------------------------------------
elif menu == "📊 Reporte General":
    from database import obtener_cortes, obtener_ingresos, obtener_gastos

    st.title("📊 Reporte General")
    st.markdown("Resumen de actividad y finanzas por período de tiempo.")

    # --------- Filtro de fechas ---------
    col1, col2 = st.columns(2)
    fecha_inicio = col1.date_input("📅 Desde", value=date(2025, 1, 1))
    fecha_fin = col2.date_input("📅 Hasta", value=date.today())

    cortes = obtener_cortes()
    ingresos = obtener_ingresos()
    gastos = obtener_gastos()

    df_cortes = pd.DataFrame(cortes)
    df_ingresos = pd.DataFrame(ingresos)
    df_gastos = pd.DataFrame(gastos)

    # --------- Filtros por fecha ---------
    def filtrar_por_fecha(df, columna="fecha"):
        if df.empty:
            return df
        df[columna] = pd.to_datetime(df[columna]).dt.date
        return df[(df[columna] >= fecha_inicio) & (df[columna] <= fecha_fin)]

    df_cortes = filtrar_por_fecha(df_cortes)
    df_ingresos = filtrar_por_fecha(df_ingresos)
    df_gastos = filtrar_por_fecha(df_gastos)

    # --------- Cortes realizados ---------
    st.subheader("💈 Cortes realizados")
    if not df_cortes.empty:
        total_cortes = len(df_cortes)
        total_por_barbero = df_cortes["barbero"].value_counts().reset_index()
        total_por_barbero.columns = ["Barbero", "Cantidad de cortes"]
        st.markdown(f"**Total de cortes:** {total_cortes}")
        st.dataframe(total_por_barbero, use_container_width=True)
    else:
        st.info("No hay cortes registrados en el rango seleccionado.")

    # --------- Ingresos ---------
    st.subheader("💰 Ingresos")
    if not df_ingresos.empty:
        total_ingresos = df_ingresos["monto"].sum()
        st.markdown(f"**Total de ingresos:** ₡{total_ingresos:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        st.dataframe(df_ingresos[["fecha", "concepto", "monto", "observacion"]], use_container_width=True)
    else:
        st.info("No hay ingresos registrados en el rango seleccionado.")

    # --------- Gastos ---------
    st.subheader("💸 Gastos")
    if not df_gastos.empty:
        total_gastos = df_gastos["monto"].sum()
        st.markdown(f"**Total de gastos:** ₡{total_gastos:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        st.dataframe(df_gastos[["fecha", "concepto", "monto", "observacion"]], use_container_width=True)
    else:
        st.info("No hay gastos registrados en el rango seleccionado.")

    # --------- Balance final ---------
    st.divider()
    st.subheader("📉 Balance del período")

    balance = total_ingresos - total_gastos
    color = "green" if balance >= 0 else "red"
    st.markdown(
        f"<strong>Balance final:</strong> <span style='color:{color}; font-weight:bold;'>₡{balance:,.2f}</span>"
        .replace(",", "X").replace(".", ",").replace("X", "."),
        unsafe_allow_html=True
    )

    # --------- Descargar resumen Excel ---------
    st.divider()
    st.subheader("⬇️ Descargar respaldo")
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_cortes.to_excel(writer, index=False, sheet_name="Cortes")
        df_ingresos.to_excel(writer, index=False, sheet_name="Ingresos")
        df_gastos.to_excel(writer, index=False, sheet_name="Gastos")

    st.download_button(
        label="📁 Descargar respaldo en Excel",
        data=output.getvalue(),
        file_name="resumen_general.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )




