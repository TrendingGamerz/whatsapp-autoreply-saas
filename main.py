import os
import sqlite3
import json
import requests
import csv


from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, send_file, flash, session
from dotenv import load_dotenv
from io import BytesIO
from werkzeug.security import generate_password_hash, check_password_hash 


# Load env
load_dotenv()

# Load config.json if exists
CONFIG = {}
try:
    with open('config.json', 'r') as f:
        CONFIG = json.load(f)
except Exception:
    CONFIG = {}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev_secret")


# env vars (Railway/Render)
WHATSAPP_VERIFY_TOKEN = os.getenv('WHATSAPP_VERIFY_TOKEN', CONFIG.get('WEBHOOK_VERIFY_TOKEN', 'verify_token'))
WHATSAPP_ACCESS_TOKEN = os.getenv('WHATSAPP_ACCESS_TOKEN', CONFIG.get('WHATSAPP_API_TOKEN', ''))
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID', CONFIG.get('WHATSAPP_PHONE_NUMBER_ID', ''))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', CONFIG.get('ADMIN_PASSWORD', 'admin123'))
BASE_URL = os.getenv('BASE_URL', CONFIG.get('BASE_URL', 'http://localhost:5000'))
AUTO_REPLY_MESSAGE = CONFIG.get('AUTO_REPLY_MESSAGE', 'Thanks for messaging. We will reply soon!')


DB_PATH = 'data.db'

# ---- INIT DATABASE ----

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Users table
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE,
        password_hash TEXT
    )
    """)

    # Leads table
    c.execute("""
    CREATE TABLE IS NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT,
    name TEXT,
    message TEXT,
    timestamp TEXT,
    handled INTEGER DEFAULT 0
    )
    """)


    conn.commit()
    conn.close()

#---- LEADS HELPERS ----

def insert_lead(phone, name, message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO leads (phone, name, message, timestamp) VALUES (?,?,?,?)',
              (phone, name, message, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def get_leads():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, phone, name, message, timestamp, handled FROM leads ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return rows



# ---- WhatsApp webbook verification & handling ----
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode and token:
            if mode == 'subscribe' and token == WHATSAPP_VERIFY_TOKEN:
                return challenge, 200
            else:
                return 'Verification token mismatch', 403
        return 'Hello webhook', 200
    
    data = request.get_json()
    if not data:
        return 'No data', 200
    # Basic safe parsing
    try:
        entry = data.get('entry', [])[0]
        changes = entry.get('changes', [])[0]
        value = changes.get('value', {})
        messages = value.get('messages', [])
        if not messages:
            return 'No messages', 200
        message = messages[0]
        from_phone = message.get('from')
        text = message.get('text', {}).get('body', '')
        contacts = value.get('contacts', [])
        name = ''
        if contacts:
            name = contacts[0].get('profile', {}).get('name', '')

        insert_lead(from_phone, name, text)

        reply_text = build_auto_reply(name, text)
        send_whatsapp_text(from_phone, reply_text)
    except Exception as e:
        print('webhook error', e)
    return 'EVENT_RECIVED', 200

def build_auto_reply(name, incoming_text):
    # Simple template. You can create more advanced flows per-costomer later.
    if incoming_text and incoming_text.strip().isdigit():
        cmd = incoming_text.strip()
        if cmd == '1':
            return CONFIG.get('PRICE_TEXT', 'Our basic plan starts at ₹199/month.')
        if cmd == '2':
            return CONFIG.get('ADDRESS_TEXT', 'We are located at ...')
    return f"Hi {name or 'there'}! {AUTO_REPLY_MESSAGE}\nReply 1 for Prices, 2 for Address."

def send_whatsapp_text(to_phone, text):
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        print('Missing WhasApp credentials - cannot send message.')
        return
    url = f'https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_NUMBER_ID}/messages'
    headers = {'Authorization': f'Bearer {WHATSAPP_ACCESS_TOKEN}', 'content-type': 'application/json'}
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'text',
        'text': {'body': text}
    }
    try:
        resp = request.post(url, json=payload, headers=headers, timeout=10)
        print('Send message status', resp.status_code, resp.text)
    except Exception as e:
        print('Failed to send message', e)

# ---- Basic admin + dashboard routes ----
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        # Validate fields
        if not email or not password:
            flash("Email and password required", "danger")
            return redirect(url_for('signup'))

        # Hash password
        password_hash = generate_password_hash(password)

        # Save user
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash))
            conn.commit()
        except:
            flash("Email already registered", "danger")
            return redirect(url_for('signup'))
        conn.close()

        flash("Account Created! You can now login.", "success")
        return redirect(url_for('login'))

    return render_template('signup.html')

# LOGIN

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        pwd = request.form.get('password')

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, password_hash FROM users WHERE email=?", (email,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[1], pwd):
            session['user_id'] = user[0]
            return redirect(url_for('dashboard'))
        else:
            flash("Wrong email or password", "danger")
            return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))


    leads = get_leads(user_id)
    return render_template('dashboard.html', leads=leads, total=len(leads))

@app.route('/export')
def export():
    leads = get_leads()

    # create CSV
    buf = BytesIO()
    writer = csv.writer(buf)
    writer.writerow(['id', 'phone', 'name', 'message', 'timestamp', 'handled'])
    for l in leads:
        writer.writerow(l)
    buf.seek(0)

    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name='leads.csv')

# Start

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
