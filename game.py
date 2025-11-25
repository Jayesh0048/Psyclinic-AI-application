import os
import json
import boto3
import time
import asyncio
import csv
import hashlib
from datetime import datetime, timedelta
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Dict, Optional
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import redis  # NEW: Redis for shared sessions

# --- TOKEN COUNTER ---
try:
    import tiktoken
    encoder = tiktoken.encoding_for_model("gpt-4")
except:
    print("tiktoken not found. Install: pip install tiktoken")
    encoder = None

def count_tokens(text: str) -> int:
    if not encoder:
        return len(text.split()) * 1.3
    try:
        return len(encoder.encode(text))
    except:
        return len(text.split()) * 1.3

# --- INITIALIZATION ---
load_dotenv()
executor = ThreadPoolExecutor(max_workers=10)

# --- AWS Bedrock ---
try:
    AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if not AWS_REGION:
        raise Exception("AWS_REGION not set")

    retry_config = Config(
        retries={'max_attempts': 3, 'mode': 'standard'},
        max_pool_connections=100,
        connect_timeout=5,
        read_timeout=30
    )

    bedrock_client = boto3.client('bedrock-runtime', region_name=AWS_REGION, config=retry_config)
    MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
    print(f"Bedrock ready: {AWS_REGION} | Model: {MODEL_ID}")

except Exception as e:
    print(f"Bedrock failed: {e}")
    bedrock_client = None

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- CORS: Allow all for now (change to domain in prod) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- USER DATABASE (CSV) ---
USER_DB_FILE = "users.csv"

def init_user_db():
    """Initialize users CSV file if it doesn't exist"""
    if not os.path.exists(USER_DB_FILE):
        with open(USER_DB_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['email', 'password_hash', 'full_name', 'created_at'])

