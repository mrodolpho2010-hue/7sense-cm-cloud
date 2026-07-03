import os
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, jsonify, render_template_string, send_file
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

APP_VERSION = "1.9.3 Correção PostgreSQL + visão hierárquica"
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
STATUS_CAMERA = ["Aguardando teste", "Testada e aprovada", "Em estoque", "Em transporte", "Chegou na obra", "Instalando", "Em operação", "Offline", "Em manutenção", "Retirada", "Aposentada"]
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
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
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
    execute("""CREATE TABLE IF NOT EXISTS camera_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER, old_contract_id INTEGER, new_contract_id INTEGER, old_location TEXT, new_location TEXT, old_service TEXT, new_service TEXT, old_status TEXT, new_status TEXT, note TEXT, user_name TEXT, created_at TEXT, install_photo TEXT
    )""")
    try:
        execute("ALTER TABLE camera_history ADD COLUMN install_photo TEXT")
    except Exception:
        pass
    execute("""CREATE TABLE IF NOT EXISTS occurrences(
        id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER, title TEXT, problem TEXT, status TEXT, responsible TEXT, notes TEXT, demo INTEGER DEFAULT 0, created_at TEXT, closed_at TEXT
    )""")
    execute("""CREATE TABLE IF NOT EXISTS agenda(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, event_date TEXT, event_time TEXT, contract_id INTEGER, notes TEXT, demo INTEGER DEFAULT 0, created_at TEXT
    )""")
    if not one("SELECT id FROM users WHERE email=?", ("marcos@7sense.local",)):
        execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)", ("Marcos", "marcos@7sense.local", generate_password_hash("123456"), "operacao"))
    if not one("SELECT id FROM users WHERE email=?", ("diretoria@7sense.local",)):
        execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)", ("Diretoria", "diretoria@7sense.local", generate_password_hash("123456"), "diretoria"))


def current_user():
    if "user_id" not in session:
        return None
    return one("SELECT * FROM users WHERE id=?", (session["user_id"],))


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
    if s in ("Em transporte",):
        return "info"
    if s in ("Aguardando teste", "Em estoque", "Retirada", "Aposentada"):
        return "muted"
    return "ok"


def rv(row, key, default=""):
    """Lê um campo de sqlite.Row sem quebrar quando a consulta não trouxe a coluna."""
    try:
        v = row[key]
        return v if v is not None else default
    except Exception:
        return default


