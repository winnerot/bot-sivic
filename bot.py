import asyncio
import io
import logging
import os
import time
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from termux_playwright import async_playwright_termux
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import urllib3
import pymysql

# Librería para generar el PDF Profesional
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# Configuración de logging y urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# Estados de la conversación
ESPERANDO_CAPTCHA = 1

# Constantes SENIAT & Telegram
TELEGRAM_BOT_TOKEN = "8648857231:AAFGoIk7Vslkj7Av8aaLHLJqzSQka0qN6Lo"
SENIAT_USER = "v9684209"
SENIAT_PASS = "v9684209"

# CONFIGURACIÓN DE ADMINISTRADOR Y ACCESO
ADMIN_USER_ID = 2098655970  # <--- ID DE ADMINISTRADOR PRINCIPAL

# Archivos de persistencia
SESSION_FILE = "seniat_session.json"
USERS_FILE = "authorized_users.txt"
LOGS_FILE = "consultas_logs.txt"

SESSION_DURATION = 30 * 60  # 30 minutos en segundos
_last_successful_login = 0

# Instancias globales para mantener la sesión persistente de Playwright y la cola de tareas
_playwright_inst = None
_browser_inst = None
_page_inst = None
consulta_lock = asyncio.Lock()


# ==============================================================================
# GESTIÓN DE ACCESO, LOGS Y NOTIFICACIONES
# ==============================================================================
def cargar_usuarios_autorizados() -> set:
    autorizados = {ADMIN_USER_ID}
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                for line in f:
                    uid = line.strip()
                    if uid.isdigit():
                        autorizados.add(int(uid))
        except Exception as e:
            logger.error(f"Error cargando usuarios autorizados: {e}")
    return autorizados


def guardar_usuario_autorizado(user_id: int):
    try:
        autorizados = cargar_usuarios_autorizados()
        autorizados.add(user_id)
        with open(USERS_FILE, "w") as f:
            for uid in autorizados:
                f.write(f"{uid}\n")
    except Exception as e:
        logger.error(f"Error guardando usuario autorizado: {e}")


def remover_usuario_autorizado(user_id: int):
    try:
        autorizados = cargar_usuarios_autorizados()
        if user_id in autorizados:
            autorizados.remove(user_id)
        with open(USERS_FILE, "w") as f:
            for uid in autorizados:
                if uid != ADMIN_USER_ID:
                    f.write(f"{uid}\n")
    except Exception as e:
        logger.error(f"Error removiendo usuario autorizado: {e}")


