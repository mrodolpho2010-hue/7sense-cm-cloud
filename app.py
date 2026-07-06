import os
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, jsonify, render_template_string, send_file, g
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO
import qrcode
import base64
from PIL import Image, ImageDraw, ImageFont

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # Ambiente local sem PostgreSQL instalado
    psycopg = None

APP_VERSION = "2.0.3 Fluxo de retorno e foto obrigatória"
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
STATUS_CAMERA = ["Aguardando teste", "Testada e aprovada", "Disponível", "Em estoque", "Reservada", "Em transporte", "Chegou na obra", "Instalando", "Em operação", "Aguardando retirada", "Em retorno", "Offline", "Em manutenção", "Aposentada", "Descartada"]
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
    if s in ("Em transporte", "Aguardando retirada", "Reservada", "Em retorno"):
        return "info"
    if s in ("Aguardando teste", "Em estoque", "Disponível", "Aposentada", "Descartada"):
        return "muted"
    return "ok"


def rv(row, key, default=""):
    """Lê um campo de sqlite.Row sem quebrar quando a consulta não trouxe a coluna."""
    try:
        v = row[key]
        return v if v is not None else default
    except Exception:
        return default


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
<title>7Sense CM</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--text:#0f172a;--muted:#64748b;--border:#dbe3ef;--accent:#0f5fff;--ok:#0f9f6e;--danger:#dc2626;--warn:#d97706;--info:#2563eb;--softdanger:#fee2e2;--softwarn:#fef3c7;--softok:#dcfce7}
*{box-sizing:border-box} body{margin:0;font-family:Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text)} a{text-decoration:none;color:inherit} .wrap{max-width:1200px;margin:0 auto;padding:18px}.top{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:14px}.brand{font-weight:800;font-size:20px}.tag{color:var(--muted);font-size:13px}.nav{display:flex;gap:8px;flex-wrap:wrap}.btn,.nav a,button{border:1px solid var(--border);background:#fff;border-radius:12px;padding:10px 14px;font-size:15px;cursor:pointer;color:var(--text)}.btn.primary,button.primary{background:var(--accent);color:white;border-color:var(--accent)}.btn.danger{background:var(--danger);color:white}.btn.small{padding:6px 10px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(5,minmax(140px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:18px}.metric{display:block}.metric h3{margin:0 0 8px;font-size:18px}.metric b{font-size:36px}.metric.danger{border-color:#fecaca;background:#fff7f7}.metric:hover{box-shadow:0 6px 20px rgba(15,23,42,.08)}.search{display:flex;gap:8px;flex:1;max-width:420px}.search input,input,select,textarea{width:100%;border:1px solid var(--border);border-radius:12px;padding:11px;font-size:15px;background:#fff}textarea{min-height:90px}.panel{background:#fff;border:1px solid var(--border);border-radius:18px;padding:16px;margin-top:12px}.row{display:grid;grid-template-columns:1.2fr 1.2fr 1fr .8fr auto;gap:8px;align-items:center;border-bottom:1px solid #eef2f7;padding:11px 4px}.row:last-child{border-bottom:none}.row.camera{grid-template-columns:.9fr 1fr 1fr 1fr 1fr 1.4fr}.row.danger{background:#fff1f2;color:#991b1b;border-radius:12px;padding-left:10px}.badge{display:inline-block;padding:5px 9px;border-radius:999px;font-size:13px;border:1px solid var(--border);background:#f8fafc}.badge.ok{background:var(--softok);color:#166534}.badge.danger{background:var(--softdanger);color:#991b1b}.badge.warn{background:var(--softwarn);color:#92400e}.badge.info{background:#dbeafe;color:#1e40af}.badge.muted{background:#e5e7eb;color:#374151}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.filters a{border:1px solid var(--border);background:#fff;padding:8px 12px;border-radius:999px}.filters a.active{background:var(--accent);color:#fff}.flash{background:#fef3c7;border:1px solid #fde68a;border-radius:12px;padding:10px;margin:10px 0}.breadcrumb{font-size:14px;color:var(--muted);margin:8px 0 14px}.actions{display:flex;gap:8px;flex-wrap:wrap}.formgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.full{grid-column:1/-1}.mobile-card{max-width:560px;margin:0 auto}.hero{font-size:22px;font-weight:800}.hidden{display:none!important}.settings{position:relative}.settings-menu{display:none;position:absolute;right:0;top:46px;background:#fff;border:1px solid var(--border);border-radius:14px;box-shadow:0 12px 30px rgba(15,23,42,.15);min-width:230px;z-index:20;padding:8px}.settings:hover .settings-menu{display:block}.settings-menu a{display:block;border:0;border-radius:10px;padding:10px 12px;background:#fff}.settings-menu a:hover{background:#f1f5f9}@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.row,.row.camera{display:block}.row>*{margin:5px 0}.formgrid{grid-template-columns:1fr}.top{display:block}.nav{margin-top:10px}.search{max-width:none;margin-top:10px}}
</style>
</head><body><div class="wrap">
<div class="top"><div><div class="brand">7Sense – Data into Action</div><div class="tag">Operations Manager {{version}}</div></div>
<div class="nav"><a href="{{url_for('dashboard')}}">🏠 Dashboard</a><a href="{{url_for('clients')}}">Clientes</a><a href="{{url_for('contracts')}}">Contratos</a><a href="{{url_for('cameras')}}">Câmeras</a><a href="{{url_for('occurrences')}}">Ocorrências</a><a href="{{url_for('agenda_page')}}">Agenda</a><a href="{{url_for('campo')}}">📱 Campo</a>{% if user %}<div class="settings"><a href="{{url_for('settings_page')}}">⚙️ Configurações</a><div class="settings-menu"><a href="{{url_for('profile_page')}}">Meu perfil</a><a href="{{url_for('change_password')}}">Alterar senha</a><a href="{{url_for('users_page')}}">Gerenciar usuários</a><a href="{{url_for('clear_database')}}">Limpar banco de dados</a><a href="{{url_for('about_page')}}">Sobre o sistema</a><a href="{{url_for('logout')}}">Sair</a></div></div>{% endif %}</div></div>
{% if user %}<div class="breadcrumb">{{breadcrumb or 'Dashboard'}} · Usuário: {{user['name']}} · Perfil: {{user['role']}}</div>{% endif %}
{% with messages = get_flashed_messages() %}{% if messages %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endif %}{% endwith %}
{{body|safe}}
</div></body></html>
"""


LOGIN_BASE = r"""
<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Entrar no 7Sense</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--text:#0f172a;--muted:#64748b;--border:#dbe3ef;--accent:#0f5fff}
*{box-sizing:border-box} body{margin:0;font-family:Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}.card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:28px;width:100%;max-width:520px;box-shadow:0 12px 35px rgba(15,23,42,.08)}.brand{font-weight:900;font-size:28px;margin:0 0 6px}.tag{color:var(--muted);font-size:14px;margin:0 0 22px}label{display:block;margin:14px 0 6px;font-weight:600}input{width:100%;border:1px solid var(--border);border-radius:12px;padding:13px;font-size:16px;background:#fff}.actions{display:flex;gap:12px;align-items:center;margin-top:18px;flex-wrap:wrap}button{border:1px solid var(--accent);background:var(--accent);color:white;border-radius:12px;padding:12px 18px;font-size:16px;cursor:pointer}.link{color:var(--accent);font-size:14px}.flash{background:#fef3c7;border:1px solid #fde68a;border-radius:12px;padding:10px;margin-bottom:14px}.footer{font-size:12px;color:var(--muted);margin-top:22px;text-align:center}
</style></head><body>
<div class="card">
  <div class="brand">7Sense</div>
  <p class="tag">Data into Action · Contract Manager</p>
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
    contracts_active = count("SELECT COUNT(*) FROM contracts WHERE status!='Encerrado' AND demo=?", (df,))
    cams_operation = count("SELECT COUNT(*) FROM cameras WHERE status='Em operação' AND demo=?", (df,))
    cams_available = count("SELECT COUNT(*) FROM cameras WHERE status IN ('Disponível','Em estoque','Testada e aprovada') AND demo=?", (df,))
    occ_open = count("SELECT COUNT(*) FROM occurrences WHERE status IN ('Aberta','Em andamento') AND archived_at IS NULL AND demo=?", (df,))
    today = date.today().isoformat()
    agenda_today = count("SELECT COUNT(*) FROM agenda WHERE event_date=? AND demo=?", (today, df))
    body = f"""
    <div class="grid">
      <a class="card metric" href="{url_for('contracts')}"><h3>Contratos ativos</h3><b>{contracts_active}</b></a>
      <a class="card metric" href="{url_for('cameras', status='Em operação')}"><h3>Câmeras em operação</h3><b>{cams_operation}</b></a>
      <a class="card metric" href="{url_for('cameras', status='Disponíveis')}"><h3>Câmeras disponíveis</h3><b>{cams_available}</b></a>
      <a class="card metric {'danger' if occ_open else ''}" href="{url_for('occurrences', status='abertas')}"><h3>Ocorrências em aberto</h3><b>{occ_open}</b></a>
      <a class="card metric" href="{url_for('agenda_page', filtro='hoje')}"><h3>Agenda do dia</h3><b>{agenda_today}</b></a>
    </div>
    <div class="panel"><h2>Pesquisa global</h2><form action="{url_for('search')}" class="search"><input name="q" placeholder="Toyota, CAM-007, Sorocaba..."><button>Pesquisar</button></form></div>
    <div class="panel"><h2>Gestão operacional</h2><p class="tag">Este painel controla patrimônio, contratos, obras, movimentações e ocorrências. Status técnicos em tempo real, como bateria ou sinal, pertencem à plataforma de monitoramento/IA.</p></div>
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
    if status == "Todos":
        q = "WHERE c.demo=?"
        params = (current_demo_flag(),)
    else:
        q = "WHERE c.demo=? AND c.status=?"
        params = (current_demo_flag(), status)
    rows = query(f"SELECT c.*, cl.name client_name, (SELECT COUNT(*) FROM cameras ca WHERE ca.contract_id=c.id AND ca.demo=c.demo) cam_count FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id {q} ORDER BY c.created_at DESC", params)
    can_edit = current_user()["role"] == "operacao"
    filters = "".join([f"<a class='{ 'active' if status==s else ''}' href='{url_for('contracts', status=s)}'>{s}</a>" for s in ["Todos"]+STATUS_CONTRATO])
    items = "".join([f"<div class='row'><b>{r['client_name'] or 'Sem cliente'}</b><span>{r['city'] or ''}/{r['state'] or ''}</span><span>{r['cam_count']} câmeras</span><span><span class='badge {status_class(r['status'])}'>{r['status']}</span></span><span class='actions'><a class='btn small' href='{url_for('contract_view', id=r['id'])}'>Ver</a>{('<a class=\"btn small\" href=\"'+url_for('contract_edit', id=r['id'])+'\">Editar</a>') if can_edit else ''}</span></div>" for r in rows])
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>Contratos</h2>{'<a class=\"btn primary\" href=\"'+url_for('contract_new')+'\">Novo contrato</a>' if can_edit else ''}</div><div class='filters'>{filters}</div>{items or '<p>Nenhum contrato.</p>'}</div>"
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
    cams = query("SELECT ca.*, co.obra, cl.name client_name FROM cameras ca LEFT JOIN contracts co ON co.id=ca.contract_id LEFT JOIN clients cl ON cl.id=co.client_id WHERE ca.contract_id=? ORDER BY ca.code", (id,))
    items = "".join([camera_row(c, current_user()["role"] if current_user() else None) for c in cams])
    body = f"""<div class="panel"><h2>{r['client_name']} – {r['obra']}</h2><p><span class="badge {status_class(r['status'])}">{r['status']}</span> · {r['city']}/{r['state']} · Previstas: {r['expected_cameras']}</p>
    <div class="actions"><a class="btn" href="{url_for('contracts')}">Voltar</a>{'<a class=\"btn primary\" href=\"'+url_for('camera_new', contract_id=r['id'])+'\">Adicionar câmera</a>' if current_user()['role']=='operacao' else ''}</div></div>
    <div class="panel"><h2>Câmeras do contrato</h2>{items or '<p>Nenhuma câmera.</p>'}</div>"""
    return page(body, breadcrumb=f"Dashboard > Contratos > {r['client_name']} {r['obra']}")


def camera_row(c, user_role=None):
    # Compatível com SQLite Row, psycopg dict_row e bancos antigos sem algumas colunas.
    cam_id = rv(c, 'id')
    code = rv(c, 'code', '-') or '-'
    status = rv(c, 'status', 'Sem status') or 'Sem status'
    cls = status_class(status)
    aprovado = " 🧪" if (rv(c, 'tested_approved_at') or status == "Testada e aprovada") else ""
    qr_btn = f"<a class='btn small' href='{url_for('camera_qr', id=cam_id)}'>📷 QR</a>"
    test_btn = f"<a class='btn small' href='{url_for('camera_approve', id=cam_id)}'>🧪 Teste</a>" if (user_role or (current_user()['role'] if current_user() else None))=='operacao' else ""
    edit_btns = (f"<a class='btn small' href='{url_for('camera_edit', id=cam_id)}'>Editar</a> <a class='btn small' href='{url_for('camera_transfer', id=cam_id)}'>Transferir</a>") if (user_role or (current_user()['role'] if current_user() else None))=='operacao' else ""
    cliente = rv(c, 'client_name', '-') or '-'
    obra = rv(c, 'obra', '-') or '-'
    local = rv(c, 'current_location', '-') or '-'
    servico = rv(c, 'service', '-') or '-'
    return f"<div class='row camera { 'danger' if cls=='danger' else ''}'><b>{code}{aprovado}</b><span><b>{cliente}</b><br><small>{obra}</small></span><span>{local}</span><span>{servico}</span><span><span class='badge {cls}'>{status}</span></span><span class='actions'>{qr_btn}{test_btn}<a class='btn small' href='{url_for('camera_view', id=cam_id)}'>Ver</a>{edit_btns}</span></div>"


@app.route("/cameras")
@login_required
def cameras():
    status = request.args.get("status", "Todas")
    params = (current_demo_flag(),)
    where = "WHERE ca.demo=?"
    if status == "Disponíveis":
        where += " AND ca.status IN ('Disponível','Em estoque','Testada e aprovada')"
    elif status != "Todas":
        where += " AND ca.status=?"; params = (current_demo_flag(), status)
    rows = query(f"""SELECT ca.*, co.obra, co.city contract_city, co.state contract_state, cl.name client_name
                    FROM cameras ca
                    LEFT JOIN contracts co ON co.id=ca.contract_id
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    {where}
                    ORDER BY cl.name, co.obra, ca.code""", params)
    filters = ["Todas", "Em operação", "Disponíveis", "Em estoque", "Em transporte", "Aguardando retirada", "Em retorno", "Offline", "Em manutenção", "Testada e aprovada", "Aguardando teste"]
    def filter_count(label):
        if label == "Todas":
            return count("SELECT COUNT(*) FROM cameras WHERE demo=?", (current_demo_flag(),))
        if label == "Disponíveis":
            return count("SELECT COUNT(*) FROM cameras WHERE status IN ('Disponível','Em estoque','Testada e aprovada') AND demo=?", (current_demo_flag(),))
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
    <p class='tag'>Visualização por <b>cliente</b>, depois <b>obra</b>, depois <b>câmera</b>. Assim fica claro antes de qual cliente e obra estamos falando.</p>
    <div class='filters'>{fhtml}</div>{items or '<p>Nenhuma câmera.</p>'}</div>"""
    return page(body, breadcrumb="Dashboard > Câmeras")


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
        vals = (request.form.get("model"), request.form.get("serial"), request.form.get("contract_id") or None, request.form.get("current_location"), request.form.get("service"), request.form.get("status"), request.form.get("notes"), datetime.now().isoformat())
        if r:
            execute("UPDATE cameras SET model=?,serial=?,contract_id=?,current_location=?,service=?,status=?,notes=?,updated_at=? WHERE id=?", vals+(r["id"],))
            flash("Câmera atualizada.")
            return redirect(url_for("camera_view", id=r["id"]))
        code = request.form.get("code") or next_camera_code()
        execute("INSERT INTO cameras(code,model,serial,contract_id,current_location,service,status,notes,demo,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (code,)+vals[:7]+(current_demo_flag(),)+vals[7:]+(datetime.now().isoformat(),))
        flash("Câmera criada.")
        return redirect(url_for("cameras"))
    def val(k): return (r[k] if r else "") or ""
    client_opts = "<option value=''>Sem cliente / estoque</option>" + "".join([f"<option value='{cl['id']}' {'selected' if selected_client_id==str(cl['id']) else ''}>{cl['name']}</option>" for cl in clients_rows])
    contract_opts = "<option value='' data-client=''>Sem contrato / estoque</option>" + "".join([f"<option value='{c['id']}' data-client='{c['client_id'] or ''}' {'selected' if selected_contract_id and str(c['id'])==str(selected_contract_id) else ''}>{c['client_name'] or 'Sem cliente'} - {c['obra']}</option>" for c in contracts_rows])
    service_opts = "".join([f"<option {'selected' if val('service')==s else ''}>{s}</option>" for s in SERVICOS])
    status_opts = "".join([f"<option {'selected' if val('status')==s else ''}>{s}</option>" for s in STATUS_CAMERA])
    code_field = f"<label>Código<input name='code' value='{next_camera_code()}'></label>" if not r else f"<label>Código<input value='{val('code')}' disabled></label>"
    body = f"""<div class="panel"><h2>{'Editar' if r else 'Nova'} Câmera</h2>
    <p class="tag">Escolha primeiro o <b>Cliente</b> e depois a <b>Obra/Contrato</b>. Isso evita confusão quando o mesmo cliente tiver várias obras.</p>
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
    occ_html = "".join([f"<div class='row'><b>{o['title']}</b><span>{o['problem']}</span><span>{o['status']}</span><span>{o['created_at'][:16]}</span><span></span></div>" for o in occs])
    occ_hist_html = "".join([f"<div class='row'><b>{o['title']}</b><span>{o['problem']}</span><span>Arquivada</span><span>{(o['archived_at'] or o['created_at'])[:16]}</span><span>{o['archived_location'] or '-'}</span></div>" for o in occs_hist])
    body = f"""<div class="panel"><h2>{c['code']}</h2><p><span class="badge {status_class(c['status'])}">{c['status']}</span></p><p>Cliente/Obra: {c['client_name'] or '-'} / {c['obra'] or '-'}</p><p>Local: {c['current_location'] or '-'}</p><p>Serviço: {c['service'] or '-'}</p>{f"<p><b>Última foto da instalação:</b><br><img src='{rv(c, 'last_install_photo', '')}' alt='Foto da instalação' style='max-width:320px;border-radius:14px;border:1px solid #dbe3ef;margin-top:8px'></p>" if rv(c, 'last_install_photo', '') else ''}<div class="actions"><a class="btn" href="{url_for('cameras')}">Voltar</a><a class="btn" href="{url_for('camera_qr', id=c['id'])}">📷 Gerar QR</a>{('<a class=\"btn primary\" href=\"'+url_for('camera_transfer', id=c['id'])+'\">Transferir</a><a class=\"btn\" href=\"'+url_for('occurrence_new', camera_id=c['id'])+'\">Abrir ocorrência</a>' + ('<a class=\"btn\" href=\"'+url_for('camera_receive_central', id=c['id'])+'\">🏢 Recebida na central</a>' if c['status']=='Em retorno' else '<a class=\"btn\" href=\"'+url_for('camera_authorize_removal', id=c['id'])+'\">🔓 Autorizar retirada</a>')) if current_user()['role']=='operacao' else ''}</div></div>
    <div class="panel"><h2>Histórico</h2>{hist_html or '<p>Sem histórico.</p>'}</div>
    <div class="panel"><h2>Ocorrências do ciclo atual</h2>{occ_html or '<p>Sem ocorrências ativas neste ciclo.</p>'}</div>
    <div class="panel"><h2>Ocorrências arquivadas de ciclos anteriores</h2>{occ_hist_html or '<p>Sem ocorrências arquivadas.</p>'}</div>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > {c['code']}")



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
        execute("UPDATE cameras SET status=?, contract_id=NULL, current_location='', service='', removal_authorized_at=NULL, removal_authorized_by=NULL, patrimonial_status=?, updated_at=? WHERE id=?", ("Aguardando teste", "Em preparação", now, id))
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
    checklist_items = ["Carregada", "Cartão SD verificado", "Limpeza realizada", "Teste de imagem OK", "Teste de comunicação OK", "Estado físico OK"]
    if request.method == "POST":
        checked = [i for i in checklist_items if request.form.get(i)]
        note = request.form.get("note", "")
        checklist = "; ".join(checked) + ((" | " + note) if note else "")
        old_status = c["status"]
        execute("UPDATE cameras SET status=?, patrimonial_status=?, tested_approved_at=?, tested_checklist=?, updated_at=? WHERE id=?", ("Testada e aprovada", "Disponível", datetime.now().isoformat(), checklist, datetime.now().isoformat(), id))
        execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (id, c["current_location"], c["current_location"], old_status, "Testada e aprovada", "Checklist: " + checklist, current_user()["name"], datetime.now().isoformat()))
        flash("Câmera testada e aprovada para novo envio.")
        return redirect(url_for("camera_view", id=id))
    boxes = "".join([f"<label><input type='checkbox' name='{item}' value='1'> {item}</label><br>" for item in checklist_items])
    body = f"""<div class='panel'><h2>🧪 Testar e Aprovar {c['code']}</h2><p>Use este checklist quando a câmera retornar de obra ou antes de liberar para novo cliente.</p><form method='post' class='formgrid'><div class='full card'>{boxes}</div><label class='full'>Observações<textarea name='note' placeholder='Ex.: bateria ok, lente limpa, cartão substituído...'></textarea></label><div class='full'><button class='primary'>Aprovar câmera</button></div></form></div>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > Teste {c['code']}")


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
        execute("UPDATE cameras SET contract_id=?,current_location=?,service=?,status=?,updated_at=? WHERE id=?", (new_contract,new_loc,new_service,new_status,datetime.now().isoformat(),id))
        execute("INSERT INTO camera_history(camera_id,old_contract_id,new_contract_id,old_location,new_location,old_service,new_service,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (id, old['contract_id'], new_contract, old['current_location'], new_loc, old['service'], new_service, old['status'], new_status, note, current_user()['name'], datetime.now().isoformat()))
        flash("Câmera transferida/atualizada.")
        return redirect(url_for("camera_view", id=id))
    client_opts = "<option value=''>Sem cliente / estoque</option>" + "".join([f"<option value='{cl['id']}' {'selected' if selected_client_id==str(cl['id']) else ''}>{cl['name']}</option>" for cl in clients_rows])
    opts = "<option value='' data-client=''>Sem contrato / estoque</option>" + "".join([f"<option value='{r['id']}' data-client='{r['client_id'] or ''}' {'selected' if c['contract_id']==r['id'] else ''}>{r['client_name'] or 'Sem cliente'} - {r['obra']}</option>" for r in contracts_rows])
    services = "".join([f"<option {'selected' if c['service']==s else ''}>{s}</option>" for s in SERVICOS])
    statuses = "".join([f"<option {'selected' if c['status']==s else ''}>{s}</option>" for s in STATUS_CAMERA])
    body = f"""<div class="panel"><h2>Transferir / Atualizar {c['code']}</h2><p class="tag">Selecione o cliente para filtrar somente as obras/contratos desse cliente.</p><form method="post" class="formgrid">
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
    rows = query(f"SELECT o.*, ca.code camera_code FROM occurrences o LEFT JOIN cameras ca ON ca.id=o.camera_id {where} ORDER BY o.created_at DESC", (current_demo_flag(),))
    can_edit = current_user()["role"] == "operacao"
    items = "".join([f"<div class='row'><b>{r['camera_code'] or '-'}</b><span>{r['title']}</span><span>{r['status']}</span><span>{r['created_at'][:16]}</span><span>{'<a class=\"btn small\" href=\"'+url_for('occurrence_close', id=r['id'])+'\">Resolver</a>' if can_edit and r['status']!='Resolvida' else ''}</span></div>" for r in rows])
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


@app.route("/occurrences/<int:id>/close")
@operacao_required
def occurrence_close(id):
    execute("UPDATE occurrences SET status='Resolvida', closed_at=? WHERE id=?", (datetime.now().isoformat(), id))
    flash("Ocorrência resolvida.")
    return redirect(url_for("occurrences"))


@app.route("/agenda")
@login_required
def agenda_page():
    filtro = request.args.get("filtro")
    if filtro == "hoje":
        rows = query("SELECT * FROM agenda WHERE event_date=? AND demo=? ORDER BY event_time", (date.today().isoformat(), current_demo_flag()))
    else:
        rows = query("SELECT * FROM agenda WHERE demo=? ORDER BY event_date,event_time LIMIT 100", (current_demo_flag(),))
    can_edit = current_user()["role"] == "operacao"
    items = "".join([f"<div class='row'><b>{r['event_date']} {r['event_time']}</b><span>{r['title']}</span><span>{r['notes'] or ''}</span><span></span><span></span></div>" for r in rows])
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>Agenda</h2>{'<a class=\"btn primary\" href=\"'+url_for('agenda_new')+'\">Novo evento</a>' if can_edit else ''}</div>{items or '<p>Nenhum evento.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Agenda")


@app.route("/agenda/new", methods=["GET", "POST"])
@operacao_required
def agenda_new():
    if request.method == "POST":
        execute("INSERT INTO agenda(title,event_date,event_time,notes,demo,created_at) VALUES(?,?,?,?,?,?)", (request.form.get("title"), request.form.get("event_date"), request.form.get("event_time"), request.form.get("notes"), current_demo_flag(), datetime.now().isoformat()))
        flash("Evento criado.")
        return redirect(url_for("agenda_page"))
    body = """<div class="panel"><h2>Novo evento</h2><form method="post" class="formgrid"><label>Título<input name="title"></label><label>Data<input type="date" name="event_date"></label><label>Hora<input type="time" name="event_time"></label><label class="full">Observações<textarea name="notes"></textarea></label><div class="full"><button class="primary">Salvar</button></div></form></div>"""
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


@app.route("/campo", methods=["GET", "POST"])
def campo():
    msg = ""
    camera = None
    history = []
    buttons_html = ""
    code = request.form.get("code", "").strip().upper() if request.method == "POST" else request.args.get("code", "").strip().upper()

    def load_camera_by_code(camera_code):
        return one("""SELECT ca.*, co.obra, cl.name client_name
                    FROM cameras ca
                    LEFT JOIN contracts co ON co.id=ca.contract_id
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    WHERE UPPER(ca.code)=?""", (camera_code,))

    if code:
        camera = load_camera_by_code(code)
        if not camera:
            msg = "Câmera não encontrada. Confira o código."

    if request.method == "POST" and camera:
        action = request.form.get("action")
        workflow = ["Em transporte", "Chegou na obra", "Instalando", "Em operação", "Retirada"]
        action_labels = {
            "Em transporte": "🚚 Em transporte",
            "Chegou na obra": "📍 Chegou na obra",
            "Instalando": "🛠 Instalando",
            "Em operação": "🟢 Ativar câmera",
            "Retirada": "↩️ Retirada",
        }

        # Descobre a maior etapa já registrada no histórico ou no status atual.
        hist_statuses = query("SELECT new_status FROM camera_history WHERE camera_id=?", (camera["id"],))
        max_done = -1
        # Regra v1.5.1: se a câmera estiver em estoque, o fluxo de campo reinicia.
        # Isso permite reutilizar uma câmera ou testar um QR sem herdar etapas antigas.
        if camera["status"] in ("Em estoque", "Testada e aprovada"):
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

        if action == "PROBLEMA":
            problem = request.form.get("problem") or "Problema operacional"
            note = request.form.get("note") or ""
            execute("INSERT INTO occurrences(camera_id,title,problem,status,responsible,notes,created_at) VALUES(?,?,?,?,?,?,?)", (camera["id"], "Problema registrado em campo", problem, "Aberta", "Campo", note, datetime.now().isoformat()))
            execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (camera["id"], camera["current_location"], camera["current_location"], camera["status"], "Problema registrado", problem + (" - " + note if note else ""), "Campo", datetime.now().isoformat()))
            msg = "Problema registrado. A ocorrência foi aberta no painel."
        elif action in workflow:
            if action != next_step:
                msg = "Esta etapa já foi realizada ou está fora de ordem. Leia o QR Code novamente e siga a próxima etapa liberada."
            else:
                old_status, old_loc = camera["status"], camera["current_location"]
                new_loc = request.form.get("local") or camera["current_location"]
                note = request.form.get("note") or ""
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
        workflow = ["Em transporte", "Chegou na obra", "Instalando", "Em operação", "Retirada"]
        action_labels = {
            "Em transporte": "🚚 Em transporte",
            "Chegou na obra": "📍 Chegou na obra",
            "Instalando": "🛠 Instalando",
            "Em operação": "🟢 Ativar câmera",
            "Retirada": "↩️ Retirada",
        }
        hist_statuses = query("SELECT new_status FROM camera_history WHERE camera_id=?", (camera["id"],))
        max_done = -1
        # Regra v1.5.1: se a câmera estiver em estoque, o fluxo de campo reinicia.
        # Isso permite reutilizar uma câmera ou testar um QR sem herdar etapas antigas.
        if camera["status"] in ("Em estoque", "Testada e aprovada"):
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

    return render_template_string(r"""
<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>7Sense Campo</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;background:#f5f7fb;color:#0f172a;margin:0}.wrap{max-width:560px;margin:0 auto;padding:18px}.card{background:#fff;border:1px solid #dbe3ef;border-radius:18px;padding:18px;margin:12px 0}.hero{font-size:24px;font-weight:800}.tag{color:#64748b}.small{font-size:13px;color:#64748b}input,textarea{width:100%;border:1px solid #dbe3ef;border-radius:12px;padding:13px;font-size:18px;margin:6px 0 12px}textarea{min-height:80px}.btn,button{display:block;width:100%;border:0;border-radius:14px;padding:16px;margin:8px 0;background:#0f5fff;color:#fff;font-size:18px;font-weight:700}.btn.secondary{background:#fff;color:#0f172a;border:1px solid #dbe3ef}.danger{background:#dc2626!important}.done{background:#16a34a!important;color:#fff;opacity:.95}.active-step{background:#0f5fff!important;color:#fff}.locked{background:#e5e7eb!important;color:#64748b!important}.badge{display:inline-block;border-radius:999px;padding:6px 10px;background:#e5e7eb}.reader{border:2px dashed #dbe3ef;border-radius:18px;padding:12px}#reader{width:100%;min-height:220px}.timeline{border-left:3px solid #dbe3ef;margin-left:6px;padding-left:12px}.timeline-item{padding:8px 0;border-bottom:1px solid #eef2f7}
</style>
<script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script></head><body><div class="wrap"><div class="card"><div class="hero">7Sense Campo</div><div class="tag">Leitura de QR Code e status operacional da câmera.</div></div>
{% if msg %}<div class="card"><b>{{msg}}</b></div>{% endif %}
<div class="card"><button type="button" id="startQr">📷 Ler QR Code</button><div id="reader" class="reader" style="display:none"></div><form method="post" id="lookup"><label>Código da câmera<input id="code" name="code" placeholder="7S-CAM-001" value="{{request.form.get('code','') or request.args.get('code','')}}"></label><button>Buscar câmera</button></form><p class="tag">Se a câmera do celular não abrir, digite o código manualmente.</p></div>
{% if camera %}<div class="card"><h2>{{camera['code']}}</h2><p><span class="badge">Status atual: {{camera['status']}}</span></p><p><b>Cliente:</b> {{camera['client_name'] or '-'}}</p><p><b>Obra:</b> {{camera['obra'] or '-'}}</p><p><b>Local:</b> {{camera['current_location'] or '-'}}</p><p><b>Serviço:</b> {{camera['service'] or '-'}}</p></div>
<div class="card"><form method="post" enctype="multipart/form-data"><input type="hidden" name="code" value="{{camera['code']}}"><label>Local atual / instalação<input name="local" placeholder="Poste 1, Retro 1, Portaria..." value="{{camera['current_location'] or ''}}"></label><label>Observação<textarea name="note" placeholder="Observação opcional"></textarea></label>{% if next_step == 'Em operação' %}<label>📸 Foto da instalação (obrigatória)<input type="file" name="install_photo" accept="image/*" capture="environment" required></label><p class="small">A foto é obrigatória para ativar a câmera e ficará anexada ao histórico.</p>{% endif %}<p class="tag"><b>Fluxo operacional</b></p><p class="small">Etapas concluídas ficam verdes e bloqueadas. Somente a próxima etapa fica liberada.</p>{% if camera['status'] == 'Aguardando teste' %}<p style="background:#fef3c7;border-radius:12px;padding:12px"><b>🧪 Aguardando teste:</b> esta câmera precisa ser testada e aprovada no painel antes de ser enviada para nova obra.</p>{% endif %}{{buttons_html|safe}}<hr style="border:0;border-top:1px solid #eef2f7;margin:18px 0"><label>Problema operacional<input name="problem" placeholder="Sem energia, sem sinal, dano físico..."></label><button name="action" value="PROBLEMA" class="danger">🔴 Registrar problema</button></form></div>
<div class="card"><h3>Histórico recente</h3><div class="timeline">{% for h in history %}<div class="timeline-item"><b>{{h['new_status']}}</b><br><span class="small">{{h['created_at'][:16].replace('T',' ')}} · {{h['user_name'] or 'Campo'}}</span><br><span class="small">{{h['note'] or ''}}</span></div>{% else %}<p class="tag">Sem histórico ainda.</p>{% endfor %}</div></div>{% endif %}
</div><script>let scanner=null;document.getElementById('startQr').addEventListener('click', async()=>{const r=document.getElementById('reader');r.style.display='block'; if(!window.Html5Qrcode){alert('Leitor QR não carregou. Digite o código manualmente.');return;} scanner=new Html5Qrcode('reader'); try{await scanner.start({facingMode:'environment'},{fps:10,qrbox:220}, txt=>{document.getElementById('code').value=txt.trim(); scanner.stop(); document.getElementById('lookup').submit();});}catch(e){alert('Não foi possível abrir a câmera. Verifique HTTPS/permissão ou digite o código manualmente.');}});</script>
</body></html>
""", camera=camera, msg=msg, buttons_html=buttons_html, history=history, next_step=next_step if camera else None)


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
