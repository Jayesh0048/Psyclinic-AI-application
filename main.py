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
import redis

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
        retries={'max_attempts': 5, 'mode': 'standard'},
        max_pool_connections=100,
        connect_timeout=5,
        read_timeout=90
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
    redis_client.ping()
    print("Redis connected for sessions")
except Exception as e:
    print(f"Redis failed: {e}. Falling back to in-memory (NOT FOR PRODUCTION)")
    redis_client = None
    active_sessions = {}

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
    chat_history: Optional[List[Dict[str, str]]] = None

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
            secure=True,
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
MAX_TOKENS_BACKSTORY = 1000
MAX_TOKENS_REPORT = 10000
MAX_TOKENS_IMPROVEMENT = 1000
MAX_TOTAL_TOKENS = 10000

# --- VIDEO SELECTION HELPER ---
# --- VIDEO SELECTION HELPER ---
def get_video_filename(age: int, gender: str) -> str:
    """
    Return appropriate video filename based on age and gender
    
    Age Groups:
    - Young: 18-25
    - Adult: 26-40
    - Middle: 41-60
    - Senior: 61+
    """
    try:
        # Determine age group
        if age <= 25:
            age_group = "young"
        elif age <= 40:
            age_group = "adult"
        elif age <= 60:
            age_group = "middle"
        else:
            age_group = "senior"
        
        # Normalize gender
        gender_lower = gender.lower().strip()
        if gender_lower in ['male', 'm', 'man']:
            gender_key = "male"
        elif gender_lower in ['female', 'f', 'woman']:
            gender_key = "female"
        else:
            # Default to male-adult for non-binary or other
            gender_key = "male"
        
        filename = f"{gender_key}-{age_group}.mp4"
        print(f"Video selection - Input: age={age}, gender='{gender}' -> Output: {filename}")
        return filename
        
    except Exception as e:
        print(f"Error in get_video_filename: {e}")
        return "male-adult.mp4"  # Default fallback

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
    prompt = """You are a clinical psychologist creating realistic patient backgrounds for therapy training simulations.

Your task:
- Generate 4-6 concise sentences about a significant life event
- Focus on realistic psychosocial stressors (job loss, relationship breakdown, isolation, loss, trauma)
- Be specific and humanizing 
- Do NOT mention diagnosis or symptoms, only life events that preceded them"""
    msg = f"{age}yo {gender} {profession} with {diseases}. What happened?"
    try:
        return await _invoke_bedrock_claude(prompt, [{"role": "user", "content": msg}], MAX_TOKENS_BACKSTORY)
    except:
        return f"Trauma from {diseases.split(',')[0].strip()}."

