import os
import json
import secrets
import zipfile
import xml.etree.ElementTree as ET
import base64
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = "proprietyguard_global_secret_secure_key"
app.permanent_session_lifetime = timedelta(days=7)

# --- CONFIGURAÇÃO DA BASE DE DADOS (POSTGRESQL / SQLALCHEMY) ---
database_url = os.environ.get("DATABASE_URL")
if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///proprietyguard.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# --- MODELOS DA BASE DE DADOS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    senha_hash = db.Column(db.String(200), nullable=False)
    alert_email = db.Column(db.String(120), nullable=True)
    reset_token = db.Column(db.String(100), nullable=True)
    is_active_account = db.Column(db.Boolean, default=True)
    contratos = db.relationship('Contract', backref='proprietario', lazy=True)

class Contract(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome_arquivo = db.Column(db.String(200), nullable=False)
    tenant_name = db.Column(db.String(150), nullable=False)
    tenant_phone = db.Column(db.String(50), nullable=True)
    rent_amount = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.String(50), nullable=False)
    end_date = db.Column(db.String(50), nullable=False)
    adjustment_index = db.Column(db.String(100), nullable=True)
    alert_language = db.Column(db.String(50), default="")
    alertas_enviados = db.Column(db.String(50), default="")
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

with app.app_context():
    db.create_all()

# --- LEITURA UNIVERSAL DE DOCUMENTOS E IMAGENS ---
def extrair_texto_universal(file_storage):
    filename = file_storage.filename.lower()
    try:
        if filename.endswith('.pdf'):
            leitor = PdfReader(file_storage)
            texto_total = ""
            for pagina in leitor.pages:
                t = pagina.extract_text()
                if t:
                    texto_total += t + "\n"
            return texto_total
            
        elif filename.endswith('.docx'):
            texto_total = ""
            with zipfile.ZipFile(file_storage) as z:
                with z.open('word/document.xml') as xml_file:
                    tree = ET.parse(xml_file)
                    root = tree.getroot()
                    for elem in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
                        if elem.text:
                            texto_total += elem.text + " "
            return texto_total
            
        elif filename.endswith('.pptx'):
            texto_total = ""
            with zipfile.ZipFile(file_storage) as z:
                for nome_arquivo in z.namelist():
                    if nome_arquivo.startswith('ppt/slides/slide'):
                        with z.open(nome_arquivo) as xml_file:
                            tree = ET.parse(xml_file)
                            root = tree.getroot()
                            for elem in root.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}t'):
                                if elem.text:
                                    texto_total += elem.text + " "
            return texto_total
            
        elif filename.endswith(('.png', '.jpg', '.jpeg')):
            file_storage.seek(0)
            return file_storage.read()
            
        elif filename.endswith('.txt'):
            return file_storage.read().decode('utf-8', errors='ignore')
            
        else:
            return None
    except Exception as e:
        print("Erro na extração universal:", e)
        return None

def analisar_contrato_com_gemini(conteudo_documento, filename):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        encoded_image = base64.b64encode(conteudo_documento).decode('utf-8')
        mime_type = "image/jpeg" if filename.lower().endswith(('.jpg', '.jpeg')) else "image/png"
        
        prompt = """
        You are an expert global legal document parser. Analyze this rental/lease contract image.
        Extract the following details precisely and return ONLY valid raw JSON format (no markdown blocks):
        - "tenant_name": Full name of the tenant
        - "tenant_phone": Phone number or "N/A"
        - "rent_amount": Exact rent amount with currency symbol or code
        - "start_date": Start date (YYYY-MM-DD)
        - "end_date": Expiration date (YYYY-MM-DD)
        - "adjustment_index": Index or rule for adjustment
        - "detected_language": The primary language of the contract (e.g., "Portuguese", "English", "French", "Mandarin", etc.)
        """
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": encoded_image}}
                ]
            }]
        }
    else:
        prompt = f"""
        You are an expert global legal document parser. Analyze the following rental/lease contract text.
        Extract the following details precisely and return ONLY valid raw JSON format (no markdown blocks):
        - "tenant_name": Full name of the tenant
        - "tenant_phone": Phone number or "N/A"
        - "rent_amount": Exact rent amount with currency symbol or code
        - "start_date": Start date (YYYY-MM-DD)
        - "end_date": Expiration date (YYYY-MM-DD)
        - "adjustment_index": Index or rule for adjustment
        - "detected_language": The primary language of the contract (e.g., "Portuguese", "English", "French", "Mandarin", etc.)

        Contract text:
        {conteudo_documento[:10000]}
        """
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            raw_text = data['candidates'][0]['content']['parts'][0]['text']
            cleaned = raw_text.replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned)
        return None
    except Exception as e:
        print("Erro IA:", e)
        return None

