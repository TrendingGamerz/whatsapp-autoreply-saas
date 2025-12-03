import os
import io
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
from supabase import create_client, Client

# ---- ENV & CONIG ----

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev_secret")

# --- SUPABASE CLIENT ----

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---- WHATSAPP CONFIG ----

CONFIG = {}
try:
    with open("config.json", "r") as f:
        CONFIG = json.load(f)
except Exception:
    CONFIG = {}

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

def create_user(email, password_hash):
    supabase.table("users").insert({
        "email": email,
        "password_hash": password_hash
    }).execute()

def get_user_by_email(email):
    result = supabase.table("users").select("*").eq("email", email).execute()
    if result.data:
        return result.data[0]
    return None

def insert_lead(user_id, phone, name, message):
    supabase.table("leads").insert({
        "user_id": user_id,
        "phone": phone,
        "name": name, 
        "message": message,
        "timestamp": datetime.utcnow().isoformat(),
        "handled": 0
    }).execute()

def get_leads(user_id):
    result = supabase.table("leads").select("*").eq("user_id", user_id).order("id", desc=True).execute() 
    return result.data or []

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

        if get_user_by_email(email):
            flash("Email already registered", "danger")
            return redirect(url_for("signup"))
        
        create_user(email, hashed)

        flash("Account created! You can now log in.", "success")
        return redirect(url_for("login"))
    
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        pwd = request.form.get("password")

        user = get_user_by_email(email)

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

    # Use StringIO for CSV text
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)

    # CSV Header
    writer.writerow(["ID", "Phone", "Name", "Message", "Timestamp", "Handled"])

    # CSV Rows
    for row in leads:
        writer.writerow([
            row["id"],
            row["phone"],
            row["name"],
            row["message"],
            row["timestamp"],
            row["handled"]
        ])

    # Convert to bytes for Flask download
    csv_data = csv_buffer.getvalue().encode("utf-8")

    mem = BytesIO()
    mem.write(csv_data)
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