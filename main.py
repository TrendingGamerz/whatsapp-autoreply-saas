import os
import io
import sqlite3
import json
import requests
import csv

from io import BytesIO
from datetime import datetime
from flask import (
    Flask,
    request,
    render_template,
    redirect,
    url_for,
    send_file,
    flash,
    session,
)
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

# ---- ENV & CONIG ----

load_dotenv()

CONFIG = {}
try:
    with open("config.json", "r") as f:
        CONFIG = json.load(f)
except Exception:
    CONFIG = {}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev_secret")

#ensure /db directory exists
if not os.path.exists('db'):
    os.makedirs('db')

DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'data.db')

# Global WhatsApp config (v1: same for all users)
WHATSAPP_VERIFY_TOKEN = os.getenv(
    "WEBHOOK_VERIFY_TOKEN", CONFIG.get("WEBHOOK_VERIFY_TOKEN", "verify_token")
)
WHATSAPP_ACCESS_TOKEN = os.getenv(
    "WHATSAPP_ACCESS_TOKEN", CONFIG.get("WHATSAPP_API_TOKEN", "")
)
WHATSAPP_PHONE_NUMBER_ID = os.getenv(
    "WHATSAPP_PHONE_NUMBER_ID", CONFIG.get("WHATSAPP_PHONE_NUMBER_ID", "")
)
AUTO_REPLY_MESSAGE = os.getenv(
    "AUTO_REPLY_MESSAGE", "Thank you for your message! we'll reply soon."
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
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password_hash TEXT
            )
            """
    )

    # Leads table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            phone TEXT,
            name TEXT,
            message TEXT,
            timestamp TEXT,
            handled INTEGER DEFAULT 0
            )
            """
    )

    conn.commit()
    conn.close()

# Create tables at import time (works with Flask 3 & Gunicorn)
with app.app_context():
    init_db()

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

# ---- WHATSAPP WEBHOOK ----

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Verification (GET)
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            return challenge, 200
        
        return "Verification error", 403
    
    # Incoming messages (POST)
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
        
        # v1: assign all leads to user_id=1 (you, the owner)
        user_id = 1

        # Save lead
        insert_lead(user_id, phone, name, text)

        # Auto_reply
        reply_text = build_auto_reply(name, text)
        send_whatsapp_text(phone, reply_text)

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
    return f"Hi {name or 'there'}! {AUTO_REPLY_MESSAGE}\nReply 1 for prices, 2 for Address."

def send_whatsapp_text(to_phone, text):
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        print("Missing WhatsApp credentials. Cannot send message.")
        return
    
    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        print("WhatsApp API response:", resp.status_code, resp.text)
    except Exception as e:
        print("Send WhatsApp message error:", e)

# ---- AUTH PAGES ----

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email")
        pwd = request.form.get("password")

        if not email or not pwd:
            flash("Email and password are required", "danger")
            return redirect(url_for("signup"))
        
        hashed = generate_password_hash(pwd)

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

@app.route("/login", methods=["GET", "POST"])
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


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    
    user_id = session["user_id"]
    leads = get_leads(user_id)
    return render_template("dashboard.html", leads=leads, total=len(leads))

@app.route("/export")
def export():
    if "user_id" not in session:
        return redirect(url_for("login"))
    
    user_id = session["user_id"]
    leads = get_leads(user_id)

    mem = BytesIO()
    leads = get_leads(user_id)

    mem = BytesIO()
    writer = csv.writer(mem)

    writer.writerow(["ID", "Phone", "Name", "Message", "Timestamp", "Handled"])

    for row in leads:
        writer.writerow([
            row["id"],
            row["phone"],
            row["name"],
            row["message"],
            row["timestamp"],
            row["handled"]
        ])
    
    mem.seek(0)

    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name="leads.csv"
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---- RUN (local dev only) ----

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)