def gerar_corpo_email_multilingue(tenant_name, end_date, dias_restantes, idioma_desejado):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
    Write a short, professional rental expiration alert email body.
    - Tenant Name: {tenant_name}
    - Expiration Date: {end_date}
    - Days remaining: {dias_restantes}
    - Language to write the email in: {idioma_desejado}

    Return ONLY the plain text body of the email, no extra conversational filler.
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception:
        pass
    return f"Hello! The contract for tenant {tenant_name} expires on {end_date} ({dias_restantes} days remaining)."

def enviar_email_generico(destinatario, assunto, corpo):
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": "ProprietyGuard <onboarding@resend.dev>",
        "to": [destinatario],
        "subject": assunto,
        "text": corpo
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        return response.status_code == 200
    except Exception:
        return False

# --- ROTAS DE AUTENTICAÇÃO E PERFIL ---

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        senha = request.form.get('senha')
        if User.query.filter_by(email=email).first():
            flash('Este e-mail já está cadastrado.', 'danger')
            return redirect(url_for('register'))
        db.session.add(User(email=email, senha_hash=generate_password_hash(senha)))
        db.session.commit()
        flash('Conta criada com sucesso! Faça o login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        senha = request.form.get('senha')
        usuario = User.query.filter_by(email=email).first()
        if usuario and check_password_hash(usuario.senha_hash, senha):
            if not usuario.is_active_account:
                flash('A tua conta foi suspensa ou bloqueada pelo administrador.', 'danger')
                return redirect(url_for('login'))
            session.permanent = True
            session['user_id'] = usuario.id
            session['user_email'] = usuario.email
            flash('Login efetuado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            flash('E-mail ou senha incorretos.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Sessão encerrada.', 'success')
    return redirect(url_for('login'))

@app.route('/perfil', methods=['GET', 'POST'])
def perfil():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    usuario = db.session.get(User, session['user_id'])
    if not usuario or not usuario.is_active_account:
        session.clear()
        flash('A tua conta foi suspensa pelo administrador.', 'danger')
        return redirect(url_for('login'))
    if request.method == 'POST':
        usuario.alert_email = request.form.get('alert_email') or None
        db.session.commit()
        flash('E-mail de alerta atualizado com sucesso!', 'success')
        return redirect(url_for('index'))
    return render_template('perfil.html', usuario=usuario)

@app.route('/esqueci-senha', methods=['GET', 'POST'])
def esqueci_senha():
    if request.method == 'POST':
        email = request.form.get('email')
        usuario = User.query.filter_by(email=email).first()
        if usuario:
            token = secrets.token_hex(16)
            usuario.reset_token = token
            db.session.commit()
            link = url_for('redefinir_senha', token=token, _external=True)
            enviar_email_generico(usuario.email, "Recuperação de Senha - ProprietyGuard", f"Clica no link para redefinir a tua senha:\n\n{link}")
        flash('Se o e-mail estiver registado, enviaremos instruções de recuperação.', 'info')
        return redirect(url_for('login'))
    return render_template('esqueci_senha.html')

@app.route('/redefinir-senha/<token>', methods=['GET', 'POST'])
def redefinir_senha(token):
    usuario = User.query.filter_by(reset_token=token).first_or_404()
    if request.method == 'POST':
        usuario.senha_hash = generate_password_hash(request.form.get('senha'))
        usuario.reset_token = None
        db.session.commit()
        flash('Senha redefinida com sucesso! Podes fazer login.', 'success')
        return redirect(url_for('login'))
    return render_template('redefinir_senha.html', token=token)

# --- PAINEL DE ADMINISTRAÇÃO ---
@app.route('/admin', methods=['GET', 'POST'])
def admin_dashboard():
    if request.method == 'POST':
        if request.form.get('admin_senha') == ADMIN_PASSWORD:
            session['is_admin'] = True
            flash('Sessão de Administrador iniciada.', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Senha de administrador incorreta.', 'danger')
            return redirect(url_for('admin_dashboard'))
    if not session.get('is_admin'):
        return render_template('admin_login.html')
    return render_template('admin.html', usuarios=User.query.all(), total_contratos=Contract.query.count())

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    flash('Sessão de admin terminada.', 'info')
    return redirect(url_for('admin'))

@app.route('/admin/toggle/<int:user_id>', methods=['POST'])
def admin_toggle_user(user_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))
    alvo = db.session.get(User, user_id)
    if alvo:
        alvo.is_active_account = not alvo.is_active_account
        db.session.commit()
        flash(f'Estado do utilizador {alvo.email} alterado com sucesso.', 'success')
    return redirect(url_for('admin_dashboard'))

# --- ROTAS PRINCIPAIS E CRUD ---

@app.route('/')
def index():
    if 'user_id' not in session:
        return render_template('landing.html')
    usuario = db.session.get(User, session['user_id'])
    if not usuario or not usuario.is_active_account:
        session.clear()
        flash('A tua conta foi suspensa pelo administrador.', 'danger')
        return redirect(url_for('login'))
    
    meus_contratos = Contract.query.filter_by(user_id=session['user_id']).all()
    hoje = date.today()
    alerta_limite = hoje + timedelta(days=30)
    contratos_alerta = sum(1 for c in meus_contratos if hoje <= datetime.strptime(c.end_date, "%Y-%m-%d").date() <= alerta_limite)
    return render_template('dashboard.html', contratos=meus_contratos, alertas_count=contratos_alerta, hoje=hoje)

@app.route('/novo', methods=['GET', 'POST'])
def novo_contrato():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    usuario = db.session.get(User, session['user_id'])
    if not usuario or not usuario.is_active_account:
        session.clear()
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        arquivo = request.files.get('contrato_pdf')
        if not arquivo or arquivo.filename == '':
            flash('Selecione um arquivo válido.', 'danger')
            return redirect(request.url)
        
        conteudo_doc = extrair_texto_universal(arquivo)
        if not conteudo_doc:
            flash('Não foi possível processar este formato de documento.', 'danger')
            return redirect(request.url)
        
        dados_ia = analisar_contrato_com_gemini(conteudo_doc, arquivo.filename)
        if not dados_ia:
            flash('Erro ao processar com a IA.', 'danger')
            return redirect(request.url)
            
        session['temp_contrato'] = dados_ia
        session['temp_nome_arquivo'] = arquivo.filename
        return redirect(url_for('revisar_contrato'))
    return render_template('novo_contrato.html')

@app.route('/revisar', methods=['GET', 'POST'])
def revisar_contrato():
    if 'user_id' not in session or 'temp_contrato' not in session:
        return redirect(url_for('index'))
    usuario = db.session.get(User, session['user_id'])
    if not usuario or not usuario.is_active_account:
        session.clear()
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        idioma_escolhido = request.form.get('alert_language') or session['temp_contrato'].get('detected_language', 'Portuguese')
        
        novo_c = Contract(
            nome_arquivo=session.get('temp_nome_arquivo'),
            tenant_name=request.form.get('tenant_name'),
            tenant_phone=request.form.get('tenant_phone'),
            rent_amount=request.form.get('rent_amount'),
            start_date=request.form.get('start_date'),
            end_date=request.form.get('end_date'),
            adjustment_index=request.form.get('adjustment_index'),
            alert_language=idioma_escolhido,
            user_id=session['user_id']
        )
        db.session.add(novo_c)
        db.session.commit()
        session.pop('temp_contrato', None)
        session.pop('temp_nome_arquivo', None)
        flash('Contrato revisado e salvo com sucesso!', 'success')
        return redirect(url_for('index'))
        
    dados = session.get('temp_contrato')
    return render_template('revisar.html', dados=dados)

@app.route('/editar/<int:id>', methods=['GET', 'POST'])
def editar_contrato(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    usuario = db.session.get(User, session['user_id'])
    if not usuario or not usuario.is_active_account:
        session.clear()
        return redirect(url_for('login'))
        
    contrato = Contract.query.filter_by(id=id, user_id=session['user_id']).first_or_404()
    if request.method == 'POST':
        contrato.tenant_name = request.form.get('tenant_name')
        contrato.tenant_phone = request.form.get('tenant_phone')
        contrato.rent_amount = request.form.get('rent_amount')
        contrato.start_date = request.form.get('start_date')
        contrato.end_date = request.form.get('end_date')
        contrato.adjustment_index = request.form.get('adjustment_index')
        contrato.alert_language = request.form.get('alert_language') or contrato.alert_language
        db.session.commit()
        flash('Contrato atualizado com sucesso!', 'success')
        return redirect(url_for('index'))
    return render_template('editar.html', contrato=contrato)

@app.route('/deletar/<int:id>', methods=['POST'])
def deletar_contrato(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    usuario = db.session.get(User, session['user_id'])
    if not usuario or not usuario.is_active_account:
        session.clear()
        return redirect(url_for('login'))
    contrato = Contract.query.filter_by(id=id, user_id=session['user_id']).first_or_404()
    db.session.delete(contrato)
    db.session.commit()
    flash('Contrato removido com sucesso.', 'success')
    return redirect(url_for('index'))

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

# --- ROTA CORRIGIDA PARA O CRON-JOB ---
@app.route('/api/verificar-prazos-automatico')
def verificar_prazos_automatico():
    hoje = date.today()
    
    try:
        contratos = Contract.query.all()
    except Exception as e:
        print("Erro ao procurar contratos:", e)
        return jsonify({"status": "error", "message": "Erro de conexao com DB"}), 500

    enviados = 0
    erros = 0
    
    for c in contratos:
        if not c.end_date:
            continue
            
        try:
            data_fim = datetime.strptime(str(c.end_date).strip(), "%Y-%m-%d").date()
            dias_restantes = (data_fim - hoje).days
            
            marco_atual = None
            if 28 <= dias_restantes <= 30:
                marco_atual = "30"
            elif 13 <= dias_restantes <= 15:
                marco_atual = "15"
            elif 1 <= dias_restantes <= 3:
                marco_atual = "3"
                
            if marco_atual:
                enviados_lista = c.alertas_enviados.split(",") if c.alertas_enviados else []
                if marco_atual not in enviados_lista:
                    dono = db.session.get(User, c.user_id)
                    if dono and dono.is_active_account:
                        destino = dono.alert_email if dono.alert_email else dono.email
                        idioma = c.alert_language if c.alert_language else "Portuguese"
                        
                        assunto = f"⚠️ Contract Expiration Alert ({dias_restantes} days) - ProprietyGuard"
                        corpo = gerar_corpo_email_multilingue(c.tenant_name, c.end_date, dias_restantes, idioma)
                        
                        if enviar_email_generico(destino, assunto, corpo):
                            enviados_lista.append(marco_atual)
                            c.alertas_enviados = ",".join(enviados_lista)
                            enviados += 1
        except Exception as err:
            print(f"Erro ao processar contrato ID {c.id}: {err}")
            erros += 1
            continue
            
    try:
        db.session.commit()
    except Exception as err:
        print("Erro ao salvar alteracoes no DB:", err)
        
    return jsonify({
        "status": "ok",
        "enviados": enviados,
        "erros_ignorados": erros
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
