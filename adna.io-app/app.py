import streamlit as st
from dotenv import load_dotenv
import os
import re
import base64
import datetime
import sqlite3
from PIL import Image, UnidentifiedImageError
from streamlit.components.v1 import html
import requests
import gspread
from google.oauth2.service_account import Credentials
import json
from passlib.context import CryptContext
import pyrebase
from pathlib import Path
import sys
from Crypto.PublicKey import RSA



# Backend API URL for OpenAI calls
BACKEND_API_URL = st.secrets["BACKEND_API_URL"]

# Firebase config from Streamlit secrets
firebase_config = {
    "apiKey": st.secrets["FIREBASE_API_KEY"],
    "authDomain": st.secrets["FIREBASE_AUTH_DOMAIN"],
    "projectId": st.secrets["FIREBASE_PROJECT_ID"],
    "storageBucket": st.secrets["FIREBASE_STORAGE_BUCKET"],
    "messagingSenderId": st.secrets["FIREBASE_MESSAGING_SENDER_ID"],
    "appId": st.secrets["FIREBASE_APP_ID"],
    "databaseURL": "",
}

firebase = pyrebase.initialize_app(firebase_config)
auth = firebase.auth()

# SQLite DB Setup for rate limiting
conn = sqlite3.connect("usage.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS usage (
    user_id TEXT PRIMARY KEY,
    hooks_used INTEGER DEFAULT 0,
    briefs_used INTEGER DEFAULT 0,
    last_reset DATE
)
""")
conn.commit()

# Password context (legacy / optional)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# User auth utilities (legacy local JSON)
USERS_DB_PATH = "users.json"

def verify_password(email, password):
    if not os.path.exists(USERS_DB_PATH):
        return False
    with open(USERS_DB_PATH, "r") as f:
        users = json.load(f)
    user = users.get(email)
    if not user:
        return False
    return pwd_context.verify(password, user["password_hash"])

def register_user(name, email, password):
    email = email.lower()
    password_hash = pwd_context.hash(password)
    if os.path.exists(USERS_DB_PATH):
        with open(USERS_DB_PATH, "r") as f:
            users = json.load(f)
    else:
        users = {}
    if email in users:
        return False, "Email already registered."
    users[email] = {"name": name, "password_hash": password_hash}
    with open(USERS_DB_PATH, "w") as f:
        json.dump(users, f)
    return True, "Registered successfully."

def get_today():
    return datetime.date.today()

def reset_usage_if_new_day(user_id):
    today = get_today()
    c.execute("SELECT last_reset FROM usage WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO usage (user_id, last_reset) VALUES (?, ?)", (user_id, today))
        conn.commit()
    else:
        last_reset = datetime.datetime.strptime(row[0], "%Y-%m-%d").date()
        if last_reset < today:
            c.execute("UPDATE usage SET hooks_used=0, briefs_used=0, last_reset=? WHERE user_id=?", (today, user_id))
            conn.commit()

def get_usage(user_id):
    reset_usage_if_new_day(user_id)
    c.execute("SELECT hooks_used, briefs_used FROM usage WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row:
        return row[0], row[1]
    return 0, 0

def update_usage(user_id, hooks_delta=0, briefs_delta=0):
    reset_usage_if_new_day(user_id)
    c.execute("UPDATE usage SET hooks_used = hooks_used + ?, briefs_used = briefs_used + ? WHERE user_id = ?", (hooks_delta, briefs_delta, user_id))
    conn.commit()

# --- Check payment status in Google Sheets ---
def is_email_paid(email):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file("adna_backend/gsheet_credentials.json", scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open("Adna_Payments").sheet1
        all_emails = sheet.col_values(2)  # 2nd column is 'Email'
        return email.lower() in [e.lower() for e in all_emails]
    except Exception as e:
        st.error(f"Error checking payment status: {e}")
        return False

# --- Firebase Login Helper ---
def firebase_login(email, password):
    try:
        user = auth.sign_in_with_email_and_password(email, password)
        id_token = user['idToken']
        return id_token
    except Exception as e:
        st.error(f"Firebase login error: {e}")
        return None

# --- Login/Register UI ---
st.set_page_config(page_title="Adna Starter MVP Secure", layout="centered")

if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "id_token" not in st.session_state:
    st.session_state.id_token = None

def auth_interface():
    st.title("🔐 Access Adna")
    tab1, tab2 = st.tabs(["Login", "Register"])

    with tab1:
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_pass")
        if st.button("Login"):
            if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
                st.error("Invalid email format.")
            else:
                id_token = firebase_login(email, password)
                if id_token:
                    if is_email_paid(email):
                        st.session_state.user_id = email.lower()
                        st.session_state.id_token = id_token
                        st.success(f"✅ Access granted for {email}")
                        st.rerun()
                    else:
                        st.error("❌ Email not found in payment records. Please complete payment first.")
                else:
                    st.error("Login failed. Please check your credentials.")

    with tab2:
        name = st.text_input("Full Name")
        reg_email = st.text_input("Email", key="reg_email")
        reg_pass = st.text_input("Password", type="password", key="reg_pass")
        if st.button("Register"):
            if not re.match(r"[^@]+@[^@]+\.[^@]+", reg_email):
                st.error("Invalid email format.")
            else:
                success, msg = register_user(name, reg_email, reg_pass)
                if success:
                    st.success(msg)
                else:
                    st.error(msg)

def logout():
    st.session_state.user_id = None
    st.session_state.id_token = None
    st.rerun()

if st.session_state.user_id is None:
    auth_interface()
    st.stop()
else:
    st.sidebar.markdown(f"👤 Logged in as: **{st.session_state.user_id}**")
    if st.sidebar.button("Logout"):
        logout()

user_id = st.session_state.user_id
id_token = st.session_state.id_token

# Usage tracking
hooks_used, briefs_used = get_usage(user_id)

# --- App UI ---

st.title("📦 Adna - Starter Plan (Secure)")

st.sidebar.header("📊 Usage Tracker")
st.sidebar.markdown(f"Ad Hooks Used: **{hooks_used}/10**")
st.sidebar.markdown(f"Briefs Used: **{briefs_used}/3**")

# 1. Product Image Upload
st.header("1. Upload Your Product Image")
st.markdown("Drag & drop your image below (PNG, JPG, JPEG) or click to browse:")

uploaded_file = st.file_uploader("", type=["png", "jpg", "jpeg"], label_visibility="collapsed")

html("""
<style>
  .dropzone {
    border: 2px dashed #7e3ff2;
    border-radius: 20px;
    padding: 60px;
    text-align: center;
    color: white;
    transition: background 0.3s, border-color 0.3s;
    animation: pulse 2.5s infinite;
    cursor: pointer;
    user-select: none;
    margin-bottom: 20px;
  }
  .dropzone:hover {
    background-color: rgba(126, 63, 242, 0.1);
    border-color: #a78bfa;
  }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(126, 63, 242, 0.5); }
    70% { box-shadow: 0 0 0 15px rgba(126, 63, 242, 0); }
    100% { box-shadow: 0 0 0 0 rgba(126, 63, 242, 0); }
  }