BASE = r"""
<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>7Sense CM</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--text:#0f172a;--muted:#64748b;--border:#dbe3ef;--accent:#0f5fff;--ok:#0f9f6e;--danger:#dc2626;--warn:#d97706;--info:#2563eb;--softdanger:#fee2e2;--softwarn:#fef3c7;--softok:#dcfce7}
*{box-sizing:border-box} body{margin:0;font-family:Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text)} a{text-decoration:none;color:inherit} .wrap{max-width:1200px;margin:0 auto;padding:18px}.top{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:14px}.brand{font-weight:800;font-size:20px}.tag{color:var(--muted);font-size:13px}.nav{display:flex;gap:8px;flex-wrap:wrap}.btn,.nav a,button{border:1px solid var(--border);background:#fff;border-radius:12px;padding:10px 14px;font-size:15px;cursor:pointer;color:var(--text)}.btn.primary,button.primary{background:var(--accent);color:white;border-color:var(--accent)}.btn.danger{background:var(--danger);color:white}.btn.small{padding:6px 10px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(5,minmax(140px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:18px}.metric{display:block}.metric h3{margin:0 0 8px;font-size:18px}.metric b{font-size:36px}.metric.danger{border-color:#fecaca;background:#fff7f7}.metric:hover{box-shadow:0 6px 20px rgba(15,23,42,.08)}.search{display:flex;gap:8px;flex:1;max-width:420px}.search input,input,select,textarea{width:100%;border:1px solid var(--border);border-radius:12px;padding:11px;font-size:15px;background:#fff}textarea{min-height:90px}.panel{background:#fff;border:1px solid var(--border);border-radius:18px;padding:16px;margin-top:12px}.row{display:grid;grid-template-columns:1.2fr 1.2fr 1fr .8fr auto;gap:8px;align-items:center;border-bottom:1px solid #eef2f7;padding:11px 4px}.row:last-child{border-bottom:none}.row.camera{grid-template-columns:.9fr 1fr 1fr 1fr 1fr 1.4fr}.row.danger{background:#fff1f2;color:#991b1b;border-radius:12px;padding-left:10px}.badge{display:inline-block;padding:5px 9px;border-radius:999px;font-size:13px;border:1px solid var(--border);background:#f8fafc}.badge.ok{background:var(--softok);color:#166534}.badge.danger{background:var(--softdanger);color:#991b1b}.badge.warn{background:var(--softwarn);color:#92400e}.badge.info{background:#dbeafe;color:#1e40af}.badge.muted{background:#e5e7eb;color:#374151}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.filters a{border:1px solid var(--border);background:#fff;padding:8px 12px;border-radius:999px}.filters a.active{background:var(--accent);color:#fff}.flash{background:#fef3c7;border:1px solid #fde68a;border-radius:12px;padding:10px;margin:10px 0}.breadcrumb{font-size:14px;color:var(--muted);margin:8px 0 14px}.actions{display:flex;gap:8px;flex-wrap:wrap}.formgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.full{grid-column:1/-1}.mobile-card{max-width:560px;margin:0 auto}.hero{font-size:22px;font-weight:800}.hidden{display:none!important}@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.row,.row.camera{display:block}.row>*{margin:5px 0}.formgrid{grid-template-columns:1fr}.top{display:block}.nav{margin-top:10px}.search{max-width:none;margin-top:10px}}
</style>
</head><body><div class="wrap">
<div class="top"><div><div class="brand">7Sense – Data into Action</div><div class="tag">Contract Manager {{version}}</div></div>
<div class="nav"><a href="{{url_for('dashboard')}}">🏠 Dashboard</a><a href="{{url_for('clients')}}">Clientes</a><a href="{{url_for('contracts')}}">Contratos</a><a href="{{url_for('cameras')}}">Câmeras</a><a href="{{url_for('occurrences')}}">Ocorrências</a><a href="{{url_for('agenda_page')}}">Agenda</a><a href="{{url_for('campo')}}">📱 Campo</a>{% if user %}<a href="{{url_for('logout')}}">Sair</a>{% endif %}</div></div>
{% if user %}<div class="breadcrumb">{{breadcrumb or 'Dashboard'}} · {{user['name']}} · Perfil: {{user['role']}}</div>{% endif %}
{% with messages = get_flashed_messages() %}{% if messages %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endif %}{% endwith %}
{{body|safe}}
</div></body></html>
"""