def registrar_log_consulta(user, parametro_busqueda: str, tipo_consulta: str):
    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = f"@{user.username}" if user.username else "Sin username"
        nombre_completo = f"{user.first_name or ''} {user.last_name or ''}".strip()
        log_line = f"[{ahora}] | ID: {user.id} | User: {username} | Nombre: {nombre_completo} | Tipo: {tipo_consulta} | Consulta: {parametro_busqueda}\n"
        with open(LOGS_FILE, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as e:
        logger.error(f"Error guardando log de consulta: {e}")


async def notificar_admin_consulta(bot, user, parametro_busqueda: str, tipo_consulta: str):
    """Envía una alerta en tiempo real al Administrador sobre la búsqueda ejecutada."""
    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = f"@{user.username}" if user.username else "Sin @username"
        nombre_completo = f"{user.first_name or ''} {user.last_name or ''}".strip()
        
        mensaje_alerta = (
            "🚨 *ALERTA DE CONSULTA REALIZADA*\n\n"
            f"👤 *Usuario:* {nombre_completo} ({username})\n"
            f"🆔 *ID Telegram:* `{user.id}`\n"
            f"🔍 *Tipo:* {tipo_consulta}\n"
            f"📄 *Búsqueda:* `{parametro_busqueda}`\n"
            f"⏰ *Fecha:* {ahora}"
        )
        
        await bot.send_message(chat_id=ADMIN_USER_ID, text=mensaje_alerta, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"No se pudo enviar la notificación al administrador: {e}")


# ==============================================================================
# CONSULTA MYSQL (SERVIDOR SERV00.COM)
# ==============================================================================
def consultar_telefono_mysql(cedula: str) -> list:
    """Consulta los números telefónicos mediante la API PHP alojada en serv00 vía HTTP y con mayor tiempo de espera."""
    telefonos = []
    try:
        url = f"http://victor159.serv00.net/consulta.php?cedula={cedula}"
        response = requests.get(url, timeout=20, verify=False)
        
        if response.status_code == 200 and response.text.strip():
            telefonos = response.json()
    except requests.exceptions.JSONDecodeError:
        logger.error(f"El script PHP no devolvió un JSON válido. Respuesta: {response.text[:200]}")
    except requests.exceptions.Timeout:
        logger.error("La conexión con el script PHP en Serv00 tardó demasiado (Timeout).")
    except Exception as e:
        logger.error(f"Error al consultar el script PHP en serv00: {e}")
    return telefonos

# ==============================================================================
# COMANDOS DE ADMINISTRADOR
# ==============================================================================
async def cmd_autorizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif args and args[0].isdigit():
        target_id = int(args[0])

    if not target_id:
        await update.message.reply_text("⚠️ Uso incorrecto. Responde a un mensaje o usa: `/autorizar ID`", parse_mode="Markdown")
        return

    guardar_usuario_autorizado(target_id)
    await update.message.reply_text(f"✅ ¡Usuario `{target_id}` autorizado exitosamente!", parse_mode="Markdown")


async def cmd_revocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif args and args[0].isdigit():
        target_id = int(args[0])

    if not target_id:
        await update.message.reply_text("⚠️ Uso incorrecto. Responde a un mensaje o usa: `/revocar ID`", parse_mode="Markdown")
        return

    if target_id == ADMIN_USER_ID:
        await update.message.reply_text("❌ No puedes revocarle el acceso al administrador principal.")
        return

    remover_usuario_autorizado(target_id)
    await update.message.reply_text(f"🚫 Acceso revocado para el usuario `{target_id}`.", parse_mode="Markdown")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ No tienes permisos.")
        return

    if not os.path.exists(LOGS_FILE):
        await update.message.reply_text("ℹ️ Aún no hay registros de consultas guardados.")
        return

    try:
        with open(LOGS_FILE, "rb") as f:
            await update.message.reply_document(document=f, caption="📊 *Historial de Consultas Realizadas*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error al enviar logs: {e}")


# ==============================================================================
# CÁLCULO DE EDAD Y GENERADOR DE REPORTE PDF PROFESIONAL
# ==============================================================================
def calcular_edad(fecha_nacimiento_str: str) -> str:
    try:
        fecha_limpia = fecha_nacimiento_str.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                Nacimiento = datetime.strptime(fecha_limpia, fmt)
                hoy = datetime.now()
                edad = hoy.year - Nacimiento.year - ((hoy.month, hoy.day) < (Nacimiento.month, Nacimiento.day))
                return f"{edad} años"
            except ValueError:
                continue
        return "No calculable"
    except Exception:
        return "No calculable"


def generar_documento_pdf(
    identificador: str,
    tipo_consulta: str,
    nombre: str,
    fecha_nacimiento: str,
    edad: str,
    cedula_rif: str,
    correo: str,
    direcciones: list,
    cargas_familiares: list,
    empresas_relaciones: list,
    ivss_info: list,
    telefonos: list
) -> str:
    """Genera un archivo PDF profesional compacto en el orden solicitado."""
    filename = f"Reporte_{identificador}.pdf"
    
    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    
    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=colors.HexColor('#1E3A8A'),
        alignment=TA_CENTER
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.HexColor('#475569'),
        alignment=TA_CENTER
    )
    
    label_style = ParagraphStyle(
        'LabelStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#334155')
    )
    
    value_style = ParagraphStyle(
        'ValueStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#0F172A')
    )

    story.append(Paragraph("Sistema Integrado de Verificación de Información Ciudadana (SIVIC)", title_style))
    story.append(Spacer(1, 3))
    story.append(Paragraph("REPORTE — VENEZUELA", subtitle_style))
    story.append(Spacer(1, 4))
    
    fechahora_actual = datetime.now().strftime("%d/%m/%Y %I:%M %p")
    fecha_style = ParagraphStyle('FechaGen', parent=subtitle_style, fontSize=8, textColor=colors.HexColor('#64748B'))
    story.append(Paragraph(f"Fecha de consulta: {fechahora_actual}", fecha_style))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#1E3A8A'), spaceBefore=2, spaceAfter=8))

    def crear_encabezado_seccion(titulo_texto):
        p_txt = Paragraph(f"<b>{titulo_texto.upper()}</b>", ParagraphStyle('SecT', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, textColor=colors.white))
        t = Table([[p_txt]], colWidths=[540])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#1E3A8A')),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('RIGHTPADDING', (0,0), (-1,-1), 6),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        return t

    # 1. DATOS BÁSICOS
    story.append(crear_encabezado_seccion("1. DATOS DE IDENTIFICACIÓN DE LA PERSONA / EMPRESA"))
    story.append(Spacer(1, 4))

    telefonos_str = ", ".join(telefonos) if telefonos else "NO INDICA"

    datos_tabla = [
        [Paragraph("Nombre / Razón Social:", label_style), Paragraph(f"<b>{nombre or 'NO INDICA'}</b>", value_style)],
        [Paragraph("Fecha de Nacimiento / Const.:", label_style), Paragraph(fecha_nacimiento or 'NO INDICA', value_style)],
        [Paragraph("Edad / Antigüedad:", label_style), Paragraph(edad or 'No calculable', value_style)],
        [Paragraph("Cédula de Identidad / RIF:", label_style), Paragraph(f"<b>{cedula_rif or 'NO INDICA'}</b>", value_style)],
        [Paragraph("Teléfono(s) Asociado(s):", label_style), Paragraph(f"<b>{telefonos_str}</b>", value_style)],
        [Paragraph("Correo Electrónico:", label_style), Paragraph(correo or 'NO INDICA', value_style)],
    ]
    
    t_datos = Table(datos_tabla, colWidths=[150, 390])
    t_datos.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#F8FAFC')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(t_datos)
    story.append(Spacer(1, 8))

    # 2. DIRECCIONES
    story.append(crear_encabezado_seccion("2. DIRECCIONES Y DOMICILIO FISCAL"))
    story.append(Spacer(1, 4))

    if direcciones and isinstance(direcciones, list) and len(direcciones) > 0:
        dir_rows = []
        for i in range(0, len(direcciones), 2):
            item1 = direcciones[i]
            lbl1 = str(item1.get('etiqueta', 'Campo')).title()
            val1 = str(item1.get('valor', 'NO INDICA'))
            
            p_lbl1 = Paragraph(f"{lbl1}:", label_style)
            p_val1 = Paragraph(val1, value_style)

            if i + 1 < len(direcciones):
                item2 = direcciones[i+1]
                lbl2 = str(item2.get('etiqueta', 'Campo')).title()
                val2 = str(item2.get('valor', 'NO INDICA'))
                p_lbl2 = Paragraph(f"{lbl2}:", label_style)
                p_val2 = Paragraph(val2, value_style)
            else:
                p_lbl2 = Paragraph("", label_style)
                p_val2 = Paragraph("", value_style)

            dir_rows.append([p_lbl1, p_val1, p_lbl2, p_val2])

        t_dir = Table(dir_rows, colWidths=[110, 160, 110, 160])
        t_dir.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#F8FAFC')),
            ('BACKGROUND', (2,0), (2,-1), colors.HexColor('#F8FAFC')),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 5),
            ('RIGHTPADDING', (0,0), (-1,-1), 5),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(t_dir)
    else:
        story.append(Paragraph("<i>No se registró información detallada de domicilio fiscal.</i>", value_style))

    story.append(Spacer(1, 8))

    # 3. CARGAS FAMILIARES
    story.append(crear_encabezado_seccion("3. CARGAS FAMILIARES REGISTRADAS"))
    story.append(Spacer(1, 4))

    if cargas_familiares and isinstance(cargas_familiares, list) and len(cargas_familiares) > 0:
        carga_headers = [
            Paragraph("<b>Parentesco</b>", label_style),
            Paragraph("<b>Nombre Completo</b>", label_style),
            Paragraph("<b>Cédula</b>", label_style),
            Paragraph("<b>F. Nacimiento</b>", label_style),
            Paragraph("<b>Edad</b>", label_style),
            Paragraph("<b>Inf. Médico</b>", label_style)
        ]
        carga_rows = [carga_headers]
        for c in cargas_familiares:
            carga_rows.append([
                Paragraph(str(c.get('parentesco', 'N/A')), value_style),
                Paragraph(str(c.get('nombres', 'N/A')), value_style),
                Paragraph(str(c.get('cedula', 'N/A')), value_style),
                Paragraph(str(c.get('fecha_nacimiento', 'N/A')), value_style),
                Paragraph(str(c.get('edad', 'N/A')), value_style),
                Paragraph(str(c.get('nro_informe_medico', 'N/A')), value_style),
            ])
        t_carga = Table(carga_rows, colWidths=[80, 130, 85, 75, 60, 110])
        t_carga.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#E2E8F0')),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(t_carga)
    else:
        story.append(Paragraph("<i>No posee cargas familiares registradas o no aplica.</i>", value_style))

    story.append(Spacer(1, 8))

    # 4. EMPRESAS / RELACIONES
    story.append(crear_encabezado_seccion("4. EMPRESAS Y CONTRIBUYENTES RELACIONADOS (SENIAT)"))
    story.append(Spacer(1, 4))

    if empresas_relaciones and isinstance(empresas_relaciones, list) and len(empresas_relaciones) > 0:
        emp_headers = [
            Paragraph("<b>RIF / C.I.</b>", label_style),
            Paragraph("<b>Nombre / Razón Social</b>", label_style),
            Paragraph("<b>Relación / Cargo</b>", label_style),
            Paragraph("<b>Fecha Inicio</b>", label_style)
        ]
        emp_rows = [emp_headers]
        for emp in empresas_relaciones:
            rif_ci = str(emp.get('rif') or emp.get('rif_ci') or 'N/A')
            nom = str(emp.get('nombre') or 'N/A')
            tip = str(emp.get('tipo') or emp.get('relacion') or 'N/A')
            fec = str(emp.get('fecha') or 'N/A')
            emp_rows.append([
                Paragraph(rif_ci, value_style),
                Paragraph(nom, value_style),
                Paragraph(tip, value_style),
                Paragraph(fec, value_style),
            ])
        t_emp = Table(emp_rows, colWidths=[100, 220, 120, 100])
        t_emp.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#E2E8F0')),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 5),
            ('RIGHTPADDING', (0,0), (-1,-1), 5),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(t_emp)
    else:
        story.append(Paragraph("<i>No se encontraron empresas o relaciones asociadas.</i>", value_style))

    story.append(Spacer(1, 8))

    # 5. IVSS
    story.append(crear_encabezado_seccion("5. INSTITUTO VENEZOLANO DE LOS SEGUROS SOCIALES (IVSS)"))
    story.append(Spacer(1, 4))

    if ivss_info and isinstance(ivss_info, list) and len(ivss_info) > 0:
        ivss_headers = [
            Paragraph("<b>Empresa / Razón Social</b>", label_style),
            Paragraph("<b>Fecha de Ingreso</b>", label_style),
            Paragraph("<b>Estatus</b>", label_style)
        ]
        ivss_rows = [ivss_headers]
        for row in ivss_info:
            ivss_rows.append([
                Paragraph(str(row.get('empresa', 'N/A')), value_style),
                Paragraph(str(row.get('fecha_ingreso', 'N/A')), value_style),
                Paragraph(str(row.get('estatus', 'N/A')), value_style),
            ])
        t_ivss = Table(ivss_rows, colWidths=[280, 130, 130])
        t_ivss.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#E2E8F0')),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 5),
            ('RIGHTPADDING', (0,0), (-1,-1), 5),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(t_ivss)
    else:
        ivss_txt = str(ivss_info or "No se obtuvo información del IVSS.")
        story.append(Paragraph(f"<i>{ivss_txt}</i>", value_style))

    doc.build(story)
    return filename


