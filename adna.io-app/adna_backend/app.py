from fastapi import FastAPI, Request, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import firebase_admin
from firebase_admin import credentials, auth

import os
from openai import OpenAI
from dotenv import load_dotenv

from passlib.context import CryptContext
import json

# --- Init App ---
app = FastAPI()

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Load Environment Variables ---
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- Google Sheets Setup ---
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS_FILE = "adna_backend/gsheet_credentials.json"
SHEET_NAME = "Adna_Payments"

# --- Firebase Admin Setup ---
FIREBASE_CRED = "adna_backend/firebase_admin_sdk.json"
cred = credentials.Certificate(FIREBASE_CRED)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

def verify_token(id_token: str):
    try:
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']
        return uid
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# --- Password hashing ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
USERS_DB_PATH = "users.json"

def save_user(user_data):
    if os.path.exists(USERS_DB_PATH):
        with open(USERS_DB_PATH, "r") as f:
            users = json.load(f)
    else:
        users = {}

    if user_data["email"] in users:
        raise HTTPException(status_code=400, detail="Email already registered.")

    users[user_data["email"]] = user_data
    with open(USERS_DB_PATH, "w") as f:
        json.dump(users, f)

# --- Request models ---
class PaymentRequest(BaseModel):
    name: str
    email: str
    amount: str
    transaction_id: str

class SignupPaymentRequest(BaseModel):
    email: EmailStr
    password: str
    name: str
    amount: str
    transaction_id: str

# --- Payment Logging Route ---
@app.post("/log-payment")
async def log_payment(req: PaymentRequest):
    try:
        credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
        gc = gspread.authorize(credentials)
        sheet = gc.open(SHEET_NAME).sheet1

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [now, req.name, req.email, req.amount, req.transaction_id]
        sheet.append_row(row)

        return {"status": "success", "message": "Logged to Google Sheets."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- Signup and Payment Logging Combined Route ---
@app.post("/signup-and-log-payment")
async def signup_and_log_payment(req: SignupPaymentRequest):
    hashed_password = pwd_context.hash(req.password)

    user_data = {
        "email": req.email,
        "password_hash": hashed_password,
        "name": req.name,
        "amount_paid": req.amount,
        "transaction_id": req.transaction_id,
    }

    try:
        save_user(user_data)
    except HTTPException as e:
        return {"status": "error", "message": e.detail}

    try:
        credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
        gc = gspread.authorize(credentials)
        sheet = gc.open(SHEET_NAME).sheet1

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [now, req.name, req.email, req.amount, req.transaction_id]
        sheet.append_row(row)
    except Exception as e:
        return {"status": "error", "message": "User saved but payment logging failed: " + str(e)}

    return {"status": "success", "message": "User signed up and payment logged."}

# --- ✅ OpenAI Content Generation Route ---
@app.post("/generate")
async def generate_content(payload: dict, authorization: str = Header(None)):
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header missing or invalid")

    id_token = authorization.split(" ")[1]
    user_uid = verify_token(id_token)

    prompt = payload.get("prompt")
    task_type = payload.get("type")

    if not prompt or not task_type:
        raise HTTPException(status_code=400, detail="Missing prompt or type")

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful marketing copywriter."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=400
        )
        content = response.choices[0].message.content.strip()
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Access Check Route ---
@app.post("/check-access")
async def check_access(data: dict = Body(...)):
    email = data.get("email", "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")

    try:
        credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
        gc = gspread.authorize(credentials)
        sheet = gc.open(SHEET_NAME).sheet1
        records = sheet.get_all_records()

        for row in records:
            if row.get("Email", "").strip().lower() == email:
                return {"access": True}

        return {"access": False}
    except Exception as e:
        return {"error": str(e)}

# --- Health Check Route ---
@app.get("/")
def root():
    return {"status": "Adna backend is running"}