# --- Persona ---
def create_base_persona(diseases: str, age: int, ethnicity: str, working_domain: str, gender: str, backstory: str) -> str:
    return f"""You are Sai, a {age}-year-old {gender} {ethnicity} {working_domain} in therapy.

BACKGROUND: Living with {diseases}. {backstory}

⚠️ ABSOLUTE RULE: OUTPUT ONLY SPOKEN WORDS ⚠️

If your response contains ANY of these, you have FAILED:
- asterisks: *sighs*
- action verbs: pauses, looks, shifts, fidgets
- physical descriptions: nervously, looking down
- stage directions of any kind

Your response must be PURE DIALOGUE that could be spoken aloud naturally.

TEST: Could someone read your exact response out loud in a conversation? 
- If YES → Correct
- If NO (because it has actions/descriptions) → Wrong, try again

RESPONSE STYLE:
- 1-3 sentences, under 50 words
- Use verbal hesitations: "I mean...", "Maybe...", "I guess..."
- Show emotion through WORDS not actions
- Stay in character as vulnerable patient
- Never give advice or analyze like a therapist
- If you don't know how to respond, say: "I'm not sure how to answer that..."

CORRECT RESPONSES (spoken words only):
"I don't know. Everything just feels wrong lately."
"I... I'm not sure I can explain it. It's just hard."
"Maybe? I want to believe that, but..."

INCORRECT (has actions - DO NOT DO THIS):
"*sighs* I don't know."
"I... pauses ...I'm not sure."
"nervously Maybe?"

Begin speaking as the patient. DIALOGUE ONLY."""
def create_report_prompt(transcript: str) -> str:
    t = transcript[:3000] + ("..." if len(transcript) > 3000 else "")
    return f"""Prompt: Clinical Supervision Competency Report Generator

You are an experienced clinical supervisor tasked with evaluating a supervisee's clinical competency and generating a comprehensive Clinical Supervision Competency Summary Report.

Your Task

Based on the supervisee information, session observations, and behavioral evidence provided, you will:

1. Rate each of the 15 clinical competencies using the 1-5 scale
2. Complete all sections of the Clinical Supervision Competency Summary Report
3. Provide specific behavioral evidence to support all ratings
4. Identify strengths and areas for development
5. Create actionable training goals and an implementation plan

---

RATING SCALE

Score 1 - Needs major improvement / missing skill
Meaning: Frequently omits skill, errors, client safety/rapport compromised

Score 2 - Emerging skill, inconsistent, needs support
Meaning: Attempts skill but inconsistent, needs prompting, misses key pieces

Score 3 - Meets expected level for role
Meaning: Performs skill reliably, occasional gaps that do not affect therapy

Score 4 - Strong skill, mostly independent
Meaning: Consistently effective, anticipates needs, minimal supervisor input

Score 5 - Advanced mastery, highly consistent, models skill
Meaning: Models skill, flexible + fluent application, enhances therapy process

---

THE 15 COMPETENCIES TO EVALUATE

# 1. Rapport & Alliance

What It Measures: Trust, safety, therapeutic relationship

Observable Indicators / Evaluation Parameters: Greets warmly; uses client's name; maintains gentle tone; shows respect; checks comfort; collaborative stance; maintains non-judgment; attuned responses

Rating Anchors:
- Score 1: Flat/tense tone, avoids eye contact, appears distracted, client guarded
- Score 2: Basic warmth but inconsistent attunement, forced or rehearsed rapport
- Score 3: Warm, respectful, open body language, client comfortable
- Score 4: Highly attuned, repairs ruptures, creates strong comfort quickly
- Score 5: Deep trust evident, client highly engaged, strong safe therapeutic bond

---

# 2. Empathic Communication

What It Measures: Emotional attunement & reflection

Observable Indicators / Evaluation Parameters: Reflects feelings accurately; uses validating language; pauses to understand; notices non-verbals; responds to emotion not only content

Rating Anchors:
- Score 1: Interrupts client, dismisses emotion, focuses only on content
- Score 2: Attempts reflection but inaccurate/robotic, misses emotional cues
- Score 3: Reflects emotion + content accurately, validates client
- Score 4: Picks nuanced emotional layers, uses silence effectively
- Score 5: Deeply attuned, facilitates emotional insight naturally

---

# 3. Boundaries & Ethics

What It Measures: Professional conduct

Observable Indicators / Evaluation Parameters: Keeps time; avoids dual relationships; appropriate self-disclosure; maintains confidentiality; avoids over-involvement

Rating Anchors:
- Score 1: Blurred boundaries, inappropriate disclosure, time violations
- Score 2: Understands boundaries but inconsistent adherence
- Score 3: Maintains limits, confidentiality, professional tone
- Score 4: Proactively manages boundaries, transparent ethical stance
- Score 5: Models ethical professionalism, addresses boundary concerns immediately

---

# 4. Session Structure & Flow

What It Measures: Organizing and holding space

Observable Indicators / Evaluation Parameters: Sets agenda; reviews goals; manages transitions; tracks time; summarizes; avoids tangents; provides closure

Rating Anchors:
- Score 1: No structure, loses focus, poor time management
- Score 2: Attempted structure, frequent redirection needed
- Score 3: Agenda set, pacing adequate, session ends with brief plan
- Score 4: Clear flow, smooth transitions, grounded closure
- Score 5: Highly strategic flow, anticipates pacing, session feels purposeful + contained

---

# 5. Assessment & Questioning

What It Measures: Information gathering

Observable Indicators / Evaluation Parameters: Balanced open/closed questions; clarifies unclear points; explores symptoms thoroughly; uses probing when appropriate; avoids leading questions

Rating Anchors:
- Score 1: Superficial questions, misses core information, leading questions
- Score 2: Gathers info but disorganized or over-reliant on closed questions
- Score 3: Functional, clinically relevant questioning, adequate depth
- Score 4: Systematic, thorough, responsive probing
- Score 5: Advanced interview skill; integrates observation + nuance seamlessly

---

# 6. Case Conceptualization

What It Measures: Clinical meaning-making

Observable Indicators / Evaluation Parameters: Identifies themes/patterns; links thoughts-emotions-behavior; integrates background; hypotheses grounded in theory; adjusts conceptualization as info emerges

Rating Anchors:
- Score 1: No clear framework, inaccurate interpretations
- Score 2: Basic understanding, struggles to link symptoms & theory
- Score 3: Logical, theory-guided, links T-E-B patterns
- Score 4: Dynamic formulation, integrates new information fluidly
- Score 5: Highly coherent formulation guiding elegant intervention choices

---

# 7. Goal-Setting & Treatment Planning

What It Measures: Direction & alignment

Observable Indicators / Evaluation Parameters: Co-creates goals; goals measurable; aligns interventions to goals; checks client consent on direction; revisits progress

Rating Anchors:
- Score 1: No goals, vague direction
- Score 2: Sets goals but not measurable, therapist-led
- Score 3: Collaborative measurable goals, aligned with client needs
- Score 4: Tracks progress, adapts goals, strong client agency
- Score 5: Client deeply engaged, goals integrated naturally, ongoing evaluation

---

# 8. Intervention Skills

What It Measures: Proper technique use

Observable Indicators / Evaluation Parameters: Chooses evidence-based tools; explains rationale; checks understanding; applies skill correctly; tailors to client; observes readiness

Rating Anchors:
- Score 1: Incorrect/unsafe interventions, no rationale
- Score 2: Attempts techniques but mechanical or mismatched
- Score 3: Correct technique, clear rationale, appropriate timing
- Score 4: Fluent technique use, adjusts to client readiness
- Score 5: Seamless, creative application, high client response

---

# 9. Managing Resistance & Affect

What It Measures: Handling distress, avoidance, conflict

Observable Indicators / Evaluation Parameters: Names emotions gently; normalizes protective defenses; uses de-escalation; slows pace when overwhelmed; maintains calm presence

Rating Anchors:
- Score 1: Avoids emotional distress, escalates conflict
- Score 2: Notices discomfort but unsure how to respond
- Score 3: Names emotions, slows pace, normalizes reaction
- Score 4: Skillfully holds intense affect, gentle de-escalation
- Score 5: Resolves ruptures smoothly, builds insight through emotion

---

# 10. Cultural Sensitivity

What It Measures: Inclusivity & cultural awareness

Observable Indicators / Evaluation Parameters: Uses inclusive language; avoids assumptions; invites client's cultural meaning; adapts interventions when culture relevant

Rating Anchors:
- Score 1: Stereotypes or assumptions, cultural blind spots
- Score 2: Awareness present but unsure how to apply
- Score 3: Respectful, asks cultural meaning, avoids assumptions
- Score 4: Culturally attuned adaptation of interventions
- Score 5: Deep cultural humility, integrates context effortlessly

---

# 11. Ethical Practice

What It Measures: Safety, informed consent, documentation

Observable Indicators / Evaluation Parameters: Introduces confidentiality & limits; safety questions when needed; reports risks; maintains clinical records accurately

Rating Anchors:
- Score 1: Ethical breaches, confidentiality lapses
- Score 2: Basic ethics but misses risk screening or forgets boundaries
- Score 3: Follows ethical guidelines, informed consent routine
- Score 4: Identifies ethical dilemmas early, consults when needed
- Score 5: Ethical leader; prevents risk, educates clients, excellent judgement

---

# 12. Clinical Judgment

What It Measures: Decision-making capacity

Observable Indicators / Evaluation Parameters: Prioritizes presenting issues; identifies risk; knows scope; seeks supervision appropriately; avoids premature conclusions

Rating Anchors:
- Score 1: Poor prioritization, unsafe decisions
- Score 2: Understands basics but inconsistent judgement
- Score 3: Prioritizes appropriately, recognizes risk cues
- Score 4: Strong reasoning, anticipates challenges
- Score 5: Excellent judgement, clinical intuition backed by theory

---

# 13. Documentation Quality

What It Measures: Professional note-taking

Observable Indicators / Evaluation Parameters: Notes accurate, objective, timely; includes presenting concerns, interventions, observations, plan; follows format (SOAP/DAP)

Rating Anchors:
- Score 1: Missing or unsafe notes, subjective, disorganized
- Score 2: Notes incomplete or vague
- Score 3: Clear, timely, objective notes following structure
- Score 4: Detailed, concise, intervention-focused
- Score 5: Model-level documentation — measurable outcomes, risk notation, clear plan

---

# 14. Reflective Practice

What It Measures: Insight & growth

Observable Indicators / Evaluation Parameters: Recognizes limitations; self-evaluates; invites feedback; adjusts behavior; remarks on personal reactions

Rating Anchors:
- Score 1: Defensive, unaware of limitations
- Score 2: Acknowledges issues but limited insight or change
- Score 3: Open to feedback, names growth areas
- Score 4: Integrates feedback consistently
- Score 5: Deep reflective capacity, uses insight proactively

---

# 15. Professionalism

What It Measures: Conduct & responsibility

Observable Indicators / Evaluation Parameters: Punctual; prepared; respectful; follows through on tasks; maintains appropriate demeanor; appropriate attire

Rating Anchors:
- Score 1: Unprepared, late, disorganized
- Score 2: Inconsistently professional
- Score 3: Reliable, timely, prepared
- Score 4: Highly dependable, self-directed
- Score 5: Professional role-model, consistently exceeds expectations

---

EVALUATION METHODOLOGY

Step 1: Review All Evidence

Carefully read all provided information about the supervisee including session transcripts or descriptions, supervisor observations, client interactions, documentation samples, self-reflection statements, and previous feedback.

Step 2: Rate Each Competency

For each of the 15 competencies:
1. Identify relevant behavioral evidence from the materials
2. Match observed behaviors to the rating anchors (1-5)
3. Consider consistency - one good moment does not equal consistent competency
4. Account for training level - adjust expectations appropriately
5. Assign the rating that best fits the overall pattern

Step 3: Calculate Average Score

Sum all 15 ratings, divide by 15, and round to one decimal place.

Step 4: Determine Overall Level

Based on average score:
- 1.0 to 1.9: Needs Remediation
- 2.0 to 2.9: Emerging
- 3.0 to 3.9: Competent
- 4.0 to 4.9: Strong
- 5.0: Advanced

---

REPORT SECTIONS TO COMPLETE

# Section 1: Overall Competency Summary

List all 15 competencies with their numerical ratings. Calculate and display average competency score. Check appropriate overall level: Needs Remediation, Emerging, Competent, Strong, or Advanced.

# Section 2: Strengths Demonstrated

Focus on observable behaviours. Provide at least 4 specific strengths.

Requirements:
- Focus on scores of 4-5
- Use behavioral language
- Include specific examples from sessions
- Highlight what supervisee should continue doing

Format: [Competency area]: [Specific observable behavior with concrete example]

# Section 3: Areas for Development

Behaviour-specific & skill-focused. Provide at least 3-4 specific areas.

Requirements:
- Focus on scores of 1-2 (and 3s that need improvement)
- Use growth-oriented language (not deficit-focused)
- Be specific about what to develop
- Suggest how to improve

Format: [Competency area]: [Specific skill gap with developmental recommendation]

# Section 4: Evidence / Supervisor Observations

Concrete examples drawn from session. Provide specific behavioral examples for each of these 5 skill areas:

Therapeutic Attunement: Provide specific example of empathy, rapport-building, or emotional responsiveness

Therapeutic Skills: Provide specific intervention used, technique application, what they did

Professional Conduct: Provide example of boundaries, time management, ethical behavior

Clinical Formulation: Provide how they conceptualized the case, pattern recognition

Risk & Ethics: Provide safety assessment, confidentiality, ethical decision-making example

Requirements:
- Use direct quotes or paraphrased behaviors when possible
- Include context (what was happening in session)
- Show both strengths AND gaps
- Be concrete and observable

# Section 5: Training Goals for Next Placement / Month

Specific, measurable, time-linked goals. Provide 2-3 goals with the following information for each:

Goal: What competency to develop

Target Behaviour / Skill: Specific observable action

Timeline: Timeframe for achievement

Measure of Progress: How success will be evaluated

Requirements:
- Focus on lowest-scoring competencies (1-2 ratings)
- Make goals Specific, Measurable, Achievable, Relevant, Time-bound
- Include observable behavioral targets
- Define clear success metrics

# Section 6: Action Plan

Provide the following 3 subsections:

Practice / assignment areas:
List 2-3 specific activities/exercises to build skills. Include homework, role-plays, practice scenarios.

Required supervision focus:
Identify 2-3 priority topics for supervision sessions. What supervisor and supervisee will work on together.

Resources recommended (readings, role-plays, shadowing):
Suggest 2-4 concrete resources: readings, videos, training modules, shadowing opportunities. Match resources to identified development areas.

---

OUTPUT REQUIREMENTS

1. Complete all sections of the report template
2. Use professional, objective language throughout
3. Ground all ratings in observable evidence - no assumptions
4. Balance developmental feedback - acknowledge strengths while addressing gaps
5. Ensure actionability - reader should know exactly what to do next
6. Match tone to training level - supportive for interns, higher expectations for practicing clinicians
7. Be specific - avoid vague statements like "good rapport" without examples

---

FORMATTING GUIDELINES - TAILWIND + DARK THEME COMPATIBLE (PSYCLINIC AI)

YOU MUST USE THESE EXACT TAILWIND CLASSES AND STYLES TO MATCH THE PAGE THEME:

1. MAIN SECTION HEADERS (Section 1–6) — **HIGHLIGHTED WITH GRADIENT BORDER + GLOW**
   Use:
   <h1 class="text-2xl md:text-3xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 to-purple-500 mb-6 pb-3 border-b-2 border-gradient-to-r from-cyan-400/50 to-purple-500/50 shadow-lg shadow-cyan-500/20">
     Section 1: Overall Competency Summary
   </h1>

2. SUB-HEADERS:
   Use:
   <h3 class="text-xl font-semibold text-cyan-300 mt-8 mb-4 flex items-center gap-2">
     <i class="fas fa-circle text-cyan-400 text-xs"></i> Competency Ratings
   </h3>

3. COMPETENCY TABLE — **GLASSMORPHIC DARK STYLE**
   <div class="overflow-x-auto rounded-xl border border-cyan-400/20 bg-gradient-to-b from-white/5 to-white/2 backdrop-blur-sm">
     <table class="w-full text-sm md:text-base">
       <thead>
         <tr class="bg-gradient-to-r from-cyan-900/30 to-purple-900/30">
           <th class="text-left p-4 font-semibold text-cyan-300">Competency</th>
           <th class="text-center p-4 w-24 font-semibold text-cyan-300">Rating/5</th>
         </tr>
       </thead>
       <tbody class="divide-y divide-cyan-400/10">
         <tr class="hover:bg-white/5 transition-colors">
           <td class="p-4 text-gray-200">1. Rapport & Alliance</td>
           <td class="p-4 text-center font-bold text-cyan-400">4</td>
         </tr>
         [... continue for all 15 ...]
       </tbody>
     </table>
   </div>

4. AVERAGE SCORE & OVERALL LEVEL — **NEON GLOW BOX**
   <div class="mt-8 p-6 rounded-2xl bg-gradient-to-br from-cyan-900/20 to-purple-900/20 border border-cyan-400/30 backdrop-blur-md shadow-xl shadow-cyan-500/20">
     <p class="text-lg md:text-xl font-bold text-cyan-300">
       <i class="fas fa-star mr-2 text-yellow-400"></i>
       Average Competency Score: <span class="text-2xl text-cyan-100">2.2</span>
     </p>
     <p class="text-lg md:text-xl font-bold text-purple-300 mt-2">
       <i class="fas fa-level-up-alt mr-2"></i>
       Overall Level: <span class="text-2xl text-purple-100">EMERGING</span>
     </p>
   </div>

5. BULLET LISTS — **CONSISTENT GLASS STYLE**
   <ul class="space-y-3 mt-4">
     <li class="flex items-start gap-3 p-4 rounded-lg bg-white/5 border border-cyan-400/20 hover:bg-white/10 transition-all">
       <i class="fas fa-check-circle text-green-400 mt-1"></i>
       <span class="text-gray-200"><strong class="text-cyan-300">Rapport & Alliance:</strong> Established warm connection immediately by greeting client by name and using open body language. Client appeared relaxed and engaged throughout.</span>
     </li>
   </ul>

6. LABELS (Goal:, etc.):
   <strong class="text-cyan-300">Goal:</strong> <span class="text-gray-200">...</span>

7. HORIZONTAL DIVIDERS:
   <hr class="my-10 border-t border-gradient-to-r from-transparent via-cyan-400/30 to-transparent">

8. CONSISTENT TYPOGRAPHY:
   - All text: text-gray-200
   - Strong labels: text-cyan-300
   - Ratings: text-cyan-400 font-bold
   - No white backgrounds, no conflicting colors

---

EXAMPLE FORMATTING (Section 1):

<h1 class="text-2xl md:text-3xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 to-purple-500 mb-6 pb-3 border-b-2 border-gradient-to-r from-cyan-400/50 to-purple-500/50 shadow-lg shadow-cyan-500/20">
  Section 1: Overall Competency Summary
</h1>

<h3 class="text-xl font-semibold text-cyan-300 mt-8 mb-4 flex items-center gap-2">
  <i class="fas fa-circle text-cyan-400 text-xs"></i> Competency Ratings
</h3>

<div class="overflow-x-auto rounded-xl border border-cyan-400/20 bg-gradient-to-b from-white/5 to-white/2 backdrop-blur-sm">
  <table class="w-full text-sm md:text-base">
    <thead>
      <tr class="bg-gradient-to-r from-cyan-900/30 to-purple-900/30">
        <th class="text-left p-4 font-semibold text-cyan-300">Competency</th>
        <th class="text-center p-4 w-24 font-semibold text-cyan-300">Rating</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-cyan-400/10">
      <tr class="hover:bg-white/5 transition-colors">
        <td class="p-4 text-gray-200">1. Rapport & Alliance</td>
        <td class="p-4 text-center font-bold text-cyan-400">4</td>
      </tr>
      ...
    </tbody>
  </table>
</div>

<div class="mt-8 p-6 rounded-2xl bg-gradient-to-br from-cyan-900/20 to-purple-900/20 border border-cyan-400/30 backdrop-blur-md shadow-xl shadow-cyan-500/20">
  <p class="text-lg md:text-xl font-bold text-cyan-300">
    <i class="fas fa-star mr-2 text-yellow-400"></i>
    Average Competency Score: <span class="text-2xl text-cyan-100">2.2</span>
  </p>
  <p class="text-lg md:text-xl font-bold text-purple-300 mt-2">
    <i class="fas fa-level-up-alt mr-2"></i>
    Overall Level: <span class="text-2xl text-purple-100">EMERGING</span>
  </p>
</div>

<hr class="my-10 border-t border-gradient-to-r from-transparent via-cyan-400/30 to-transparent">

---

CRITICAL FIXES:
- **All section headers use gradient text + glowing border**
- **No white backgrounds anywhere**
- **Table uses glassmorphic dark style with cyan/purple gradients**
- **Average box is neon-glow styled**
- **All lists use identical glass card style**
- **All text is text-gray-200, strong labels text-cyan-300**
- **Icons used for visual hierarchy**
- **Fully compatible with Psyclinic AI dark theme**

---

INPUT YOU WILL RECEIVE

When generating the report, you will be provided with: Supervisee name and training level, Supervisor name and evaluation date, Session modality and observation source, Session description, transcript excerpts, or behavioral observations, Any relevant background information, and Documentation samples (if applicable).

Based on this input, apply the evaluation criteria above to generate a complete, evidence-based Clinical Supervision Competency Summary Report.

---

FINAL CHECKLIST

Before submitting the report, verify:
- All 15 competencies have numerical ratings (1-5)
- Average score is calculated correctly
- Overall level is checked appropriately
- At least 4 specific strengths are listed with examples
- At least 3 specific development areas are identified
- Evidence section has concrete examples in all 5 skill areas
- 2-3 SMART training goals are included with all components
- Action plan has all 3 subsections completed
- All ratings are supported by observable behavioral evidence
- Language is professional, objective, and developmental
- **FORMATTING: Gradient headers, glass tables, neon boxes, consistent dark theme styling**

TRANSCRIPT:
{t}

Generate a complete Clinical Supervision Competency Summary Report following all sections and guidelines above. **USE TAILWIND CLASSES AND DARK THEME STYLES ABOVE — DO NOT USE HTML STYLES.**
"""
# --- NEW: Improvement Suggestions ---
def create_improvement_prompt(therapist_msg: str, patient_msg: str, context: str) -> str:
    """Generate improvement suggestions for a specific therapist message"""
    context_str = context[:300] if context else "Start of conversation"
    patient_str = patient_msg if patient_msg else "No response yet"
    
    return f"""You are an expert therapy supervisor. Analyze this exchange:

CONTEXT: {context_str}

THERAPIST: "{therapist_msg}"
PATIENT: "{patient_str}"

Respond in this EXACT format:
STATUS: [GOOD or NEEDS_IMPROVEMENT]
ANALYSIS: [1 sentence explaining why]
SUGGESTION: [If NEEDS_IMPROVEMENT, provide a better alternative in 2-3 sentences. If GOOD, write "No changes needed."]

Be strict - only mark as GOOD if the response shows excellent therapeutic skills."""