# ==============================================================================
# PLAYWRIGHT CORE FUNCTIONS (CONFIGURADO PARA ANDROID / TERMUX)
# ==============================================================================
async def get_or_create_page():
    global _playwright_inst, _browser_inst, _page_inst
    if not _playwright_inst:
        # Inicializamos el objeto de playwright directamente
        _playwright_inst = await async_playwright_termux().__aenter__()
        
    if not _browser_inst or not _browser_inst.is_connected():
        # Lanzamos el Chromium nativo de Termux que ya está instalado en el sistema
        _browser_inst = await _playwright_inst.chromium.launch(
            executable_path='/data/data/com.termux/files/usr/bin/chromium',
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
                "--disable-gpu"
            ]
        )
    
    if _page_inst and not _page_inst.is_closed():
        return _page_inst

    context_kwargs = {"viewport": {"width": 1280, "height": 800}}
    if os.path.exists(SESSION_FILE) and os.path.getsize(SESSION_FILE) > 50:
        try:
            context = await _browser_inst.new_context(storage_state=SESSION_FILE, **context_kwargs)
        except Exception:
            context = await _browser_inst.new_context(**context_kwargs)
    else:
        context = await _browser_inst.new_context(**context_kwargs)
        
    _page_inst = await context.new_page()
    return _page_inst


async def guardar_estado_sesion(page):
    global _last_successful_login
    try:
        await page.context.storage_state(path=SESSION_FILE)
        _last_successful_login = time.time()
        print("-> [✓] Sesión y cookies guardadas exitosamente.")
    except Exception as e:
        logger.error(f"No se pudo guardar el estado de la sesión: {e}")


# ==============================================================================
# CONSULTAS TELEFONO
# ==============================================================================
def consultar_inversa_mysql(telefono: str) -> list:
    """Consulta la cédula asociada a un número telefónico mediante el script PHP en serv00."""
    coincidencias = []
    try:
        url = f"http://victor159.serv00.net/consulta.php?telefono={telefono}"
        response = requests.get(url, timeout=20, verify=False)
        if response.status_code == 200 and response.text.strip():
            coincidencias = response.json()
    except Exception as e:
        logger.error(f"Error en consulta por teléfono: {e}")
    return coincidencias