def hash_password(password: str) -> str:
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_user(email: str, password: str) -> bool:
    """Verify user credentials"""
    if not os.path.exists(USER_DB_FILE):
        return False

    password_hash = hash_password(password)
    with open(USER_DB_FILE, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['email'] == email and row['password_hash'] == password_hash:
                return True
    return False

def user_exists(email: str) -> bool:
    """Check if user already exists"""
    if not os.path.exists(USER_DB_FILE):
        return False

    with open(USER_DB_FILE, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['email'] == email:
                return True
    return False

def create_user(email: str, password: str, full_name: str) -> bool:
    """Create new user"""
    if user_exists(email):
        return False

    password_hash = hash_password(password)
    with open(USER_DB_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([email, password_hash, full_name, datetime.now().isoformat()])
    return True

# Initialize DB on startup
init_user_db()

# --- REDIS SESSION MANAGEMENT ---
try:
    redis_client = redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)
    redis_client.ping()  # Test connection
    print("Redis connected for sessions")
except Exception as e:
    print(f"Redis failed: {e}. Falling back to in-memory (NOT FOR PRODUCTION)")
    redis_client = None
    active_sessions = {}  # Fallback (only for dev)

def create_session(email: str) -> str:
    """Create session token and store in Redis"""
    session_token = hashlib.sha256(f"{email}{time.time()}{os.urandom(16)}".encode()).hexdigest()
    session_data = {
        'email': email,
        'created_at': time.time()
    }
    if redis_client:
        redis_client.setex(session_token, 86400, json.dumps(session_data))
    else:
        active_sessions[session_token] = session_data
    return session_token

def verify_session(session_token: Optional[str]) -> Optional[str]:
    """Verify session token from Redis or memory"""
    if not session_token:
        return None

    if redis_client:
        data = redis_client.get(session_token)
        if not data:
            return None
        try:
            session = json.loads(data)
            if time.time() - session['created_at'] > 86400:
                redis_client.delete(session_token)
                return None
            return session['email']
        except:
            return None
    else:
        if session_token not in active_sessions:
            return None
        session = active_sessions[session_token]
        if time.time() - session['created_at'] > 86400:
            del active_sessions[session_token]
            return None
        return session['email']

async def get_current_user(request: Request) -> str:
    """Dependency to get current user from session"""
    session_token = request.cookies.get('session_token')
    email = verify_session(session_token)
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return email

# --- MODELS ---
class LoginRequest(BaseModel):
    email: str
    password: str

class SignupRequest(BaseModel):
    email: str
    password: str
    full_name: str

class HistoryMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    history: List[HistoryMessage]
    persona_prompt: str

class SessionStartRequest(BaseModel):
    age: int
    ethnicity: str
    diseases: str
    working_domain: str
    gender: str
    session_duration: int

class ReportRequest(BaseModel):
    transcript: str

# --- AUTH ENDPOINTS ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Redirect to login or main page based on session"""
    session_token = request.cookies.get('session_token')
    if verify_session(session_token):
        return RedirectResponse(url="/app", status_code=302)
    return RedirectResponse(url="/login", status_code=302)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve login/signup page"""
    return templates.TemplateResponse("logsign.html", {"request": request})

@app.post("/api/login")
async def login(request: LoginRequest, response: Response):
    """Handle login"""
    if verify_user(request.email, request.password):
        session_token = create_session(request.email)
        response.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            secure=True,        # REQUIRED FOR HTTPS (AWS)
            max_age=86400,
            samesite="lax"
        )
        return {"success": True, "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.post("/api/signup")
async def signup(request: SignupRequest):
    """Handle signup"""
    if user_exists(request.email):
        raise HTTPException(status_code=400, detail="Email already registered")

    if create_user(request.email, request.password, request.full_name):
        return {"success": True, "message": "Account created successfully"}
    raise HTTPException(status_code=500, detail="Failed to create account")

@app.post("/api/logout")
async def logout(request: Request, response: Response):
    """Handle logout"""
    session_token = request.cookies.get('session_token')
    if redis_client and session_token:
        redis_client.delete(session_token)
    elif not redis_client and session_token in active_sessions:
        del active_sessions[session_token]
    response.delete_cookie("session_token")
    return {"success": True, "message": "Logged out"}

@app.get("/app", response_class=HTMLResponse)
async def home(request: Request, current_user: str = Depends(get_current_user)):
    """Main application page (protected)"""
    return templates.TemplateResponse("index.html", {"request": request, "user_email": current_user})

# --- CONSTANTS ---
MAX_TOKENS_CHAT = 500
MAX_TOKENS_BACKSTORY = 500
MAX_TOKENS_REPORT = 2000
MAX_TOTAL_TOKENS = 10000

# --- UNLIMITED MEMORY: Shrink Old Messages ---
def fit_to_token_limit(messages: List[Dict], system_prompt: str) -> List[Dict]:
    system_tokens = count_tokens(system_prompt)
    available = MAX_TOTAL_TOKENS - MAX_TOKENS_CHAT - system_tokens - 100

    if available < 500:
        raise HTTPException(500, "System prompt too long")

    total = 0
    kept = []

    for msg in reversed(messages):
        msg_tokens = count_tokens(msg["content"])
        if total + msg_tokens > available:
            max_chars = int(available * 3.5)
            if max_chars > 50:
                msg["content"] = msg["content"][:max_chars] + "..."
                msg_tokens = count_tokens(msg["content"])
            else:
                break
        kept.append(msg)
        total += msg_tokens
        if total > available:
            break

    return list(reversed(kept))

# --- Bedrock Call ---
def _invoke_bedrock_sync(system_prompt: str, messages: List[Dict], max_tokens: int) -> str:
    try:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
            "temperature": 0.8,
            "top_p": 0.9
        })

        response = bedrock_client.invoke_model(
            body=body,
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json"
        )

        result = json.loads(response['body'].read().decode('utf-8'))
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            return content[0]["text"]
        raise Exception("No text in response")

    except ClientError as e:
        code = e.response['Error']['Code']
        if code == 'ThrottlingException':
            raise HTTPException(429, "Rate limit. Wait 10s.")
        raise HTTPException(500, f"Bedrock error: {code}")
    except Exception as e:
        raise HTTPException(500, f"Call failed: {e}")

async def _invoke_bedrock_claude(system_prompt: str, messages: List[Dict], max_tokens: int) -> str:
    if not bedrock_client:
        raise HTTPException(500, "Bedrock not ready")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _invoke_bedrock_sync, system_prompt, messages, max_tokens)

# --- Backstory ---
async def _generate_unique_backstory(diseases: str, age: int, gender: str, profession: str) -> str:
    prompt = "10-sentence trauma backstory. Be specific."
    msg = f"{age}yo {gender} {profession} with {diseases}. What happened?"
    try:
        return await _invoke_bedrock_claude(prompt, [{"role": "user", "content": msg}], MAX_TOKENS_BACKSTORY)
    except:
        return f"Trauma from {diseases.split(',')[0].strip()}."

# --- Persona ---
def create_base_persona(diseases: str, age: int, ethnicity: str, working_domain: str, gender: str, backstory: str) -> str:
    return f"""You are Alex, {age}-year-old {gender} {ethnicity} {working_domain}.
Conditions: {diseases}

Backstory (reveal slowly): {backstory}

RULES:
- Speak in 1-3 short, hesitant sentences.
- Use:"I mean...", "Not sure if..."
- NO actions, NO *asterisks*, NO descriptions.
- Example: "Hi... I don't know where to begin."

You reply with spoken words only."""

# --- Report ---
def create_report_prompt(transcript: str) -> str:
    t = transcript[:3000] + ("..." if len(transcript) > 3000 else "")
    return f"""Analyze session:

{t}


  "You are an expert clinical supervisor. Generate a complete Clinical Supervision Competency Summary Report for the supervisee using the 15 competencies below. Rate each 1-5 (1=major issues, 2=emerging/inconsistent, 3=meets expectations, 4=strong/independent, 5=advanced/model). Base every rating on concrete behavioral evidence from the provided transcript/observations. Calculate average (round to 1 decimal) and overall level (1.0-1.9 Remediation, 2.0-2.9 Emerging, 3.0-3.9 Competent, 4.0-4.9 Strong, 5.0 Advanced).\n\nCOMPETENCIES & ANCHORS (short):\n1. Rapport & Alliance: warm greeting, attunement, trust (1=flat/distant ↔ 5=deep bond)\n2. Empathic Communication: accurate reflection, validates emotion (1=misses/dismisses ↔ 5=deep insight)\n3. Boundaries & Ethics: time, confidentiality, no dual rel. (1=breaches ↔ 5=models ethics)\n4. Session Structure & Flow: agenda, pacing, closure (1=no structure ↔ 5=strategic flow)\n5. Assessment & Questioning: balanced open/closed, thorough (1=superficial/leading ↔ 5=seamless)\n6. Case Conceptualization: links T-E-B, theory-based (1=none ↔ 5=elegant formulation)\n7. Goal-Setting & Treatment Planning: collaborative, measurable (1=vague ↔ 5=client-owned)\n8. Intervention Skills: correct EBP technique, tailored (1=wrong ↔ 5=creative mastery)\n9. Managing Resistance & Affect: names emotion, de-escalates (1=avoids ↔ 5=resolves ruptures)\n10. Cultural Sensitivity: inclusive, adapts (1=stereotypes ↔ 5=deep humility)\n11. Ethical Practice: consent, risk screening (1=lapses ↔ 5=prevents risk)\n12. Clinical Judgment: prioritizes, scope (1=unsafe ↔ 5=intuitive+theory)\n13. Documentation Quality: SOAP/DAP, objective (1=missing ↔ 5=model notes)\n14. Reflective Practice: self-aware, uses feedback (1=defensive ↔ 5=proactive growth)\n15. Professionalism: punctual, prepared (1=late/unprepared ↔ 5=role-model)\n\nREPORT SECTIONS (complete ALL):\n1. Overall Competency Summary: list 15 ratings, average, overall level\n2. Strengths Demonstrated: ≥4 specific examples (score 4-5), format [Competency]: \"quote/paraphrase + behavior\"\n3. Areas for Development: ≥3 specific gaps (score 1-3), format [Competency]: gap + how to improve\n4. Evidence / Supervisor Observations: concrete examples for these 5 areas (quote/paraphrase + context):\n   - Therapeutic Attunement\n   - Therapeutic Skills\n   - Professional Conduct\n   - Clinical Formulation\n   - Risk & Ethics\n5. Training Goals (2-3 SMART goals for lowest scores):\n   Goal | Target Behavior | Timeline | Measure of Progress\n6. Action Plan:\n   - Practice/assignments (2-3)\n   - Required supervision focus (2-3)\n   - Resources (2-4 specific readings/videos/shadowing)\n\nUse professional, evidence-based, developmental language. Be specific, never vague. Output ONLY the sections above with clear headers. Language: {report_language}\n\nTRANSCRIPT/OBSERVATIONS:\n{patient_transcript}"
"""

# --- PROTECTED ENDPOINTS ---
@app.get("/M-30India.mp4")
async def get_video(current_user: str = Depends(get_current_user)):
    path = "templates/M-30India.mp4"
    if not os.path.exists(path):
        raise HTTPException(404, "Video not found")
    return FileResponse(path, media_type="video/mp4")

@app.post("/start_session")
async def start_session(request: SessionStartRequest, current_user: str = Depends(get_current_user)):
    start = time.time()
    backstory = await _generate_unique_backstory(
        request.diseases, request.age, request.gender, request.working_domain
    )
    persona = create_base_persona(
        request.diseases, request.age, request.ethnicity,
        request.working_domain, request.gender, backstory
    )
    print(f"Session ready: {time.time()-start:.2f}s")
    return {"system_prompt": persona}

@app.post("/chat")
async def chat(request: ChatRequest, current_user: str = Depends(get_current_user)):
    start = time.time()
    system_prompt = request.persona_prompt

    messages = [
        {"role": m.role, "content": m.content.strip()}
        for m in request.history
        if m.content and m.content.strip()
    ]

    if not messages:
        raise HTTPException(400, "Empty history")

    for i in range(1, len(messages)):
        if messages[i]["role"] == messages[i-1]["role"]:
            raise HTTPException(400, "Wait for patient reply")

    if messages[-1]["role"] != "user":
        raise HTTPException(400, "Therapist must speak last")

    messages = fit_to_token_limit(messages, system_prompt)

    reply = await _invoke_bedrock_claude(system_prompt, messages, MAX_TOKENS_CHAT)
    print(f"Reply: {time.time()-start:.2f}s | Context: {len(messages)} msgs, ~{count_tokens(json.dumps(messages))} tokens")
    return {"reply": reply}

@app.post("/generate_report")
async def generate_report(request: ReportRequest, current_user: str = Depends(get_current_user)):
    start = time.time()
    prompt = create_report_prompt(request.transcript)
    report = await _invoke_bedrock_claude(
        "Concise therapy evaluator.",
        [{"role": "user", "content": prompt}],
        MAX_TOKENS_REPORT
    )
    print(f"Report: {time.time()-start:.2f}s")
    return {"report": report}

@app.get("/loginlist", response_class=HTMLResponse)
async def login_list(request: Request):
    """Admin page to view all registered users"""
    return templates.TemplateResponse("loginlist.html", {"request": request})

@app.get("/api/users")
async def get_users():
    """API endpoint to fetch all users"""
    if not os.path.exists(USER_DB_FILE):
        return {"users": []}

    users = []
    with open(USER_DB_FILE, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            users.append({
                'email': row['email'],
                'full_name': row['full_name'],
                'created_at': row['created_at'],
                'password_hash': row['password_hash'][:10] + '...'  # Show partial hash
            })

    return {"users": users, "total": len(users)}

@app.get("/api/download_users_csv")
async def download_users_csv():
    """Download the complete users.csv file"""
    if not os.path.exists(USER_DB_FILE):
        raise HTTPException(404, "User database not found")
    return FileResponse(
        USER_DB_FILE,
        media_type="text/csv",
        filename=f"users_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )

# --- DELETE USER ---
def delete_user(email: str) -> bool:
    """Delete a user by email. Returns True if deleted."""
    if not os.path.exists(USER_DB_FILE):
        return False

    users = []
    deleted = False

    with open(USER_DB_FILE, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['email'] == email:
                deleted = True
                continue
            users.append(row)

    if deleted:
        with open(USER_DB_FILE, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['email', 'password_hash', 'full_name', 'created_at'])
            writer.writeheader()
            writer.writerows(users)

    return deleted

@app.delete("/api/delete_user/{email}")
async def delete_user_endpoint(email: str):
    """API: Delete user by email"""
    if not delete_user(email):
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "message": "User deleted successfully"}

@app.get("/health")
async def health():
    return {"status": "UNLIMITED MEMORY", "model": MODEL_ID, "ready": True}