# --- VIDEO ENDPOINTS ---
@app.get("/videos/{filename}")
async def get_video_by_name(filename: str, current_user: str = Depends(get_current_user)):
    """Serve video files dynamically based on filename"""
    # Security: Only allow specific video files
    allowed_videos = [
        "male-young.mp4", "male-adult.mp4", "male-middle.mp4", "male-senior.mp4",
        "female-young.mp4", "female-adult.mp4", "female-middle.mp4", "female-senior.mp4"
    ]
    
    if filename not in allowed_videos:
        raise HTTPException(404, "Video not found")
    
    path = f"templates/{filename}"
    if not os.path.exists(path):
        raise HTTPException(404, f"Video file not found: {filename}")
    
    return FileResponse(path, media_type="video/mp4")

# --- LEGACY VIDEO ENDPOINT (for backwards compatibility) ---
@app.get("/M-30India.mp4")
async def get_video(current_user: str = Depends(get_current_user)):
    path = "templates/M-30India.mp4"
    if not os.path.exists(path):
        raise HTTPException(404, "Video not found")
    return FileResponse(path, media_type="video/mp4")

# --- SESSION ENDPOINTS ---
@app.post("/start_session")
async def start_session(request: SessionStartRequest, current_user: str = Depends(get_current_user)):
    start = time.time()
    
    try:
        # Generate backstory and persona
        backstory = await _generate_unique_backstory(
            request.diseases, request.age, request.gender, request.working_domain
        )
        persona = create_base_persona(
            request.diseases, request.age, request.ethnicity,
            request.working_domain, request.gender, backstory
        )
        
        # Get appropriate video filename with validation
        video_filename = get_video_filename(request.age, request.gender)
        
        # Debug: Print all relevant information
        print(f"Session Parameters - Age: {request.age}, Gender: {request.gender}")
        print(f"Generated video filename: {video_filename}")
        
        # Check if video file actually exists
        video_path = f"templates/{video_filename}"
        if not os.path.exists(video_path):
            print(f"WARNING: Video file not found: {video_path}")
            # Fallback to default video
            video_filename = "male-adult.mp4"
            print(f"Using fallback video: {video_filename}")
        
        print(f"Session ready: {time.time()-start:.2f}s | Video: {video_filename}")
        
        return {
            "system_prompt": persona,
            "video_filename": video_filename,
            "backstory": backstory  # Optional: for debugging
        }
        
    except Exception as e:
        print(f"Error in start_session: {str(e)}")
        # Provide fallback response
        return {
            "system_prompt": f"You are Sai, a {request.age}-year-old {request.gender} patient.",
            "video_filename": "male-adult.mp4",
            "error": str(e)
        }

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
    
    # Generate main report
    prompt = create_report_prompt(request.transcript)
    report = await _invoke_bedrock_claude(
        "Concise therapy evaluator.",
        [{"role": "user", "content": prompt}],
        MAX_TOKENS_REPORT
    )
    
    # NEW: Generate improvement suggestions for each therapist message (if chat_history provided)
    improvements = []
    
    if request.chat_history:
        chat_history = request.chat_history
        
        for i in range(len(chat_history)):
            msg = chat_history[i]
            if msg['role'] == 'user':  # Therapist message
                # Get patient's response (next message)
                patient_response = ""
                if i + 1 < len(chat_history) and chat_history[i + 1]['role'] == 'assistant':
                    patient_response = chat_history[i + 1]['content']
                
                # Get context (previous 2 messages)
                context = ""
                if i > 0:
                    context = " | ".join([f"{chat_history[j]['role']}: {chat_history[j]['content'][:100]}" 
                                         for j in range(max(0, i-2), i)])
                
                try:
                    improvement_prompt = create_improvement_prompt(
                        msg['content'], 
                        patient_response, 
                        context
                    )
                    
                    # Add retry logic with exponential backoff
                    max_retries = 3
                    retry_delay = 1
                    improvement = None
                    
                    for attempt in range(max_retries):
                        try:
                            improvement = await _invoke_bedrock_claude(
                                "Expert therapy supervisor providing constructive feedback.",
                                [{"role": "user", "content": improvement_prompt}],
                                MAX_TOKENS_IMPROVEMENT
                            )
                            break  # Success, exit retry loop
                        except HTTPException as http_err:
                            if http_err.status_code == 429 and attempt < max_retries - 1:
                                # Rate limit hit, wait and retry
                                print(f"Rate limit hit for message {i}, retrying in {retry_delay}s...")
                                await asyncio.sleep(retry_delay)
                                retry_delay *= 2  # Exponential backoff
                            else:
                                raise
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"Attempt {attempt + 1} failed for message {i}: {e}, retrying...")
                                await asyncio.sleep(retry_delay)
                                retry_delay *= 2
                            else:
                                raise
                    
                    if improvement:
                        # Parse the structured response
                        needs_improvement = "NEEDS_IMPROVEMENT" in improvement.upper()
                        
                        improvements.append({
                            "therapist_message": msg['content'],
                            "patient_response": patient_response,
                            "improvement": improvement,
                            "needs_improvement": needs_improvement
                        })
                    else:
                        raise Exception("Failed after all retries")
                        
                except Exception as e:
                    error_msg = str(e)
                    print(f"Failed to generate improvement for message {i} after retries: {error_msg}")
                    
                    # Provide a more helpful fallback message
                    fallback = "Unable to generate detailed analysis at this time. "
                    if "rate limit" in error_msg.lower() or "429" in error_msg:
                        fallback += "The AI service is currently busy. This response will be analyzed in the summary above."
                    elif "timeout" in error_msg.lower():
                        fallback += "The analysis request timed out. Your overall performance is covered in the main report."
                    else:
                        fallback += "Please refer to the overall evaluation above for guidance on this exchange."
                    
                    improvements.append({
                        "therapist_message": msg['content'],
                        "patient_response": patient_response,
                        "improvement": fallback,
                        "needs_improvement": False
                    })
                
                # Small delay between API calls to avoid rate limiting
                if i < len(chat_history) - 1:
                    await asyncio.sleep(0.5)
    
    print(f"Report + Improvements: {time.time()-start:.2f}s")
    return {
        "report": report,
        "improvements": improvements
    }

# --- ADMIN ENDPOINTS ---
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
                'password_hash': row['password_hash'][:10] + '...'
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
    return {"status": "UNLIMITED MEMORY + CHAT ANALYSIS + DYNAMIC VIDEOS", "model": MODEL_ID, "ready": True}