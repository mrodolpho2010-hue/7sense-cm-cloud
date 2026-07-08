import os
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, jsonify, render_template_string, send_file, g
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO
from urllib.parse import quote
from html import escape
import qrcode
import base64
import re
from PIL import Image, ImageDraw, ImageFont

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # Ambiente local sem PostgreSQL instalado
    psycopg = None

APP_VERSION = "3.2.1 Ciclo de vida dos contratos"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(DATA_DIR, "7sense_cm.sqlite3"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

# Supabase geralmente exige SSL. Se a URL não trouxer sslmode, adicionamos automaticamente.
if USE_POSTGRES and "sslmode=" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "7sense-dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # limite de upload para foto de instalação

SERVICOS = ["IA Segurança", "Timelapse", "IA BIM", "Acompanhamento de Valas", "Controle de Pessoas", "Monitoramento de Equipamentos", "Outro"]
STATUS_CONTRATO = ["Planejamento", "Implantação", "Operação", "Manutenção", "Encerrado"]
STATUS_CAMERA = ["Aguardando teste", "Testada e aprovada", "Reservada", "Em transporte", "Na obra aguardando instalação", "Instalando", "Em operação", "Aguardando retirada", "Em retorno", "Offline", "Em manutenção", "Inutilizada"]
STATUS_ATENCAO = ["Offline", "Em manutenção"]


def _pg_sql(sql):
    """Adapta SQL simples do SQLite para PostgreSQL sem mudar o restante do sistema."""
    if not USE_POSTGRES:
        return sql
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("SELECT last_insert_rowid()", "SELECT lastval()")
    sql = sql.replace("?", "%s")
    return sql


def db():
    if USE_POSTGRES:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL definido, mas o driver psycopg não está instalado. Verifique requirements.txt")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def execute(sql, params=()):
    con = db()
    try:
        if USE_POSTGRES:
            cur = con.cursor()
            cur.execute(_pg_sql(sql), params)
            con.commit()
            return cur
        cur = con.execute(sql, params)
        con.commit()
        return cur
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            con.close()
        except Exception:
            pass


def query(sql, params=()):
    con = db()
    try:
        if USE_POSTGRES:
            cur = con.cursor()
            cur.execute(_pg_sql(sql), params)
            return cur.fetchall()
        return con.execute(sql, params).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass


def one(sql, params=()):
    con = db()
    try:
        if USE_POSTGRES:
            cur = con.cursor()
            cur.execute(_pg_sql(sql), params)
            return cur.fetchone()
        return con.execute(sql, params).fetchone()
    finally:
        try:
            con.close()
        except Exception:
            pass


def init_db():
    execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, password_hash TEXT, role TEXT, active INTEGER DEFAULT 1
    )""")
    execute("""CREATE TABLE IF NOT EXISTS clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, fantasy TEXT, cnpj TEXT, responsible TEXT, phone TEXT, email TEXT, city TEXT, state TEXT, notes TEXT, active INTEGER DEFAULT 1, demo INTEGER DEFAULT 0, created_at TEXT
    )""")
    execute("""CREATE TABLE IF NOT EXISTS contracts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, client_id INTEGER, obra TEXT, city TEXT, state TEXT, start_date TEXT, end_date TEXT, expected_cameras INTEGER DEFAULT 0, status TEXT, notes TEXT, demo INTEGER DEFAULT 0, created_at TEXT
    )""")
    # V3.2.1: ciclo de vida do contrato e encerramento operacional
    for col in [
        "closed_at TEXT",
        "closed_reason TEXT",
        "closed_notes TEXT",
        "closed_by TEXT",
    ]:
        try:
            execute(f"ALTER TABLE contracts ADD COLUMN {col}")
        except Exception:
            pass
    execute("""CREATE TABLE IF NOT EXISTS cameras(
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, model TEXT, serial TEXT, contract_id INTEGER, current_location TEXT, service TEXT, status TEXT, notes TEXT, demo INTEGER DEFAULT 0, updated_at TEXT, created_at TEXT
    )""")
    # Migração leve para versões antigas do SQLite: adiciona campos de teste/aprovação se ainda não existirem.
    try:
        execute("ALTER TABLE cameras ADD COLUMN tested_approved_at TEXT")
    except Exception:
        pass
    try:
        execute("ALTER TABLE cameras ADD COLUMN tested_checklist TEXT")
    except Exception:
        pass
    # V1.9: foto opcional da última instalação, salva como texto base64 no banco permanente.
    try:
        execute("ALTER TABLE cameras ADD COLUMN last_install_photo TEXT")
    except Exception:
        pass
    # V2.0: governança de retirada e status patrimonial simples
    for col in [
        "removal_authorized_at TEXT",
        "removal_authorized_by TEXT",
        "patrimonial_status TEXT",
    ]:
        try:
            execute(f"ALTER TABLE cameras ADD COLUMN {col}")
        except Exception:
            pass
    execute("""CREATE TABLE IF NOT EXISTS camera_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER, old_contract_id INTEGER, new_contract_id INTEGER, old_location TEXT, new_location TEXT, old_service TEXT, new_service TEXT, old_status TEXT, new_status TEXT, note TEXT, user_name TEXT, created_at TEXT, install_photo TEXT
    )""")
    try:
        execute("ALTER TABLE camera_history ADD COLUMN install_photo TEXT")
    except Exception:
        pass
    execute("""CREATE TABLE IF NOT EXISTS occurrences(
        id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER, title TEXT, problem TEXT, status TEXT, responsible TEXT, notes TEXT, demo INTEGER DEFAULT 0, created_at TEXT, closed_at TEXT, archived_at TEXT, archived_contract_id INTEGER, archived_location TEXT
    )""")
    # V1.9.5: arquivamento de ocorrências por ciclo operacional da câmera.
    # Ao retirar a câmera de uma obra, as ocorrências saem da visão ativa e ficam preservadas no histórico.
    for col in [
        "archived_at TEXT",
        "archived_contract_id INTEGER",
        "archived_location TEXT",
        "resolution_notes TEXT",
        "resolved_by TEXT",
    ]:
        try:
            execute(f"ALTER TABLE occurrences ADD COLUMN {col}")
        except Exception:
            pass
    execute("""CREATE TABLE IF NOT EXISTS agenda(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, event_date TEXT, event_time TEXT, contract_id INTEGER, notes TEXT, demo INTEGER DEFAULT 0, created_at TEXT
    )""")
    if not one("SELECT id FROM users WHERE email=?", ("marcos@7sense.local",)):
        execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)", ("Marcos", "marcos@7sense.local", generate_password_hash("123456"), "operacao"))
    if not one("SELECT id FROM users WHERE email=?", ("diretoria@7sense.local",)):
        execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)", ("Diretoria", "diretoria@7sense.local", generate_password_hash("123456"), "diretoria"))
    # V2.0.5: cada câmera deve estar em um único estado operacional.
    # O estoque físico passa a ser representado por dois estados únicos:
    # "Aguardando teste" e "Testada e aprovada". Assim a soma dos filtros fecha com o total.
    for sql in [
        "UPDATE cameras SET status='Testada e aprovada' WHERE status='Disponível'",
        "UPDATE cameras SET status='Aguardando teste' WHERE status='Em estoque'",
        "UPDATE cameras SET status='Na obra aguardando instalação' WHERE status='Chegou na obra'",
        "UPDATE camera_history SET new_status='Na obra aguardando instalação' WHERE new_status='Chegou na obra'",
        "UPDATE camera_history SET old_status='Na obra aguardando instalação' WHERE old_status='Chegou na obra'",
        "UPDATE cameras SET status='Inutilizada', patrimonial_status='Inutilizada' WHERE status IN ('Aposentada','Descartada')",
        "UPDATE camera_history SET new_status='Inutilizada' WHERE new_status IN ('Aposentada','Descartada')",
        "UPDATE camera_history SET old_status='Inutilizada' WHERE old_status IN ('Aposentada','Descartada')",
        "UPDATE cameras SET patrimonial_status='Em estoque' WHERE status IN ('Aguardando teste','Testada e aprovada') AND (patrimonial_status IS NULL OR patrimonial_status='')",
        "UPDATE cameras SET patrimonial_status='Reservada' WHERE status='Reservada' AND (patrimonial_status IS NULL OR patrimonial_status='')"
    ]:
        try:
            execute(sql)
        except Exception:
            pass


def current_user():
    # Evita abrir várias conexões ao Supabase na mesma página.
    # Antes, cada linha de câmera chamava current_user() e abria uma nova conexão,
    # causando lentidão/timeout no Render com PostgreSQL.
    if "user_id" not in session:
        return None
    if hasattr(g, "current_user_cache"):
        return g.current_user_cache
    g.current_user_cache = one("SELECT * FROM users WHERE id=?", (session["user_id"],))
    return g.current_user_cache


def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrap


def operacao_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if u["role"] != "operacao":
            flash("Acesso somente leitura para diretoria.")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrap


def scalar(row, default=None):
    """Retorna o primeiro valor de uma linha, compatível com sqlite.Row e dict_row do PostgreSQL."""
    if not row:
        return default
    try:
        return row[0]
    except Exception:
        try:
            return next(iter(row.values()))
        except Exception:
            return default




def parse_failed_checklist(checklist_text):
    """Extrai itens reprovados do checklist salvo em texto.
    Formato esperado: Item: Reprovado (observação); Item 2: Aprovado
    Retorna lista de dicts: {item, obs}
    """
    failed = []
    if not checklist_text:
        return failed
    for part in str(checklist_text).split(';'):
        part = part.strip()
        if not part or ': Reprovado' not in part:
            continue
        item = part.split(': Reprovado', 1)[0].strip()
        obs = ''
        m = re.search(r'\((.*?)\)', part)
        if m:
            obs = m.group(1).strip()
        if item:
            failed.append({'item': item, 'obs': obs})
    return failed

def count(sql, params=()):
    r = one(sql, params)
    return scalar(r, 0)


def contract_code():
    y = datetime.now().year
    n = count("SELECT COUNT(*) FROM contracts WHERE code LIKE ?", (f"CTR-{y}-%",)) + 1
    return f"CTR-{y}-{n:04d}"


def next_camera_code():
    n = count("SELECT COUNT(*) FROM cameras") + 1
    return f"7S-CAM-{n:03d}"


def status_class(s):
    if s in ("Offline", "Em manutenção"):
        return "danger"
    if s in ("Em implantação", "Instalando", "Chegou na obra"):
        return "warn"
    if s in ("Em transporte", "Aguardando retirada", "Reservada", "Em retorno", "Na obra aguardando instalação", "Instalando"):
        return "info"
    if s in ("Aguardando teste", "Em estoque", "Disponível", "Aposentada", "Descartada", "Inutilizada"):
        return "muted"
    return "ok"


def can_send_to_field(status):
    """Regra operacional: somente câmeras testadas e aprovadas podem iniciar novo envio para obra."""
    return status in ("Testada e aprovada", "Reservada")


def field_statuses():
    return ["Em transporte", "Na obra aguardando instalação", "Instalando", "Em operação", "Aguardando retirada"]


def rv(row, key, default=""):
    """Lê um campo de sqlite.Row sem quebrar quando a consulta não trouxe a coluna."""
    try:
        v = row[key]
        return v if v is not None else default
    except Exception:
        return default



def parse_iso_date(value):
    """Converte data YYYY-MM-DD em date, retornando None se estiver vazia/inválida."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def contract_deadline_info(contract):
    """Retorna (texto, classe_badge, dias) para o prazo do contrato."""
    if rv(contract, 'status', '') == 'Encerrado':
        return ('Encerrado', 'muted', None)
    end = parse_iso_date(rv(contract, 'end_date', ''))
    if not end:
        return ('Sem data fim', 'muted', None)
    days = (end - date.today()).days
    if days < 0:
        return (f'Vencido há {abs(days)} dia(s)', 'danger', days)
    if days == 0:
        return ('Vence hoje', 'danger', days)
    if days <= 30:
        return (f'Vence em {days} dia(s)', 'orange', days)
    if days <= 60:
        return (f'Vence em {days} dia(s)', 'warn', days)
    return (f'Vigente · {days} dia(s)', 'ok', days)


def contract_deadline_summary(demo_flag=0):
    """Conta contratos vencidos, vencendo em 30 dias e vencendo em 60 dias."""
    rows = query("SELECT * FROM contracts WHERE demo=? AND status!='Encerrado'", (demo_flag,))
    summary = {'expired': 0, 'd30': 0, 'd60': 0, 'ok': 0, 'no_date': 0}
    for r in rows:
        _, cls, days = contract_deadline_info(r)
        if days is None:
            summary['no_date'] += 1
        elif days < 0:
            summary['expired'] += 1
        elif days <= 30:
            summary['d30'] += 1
        elif days <= 60:
            summary['d60'] += 1
        else:
            summary['ok'] += 1
    summary['attention'] = summary['expired'] + summary['d30'] + summary['d60']
    return summary

def is_demo_mode():
    """V2.0: o sistema trabalha somente em Operação Oficial."""
    return False


def current_demo_flag():
    return 0


def mode_label():
    return "OPERAÇÃO"


def demo_where(alias=None):
    prefix = f"{alias}." if alias else ""
    return f"{prefix}demo=?", (current_demo_flag(),)


