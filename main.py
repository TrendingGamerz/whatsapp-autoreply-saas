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

    # LEADS table (FIXED SQL)
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

# Make sure DB is created on Railway
@app.before_first_request
def setup():
    init_db()

#---- LEAD HELPERS ----

def insert_lead(phone, name, message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO leads (phone, name, message, timestamp) VALUES (?,?,?,?)""",
              (user_id, phone, name, message, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def get_leads():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, phone, name, message, timestamp, handled FROM leads ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows


# ---- WhatsApp webbook ----

WHATSAPP_VERIFY_TOKEN = os.getenv('WHATSAPP_VERIFY_TOKEN', CONFIG.get('WEBHOOK_VERIFY_TOKEN', 'verify_token'))
WHATSAPP_ACCESS_TOKEN = os.getenv('WHATSAPP_ACCESS_TOKEN', CONFIG.get('WHATSAPP_API_TOKEN', ''))
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID', CONFIG.get('WHATSAPP_PHONE_NUMBER_ID', ''))
AUTO_REPLY_MESSAGE = CONFIG.get("AUTO_REPLY_MESSAGE", "Thank you for messaging us!")

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            return challenge, 200
        return "Verification Error", 403

    # POST (incoming message)
    data = request.get_json()
    if not data:
        return "no data", 200
    
    try:
        entry = data["entry"][0]
        value = entry["changes"][0]["value"]
        messages = value.get("messages", [])

        if not messages:
            return "OK", 200

        msg = messages[0]
        phone = msg.get("from")
        text = msg.get("text", {}).get("body", "")

        name = ""
        if "contacts" in value:
            name = value["contacts"][0]["profile"]["name"]

        insert_lead(phone, name, text)

        send_whatsapp_text(phone, f"Hi {name}! {AUTO_REPLY_MESSAGE}")

    except Exception as e:
        print("Webhook error:", e)

    return "EVENT_RECEIVED", 200

    except Exception as e:
        print("Webhook Error:", e)
        return "ERROR", 200

def send_whatsapp_text(to_phone, text):
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        print("Missing Whatsapp credentials")
        return

    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text}
    }

    try:
        # FIXED (uses requests.post insted of request.post)
        r = request.post(url, json=payload, headers=headers)
        print("WhatsApp API Response:", r.text)
    except Exception as e:
        print("Send error:", e)

# ---- ROUTES ----
@app.route('/')
def index():
    return render_template('index.html')


# SIGNUP PAGE
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

# LOGIN PAGE

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
        
        flash("Wrong email or password", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")

# DASHBOARD PAGE

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    leads = get_leads()
    return render_template("dashboard.html", leads=leads, total=len(leads))

# CSV EXPORT

@app.route('/export')
def export():
    leads = get_leads()

    buf = BytesIO()
    writer = csv.writer(buf)
    writer.writerow(['ID', 'Phone', 'Name', 'Message', 'Timestamp', 'Handled'])

    for row in leads: 
        writer.writerow(row)

    buf.seek(0)
    return send_file(buf, mimetype="text/csv", download_name="leads.csv", as_attachment=True)

# ---- RUN ----

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