def page(body, breadcrumb="Dashboard", **ctx):
    return render_template_string(BASE, body=body, user=current_user(), version=APP_VERSION, breadcrumb=breadcrumb, **ctx)


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
    <div class="card mobile-card"><div class="hero">Entrar no 7Sense CM</div><p class="tag">Gerenciamento de contratos e câmeras.</p>
    <form method="post"><p><label>Email<input name="email" value="marcos@7sense.local"></label></p><p><label>Senha<input name="password" type="password" value="123456"></label></p><button class="primary">Entrar</button></form>
    <p class="tag">Operação: marcos@7sense.local / 123456<br>Diretoria: diretoria@7sense.local / 123456</p></div>
    """
    return page(body, breadcrumb="Login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/app")
@login_required
def dashboard():
    contracts_active = count("SELECT COUNT(*) FROM contracts WHERE status!='Encerrado'")
    cams = count("SELECT COUNT(*) FROM cameras")
    attention = count("SELECT COUNT(*) FROM cameras WHERE status IN ('Offline','Em manutenção')")
    occ_open = count("SELECT COUNT(*) FROM occurrences WHERE status IN ('Aberta','Em andamento')")
    today = date.today().isoformat()
    agenda_today = count("SELECT COUNT(*) FROM agenda WHERE event_date=?", (today,))
    body = f"""
    <div class="grid">
      <a class="card metric" href="{url_for('contracts')}"><h3>Contratos ativos</h3><b>{contracts_active}</b></a>
      <a class="card metric" href="{url_for('cameras')}"><h3>Câmeras cadastradas</h3><b>{cams}</b></a>
      <a class="card metric {'danger' if attention else ''}" href="{url_for('cameras', status='atenção')}"><h3>Câmeras com atenção</h3><b>{attention}</b></a>
      <a class="card metric" href="{url_for('occurrences', status='abertas')}"><h3>Ocorrências abertas</h3><b>{occ_open}</b></a>
      <a class="card metric" href="{url_for('agenda_page', filtro='hoje')}"><h3>Agenda hoje</h3><b>{agenda_today}</b></a>
    </div>
    <div class="panel"><h2>Pesquisa global</h2><form action="{url_for('search')}" class="search"><input name="q" placeholder="Toyota, CAM-007, Sorocaba..."><button>Pesquisar</button></form></div>
    <div class="panel"><h2>Modo demonstração</h2><div class="actions"><a class="btn primary" href="{url_for('load_demo')}">Carregar dados de demonstração</a><a class="btn danger" href="{url_for('clear_demo')}">Limpar dados demonstração</a></div></div>
    """
    return page(body)


@app.route("/clients")
@login_required
def clients():
    rows = query("SELECT * FROM clients ORDER BY active DESC, name")
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
            execute("INSERT INTO clients(name,fantasy,cnpj,responsible,phone,email,city,state,notes,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", vals+(datetime.now().isoformat(),))
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
    q = "" if status == "Todos" else "WHERE c.status=?"
    params = () if status == "Todos" else (status,)
    rows = query(f"SELECT c.*, cl.name client_name, (SELECT COUNT(*) FROM cameras ca WHERE ca.contract_id=c.id) cam_count FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id {q} ORDER BY c.created_at DESC", params)
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
    clients_rows = query("SELECT * FROM clients WHERE active=1 ORDER BY name")
    if request.method == "POST":
        vals = (request.form.get("client_id"), request.form.get("obra"), request.form.get("city"), request.form.get("state"), request.form.get("start_date"), request.form.get("end_date"), int(request.form.get("expected_cameras") or 0), request.form.get("status"), request.form.get("notes"))
        if r:
            execute("UPDATE contracts SET client_id=?,obra=?,city=?,state=?,start_date=?,end_date=?,expected_cameras=?,status=?,notes=? WHERE id=?", vals+(r["id"],))
            flash("Contrato atualizado.")
            return redirect(url_for("contract_view", id=r["id"]))
        else:
            execute("INSERT INTO contracts(code,client_id,obra,city,state,start_date,end_date,expected_cameras,status,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (contract_code(),)+vals+(datetime.now().isoformat(),))
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
    items = "".join([camera_row(c) for c in cams])
    body = f"""<div class="panel"><h2>{r['client_name']} – {r['obra']}</h2><p><span class="badge {status_class(r['status'])}">{r['status']}</span> · {r['city']}/{r['state']} · Previstas: {r['expected_cameras']}</p>
    <div class="actions"><a class="btn" href="{url_for('contracts')}">Voltar</a>{'<a class=\"btn primary\" href=\"'+url_for('camera_new', contract_id=r['id'])+'\">Adicionar câmera</a>' if current_user()['role']=='operacao' else ''}</div></div>
    <div class="panel"><h2>Câmeras do contrato</h2>{items or '<p>Nenhuma câmera.</p>'}</div>"""
    return page(body, breadcrumb=f"Dashboard > Contratos > {r['client_name']} {r['obra']}")


def camera_row(c):
    cls = status_class(c["status"])
    aprovado = " 🧪" if ("tested_approved_at" in c.keys() and c["tested_approved_at"]) or c["status"] == "Testada e aprovada" else ""
    qr_btn = f"<a class='btn small' href='{url_for('camera_qr', id=c['id'])}'>📷 QR</a>"
    test_btn = f"<a class='btn small' href='{url_for('camera_approve', id=c['id'])}'>🧪 Teste</a>" if current_user() and current_user()['role']=='operacao' else ""
    edit_btns = (f"<a class='btn small' href='{url_for('camera_edit', id=c['id'])}'>Editar</a> <a class='btn small' href='{url_for('camera_transfer', id=c['id'])}'>Transferir</a>") if current_user() and current_user()['role']=='operacao' else ""
    cliente = rv(c, 'client_name', '-') or '-'
    obra = rv(c, 'obra', '-') or '-'
    return f"<div class='row camera { 'danger' if cls=='danger' else ''}'><b>{c['code']}{aprovado}</b><span><b>{cliente}</b><br><small>{obra}</small></span><span>{c['current_location'] or '-'}</span><span>{c['service'] or '-'}</span><span><span class='badge {cls}'>{c['status']}</span></span><span class='actions'>{qr_btn}{test_btn}<a class='btn small' href='{url_for('camera_view', id=c['id'])}'>Ver</a>{edit_btns}</span></div>"


@app.route("/cameras")
@login_required
def cameras():
    status = request.args.get("status", "Todas")
    params = ()
    where = ""
    if status == "atenção":
        where = "WHERE ca.status IN ('Offline','Em manutenção')"
    elif status != "Todas":
        where = "WHERE ca.status=?"; params = (status,)
    rows = query(f"""SELECT ca.*, co.obra, co.city contract_city, co.state contract_state, cl.name client_name
                    FROM cameras ca
                    LEFT JOIN contracts co ON co.id=ca.contract_id
                    LEFT JOIN clients cl ON cl.id=co.client_id
                    {where}
                    ORDER BY cl.name, co.obra, ca.code""", params)
    filters = ["Todas", "Em operação", "Offline", "Em manutenção", "Em estoque", "Em transporte", "Retirada", "Testada e aprovada", "Aguardando teste"]
    fhtml = "".join([f"<a class='{ 'active' if status==s else ''}' href='{url_for('cameras', status=s)}'>{s}</a>" for s in filters])

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
            cam_rows = "".join([camera_row(cam) for cam in cams])
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
    clients_rows = query("SELECT * FROM clients WHERE active=1 ORDER BY name")
    contracts_rows = query("SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id ORDER BY cl.name, c.obra")
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
        execute("INSERT INTO cameras(code,model,serial,contract_id,current_location,service,status,notes,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (code,)+vals+(datetime.now().isoformat(),))
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
    occs = query("SELECT * FROM occurrences WHERE camera_id=? ORDER BY created_at DESC", (id,))
    hist_parts = []
    for h in hist:
        foto = rv(h, 'install_photo', '')
        foto_html = f"<br><img src='{foto}' alt='Foto da instalação' style='max-width:220px;border-radius:12px;border:1px solid #dbe3ef;margin-top:8px'>" if foto else ""
        hist_parts.append(f"<div class='row'><b>{h['created_at'][:16]}</b><span>{h['old_status'] or '-'} → {h['new_status'] or '-'}</span><span>{h['old_location'] or '-'} → {h['new_location'] or '-'}</span><span>{h['user_name'] or ''}</span><span>{h['note'] or ''}{foto_html}</span></div>")
    hist_html = "".join(hist_parts)
    occ_html = "".join([f"<div class='row'><b>{o['title']}</b><span>{o['problem']}</span><span>{o['status']}</span><span>{o['created_at'][:16]}</span><span></span></div>" for o in occs])
    body = f"""<div class="panel"><h2>{c['code']}</h2><p><span class="badge {status_class(c['status'])}">{c['status']}</span></p><p>Cliente/Obra: {c['client_name'] or '-'} / {c['obra'] or '-'}</p><p>Local: {c['current_location'] or '-'}</p><p>Serviço: {c['service'] or '-'}</p>{f"<p><b>Última foto da instalação:</b><br><img src='{rv(c, 'last_install_photo', '')}' alt='Foto da instalação' style='max-width:320px;border-radius:14px;border:1px solid #dbe3ef;margin-top:8px'></p>" if rv(c, 'last_install_photo', '') else ''}<div class="actions"><a class="btn" href="{url_for('cameras')}">Voltar</a>{'<a class=\"btn primary\" href=\"'+url_for('camera_transfer', id=c['id'])+'\">Transferir</a><a class=\"btn\" href=\"'+url_for('occurrence_new', camera_id=c['id'])+'\">Abrir ocorrência</a>' if current_user()['role']=='operacao' else ''}</div></div>
    <div class="panel"><h2>Histórico</h2>{hist_html or '<p>Sem histórico.</p>'}</div><div class="panel"><h2>Ocorrências</h2>{occ_html or '<p>Sem ocorrências.</p>'}</div>"""
    return page(body, breadcrumb=f"Dashboard > Câmeras > {c['code']}")


def build_qr_label(code):
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
    draw.text((450, 1000), "Patrimônio 7Sense", anchor="mm", font=font_sub, fill=(70, 70, 70))
    return label


@app.route("/cameras/<int:id>/qr")
@login_required
def camera_qr(id):
    c = one("SELECT * FROM cameras WHERE id=?", (id,))
    if not c:
        flash("Câmera não encontrada.")
        return redirect(url_for("cameras"))
    img = build_qr_label(c["code"])
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
        execute("UPDATE cameras SET status=?, tested_approved_at=?, tested_checklist=?, updated_at=? WHERE id=?", ("Testada e aprovada", datetime.now().isoformat(), checklist, datetime.now().isoformat(), id))
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
    clients_rows = query("SELECT * FROM clients WHERE active=1 ORDER BY name")
    contracts_rows = query("SELECT c.*, cl.name client_name FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id ORDER BY cl.name, c.obra")
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
    where = "WHERE o.status IN ('Aberta','Em andamento')" if status == "abertas" else ""
    rows = query(f"SELECT o.*, ca.code camera_code FROM occurrences o LEFT JOIN cameras ca ON ca.id=o.camera_id {where} ORDER BY o.created_at DESC")
    can_edit = current_user()["role"] == "operacao"
    items = "".join([f"<div class='row'><b>{r['camera_code'] or '-'}</b><span>{r['title']}</span><span>{r['status']}</span><span>{r['created_at'][:16]}</span><span>{'<a class=\"btn small\" href=\"'+url_for('occurrence_close', id=r['id'])+'\">Resolver</a>' if can_edit and r['status']!='Resolvida' else ''}</span></div>" for r in rows])
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>Ocorrências</h2>{'<a class=\"btn primary\" href=\"'+url_for('occurrence_new')+'\">Nova ocorrência</a>' if can_edit else ''}</div>{items or '<p>Nenhuma ocorrência.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Ocorrências")


@app.route("/occurrences/new", methods=["GET", "POST"])
@operacao_required
def occurrence_new():
    cameras_rows = query("SELECT * FROM cameras ORDER BY code")
    cam_id = request.args.get("camera_id")
    if request.method == "POST":
        execute("INSERT INTO occurrences(camera_id,title,problem,status,responsible,notes,created_at) VALUES(?,?,?,?,?,?,?)", (request.form.get("camera_id"), request.form.get("title"), request.form.get("problem"), "Aberta", request.form.get("responsible"), request.form.get("notes"), datetime.now().isoformat()))
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
        rows = query("SELECT * FROM agenda WHERE event_date=? ORDER BY event_time", (date.today().isoformat(),))
    else:
        rows = query("SELECT * FROM agenda ORDER BY event_date,event_time LIMIT 100")
    can_edit = current_user()["role"] == "operacao"
    items = "".join([f"<div class='row'><b>{r['event_date']} {r['event_time']}</b><span>{r['title']}</span><span>{r['notes'] or ''}</span><span></span><span></span></div>" for r in rows])
    body = f"<div class='panel'><div class='actions'><h2 style='flex:1'>Agenda</h2>{'<a class=\"btn primary\" href=\"'+url_for('agenda_new')+'\">Novo evento</a>' if can_edit else ''}</div>{items or '<p>Nenhum evento.</p>'}</div>"
    return page(body, breadcrumb="Dashboard > Agenda")


@app.route("/agenda/new", methods=["GET", "POST"])
@operacao_required
def agenda_new():
    if request.method == "POST":
        execute("INSERT INTO agenda(title,event_date,event_time,notes,created_at) VALUES(?,?,?,?,?)", (request.form.get("title"), request.form.get("event_date"), request.form.get("event_time"), request.form.get("notes"), datetime.now().isoformat()))
        flash("Evento criado.")
        return redirect(url_for("agenda_page"))
    body = """<div class="panel"><h2>Novo evento</h2><form method="post" class="formgrid"><label>Título<input name="title"></label><label>Data<input type="date" name="event_date"></label><label>Hora<input type="time" name="event_time"></label><label class="full">Observações<textarea name="notes"></textarea></label><div class="full"><button class="primary">Salvar</button></div></form></div>"""
    return page(body, breadcrumb="Dashboard > Agenda > Novo")


@app.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    like = f"%{q}%"
    clients_rs = query("SELECT 'Cliente' tipo, name titulo, city detalhe, '/clients' link FROM clients WHERE name LIKE ? OR city LIKE ?", (like, like)) if q else []
    contracts_rs = query("SELECT 'Contrato' tipo, coalesce(cl.name,'') || ' - ' || coalesce(c.obra,'') titulo, c.city detalhe, '/contracts/' || c.id link FROM contracts c LEFT JOIN clients cl ON cl.id=c.client_id WHERE cl.name LIKE ? OR c.obra LIKE ? OR c.city LIKE ? OR c.code LIKE ?", (like, like, like, like)) if q else []
    cameras_rs = query("SELECT 'Câmera' tipo, code titulo, current_location detalhe, '/cameras/' || id link FROM cameras WHERE code LIKE ? OR current_location LIKE ? OR service LIKE ?", (like, like, like)) if q else []
    occ_rs = query("SELECT 'Ocorrência' tipo, title titulo, problem detalhe, '/occurrences' link FROM occurrences WHERE title LIKE ? OR problem LIKE ?", (like, like)) if q else []
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
                if action == "Retirada":
                    # Retirada encerra o ciclo operacional: limpa contrato/local/serviço e exige novo teste antes de reutilizar.
                    reset_note = (note + " | " if note else "") + "Retirada confirmada. Dados operacionais limpos; câmera aguardando teste."
                    execute("UPDATE cameras SET status=?, contract_id=NULL, current_location='', service='', updated_at=? WHERE id=?", ("Aguardando teste", datetime.now().isoformat(), camera["id"]))
                    execute("INSERT INTO camera_history(camera_id,old_location,new_location,old_status,new_status,note,user_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (camera["id"], old_loc, "", old_status, "Retirada / Aguardando teste", reset_note, "Campo", datetime.now().isoformat()))
                    msg = "Câmera retirada. Dados operacionais limpos e câmera enviada para Aguardando teste."
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
        btns = []
        for idx, step in enumerate(workflow):
            label = action_labels[step]
            if idx <= max_done:
                btns.append(f'<button type="button" class="done" disabled>✅ {label}</button>')
            elif step == next_step:
                confirm = ' onclick="return confirm(\'Confirmar retirada? Os dados operacionais serão limpos e a câmera ficará aguardando teste.\')"' if step == 'Retirada' else ''; btns.append(f'<button name="action" value="{step}" class="active-step"{confirm}>{label}</button>')
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
<div class="card"><form method="post" enctype="multipart/form-data"><input type="hidden" name="code" value="{{camera['code']}}"><label>Local atual / instalação<input name="local" placeholder="Poste 1, Retro 1, Portaria..." value="{{camera['current_location'] or ''}}"></label><label>Observação<textarea name="note" placeholder="Observação opcional"></textarea></label>{% if next_step == 'Em operação' %}<label>📸 Foto da instalação (opcional)<input type="file" name="install_photo" accept="image/*" capture="environment"></label><p class="small">A foto não trava o fluxo. Se enviada, ficará anexada ao histórico da câmera.</p>{% endif %}<p class="tag"><b>Fluxo operacional</b></p><p class="small">Etapas concluídas ficam verdes e bloqueadas. Somente a próxima etapa fica liberada.</p>{% if camera['status'] == 'Aguardando teste' %}<p style="background:#fef3c7;border-radius:12px;padding:12px"><b>🧪 Aguardando teste:</b> esta câmera precisa ser testada e aprovada no painel antes de ser enviada para nova obra.</p>{% endif %}{{buttons_html|safe}}<hr style="border:0;border-top:1px solid #eef2f7;margin:18px 0"><label>Problema operacional<input name="problem" placeholder="Sem energia, sem sinal, dano físico..."></label><button name="action" value="PROBLEMA" class="danger">🔴 Registrar problema</button></form></div>
<div class="card"><h3>Histórico recente</h3><div class="timeline">{% for h in history %}<div class="timeline-item"><b>{{h['new_status']}}</b><br><span class="small">{{h['created_at'][:16].replace('T',' ')}} · {{h['user_name'] or 'Campo'}}</span><br><span class="small">{{h['note'] or ''}}</span></div>{% else %}<p class="tag">Sem histórico ainda.</p>{% endfor %}</div></div>{% endif %}
</div><script>let scanner=null;document.getElementById('startQr').addEventListener('click', async()=>{const r=document.getElementById('reader');r.style.display='block'; if(!window.Html5Qrcode){alert('Leitor QR não carregou. Digite o código manualmente.');return;} scanner=new Html5Qrcode('reader'); try{await scanner.start({facingMode:'environment'},{fps:10,qrbox:220}, txt=>{document.getElementById('code').value=txt.trim(); scanner.stop(); document.getElementById('lookup').submit();});}catch(e){alert('Não foi possível abrir a câmera. Verifique HTTPS/permissão ou digite o código manualmente.');}});</script>
</body></html>
""", camera=camera, msg=msg, buttons_html=buttons_html, history=history, next_step=next_step if camera else None)