BASE = r"""
<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>7Sense Operations Manager</title>
<style>
:root{
  --bg:#08111f;--bg2:#0b1628;--sidebar:#07101d;--card:#0f1b2d;--card2:#111f35;--text:#e5edf8;--muted:#8da0ba;--border:#203149;
  --accent:#1f6fff;--accent2:#38bdf8;--ok:#22c55e;--danger:#ef4444;--warn:#f59e0b;--info:#3b82f6;--purple:#8b5cf6;
  --softdanger:rgba(239,68,68,.14);--softwarn:rgba(245,158,11,.14);--softok:rgba(34,197,94,.14);--softinfo:rgba(59,130,246,.14);--shadow:0 18px 45px rgba(0,0,0,.28)
}
*{box-sizing:border-box} html,body{min-height:100%} body{margin:0;font-family:Inter,Manrope,Segoe UI,Arial,Helvetica,sans-serif;background:radial-gradient(circle at top left,rgba(31,111,255,.18),transparent 28%),linear-gradient(135deg,var(--bg),var(--bg2));color:var(--text)} a{text-decoration:none;color:inherit} small{color:var(--muted)}
.app-shell{display:flex;min-height:100vh}.sidebar{position:fixed;left:0;top:0;bottom:0;width:245px;background:linear-gradient(180deg,rgba(7,16,29,.98),rgba(5,12,24,.98));border-right:1px solid var(--border);padding:22px 14px;display:flex;flex-direction:column;gap:18px}.logo{display:flex;align-items:center;gap:12px;padding:0 8px 16px}.logo-mark{font-size:38px;font-weight:950;line-height:1;color:#1f6fff;text-shadow:0 0 24px rgba(31,111,255,.35)}.logo-title{font-size:21px;font-weight:900}.logo-sub{font-size:12px;color:var(--muted);margin-top:2px}.nav{display:flex;flex-direction:column;gap:8px}.nav-sep{height:1px;background:rgba(32,49,73,.85);margin:6px 8px}.nav a,.settings>a{display:flex;align-items:center;gap:10px;border:1px solid transparent;background:transparent;border-radius:13px;padding:12px 13px;font-size:15px;color:#dbeafe}.nav a:hover,.settings>a:hover{background:rgba(31,111,255,.12);border-color:rgba(31,111,255,.3)}.nav a:first-child{background:linear-gradient(135deg,rgba(31,111,255,.28),rgba(31,111,255,.08));border-color:rgba(59,130,246,.55);box-shadow:inset 0 0 18px rgba(31,111,255,.12)}.side-footer{margin-top:auto;background:rgba(15,27,45,.8);border:1px solid var(--border);border-radius:16px;padding:14px}.avatar{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,var(--accent),#0ea5e9);display:inline-flex;align-items:center;justify-content:center;font-weight:800;margin-right:10px}.version{margin-top:14px;color:var(--muted);font-size:12px;text-align:center}
.main{margin-left:245px;width:calc(100% - 245px);padding:26px 28px 40px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:20px}.brand{font-size:26px;font-weight:900;letter-spacing:-.03em}.tag{color:var(--muted);font-size:14px;line-height:1.45}.breadcrumb{font-size:13px;color:var(--muted);margin-top:6px}.top-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.pill{border:1px solid var(--border);background:rgba(15,27,45,.78);border-radius:13px;padding:10px 14px;color:#cbd5e1}.wrap{max-width:1480px;margin:0 auto}.btn,.nav a,button{border:1px solid var(--border);background:rgba(15,27,45,.88);border-radius:12px;padding:10px 14px;font-size:14px;cursor:pointer;color:var(--text)}button:hover,.btn:hover{border-color:rgba(59,130,246,.75);background:rgba(31,111,255,.12)}.btn.primary,button.primary{background:linear-gradient(135deg,var(--accent),#0ea5e9);color:white;border-color:#3384ff}.btn.danger,button.danger{background:rgba(239,68,68,.18);color:#fecaca;border-color:rgba(239,68,68,.45)}.btn.small{padding:7px 10px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(5,minmax(145px,1fr));gap:14px}.card,.panel{background:linear-gradient(180deg,rgba(17,31,53,.92),rgba(12,24,42,.92));border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow)}.card{padding:18px}.panel{padding:18px;margin-top:16px}.panel h2{margin:0 0 12px;font-size:22px;letter-spacing:-.02em}.metric{display:block;position:relative;overflow:hidden;min-height:118px}.metric:before{content:"";position:absolute;right:-35px;top:-35px;width:110px;height:110px;border-radius:50%;background:rgba(31,111,255,.12)}.metric h3{margin:0 0 8px;font-size:15px;color:#dbeafe;max-width:160px}.metric b{font-size:38px;line-height:1;font-weight:900}.metric.danger{border-color:rgba(239,68,68,.5);background:linear-gradient(180deg,rgba(69,20,30,.75),rgba(22,15,28,.75))}.metric:hover{transform:translateY(-1px);border-color:rgba(59,130,246,.55);transition:.15s ease}.search{display:flex;gap:8px;flex:1;max-width:420px}input,select,textarea{width:100%;border:1px solid var(--border);border-radius:12px;padding:12px;font-size:15px;background:#0b1628;color:var(--text)}input::placeholder,textarea::placeholder{color:#64748b}textarea{min-height:92px}.row{display:grid;grid-template-columns:1.2fr 1.2fr 1fr .8fr auto;gap:10px;align-items:center;border-bottom:1px solid rgba(32,49,73,.78);padding:13px 4px}.row:last-child{border-bottom:none}.row.camera{grid-template-columns:.95fr 1.45fr 1fr 1fr 1fr 1.6fr}.row.danger{background:rgba(239,68,68,.08);color:#fecaca;border-radius:14px;padding-left:10px}.badge{display:inline-block;padding:6px 10px;border-radius:999px;font-size:12.5px;border:1px solid var(--border);background:rgba(148,163,184,.12);color:#cbd5e1}.badge.ok{background:var(--softok);color:#86efac;border-color:rgba(34,197,94,.35)}.badge.danger{background:var(--softdanger);color:#fca5a5;border-color:rgba(239,68,68,.35)}.badge.warn{background:var(--softwarn);color:#fcd34d;border-color:rgba(245,158,11,.35)}.badge.orange{background:rgba(249,115,22,.18);color:#fdba74;border-color:rgba(249,115,22,.45)}.badge.info{background:var(--softinfo);color:#93c5fd;border-color:rgba(59,130,246,.35)}.badge.muted{background:rgba(148,163,184,.12);color:#cbd5e1}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:13px 0}.filters a{border:1px solid var(--border);background:rgba(15,27,45,.88);padding:9px 13px;border-radius:999px;color:#dbeafe}.filters a.active{background:linear-gradient(135deg,var(--accent),#0ea5e9);color:#fff;border-color:#3384ff}.flash{background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.35);border-radius:14px;padding:11px;margin:12px 0;color:#fde68a}.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.formgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:13px}.full{grid-column:1/-1}.mobile-card{max-width:560px;margin:0 auto}.hero{font-size:22px;font-weight:900}.hidden{display:none!important}.settings{position:relative}.settings-menu{display:none;position:absolute;left:0;bottom:48px;background:#0b1628;border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);min-width:230px;z-index:20;padding:8px}.settings:hover .settings-menu{display:block}.settings-menu a{display:block;border:0;border-radius:10px;padding:10px 12px;background:transparent;color:#dbeafe}.settings-menu a:hover{background:rgba(31,111,255,.12)}details.card,details.panel{padding:16px}summary{list-style:none}summary::-webkit-details-marker{display:none}summary:before{content:'▾';display:inline-block;margin-right:8px;color:#93c5fd}.panel .grid .metric{min-height:112px;text-align:center}.panel .grid .metric h3{margin:auto auto 8px}.panel .grid .metric b{font-size:30px}

.oc-grid{display:grid;grid-template-columns:2fr 1.15fr;gap:16px}.mini-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:16px}.hero-card{display:flex;align-items:center;gap:16px;min-height:124px}.hero-icon{width:64px;height:64px;border-radius:18px;display:flex;align-items:center;justify-content:center;font-size:30px;background:linear-gradient(135deg,rgba(31,111,255,.35),rgba(14,165,233,.15));box-shadow:inset 0 0 22px rgba(255,255,255,.04)}.hero-card.green .hero-icon{background:linear-gradient(135deg,rgba(34,197,94,.35),rgba(34,197,94,.08))}.hero-card.orange .hero-icon{background:linear-gradient(135deg,rgba(245,158,11,.35),rgba(245,158,11,.08))}.hero-card.red .hero-icon{background:linear-gradient(135deg,rgba(239,68,68,.35),rgba(239,68,68,.08))}.hero-card b{font-size:38px}.hero-card span{color:var(--muted);font-size:13px}.flow-board{padding:22px}.flow-line{display:grid;grid-template-columns:repeat(8,1fr);gap:14px;align-items:stretch;margin-top:18px}.flow-step{position:relative;text-align:center;border:1px solid var(--border);border-radius:18px;padding:18px 10px;background:rgba(15,27,45,.72);min-height:142px}.flow-step:after{content:'→';position:absolute;right:-13px;top:48%;color:#64748b}.flow-step:last-child:after{display:none}.flow-step .circle{margin:0 auto 10px;width:54px;height:54px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:26px;background:rgba(31,111,255,.16);border:1px solid rgba(59,130,246,.35)}.flow-step b{display:block;font-size:28px;margin-top:8px}.flow-step small{display:block;min-height:32px}.timeline{position:relative}.timeline-item{display:grid;grid-template-columns:70px 42px 1fr auto;gap:10px;align-items:center;padding:12px 0;border-bottom:1px solid rgba(32,49,73,.7)}.timeline-item:last-child{border-bottom:none}.dot{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:rgba(59,130,246,.18)}.status-list{display:flex;flex-direction:column;gap:12px}.status-row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}.bar{height:8px;background:#13243d;border-radius:999px;overflow:hidden;margin-top:6px}.bar span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#1f6fff,#22c55e)}.bottom-strip{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:16px}.strip-card{display:flex;align-items:center;gap:12px;padding:14px;border:1px solid var(--border);border-radius:18px;background:rgba(15,27,45,.78)}.strip-card b{font-size:22px}.top-search input{height:46px}.app-footer{color:var(--muted);font-size:12px;text-align:center;margin:26px 0 6px;padding-top:18px;border-top:1px solid rgba(32,49,73,.65)}
@media(max-width:1180px){.oc-grid,.mini-grid,.bottom-strip{grid-template-columns:1fr 1fr}.flow-line{grid-template-columns:repeat(4,1fr)}}
@media(max-width:760px){.oc-grid,.mini-grid,.bottom-strip{grid-template-columns:1fr}.flow-line{grid-template-columns:1fr}.flow-step:after{display:none}.timeline-item{grid-template-columns:50px 38px 1fr}}
@media(max-width:980px){.sidebar{position:relative;width:100%;height:auto}.app-shell{display:block}.main{margin-left:0;width:100%;padding:18px}.nav{flex-direction:row;flex-wrap:wrap}.side-footer{display:none}.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:760px){.grid{grid-template-columns:1fr}.row,.row.camera{display:block}.row>*{margin:6px 0}.formgrid{grid-template-columns:1fr}.top{display:block}.top-actions{margin-top:12px}.search{max-width:none;margin-top:10px}}
</style>
</head><body><div class="app-shell">
<aside class="sidebar"><div class="logo"><div class="logo-mark">7</div><div><div class="logo-title">7Sense</div><div class="logo-sub">Operations Manager<br>Data into Action</div></div></div>
<nav class="nav"><a href="{{url_for('dashboard')}}">🏠 Dashboard</a><div class="nav-sep"></div><a href="{{url_for('clients')}}">👥 Clientes</a><a href="{{url_for('contracts')}}">📑 Contratos</a><a href="{{url_for('cameras')}}">📷 Câmeras</a><a href="{{url_for('agenda_page')}}">📅 Agenda Operacional</a><a href="{{url_for('field_links')}}">📱 Operação de Campo</a><div class="nav-sep"></div><a href="{{url_for('occurrences')}}">⚠️ Ocorrências</a><a href="{{url_for('maintenance_page')}}">🔧 Centro de Manutenção</a><div class="nav-sep"></div>{% if user %}<div class="settings"><a href="{{url_for('settings_page')}}">⚙️ Configurações</a><div class="settings-menu"><a href="{{url_for('profile_page')}}">Meu perfil</a><a href="{{url_for('change_password')}}">Alterar senha</a><a href="{{url_for('users_page')}}">Gerenciar usuários</a><a href="{{url_for('clear_database')}}">Limpar banco de dados</a><a href="{{url_for('about_page')}}">Sobre o sistema</a><a href="{{url_for('logout')}}">Sair</a></div></div>{% endif %}</nav>
{% if user %}<div class="side-footer"><span class="avatar">{{user['name'][:1]}}{{user['name'][1:2]}}</span><b>{{user['name']}}</b><div class="tag">{{user['role']}} · Online</div><div class="version">v{{version}}</div></div>{% endif %}</aside>
<main class="main"><div class="wrap"><div class="top"><div><div class="brand">{{breadcrumb.split(' > ')[-1] if breadcrumb else 'Dashboard'}}</div><div class="tag">7Sense Operations Manager · Data into Action</div>{% if user %}<div class="breadcrumb">{{breadcrumb or 'Dashboard'}} · Usuário: {{user['name']}} · Perfil: {{user['role']}}</div>{% endif %}</div><div class="top-actions"><div class="pill">🔔 0</div><div class="pill">📅 Operação</div><a class="pill" href="{{url_for('settings_page')}}">⚙️ Configurações</a></div></div>
{% with messages = get_flashed_messages() %}{% if messages %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endif %}{% endwith %}
{{body|safe}}
<div class="app-footer">7Sense Operations Manager • Versão {{version}} • © 2026 Seven Sense Tecnologia</div>
</div></main></div></body></html>
"""

LOGIN_BASE = r"""
<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Entrar no 7Sense</title>
<style>
:root{--bg:#08111f;--card:#0f1b2d;--text:#e5edf8;--muted:#8da0ba;--border:#203149;--accent:#1f6fff}
*{box-sizing:border-box} body{margin:0;font-family:Inter,Manrope,Segoe UI,Arial,Helvetica,sans-serif;background:radial-gradient(circle at top left,rgba(31,111,255,.25),transparent 30%),linear-gradient(135deg,#08111f,#0b1628);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}.card{background:linear-gradient(180deg,rgba(17,31,53,.94),rgba(12,24,42,.94));border:1px solid var(--border);border-radius:24px;padding:30px;width:100%;max-width:520px;box-shadow:0 22px 60px rgba(0,0,0,.35)}.brand{font-weight:950;font-size:36px;margin:0 0 6px;letter-spacing:-.04em}.mark{font-size:46px;color:#1f6fff;font-weight:950;line-height:1}.tag{color:var(--muted);font-size:14px;margin:0 0 22px}label{display:block;margin:14px 0 6px;font-weight:650}input{width:100%;border:1px solid var(--border);border-radius:14px;padding:14px;font-size:16px;background:#0b1628;color:var(--text)}input::placeholder{color:#64748b}.actions{display:flex;gap:12px;align-items:center;margin-top:20px;flex-wrap:wrap}button{border:1px solid #3384ff;background:linear-gradient(135deg,var(--accent),#0ea5e9);color:white;border-radius:13px;padding:12px 20px;font-size:16px;cursor:pointer}.link{color:#93c5fd;font-size:14px}.flash{background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.35);border-radius:12px;padding:10px;margin-bottom:14px;color:#fde68a}.footer{font-size:12px;color:var(--muted);margin-top:22px;text-align:center}.brandline{display:flex;align-items:center;gap:12px;margin-bottom:16px}
</style></head><body>
<div class="card"><div class="brandline"><div class="mark">7</div><div><div class="brand">7Sense</div><p class="tag">Operations Manager · Data into Action</p></div></div>
  {% with messages = get_flashed_messages() %}{% if messages %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endif %}{% endwith %}
  {{body|safe}}
  <div class="footer">Acesso restrito à operação 7Sense.</div>
</div>
</body></html>
"""

def login_page(body):
    return render_template_string(LOGIN_BASE, body=body)


def page(body, breadcrumb="Dashboard", **ctx):
    return render_template_string(BASE, body=body, user=current_user(), version=APP_VERSION, breadcrumb=breadcrumb, mode_label=mode_label(), demo_mode=False, **ctx)


@app.route("/")
def root():
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = one("SELECT * FROM users WHERE email=? AND active=1", (request.form.get("email", "").strip(),))
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session["user_id"] = u["id"]
            return redirect(url_for("dashboard"))
        flash("Usuário ou senha inválidos.")
    body = """
    <h1 style="margin:0 0 8px;font-size:26px">Entrar no 7Sense</h1>
    <p class="tag">Informe seu e-mail e senha para acessar o painel.</p>
    <form method="post">
      <label>Email</label>
      <input name="email" type="email" autocomplete="username" placeholder="seuemail@empresa.com.br" required>
      <label>Senha</label>
      <input name="password" type="password" autocomplete="current-password" placeholder="Digite sua senha" required>
      <div class="actions">
        <button>Entrar</button>
        <a class="link" href="/forgot-password">Esqueci minha senha</a>
      </div>
    </form>
    """
    return login_page(body)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        flash("Solicitação registrada. Procure o administrador do sistema para redefinir sua senha.")
        return redirect(url_for("login"))
    body = """
    <h1 style="margin:0 0 8px;font-size:26px">Recuperar acesso</h1>
    <p class="tag">Informe seu e-mail. Nesta fase, a redefinição será tratada pelo administrador do sistema.</p>
    <form method="post">
      <label>Email</label>
      <input name="email" type="email" placeholder="seuemail@empresa.com.br" required>
      <div class="actions">
        <button>Solicitar redefinição</button>
        <a class="link" href="/login">Voltar ao login</a>
      </div>
    </form>
    """
    return login_page(body)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/settings")
@login_required
def settings_page():
    body = """<div class='panel'><h2>⚙️ Configurações</h2><div class='actions'>
    <a class='btn' href='/profile'>Meu perfil</a><a class='btn' href='/change-password'>Alterar senha</a><a class='btn' href='/users'>Gerenciar usuários</a><a class='btn danger' href='/clear-database'>Limpar banco de dados</a><a class='btn' href='/about'>Sobre o sistema</a><a class='btn' href='/logout'>Sair</a>
    </div></div>"""
    return page(body, breadcrumb="Dashboard > Configurações")

@app.route("/profile")
@login_required
def profile_page():
    u=current_user()
    body=f"""<div class='panel'><h2>Meu perfil</h2><p><b>Nome:</b> {u['name']}</p><p><b>Email:</b> {u['email']}</p><p><b>Perfil:</b> {u['role']}</p></div>"""
    return page(body, breadcrumb="Dashboard > Meu perfil")

@app.route("/change-password", methods=["GET","POST"])
@login_required
def change_password():
    u=current_user()
    if request.method=="POST":
        atual=request.form.get("current_password","")
        nova=request.form.get("new_password","")
        confirma=request.form.get("confirm_password","")
        if not check_password_hash(u["password_hash"], atual):
            flash("Senha atual inválida.")
        elif len(nova)<6:
            flash("A nova senha deve ter pelo menos 6 caracteres.")
        elif nova!=confirma:
            flash("A confirmação não confere.")
        else:
            execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(nova), u["id"]))
            flash("Senha alterada com sucesso.")
            return redirect(url_for("settings_page"))
    body="""<div class='panel'><h2>Alterar senha</h2><form method='post' class='formgrid'><label>Senha atual<input type='password' name='current_password' required></label><label>Nova senha<input type='password' name='new_password' required></label><label>Confirmar nova senha<input type='password' name='confirm_password' required></label><div class='full'><button class='primary'>Salvar nova senha</button></div></form></div>"""
    return page(body, breadcrumb="Dashboard > Alterar senha")

@app.route("/users")
@login_required
def users_page():
    rows=query("SELECT id,name,email,role,active FROM users ORDER BY name")
    items="".join([f"<div class='row'><b>{r['name']}</b><span>{r['email']}</span><span>{r['role']}</span><span>{'Ativo' if r['active'] else 'Inativo'}</span><span></span></div>" for r in rows])
    return page(f"<div class='panel'><h2>Usuários</h2>{items}</div>", breadcrumb="Dashboard > Usuários")

@app.route("/about")
@login_required
def about_page():
    body=f"""<div class='panel'><h2>Sobre o sistema</h2><p><b>7Sense Operations Manager</b></p><p>Versão: {APP_VERSION}</p><p class='tag'>Sistema para gestão operacional de contratos, patrimônio, câmeras, QR Codes, ocorrências e movimentações de campo.</p></div>"""
    return page(body, breadcrumb="Dashboard > Sobre")

@app.route("/clear-database", methods=["GET","POST"])
@operacao_required
def clear_database():
    if request.method=="POST":
        email=request.form.get("admin_email","").strip()
        senha=request.form.get("admin_password","")
        palavra=request.form.get("confirm_text","").strip().upper()
        admin=one("SELECT * FROM users WHERE email=? AND role='operacao' AND active=1", (email,))
        if not admin or not check_password_hash(admin["password_hash"], senha):
            flash("Login ou senha de administrador inválidos.")
        elif palavra!="APAGAR":
            flash("Digite APAGAR para confirmar.")
        else:
            for table in ["agenda","occurrences","camera_history","cameras","contracts","clients"]:
                execute(f"DELETE FROM {table}")
            flash("Banco operacional limpo. Usuários foram preservados.")
            return redirect(url_for("dashboard"))
    body="""<div class='panel'><h2>⚠️ Limpar banco de dados</h2><p>Esta ação apaga clientes, contratos, câmeras, históricos, ocorrências e agenda. Usuários serão preservados.</p><form method='post' class='formgrid'><label>Email do administrador<input name='admin_email' type='email' required></label><label>Senha do administrador<input name='admin_password' type='password' required></label><label class='full'>Digite APAGAR para confirmar<input name='confirm_text' required></label><div class='full'><button class='danger'>Limpar banco de dados</button></div></form></div>"""
    return page(body, breadcrumb="Dashboard > Limpar banco")