# ==============================================================================
# CONSULTAS PÚBLICAS (SENIAT EMPRESAS & IVSS)
# ==============================================================================
def consultar_seniat_y_nombre(cedula: str):
    url = f"http://contribuyente.seniat.gob.ve/relacionesrif/inicioConsulta.do?personalidad=1&ci={cedula}"
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12, verify=False)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            texto_total = soup.get_text(separator=" ", strip=True)

            if any(kw in texto_total.lower() for kw in ["no existe", "no se encuentra", "error", "invalida"]):
                return "No especificado", "❌ *SENIAT (EMPRESA):* La cédula no se encuentra registrada.", []

            nombre_persona = "No especificado"
            for td in soup.find_all("td"):
                texto_td = td.get_text(strip=True)
                if "NOMBRE:" in texto_td.upper() or "NOMBRE" in texto_td.upper():
                    cleaned = re.sub(r"nombre\s*:\s*", "", texto_td, flags=re.IGNORECASE).strip()
                    if cleaned:
                        nombre_persona = cleaned
                        break

            relaciones = []
            for table in soup.find_all("table"):
                for row in table.find_all("tr"):
                    cols = row.find_all(["td", "th"])
                    cols_text = [c.get_text(strip=True) for c in cols]
                    if len(cols_text) >= 3:
                        primer_col = cols_text[0].upper()
                        if any(primer_col.startswith(pref) for pref in ["V", "J", "E", "G"]) or ("-" in primer_col):
                            relaciones.append({
                                "rif": cols_text[0],
                                "nombre": cols_text[1] if len(cols_text) > 1 else "N/A",
                                "tipo": cols_text[2] if len(cols_text) > 2 else "N/A",
                                "fecha": cols_text[3] if len(cols_text) > 3 else "N/A",
                            })

            resultado_empresas = "🏢 *SENIAT (EMPRESAS / RELACIONES)*\n"
            if relaciones:
                resultado_empresas += "📋 *Contribuyentes Relacionados:*\n"
                for rel in relaciones:
                    resultado_empresas += f"  • *RIF:* `{rel['rif']}`\n"
                    resultado_empresas += f"    *Nombre:* {rel['nombre']}\n"
                    resultado_empresas += f"    *Relación:* {rel['tipo']}\n"
                    resultado_empresas += f"    *Fecha Inicio:* {rel['fecha']}\n"
            else:
                resultado_empresas += "ℹ️ *No se encontraron empresas o relaciones asociadas.*\n"
            
            return nombre_persona, resultado_empresas.strip(), relaciones
        
        return "No especificado", "❌ *SENIAT (EMPRESA):* Error de conexión.", []
    except Exception as e:
        logger.error(f"Error SENIAT: {e}")
        return "No especificado", "❌ *SENIAT (EMPRESA):* Error en la petición HTTP.", []


def consultar_ivss(cedula: str):
    url_base = "http://www.ivss.gob.ve:28083/CuentaIndividualIntranet/"
    url_accion = "http://www.ivss.gob.ve:28083/CuentaIndividualIntranet/CtaIndividualCTRL"
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": url_base,
        "Origin": "http://www.ivss.gob.ve:28083/CuentaIndividualIntranet/",
        "Content-Type": "application/x-www-form-urlencoded",
        "Connection": "close",
    }
    
    try:
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET", "POST"])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        session.get(url_base, headers=headers, timeout=15, verify=False)
        payload = {"Accion": "", "Accion1": "", "nacionalidad_aseg": "V", "cedula_aseg": cedula, "consultar": "Buscar"}
        response = session.post(url_accion, data=payload, headers=headers, timeout=20, verify=False)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            texto_total = soup.get_text(separator=" ", strip=True)
            if any(kw in texto_total.lower() for kw in ["no existe", "no registrado", "error"]):
                return [], "🛡️ *IVSS SEGURO:* La cédula no se encuentra registrada."

            empresas, fechas_ingreso, estatus_list = [], [], []
            for tr in soup.find_all("tr"):
                tds = tr.find_all(["td", "th"])
                for i, td in enumerate(tds):
                    txt = td.get_text(strip=True).upper()
                    if "EMPRESA" in txt or "RAZON SOCIAL" in txt:
                        if i + 1 < len(tds):
                            val = tds[i + 1].get_text(strip=True)
                            if val and val != ":" and val not in empresas:
                                empresas.append(val)
                    if "FECHA" in txt and "INGRESO" in txt:
                        if i + 1 < len(tds):
                            val = tds[i + 1].get_text(strip=True)
                            if val and val != ":" and val not in fechas_ingreso:
                                fechas_ingreso.append(val)
                    if "ESTATUS" in txt or "ESTADO" in txt:
                        if i + 1 < len(tds):
                            val = tds[i + 1].get_text(strip=True)
                            if val and val != ":" and val not in estatus_list:
                                estatus_list.append(val)

            registros_ivss = []
            max_len = max(len(empresas), len(fechas_ingreso), len(estatus_list), 1)
            for idx in range(max_len):
                registros_ivss.append({
                    "empresa": empresas[idx] if idx < len(empresas) else "N/A",
                    "fecha_ingreso": fechas_ingreso[idx] if idx < len(fechas_ingreso) else "N/A",
                    "estatus": estatus_list[idx] if idx < len(estatus_list) else "N/A"
                })

            resultado_msj = "🛡️ *IVSS SEGURO*\n"
            if empresas or fechas_ingreso or estatus_list:
                if empresas:
                    resultado_msj += "*Empresa(s):*\n" + "\n".join([f"• {e}" for e in empresas]) + "\n"
                if fechas_ingreso:
                    resultado_msj += "*Fecha(s) de Ingreso:*\n" + "\n".join([f"• {f}" for f in fechas_ingreso]) + "\n"
                if estatus_list:
                    resultado_msj += "*Estatus:*\n" + "\n".join([f"• {es}" for es in estatus_list]) + "\n"
                return registros_ivss, resultado_msj.strip()
            
            return [], "🛡️ *IVSS SEGURO:* NO ASEGURADO"
        
        return [], "❌ *IVSS SEGURO:* El servicio tardó demasiado en responder."
    except Exception as e:
        logger.warning(f"Aviso IVSS: {e}")
        return [], "⚠️ *IVSS SEGURO:* El servidor tardó en responder."


