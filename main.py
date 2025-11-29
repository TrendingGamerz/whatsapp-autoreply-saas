import os
import sqlite3
import json
import requests
import csv


from io import BytesIO
from flask import Flask, request, jsonify, render_template, redirect, url_for, send_file, flash, session
from datetime import datetime
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash 

# ---- ENV & CONFIG ----

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

# WhatsApp global fallback (for now, used for ALL users)
WHATSAPP_VERIFY_TOKEN = os.getenv(
    "WHATSAPP_VERIFY_TOKEN", CONFIG.get("WEBHOOK_VERIFY_TOKEN", "verify_token")
)
WHATSAPP_ACCESS_TOKEN = os.getenv(
    "WHATSAPP_ACCESS_TOKEN", CONFIG.get("WHATSAPP_API_TOKEN", "")
)
WHATSAPP_PHONE_NUMBER_ID = os.getenv(
    "WHATSAPP_PHONE_NUMBER_ID", CONFIG.get("WHATSAPP_PHONE_NUMBER_ID", "")
)
AUTO_REPLY_MESSAGE = CONFIG.get(
    "AUTO_REPLY_MESSAGE", "Thank you for you message! We'll reply soon."
)

# ---- DB HELPERS ----

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Users table
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE,
        password_hash TEXT,
        whatsapp_access_token TEXT,
        whatsapp_phone_id TEXT,
        whatsapp_verify_token TEXT
    )
    """)

    # LEADS table (FIXED SQL)
    c.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        phone TEXT,
        name TEXT,
        message TEXT,
        timestamp TEXT,
        handled INTEGER DEFAULT 0
    )
    """)

    # ---- Simple migrations ----

    # Ensure leads.user_id column exists
    c.execute("PRAGMA table_info(leads)")
    cols = [row["name"] for row in c.fetchall()]
    if "user_id" not in cols:
        c.execute("ALTER TABLE leads ADD COLUMN uaer_id INTEGER")

    # Ensure users has the extra WhatsApp columns
    c.execute("PRAGMA table_info(users)")
    ucols = [row["name"] for row in c.fetchall()]

    if "whatsapp_access_token" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN whatsapp_access_token TEXT")
    if "whatsapp_phone_id" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN whatsapp_phone_id TEXT")
    if "whatsapp_verify_token" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN whatsapp_verify_token TEXT")

    conn.commit()
    conn.close()

# Initialize DB on startup (Flash 3.x safe)
with app.app_context():
    init_db()

#---- LEAD HELPERS ----

def insert_lead(user_id, phone, name, message):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO leads (user_id, phone, name, message, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, phone, name, message, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def get_leads(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, phone, name, message, timestamp, handled
        FROM leads
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows

# ---- WhatsApp webbook ----

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # Verification from Meta
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            return challenge, 200
        
        return "Verification Error", 403

    # Incoming message (POST)
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
            name = value["contacts"][0].get("profile", {}).get("name", "")

        # --- Multi-tenant logic (basic version) ---
        # Get phone_number_id from metadata
        phone_number_id = value.get("metadata", {}).get("phone_number_id")

        # Try to map this phone_number_id to a user
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT id, whatsapp_access_token, whatsapp_phone_id FROM users WHERE whatsapp_phone_id = ?",
            (phone_number_id),
        )
        user_row = c.fetchone()
        conn.close()

        if user_row:
            user_id = user_row["id"]
            token_to_use = user_row["whatsapp_access_token"] or WHATSAPP_ACCESS_TOKEN
            phone_id_to_use = user_row["whatspp_phone_id"] or WHATSAPP_PHONE_NUMBER_ID
        else:
            # fallback: assign to main admin user_id = 1
            user_id = 1
            token_to_use = WHATSAPP_ACCESS_TOKEN
            phone_id_to_use = WHATSAPP_PHONE_NUMBER_ID

        # Save lead
        insert_lead(user_id, phone, name, text)

        # Auto-reply
        reply = build_auto_reply(name, text)
        send_whatsapp_text(phone, reply, token_to_use, phone_id_to_use)

    except Exception as e:
        print("Webhook error:", e)

    return "EVENT_RECEIVED", 200

def build_auto_reply(name, incoming_text):
    incoming_text = (incoming_text or "").strip()

    if incoming_text.isdigit():
        if incoming_text == "1":
            return CONFIG.get("PRICE_TEXT", "Our basic plan starts at ₹199/month.")
        if incoming_text == "2":
            return CONFIG.get("ADDRESS_TEXT", "We are located at ...")
    
    return f"Hi {name or 'there'}! {AUTO_REPLY_MESSAGE}\nReply 1 for Prices, 2 for Address."


def send_whatsapp_text(to_phone, text, access_token=None, phone_number_id=None):
    access_token = access_token or WHATSAPP_ACCESS_TOKEN
    phone_number_id = phone_number_id or WHATSAPP_PHONE_NUMBER_ID
    
    if not access_token or not phone_number_id:
        print("Missing Whatsapp credentials. Cannot send message.")
        return
    
    url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }

    try:
        resp = request.post(url, json=payload, headers=headers, timeout=10)
        print("WhatsApp API respnse:", resp.status_code, resp.text)
    except Exception as e:
        print("Send WhatsApp error:", e)

# ---- AUTH & PAGES ----

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
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute(
                    "INSERT INTO users (email, password_hash) VALUES (?, ?)", 
                    (email, hashed),
            )
            conn.commit()
        except Exception as e:
            print("Signup error:", e)
            flash("Email already registered", "danger")
            conn.close()
            return redirect(url_for("signup"))
        conn.close()

        flash("Account created! You can now log in.", "success")
        return redirect(url_for("login"))
    
    return render_template("signup.html")

# LOGIN PAGE

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        pwd = request.form.get("password")

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], pwd):
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        
        flash("Wrong email or password", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")

# DASHBOARD PAGE

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session["user_id"]
    leads = get_leads(user_id)
    return render_template("dashboard.html", leads=leads, total=len(leads))

# CSV EXPORT

@app.route('/export')
def export():
    if "user_id" not in session:
        return redirect(url_for("login"))
    
    user_id = session["user_id"]
    leads = get_leads(user_id)

    buf = BytesIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Phone", "Name", "Message", "Timestamp", "Handled"])
    for row in leads:
        writer.writerow(row)
    buf.seek(0)

    return send_file(
        buf,
        mimetype="text/csv",
        download_name="leads.csv",
        as_attachment=True,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---- RUN ----

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