@app.route("/demo/load")
@operacao_required
def load_demo():
    clear_demo_data()
    now = datetime.now().isoformat()
    demo_contracts = [
        ("Toyota", "Toyota Sorocaba", "Sorocaba", "SP", 10, "Operação"),
        ("Equinix", "Tamboré", "Barueri", "SP", 5, "Operação"),
        ("Microsoft", "Hortolândia", "Hortolândia", "SP", 6, "Implantação"),
        ("Zortea", "Itaituba", "Itaituba", "PA", 3, "Planejamento"),
        ("Afonso França", "Hangar Guarulhos", "Guarulhos", "SP", 4, "Manutenção"),
    ]
    service_cycle = ["Acompanhamento de Valas","Acompanhamento de Valas","Timelapse","Timelapse","IA Segurança","IA Segurança","Controle de Pessoas","IA BIM","IA BIM","Timelapse"]
    loc_cycle = ["Retro 01","Retro 01","Torre Norte","Torre Sul","Portaria","Almoxarifado","Entrada","Frente de Obra","Vala 03","Canteiro"]
    cam_num = 1
    for name, obra, city, st, qt, status in demo_contracts:
        execute("INSERT INTO clients(name,city,state,responsible,active,demo,created_at) VALUES(?,?,?,?,?,?,?)", (name, city, st, "Responsável teste", 1, 1, now))
        cid = scalar(one("SELECT id FROM clients WHERE name=? AND city=? AND demo=1 AND created_at=? ORDER BY id DESC", (name, city, now)))
        code_contract = contract_code()
        execute("INSERT INTO contracts(code,client_id,obra,city,state,start_date,end_date,expected_cameras,status,demo,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (code_contract, cid, obra, city, st, date.today().isoformat(), (date.today()+timedelta(days=365)).isoformat(), qt, status, 1, now))
        coid = scalar(one("SELECT id FROM contracts WHERE code=? ORDER BY id DESC", (code_contract,)))
        for i in range(qt):
            code = f"7S-CAM-{cam_num:03d}"
            cstatus = "Em operação"
            if cam_num in (6,): cstatus = "Offline"
            if cam_num in (10,): cstatus = "Em transporte"
            execute("INSERT INTO cameras(code,contract_id,current_location,service,status,demo,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?)", (code, coid, loc_cycle[i % len(loc_cycle)], service_cycle[i % len(service_cycle)], cstatus, 1, now, now))
            cam_id = scalar(one("SELECT id FROM cameras WHERE code=?", (code,)))
            if cstatus == "Offline":
                execute("INSERT INTO occurrences(camera_id,title,problem,status,responsible,demo,created_at) VALUES(?,?,?,?,?,?,?)", (cam_id,"Sem comunicação","Câmera offline para demonstração","Aberta","João",1,now))
            cam_num += 1
    execute("INSERT INTO agenda(title,event_date,event_time,notes,demo,created_at) VALUES(?,?,?,?,?,?)", ("Instalação Microsoft", date.today().isoformat(), "09:00", "Demonstração", 1, now))
    execute("INSERT INTO agenda(title,event_date,event_time,notes,demo,created_at) VALUES(?,?,?,?,?,?)", ("Visita Equinix", date.today().isoformat(), "14:00", "Demonstração", 1, now))
    flash("Dados de demonstração carregados.")
    return redirect(url_for("dashboard"))


def clear_demo_data():
    execute("DELETE FROM agenda WHERE demo=1")
    execute("DELETE FROM occurrences WHERE demo=1")
    execute("DELETE FROM camera_history WHERE camera_id IN (SELECT id FROM cameras WHERE demo=1)")
    execute("DELETE FROM cameras WHERE demo=1")
    execute("DELETE FROM contracts WHERE demo=1")
    execute("DELETE FROM clients WHERE demo=1")


@app.route("/demo/clear")
@operacao_required
def clear_demo():
    clear_demo_data()
    flash("Dados de demonstração removidos.")
    return redirect(url_for("dashboard"))


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
