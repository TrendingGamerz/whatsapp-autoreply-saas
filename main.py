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


WHATSAPP_VERIFY_TOKEN = os.getenv('WHATSAPP_VERIFY_TOKEN')
WHATSAPP_ACCESS_TOKEN = os.getenv('WHATSAPP_ACESS_TOKEN')
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID')


DB_PATH = 'data.db'

# ---- DB INIT ----

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


    c.execute("""
    CREATE TABLE IF NOT EXISTS leads (
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

#---- LEADS ----

def insert_lead(phone, name, message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO leads (phone, name, message, timestamp) VALUES (?,?,?,?)',
              (user_id, phone, name, message, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def get_leads():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, phone, name, message, timestamp FROM leads WHERE user_id=? ORDER BY id DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


# ---- WhatsApp webbook ----

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            return challenge, 200
        return "Verification Error", 403

    try:
        data = request.get_json()
        entry = data['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        messages = value.get('messages', [])

        if not messages:
            return "OK", 200

        message = messages[0]
        from_phone = message['from']
        text = message['text']['body']
        name = value['contacts'][0]['profile']['name']

        # default user 1 for now
        insert_lead(1, from_phone, name, text)

        return "EVENT_RECEIVED", 200

    except Exception as e:
        print("Webhook Error:", e)
        return "ERROR", 200

# ---- ROUTES ----
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email')
        pwd = request.form.get('password')

        # Validate fields
        if not email or not pwd:
            flash("Email and password required", "danger")
            return redirect(url_for('signup'))

        # Hash password
        hashed = generate_password_hash(pwd)

        # Save user
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, hashed))
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
    if request.method == "POST":
        email = request.form.get("email")
        pwd = request.form.get("password")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, password_hash FROM users WHERE email=?", (email,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[1], pwd):
            session["user_id"] = user[0]
            return redirect(url_for("dashword"))
        else:
            flash("Wrong email or password", "danger")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    leads = get_leads(user_id)
    return render_template("dashboard.html", leads=leads, total=len(leads))

@app.route('/export')
def export():
    if 'user_id' not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    leads = get_leads(user_id)

    buf = BytesIO()
    writer = csv.writer(buf)
    writer.writerow(['id', 'phone', 'name', 'message', 'timestamp'])
    for row in leads:
        writer.writerow(row)

    buf.seek(0)
    return send_file(buf, minetyep="text/csv", as_attachment=True, download_name="leads.csv")

# ---- RUN ----

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