@app.route("/app")
@login_required
def dashboard():
    df = 0

    # Otimização v3.1.10:
    # O Dashboard antes fazia uma consulta COUNT separada para cada status.
    # No Render/Supabase isso podia abrir várias conexões e gerar timeout.
    # Agora os status das câmeras são carregados em uma única consulta agrupada.
    status_rows_db = query("SELECT status, COUNT(*) AS total FROM cameras WHERE demo=? GROUP BY status", (df,))
    status_counts = {rv(r, 'status', ''): int(rv(r, 'total', 0) or 0) for r in status_rows_db}
    cams_total = sum(status_counts.values())
    cams_operation = status_counts.get('Em operação', 0)
    cams_ready = status_counts.get('Testada e aprovada', 0)

    contracts_active = count("SELECT COUNT(*) FROM contracts WHERE status!='Encerrado' AND demo=?", (df,))
    deadline_summary = contract_deadline_summary(df)
    occ_open = count("SELECT COUNT(*) FROM occurrences WHERE status IN ('Aberta','Em andamento') AND archived_at IS NULL AND demo=?", (df,))
    today = date.today().isoformat()
    agenda_upcoming = count("SELECT COUNT(*) FROM agenda WHERE event_date>=? AND demo=?", (today, df))

    flow_items = [
        ("Aguardando teste", "🧪", "Aguardando teste"),
        ("Testada e aprovada", "✅", "Testada e aprovada"),
        ("Reservada", "📎", "Reservada"),
        ("Em transporte", "🚚", "Em transporte"),
        ("Na obra", "🏗️", "Na obra aguardando instalação"),
        ("Em operação", "📡", "Em operação"),
        ("Aguardando retirada", "⏬", "Aguardando retirada"),
        ("Em retorno", "↩️", "Em retorno"),
        ("Em manutenção", "🔧", "Em manutenção"),
        ("Inutilizada", "🚫", "Inutilizada"),
    ]
    flow_cards = ""
    flow_counts = []
    for label, icon, status_value in flow_items:
        n = status_counts.get(status_value, 0)
        flow_counts.append((label, n, status_value))
        flow_cards += f"""<a class='flow-step' href='{url_for('cameras', status=status_value)}'><div class='circle'>{icon}</div><small>{label}</small><b>{n}</b></a>"""

    hist = query("""SELECT h.*, c.code camera_code, cl.name client_name, co.obra obra
                    FROM camera_history h
                    LEFT JOIN cameras c ON c.id=h.camera_id
                    LEFT JOIN contracts co ON co.id=COALESCE(h.new_contract_id, c.contract_id)
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    ORDER BY h.created_at DESC LIMIT 5""")
    moves = ""
    for h in hist:
        when = (rv(h, 'created_at') or '')[11:16] or '--:--'
        cam = rv(h, 'camera_code', 'Câmera') or 'Câmera'
        st = rv(h, 'new_status', '') or 'Movimentação'
        obra = rv(h, 'obra', '') or ''
        cliente = rv(h, 'client_name', '') or ''
        context = f"{cliente} · {obra}" if (cliente or obra) else "Registro operacional"
        moves += f"""<div class='timeline-item'><span class='tag'>{when}</span><span class='dot'>📷</span><div><b>{cam} - {st}</b><div class='tag'>{context}</div></div><span class='badge info'>{st[:16]}</span></div>"""
    if not moves:
        moves = "<p class='tag'>Nenhuma movimentação registrada ainda.</p>"

    action_rows = []
    for label, status_value, icon in [("Testar câmeras", "Aguardando teste", "🧪"), ("Enviar para obra", "Testada e aprovada", "🚚"), ("Instalação pendente", "Na obra aguardando instalação", "🏗️"), ("Retirada autorizada", "Aguardando retirada", "↩️")]:
        n = status_counts.get(status_value, 0)
        if n:
            action_rows.append(f"""<a class='timeline-item' href='{url_for('cameras', status=status_value)}'><span class='dot'>{icon}</span><div style='grid-column:span 2'><b>{label}</b><div class='tag'>{n} câmera(s)</div></div><span>›</span></a>""")
    actions = "".join(action_rows) or "<p class='tag'>Nenhuma ação pendente no momento.</p>"

    status_rows = ""
    for label, n, status_value in flow_counts:
        pct = int((n / cams_total) * 100) if cams_total else 0
        status_rows += f"""<a class='status-row' href='{url_for('cameras', status=status_value)}'><div><b>{label}</b><div class='bar'><span style='width:{pct}%'></span></div></div><span>{n} ({pct}%)</span></a>"""

    flow_map = {s:n for s,n,v in flow_counts}
    body = f"""
    <div class="mini-grid">
      <a class="card hero-card" href="{url_for('contracts', status='Ativos')}"><div class="hero-icon">📁</div><div><h3>Contratos ativos</h3><b>{contracts_active}</b><br><span>ver ativos →</span></div></a>
      <a class="card hero-card orange" href="{url_for('contracts', status='A vencer')}"><div class="hero-icon">⏳</div><div><h3>Contratos a vencer</h3><b>{deadline_summary['d60'] + deadline_summary['d30']}</b><br><span>60d: {deadline_summary['d60']} · 30d: {deadline_summary['d30']}</span></div></a>
      <a class="card hero-card green" href="{url_for('cameras', status='Em operação')}"><div class="hero-icon">📷</div><div><h3>Câmeras em operação</h3><b>{cams_operation}</b><br><span>ver câmeras →</span></div></a>
      <a class="card hero-card orange" href="{url_for('agenda_page', filtro='proximos')}"><div class="hero-icon">📅</div><div><h3>Próximos agendamentos</h3><b>{agenda_upcoming}</b><br><span>ver agenda →</span></div></a>
      <a class="card hero-card red" href="{url_for('occurrences', status='abertas')}"><div class="hero-icon">⚠️</div><div><h3>Ocorrências abertas</h3><b>{occ_open}</b><br><span>{'requer atenção' if occ_open else 'sem pendências'}</span></div></a>
    </div>

    <div class="oc-grid">
      <div class="panel flow-board"><div class="actions"><div style="flex:1"><h2>Fluxo operacional das câmeras</h2><p class="tag">Total de câmeras: <b>{cams_total}</b>. Clique em uma etapa para visualizar os detalhes.</p></div><a class="btn" href="{url_for('cameras')}">Ver todas</a><a class="btn primary" href="{url_for('cameras')}">Gerenciar câmeras</a></div><div class="flow-line">{flow_cards}</div></div>
      <div class="panel"><div class="actions"><h2 style="flex:1">Próximas ações</h2><a class="btn small" href="{url_for('agenda_page')}">Agenda</a></div>{actions}</div>
    </div>

    <div class="oc-grid">
      <div class="panel"><div class="actions"><h2 style="flex:1">Últimas movimentações</h2><a class="btn small" href="{url_for('cameras')}">Ver histórico</a></div><div class="timeline">{moves}</div></div>
      <div class="panel"><h2>Distribuição por status</h2><div class="status-list">{status_rows}</div><p class="tag" style="margin-top:16px">Atualizado com dados do Supabase.</p></div>
    </div>

    <div class="bottom-strip">
      <a class="strip-card" href="{url_for('contracts', status='Ativos')}"><span class="dot">🏗️</span><div><b>{contracts_active}</b><div class="tag">Contratos ativos</div></div></a>
      <a class="strip-card" href="{url_for('cameras', status='Testada e aprovada')}"><span class="dot">✅</span><div><b>{cams_ready}</b><div class="tag">Prontas para envio</div></div></a>
      <a class="strip-card" href="{url_for('cameras', status='Na obra aguardando instalação')}"><span class="dot">🏗️</span><div><b>{flow_map.get('Na obra',0)}</b><div class="tag">Na obra</div></div></a>
      <a class="strip-card" href="{url_for('cameras', status='Em retorno')}"><span class="dot">↩️</span><div><b>{flow_map.get('Em retorno',0)}</b><div class="tag">Em retorno</div></div></a>
      <a class="strip-card" href="{url_for('occurrences', status='abertas')}"><span class="dot">⚠️</span><div><b>{occ_open}</b><div class="tag">Ocorrências abertas</div></div></a>
    </div>
    """
    return page(body)