</style>
<div class="dropzone" onclick="document.querySelector('input[type=file]').click();">
  <p>🖱️ Drop your product image here or click to browse</p>
</div>
""", height=160)

product_category = None

if uploaded_file:
    try:
        image = Image.open(uploaded_file)
        image.verify()
        st.image(uploaded_file, caption="✅ Uploaded Image Preview", use_column_width=True)
        st.success("Image uploaded successfully!")
        # TODO: Replace with real category detection
        product_category = "Lifestyle Gadget"
        st.info(f"Detected Category: **{product_category}**")
    except UnidentifiedImageError:
        st.error("❌ Invalid image file. Please upload a valid PNG or JPG.")
        st.stop()

def sanitize_category(cat: str) -> str:
    cat = cat.strip()[:50]
    cat = re.sub(r"[^a-zA-Z0-9 ]+", "", cat)
    if len(cat) == 0:
        return None
    return cat

safe_category = sanitize_category(product_category) if product_category else None

st.header("2. Generate Ad Hooks & Captions")

if safe_category:
    if hooks_used >= 10:
        st.warning("Monthly ad hook limit reached.")
    else:
        if st.button("🎯 Generate Hooks & Captions"):
            prompt = f"Write 5 catchy ad hooks and captions for a {safe_category}"
            try:
                headers = {"Authorization": f"Bearer {id_token}"}
                resp = requests.post(BACKEND_API_URL, json={"prompt": prompt, "type": "hooks"}, headers=headers)
                if resp.status_code == 200:
                    content = resp.json().get("content")
                    if content:
                        update_usage(user_id, hooks_delta=5)
                        st.markdown("### Generated Hooks:")
                        st.markdown(content)
                    else:
                        st.error("No content returned from backend.")
                else:
                    st.error(f"Backend error: {resp.text}")
            except Exception as e:
                st.error(f"Error generating content: {e}")
else:
    st.info("Upload an image first to detect product category.")

st.header("3. Creative Brief Generator")
if safe_category:
    if briefs_used >= 3:
        st.warning("Monthly brief limit reached.")
    else:
        if st.button("📝 Generate Creative Brief"):
            brief_prompt = f"Create a short creative brief for shooting social content for a {safe_category}"
            try:
                headers = {"Authorization": f"Bearer {id_token}"}
                resp = requests.post(BACKEND_API_URL, json={"prompt": brief_prompt, "type": "brief"}, headers=headers)
                if resp.status_code == 200:
                    content = resp.json().get("content")
                    if content:
                        update_usage(user_id, briefs_delta=1)
                        st.markdown(content)
                    else:
                        st.error("No content returned from backend.")
                else:
                    st.error(f"Backend error: {resp.text}")
            except Exception as e:
                st.error(f"Error generating brief: {e}")
else:
    st.info("Upload an image first to detect product category.")

st.header("5. Hashtag Suggestions")
hashtag_map = {
    "Lifestyle Gadget": ["#LifeHack", "#GadgetGoals", "#SmartLiving", "#TechTok", "#GiftIdeas"],
    "Beauty": ["#GlowingSkin", "#MakeupRoutine", "#BeautyTok", "#ViralProduct"]
}
hashtags = hashtag_map.get(safe_category, ["#Product", "#Ad", "#MustHave"])
st.markdown("**Suggested Hashtags:**")
st.markdown(", ".join(hashtags))

st.header("4. Launch Plan Template")
launch_plan_md = """
### 📲 TikTok Launch Plan
- Hook-based video (0-3s strong opening)
- Include 1 UGC testimonial
- Add 3 hashtags and CTA

### 📸 Instagram Launch Plan
- Carousel with product benefits
- Include creator quote

### 🧠 Meta Ads
- 1x Benefit Ad, 1x Testimonial Ad, 1x UGC Ad
- Test across 3 audiences
"""
st.download_button("📥 Download Launch Plan", launch_plan_md, file_name="launch_plan.md")

st.header("6. UGC Creator Directory (View Only)")
creators = [
    {"Name": "Lebo M.", "Niche": "Beauty", "@Handle": "https://example.com/lebo", "Price": "$150"},
    {"Name": "Thabo D.", "Niche": "Fitness", "@Handle": "https://example.com/thabo", "Price": "$100"},
    {"Name": "Zanele P.", "Niche": "Tech", "@Handle": "https://example.com/zanele", "Price": "$200"},
]
st.table(creators)

st.markdown("---")
st.caption("Adna MVP - Built with ❤️ using Streamlit")