# ==============================================================================
# FUNCIÓN CENTRAL DE BÚSQUEDA Y EXTRACCIÓN (CÉDULA Y RIF)
# ==============================================================================
async def ejecutar_consulta_rif(page, target_val: str, tipo_consulta: str, update: Update, msg_espera):
    user_actual = update.effective_user

    await notificar_admin_consulta(update.get_bot(), user_actual, target_val, tipo_consulta)

    await page.goto("http://contribuyente.seniat.gob.ve/rifconsulta/login.do", timeout=30000)
    await page.wait_for_load_state("networkidle")
    
    if tipo_consulta == "RIF":
        await page.wait_for_selector("input[name='rif']", timeout=15000)
        await page.fill("input[name='rif']", target_val)
    else:
        await page.wait_for_selector("input[name='cedula']", timeout=15000)
        await page.fill("input[name='cedula']", f"V{target_val}")

    await page.click("input[name='buscar']")
    await asyncio.sleep(4)

    enlace_expediente = page.locator("a[href*='buscarexpediente.do']").first
    if await enlace_expediente.count() == 0:
        try:
            await update.get_bot().delete_message(chat_id=update.effective_chat.id, message_id=msg_espera.message_id)
        except Exception:
            pass
        await update.message.reply_text(f"❌ No se encontró ningún registro para `{target_val}`.", parse_mode="Markdown")
        return

    await enlace_expediente.click()
    await asyncio.sleep(3)

    # 1. Extracción de Domicilio Fiscal
    enlace_direcciones = page.locator("a.menuitem[href*='datosdirecciones.do']").first
    if await enlace_direcciones.count() > 0:
        await enlace_direcciones.click()
        await asyncio.sleep(2)

    enlace_domicilio = page.locator("a[href*='datosdirecciones.do?consultar=S']").first
    if await enlace_domicilio.count() > 0:
        await enlace_domicilio.click()
        await asyncio.sleep(2)

    dir_info = await page.evaluate(r"""() => {
        const tables = document.querySelectorAll('table');
        let t = Array.from(tables).find(tbl => tbl.innerText.includes('Tipo de Dirección') && tbl.innerText.includes('Estado'));
        if (!t) return null;
        const trs = t.querySelectorAll('tr');
        let resultados = [];
        for (let i = 0; i < trs.length - 1; i++) {
            let currentTr = trs[i];
            let nextTr = trs[i+1];
            let subs = Array.from(currentTr.querySelectorAll('.tablaSubTitulo')).map(e => e.innerText.replace(/\u00a0/g, ' ').trim());
            let smalls = Array.from(nextTr.querySelectorAll('.letrasSmall')).map(e => e.innerText.replace(/\u00a0/g, ' ').trim());
            if (subs.length > 0 && smalls.length > 0) {
                for (let j = 0; j < subs.length; j++) {
                    let header = subs[j] ? subs[j] : '';
                    let val = smalls[j] || 'NO INDICA';
                    if (header) {
                        resultados.push({ etiqueta: header, valor: val.toUpperCase() });
                    }
                }
            }
        }
        return resultados;
    }""")

    # 2. Extracción de Datos Básicos
    await page.goto(f"http://contribuyente.seniat.gob.ve/rifconsulta/datosbasicos.do", timeout=30000)
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)

    datos_basicos = await page.evaluate(r"""() => {
        let info = { nombre_razon: 'NO INDICA', fecha_nacimiento: 'NO INDICA', correo: 'NO INDICA', estado_rif: 'NO INDICA' };
        
        const tds = document.querySelectorAll('td, th');
        tds.forEach((td) => {
            let txt = td.innerText.replace(/\u00a0/g, ' ').trim().toUpperCase();
            
            if (txt.includes('NOMBRE / RAZÓN SOCIAL') || txt.includes('NOMBRE') || txt.includes('RAZÓN SOCIAL')) {
                const table = td.closest('table');
                if (table) {
                    const filas = table.querySelectorAll('tr');
                    for (let r = 0; r < filas.length; r++) {
                        if (filas[r].contains(td) && r + 1 < filas.length) {
                            const celdasVal = filas[r + 1].querySelectorAll('td');
                            const celdasTit = td.parentElement.querySelectorAll('td, th');
                            let colIdx = Array.from(celdasTit).indexOf(td);
                            if (celdasVal[colIdx]) {
                                let val = celdasVal[colIdx].innerText.replace(/\u00a0/g, ' ').trim();
                                if (val) info.nombre_razon = val;
                            }
                        }
                    }
                }
            }

            if (txt === 'FECHA DE NACIMIENTO' || txt.includes('CONSTITUCIÓN')) {
                const table = td.closest('table');
                if (table) {
                    const filas = table.querySelectorAll('tr');
                    for (let r = 0; r < filas.length; r++) {
                        if (filas[r].contains(td) && r + 1 < filas.length) {
                            const celdasValor = filas[r + 1].querySelectorAll('td');
                            const celdasTitulo = td.parentElement.querySelectorAll('td, th');
                            let colIndex = Array.from(celdasTitulo).indexOf(td);
                            if (celdasValor[colIndex]) {
                                let val = celdasValor[colIndex].innerText.replace(/\u00a0/g, ' ').trim();
                                if (val) info.fecha_nacimiento = val;
                            }
                        }
                    }
                }
            }
            
            if (txt.includes('CORREO ELECTRÓNICO') || txt.includes('CORREO')) {
                const table = td.closest('table');
                if (table) {
                    const filas = table.querySelectorAll('tr');
                    for (let r = 0; r < filas.length; r++) {
                        if (filas[r].contains(td) && r + 1 < filas.length) {
                            const celdasValor = filas[r + 1].querySelectorAll('td');
                            celdasValor.forEach(cv => {
                                let val = cv.innerText.replace(/\u00a0/g, ' ').trim();
                                if (val.includes('@')) {
                                    info.correo = val;
                                }
                            });
                        }
                    }
                }
            }
        });
        
        return info;
    }""")

    if datos_basicos['fecha_nacimiento'] == 'NO INDICA':
        page_text = await page.inner_text('body')
        match_fn = re.search(r'(?:Fecha de Nacimiento|Nacimiento|Constitución)[\s:]*([0-9]{2}[-/][0-9]{2}[-/][0-9]{4})', page_text, re.IGNORECASE)
        if match_fn:
            datos_basicos['fecha_nacimiento'] = match_fn.group(1)

    if datos_basicos['correo'] == 'NO INDICA':
        page_text = await page.inner_text('body')
        match_mail = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', page_text)
        if match_mail:
            datos_basicos['correo'] = match_mail.group(0)

    edad_calculada = calcular_edad(datos_basicos['fecha_nacimiento'])

    cargas_familiares = []
    relaciones_empresa = []

    if tipo_consulta == "CEDULA":
        await page.goto(f"http://contribuyente.seniat.gob.ve/rifconsulta/datoscargasfamiliares.do", timeout=30000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        cargas_familiares = []
        links_count = await page.locator('a[href*="consultar=S&idCarga="]').count()
        
        for i in range(links_count):
            await page.goto(f"http://contribuyente.seniat.gob.ve/rifconsulta/datoscargasfamiliares.do", timeout=30000)
            await page.wait_for_load_state("networkidle")
            
            links = page.locator('a[href*="consultar=S&idCarga="]')
            link = links.nth(i)
            
            parentesco = (await link.inner_text()).replace('\xa0', ' ').strip()
            
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            
            detalle = await page.evaluate(r"""() => {
                let nombres = 'NO INDICA';
                let cedula = 'NO INDICA';
                let fecha_nacimiento = 'NO INDICA';
                let nro_informe_medico = 'NO INDICA';
                
                const rows = document.querySelectorAll('tr');
                for (let r = 0; r < rows.length; r++) {
                    let cells = rows[r].querySelectorAll('td');
                    for (let c = 0; c < cells.length; c++) {
                        let rawLabel = cells[c].innerText.replace(/\u00a0/g, ' ').trim().toUpperCase();
                        let label = rawLabel.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
                        
                        if (rows[r + 1]) {
                            let nextCells = rows[r + 1].querySelectorAll('td');
                            if (nextCells[c]) {
                                let val = nextCells[c].innerText.replace(/\u00a0/g, ' ').trim();
                                
                                if (label.includes('NOMBRES') && val && val !== ':') {
                                    nombres = val;
                                }
                                if ((label.includes('CEDULA') || label.includes('C.I.')) && val && val !== ':') {
                                    if (val.length >= 6 && val.length <= 15) {
                                        cedula = val;
                                    }
                                }
                                if (label.includes('FECHA DE NACIMIENTO') && val && val !== ':') {
                                    if (/^\d{2}\/\d{2}\/\d{4}$/.test(val)) {
                                        fecha_nacimiento = val;
                                    }
                                }
                                if ((label.includes('INFORME MEDICO') || label.includes('NRO. INFORME')) && val && val !== ':') {
                                    if (val && val !== ':') {
                                        nro_informe_medico = val;
                                    }
                                }
                            }
                        }
                    }
                }
                return { nombres, cedula, fecha_nacimiento, nro_informe_medico };
            }""")
            
            cargas_familiares.append({
                "parentesco": parentesco,
                "nombres": detalle["nombres"],
                "cedula": detalle["cedula"],
                "fecha_nacimiento": detalle["fecha_nacimiento"],
                "nro_informe_medico": detalle["nro_informe_medico"]
            })

        for carga in cargas_familiares:
            if carga.get('fecha_nacimiento') and carga['fecha_nacimiento'] != 'NO INDICA':
                carga['edad'] = calcular_edad(carga['fecha_nacimiento'])
            else:
                carga['edad'] = 'No calculable'
                
    if tipo_consulta == "RIF":
        await page.goto(f"http://contribuyente.seniat.gob.ve/rifconsulta/datosrelaciones.do", timeout=30000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        relaciones_empresa = await page.evaluate(r"""() => {
            let rels = [];
            const trs = document.querySelectorAll('tr.letrasLista, tr.tablaFilas');
            for (const tr of trs) {
                const tds = tr.querySelectorAll('td');
                if (tds.length >= 2) {
                    let cols = Array.from(tds).map(td => td.innerText.replace(/\u00a0/g, ' ').trim());
                    if (cols.some(c => c.length > 0)) {
                        rels.push({
                            rif_ci: cols[0] || 'NO INDICA',
                            nombre: cols[1] || 'NO INDICA',
                            relacion: cols[2] || 'NO INDICA',
                            fecha: cols[3] || 'NO INDICA'
                        });
                    }
                }
            }
            return rels;
        }""")

    nombre_persona, res_seniat_empresa, relaciones_publicas = "No especificado", "", []
    ivss_estructurado, res_ivss = [], ""
    telefonos_registrados = []
    
    if tipo_consulta == "CEDULA":
        nombre_persona, res_seniat_empresa, relaciones_publicas = await asyncio.to_thread(consultar_seniat_y_nombre, target_val)
        ivss_estructurado, res_ivss = await asyncio.to_thread(consultar_ivss, target_val)
        telefonos_registrados = await asyncio.to_thread(consultar_telefono_mysql, target_val)

    nombre_final = nombre_persona if (nombre_persona and nombre_persona != "No especificado") else datos_basicos['nombre_razon']
    cedula_rif_str = f"V-{target_val}" if tipo_consulta == "CEDULA" else target_val

    # ==========================================================================
    # CONSTRUCCIÓN DEL MENSAJE TELEGRAM
    # ==========================================================================
    mensaje_final = f"📋 *RESULTADO SIVIC*\n"
    mensaje_final += f"─────---------─────\n"
    mensaje_final += f"👤 *Nombre / Razón Social:* {nombre_final}\n"
    if tipo_consulta == "CEDULA":
        mensaje_final += f"📅 *Fecha de Nacimiento:* {datos_basicos['fecha_nacimiento']}\n"
        mensaje_final += f"⏳ *Edad:* {edad_calculada}\n"
        mensaje_final += f"🪪 *Cédula de Identidad:* `{cedula_rif_str}`\n"
        
        mensaje_final += f"📞 *Teléfono(s) Asociado(s):*\n"
        if telefonos_registrados:
            for tlf in telefonos_registrados:
                mensaje_final += f"  • `{tlf}`\n"
        else:
            mensaje_final += "  • ℹ️ *No se encontraron registros telefónicos.*\n"
        mensaje_final += "\n"
    else:
        mensaje_final += f"📅 *Fecha de Constitución:* {datos_basicos['fecha_nacimiento']}\n"
        mensaje_final += f"🪪 *RIF:* `{cedula_rif_str}`\n"
    
    if datos_basicos['correo'] and datos_basicos['correo'] != 'NO INDICA':
        mensaje_final += f"📧 *Correo:* `{datos_basicos['correo']}`\n"
    mensaje_final += "\n"

    mensaje_final += "📍 *DOMICILIO FISCAL / DIRECCIONES*\n"
    if dir_info:
        for item in dir_info:
            etiqueta = item.get("etiqueta", "").title()
            valor = item.get("valor", "NO INDICA")
            if etiqueta.strip() and valor.strip():
                mensaje_final += f"• *{etiqueta}:* {valor}\n"
        mensaje_final += "\n"
    else:
        mensaje_final += "ℹ️ *Sin información detallada de domicilio.*\n\n"

    if tipo_consulta == "CEDULA":
        mensaje_final += "👨‍👩‍👧‍👦 *CARGAS FAMILIARES*\n"
        if cargas_familiares:
            for idx, carga in enumerate(cargas_familiares, 1):
                mensaje_final += f"  • *Parentesco:* {carga.get('parentesco', 'N/A')}\n"
                mensaje_final += f"    *Nombre:* {carga.get('nombres', 'N/A')}\n"
                mensaje_final += f"    *Cédula de Identidad:* `{carga.get('cedula', 'N/A')}`\n"
                mensaje_final += f"    *F. Nacimiento:* {carga.get('fecha_nacimiento', 'N/A')}\n"
                if carga.get('edad') and carga['edad'] != 'No calculable':
                    mensaje_final += f"    *Edad:* {carga['edad']}\n"
                mensaje_final += f"    *Nro. Informe Médico:* `{carga.get('nro_informe_medico', 'N/A')}`\n"
                mensaje_final += "\n"
        else:
            mensaje_final += "ℹ️ *No posee cargas familiares registradas.*\n\n"

    mensaje_final += "🏢 *EMPRESAS / RELACIONES (SENIAT)*\n"
    lista_emp_usar = relaciones_empresa if tipo_consulta == "RIF" else relaciones_publicas
    if lista_emp_usar:
        for rel in lista_emp_usar:
            rif_c = rel.get('rif') or rel.get('rif_ci') or 'N/A'
            nom_c = rel.get('nombre') or 'N/A'
            tip_c = rel.get('tipo') or rel.get('relacion') or 'N/A'
            fec_c = rel.get('fecha') or 'N/A'
            mensaje_final += f"  • *RIF/CI:* `{rif_c}`\n"
            mensaje_final += f"    *Nombre:* {nom_c}\n"
            mensaje_final += f"    *Relación:* {tip_c}\n"
            if fec_c != 'NO INDICA' and fec_c != 'N/A':
                mensaje_final += f"    *Fecha:* {fec_c}\n"
            mensaje_final += "\n"
    else:
        mensaje_final += "ℹ️ *No se encontraron empresas o relaciones asociadas.*\n\n"

    if tipo_consulta == "CEDULA":
        mensaje_final += res_ivss
    else:
        mensaje_final += "🛡️ *IVSS SEGURO:* No aplica para persona jurídica."

    try:
        await update.get_bot().delete_message(chat_id=update.effective_chat.id, message_id=msg_espera.message_id)
    except Exception:
        pass

    await update.message.reply_text(mensaje_final, parse_mode="Markdown")

    try:
        nom_archivo = target_val if tipo_consulta == "RIF" else f"V{target_val}"
        archivo_pdf = await asyncio.to_thread(
            generar_documento_pdf,
            identificador=nom_archivo,
            tipo_consulta=tipo_consulta,
            nombre=nombre_final,
            fecha_nacimiento=datos_basicos['fecha_nacimiento'],
            edad=edad_calculada if tipo_consulta == "CEDULA" else "N/A",
            cedula_rif=cedula_rif_str,
            correo=datos_basicos['correo'],
            direcciones=dir_info,
            cargas_familiares=cargas_familiares,
            empresas_relaciones=lista_emp_usar,
            ivss_info=ivss_estructurado if tipo_consulta == "CEDULA" else [],
            telefonos=telefonos_registrados
        )
        
        with open(archivo_pdf, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                caption=f"📄 *Reporte PDF generado para {nom_archivo}*",
                parse_mode="Markdown"
            )
        if os.path.exists(archivo_pdf):
            os.remove(archivo_pdf)
    except Exception as e:
        logger.error(f"Error generando o enviando el archivo .pdf: {e}")


# ==============================================================================
# MANEJADOR DE MENSAJES Y ACCESO
# ==============================================================================
async def manejar_cedula_o_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    usuarios_permitidos = cargar_usuarios_autorizados()
    
    if user.id not in usuarios_permitidos:
        await update.message.reply_text(
            "⛔ *Acceso Denegado*\n\nNo estás autorizado para utilizar este bot.\n"
            f"Tu ID de Telegram es: `{user.id}`\nComunícate con el administrador para solicitar acceso.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    raw_text = update.message.text.strip()
    texto_limpio = re.sub(r"[^A-Z0-9]", "", raw_text.upper())

    es_telefono = False
    digitos_puros = re.sub(r"\D", "", raw_text)
    
    if (texto_limpio.startswith("04") and len(digitos_puros) >= 10) or (len(digitos_puros) == 10 and not texto_limpio.startswith("V") and not texto_limpio.startswith("J")):
        es_telefono = True

    if es_telefono:
        await update.message.reply_text(f"🔍 Ejecutando búsqueda para el teléfono `{digitos_puros}`...", parse_mode="Markdown")
        
        resultados_inv = await asyncio.to_thread(consultar_inversa_mysql, digitos_puros)
        
        mensaje_res = f"📱 *RESULTADO DE BÚSQUEDA*\n"
        mensaje_res += f"───────────────────────\n"
        mensaje_res += f"📞 *Teléfono consultado:* `{digitos_puros}`\n\n"
        
        if resultados_inv and isinstance(resultados_inv, list) and len(resultados_inv) > 0:
            mensaje_res += "👥 *Registros Asociados en Base de Datos:*\n"
            for item in resultados_inv:
                ced = item.get("cedula", "N/A")
                num = item.get("numero", "N/A")
                mensaje_res += f"  • *Cédula Encontrada:* `V-{ced}`\n"
                mensaje_res += f"    *Teléfono en sistema:* `{num}`\n\n"
            mensaje_res += "💡 *Tip:* Puedes enviar esta cédula directamente al bot para generar el reporte completo del SENIAT e IVSS."
        else:
            mensaje_res += "ℹ️ *No se encontraron registros asociados a este número telefónico en la base de datos.*"
            
        await update.message.reply_text(mensaje_res, parse_mode="Markdown")
        return ConversationHandler.END

    tipo_consulta = "CEDULA"
    parametro_busqueda = ""

    if any(texto_limpio.startswith(pref) for pref in ["J", "G", "C"]):
        tipo_consulta = "RIF"
        parametro_busqueda = texto_limpio
    elif texto_limpio.startswith("V") or texto_limpio.startswith("E"):
        if len(texto_limpio) >= 10:
            tipo_consulta = "RIF"
            parametro_busqueda = texto_limpio
        else:
            tipo_consulta = "CEDULA"
            parametro_busqueda = re.sub(r"\D", "", texto_limpio)
    elif texto_limpio.isdigit():
        tipo_consulta = "CEDULA"
        parametro_busqueda = texto_limpio

    if not parametro_busqueda or len(parametro_busqueda) < 6:
        await update.message.reply_text("👋 Envíame un número de Cédula, un RIF de Empresa o un **Número Telefónico** para realizar búsqueda inversa.", parse_mode="Markdown")
        return ConversationHandler.END

    registrar_log_consulta(user, parametro_busqueda, tipo_consulta)

    msg_espera = await update.message.reply_text(f"🔄 Procesando consulta de {tipo_consulta} (`{parametro_busqueda}`)... (Esperando turno en cola).", parse_mode="Markdown")
    
    async with consulta_lock:
        try:
            page = await get_or_create_page()
            tiempo_transcurrido = time.time() - _last_successful_login
            sesion_vigente = tiempo_transcurrido < SESSION_DURATION

            necesita_login = True
            if sesion_vigente:
                try:
                    await page.goto("http://contribuyente.seniat.gob.ve/rifconsulta/login.do", timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=8000)
                    if "iseniatlogin" not in page.url and (await page.locator("input[name='cedula']").count() > 0 or await page.locator("input[name='rif']").count() > 0):
                        necesita_login = False
                except Exception:
                    necesita_login = True

            if necesita_login:
                await page.goto("http://contribuyente.seniat.gob.ve/iseniatlogin/contribuyente.do", timeout=60000)
                await page.wait_for_load_state("networkidle")

                captcha_img = page.locator("img[src*='kapatch']")
                await captcha_img.wait_for(timeout=10000)
                captcha_bytes = await captcha_img.screenshot()

                context.user_data["pending_target"] = parametro_busqueda
                context.user_data["pending_tipo"] = tipo_consulta

                try:
                    await update.get_bot().delete_message(chat_id=update.effective_chat.id, message_id=msg_espera.message_id)
                except Exception:
                    pass

                file_stream = io.BytesIO(captcha_bytes)
                file_stream.name = "captcha_seniat.jpg"

                sent_msg = await update.message.reply_photo(
                    photo=file_stream,
                    caption=f"🧩 *Sesión expirada.* \n\nPor favor, **responde a este mensaje** con los caracteres del captcha para continuar con la consulta `{parametro_busqueda}`:",
                    parse_mode="Markdown"
                )
                context.user_data["captcha_msg_id"] = sent_msg.message_id
                return ESPERANDO_CAPTCHA
            else:
                await ejecutar_consulta_rif(page, parametro_busqueda, tipo_consulta, update, msg_espera)
                return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error: {e}")
            try:
                await update.get_bot().delete_message(chat_id=update.effective_chat.id, message_id=msg_espera.message_id)
            except Exception:
                pass
            await update.message.reply_text("❌ Ocurrió un error vuelve a intentarlo.")
            return ConversationHandler.END


async def recibir_captcha_y_consultar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codigo_usuario = update.message.text.strip()
    target_val = context.user_data.get("pending_target")
    tipo_consulta = context.user_data.get("pending_tipo", "CEDULA")
    captcha_msg_id = context.user_data.get("captcha_msg_id")

    if captcha_msg_id:
        try:
            await update.get_bot().delete_message(chat_id=update.effective_chat.id, message_id=captcha_msg_id)
        except Exception:
            pass

    try:
        await update.message.delete()
    except Exception:
        pass

    if not target_val:
        await update.message.reply_text("⚠️ Sesión expirada. Envía la consulta nuevamente.")
        return ConversationHandler.END

    msg_espera = await update.message.reply_text("🔄 Procesando consulta...")

    async with consulta_lock:
        try:
            page = await get_or_create_page()
            await page.fill("input[name='usuario']", SENIAT_USER)
            await page.fill("input[name='clave']", SENIAT_PASS)
            await page.fill("input[name='kaptcha']", codigo_usuario)

            await page.evaluate("validarIngreso()")
            await asyncio.sleep(4) 

            error_locator = page.locator("#mensajeError")
            error_text = await error_locator.inner_text() if await error_locator.count() > 0 else ""
            
            if error_text.strip() and "incorrecto" in error_text.lower():
                try:
                    await update.get_bot().delete_message(chat_id=update.effective_chat.id, message_id=msg_espera.message_id)
                except Exception:
                    pass
                if os.path.exists(SESSION_FILE):
                    os.remove(SESSION_FILE)
                await update.message.reply_text(f"❌ El SENIAT rechazó el acceso: _{error_text.strip()}_. Envía la consulta de novo.", parse_mode="Markdown")
                context.user_data.clear()
                return ConversationHandler.END

            await guardar_estado_sesion(page)
            await ejecutar_consulta_rif(page, target_val, tipo_consulta, update, msg_espera)

        except Exception as e:
            logger.error(f"Error tras captcha: {e}")
            try:
                await update.get_bot().delete_message(chat_id=update.effective_chat.id, message_id=msg_espera.message_id)
            except Exception:
                pass
            await update.message.reply_text("❌ Ocurrió un error vuelve a intentarlo.")
        finally:
            context.user_data.clear()

    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("autorizar", cmd_autorizar))
    app.add_handler(CommandHandler("revocar", cmd_revocar))
    app.add_handler(CommandHandler("logs", cmd_logs))

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & (~filters.COMMAND), manejar_cedula_o_texto)],
        states={
            ESPERANDO_CAPTCHA: [MessageHandler(filters.TEXT & (~filters.COMMAND), recibir_captcha_y_consultar)]
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    app.add_handler(conv_handler)

    print("🤖 Bot activo con notificaciones al administrador en tiempo real...")
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