@app.route("/clients")
@login_required
def clients():
    rows = query("SELECT * FROM clients WHERE demo=? ORDER BY active DESC, name", (current_demo_flag(),))
    can_edit = current_user()["role"] == "operacao"
    items = "".join([f"<div class='row'><b>{r['name']}</b><span>{r['city'] or ''}/{r['state'] or ''}</span><span>{r['responsible'] or ''}</span><span>{'Ativo' if r['active'] else 'Inativo'}</span><span class='actions'>{'<a class=\"btn small\" href=\"'+url_for('client_edit', id=r['id'])+'\">Editar</a>' if can_edit else ''}</span></div>" for r in rows])
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>Clientes</h2>{'<a class=\"btn primary\" href=\"'+url_for('client_new')+'\">Novo cliente</a>' if can_edit else ''}</div>{items or '<p>Nenhum cliente.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Clientes")


@app.route("/clients/new", methods=["GET", "POST"])
@operacao_required
def client_new():
    return client_form()


@app.route("/clients/<int:id>/edit", methods=["GET", "POST"])
@operacao_required
def client_edit(id):
    r = one("SELECT * FROM clients WHERE id=?", (id,))
    return client_form(r)


def client_form(r=None):
    if request.method == "POST":
        vals = (request.form.get("name"), request.form.get("fantasy"), request.form.get("cnpj"), request.form.get("responsible"), request.form.get("phone"), request.form.get("email"), request.form.get("city"), request.form.get("state"), request.form.get("notes"), 1 if request.form.get("active") == "1" else 0)
        if r:
            execute("UPDATE clients SET name=?,fantasy=?,cnpj=?,responsible=?,phone=?,email=?,city=?,state=?,notes=?,active=? WHERE id=?", vals+(r["id"],))
            flash("Cliente atualizado.")
        else:
            execute("INSERT INTO clients(name,fantasy,cnpj,responsible,phone,email,city,state,notes,active,demo,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", vals+(current_demo_flag(), datetime.now().isoformat(),))
            flash("Cliente criado.")
        return redirect(url_for("clients"))
    def val(k): return (r[k] if r else "") or ""
    body = f"""<div class="panel"><h2>{'Editar' if r else 'Novo'} Cliente</h2><form method="post" class="formgrid">
    <label>Razão Social<input name="name" value="{val('name')}"></label><label>Nome Fantasia<input name="fantasy" value="{val('fantasy')}"></label>
    <label>CNPJ<input name="cnpj" value="{val('cnpj')}"></label><label>Responsável<input name="responsible" value="{val('responsible')}"></label>
    <label>Telefone<input name="phone" value="{val('phone')}"></label><label>Email<input name="email" value="{val('email')}"></label>
    <label>Cidade<input name="city" value="{val('city')}"></label><label>Estado<input name="state" value="{val('state')}"></label>
    <label>Status<select name="active"><option value="1">Ativo</option><option value="0" {'selected' if r and not r['active'] else ''}>Inativo</option></select></label>
    <label class="full">Observações<textarea name="notes">{val('notes')}</textarea></label><div class="full"><button class="primary">Salvar</button></div></form></div>"""
    return page(body, breadcrumb="Dashboard > Clientes > Formulário")


@app.route("/contracts")
@login_required
def contracts():
    status = request.args.get("status", "Todos")
    df = current_demo_flag()
    today = date.today().isoformat()
    d30 = (date.today()+timedelta(days=30)).isoformat()
    d60 = (date.today()+timedelta(days=60)).isoformat()
    if status == "Todos":
        q = "WHERE c.demo=?"
        params = (df,)
    elif status == "Ativos":
        q = "WHERE c.demo=? AND c.status!='Encerrado'"
        params = (df,)
    elif status == "A vencer":
        q = "WHERE c.demo=? AND c.status!='Encerrado' AND c.end_date IS NOT NULL AND c.end_date!='' AND c.end_date >= ? AND c.end_date <= ?"
        params = (df, today, d60)
    elif status == "Vencidos":
        q = "WHERE c.demo=? AND c.status!='Encerrado' AND c.end_date IS NOT NULL AND c.end_date!='' AND c.end_date < ?"
        params = (df, today)
    elif status == "Vence em 30d":
        q = "WHERE c.demo=? AND c.status!='Encerrado' AND c.end_date IS NOT NULL AND c.end_date!='' AND c.end_date >= ? AND c.end_date <= ?"
        params = (df, today, d30)
    elif status == "Vence em 60d":
        q = "WHERE c.demo=? AND c.status!='Encerrado' AND c.end_date IS NOT NULL AND c.end_date!='' AND c.end_date > ? AND c.end_date <= ?"
        params = (df, d30, d60)
    elif status == "Encerrados":
        q = "WHERE c.demo=? AND c.status='Encerrado'"
        params = (df,)
    else:
        q = "WHERE c.demo=? AND c.status=?"
        params = (df, status)
    rows = query(f"SELECT c.*, cl.name client_name, (SELECT COUNT(*) FROM cameras ca WHERE ca.contract_id=c.id AND ca.demo=c.demo) cam_count FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id {q} ORDER BY c.created_at DESC", params)
    can_edit = current_user()["role"] == "operacao"
    filter_names = ["Todos", "Ativos", "A vencer", "Vence em 60d", "Vence em 30d", "Vencidos", "Encerrados"]
    filters = "".join([f"<a class='{ 'active' if status==s else ''}' href='{url_for('contracts', status=s)}'>{s}</a>" for s in filter_names])
    items = ""
    for r in rows:
        prazo_txt, prazo_cls, _ = contract_deadline_info(r)
        prazo_badge = f"<br><span class='badge {prazo_cls}'>{prazo_txt}</span>"
        closed_txt = f"<br><small>Encerrado em {(rv(r,'closed_at','') or '')[:10]} · {rv(r,'closed_reason','')}</small>" if rv(r,'status')=='Encerrado' else ''
        items += f"<div class='row'><b>{r['client_name'] or 'Sem cliente'}<br><small>{r['obra'] or ''}</small></b><span>{r['city'] or ''}/{r['state'] or ''}</span><span>{r['cam_count']} câmeras</span><span><span class='badge {status_class(r['status'])}'>{r['status']}</span>{prazo_badge}{closed_txt}</span><span class='actions'><a class='btn small' href='{url_for('contract_view', id=r['id'])}'>Ver</a>{('<a class="btn small" href="'+url_for('contract_edit', id=r['id'])+'">Editar</a>') if can_edit and r['status']!='Encerrado' else ''}</span></div>"
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>Contratos</h2>{'<a class="btn primary" href="'+url_for('contract_new')+'">Novo contrato</a>' if can_edit else ''}</div><div class='filters'>{filters}</div>{items or '<p>Nenhum contrato.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Contratos")


@app.route("/contracts/new", methods=["GET", "POST"])
@operacao_required
def contract_new(): return contract_form()


@app.route("/contracts/<int:id>/edit", methods=["GET", "POST"])
@operacao_required
def contract_edit(id): return contract_form(one("SELECT * FROM contracts WHERE id=?", (id,)))


def contract_form(r=None):
    clients_rows = query("SELECT * FROM clients WHERE active=1 AND demo=? ORDER BY name", (current_demo_flag(),))
    if request.method == "POST":
        vals = (request.form.get("client_id"), request.form.get("obra"), request.form.get("city"), request.form.get("state"), request.form.get("start_date"), request.form.get("end_date"), int(request.form.get("expected_cameras") or 0), request.form.get("status"), request.form.get("notes"))
        if r:
            execute("UPDATE contracts SET client_id=?,obra=?,city=?,state=?,start_date=?,end_date=?,expected_cameras=?,status=?,notes=? WHERE id=?", vals+(r["id"],))
            flash("Contrato atualizado.")
            return redirect(url_for("contract_view", id=r["id"]))
        else:
            execute("INSERT INTO contracts(code,client_id,obra,city,state,start_date,end_date,expected_cameras,status,notes,demo,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (contract_code(),)+vals+(current_demo_flag(), datetime.now().isoformat(),))
            flash("Contrato criado.")
            return redirect(url_for("contracts"))
    def val(k): return (r[k] if r else "") or ""
    clients_opts = "".join([f"<option value='{c['id']}' {'selected' if r and r['client_id']==c['id'] else ''}>{c['name']}</option>" for c in clients_rows])
    status_opts = "".join([f"<option {'selected' if val('status')==s else ''}>{s}</option>" for s in STATUS_CONTRATO])
    body = f"""<div class="panel"><h2>{'Editar' if r else 'Novo'} Contrato</h2><form method="post" class="formgrid">
    <label>Cliente<select name="client_id">{clients_opts}</select></label><label>Nome da obra<input name="obra" value="{val('obra')}"></label>
    <label>Cidade<input name="city" value="{val('city')}"></label><label>Estado<input name="state" value="{val('state')}"></label>
    <label>Data início<input type="date" name="start_date" value="{val('start_date')}"></label><label>Data fim<input type="date" name="end_date" value="{val('end_date')}"></label>
    <label>Qtd. câmeras previstas<input type="number" name="expected_cameras" value="{val('expected_cameras')}"></label><label>Status<select name="status">{status_opts}</select></label>
    <label class="full">Observações<textarea name="notes">{val('notes')}</textarea></label><div class="full"><button class="primary">Salvar</button></div></form></div>"""
    return page(body, breadcrumb="Dashboard > Contratos > Formulário")


@app.route("/contracts/<int:id>")
@login_required
def contract_view(id):
    r = one("SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE c.id=?", (id,))
    cams = query("""SELECT ca.*, co.obra, cl.name client_name,
                    (SELECT COUNT(*) FROM occurrences o WHERE o.camera_id=ca.id AND o.archived_at IS NULL AND o.status IN ('Aberta','Em andamento')) AS active_occ_count,
                    (SELECT o.id FROM occurrences o WHERE o.camera_id=ca.id AND o.archived_at IS NULL AND o.status IN ('Aberta','Em andamento') ORDER BY o.created_at DESC LIMIT 1) AS active_occ_id
                    FROM cameras ca
                    LEFT JOIN contracts co ON co.id=ca.contract_id
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    WHERE ca.contract_id=? ORDER BY ca.code""", (id,))
    items = "".join([camera_row(c, current_user()["role"] if current_user() else None) for c in cams])
    expected = int(r['expected_cameras'] or 0)
    linked = len(cams)
    slots = max(expected - linked, 0)
    if current_user()['role']=='operacao':
        if expected and linked >= expected:
            link_btn = "<span class='badge warn'>Limite de câmeras atingido</span>"
        else:
            link_btn = f"<a class='btn primary' href='{url_for('contract_link_camera', id=r['id'])}'>📎 Vincular câmera</a>"
    else:
        link_btn = ""
    prazo_txt, prazo_cls, prazo_days = contract_deadline_info(r)
    prazo_extra = f"<span class='badge {prazo_cls}'>{prazo_txt}</span>"
    body = f"""<div class="panel"><h2>{r['client_name']} – {r['obra']}</h2><p><span class="badge {status_class(r['status'])}">{r['status']}</span> {prazo_extra} · {r['city']}/{r['state']}</p>
    <p class='tag'><b>Início:</b> {r['start_date'] or '-'} · <b>Fim:</b> {r['end_date'] or '-'} · <b>Controle:</b> 60 dias amarelo, 30 dias laranja e vencido vermelho.</p>
    <div class='grid' style='margin:14px 0'><div class='metric'><h3>Câmeras previstas</h3><b>{expected}</b></div><div class='metric'><h3>Vinculadas</h3><b>{linked}</b></div><div class='metric'><h3>Vagas disponíveis</h3><b>{slots}</b></div></div>
    <div class="actions"><a class="btn" href="{url_for('contracts')}">Voltar</a>{link_btn}{('<a class="btn danger" href="'+url_for('contract_close', id=r['id'])+'">Encerrar contrato</a>') if current_user()['role']=='operacao' and r['status']!='Encerrado' else ''}{('<a class="btn" href="'+url_for('contract_renew', id=r['id'])+'">Criar novo contrato baseado neste</a>') if current_user()['role']=='operacao' and r['status']=='Encerrado' else ''}</div></div>
    <div class="panel"><h2>Câmeras vinculadas ao contrato</h2>{items or '<p>Nenhuma câmera vinculada a este contrato.</p>'}</div>"""
    return page(body, breadcrumb=f"Dashboard > Contratos > {r['client_name']} {r['obra']}")



@app.route("/contracts/<int:id>/close", methods=["GET", "POST"])
@operacao_required
def contract_close(id):
    r = one("SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE c.id=?", (id,))
    if not r:
        flash("Contrato não encontrado.")
        return redirect(url_for("contracts"))
    linked = count("SELECT COUNT(*) FROM cameras WHERE contract_id=?", (id,))
    open_occ = count("""SELECT COUNT(*) FROM occurrences o
                        LEFT JOIN cameras ca ON ca.id=o.camera_id
                        WHERE o.status IN ('Aberta','Em andamento') AND o.archived_at IS NULL
                        AND (ca.contract_id=? OR o.archived_contract_id=?)""", (id, id))
    if request.method == "POST":
        if linked > 0 or open_occ > 0:
            flash("Não é possível encerrar: existem câmeras vinculadas ou ocorrências abertas neste contrato.")
            return redirect(url_for("contract_close", id=id))
        closed_at = request.form.get("closed_at") or date.today().isoformat()
        reason = request.form.get("closed_reason") or "Contrato concluído"
        notes = request.form.get("closed_notes") or ""
        execute("UPDATE contracts SET status='Encerrado', closed_at=?, closed_reason=?, closed_notes=?, closed_by=? WHERE id=?", (closed_at, reason, notes, current_user()["name"], id))
        flash("Contrato encerrado com sucesso.")
        return redirect(url_for("contract_view", id=id))
    reasons = ["Contrato concluído", "Rescisão antecipada", "Cancelamento pelo cliente", "Renovação / novo contrato", "Outro"]
    reason_opts = "".join([f"<option>{x}</option>" for x in reasons])
    block = ""
    if linked > 0 or open_occ > 0:
        block = f"<div class='alert danger'><b>Atenção:</b> este contrato ainda possui {linked} câmera(s) vinculada(s) e {open_occ} ocorrência(s) aberta(s). Finalize as pendências antes de encerrar.</div>"
    body = f"""<div class='panel'><h2>Encerrar contrato</h2>
    <p><b>Cliente:</b> {rv(r,'client_name','-')}<br><b>Obra:</b> {rv(r,'obra','-')}<br><b>Status atual:</b> <span class='badge {status_class(rv(r,'status',''))}'>{rv(r,'status','')}</span></p>
    {block}
    <form method='post' class='formgrid'>
      <label>Data de encerramento<input type='date' name='closed_at' value='{date.today().isoformat()}'></label>
      <label>Motivo<select name='closed_reason'>{reason_opts}</select></label>
      <label class='full'>Observações<textarea name='closed_notes' placeholder='Descreva o motivo ou pendências finais do encerramento'></textarea></label>
      <div class='full actions'><a class='btn' href='{url_for('contract_view', id=id)}'>Voltar</a><button class='primary' {'disabled' if linked>0 or open_occ>0 else ''}>Confirmar encerramento</button></div>
    </form></div>"""
    return page(body, breadcrumb="Dashboard > Contratos > Encerrar")


@app.route("/contracts/<int:id>/renew")
@operacao_required
def contract_renew(id):
    r = one("SELECT * FROM contracts WHERE id=?", (id,))
    if not r:
        flash("Contrato não encontrado.")
        return redirect(url_for("contracts"))
    execute("""INSERT INTO contracts(code,client_id,obra,city,state,start_date,end_date,expected_cameras,status,notes,demo,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (contract_code(), rv(r,'client_id'), rv(r,'obra'), rv(r,'city'), rv(r,'state'), '', '', int(rv(r,'expected_cameras',0) or 0), 'Planejamento', f"Novo contrato baseado no {rv(r,'code','contrato anterior')}", current_demo_flag(), datetime.now().isoformat()))
    new_id = count("SELECT MAX(id) FROM contracts WHERE demo=?", (current_demo_flag(),))
    flash("Novo contrato criado a partir do contrato encerrado. Ajuste datas, valores e observações.")
    return redirect(url_for("contract_edit", id=new_id))


@app.route("/contracts/<int:id>/link-camera", methods=["GET", "POST"])
@operacao_required
def contract_link_camera(id):
    r = one("SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE c.id=?", (id,))
    if not r:
        flash("Contrato não encontrado.")
        return redirect(url_for("contracts"))
    expected = int(r["expected_cameras"] or 0)
    linked = count("SELECT COUNT(*) FROM cameras WHERE contract_id=?", (id,))
    slots = max(expected - linked, 0)
    if expected and slots <= 0:
        flash("Limite de câmeras do contrato atingido. Para vincular mais câmeras, edite a quantidade prevista no contrato.")
        return redirect(url_for("contract_view", id=id))

    if request.method == "POST":
        camera_id = request.form.get("camera_id")
        cam = one("SELECT * FROM cameras WHERE id=?", (camera_id,))
        if not cam:
            flash("Câmera não encontrada.")
            return redirect(url_for("contract_link_camera", id=id))
        if cam["contract_id"]:
            flash("Esta câmera já está vinculada a outro contrato/obra.")
            return redirect(url_for("contract_link_camera", id=id))
        if cam["status"] != "Testada e aprovada":
            flash("Somente câmeras testadas e aprovadas podem ser vinculadas ao contrato.")
            return redirect(url_for("contract_link_camera", id=id))
        now = datetime.now().isoformat()
        execute("UPDATE cameras SET contract_id=?, status=?, patrimonial_status=?, updated_at=? WHERE id=?", (id, "Reservada", "Reservada", now, camera_id))
        execute("INSERT INTO camera_history(camera_id,old_contract_id,new_contract_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (camera_id, cam["contract_id"], id, cam["current_location"], cam["current_location"], cam["status"], "Reservada", f"Câmera reservada/vinculada ao contrato {r['obra']}", current_user()["name"], now))
        flash("Câmera vinculada ao contrato e reservada para esta obra.")
        return redirect(url_for("contract_view", id=id))

    cams = query("""SELECT * FROM cameras
                    WHERE demo=? AND contract_id IS NULL AND status='Testada e aprovada'
                    ORDER BY code""", (current_demo_flag(),))
    rows = []
    for c in cams:
        rows.append(f"""<div class='row'>
            <b>{c['code']}</b><span>{c['model'] or '-'}</span><span>{c['serial'] or '-'}</span><span><span class='badge ok'>Testada e aprovada</span></span>
            <span><form method='post' style='margin:0'><input type='hidden' name='camera_id' value='{c['id']}'><button class='primary'>Selecionar</button></form></span>
        </div>""")
    body = f"""<div class='panel'><h2>📎 Vincular câmera ao contrato</h2>
    <p><b>Cliente:</b> {r['client_name'] or '-'}<br><b>Obra:</b> {r['obra'] or '-'}<br><b>Quantidade prevista:</b> {expected} · <b>Vinculadas:</b> {linked} · <b>Vagas:</b> {slots}</p>
    <p class='tag'>Apenas câmeras <b>testadas e aprovadas</b>, sem vínculo com outra obra, aparecem nesta lista.</p>
    <div class='actions'><a class='btn' href='{url_for('contract_view', id=id)}'>Voltar ao contrato</a></div>
    <div style='margin-top:12px'>{''.join(rows) if rows else '<p>Nenhuma câmera testada e aprovada disponível para vínculo.</p>'}</div></div>"""
    return page(body, breadcrumb=f"Dashboard > Contratos > Vincular câmera")


@app.route("/contracts/<int:contract_id>/unlink-camera/<int:camera_id>", methods=["POST"])
@operacao_required
def contract_unlink_camera(contract_id, camera_id):
    cam = one("SELECT * FROM cameras WHERE id=? AND contract_id=?", (camera_id, contract_id))
    if not cam:
        flash("Câmera não encontrada neste contrato.")
        return redirect(url_for("contract_view", id=contract_id))
    if cam["status"] != "Reservada":
        flash("Só é possível desvincular câmera que ainda está apenas reservada.")
        return redirect(url_for("contract_view", id=contract_id))
    now = datetime.now().isoformat()
    execute("UPDATE cameras SET contract_id=NULL, status=?, patrimonial_status=?, updated_at=? WHERE id=?", ("Testada e aprovada", "Em estoque", now, camera_id))
    execute("INSERT INTO camera_history(camera_id,old_contract_id,new_contract_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (camera_id, contract_id, None, cam["current_location"], cam["current_location"], cam["status"], "Testada e aprovada", "Reserva cancelada / câmera desvinculada do contrato", current_user()["name"], now))
    flash("Câmera desvinculada e retornou para testada/aprovada em estoque.")
    return redirect(url_for("contract_view", id=contract_id))

def camera_row(c, user_role=None):
    # Compatível com SQLite Row, psycopg dict_row e bancos antigos sem algumas colunas.
    cam_id = rv(c, 'id')
    code = rv(c, 'code', '-') or '-'
    status = rv(c, 'status', 'Sem status') or 'Sem status'
    active_occ = int(rv(c, 'active_occ_count', 0) or 0)
    cls = status_class(status)
    if active_occ > 0:
        cls = 'danger'
    aprovado = " 🧪" if (rv(c, 'tested_approved_at') or status == "Testada e aprovada") else ""
    active_occ_id = rv(c, 'active_occ_id', None)
    if active_occ and active_occ_id:
        occ_badge = f" <a class='badge danger' href='{url_for('occurrence_close', id=active_occ_id)}'>⚠ {active_occ} ocorrência(s) aberta(s)</a>"
    elif active_occ:
        occ_badge = f" <a class='badge danger' href='{url_for('occurrences', status='abertas')}'>⚠ {active_occ} ocorrência(s) aberta(s)</a>"
    else:
        occ_badge = ""
    qr_btn = f"<a class='btn small' href='{url_for('camera_qr', id=cam_id)}'>📷 QR</a>"
    dossier_btn = f"<a class='btn small' href='{url_for('camera_dossie', id=cam_id)}'>📑 Dossiê</a>"
    test_btn = f"<a class='btn small' href='{url_for('camera_approve', id=cam_id)}'>🧪 Testar</a>" if (user_role or (current_user()['role'] if current_user() else None))=='operacao' else ""
    role = (user_role or (current_user()['role'] if current_user() else None))
    edit_btns = (f"<a class='btn small' href='{url_for('camera_edit', id=cam_id)}'>Editar</a> <a class='btn small' href='{url_for('camera_transfer', id=cam_id)}'>Transferir</a>") if role=='operacao' else ""
    if role=='operacao' and status == 'Reservada' and rv(c, 'contract_id', None):
        edit_btns += f" <form method='post' action='{url_for('contract_unlink_camera', contract_id=rv(c, 'contract_id'), camera_id=cam_id)}' style='display:inline' onsubmit='return confirm(&quot;Cancelar reserva desta câmera?&quot;)'><button class='btn small' type='submit'>Desvincular</button></form>"
    cliente = rv(c, 'client_name', '-') or '-'
    obra = rv(c, 'obra', '-') or '-'
    local = rv(c, 'current_location', '-') or '-'
    servico = rv(c, 'service', '-') or '-'
    ver_label = 'Ver / ocorrência' if active_occ else 'Ver'
    return f"<div class='row camera { 'danger' if cls=='danger' else ''}'><b>{code}{aprovado}</b><span><b>{cliente}</b><br><small>{obra}</small></span><span>{local}</span><span>{servico}</span><span><span class='badge {cls}'>{status}</span>{occ_badge}</span><span class='actions'>{qr_btn}{dossier_btn}{test_btn}<a class='btn small' href='{url_for('camera_maintenance', id=cam_id) if status == 'Em manutenção' else url_for('camera_view', id=cam_id)}'>{'Verificar' if status == 'Em manutenção' else ver_label}</a>{edit_btns}</span></div>"


@app.route("/cameras")
@login_required
def cameras():
    status = request.args.get("status", "Todas")
    params = (current_demo_flag(),)
    where = "WHERE ca.demo=?"
    if status != "Todas":
        where += " AND ca.status=?"; params = (current_demo_flag(), status)
    rows = query(f"""SELECT ca.*, co.obra, co.city contract_city, co.state contract_state, cl.name client_name,
                    (SELECT COUNT(*) FROM occurrences o WHERE o.camera_id=ca.id AND o.archived_at IS NULL AND o.status IN ('Aberta','Em andamento')) AS active_occ_count,
                    (SELECT o.id FROM occurrences o WHERE o.camera_id=ca.id AND o.archived_at IS NULL AND o.status IN ('Aberta','Em andamento') ORDER BY o.created_at DESC LIMIT 1) AS active_occ_id
                    FROM cameras ca
                    LEFT JOIN contracts co ON co.id=ca.contract_id
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    {where}
                    ORDER BY cl.name, co.obra, ca.code""", params)
    filters = ["Todas", "Aguardando teste", "Testada e aprovada", "Reservada", "Em transporte", "Na obra aguardando instalação", "Instalando", "Em operação", "Aguardando retirada", "Em retorno", "Offline", "Em manutenção", "Inutilizada"]
    def filter_count(label):
        if label == "Todas":
            return count("SELECT COUNT(*) FROM cameras WHERE demo=?", (current_demo_flag(),))
        return count("SELECT COUNT(*) FROM cameras WHERE status=? AND demo=?", (label, current_demo_flag()))
    fhtml = "".join([f"<a class='{ 'active' if status==s else ''}' href='{url_for('cameras', status=s)}'>{s} ({filter_count(s)})</a>" for s in filters])

    # V1.9: visão hierárquica para operação real: Status > Cliente > Obra > Câmeras.
    # Mesmo em filtros específicos, primeiro aparece o cliente, depois a obra/contrato.
    groups = {}
    for c in rows:
        cliente = rv(c, 'client_name', 'Sem cliente / estoque') or 'Sem cliente / estoque'
        obra_nome = rv(c, 'obra', 'Sem obra / estoque') or 'Sem obra / estoque'
        cidade = rv(c, 'contract_city', '') or ''
        estado = rv(c, 'contract_state', '') or ''
        obra_label = obra_nome + (f" · {cidade}/{estado}" if cidade or estado else "")
        groups.setdefault(cliente, {}).setdefault(obra_label, []).append(c)

    blocks = []
    for cliente, obras in groups.items():
        total_cliente = sum(len(v) for v in obras.values())
        obra_blocks = []
        for obra, cams in obras.items():
            cam_rows = "".join([camera_row(cam, current_user()["role"] if current_user() else None) for cam in cams])
            obra_blocks.append(f"""<details class='card' open>
                <summary style='cursor:pointer;font-weight:700'>🏗 {obra} <span class='badge muted'>{len(cams)} câmera(s)</span></summary>
                <div style='margin-top:10px'>{cam_rows}</div>
            </details>""")
        blocks.append(f"""<details class='panel' open>
            <summary style='cursor:pointer;font-size:20px;font-weight:800'>👤 {cliente} <span class='badge muted'>{total_cliente} câmera(s)</span></summary>
            <div style='margin-top:12px'>{''.join(obra_blocks)}</div>
        </details>""")
    items = "".join(blocks)

    body = f"""<div class='panel'><div class='actions'><h2 style='flex:1'>Câmeras</h2>{'<a class=\"btn primary\" href=\"'+url_for('camera_new')+'\">Nova câmera</a>' if current_user()['role']=='operacao' else ''}</div>
    <p class='tag'>Visualização por <b>cliente</b>, depois <b>obra</b>, depois <b>câmera</b>. cada câmera aparece em <b>uma única etapa</b>. A soma dos filtros fecha com o total.</p>
    <div class='filters'>{fhtml}</div>{items or '<p>Nenhuma câmera.</p>'}</div>"""
    return page(body, breadcrumb="Dashboard > Gestão de Câmeras")


@app.route("/cameras/new", methods=["GET", "POST"])
@operacao_required
def camera_new(): return camera_form(None, request.args.get("contract_id"))


@app.route("/cameras/<int:id>/edit", methods=["GET", "POST"])
@operacao_required
def camera_edit(id): return camera_form(one("SELECT * FROM cameras WHERE id=?", (id,)))


def camera_form(r=None, contract_id=None):
    clients_rows = query("SELECT * FROM clients WHERE active=1 AND demo=? ORDER BY name", (current_demo_flag(),))
    contracts_rows = query("SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE c.demo=? ORDER BY cl.name, c.obra", (current_demo_flag(),))
    selected_contract_id = (r["contract_id"] if r else contract_id)
    selected_client_id = ""
    if selected_contract_id:
        cr = one("SELECT client_id FROM contracts WHERE id=?", (selected_contract_id,))
        selected_client_id = str(cr["client_id"]) if cr and cr["client_id"] else ""
    if request.method == "POST":
        status_form = request.form.get("status") or "Aguardando teste"
        posted_contract_id = request.form.get("contract_id") or None
        # V2.0.7: não permite enviar/vincular câmera a obra se ela ainda estiver aguardando teste.
        # A câmera só pode iniciar novo fluxo de campo quando estiver Testada e aprovada.
        if r and not can_send_to_field(r["status"]):
            if posted_contract_id and not r["contract_id"]:
                flash("Esta câmera ainda não está testada e aprovada. Conclua o checklist antes de vinculá-la a uma obra.")
                return redirect(url_for("camera_edit", id=r["id"]))
            if status_form in field_statuses():
                flash("Esta câmera ainda não está testada e aprovada. Para enviar à obra, conclua primeiro o checklist de teste.")
                return redirect(url_for("camera_edit", id=r["id"]))
        if not r and (posted_contract_id or status_form in field_statuses()):
            flash("Nova câmera entra como Aguardando teste. Conclua o checklist antes de enviá-la para obra.")
            return redirect(url_for("camera_new"))
        patrimonial_status = "Em estoque" if status_form in ("Aguardando teste", "Testada e aprovada") else status_form
        vals = (request.form.get("model"), request.form.get("serial"), posted_contract_id, request.form.get("current_location"), request.form.get("service"), status_form, request.form.get("notes"), datetime.now().isoformat(), patrimonial_status)
        if r:
            execute("UPDATE cameras SET model=?,serial=?,contract_id=?,current_location=?,service=?,status=?,notes=?,updated_at=?,patrimonial_status=? WHERE id=?", vals+(r["id"],))
            flash("Câmera atualizada.")
            return redirect(url_for("camera_view", id=r["id"]))
        code = request.form.get("code") or next_camera_code()
        execute("INSERT INTO cameras(code,model,serial,contract_id,current_location,service,status,notes,demo,updated_at,created_at,patrimonial_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (code,)+vals[:7]+(current_demo_flag(),)+vals[7:8]+(datetime.now().isoformat(), vals[8]))
        flash("Câmera criada em estoque, aguardando teste.")
        return redirect(url_for("cameras"))
    def val(k): return (r[k] if r else "") or ""
    client_opts = "<option value=''>Sem cliente / estoque</option>" + "".join([f"<option value='{cl['id']}' {'selected' if selected_client_id==str(cl['id']) else ''}>{cl['name']}</option>" for cl in clients_rows])
    contract_opts = "<option value='' data-client=''>Sem contrato / estoque</option>" + "".join([f"<option value='{c['id']}' data-client='{c['client_id'] or ''}' {'selected' if selected_contract_id and str(c['id'])==str(selected_contract_id) else ''}>{c['client_name'] or 'Sem cliente'} - {c['obra']}</option>" for c in contracts_rows])
    service_opts = "".join([f"<option {'selected' if val('service')==s else ''}>{s}</option>" for s in SERVICOS])
    status_opts = "".join([f"<option {'selected' if val('status')==s else ''}>{s}</option>" for s in STATUS_CAMERA])
    code_field = f"<label>Código<input name='code' value='{next_camera_code()}'></label>" if not r else f"<label>Código<input value='{val('code')}' disabled></label>"
    bloqueio_html = ""
    if r and not can_send_to_field(r["status"]):
        bloqueio_html = "<p style='background:#fef3c7;border-radius:12px;padding:12px'><b>Regra operacional:</b> esta câmera ainda não está testada e aprovada. Ela não pode ser enviada para obra nem liberada para transporte.</p>"
    body = f"""<div class="panel"><h2>{'Editar' if r else 'Nova'} Câmera</h2>
    <p class="tag">Escolha primeiro o <b>Cliente</b> e depois a <b>Obra/Contrato</b>. Isso evita confusão quando o mesmo cliente tiver várias obras.</p>{bloqueio_html}
    <form method="post" class="formgrid">{code_field}
    <label>Modelo<input name="model" value="{val('model')}"></label><label>Nº Série<input name="serial" value="{val('serial')}"></label>
    <label>Cliente<select id="client_select" name="client_select">{client_opts}</select></label><label>Obra / contrato atual<select id="contract_select" name="contract_id">{contract_opts}</select></label>
    <label>Local atual<input name="current_location" value="{val('current_location')}"></label><label>Serviço<select name="service">{service_opts}</select></label>
    <label>Status<select name="status">{status_opts}</select></label><label class="full">Observações<textarea name="notes">{val('notes')}</textarea></label>
    <div class="full"><button class="primary">Salvar</button></div></form></div>
    <script>
    function filtrarContratos(){{
      const cliente = document.getElementById('client_select').value;
      const contrato = document.getElementById('contract_select');
      let selectedStillVisible = false;
      Array.from(contrato.options).forEach(opt => {{
        const show = !opt.value || !cliente || opt.dataset.client === cliente;
        opt.hidden = !show;
        if (opt.selected && show) selectedStillVisible = true;
      }});
      if (!selectedStillVisible) contrato.value = '';
    }}
    document.getElementById('client_select').addEventListener('change', filtrarContratos);
    filtrarContratos();
    </script>"""
    return page(body, breadcrumb="Dashboard > Câmeras > Formulário")


@app.route("/cameras/<int:id>")
@login_required
def camera_view(id):
    c = one("SELECT ca.*, co.obra, cl.name client_name FROM cameras ca LEFT JOIN contracts co ON co.id=ca.contract_id LEFT JOIN clients cl ON cl.id=co.client_id WHERE ca.id=?", (id,))
    hist = query("SELECT * FROM camera_history WHERE camera_id=? ORDER BY created_at DESC", (id,))
    occs = query("SELECT * FROM occurrences WHERE camera_id=? AND archived_at IS NULL ORDER BY created_at DESC", (id,))
    occs_hist = query("SELECT * FROM occurrences WHERE camera_id=? AND archived_at IS NOT NULL ORDER BY archived_at DESC", (id,))
    hist_parts = []
    for h in hist:
        foto = rv(h, 'install_photo', '')
        foto_html = f"<br><img src='{foto}' alt='Foto da instalação' style='max-width:220px;border-radius:12px;border:1px solid #dbe3ef;margin-top:8px'>" if foto else ""
        hist_parts.append(f"<div class='row'><b>{h['created_at'][:16]}</b><span>{h['old_status'] or '-'} → {h['new_status'] or '-'}</span><span>{h['old_location'] or '-'} → {h['new_location'] or '-'}</span><span>{h['user_name'] or ''}</span><span>{h['note'] or ''}{foto_html}</span></div>")
    hist_html = "".join(hist_parts)
    occ_html = "".join([f"<div class='row'><b>{o['title']}</b><span>{o['problem']}</span><span><span class='badge danger'>{o['status']}</span></span><span>{o['created_at'][:16]}</span><span><a class='btn small primary' href='{url_for('occurrence_close', id=o['id'])}'>Ver / resolver</a></span></div>" for o in occs])
    occ_hist_html = "".join([f"<div class='row'><b>{o['title']}</b><span>{o['problem']}</span><span>Arquivada</span><span>{(o['archived_at'] or o['created_at'])[:16]}</span><span>{o['archived_location'] or '-'}</span></div>" for o in occs_hist])
    active_occ_callout = ""
    if occs:
        first_occ = occs[0]
        active_occ_callout = f"""<div class='panel' style='border-color:rgba(239,68,68,.45)'>
        <h2>⚠ Ocorrência aberta nesta câmera</h2>
        <p><b>{first_occ['title'] or 'Problema operacional'}</b></p>
        <p>{first_occ['problem'] or first_occ['notes'] or 'Sem descrição.'}</p>
        <div class='actions'><a class='btn primary' href='{url_for('occurrence_close', id=first_occ['id'])}'>Ver / resolver ocorrência</a><a class='btn' href='{url_for('occurrences', status='abertas')}'>Ver todas ocorrências</a></div>
        </div>"""
    inutilizar_action = ""
    if current_user()["role"] == "operacao" and c["status"] != "Inutilizada":
        inutilizar_action = f"<a class='btn danger' href='{url_for('camera_inutilizar', id=c['id'])}'>🚫 Inutilizar</a>"
    if current_user()["role"] == "operacao" and c["status"] == "Em manutenção":
        inutilizar_action += f"<a class='btn' href='{url_for('camera_maintenance', id=c['id'])}'>🔧 Manutenção</a>"
    body = f"""<div class="panel"><h2>{c['code']}</h2><p><span class="badge {status_class(c['status'])}">{c['status']}</span></p><p>Cliente/Obra: {c['client_name'] or '-'} / {c['obra'] or '-'}</p><p>Local: {c['current_location'] or '-'}</p><p>Serviço: {c['service'] or '-'}</p>{f"<p><b>Última foto da instalação:</b><br><img src='{rv(c, 'last_install_photo', '')}' alt='Foto da instalação' style='max-width:320px;border-radius:14px;border:1px solid #dbe3ef;margin-top:8px'></p>" if rv(c, 'last_install_photo', '') else ''}<div class="actions"><a class="btn" href="{url_for('cameras')}">Voltar</a><a class="btn" href="{url_for('camera_qr', id=c['id'])}">📷 Gerar QR</a><a class="btn" href="{url_for('camera_dossie', id=c['id'])}">📑 Dossiê</a>{inutilizar_action}{('<a class=\"btn primary\" href=\"'+url_for('camera_transfer', id=c['id'])+'\">Transferir</a><a class=\"btn\" href=\"'+url_for('occurrence_new', camera_id=c['id'])+'\">Abrir ocorrência</a>' + ('<a class=\"btn\" href=\"'+url_for('camera_receive_central', id=c['id'])+'\">🏢 Recebida na central</a>' if c['status']=='Em retorno' else '<a class=\"btn\" href=\"'+url_for('camera_authorize_removal', id=c['id'])+'\">🔓 Autorizar retirada</a>')) if current_user()['role']=='operacao' else ''}</div></div>
    {active_occ_callout}
    <div class="panel"><h2>Histórico</h2>{hist_html or '<p>Sem histórico.</p>'}</div>
    <div class="panel"><h2>Ocorrências do ciclo atual</h2>{occ_html or '<p>Sem ocorrências ativas neste ciclo.</p>'}</div>
    <div class="panel"><h2>Ocorrências arquivadas de ciclos anteriores</h2>{occ_hist_html or '<p>Sem ocorrências arquivadas.</p>'}</div>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > {c['code']}")



@app.route("/cameras/<int:id>/dossie")
@login_required
def camera_dossie(id):
    c = one("""SELECT ca.*, co.obra, co.city, co.state, cl.name client_name
               FROM cameras ca
               LEFT JOIN contracts co ON co.id=ca.contract_id
               LEFT JOIN clients cl ON cl.id=co.client_id
               WHERE ca.id=?""", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))

    hist = query("""SELECT h.*, oc.obra old_obra, nc.obra new_obra, ocl.name old_client, ncl.name new_client
                    FROM camera_history h
                    LEFT JOIN contracts oc ON oc.id=h.old_contract_id
                    LEFT JOIN clients ocl ON ocl.id=oc.client_id
                    LEFT JOIN contracts nc ON nc.id=h.new_contract_id
                    LEFT JOIN clients ncl ON ncl.id=nc.client_id
                    WHERE h.camera_id=?
                    ORDER BY h.created_at ASC""", (id,))
    occs = query("""SELECT o.*, co.obra, cl.name client_name
                    FROM occurrences o
                    LEFT JOIN contracts co ON co.id=COALESCE(o.archived_contract_id, (SELECT contract_id FROM cameras WHERE id=o.camera_id))
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    WHERE o.camera_id=?
                    ORDER BY o.created_at ASC""", (id,))

    total_hist = len(hist)
    obras_ids = set()
    for h in hist:
        if rv(h, 'old_contract_id'):
            obras_ids.add(rv(h, 'old_contract_id'))
        if rv(h, 'new_contract_id'):
            obras_ids.add(rv(h, 'new_contract_id'))
    obras_count = len(obras_ids)
    occ_count = len(occs)
    manut_count = sum(1 for h in hist if 'manutenção' in (rv(h, 'new_status', '') or '').lower() or 'manutenção' in (rv(h, 'note', '') or '').lower())
    fotos_count = sum(1 for h in hist if rv(h, 'install_photo')) + (1 if rv(c, 'last_install_photo') else 0)

    first_date = (rv(hist[0], 'created_at') if hist else rv(c, 'created_at')) or ''
    last_date = (rv(hist[-1], 'created_at') if hist else rv(c, 'updated_at')) or ''
    timeline = ""
    for h in hist:
        created = (rv(h, 'created_at') or '')[:16].replace('T',' ')
        old_st = rv(h, 'old_status', '-') or '-'
        new_st = rv(h, 'new_status', '-') or '-'
        user = rv(h, 'user_name', '') or ''
        note = rv(h, 'note', '') or ''
        new_client = rv(h, 'new_client') or rv(h, 'old_client') or ''
        new_obra = rv(h, 'new_obra') or rv(h, 'old_obra') or ''
        contexto = (new_client + (' · ' if new_client and new_obra else '') + new_obra) or 'Registro patrimonial'
        foto = rv(h, 'install_photo', '')
        foto_html = f"<br><img src='{foto}' alt='Foto da instalação' style='max-width:240px;border-radius:14px;border:1px solid var(--border);margin-top:10px'>" if foto else ""
        timeline += f"""<div class='timeline-item'><span class='tag'>{created}</span><span class='dot'>📷</span><div><b>{old_st} → {new_st}</b><div class='tag'>{contexto}</div><div class='tag'>{note}</div>{foto_html}</div><span class='badge {status_class(new_st)}'>{user}</span></div>"""
    if not timeline:
        timeline = "<p class='tag'>Nenhuma movimentação registrada ainda.</p>"

    # Agrupamento simples por obra/contrato com base no histórico e ocorrências
    obras = {}
    for h in hist:
        cid = rv(h, 'new_contract_id') or rv(h, 'old_contract_id') or 'sem'
        label = ((rv(h, 'new_client') or rv(h, 'old_client') or 'Sem cliente') + ' · ' + (rv(h, 'new_obra') or rv(h, 'old_obra') or 'Sem obra')).strip(' ·')
        obras.setdefault(cid, {'label': label, 'hist': [], 'occs': []})['hist'].append(h)
    for o in occs:
        cid = rv(o, 'archived_contract_id') or 'atual'
        label = ((rv(o, 'client_name') or rv(c, 'client_name') or 'Sem cliente') + ' · ' + (rv(o, 'obra') or rv(c, 'obra') or 'Sem obra')).strip(' ·')
        obras.setdefault(cid, {'label': label, 'hist': [], 'occs': []})['occs'].append(o)
    obras_html = ""
    for data in obras.values():
        hlist = data['hist']
        olist = data['occs']
        inicio = (rv(hlist[0], 'created_at') if hlist else (rv(olist[0], 'created_at') if olist else ''))[:10]
        fim = (rv(hlist[-1], 'created_at') if hlist else (rv(olist[-1], 'closed_at') or rv(olist[-1], 'created_at') if olist else ''))[:10]
        obras_html += f"""<details class='card'><summary style='cursor:pointer;font-weight:800'>🏗 {data['label']} <span class='badge muted'>{len(hlist)} movimento(s)</span> <span class='badge {'danger' if any(rv(o,'status') in ('Aberta','Em andamento') for o in olist) else 'ok'}'>{len(olist)} ocorrência(s)</span></summary><div style='margin-top:12px'><p class='tag'>Período: {inicio or '-'} até {fim or '-'}</p>"""
        for o in olist:
            obras_html += f"""<div class='row'><b>{rv(o,'title','Ocorrência')}</b><span>{rv(o,'problem','')}</span><span><span class='badge {status_class(rv(o,'status',''))}'>{rv(o,'status','')}</span></span><span>{(rv(o,'created_at','') or '')[:16].replace('T',' ')}</span><span><a class='btn small' href='{url_for('occurrence_close', id=rv(o,'id'))}'>Ver</a></span></div>"""
        obras_html += "</div></details>"
    if not obras_html:
        obras_html = "<p class='tag'>Sem ciclos anteriores registrados.</p>"

    occ_html = ""
    for o in occs:
        occ_html += f"""<div class='row'><b>{rv(o,'title','Ocorrência')}</b><span>{rv(o,'problem','')}</span><span><span class='badge {status_class(rv(o,'status',''))}'>{rv(o,'status','')}</span></span><span>{(rv(o,'created_at','') or '')[:16].replace('T',' ')}</span><span><a class='btn small' href='{url_for('occurrence_close', id=rv(o,'id'))}'>Ver detalhe</a></span></div>"""
    if not occ_html:
        occ_html = "<p class='tag'>Nenhuma ocorrência registrada nesta câmera.</p>"

    fotos_html = ""
    if rv(c, 'last_install_photo'):
        fotos_html += f"<div class='card'><b>Última instalação</b><br><img src='{rv(c,'last_install_photo')}' style='max-width:260px;border-radius:14px;border:1px solid var(--border);margin-top:10px'></div>"
    for h in hist:
        if rv(h, 'install_photo'):
            fotos_html += f"<div class='card'><b>{(rv(h,'created_at','') or '')[:16].replace('T',' ')}</b><br><span class='tag'>{rv(h,'note','Foto de instalação')}</span><br><img src='{rv(h,'install_photo')}' style='max-width:260px;border-radius:14px;border:1px solid var(--border);margin-top:10px'></div>"
    if not fotos_html:
        fotos_html = "<p class='tag'>Nenhuma foto anexada ao dossiê.</p>"

    inutilizada_callout = ""
    if rv(c, 'status') == 'Inutilizada':
        inutilizada_callout = f"""<div class='panel' style='border-color:rgba(239,68,68,.45)'><h2>🚫 Câmera inutilizada</h2><p>{rv(c,'notes','Sem motivo informado.')}</p></div>"""

    body = f"""
    <div class='panel'>
      <div class='actions'><div style='flex:1'><h2>📑 Dossiê da Câmera</h2><p class='tag'>Prontuário completo de vida útil, movimentações, obras, ocorrências e fotos.</p></div><a class='btn' href='{url_for('camera_view', id=id)}'>Voltar à ficha</a><a class='btn' href='{url_for('cameras')}'>Câmeras</a></div>
      <div class='mini-grid'>
        <div class='card hero-card'><div class='hero-icon'>📷</div><div><h3>Câmera</h3><b style='font-size:26px'>{rv(c,'code')}</b><br><span>{rv(c,'model','')}</span></div></div>
        <div class='card hero-card green'><div class='hero-icon'>🏗</div><div><h3>Obras atendidas</h3><b>{obras_count}</b><br><span>ciclos registrados</span></div></div>
        <div class='card hero-card orange'><div class='hero-icon'>⚠</div><div><h3>Ocorrências</h3><b>{occ_count}</b><br><span>ativas e arquivadas</span></div></div>
        <div class='card hero-card'><div class='hero-icon'>🖼</div><div><h3>Fotos</h3><b>{fotos_count}</b><br><span>instalações/anexos</span></div></div>
      </div>
      <p><span class='badge {status_class(rv(c,'status'))}'>{rv(c,'status','Sem status')}</span> · Cliente/Obra atual: {rv(c,'client_name','-') or '-'} / {rv(c,'obra','-') or '-'}</p>
      <p class='tag'>Primeiro registro: {first_date[:16].replace('T',' ') if first_date else '-'} · Último registro: {last_date[:16].replace('T',' ') if last_date else '-'}</p>
    </div>
    {inutilizada_callout}
    <div class='panel'><h2>Linha do tempo completa</h2><div class='timeline'>{timeline}</div></div>
    <div class='panel'><h2>Obras e ciclos operacionais</h2>{obras_html}</div>
    <div class='panel'><h2>Ocorrências da câmera</h2>{occ_html}</div>
    <div class='panel'><h2>Fotos anexadas</h2><div class='grid'>{fotos_html}</div></div>
    """
    return page(body, breadcrumb=f"Dashboard > Câmeras > Dossiê {rv(c,'code','')}")



@app.route("/cameras/<int:id>/inutilizar", methods=["GET", "POST"])
@operacao_required
def camera_inutilizar(id):
    c = one("""SELECT ca.*, co.obra, cl.name client_name FROM cameras ca
               LEFT JOIN contracts co ON co.id=ca.contract_id
               LEFT JOIN clients cl ON cl.id=co.client_id
               WHERE ca.id=?""", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))
    if request.method == "POST":
        motivo = request.form.get("motivo", "").strip()
        old_status = c["status"]
        now = datetime.now().isoformat()
        note = "Câmera inutilizada/baixada do patrimônio." + ((" Motivo: " + motivo) if motivo else "")
        execute("UPDATE cameras SET status=?, patrimonial_status=?, notes=?, updated_at=? WHERE id=?", ("Inutilizada", "Inutilizada", ((c["notes"] or "") + "\n" + note).strip(), now, id))
        execute("INSERT INTO camera_history(camera_id,old_contract_id,new_contract_id,old_location,new_location,old_service,new_service,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (id, c["contract_id"], c["contract_id"], c["current_location"], c["current_location"], c["service"], c["service"], old_status, "Inutilizada", note, current_user()["name"], now))
        flash("Câmera marcada como inutilizada. O histórico foi preservado.")
        return redirect(url_for("camera_view", id=id))
    body = f"""<div class='panel'><h2>🚫 Inutilizar câmera {c['code']}</h2>
    <p class='tag'>Use esta opção quando a câmera estiver quebrada, sem conserto ou não puder mais ser usada em contratos.</p><p><a class='btn' href='{url_for('camera_dossie', id=id)}'>📑 Ver dossiê completo antes de inutilizar</a></p>
    <div class='card'><p><b>Câmera:</b> {c['code']}</p><p><b>Cliente/Obra atual:</b> {c['client_name'] or '-'} / {c['obra'] or '-'}</p><p><b>Status atual:</b> {c['status']}</p></div>
    <form method='post' class='formgrid' onsubmit="return confirm('Confirmar que esta câmera será marcada como inutilizada?')">
      <label class='full'>Motivo / observação<textarea name='motivo' required placeholder='Ex.: dano físico sem reparo, entrada de água, placa queimada, perda total...'></textarea></label>
      <div class='full actions'><button class='danger'>Marcar como inutilizada</button><a class='btn' href='{url_for('camera_view', id=id)}'>Cancelar</a></div>
    </form></div>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > Inutilizar {c['code']}")

@app.route("/cameras/<int:id>/authorize-removal", methods=["GET", "POST"])
@operacao_required
def camera_authorize_removal(id):
    c = one("SELECT ca.*, co.obra, cl.name client_name FROM cameras ca LEFT JOIN contracts co ON co.id=ca.contract_id LEFT JOIN clients cl ON cl.id=co.client_id WHERE ca.id=?", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))
    if request.method == "POST":
        old_status = c["status"]
        user_name = current_user()["name"]
        now = datetime.now().isoformat()
        execute("UPDATE cameras SET status=?, removal_authorized_at=?, removal_authorized_by=?, updated_at=? WHERE id=?", ("Aguardando retirada", now, user_name, now, id))
        execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (id, c["current_location"], c["current_location"], old_status, "Aguardando retirada", "Retirada autorizada pelo painel de controle.", user_name, now))
        flash("Retirada autorizada. Agora o técnico poderá retirar a câmera pelo QR Code em campo.")
        return redirect(url_for("camera_view", id=id))
    body = f"""<div class='panel'><h2>🔓 Autorizar retirada</h2><p>Autorize esta retirada somente após os trâmites internos e encerramento/realocação do contrato.</p>
    <div class='card'><p><b>Câmera:</b> {c['code']}</p><p><b>Cliente:</b> {c['client_name'] or '-'}</p><p><b>Obra:</b> {c['obra'] or '-'}</p><p><b>Status atual:</b> {c['status']}</p></div>
    <form method='post' onsubmit="return confirm('Confirmar autorização de retirada desta câmera?')"><button class='primary'>Autorizar retirada</button> <a class='btn' href='{url_for('camera_view', id=id)}'>Cancelar</a></form></div>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > Autorizar retirada {c['code']}")


@app.route("/cameras/<int:id>/receive-central", methods=["GET", "POST"])
@operacao_required
def camera_receive_central(id):
    c = one("""SELECT ca.*, co.obra, cl.name client_name FROM cameras ca
               LEFT JOIN contracts co ON co.id=ca.contract_id
               LEFT JOIN clients cl ON cl.id=co.client_id
               WHERE ca.id=?""", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))
    if request.method == "POST":
        old_status = c["status"]
        old_loc = c["current_location"]
        now = datetime.now().isoformat()
        active_occ_count = count("SELECT COUNT(*) FROM occurrences WHERE camera_id=? AND archived_at IS NULL", (id,))
        execute("UPDATE occurrences SET archived_at=?, archived_contract_id=?, archived_location=? WHERE camera_id=? AND archived_at IS NULL", (now, c["contract_id"], old_loc, id))
        note = f"Câmera recebida na central. Dados operacionais limpos; {active_occ_count} ocorrência(s) arquivada(s) no histórico da obra; câmera aguardando teste."
        execute("UPDATE cameras SET status=?, contract_id=NULL, current_location='', service='', removal_authorized_at=NULL, removal_authorized_by=NULL, patrimonial_status=?, updated_at=? WHERE id=?", ("Aguardando teste", "Em estoque", now, id))
        execute("INSERT INTO camera_history(camera_id,old_contract_id,new_contract_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (id, c["contract_id"], None, old_loc, "", old_status, "Recebida na central / Aguardando teste", note, current_user()["name"], now))
        flash("Câmera recebida na central. Ela agora está aguardando teste.")
        return redirect(url_for("camera_view", id=id))
    body = f"""<div class='panel'><h2>🏢 Recebida na central</h2>
    <p>Confirme somente quando a câmera realmente chegou ao escritório/central.</p>
    <div class='card'><p><b>Câmera:</b> {c['code']}</p><p><b>Cliente:</b> {c['client_name'] or '-'}</p><p><b>Obra:</b> {c['obra'] or '-'}</p><p><b>Status atual:</b> {c['status']}</p></div>
    <form method='post' onsubmit="return confirm('Confirmar recebimento na central? A câmera ficará aguardando teste e o ciclo anterior será arquivado.')"><button class='primary'>Confirmar recebimento</button> <a class='btn' href='{url_for('camera_view', id=id)}'>Cancelar</a></form></div>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > Recebida na central {c['code']}")

def build_qr_label(code, cliente="", obra=""):
    qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=14, border=4)
    qr.add_data(code)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    label = Image.new("RGB", (900, 1100), "white")
    draw = ImageDraw.Draw(label)
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 68)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
        font_code = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
    except Exception:
        font_title = font_sub = font_code = None
    draw.text((450, 70), "7SENSE", anchor="mm", font=font_title, fill=(20, 70, 140))
    draw.text((450, 132), "Data into Action", anchor="mm", font=font_sub, fill=(90, 90, 90))
    qr_img = qr_img.resize((650, 650), Image.Resampling.NEAREST)
    label.paste(qr_img, (125, 185))
    draw.rounded_rectangle((210, 875, 690, 955), radius=18, fill=(20,70,140))
    draw.text((450, 915), code, anchor="mm", font=font_code, fill="white")
    
    if cliente or obra:
        draw.text((450, 985), (cliente or "Cliente não definido")[:34], anchor="mm", font=font_sub, fill=(30, 30, 30))
        draw.text((450, 1032), (obra or "Obra não definida")[:38], anchor="mm", font=font_sub, fill=(70, 70, 70))
    else:
        draw.text((450, 1000), "Patrimônio 7Sense", anchor="mm", font=font_sub, fill=(70, 70, 70))
    return label


@app.route("/cameras/<int:id>/qr")
@login_required
def camera_qr(id):
    c = one("""SELECT ca.*, co.obra, cl.name client_name FROM cameras ca LEFT JOIN contracts co ON co.id=ca.contract_id LEFT JOIN clients cl ON cl.id=co.client_id WHERE ca.id=?""", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))
    img = build_qr_label(c["code"], rv(c, "client_name", ""), rv(c, "obra", ""))
    bio = BytesIO()
    img.save(bio, "PNG")
    bio.seek(0)
    return send_file(bio, mimetype="image/png", as_attachment=True, download_name=f"{c['code']}_etiqueta_qr.png")


@app.route("/cameras/<int:id>/approve", methods=["GET", "POST"])
@operacao_required
def camera_approve(id):
    c = one("SELECT * FROM cameras WHERE id=?", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))

    checklist_items = [
        "Carregada",
        "Cartão SD verificado",
        "Limpeza realizada",
        "Teste de imagem",
        "Teste de comunicação",
        "Estado físico",
    ]

    def render_checklist(form_data=None, field_errors=None, summary_errors=None):
        form_data = form_data or {}
        field_errors = field_errors or {}
        summary_errors = summary_errors or []
        answered = sum(1 for item in checklist_items if form_data.get(f"result_{item}") in ("Aprovado", "Reprovado"))
        progress = int((answered / len(checklist_items)) * 100)
        pending_html = ""
        if summary_errors:
            pending_html = "<div class='card' style='border:1px solid #ef4444;background:rgba(239,68,68,.12);color:#fecaca'><b>Não foi possível concluir o teste.</b><ul>" + "".join(f"<li>{escape(e)}</li>" for e in summary_errors) + "</ul></div>"

        rows = []
        for item in checklist_items:
            result = form_data.get(f"result_{item}", "")
            obs = form_data.get(f"obs_{item}", "")
            err = field_errors.get(item, "")
            border = "border:1px solid #ef4444;background:rgba(239,68,68,.10)" if err else ""
            safe_item = escape(item)
            obs_safe = escape(obs)
            approved_checked = "checked" if result == "Aprovado" else ""
            rejected_checked = "checked" if result == "Reprovado" else ""
            err_html = f"<p style='color:#fca5a5;margin:8px 0 0'><b>⚠ {escape(err)}</b></p>" if err else ""
            rows.append(f"""
            <div class='card' style='margin-bottom:10px;{border}' id='item-{escape(item).replace(' ', '-')}' >
              <h3 style='margin-top:0'>{safe_item}</h3>
              <label style='display:inline-block;margin-right:18px'>
                <input type='radio' name='result_{safe_item}' value='Aprovado' required {approved_checked}> ✅ Aprovado
              </label>
              <label style='display:inline-block'>
                <input type='radio' name='result_{safe_item}' value='Reprovado' required {rejected_checked}> ❌ Reprovado
              </label>
              <textarea name='obs_{safe_item}' placeholder='Se reprovar este item, descreva o problema encontrado.' style='margin-top:10px'>{obs_safe}</textarea>
              {err_html}
            </div>""")
        boxes = "".join(rows)
        note_value = escape(form_data.get("note", ""))
        body = f"""<div class='panel'><h2>🧪 Testar câmera {escape(c['code'])}</h2>
        <p class='tag'>Marque cada item como aprovado ou reprovado. Se algum item for reprovado, descreva o motivo e envie a câmera para manutenção.</p>
        <div class='card' style='margin-bottom:14px'>
          <b>Checklist</b>
          <div style='height:10px;background:rgba(148,163,184,.25);border-radius:999px;overflow:hidden;margin:10px 0'>
            <div style='height:10px;width:{progress}%;background:#22c55e'></div>
          </div>
          <span class='muted'>{answered} de {len(checklist_items)} itens avaliados</span>
        </div>
        {pending_html}
        <form method='post' class='formgrid'>
          <div class='full'>{boxes}</div>
          <label class='full'>Observações gerais<textarea name='note' placeholder='Ex.: bateria ok, lente limpa, cartão substituído...'>{note_value}</textarea></label>
          <div class='full actions'>
            <button class='primary' name='action' value='approve'>✅ Aprovar câmera</button>
            <button class='danger' name='action' value='maintenance'>🔧 Enviar para manutenção</button>
            <a class='btn' href='{url_for('camera_view', id=id)}'>Cancelar</a>
          </div>
        </form></div>"""
        return page(body, breadcrumb=f"Dashboard > Câmeras > Teste {escape(c['code'])}")

    if request.method == "POST":
        action = request.form.get("action", "approve")
        results = []
        rejected = []
        notes_by_item = []
        field_errors = {}
        summary_errors = []
        form_data = {k: v for k, v in request.form.items()}

        for item in checklist_items:
            result = request.form.get(f"result_{item}", "").strip()
            obs = request.form.get(f"obs_{item}", "").strip()
            if result not in ("Aprovado", "Reprovado"):
                field_errors[item] = "Informe se este item foi aprovado ou reprovado."
                summary_errors.append(f"{item} não avaliado.")
                continue
            results.append(f"{item}: {result}" + ((f" ({obs})") if obs else ""))
            if result == "Reprovado":
                rejected.append(item)
                if not obs:
                    field_errors[item] = "Obrigatório informar o motivo da reprovação."
                    summary_errors.append(f"{item} reprovado sem observação.")
                else:
                    notes_by_item.append(f"{item}: {obs}")

        if field_errors:
            return render_checklist(form_data, field_errors, summary_errors)

        note = request.form.get("note", "").strip()
        checklist = "; ".join(results) + ((" | Observações gerais: " + note) if note else "")
        old_status = c["status"]
        now = datetime.now().isoformat()
        if rejected:
            if action != "maintenance":
                summary_errors.append("Existem itens reprovados. Use a opção Enviar para manutenção.")
                return render_checklist(form_data, {}, summary_errors)
            manut_note = "Teste reprovado. Itens reprovados: " + "; ".join(notes_by_item)
            if note:
                manut_note += " | Observações gerais: " + note
            execute("UPDATE cameras SET status=?, patrimonial_status=?, tested_checklist=?, notes=?, updated_at=? WHERE id=?", ("Em manutenção", "Em manutenção", checklist, (((c["notes"] or "") + "\n" + manut_note).strip()), now, id))
            execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (id, c["current_location"], c["current_location"], old_status, "Em manutenção", manut_note, current_user()["name"], now))
            flash("Teste reprovado. Câmera enviada para manutenção.")
            return redirect(url_for("camera_maintenance", id=id))
        if action == "maintenance":
            summary_errors.append("Nenhum item foi reprovado. Para enviar à manutenção, marque ao menos um item como reprovado.")
            return render_checklist(form_data, {}, summary_errors)
        execute("UPDATE cameras SET status=?, patrimonial_status=?, tested_approved_at=?, tested_checklist=?, updated_at=? WHERE id=?", ("Testada e aprovada", "Em estoque", now, checklist, now, id))
        execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (id, c["current_location"], c["current_location"], old_status, "Testada e aprovada", "Checklist aprovado: " + checklist, current_user()["name"], now))
        flash("Câmera testada e aprovada para novo envio.")
        return redirect(url_for("camera_view", id=id))

    return render_checklist()

@app.route("/maintenance")
@operacao_required
def maintenance_page():
    rows = query("""SELECT cameras.*, contracts.obra, contracts.city contract_city, contracts.state contract_state, clients.name client_name
                    FROM cameras
                    LEFT JOIN contracts ON contracts.id=cameras.contract_id
                    LEFT JOIN clients ON clients.id=contracts.client_id
                    WHERE cameras.status=? AND cameras.demo=?
                    ORDER BY cameras.updated_at DESC, cameras.code""", ("Em manutenção", current_demo_flag()))
    total = len(rows)

    def maintenance_row(c):
        failed = parse_failed_checklist(rv(c, 'tested_checklist', '') or '')
        if failed:
            reason = "; ".join([f"{f['item']}: {f['obs']}" if f.get('obs') else f['item'] for f in failed[:3]])
            if len(failed) > 3:
                reason += f"; +{len(failed)-3} item(ns)"
        else:
            reason = "Manutenção sem item reprovado vinculado"
        code = escape(rv(c, 'code', '-') or '-')
        cliente = escape(rv(c, 'client_name', 'Sem cliente / estoque') or 'Sem cliente / estoque')
        obra = escape(rv(c, 'obra', 'Sem obra / estoque') or 'Sem obra / estoque')
        local = escape(rv(c, 'current_location', '-') or '-')
        updated = escape((rv(c, 'updated_at', '') or '')[:16].replace('T',' '))
        return f"""<div class='row camera'>
            <b>{code}</b>
            <span><b>{cliente}</b><br><small>{obra}</small></span>
            <span>{escape(reason)}</span>
            <span>{local}<br><small>{updated}</small></span>
            <span><span class='badge warn'>Em manutenção</span></span>
            <span class='actions'><a class='btn small primary' href='{url_for('camera_maintenance', id=rv(c,'id'))}'>🔍 Verificar</a><a class='btn small' href='{url_for('camera_dossie', id=rv(c,'id'))}'>📑 Dossiê</a></span>
        </div>"""

    items = "".join(maintenance_row(c) for c in rows) or "<div class='card'><p class='tag'>Nenhuma câmera em manutenção neste momento.</p></div>"
    body = f"""<div class='panel'>
      <h2>🔧 Manutenção</h2>
      <p class='tag'>Câmeras reprovadas no controle de qualidade ou encaminhadas para reparo. O motivo principal já aparece na lista; clique em <b>Verificar</b> para resolver os itens, aprovar a câmera ou condenar o equipamento.</p>
      <div class='grid' style='margin:14px 0'>
        <a class='card metric' href='{url_for('cameras', status='Em manutenção')}'><h3>Câmeras em manutenção</h3><b>{total}</b></a>
      </div>
      <div class='row camera' style='color:#8da0ba;font-weight:700'><span>Câmera</span><span>Cliente / Obra</span><span>Motivo</span><span>Local / Data</span><span>Status</span><span>Ações</span></div>
      {items}
    </div>"""
    return page(body, breadcrumb="Dashboard > Centro de Manutenção")

@app.route("/cameras/<int:id>/maintenance", methods=["GET", "POST"])
@operacao_required
def camera_maintenance(id):
    c = one("SELECT * FROM cameras WHERE id=?", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))

    failed_items = parse_failed_checklist(rv(c, 'tested_checklist', '') or '')

    def render_maintenance(form_data=None, field_errors=None, summary_errors=None):
        form_data = form_data or {}
        field_errors = field_errors or {}
        summary_errors = summary_errors or []
        pending_html = ""
        if summary_errors:
            pending_html = "<div class='card' style='border:1px solid #ef4444;background:rgba(239,68,68,.12);color:#fecaca'><b>Não foi possível concluir a manutenção.</b><ul>" + "".join(f"<li>{escape(e)}</li>" for e in summary_errors) + "</ul></div>"

        if failed_items:
            item_cards = []
            resolved_count = 0
            for i, fail in enumerate(failed_items):
                item = fail['item']
                original_obs = fail['obs']
                key = f"item_{i}"
                result = form_data.get(f"result_{key}", "")
                obs = form_data.get(f"obs_{key}", "")
                if result == "Resolvido":
                    resolved_count += 1
                err = field_errors.get(key, "")
                border = "border:1px solid #ef4444;background:rgba(239,68,68,.10)" if err else ""
                checked_ok = "checked" if result == "Resolvido" else ""
                checked_bad = "checked" if result == "Sem solução" else ""
                err_html = f"<p style='color:#fca5a5;margin:8px 0 0'><b>⚠ {escape(err)}</b></p>" if err else ""
                item_cards.append(f"""
                <div class='card' style='margin-bottom:10px;{border}'>
                  <h3 style='margin-top:0'>{escape(item)}</h3>
                  <p class='tag'><b>Problema registrado no teste:</b> {escape(original_obs or 'Sem observação anterior.')}</p>
                  <label style='display:inline-block;margin-right:18px'>
                    <input type='radio' name='result_{key}' value='Resolvido' required {checked_ok}> ✅ Resolvido / aprovado
                  </label>
                  <label style='display:inline-block'>
                    <input type='radio' name='result_{key}' value='Sem solução' required {checked_bad}> ❌ Sem solução
                  </label>
                  <textarea name='obs_{key}' placeholder='O que foi feito neste item? Ex.: cartão substituído, conector limpo, teste de imagem normalizado...' style='margin-top:10px'>{escape(obs)}</textarea>
                  {err_html}
                </div>""")
            progress = int((resolved_count / len(failed_items)) * 100)
            checklist_html = f"""
              <div class='card' style='margin-bottom:14px'>
                <b>Itens reprovados no teste</b>
                <div style='height:10px;background:rgba(148,163,184,.25);border-radius:999px;overflow:hidden;margin:10px 0'>
                  <div style='height:10px;width:{progress}%;background:#22c55e'></div>
                </div>
                <span class='muted'>{resolved_count} de {len(failed_items)} itens resolvidos</span>
              </div>
              {''.join(item_cards)}
            """
        else:
            checklist_html = """<div class='card'><h3>Nenhum item reprovado encontrado</h3><p class='tag'>Registre os serviços realizados ou condene o equipamento, se não houver reparo viável.</p></div>"""

        details_value = escape(form_data.get('details', ''))
        body = f"""<div class='panel'><h2>🔧 Manutenção {escape(c['code'])}</h2>
          <p><span class='badge warn'>{escape(c['status'])}</span></p>
          <p class='tag'>Verifique somente os itens que foram reprovados no teste. Se todos forem corrigidos, aprove a câmera diretamente. Se não houver conserto, condene o equipamento.</p>
          {pending_html}
          <form method='post' class='formgrid'>
            <div class='full'>{checklist_html}</div>
            <label class='full'>Observações gerais da manutenção<textarea name='details' placeholder='Ex.: limpeza realizada, conector substituído, cartão SD novo, equipamento sem reparo...'>{details_value}</textarea></label>
            <div class='full actions'>
              <button class='primary' name='action' value='approve'>✅ Aprovar câmera</button>
              <button class='danger' name='action' value='condemn' onclick="return confirm('Confirmar condenação do equipamento?')">🚫 Condenar equipamento</button>
              <a class='btn' href='{url_for('camera_view', id=id)}'>Cancelar</a>
            </div>
          </form>
        </div>"""
        return page(body, breadcrumb=f"Dashboard > Câmeras > Manutenção {escape(c['code'])}")

    if request.method == "POST":
        action = request.form.get("action")
        details = request.form.get("details", "").strip()
        now = datetime.now().isoformat()
        old_status = c["status"]
        form_data = {k: v for k, v in request.form.items()}
        field_errors = {}
        summary_errors = []

        if action == "approve":
            resolved_notes = []
            if failed_items:
                for i, fail in enumerate(failed_items):
                    key = f"item_{i}"
                    result = request.form.get(f"result_{key}", "").strip()
                    obs = request.form.get(f"obs_{key}", "").strip()
                    if result not in ("Resolvido", "Sem solução"):
                        field_errors[key] = "Informe se este item foi resolvido ou permanece sem solução."
                        summary_errors.append(f"{fail['item']} não verificado.")
                    elif result == "Sem solução":
                        field_errors[key] = "Item ainda sem solução. Para aprovar a câmera, todos os itens precisam estar resolvidos."
                        summary_errors.append(f"{fail['item']} permanece sem solução.")
                    else:
                        resolved_notes.append(f"{fail['item']}: resolvido" + (f" ({obs})" if obs else ""))
                if field_errors:
                    return render_maintenance(form_data, field_errors, summary_errors)
            note = "Manutenção concluída e câmera aprovada. Itens corrigidos: " + ("; ".join(resolved_notes) if resolved_notes else "Sem itens reprovados pendentes")
            if details:
                note += " | Observações gerais: " + details
            execute("UPDATE cameras SET status=?, patrimonial_status=?, tested_approved_at=?, notes=?, updated_at=? WHERE id=?", ("Testada e aprovada", "Em estoque", now, (((c["notes"] or "") + "\n" + note).strip()), now, id))
            execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (id, c["current_location"], c["current_location"], old_status, "Testada e aprovada", note, current_user()["name"], now))
            flash("Manutenção concluída. Câmera testada/aprovada e liberada para novo envio.")
            return redirect(url_for("camera_view", id=id))

        if action == "condemn":
            if not details:
                summary_errors.append("Informe o motivo para condenar o equipamento.")
                return render_maintenance(form_data, {}, summary_errors)
            note = "Equipamento condenado pela manutenção. Motivo: " + details
            execute("UPDATE cameras SET status=?, patrimonial_status=?, notes=?, updated_at=? WHERE id=?", ("Inutilizada", "Inutilizada", (((c["notes"] or "") + "\n" + note).strip()), now, id))
            execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (id, c["current_location"], c["current_location"], old_status, "Inutilizada", note, current_user()["name"], now))
            flash("Câmera condenada e marcada como inutilizada. O dossiê foi preservado.")
            return redirect(url_for("camera_dossie", id=id))

    return render_maintenance()

@app.route("/cameras/<int:id>/transfer", methods=["GET", "POST"])
@operacao_required
def camera_transfer(id):
    c = one("SELECT * FROM cameras WHERE id=?", (id,))
    clients_rows = query("SELECT * FROM clients WHERE active=1 AND demo=? ORDER BY name", (current_demo_flag(),))
    contracts_rows = query("SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE c.demo=? ORDER BY cl.name, c.obra", (current_demo_flag(),))
    selected_client_id = ""
    if c and c["contract_id"]:
        cr = one("SELECT client_id FROM contracts WHERE id=?", (c["contract_id"],))
        selected_client_id = str(cr["client_id"]) if cr and cr["client_id"] else ""
    if request.method == "POST":
        old = c
        new_contract = request.form.get("contract_id") or None
        new_loc = request.form.get("current_location")
        new_service = request.form.get("service")
        new_status = request.form.get("status")
        note = request.form.get("note")
        # V2.0.7: bloqueia transporte/obra se câmera não foi testada e aprovada.
        # Evita que câmera aguardando teste saia para cliente por engano.
        if not can_send_to_field(old["status"]):
            if (not old["contract_id"] and new_contract) or new_status in field_statuses():
                flash("Esta câmera ainda não está testada e aprovada. Conclua o checklist antes de liberar transporte ou vínculo com obra.")
                return redirect(url_for("camera_transfer", id=id))
        execute("UPDATE cameras SET contract_id=?,current_location=?,service=?,status=?,updated_at=? WHERE id=?", (new_contract,new_loc,new_service,new_status,datetime.now().isoformat(),id))
        execute("INSERT INTO camera_history(camera_id,old_contract_id,new_contract_id,old_location,new_location,old_service,new_service,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (id, old['contract_id'], new_contract, old['current_location'], new_loc, old['service'], new_service, old['status'], new_status, note, current_user()['name'], datetime.now().isoformat()))
        flash("Câmera transferida/atualizada.")
        return redirect(url_for("camera_view", id=id))
    client_opts = "<option value=''>Sem cliente / estoque</option>" + "".join([f"<option value='{cl['id']}' {'selected' if selected_client_id==str(cl['id']) else ''}>{cl['name']}</option>" for cl in clients_rows])
    opts = "<option value='' data-client=''>Sem contrato / estoque</option>" + "".join([f"<option value='{r['id']}' data-client='{r['client_id'] or ''}' {'selected' if c['contract_id']==r['id'] else ''}>{r['client_name'] or 'Sem cliente'} - {r['obra']}</option>" for r in contracts_rows])
    services = "".join([f"<option {'selected' if c['service']==s else ''}>{s}</option>" for s in SERVICOS])
    statuses = "".join([f"<option {'selected' if c['status']==s else ''}>{s}</option>" for s in STATUS_CAMERA])
    bloqueio_html = ""
    if not can_send_to_field(c["status"]):
        bloqueio_html = "<p style='background:#fef3c7;border-radius:12px;padding:12px'><b>Envio bloqueado:</b> esta câmera precisa estar Testada e aprovada antes de ser enviada para obra ou transporte.</p>"
    body = f"""<div class="panel"><h2>Transferir / Atualizar {c['code']}</h2><p class="tag">Selecione o cliente para filtrar somente as obras/contratos desse cliente.</p>{bloqueio_html}<form method="post" class="formgrid">
    <label>Cliente<select id="client_select" name="client_select">{client_opts}</select></label><label>Novo contrato / obra<select id="contract_select" name="contract_id">{opts}</select></label>
    <label>Novo local<input name="current_location" value="{c['current_location'] or ''}"></label><label>Novo serviço<select name="service">{services}</select></label>
    <label>Novo status<select name="status">{statuses}</select></label><label class="full">Observação da movimentação<textarea name="note"></textarea></label><div class="full"><button class="primary">Salvar transferência</button></div></form></div>
    <script>
    function filtrarContratos(){{
      const cliente = document.getElementById('client_select').value;
      const contrato = document.getElementById('contract_select');
      let selectedStillVisible = false;
      Array.from(contrato.options).forEach(opt => {{
        const show = !opt.value || !cliente || opt.dataset.client === cliente;
        opt.hidden = !show;
        if (opt.selected && show) selectedStillVisible = true;
      }});
      if (!selectedStillVisible) contrato.value = '';
    }}
    document.getElementById('client_select').addEventListener('change', filtrarContratos);
    filtrarContratos();
    </script>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > Transferir {c['code']}")


@app.route("/occurrences")
@login_required
def occurrences():
    status = request.args.get("status")
    if status == "abertas":
        where = "WHERE o.status IN ('Aberta','Em andamento') AND o.archived_at IS NULL AND o.demo=?"
    elif status == "arquivadas":
        where = "WHERE o.archived_at IS NOT NULL AND o.demo=?"
    else:
        where = "WHERE o.archived_at IS NULL AND o.demo=?"
    rows = query(f"""SELECT o.*, ca.code camera_code, ca.current_location, co.obra, cl.name client_name
                     FROM occurrences o
                     LEFT JOIN cameras ca ON ca.id=o.camera_id
                     LEFT JOIN contracts co ON co.id=ca.contract_id
                     LEFT JOIN clients cl ON cl.id=co.client_id
                     {where}
                     ORDER BY o.created_at DESC""", (current_demo_flag(),))
    can_edit = current_user()["role"] == "operacao"
    items = "".join([f"""<div class='row'>
        <b>{r['camera_code'] or '-'}</b>
        <span>{r['title']}</span>
        <span>{r['status']}</span>
        <span>{(r['created_at'] or '')[:16]}</span>
        <span><a class='btn small' href='{url_for('occurrence_close', id=r['id'])}'>{'Ver / resolver' if can_edit and r['status']!='Resolvida' else 'Ver detalhe'}</a></span>
    </div>""" for r in rows])
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>Ocorrências</h2>{'<a class=\"btn primary\" href=\"'+url_for('occurrence_new')+'\">Nova ocorrência</a>' if can_edit else ''}</div>{items or '<p>Nenhuma ocorrência.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Ocorrências")


@app.route("/occurrences/new", methods=["GET", "POST"])
@operacao_required
def occurrence_new():
    cameras_rows = query("SELECT * FROM cameras WHERE demo=? ORDER BY code", (current_demo_flag(),))
    cam_id = request.args.get("camera_id")
    if request.method == "POST":
        execute("INSERT INTO occurrences(camera_id,title,problem,status,responsible,notes,demo,created_at) VALUES(?,?,?,?,?,?,?,?)", (request.form.get("camera_id"), request.form.get("title"), request.form.get("problem"), "Aberta", request.form.get("responsible"), request.form.get("notes"), current_demo_flag(), datetime.now().isoformat()))
        flash("Ocorrência criada.")
        return redirect(url_for("occurrences"))
    opts = "".join([f"<option value='{c['id']}' {'selected' if cam_id and str(c['id'])==str(cam_id) else ''}>{c['code']}</option>" for c in cameras_rows])
    body = f"""<div class="panel"><h2>Nova ocorrência</h2><form method="post" class="formgrid"><label>Câmera<select name="camera_id">{opts}</select></label><label>Título<input name="title" value="Problema operacional"></label><label>Problema<input name="problem"></label><label>Responsável<input name="responsible"></label><label class="full">Observações<textarea name="notes"></textarea></label><div class="full"><button class="primary">Salvar</button></div></form></div>"""
    return page(body, breadcrumb="Dashboard > Ocorrências > Nova")


@app.route("/occurrences/<int:id>/close", methods=["GET", "POST"])
@operacao_required
def occurrence_close(id):
    r = one("""SELECT o.*, ca.code camera_code, ca.current_location, co.obra, cl.name client_name
               FROM occurrences o
               LEFT JOIN cameras ca ON ca.id=o.camera_id
               LEFT JOIN contracts co ON co.id=ca.contract_id
               LEFT JOIN clients cl ON cl.id=co.client_id
               WHERE o.id=?""", (id,))
    if not r:
        flash("Ocorrência não encontrada.")
        return redirect(url_for("occurrences"))
    if request.method == "POST":
        resolution_notes = request.form.get("resolution_notes", "").strip()
        execute("UPDATE occurrences SET status='Resolvida', closed_at=?, resolution_notes=?, resolved_by=? WHERE id=?", (datetime.now().isoformat(), resolution_notes, current_user()["name"], id))
        flash("Ocorrência marcada como solucionada.")
        return redirect(url_for("occurrences"))

    detalhes = f"""
    <div class='panel'>
      <h2>Detalhe da ocorrência</h2>
      <p><span class='badge danger'>{r['status']}</span></p>
      <div class='grid'>
        <div><b>Câmera</b><br>{r['camera_code'] or '-'}</div>
        <div><b>Cliente</b><br>{r['client_name'] or '-'}</div>
        <div><b>Obra</b><br>{r['obra'] or '-'}</div>
        <div><b>Local</b><br>{r['current_location'] or '-'}</div>
      </div>
      <hr style='border:0;border-top:1px solid #22314f;margin:18px 0'>
      <p><b>Título:</b><br>{r['title'] or '-'}</p>
      <p><b>Problema registrado:</b><br>{r['problem'] or '-'}</p>
      <p><b>Observações:</b><br>{r['notes'] or '-'}</p>
      <p><b>Responsável:</b><br>{r['responsible'] or '-'}</p>
      <p><b>Data de abertura:</b><br>{(r['created_at'] or '')[:16]}</p>
    </div>
    """
    if r['status'] == 'Resolvida':
        acao = f"""<div class='panel'><h2>Ocorrência solucionada</h2><p><b>Solucionado por:</b> {rv(r, 'resolved_by', '-') or '-'}</p><p><b>Data:</b> {(rv(r, 'closed_at', '') or '')[:16]}</p><p><b>Solução aplicada:</b><br>{rv(r, 'resolution_notes', '-') or '-'}</p><a class='btn' href='{url_for('occurrences')}'>Voltar</a></div>"""
    else:
        acao = f"""<div class='panel'><h2>Resolver ocorrência</h2><p class='tag'>Confira o problema antes de marcar como solucionado. Enquanto a ocorrência estiver aberta, o fluxo de campo da câmera fica bloqueado.</p><form method='post' class='formgrid'><label class='full'>O que foi feito para corrigir?<textarea name='resolution_notes' placeholder='Ex.: Conector substituído, câmera religada, suporte ajustado...'></textarea></label><div class='full actions'><a class='btn' href='{url_for('occurrences')}'>Cancelar</a><button class='primary'>✅ Marcar como solucionada</button></div></form></div>"""
    return page(detalhes + acao, breadcrumb="Dashboard > Ocorrências > Detalhe")


@app.route("/agenda")
@login_required
def agenda_page():
    filtro = request.args.get("filtro", "proximos")
    today = date.today().isoformat()
    if filtro == "hoje":
        rows = query("SELECT * FROM agenda WHERE event_date=? AND demo=? ORDER BY event_time", (today, current_demo_flag()))
        titulo = "Agenda de hoje"
    elif filtro == "todos":
        rows = query("SELECT * FROM agenda WHERE demo=? ORDER BY event_date,event_time LIMIT 200", (current_demo_flag(),))
        titulo = "Todos os agendamentos"
    else:
        rows = query("SELECT * FROM agenda WHERE event_date>=? AND demo=? ORDER BY event_date,event_time LIMIT 100", (today, current_demo_flag()))
        titulo = "Próximos agendamentos"
    can_edit = current_user()["role"] == "operacao"
    filters = f"""
      <div class='filters'>
        <a class='{ 'active' if filtro=='proximos' else '' }' href='{url_for('agenda_page', filtro='proximos')}'>Próximos</a>
        <a class='{ 'active' if filtro=='hoje' else '' }' href='{url_for('agenda_page', filtro='hoje')}'>Hoje</a>
        <a class='{ 'active' if filtro=='todos' else '' }' href='{url_for('agenda_page', filtro='todos')}'>Todos</a>
      </div>
    """
    items = "".join([f"<div class='row'><b>{r['event_date']} {r['event_time'] or ''}</b><span>{r['title']}</span><span>{r['notes'] or ''}</span><span></span><span></span></div>" for r in rows])
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>{titulo}</h2>{'<a class="btn primary" href="'+url_for('agenda_new')+'">Novo evento</a>' if can_edit else ''}</div>{filters}{items or '<p>Nenhum evento encontrado.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Agenda Operacional")


@app.route("/agenda/new", methods=["GET", "POST"])
@operacao_required
def agenda_new():
    if request.method == "POST":
        execute("INSERT INTO agenda(title,event_date,event_time,notes,demo,created_at) VALUES(?,?,?,?,?,?)", (request.form.get("title"), request.form.get("event_date"), request.form.get("event_time"), request.form.get("notes"), current_demo_flag(), datetime.now().isoformat()))
        flash("Evento criado.")
        return redirect(url_for("agenda_page", filtro="proximos"))
    body = """<div class="panel"><h2>Novo evento</h2><p class='tag'>Selecione a data e o horário pelo calendário do navegador. O evento aparecerá automaticamente no Dashboard em Próximos agendamentos.</p><form method="post" class="formgrid"><label>Título<input name="title" required placeholder="Ex.: Instalação Toyota"></label><label>Data<input type="date" name="event_date" required onclick="this.showPicker && this.showPicker()"></label><label>Hora<input type="time" name="event_time" required onclick="this.showPicker && this.showPicker()"></label><label class="full">Observações<textarea name="notes" placeholder="Detalhes do agendamento"></textarea></label><div class="full"><button class="primary">Salvar agendamento</button></div></form></div>"""
    return page(body, breadcrumb="Dashboard > Agenda > Novo")


@app.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    like = f"%{q}%"
    clients_rs = query("SELECT 'Cliente' tipo, name titulo, city detalhe, '/clients' link FROM clients WHERE demo=? AND (name LIKE ? OR city LIKE ?)", (current_demo_flag(), like, like)) if q else []
    contracts_rs = query("SELECT 'Contrato' tipo, coalesce(cl.name,'') || ' - ' || coalesce(c.obra,'') titulo, c.city detalhe, '/contracts/' || c.id link FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE c.demo=? AND (cl.name LIKE ? OR c.obra LIKE ? OR c.city LIKE ? OR c.code LIKE ?)", (current_demo_flag(), like, like, like, like)) if q else []
    cameras_rs = query("SELECT 'Câmera' tipo, code titulo, current_location detalhe, '/cameras/' || id link FROM cameras WHERE demo=? AND (code LIKE ? OR current_location LIKE ? OR service LIKE ?)", (current_demo_flag(), like, like, like)) if q else []
    occ_rs = query("SELECT 'Ocorrência' tipo, title titulo, problem detalhe, '/occurrences' link FROM occurrences WHERE archived_at IS NULL AND demo=? AND (title LIKE ? OR problem LIKE ?)", (current_demo_flag(), like, like)) if q else []
    rows = list(clients_rs)+list(contracts_rs)+list(cameras_rs)+list(occ_rs)
    items = "".join([f"<div class='row'><b>{r['tipo']}</b><span>{r['titulo']}</span><span>{r['detalhe'] or ''}</span><span></span><span><a class='btn small' href='{r['link']}'>Abrir</a></span></div>" for r in rows])
    body = f"<div class='panel'><h2>Pesquisa: {q}</h2><form class='search'><input name='q' value='{q}'><button>Pesquisar</button></form>{items or '<p>Nenhum resultado.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Pesquisa")



@app.route("/campo-links")
@login_required
def field_links():
    """Painel para gerar links de campo por obra/contrato."""
    rows = query("""
        SELECT c.*, cl.name client_name, cl.responsible, cl.phone,
               (SELECT COUNT(*) FROM cameras ca WHERE ca.contract_id=c.id AND ca.demo=c.demo) cam_count
        FROM contracts c
        LEFT JOIN clients cl ON cl.id=c.client_id
        WHERE c.demo=? AND c.status <> 'Encerrado'
        ORDER BY cl.name, c.obra
    """, (current_demo_flag(),))
    groups = {}
    for r in rows:
        groups.setdefault(r['client_name'] or 'Sem cliente', []).append(r)
    def jsq(v):
        return (v or '').replace('\\','\\\\').replace("'", "\\'").replace('\n',' ')
    blocks = []
    for client_name, contracts_list in groups.items():
        inner = []
        for r in contracts_list:
            path = url_for('campo_contract', contract_id=r['id'])
            full = request.host_url.rstrip('/') + path
            msg = f"""7Sense Operations Manager - Link de acesso à operação de campo

Cliente: {r['client_name'] or 'Sem cliente'}
Obra: {r['obra'] or 'Obra sem nome'} - {r['city'] or ''}/{r['state'] or ''}

Acesse pelo celular:
{full}

Após abrir o link, leia o QR Code da câmera e siga apenas as etapas liberadas pelo sistema. Em caso de problema, utilize a opção Registrar problema."""
            wa_url = "https://web.whatsapp.com/send?text=" + quote(msg)
            inner.append(f"""
            <div class='row' style='grid-template-columns:1.4fr .8fr .7fr auto'>
                <div><b>🏗️ {r['obra'] or 'Obra sem nome'}</b><br><small>{r['city'] or ''}/{r['state'] or ''}</small></div>
                <span>{r['cam_count']} câmera(s)</span>
                <span><span class='badge {status_class(r['status'])}'>{r['status']}</span></span>
                <span class='actions'>
                    <button type='button' class='btn small' onclick="copyText('{jsq(msg)}')">🔗 Copiar link de acesso</button>
                    <a class='btn small' target='_blank' rel='noopener' href='{wa_url}'>💬 Enviar link de acesso</a>
                    <a class='btn small' target='_blank' href='{path}'>Abrir</a>
                </span>
            </div>""")
        blocks.append(f"<details class='panel' open><summary><b>👥 {client_name}</b> <span class='badge'>{len(contracts_list)} obra(s)</span></summary>{''.join(inner)}</details>")
    body = f"""
    <div class='panel'>
        <h2>Campo por obra</h2>
        <p class='tag'>Gere o link de acesso da obra para enviar ao técnico. O botão copiar leva a mensagem completa com cliente, obra e link; o WhatsApp abre com essa mesma mensagem já preenchida.</p>
    </div>
    {''.join(blocks) if blocks else '<div class="panel"><p>Nenhum contrato ativo encontrado.</p></div>'}
    <script>
    function copyText(txt){{
        navigator.clipboard.writeText(txt).then(()=>alert('Copiado para a área de transferência.'));
    }}
    </script>
    """
    return page(body, breadcrumb="Dashboard > Operação de Campo")

@app.route("/campo", methods=["GET", "POST"])
def campo():
    return campo_core(None)

@app.route("/campo/contrato/<int:contract_id>", methods=["GET", "POST"])
def campo_contract(contract_id):
    return campo_core(contract_id)

def campo_core(contract_id=None):
    msg = ""
    camera = None
    history = []
    buttons_html = ""
    contract_ctx = one("""SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE c.id=?""", (contract_id,)) if contract_id else None
    contract_blocked = False
    code = request.form.get("code", "").strip().upper() if request.method == "POST" else request.args.get("code", "").strip().upper()

    def load_camera_by_code(camera_code):
        return one("""SELECT ca.*, co.obra, cl.name client_name,
                    (SELECT COUNT(*) FROM occurrences o WHERE o.camera_id=ca.id AND o.archived_at IS NULL AND o.status IN ('Aberta','Em andamento')) AS active_occ_count,
                    (SELECT o.id FROM occurrences o WHERE o.camera_id=ca.id AND o.archived_at IS NULL AND o.status IN ('Aberta','Em andamento') ORDER BY o.created_at DESC LIMIT 1) AS active_occ_id
                    FROM cameras ca
                    LEFT JOIN contracts co ON co.id=ca.contract_id
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    WHERE UPPER(ca.code)=?""", (camera_code,))

    if code:
        camera = load_camera_by_code(code)
        if not camera:
            msg = "Câmera não encontrada. Confira o código."
        elif contract_ctx:
            if not camera["contract_id"]:
                contract_blocked = True
                msg = "Esta câmera ainda não está vinculada a esta obra. Primeiro transfira/reserve a câmera para esta obra no painel antes de operar pelo link de campo."
            elif camera["contract_id"] != contract_id:
                contract_blocked = True
                msg = "Esta câmera está vinculada a outra obra/contrato. Confira o QR Code ou transfira a câmera pelo painel antes de operar neste link."

    if request.method == "POST" and camera:
        action = request.form.get("action")
        if contract_ctx and action:
            if not camera["contract_id"]:
                contract_blocked = True
                msg = "Operação bloqueada: esta câmera ainda não está vinculada a esta obra. Reserve/transfira no painel antes de usar este link de campo."
                action = None
            elif camera["contract_id"] != contract_id:
                contract_blocked = True
                msg = "Operação bloqueada: esta câmera está vinculada a outra obra/contrato."
                action = None
        workflow = ["Em transporte", "Na obra aguardando instalação", "Instalando", "Em operação", "Retirada"]
        action_labels = {
            "Em transporte": "🚚 Em transporte",
            "Na obra aguardando instalação": "📍 Na obra / aguardando instalação",
            "Instalando": "🛠 Instalando",
            "Em operação": "🟢 Ativar câmera",
            "Retirada": "↩️ Retirada",
        }

        # Descobre a maior etapa já registrada no histórico ou no status atual.
        hist_statuses = query("SELECT new_status FROM camera_history WHERE camera_id=?", (camera["id"],))
        max_done = -1
        # Regra v1.5.1: se a câmera estiver em estoque, o fluxo de campo reinicia.
        # Isso permite reutilizar uma câmera ou testar um QR sem herdar etapas antigas.
        if camera["status"] in ("Testada e aprovada",):
            max_done = -1
        elif camera["status"] == "Aguardando teste":
            max_done = -2
        elif camera["status"] in workflow:
            max_done = max(max_done, workflow.index(camera["status"]))
        else:
            # Para status fora do fluxo, preserva a etapa mais avançada já realizada.
            for hs in hist_statuses:
                if hs["new_status"] in workflow:
                    max_done = max(max_done, workflow.index(hs["new_status"]))
        next_step = None if max_done == -2 else (workflow[max_done + 1] if max_done + 1 < len(workflow) else None)
        # V2.0: retirada só pode ser executada em campo após autorização no painel.
        if next_step == "Retirada" and camera["status"] != "Aguardando retirada":
            next_step = None
        if camera["status"] == "Aguardando retirada":
            max_done = workflow.index("Em operação")
            next_step = "Retirada"
        active_occ_count = int(rv(camera, 'active_occ_count', 0) or 0)
        occurrence_blocked = active_occ_count > 0
        if contract_ctx and (not camera["contract_id"] or camera["contract_id"] != contract_id):
            contract_blocked = True
            next_step = None
        if occurrence_blocked:
            next_step = None

        if occurrence_blocked and action not in (None, "PROBLEMA"):
            msg = "Operação bloqueada: existe ocorrência aberta para esta câmera. Resolva/feche a ocorrência no painel antes de continuar a instalação."
        elif action == "PROBLEMA":
            problem = request.form.get("problem") or "Problema operacional"
            note = request.form.get("note") or ""
            execute("INSERT INTO occurrences(camera_id,title,problem,status,responsible,notes,created_at) VALUES(?,?,?,?,?,?,?)", (camera["id"], "Problema registrado em campo", problem, "Aberta", "Campo", note, datetime.now().isoformat()))
            execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (camera["id"], camera["current_location"], camera["current_location"], camera["status"], "Problema registrado", problem + (" - " + note if note else ""), "Campo", datetime.now().isoformat()))
            msg = "Problema registrado. A ocorrência foi aberta no painel."
        elif action in workflow:
            if camera["status"] == "Aguardando teste":
                msg = "Esta câmera ainda está aguardando teste. Ela só pode ser enviada para obra depois de testada e aprovada no painel."
            elif action != next_step:
                msg = "Esta etapa já foi realizada ou está fora de ordem. Leia o QR Code novamente e siga a próxima etapa liberada."
            else:
                old_status, old_loc = camera["status"], camera["current_location"]
                new_loc = request.form.get("local") or camera["current_location"]
                note = request.form.get("note") or ""
                # V3.0.3: link de campo é restrito à obra.
                # A câmera precisa estar previamente reservada/vinculada ao contrato no painel;
                # o campo não faz vínculo automático para evitar instalação em obra errada.
                install_photo_data = ""
                # V1.9: foto opcional da instalação, enviada somente na etapa de ativação.
                if action == "Em operação" and "install_photo" in request.files:
                    f = request.files.get("install_photo")
                    if f and f.filename:
                        raw = f.read()
                        if raw:
                            # Reduz a foto antes de salvar no banco, mantendo o sistema leve para uso em campo.
                            try:
                                im = Image.open(BytesIO(raw)).convert("RGB")
                                im.thumbnail((1200, 1200))
                                out = BytesIO()
                                im.save(out, format="JPEG", quality=72, optimize=True)
                                raw = out.getvalue()
                                mime = "image/jpeg"
                            except Exception:
                                mime = f.mimetype or "image/jpeg"
                            install_photo_data = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
                if action == "Em operação" and not install_photo_data:
                    msg = "Para ativar a câmera, é obrigatório tirar/enviar a foto da instalação."
                elif action == "Retirada":
                    # Retirada da obra NÃO significa recebida na central.
                    # Mantém cliente/obra/local vinculados e muda para Em retorno.
                    # O ciclo só será limpo quando o gestor confirmar "Recebida na central" no painel.
                    now = datetime.now().isoformat()
                    ret_note = (note + " | " if note else "") + "Retirada da obra confirmada em campo. Câmera em retorno para a central."
                    execute("UPDATE cameras SET status=?, patrimonial_status=?, updated_at=? WHERE id=?", ("Em retorno", "Em retorno", now, camera["id"]))
                    execute("INSERT INTO camera_history(camera_id,old_contract_id,new_contract_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (camera["id"], camera["contract_id"], camera["contract_id"], old_loc, old_loc, old_status, "Em retorno", ret_note, "Campo", now))
                    msg = "Câmera retirada da obra. Ela está em retorno para a central."
                else:
                    if install_photo_data:
                        execute("UPDATE cameras SET status=?, current_location=?, last_install_photo=?, updated_at=? WHERE id=?", (action, new_loc, install_photo_data, datetime.now().isoformat(), camera["id"]))
                        execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at,install_photo) VALUES(?,?,?,?,?,?,?,?,?)", (camera["id"], old_loc, new_loc, old_status, action, note, "Campo", datetime.now().isoformat(), install_photo_data))
                    else:
                        execute("UPDATE cameras SET status=?, current_location=?, updated_at=? WHERE id=?", (action, new_loc, datetime.now().isoformat(), camera["id"]))
                        execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (camera["id"], old_loc, new_loc, old_status, action, note, "Campo", datetime.now().isoformat()))
                    msg = f"Etapa registrada: {action_labels[action]}."
        camera = load_camera_by_code(code)

    if camera:
        workflow = ["Em transporte", "Na obra aguardando instalação", "Instalando", "Em operação", "Retirada"]
        action_labels = {
            "Em transporte": "🚚 Em transporte",
            "Na obra aguardando instalação": "📍 Na obra / aguardando instalação",
            "Instalando": "🛠 Instalando",
            "Em operação": "🟢 Ativar câmera",
            "Retirada": "↩️ Retirada",
        }
        hist_statuses = query("SELECT new_status FROM camera_history WHERE camera_id=?", (camera["id"],))
        max_done = -1
        # Regra v1.5.1: se a câmera estiver em estoque, o fluxo de campo reinicia.
        # Isso permite reutilizar uma câmera ou testar um QR sem herdar etapas antigas.
        if camera["status"] in ("Testada e aprovada",):
            max_done = -1
        elif camera["status"] == "Aguardando teste":
            max_done = -2
        elif camera["status"] in workflow:
            max_done = max(max_done, workflow.index(camera["status"]))
        else:
            # Para status fora do fluxo, preserva a etapa mais avançada já realizada.
            for hs in hist_statuses:
                if hs["new_status"] in workflow:
                    max_done = max(max_done, workflow.index(hs["new_status"]))
        next_step = None if max_done == -2 else (workflow[max_done + 1] if max_done + 1 < len(workflow) else None)
        # V2.0: retirada só aparece liberada após autorização feita no painel.
        if next_step == "Retirada" and camera["status"] != "Aguardando retirada":
            next_step = None
        if camera["status"] == "Aguardando retirada":
            max_done = workflow.index("Em operação")
            next_step = "Retirada"
        # V3.0.3: link de campo por obra bloqueia câmera livre ou vinculada a outra obra.
        if contract_ctx and (not camera["contract_id"] or camera["contract_id"] != contract_id):
            contract_blocked = True
            next_step = None
        active_occ_count = int(rv(camera, 'active_occ_count', 0) or 0)
        occurrence_blocked = active_occ_count > 0
        if occurrence_blocked:
            next_step = None
        btns = []
        for idx, step in enumerate(workflow):
            label = action_labels[step]
            if idx <= max_done:
                btns.append(f'<button type="button" class="done" disabled>✅ {label}</button>')
            elif step == next_step:
                confirm = ' onclick="return confirm(\'Confirmar retirada da obra? A câmera ficará Em retorno até ser recebida na central.\')"' if step == 'Retirada' else ''; btns.append(f'<button name="action" value="{step}" class="active-step"{confirm}>{label}</button>')
            else:
                btns.append(f'<button type="button" class="locked" disabled>🔒 {label}</button>')
        buttons_html = "".join(btns)
        history = query("SELECT * FROM camera_history WHERE camera_id=? ORDER BY created_at DESC LIMIT 12", (camera["id"],))

    occurrence_blocked = bool(camera and int(rv(camera, 'active_occ_count', 0) or 0))
    return render_template_string(r"""
<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>7Sense Campo</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;background:#f5f7fb;color:#0f172a;margin:0}.wrap{max-width:560px;margin:0 auto;padding:18px}.card{background:#fff;border:1px solid #dbe3ef;border-radius:18px;padding:18px;margin:12px 0}.hero{font-size:24px;font-weight:800}.tag{color:#64748b}.small{font-size:13px;color:#64748b}input,textarea{width:100%;border:1px solid #dbe3ef;border-radius:12px;padding:13px;font-size:18px;margin:6px 0 12px}textarea{min-height:80px}.btn,button{display:block;width:100%;border:0;border-radius:14px;padding:16px;margin:8px 0;background:#0f5fff;color:#fff;font-size:18px;font-weight:700}.btn.secondary{background:#fff;color:#0f172a;border:1px solid #dbe3ef}.danger{background:#dc2626!important}.done{background:#16a34a!important;color:#fff;opacity:.95}.active-step{background:#0f5fff!important;color:#fff}.locked{background:#e5e7eb!important;color:#64748b!important}.badge{display:inline-block;border-radius:999px;padding:6px 10px;background:#e5e7eb}.reader{border:2px dashed #dbe3ef;border-radius:18px;padding:12px}#reader{width:100%;min-height:220px}.timeline{border-left:3px solid #dbe3ef;margin-left:6px;padding-left:12px}.timeline-item{padding:8px 0;border-bottom:1px solid #eef2f7}
</style>
<script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script></head><body><div class="wrap"><div class="card"><div class="hero">7Sense Campo</div><div class="tag">Leitura de QR Code e status operacional da câmera.</div></div>
{% if msg %}<div class="card"><b>{{msg}}</b></div>{% endif %}
{% if contract_ctx %}<div class="card"><b>🏗️ Link de campo da obra</b><p><b>Cliente:</b> {{contract_ctx['client_name'] or '-'}}</p><p><b>Obra:</b> {{contract_ctx['obra'] or '-'}}</p><p class="tag">Este link registra movimentações somente das câmeras previamente vinculadas a esta obra.</p></div>{% endif %}
<div class="card"><button type="button" id="startQr">📷 Ler QR Code</button><div id="reader" class="reader" style="display:none"></div><form method="post" id="lookup"><label>Código da câmera<input id="code" name="code" placeholder="7S-CAM-001" value="{{request.form.get('code','') or request.args.get('code','')}}"></label><button>Buscar câmera</button></form><p class="tag">Se a câmera do celular não abrir, digite o código manualmente.</p></div>
{% if camera %}<div class="card"><h2>{{camera['code']}}</h2><p><span class="badge">Status atual: {{camera['status']}}</span></p><p><b>Cliente:</b> {{camera['client_name'] or '-'}}</p><p><b>Obra:</b> {{camera['obra'] or '-'}}</p><p><b>Local:</b> {{camera['current_location'] or '-'}}</p><p><b>Serviço:</b> {{camera['service'] or '-'}}</p></div>
<div class="card"><form method="post" enctype="multipart/form-data"><input type="hidden" name="code" value="{{camera['code']}}">{% if contract_blocked %}<p style="background:#fee2e2;border-radius:12px;padding:12px;color:#991b1b"><b>Operação bloqueada:</b> esta câmera não está autorizada para este link de obra.</p>{% endif %}{% if occurrence_blocked %}<p style="background:#fee2e2;border-radius:12px;padding:12px;color:#991b1b"><b>Operação bloqueada:</b> existe ocorrência aberta para esta câmera. A central precisa resolver a ocorrência antes de continuar o fluxo.</p>{% endif %}<label>Local atual / instalação<input name="local" placeholder="Poste 1, Retro 1, Portaria..." value="{{camera['current_location'] or ''}}"></label><label>Observação<textarea name="note" placeholder="Observação opcional"></textarea></label>{% if next_step == 'Em operação' %}<label>📸 Foto da instalação (obrigatória)<input type="file" name="install_photo" accept="image/*" capture="environment" required></label><p class="small">A foto é obrigatória para ativar a câmera e ficará anexada ao histórico.</p>{% endif %}<p class="tag"><b>Fluxo operacional</b></p><p class="small">Etapas concluídas ficam verdes e bloqueadas. Somente a próxima etapa fica liberada.</p>{% if camera['status'] == 'Aguardando teste' %}<p style="background:#fef3c7;border-radius:12px;padding:12px"><b>🧪 Aguardando teste:</b> esta câmera precisa ser testada e aprovada no painel antes de ser enviada para nova obra.</p>{% endif %}{{buttons_html|safe}}<hr style="border:0;border-top:1px solid #eef2f7;margin:18px 0"><label>Problema operacional<input name="problem" placeholder="Sem energia, sem sinal, dano físico..."></label><button name="action" value="PROBLEMA" class="danger">🔴 Registrar problema</button></form></div>
<div class="card"><h3>Histórico recente</h3><div class="timeline">{% for h in history %}<div class="timeline-item"><b>{{h['new_status']}}</b><br><span class="small">{{h['created_at'][:16].replace('T',' ')}} · {{h['user_name'] or 'Campo'}}</span><br><span class="small">{{h['note'] or ''}}</span></div>{% else %}<p class="tag">Sem histórico ainda.</p>{% endfor %}</div></div>{% endif %}
</div><script>let scanner=null;document.getElementById('startQr').addEventListener('click', async()=>{const r=document.getElementById('reader');r.style.display='block'; if(!window.Html5Qrcode){alert('Leitor QR não carregou. Digite o código manualmente.');return;} scanner=new Html5Qrcode('reader'); try{await scanner.start({facingMode:'environment'},{fps:10,qrbox:220}, txt=>{document.getElementById('code').value=txt.trim(); scanner.stop(); document.getElementById('lookup').submit();});}catch(e){alert('Não foi possível abrir a câmera. Verifique HTTPS/permissão ou digite o código manualmente.');}});</script>
</body></html>
""", camera=camera, msg=msg, buttons_html=buttons_html, history=history, next_step=next_step if camera else None, contract_ctx=contract_ctx, contract_blocked=contract_blocked, occurrence_blocked=occurrence_blocked)


def next_free_demo_camera_code(start=900):
    """Gera código demo sem conflitar com câmeras reais já cadastradas no banco permanente."""
    n = start
    while one("SELECT id FROM cameras WHERE code=?", (f"7S-DEMO-CAM-{n:03d}",)):
        n += 1
    return n



@app.route("/demo/load")
@operacao_required
def load_demo():
    flash("Modo demonstração foi removido na versão 2.0. Use somente a operação oficial.")
    return redirect(url_for("dashboard"))

@app.route("/demo/operation")
@operacao_required
def operation_mode():
    flash("O sistema já está em operação oficial.")
    return redirect(url_for("dashboard"))

@app.route("/demo/clear")
@operacao_required
def clear_demo():
    flash("Modo demonstração removido. Nenhum dado foi alterado.")
    return redirect(url_for("dashboard"))


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
