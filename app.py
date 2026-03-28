"""
Animitr — WhatsApp AI Animal Rescue Bot
PostgreSQL edition — v2 with full security fixes

FIXES IN THIS VERSION:
  P1  — Volunteer registration now requires admin approval (website → pending → APPROVE)
  P3  — COMPLETED only works for assigned volunteer (auth check)
  P4  — increment_rescues only fires if case was ACCEPTED before COMPLETED
  P5  — Ghost volunteer timeout: 45min HIGH, 90min others → re-escalate
  P6  — Max 1 active case per reporter at a time
  P7  — Rate limiting: 10 messages per minute per number
  P9  — Location validation now uses Groq to detect nonsense addresses
  P15 — Escalation timers recovered from DB on restart
  P17 — active_cases recovered from DB on restart (RESPONDING fallback)
  P19 — Case IDs now use secrets.token_hex → 16.7M possibilities/day
  NEW — VSTATUS keyword for volunteers to check their status
  NEW — Admin commands: APPROVE, REJECT, REMOVE_VOL, BLOCK, UNBLOCK,
        ADMIN_STATS, ADMIN_CASES, CLOSE_CASE
  NEW — JOIN keyword removed — registration only via website form
  NEW — volunteer_applications table for pending applicants
  NEW — blocked_numbers table

Author: Dhruv Rangan
"""

import os, secrets
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "0"
os.environ["MPLBACKEND"] = "Agg"
os.environ["DISPLAY"] = ""

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
from groq import Groq

import requests, PIL.Image, json, threading
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1008569229008784")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
DATABASE_URL    = os.getenv("DATABASE_URL")
ADMIN_NUMBER    = os.getenv("ADMIN_NUMBER", "")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")
groq_client  = Groq(api_key=GROQ_API_KEY)

waiting_reporters           = {}
active_cases                = {}
pending_volunteer_responses = {}
pending_outcome             = {}
message_timestamps          = {}


# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS volunteers (
            phone_number   TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            status         TEXT DEFAULT 'active',
            city           TEXT,
            tier           TEXT,
            total_rescues  INTEGER DEFAULT 0,
            registered_at  TIMESTAMP DEFAULT NOW()
        );""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS volunteer_applications (
            phone_number  TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            city          TEXT,
            tier          TEXT,
            applied_at    TIMESTAMP DEFAULT NOW(),
            status        TEXT DEFAULT 'pending'
        );""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blocked_numbers (
            phone_number TEXT PRIMARY KEY,
            reason       TEXT,
            blocked_at   TIMESTAMP DEFAULT NOW()
        );""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            case_id            TEXT PRIMARY KEY,
            reporter           TEXT NOT NULL,
            animal             TEXT,
            severity           TEXT,
            location           TEXT,
            bleeding           TEXT,
            can_move           TEXT,
            urgency            TEXT,
            status             TEXT DEFAULT 'PENDING',
            volunteer          TEXT,
            volunteer_number   TEXT,
            alerted_volunteers TEXT DEFAULT '[]',
            outcome            TEXT,
            completion_photo   BOOLEAN DEFAULT FALSE,
            time_reported      TEXT,
            time_accepted      TEXT,
            time_completed     TEXT
        );""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone_number TEXT PRIMARY KEY,
            stage        TEXT DEFAULT 'warning',
            session_data TEXT DEFAULT '{}',
            updated_at   TIMESTAMP DEFAULT NOW()
        );""")
    conn.commit(); cur.close(); conn.close()
    print("DB initialised.")
    cleanup_stale_sessions()
    recover_state_from_db()


# ── VOLUNTEER CRUD ────────────────────────────────────────────────

def load_volunteers():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM volunteers WHERE status='active';")
    rows = cur.fetchall(); cur.close(); conn.close()
    return {r["phone_number"]: dict(r) for r in rows}

def save_volunteer(phone, name, city=None, tier=None):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO volunteers (phone_number, name, city, tier, status)
        VALUES (%s,%s,%s,%s,'active')
        ON CONFLICT (phone_number) DO UPDATE SET
            name=EXCLUDED.name,
            city=COALESCE(EXCLUDED.city,volunteers.city),
            tier=COALESCE(EXCLUDED.tier,volunteers.tier),
            status='active';
    """, (phone, name, city, tier))
    conn.commit(); cur.close(); conn.close()

def increment_rescues(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE volunteers SET total_rescues=total_rescues+1 WHERE phone_number=%s;", (phone,))
    conn.commit(); cur.close(); conn.close()


# ── APPLICATIONS CRUD ─────────────────────────────────────────────

def save_application(phone, name, city=None, tier=None):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO volunteer_applications (phone_number,name,city,tier,status)
        VALUES (%s,%s,%s,%s,'pending')
        ON CONFLICT (phone_number) DO UPDATE SET
            name=EXCLUDED.name,
            city=COALESCE(EXCLUDED.city,volunteer_applications.city),
            tier=COALESCE(EXCLUDED.tier,volunteer_applications.tier),
            status='pending', applied_at=NOW();
    """, (phone, name, city, tier))
    conn.commit(); cur.close(); conn.close()

def get_application(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM volunteer_applications WHERE phone_number=%s;", (phone,))
    row = cur.fetchone(); cur.close(); conn.close()
    return dict(row) if row else None

def update_application_status(phone, status):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE volunteer_applications SET status=%s WHERE phone_number=%s;", (status, phone))
    conn.commit(); cur.close(); conn.close()

def get_volunteer_status(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT status FROM volunteers WHERE phone_number=%s;", (phone,))
    vol = cur.fetchone()
    if vol:
        cur.close(); conn.close()
        return "active" if vol["status"] == "active" else "inactive"
    cur.execute("SELECT status FROM volunteer_applications WHERE phone_number=%s;", (phone,))
    app = cur.fetchone(); cur.close(); conn.close()
    return app["status"] if app else "not_found"


# ── BLOCKED NUMBERS ───────────────────────────────────────────────

def is_blocked(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM blocked_numbers WHERE phone_number=%s;", (phone,))
    result = cur.fetchone() is not None; cur.close(); conn.close()
    return result

def block_number(phone, reason=""):
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO blocked_numbers (phone_number,reason) VALUES (%s,%s) ON CONFLICT (phone_number) DO UPDATE SET reason=EXCLUDED.reason;", (phone, reason))
    conn.commit(); cur.close(); conn.close()

def unblock_number(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM blocked_numbers WHERE phone_number=%s;", (phone,))
    conn.commit(); cur.close(); conn.close()


# ── CASE CRUD ─────────────────────────────────────────────────────

def load_case(case_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM cases WHERE case_id=%s;", (case_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return None
    d = dict(row)
    try: d["alerted_volunteers"] = json.loads(d.get("alerted_volunteers") or "[]")
    except: d["alerted_volunteers"] = []
    return d

def load_cases():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM cases;")
    rows = cur.fetchall(); cur.close(); conn.close()
    result = {}
    for row in rows:
        d = dict(row)
        try: d["alerted_volunteers"] = json.loads(d.get("alerted_volunteers") or "[]")
        except: d["alerted_volunteers"] = []
        result[d["case_id"]] = d
    return result

def save_case(c):
    alerted_json = json.dumps(c.get("alerted_volunteers", []))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO cases (
            case_id,reporter,animal,severity,location,bleeding,can_move,urgency,
            status,volunteer,volunteer_number,alerted_volunteers,outcome,
            completion_photo,time_reported,time_accepted,time_completed
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (case_id) DO UPDATE SET
            status=EXCLUDED.status, volunteer=EXCLUDED.volunteer,
            volunteer_number=EXCLUDED.volunteer_number,
            alerted_volunteers=EXCLUDED.alerted_volunteers,
            outcome=EXCLUDED.outcome, completion_photo=EXCLUDED.completion_photo,
            time_accepted=EXCLUDED.time_accepted, time_completed=EXCLUDED.time_completed;
    """, (
        c.get("case_id"), c.get("reporter"), c.get("animal"),
        str(c.get("severity","?")), c.get("location"), c.get("bleeding"),
        c.get("can_move"), c.get("urgency"), c.get("status","PENDING"),
        c.get("volunteer"), c.get("volunteer_number"), alerted_json,
        c.get("outcome"), c.get("completion_photo",False),
        c.get("time_reported"), c.get("time_accepted"), c.get("time_completed"),
    ))
    conn.commit(); cur.close(); conn.close()

def generate_case_id():
    # P19 FIX: 16.7M possibilities/day instead of 9000
    return f"CASE-{datetime.now().strftime('%d%m')}-{secrets.token_hex(3).upper()}"

def count_active_cases_for_reporter(reporter):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM cases WHERE reporter=%s AND status IN ('PENDING','ACCEPTED');", (reporter,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row["cnt"] if row else 0

def create_case(reporter, session, urgency):
    # P6 FIX: Max 1 active case per reporter
    if count_active_cases_for_reporter(reporter) >= 1:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT case_id FROM cases WHERE reporter=%s AND status IN ('PENDING','ACCEPTED') ORDER BY time_reported DESC LIMIT 1;", (reporter,))
        row = cur.fetchone(); cur.close(); conn.close()
        existing = row["case_id"] if row else "your existing case"
        send_message(reporter,
            f"⚠️ You already have an active rescue case: {existing}\n\n"
            "Reply STATUS to check it.\n"
            "Please wait for it to complete before reporting another animal."
        )
        return None
    case_id = generate_case_id()
    case = {
        "case_id": case_id, "reporter": reporter,
        "animal": session.get("animal","Unknown"), "severity": session.get("severity","?"),
        "location": session.get("location","Not shared"), "bleeding": session.get("bleeding","?"),
        "can_move": session.get("can_move","?"), "urgency": urgency,
        "status": "PENDING", "volunteer": None, "volunteer_number": None,
        "alerted_volunteers": [], "outcome": None, "completion_photo": False,
        "time_reported": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "time_accepted": None, "time_completed": None,
    }
    save_case(case)
    s = load_session(reporter); s["case_id"] = case_id; save_session(reporter, s)
    print(f"Case created: {case_id}")
    return case_id


# ── SESSION CRUD ──────────────────────────────────────────────────

def load_session(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT session_data FROM sessions WHERE phone_number=%s;", (phone,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return {}
    try: return json.loads(row["session_data"])
    except: return {}

def save_session(phone, data):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (phone_number,stage,session_data,updated_at)
        VALUES (%s,%s,%s,NOW())
        ON CONFLICT (phone_number) DO UPDATE SET
            stage=EXCLUDED.stage, session_data=EXCLUDED.session_data, updated_at=NOW();
    """, (phone, data.get("stage","warning"), json.dumps(data)))
    conn.commit(); cur.close(); conn.close()

def delete_session(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE phone_number=%s;", (phone,))
    conn.commit(); cur.close(); conn.close()

def session_exists(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM sessions WHERE phone_number=%s;", (phone,))
    exists = cur.fetchone() is not None; cur.close(); conn.close()
    return exists

def clear_reporter_session(sender):
    delete_session(sender)
    waiting_reporters.pop(sender, None)
    print(f"Session cleared: {sender}")

def cleanup_stale_sessions():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE updated_at < NOW() - INTERVAL '2 hours';")
        deleted = cur.rowcount; conn.commit(); cur.close(); conn.close()
        if deleted: print(f"Cleaned {deleted} stale sessions.")
    except Exception as e: print(f"Session cleanup error: {e}")

def schedule_session_cleanup():
    cleanup_stale_sessions()
    t = threading.Timer(21600, schedule_session_cleanup); t.daemon = True; t.start()


# ══════════════════════════════════════════════════════════════════
# RATE LIMITING  (P7 FIX)
# ══════════════════════════════════════════════════════════════════

def is_rate_limited(phone):
    now = datetime.now()
    timestamps = message_timestamps.get(phone, [])
    recent = [t for t in timestamps if (now - t).total_seconds() < 60]
    recent.append(now)
    message_timestamps[phone] = recent
    if len(recent) > 10:
        print(f"Rate limited: {phone}")
        return True
    return False


# ══════════════════════════════════════════════════════════════════
# STARTUP RECOVERY  (P15 + P17 FIX)
# ══════════════════════════════════════════════════════════════════

def recover_state_from_db():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT case_id, reporter, status, urgency, volunteer_number,
                   time_reported, time_accepted
            FROM cases WHERE status IN ('PENDING','ACCEPTED');
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        now = datetime.now()
        for row in rows:
            case_id = row["case_id"]; status = row["status"]
            urgency = row["urgency"] or "MEDIUM"
            reporter = row["reporter"]; vol_number = row["volunteer_number"]

            # P17: Rebuild active_cases so RESPONDING works post-restart
            if status == "ACCEPTED" and vol_number:
                active_cases[vol_number] = {"reporter": reporter, "case_id": case_id}
                print(f"Recovered active_cases: {vol_number} → {case_id}")

            # P15: Restart timers with remaining time
            try:
                if status == "PENDING" and row["time_reported"]:
                    reported_at = datetime.strptime(row["time_reported"], "%d %b %Y, %I:%M %p")
                    elapsed = (now - reported_at).total_seconds()
                    remaining = 600 - elapsed
                    if remaining > 0:
                        start_escalation_timer(case_id, int(remaining))
                    else:
                        threading.Thread(target=escalate_case, args=[case_id], daemon=True).start()
                elif status == "ACCEPTED" and row["time_accepted"]:
                    accepted_at = datetime.strptime(row["time_accepted"], "%d %b %Y, %I:%M %p")
                    elapsed = (now - accepted_at).total_seconds()
                    timeout = 2700 if urgency == "HIGH" else 5400
                    remaining = timeout - elapsed
                    if remaining > 0:
                        start_acceptance_timeout(case_id, vol_number or "unknown", urgency, int(remaining))
                    else:
                        threading.Thread(target=reopen_stale_case, args=[case_id], daemon=True).start()
            except Exception as e:
                print(f"Timer recovery error {case_id}: {e}")
        print(f"Recovery complete. {len(rows)} cases processed.")
    except Exception as e:
        print(f"State recovery failed: {e}")


# ══════════════════════════════════════════════════════════════════
# WHATSAPP HELPERS
# ══════════════════════════════════════════════════════════════════

def send_message(to, message):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp", "recipient_type": "individual",
        "to": to, "type": "text",
        "text": {"preview_url": False, "body": message[:4096]},
    }
    response = requests.post(url, headers=headers, json=data)
    print("SEND:", response.status_code)

def get_image_url(image_id):
    url = f"https://graph.facebook.com/v18.0/{image_id}"
    return requests.get(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}).json()["url"]

def download_image(image_url, save_path="received.jpg"):
    try:
        r = requests.get(image_url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, timeout=15)
        r.raise_for_status()
        with open(save_path, "wb") as f: f.write(r.content)
        print(f"Image saved: {save_path}"); return True
    except Exception as e:
        print(f"Image download error: {e}"); return False

def upload_and_send_photo(to, photo_path, caption=""):
    if not os.path.exists(photo_path): return
    upload_url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    with open(photo_path, "rb") as f:
        files = {"file": (photo_path, f, "image/jpeg"), "messaging_product": (None, "whatsapp"), "type": (None, "image/jpeg")}
        upload_r = requests.post(upload_url, headers=headers, files=files)
    media_id = upload_r.json().get("id")
    if not media_id: return
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    hdrs = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "image", "image": {"id": media_id, "caption": caption}}
    requests.post(url, headers=hdrs, json=data)
    print(f"Photo sent to {to}")

def send_photo_to_volunteer(to):
    upload_and_send_photo(to, "received.jpg", "📸 Photo reported by rescue reporter")


# ══════════════════════════════════════════════════════════════════
# ESCALATION + GHOST VOLUNTEER TIMEOUT  (P5 + P15 FIX)
# ══════════════════════════════════════════════════════════════════

def escalate_case(case_id):
    case = load_case(case_id)
    if not case or case["status"] in ["ACCEPTED","COMPLETED"]: return
    print(f"ESCALATING {case_id}")
    volunteers = load_volunteers()
    alerted = case.get("alerted_volunteers", [])
    remaining = [v for v in volunteers if v not in alerted]
    if remaining:
        for vol in remaining:
            send_message(vol,
                f"🚨 ESCALATION ALERT 🚨\nNo volunteer responded in 10 minutes!\n\n"
                f"Case ID: {case_id}\nAnimal: {case['animal']}\n"
                f"Severity: {case['severity']}/10\n📍 {case['location']}\n\n"
                f"Reporter: +{case['reporter']}\n\nReply RESPONDING immediately.\nWhen done: COMPLETED {case_id}"
            )
            active_cases[vol] = {"reporter": case["reporter"], "case_id": case_id}
            alerted.append(vol)
        case["alerted_volunteers"] = alerted; save_case(case)
        send_message(case["reporter"], f"⏰ Update on {case_id}:\nStill looking for a volunteer. Additional team alerted. Help is coming.")
    else:
        send_message(case["reporter"],
            f"⚠️ Update on {case_id}:\nAll volunteers alerted.\n\n📞 Animal Helpline: 1962\n📞 SPCA: 011-23619027\n\nPlease contact them directly while we keep trying."
        )

def reopen_stale_case(case_id):
    # P5 FIX: Ghost volunteer timeout — reopen to other volunteers
    case = load_case(case_id)
    if not case or case["status"] != "ACCEPTED": return
    stale_vol = case.get("volunteer", "The volunteer")
    stale_num = case.get("volunteer_number")
    print(f"Reopening stale case {case_id} — {stale_vol} ghosted")
    case["status"] = "PENDING"; case["volunteer"] = None
    case["volunteer_number"] = None; case["time_accepted"] = None
    save_case(case)
    if stale_num:
        active_cases.pop(stale_num, None); pending_outcome.pop(stale_num, None)
        send_message(stale_num,
            f"⚠️ Case {case_id} has been reassigned.\n\n"
            "You did not complete this rescue within the expected time.\n"
            "The case is being reopened to other volunteers."
        )
    send_message(case["reporter"],
        f"🔄 Update on {case_id}:\n\nOur volunteer has been unable to complete the rescue in time.\n"
        "We are alerting backup volunteers now. Help is still on the way."
    )
    volunteers = load_volunteers()
    for vol in volunteers:
        if vol == stale_num: continue
        send_message(vol,
            f"🔄 REACTIVATED CASE — {case_id}\n\nPrevious volunteer did not complete rescue.\n\n"
            f"Animal: {case['animal']}\nSeverity: {case['severity']}/10\n📍 {case['location']}\n\n"
            "Reply RESPONDING if you can help now."
        )
        active_cases[vol] = {"reporter": case["reporter"], "case_id": case_id}
    start_escalation_timer(case_id, 600)
    if ADMIN_NUMBER:
        send_message(ADMIN_NUMBER,
            f"⚠️ GHOST VOLUNTEER: Case {case_id} reopened.\n"
            f"Ghost: {stale_vol} (+{stale_num})\nAnimal: {case['animal']} at {case['location']}"
        )

def start_escalation_timer(case_id, delay_seconds=600):
    t = threading.Timer(delay_seconds, escalate_case, args=[case_id])
    t.daemon = True; t.start()
    print(f"Escalation timer: {case_id} in {delay_seconds}s")

def start_acceptance_timeout(case_id, volunteer_name, urgency, delay_seconds=None):
    if delay_seconds is None:
        delay_seconds = 2700 if urgency == "HIGH" else 5400
    t = threading.Timer(delay_seconds, reopen_stale_case, args=[case_id])
    t.daemon = True; t.start()
    print(f"Acceptance timeout: {case_id} ({volunteer_name}) in {delay_seconds}s")


# ══════════════════════════════════════════════════════════════════
# GROQ + LOCATION VALIDATION
# ══════════════════════════════════════════════════════════════════

def interpret_answer(question_type, user_answer):
    prompt = f"""You are interpreting a WhatsApp message from someone reporting an animal emergency.
Question type: {question_type}
User answer: "{user_answer}"
Return ONLY one value:
- yes_no → YES / NO / UNCLEAR
- animal → dog / cat / cow / horse / bird / other / UNCLEAR
- severity → number 1-10 / UNCLEAR
- text → cleaned answer / UNCLEAR
Rules: "haan","ji","yep" → YES | "nahi","nope" → NO | "bahut bura hai" → 8 | "minor" → 2
Return ONLY the value. Nothing else."""
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role":"user","content":prompt}],
        max_tokens=20, temperature=0
    )
    result = r.choices[0].message.content.strip()
    print(f"GROQ: '{user_answer}' → '{result}'")
    return result

def is_valid_location(text):
    # P9 FIX: Groq validates if location is real and navigable
    t = text.strip().lower()
    if len(t) < 10: return False
    invalid_words = ["here","nearby","near me","idk","don't know","dont know","not sure",
                     "somewhere","outside","there","this place","same place","abc","xyz","test","na","n/a"]
    if any(t == w or t.startswith(w + " ") for w in invalid_words): return False
    if len(set(t.replace(" ",""))) < 5: return False
    try:
        prompt = f"""Is this a real, navigable location a rescue volunteer could physically find?
Location: "{text}"
Answer YES if it contains a recognizable area, landmark, street, or city name.
Answer NO if it is vague, nonsensical, or not a real address.
Reply ONLY: YES or NO"""
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"user","content":prompt}],
            max_tokens=5, temperature=0
        )
        return "YES" in r.choices[0].message.content.strip().upper()
    except Exception as e:
        print(f"Location validation error: {e}")
        return len(t) >= 20


# ══════════════════════════════════════════════════════════════════
# QUESTION FLOW
# ══════════════════════════════════════════════════════════════════

ALL_QUESTION_MAP = {
    "animal":         ("animal",  "Which animal is it?\n\n1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other — please specify"),
    "bleeding":       ("yes_no",  "Is the animal bleeding?\n\nReply YES or NO"),
    "can_move":       ("yes_no",  "Can the animal move on its own?\n\nReply YES or NO"),
    "wounds":         ("yes_no",  "Are there any visible wounds or injuries?\n\nReply YES or NO"),
    "eating":         ("yes_no",  "Is the animal eating or drinking?\n\nReply YES or NO"),
    "duration":       ("text",    "How long has the animal been in this condition?\n\n(Example: 1 hour, since morning, not sure)"),
    "behavior":       ("text",    "Is the animal aggressive or calm?\n\n(Example: calm, scared, aggressive, unconscious)"),
    "ground_support": ("yes_no",  "Is there anyone with the animal right now?\n\nYES — please share their WhatsApp number\nNO — rescuer will be dispatched urgently"),
}

def get_next_question(session):
    severity = session.get("severity", 0); answered = session.get("answered", [])
    base = [
        ("animal","animal","Which animal is it?\n\n1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other — please specify"),
        ("bleeding","yes_no","Is the animal bleeding?\n\nReply YES or NO"),
        ("can_move","yes_no","Can the animal move on its own?\n\nReply YES or NO"),
    ]
    moderate = [
        ("wounds","yes_no","Are there any visible wounds or injuries?\n\nReply YES or NO"),
        ("eating","yes_no","Is the animal eating or drinking?\n\nReply YES or NO"),
        ("duration","text","How long has the animal been in this condition?\n\n(Example: 1 hour, since morning, not sure)"),
    ]
    mild_extra = moderate + [("behavior","text","Is the animal aggressive or calm?\n\n(Example: calm, scared, aggressive, unconscious)")]
    support = ("ground_support","yes_no","Is there anyone with the animal right now?\n\nYES — please share their WhatsApp number\nNO — our rescuer will be dispatched urgently")
    location = ("location","text",
        "Please share the exact location of the animal 📍\n\nOption 1 — Live location (recommended):\nTap the 📎 attachment icon\n→ Select Location\n→ Share Live Location\n\n"
        "Option 2 — Type address:\nInclude area name + landmark + city\n\nExample: Near Sector 5 Metro, Rohini, New Delhi\n\n⚠️ Accurate location = faster rescue.")
    if   severity >= 7: all_q = base + [support] + [location]
    elif severity >= 4: all_q = base + moderate   + [support] + [location]
    else:               all_q = base + mild_extra + [support] + [location]
    for key, qtype, question in all_q:
        if key not in answered: return key, qtype, question
    return "photo","text","Almost done! Please send a clear photo of the animal 📸\n\nTips:\n• Get as close as safely possible\n• Make sure the animal is clearly in frame\n• Good lighting helps our AI analyse the injury\n\nSend the photo now 👇"

def advance_to_next(sender, session):
    next_key, next_qtype, next_question = get_next_question(session)
    if next_key == "location": session["stage"] = "location"
    elif next_key == "photo": session["stage"] = "photo"
    else:
        session["pending_key"] = next_key; session["pending_qtype"] = next_qtype
        if next_key not in session.get("answered",[]): session["answered"].append(next_key)
    save_session(sender, session); send_message(sender, next_question)


# ══════════════════════════════════════════════════════════════════
# FIRST AID
# ══════════════════════════════════════════════════════════════════

def send_first_aid(sender, session):
    animal = session.get("animal","animal").lower()
    bleeding = session.get("bleeding","NO"); can_move = session.get("can_move","YES")
    severity = session.get("severity", 5)
    note = "⚠️ Serious case. Do not move the animal unless absolutely necessary.\n\n" if severity >= 7 else ""
    tips = []
    if bleeding == "YES": tips.append("🩸 Gentle pressure with clean cloth. Do not remove it.")
    if can_move == "NO":  tips.append("🚫 Do not lift or drag — can cause more injury.")
    if   animal == "dog":  tips += ["🐕 Keep people away.", "💧 Offer water only if conscious and calm."]
    elif animal == "cat":  tips += ["🐈 Very still and quiet near them.", "🧤 Loosely cover with cloth — reduces panic."]
    elif animal == "cow":  tips += ["🐄 Keep crowd away.", "☀️ Shade if in direct sun."]
    elif animal == "bird": tips += ["🐦 Loosely cover with cloth.", "🌡️ Keep warm — birds shock quickly."]
    else:                  tips += ["🐾 Stay calm, keep distance.", "👥 Ask bystanders to move away."]
    tips += ["📵 Low noise.", "🚫 No food or medicine without vet."]
    send_message(sender,
        "🐾 First Aid While You Wait:\n\n" + note + "\n".join(tips) +
        "\n\nVolunteer being alerted. You'll be notified when someone accepts.\n\n"
        "━━━━━━━━━━━━━━━\nCan you stay with the animal?\n\nReply STAY — I am waiting\nReply LEAVE — I have to leave"
    )


# ══════════════════════════════════════════════════════════════════
# VOLUNTEER FLOW
# ══════════════════════════════════════════════════════════════════

def handle_responding(sender, volunteer_name, case_data):
    reporter = case_data["reporter"]; case_id_found = case_data["case_id"]
    case = load_case(case_id_found)
    if not case:
        send_message(sender, "Case not found. Please check your rescue alert and try again."); return
    if case["status"] == "COMPLETED":
        send_message(sender, f"Case {case_id_found} has already been completed. Thank you! 🐾"); return
    if case["status"] == "ACCEPTED" and case["volunteer_number"] != sender:
        send_message(sender, f"Case {case_id_found} has already been accepted by another volunteer.\nThank you for responding — please wait for the next alert."); return
    case["status"] = "ACCEPTED"; case["volunteer"] = volunteer_name
    case["volunteer_number"] = sender; case["time_accepted"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    save_case(case)
    rp = waiting_reporters.get(reporter)
    rp_text = ("✅ Reporter IS waiting." if rp is True else "⚠️ Reporter has LEFT." if rp is False else "ℹ️ Reporter presence unknown.")
    send_message(sender,
        f"✅ Case accepted — you are now the assigned rescuer.\n\n"
        f"📋 {case_id_found}\n📍 {case['location']}\nAnimal: {case['animal']} | Severity: {case['severity']}/10\n"
        f"Reporter: +{reporter}\n{rp_text}\n\n📝 Send an outcome note anytime, then:\nCOMPLETED {case_id_found}"
    )
    send_message(reporter,
        f"🐾 A volunteer has accepted your rescue case!\n\nVolunteer: {volunteer_name}\nContact: +{sender}\n\n"
        "They are heading to the location now.\nYou will be notified when the rescue is complete."
    )
    pending_outcome[sender] = {"case_id": case_id_found, "note": None}
    active_cases.pop(sender, None); pending_volunteer_responses.pop(sender, None)
    # P5 FIX: Start ghost volunteer timeout
    start_acceptance_timeout(case_id_found, volunteer_name, case.get("urgency","MEDIUM"))

def handle_outcome_note(sender, text):
    data = pending_outcome.get(sender)
    if not data or not isinstance(data, dict): return False
    note = text.strip()[:500]  # P10 FIX: max 500 chars
    if len(note) < 5:
        send_message(sender, "Please write a proper outcome note (minimum 5 characters).\nExample: Taken to Blue Cross clinic. Stable."); return True
    data["note"] = note; pending_outcome[sender] = data
    send_message(sender, f"✅ Note saved.\nWhen done: COMPLETED {data['case_id']}"); return True

def connect_reporter_volunteer(reporter, volunteer_number, volunteer_name):
    cd = pending_volunteer_responses.get(volunteer_number, {})
    case_id_found = cd.get("case_id") if isinstance(cd, dict) else None
    if not case_id_found:
        for cid, c in load_cases().items():
            if c["reporter"] == reporter and c["status"] in ["PENDING","ACCEPTED"]:
                case_id_found = cid; break
    if case_id_found:
        case = load_case(case_id_found)
        if case:
            case.update({"status":"ACCEPTED","volunteer":volunteer_name,"volunteer_number":volunteer_number,
                         "time_accepted":datetime.now().strftime("%d %b %Y, %I:%M %p")})
            save_case(case)
    send_message(reporter, f"🙏 Thank you for staying.\n\nVolunteer {volunteer_name} is on the way.\nContact: +{volunteer_number}")
    send_message(volunteer_number, f"✅ Reporter is waiting.\n📋 {case_id_found}\nReporter: +{reporter}\n\nWhen done: COMPLETED {case_id_found}")
    clear_reporter_session(reporter)
    active_cases.pop(volunteer_number, None); pending_volunteer_responses.pop(volunteer_number, None)

def handle_status(sender, text):
    # P11 FIX: Only reporter or assigned volunteer gets full details
    parts = text.strip().upper().replace(" -","-").replace("- ","-").split()
    case_id = parts[1] if len(parts) >= 2 else None
    if not case_id:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT case_id FROM cases WHERE reporter=%s ORDER BY time_reported DESC LIMIT 1;", (sender,))
        row = cur.fetchone(); cur.close(); conn.close()
        if row: case_id = row["case_id"]
    if not case_id:
        send_message(sender, "No case found for your number.\nTo check a specific case: STATUS CASE-XXXX"); return
    case = load_case(case_id)
    if not case:
        send_message(sender, f"Case {case_id} not found."); return
    volunteers = load_volunteers()
    is_reporter = (sender == case["reporter"])
    is_assigned = (sender == case.get("volunteer_number"))
    is_volunteer = (sender in volunteers)
    if not is_reporter and not is_assigned:
        if is_volunteer:
            send_message(sender, f"📋 Case {case_id}\nStatus: {case['status']}\nAnimal: {case['animal']}\n\nFull details only available to the assigned volunteer.")
        else:
            send_message(sender, "❌ You can only check status of cases you reported.")
        return
    status_text = ("⏳ Waiting for volunteer" if case["status"]=="PENDING" else
                   f"🚑 Volunteer {case['volunteer']} is on the way" if case["status"]=="ACCEPTED" else "✅ Rescue completed")
    msg = (f"📋 CASE STATUS\n\nCase ID: {case_id}\nAnimal: {case['animal']}\nLocation: {case['location']}\n"
           f"Severity: {case['severity']}/10\nReported: {case['time_reported']}\n\nStatus: {status_text}\n")
    if case.get("time_accepted"):  msg += f"Accepted: {case['time_accepted']}\n"
    if case.get("time_completed"): msg += f"Completed: {case['time_completed']}\n"
    if case.get("outcome"):        msg += f"\n📝 Outcome: \"{case['outcome']}\""
    send_message(sender, msg)

def handle_completed(sender, text):
    # P3 + P4 FIX: Auth check + only count if properly ACCEPTED→COMPLETED
    parts = text.strip().upper().replace(" -","-").replace("- ","-").split()
    if len(parts) < 2:
        send_message(sender, "Include Case ID.\nExample: COMPLETED CASE-XXXX"); return
    case_id = parts[1]; case = load_case(case_id)
    if not case:
        send_message(sender, f"Case {case_id} not found."); return
    if case["status"] == "COMPLETED":
        send_message(sender, f"Case {case_id} is already completed. 🐾"); return
    is_assigned = (case.get("volunteer_number") == sender)
    is_reporter  = (case.get("reporter") == sender)
    if not is_assigned and not is_reporter:
        send_message(sender, "❌ You are not authorized to complete this case.\nOnly the assigned volunteer can mark a rescue complete."); return
    if is_reporter and not is_assigned:
        if case["status"] == "ACCEPTED":
            try:
                accepted_at = datetime.strptime(case["time_accepted"], "%d %b %Y, %I:%M %p")
                hours_elapsed = (datetime.now() - accepted_at).total_seconds() / 3600
            except: hours_elapsed = 0
            if hours_elapsed < 3:
                send_message(sender, "⏳ A volunteer is still assigned to your case.\nPlease wait for them to complete the rescue."); return
    note_data = pending_outcome.get(sender, {})
    note = note_data.get("note") if isinstance(note_data, dict) else None
    was_accepted = (case["status"] == "ACCEPTED")
    case["status"] = "COMPLETED"; case["time_completed"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    if note: case["outcome"] = note
    save_case(case); pending_outcome.pop(sender, None)
    # P4 FIX: Only increment if properly ACCEPTED→COMPLETED by assigned volunteer
    if is_assigned and was_accepted: increment_rescues(sender)
    send_message(sender,
        f"✅ Case {case_id} marked as completed.\n\nThank you for showing up today. 🐾\n\n"
        "Every rescue you complete is logged on the Animitr leaderboard.\nYou made a real difference to an animal that had no voice. 💚"
    )
    reporter = case["reporter"]
    reporter_msg = f"🐾 Your rescue case is complete.\n\nCase ID: {case_id}\n"
    if note: reporter_msg += f"Volunteer note: \"{note}\"\n\n"
    reporter_msg += "Thank you for reporting and helping save an animal. 💚"
    send_message(reporter, reporter_msg); clear_reporter_session(reporter)
    print(f"Case {case_id} completed by {sender}")


# ══════════════════════════════════════════════════════════════════
# GEMINI
# ══════════════════════════════════════════════════════════════════

def analyze_with_gemini(image_path, user_answers):
    try:
        img = PIL.Image.open(image_path)
        prompt = (
            "You are an animal rescue triage assistant.\n\n"
            f"Reporter info:\n{user_answers}\n\n"
            "From the image:\n1. Animal seen?\n2. Matches description?\n3. Severity 1-10?\n"
            "4. Signs of distress?\n5. Urgency: HIGH / MEDIUM / LOW?\n\nBe concise."
        )
        response = gemini_model.generate_content([prompt, img])
        print("GEMINI:", response.text[:120]); return response.text
    except Exception as e:
        print(f"Gemini error: {e}"); return "AI analysis unavailable. Urgency: HIGH"

def extract_urgency(text):
    t = text.upper()
    if "HIGH" in t: return "HIGH"
    if "MEDIUM" in t: return "MEDIUM"
    return "LOW"

def alert_volunteers(sender, session, urgency, gemini_analysis, case_id):
    volunteers = load_volunteers()
    if not volunteers:
        send_message(sender, "⚠️ No volunteers registered yet.\n\n📞 Animal Helpline: 1962\n📞 SPCA: 011-23619027"); return
    line = ("🔴 URGENT — IMMEDIATE RESPONSE" if urgency=="HIGH" else "🟡 MEDIUM — RESPOND SOON" if urgency=="MEDIUM" else "🟢 LOW — MONITOR SITUATION")
    message = (
        f"🚨 RESCUE ALERT 🚨\n{line}\n\n📋 Case ID: {case_id}\n\n"
        f"Animal: {session.get('animal','?')}\nSeverity: {session.get('severity','?')}/10\n"
        f"Bleeding: {session.get('bleeding','?')}\nCan move: {session.get('can_move','?')}\n"
        f"Ground support: {session.get('ground_support','?')}\n📍 Location: {session.get('location','?')}\n\n"
        f"AI Analysis:\n{gemini_analysis}\n\nReported by: +{sender}\n\n"
        f"Reply RESPONDING to accept.\nWhen done: COMPLETED {case_id}"
    )
    alerted = []
    for vol in volunteers:
        send_message(vol, message); send_photo_to_volunteer(vol)
        active_cases[vol] = {"reporter": sender, "case_id": case_id}; alerted.append(vol); print(f"Alerted: {vol}")
    case = load_case(case_id)
    if case: case["alerted_volunteers"] = alerted; save_case(case)
    start_escalation_timer(case_id)
    if ADMIN_NUMBER:
        send_message(ADMIN_NUMBER,
            f"📋 NEW CASE: {case_id}\nAnimal: {session.get('animal','?')} | {urgency}\n"
            f"Location: {session.get('location','?')}\nVolunteers alerted: {len(alerted)}"
        )


# ══════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════

def handle_admin_command(text):
    parts = text.strip().split(); cmd = parts[0].upper()
    target = parts[1] if len(parts) > 1 else None
    reason = " ".join(parts[2:]) if len(parts) > 2 else ""

    if cmd == "APPROVE" and target:
        app = get_application(target)
        if not app: return f"No application found for {target}"
        save_volunteer(target, app["name"], app["city"], app["tier"])
        update_application_status(target, "approved")
        send_message(target,
            f"✅ Welcome to Animitr, {app['name']}!\n\nYour application has been approved.\n\n"
            "You will now receive rescue alerts.\n\nCommands:\n"
            "RESPONDING — accept a case\nCOMPLETED CASE-XXXX — close a case\nVSTATUS — check your status\n\n"
            "Thank you for joining. You are going to save lives. 💚"
        )
        return f"✅ Approved: {app['name']} (+{target})"

    elif cmd == "REJECT" and target:
        app = get_application(target)
        if not app: return f"No application found for {target}"
        update_application_status(target, "rejected")
        send_message(target,
            "Thank you for applying to Animitr.\n\nAfter review, we are unable to approve your application at this time.\n\n"
            "Questions? contact.animitr@gmail.com"
        )
        return f"❌ Rejected: {app['name'] if app else ''} (+{target})"

    elif cmd == "REMOVE_VOL" and target:
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE volunteers SET status='inactive' WHERE phone_number=%s;", (target,))
        conn.commit(); cur.close(); conn.close()
        active_cases.pop(target, None); pending_outcome.pop(target, None)
        send_message(target, "Your Animitr volunteer account has been deactivated.\nContact contact.animitr@gmail.com if you have questions.")
        return f"🚫 Removed volunteer: +{target}"

    elif cmd == "BLOCK" and target:
        block_number(target, reason)
        return f"🔒 Blocked: +{target} — {reason or 'no reason'}"

    elif cmd == "UNBLOCK" and target:
        unblock_number(target)
        return f"🔓 Unblocked: +{target}"

    elif cmd == "CLOSE_CASE" and target:
        case = load_case(target)
        if not case: return f"Case {target} not found"
        case["status"] = "COMPLETED"; case["time_completed"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        case["outcome"] = "Force-closed by admin"; save_case(case)
        send_message(case["reporter"], f"📋 Your case {target} has been closed by the admin.\nIf you still need help, please submit a new report.")
        return f"🔒 Force-closed: {target}"

    elif cmd == "ADMIN_STATS":
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS t FROM cases;"); total = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) AS t FROM cases WHERE status='COMPLETED';"); done = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) AS t FROM cases WHERE status IN ('PENDING','ACCEPTED');"); active = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) AS t FROM volunteers WHERE status='active';"); vols = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) AS t FROM volunteer_applications WHERE status='pending';"); pending = cur.fetchone()["t"]
        cur.close(); conn.close()
        return (f"📊 ANIMITR STATS\n\nTotal cases: {total}\nCompleted: {done}\nActive: {active}\n"
                f"Active volunteers: {vols}\nPending applications: {pending}\n"
                f"Completion rate: {round(done/total*100,1) if total else 0}%")

    elif cmd == "ADMIN_CASES":
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT case_id,animal,urgency,status FROM cases WHERE status IN ('PENDING','ACCEPTED') ORDER BY time_reported DESC LIMIT 10;")
        rows = cur.fetchall(); cur.close(); conn.close()
        if not rows: return "No active cases right now."
        lines = ["📋 ACTIVE CASES\n"]
        for r in rows: lines.append(f"{r['case_id']} | {r['animal']} | {r['urgency']} | {r['status']}")
        return "\n".join(lines)

    else:
        return ("Admin commands:\nAPPROVE 91XXXXXXXXXX\nREJECT 91XXXXXXXXXX\nREMOVE_VOL 91XXXXXXXXXX\n"
                "BLOCK 91XXXXXXXXXX reason\nUNBLOCK 91XXXXXXXXXX\nCLOSE_CASE CASE-XXXX\nADMIN_STATS\nADMIN_CASES")


# ══════════════════════════════════════════════════════════════════
# PROCESS ANSWER
# ══════════════════════════════════════════════════════════════════

def process_answer(sender, text):
    session = load_session(sender); stage = session.get("stage","warning")

    if stage == "warning":
        if interpret_answer("yes_no", text) == "YES":
            session["stage"] = "severity"; save_session(sender, session)
            send_message(sender,
                "On a scale of 1 to 10, how serious is the animal's condition?\n\n"
                "1 = Minor injury, alert but moving\n5 = Moderate, needs attention soon\n10 = Critical, life threatening\n\n"
                "Please reply with a number between 1 and 10."
            )
        else:
            send_message(sender, "🚨 ANIMAL RESCUE SYSTEM 🚨\n\nYour number is registered.\nFalse reports = legal action.\n\nGenuine emergency only. Reply YES to proceed.")

    elif stage == "severity":
        interpreted = interpret_answer("severity", text)
        try:
            severity = int(interpreted)
            if 1 <= severity <= 10:
                session.update({"severity":severity,"stage":"questions","answered":[],"unclear_count":0})
                save_session(sender, session)
                level = "CRITICAL" if severity>=7 else "MODERATE" if severity>=4 else "MILD"
                send_message(sender, f"Severity {severity}/10 — {level}\n\nWhich animal is it?\n\n1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other — please specify")
                session["answered"].append("animal"); session["pending_key"] = "animal"; session["pending_qtype"] = "animal"
                save_session(sender, session)
            else: send_message(sender, "Please enter a number between 1 and 10.")
        except: send_message(sender, "Please enter a number between 1 and 10.")

    elif stage == "questions":
        pending_key = session.get("pending_key"); pending_qtype = session.get("pending_qtype","text")
        if pending_key:
            interpreted = interpret_answer(pending_qtype, text)
            if interpreted == "UNCLEAR":
                unclear = session.get("unclear_count",0) + 1; session["unclear_count"] = unclear
                if unclear >= 3 and session.get("severity",5) >= 4:
                    session[pending_key] = "Not provided"; session["pending_key"] = None; session["unclear_count"] = 0
                    save_session(sender, session); advance_to_next(sender, session)
                else:
                    save_session(sender, session)
                    _, q = ALL_QUESTION_MAP.get(pending_key, ("text","Could you clarify?"))
                    send_message(sender, f"Sorry, I didn't understand that.\n\nCould you clarify?\n\n{q}")
                return
            session["unclear_count"] = 0
            if pending_key == "animal":
                am = {"1":"Dog","2":"Cat","3":"Cow","4":"Horse","5":"Other","dog":"Dog","cat":"Cat","cow":"Cow","horse":"Horse","bird":"Bird","other":"Other"}
                session["animal"] = am.get(interpreted.lower(), interpreted.capitalize())
            elif pending_key == "ground_support":
                if interpreted == "YES":
                    session["ground_support"] = "YES"
                    num = None
                    for p in text.strip().split():
                        c = p.replace("+","").replace(" ","")
                        if c.isdigit() and len(c) >= 10: num = c; break
                    if num:
                        session["support_number"] = num; session["answered"].append("ground_support")
                        session["pending_key"] = None; session["unclear_count"] = 0; save_session(sender, session)
                    else:
                        session["stage"] = "support_number"; save_session(sender, session)
                        send_message(sender, "Please share their WhatsApp number:"); return
                else: session["ground_support"] = "NO"
            else: session[pending_key] = interpreted
            session["pending_key"] = None; session["unclear_count"] = 0; save_session(sender, session)
        advance_to_next(sender, session)

    elif stage == "support_number":
        session["support_number"] = text.strip(); session["stage"] = "questions"
        session["answered"].append("ground_support"); save_session(sender, session)
        advance_to_next(sender, session)

    elif stage == "location":
        if not is_valid_location(text):
            send_message(sender,
                "⚠️ Location not accepted.\n\nPlease provide a proper address:\n• Area or colony name\n• Nearby landmark\n• City name\n\n"
                "Example: Near Sector 5 Metro, Rohini, New Delhi\n\nOr share your live location using the 📎 button.\n\nAccurate location = faster rescue:"
            ); return
        session["location"] = text.strip(); session["stage"] = "photo"; save_session(sender, session)
        send_message(sender, "📍 Location confirmed!\n\nNow send a clear photo of the animal.\n\nTips:\n• Get as close as safely possible\n• Make sure animal is clearly visible\n• Good lighting helps AI analysis\n\nSend photo now 📸")

    elif stage == "waiting":
        interpreted = interpret_answer("yes_no", text)
        if text.upper() == "STAY" or interpreted == "YES":
            waiting_reporters[sender] = True
            vol_w = vol_name_w = None
            for vn, cd in list(pending_volunteer_responses.items()):
                if isinstance(cd,dict) and cd.get("reporter") == sender:
                    vol_w = vn; vols = load_volunteers(); vol_name_w = vols.get(vn,{}).get("name","Volunteer"); break
            if vol_w: connect_reporter_volunteer(sender, vol_w, vol_name_w)
            else: send_message(sender, "🙏 Thank you for staying.\nYou will be notified the moment a volunteer accepts.")
            clear_reporter_session(sender)
        elif text.upper() == "LEAVE" or interpreted == "NO":
            waiting_reporters[sender] = False
            send_message(sender, "Understood. Thank you for reporting. Help is on the way. 🐾\n\nYou will receive an update when a volunteer reaches the animal.")
            for vn, cd in list(pending_volunteer_responses.items()):
                if isinstance(cd,dict) and cd.get("reporter") == sender:
                    send_message(vn, f"⚠️ Reporter has left the location.\nPlease proceed urgently.\nReporter: +{sender}")
                    pending_volunteer_responses.pop(vn,None); active_cases.pop(vn,None); break
            clear_reporter_session(sender)
        else: send_message(sender, "Please reply STAY (waiting with animal) or LEAVE (leaving).")

    else:
        save_session(sender, {"stage":"warning"})
        send_message(sender, "🚨 ANIMAL RESCUE SYSTEM 🚨\n\nYour number is registered.\nGenuine emergency only. Reply YES to proceed.")


# ══════════════════════════════════════════════════════════════════
# WEBHOOK
# ══════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]
        if "messages" not in value: return "OK", 200
        message = value["messages"][0]
        sender  = message["from"]

        if is_blocked(sender): return "OK", 200
        if is_rate_limited(sender): return "OK", 200

        if message["type"] == "text":
            text = message["text"]["body"].strip()
            text_up = text.upper()

            # Admin commands
            if sender == ADMIN_NUMBER:
                admin_cmds = ["APPROVE","REJECT","REMOVE_VOL","BLOCK","UNBLOCK","CLOSE_CASE","ADMIN_STATS","ADMIN_CASES"]
                if any(text_up.startswith(cmd) for cmd in admin_cmds):
                    send_message(sender, handle_admin_command(text)); return "OK", 200

            # VSTATUS
            if text_up == "VSTATUS":
                vstatus = get_volunteer_status(sender)
                msgs = {
                    "active":   "✅ You are an active Animitr volunteer.\n\nYou will receive rescue alerts.\n\nCommands:\nRESPONDING — accept a case\nCOMPLETED CASE-XXXX — close a case",
                    "pending":  "⏳ Your application is under review.\n\nWe will contact you for a verification call.\nThis usually takes 1-3 days.\n\nQuestions? contact.animitr@gmail.com",
                    "rejected": "Your volunteer application was not approved.\n\nContact: contact.animitr@gmail.com",
                    "inactive": "Your volunteer account is inactive.\n\nContact: contact.animitr@gmail.com to reactivate.",
                    "not_found":"You are not registered as a volunteer.\n\nTo apply, visit:\nanimitr.netlify.app → Volunteer page\n\nText VSTATUS after applying to check your status.",
                }
                send_message(sender, msgs.get(vstatus, msgs["not_found"])); return "OK", 200

            if text_up.startswith("STATUS"):
                handle_status(sender, text); return "OK", 200

            if text_up.startswith("COMPLETED"):
                handle_completed(sender, text); return "OK", 200

            if sender in pending_outcome:
                if not text_up.startswith("COMPLETED") and not text_up.startswith("STATUS"):
                    if handle_outcome_note(sender, text): return "OK", 200

            if text_up == "RESPONDING":
                vols = load_volunteers()
                if sender not in vols:
                    send_message(sender, "You are not a registered volunteer.\n\nTo apply, visit:\nanimitr.netlify.app → Volunteer page"); return "OK", 200
                cd = active_cases.get(sender)
                if not cd:
                    # P17 FIX: DB fallback after restart
                    conn = get_db(); cur = conn.cursor()
                    cur.execute("SELECT case_id, reporter FROM cases WHERE status='PENDING' AND alerted_volunteers LIKE %s ORDER BY time_reported DESC LIMIT 1;", (f'%{sender}%',))
                    row = cur.fetchone(); cur.close(); conn.close()
                    if row: cd = {"reporter": row["reporter"], "case_id": row["case_id"]}; active_cases[sender] = cd
                if cd and isinstance(cd, dict): handle_responding(sender, vols[sender]["name"], cd)
                else: send_message(sender, "No active rescue cases found right now.\n\nYou will receive an alert when an animal needs help.")
                return "OK", 200

            # JOIN redirected to website
            if text_up == "JOIN":
                send_message(sender,
                    "🐾 Thank you for your interest in volunteering!\n\n"
                    "Volunteer registration is done through our website.\n\n"
                    "Visit: animitr.netlify.app → Volunteer page\n\n"
                    "Fill in the form. We will contact you for a verification call before approving you.\n\n"
                    "Text VSTATUS anytime to check your application status."
                ); return "OK", 200

            if not session_exists(sender):
                save_session(sender, {"stage":"warning"})
                send_message(sender, "🚨 ANIMAL RESCUE SYSTEM 🚨\n\nYour number is registered.\nFalse reports result in legal action.\n\nGenuine emergency only. Reply YES to proceed.")
            else:
                process_answer(sender, text)

        elif message["type"] == "location":
            session = load_session(sender)
            if session.get("stage") != "location":
                send_message(sender, "Please complete the questions first."); return "OK", 200
            lat = message["location"]["latitude"]; lng = message["location"]["longitude"]
            session["location"] = f"https://maps.google.com/?q={lat},{lng}"
            session["stage"] = "photo"; save_session(sender, session)
            send_message(sender, "📍 Live location received!\n\nNow send a clear photo of the animal 📸")

        elif message["type"] == "image":
            session = load_session(sender); vols = load_volunteers()
            if sender in vols and sender in pending_outcome:
                od = pending_outcome.get(sender, {}); cid = od.get("case_id") if isinstance(od,dict) else None
                if cid:
                    path = f"completion_{cid}.jpg"
                    download_image(get_image_url(message["image"]["id"]), path)
                    case = load_case(cid)
                    if case:
                        upload_and_send_photo(case["reporter"], path, "📸 Photo from volunteer after rescue")
                        case["completion_photo"] = True; save_case(case)
                        send_message(sender, "✅ Photo shared with the reporter.")
                    return "OK", 200
            if sender in vols and session.get("stage") != "photo": return "OK", 200
            if session.get("stage") == "location":
                send_message(sender, "Please share your location first 📍"); return "OK", 200
            if session.get("stage") != "photo":
                send_message(sender, "Please answer all questions first before sending a photo."); return "OK", 200
            send_message(sender, "📸 Photo received. Analysing with AI...")
            ok = download_image(get_image_url(message["image"]["id"]))
            if not ok:
                send_message(sender, "⚠️ Could not download your photo.\n\nPlease try sending it again."); return "OK", 200
            user_answers = (
                f"Animal: {session.get('animal','?')}\nSeverity: {session.get('severity','?')}/10\n"
                f"Bleeding: {session.get('bleeding','?')}\nCan move: {session.get('can_move','?')}\n"
                f"Wounds: {session.get('wounds','?')}\nEating: {session.get('eating','?')}\n"
                f"Duration: {session.get('duration','?')}\nBehavior: {session.get('behavior','?')}\n"
                f"Ground support: {session.get('ground_support','?')}"
            )
            gemini_analysis = analyze_with_gemini("received.jpg", user_answers)
            urgency = extract_urgency(gemini_analysis)
            case_id = create_case(sender, session, urgency)
            if not case_id: return "OK", 200  # P6: already has active case
            send_message(sender, f"📋 Your Case ID: {case_id}\n\nSave this — check status anytime:\nSTATUS {case_id}")
            if urgency == "HIGH":
                send_message(sender, "🚨 HIGH URGENCY case created.\n\nDispatched to rescue team immediately.\nPlease stay with the animal if safe to do so.")
            elif urgency == "MEDIUM":
                send_message(sender, "✅ Report dispatched to rescue team.\n\nA volunteer will respond soon.\nPlease stay close to the animal if possible.")
            else:
                send_message(sender, "✅ Report sent to rescue team.\n\nA volunteer will check on the animal.\nThank you for reporting.")
            session["stage"] = "waiting"; save_session(sender, session)
            send_first_aid(sender, session)
            alert_volunteers(sender, session, urgency, gemini_analysis, case_id)

    except Exception as e:
        print("Webhook error:", e)
    return "OK", 200


@app.route("/webhook", methods=["GET"])
def verify():
    mode, token, challenge = (request.args.get("hub.mode"), request.args.get("hub.verify_token"), request.args.get("hub.challenge"))
    if mode == "subscribe" and token == "12345":
        print("Webhook verified!"); return challenge, 200
    return "Verification failed", 403


# ══════════════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/api/stats", methods=["GET"])
def api_stats():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM cases;"); total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) AS done FROM cases WHERE status='COMPLETED';"); done = cur.fetchone()["done"]
        cur.execute("SELECT COUNT(*) AS live FROM cases WHERE status IN ('PENDING','ACCEPTED');"); live = cur.fetchone()["live"]
        cur.execute("SELECT COUNT(*) AS vols FROM volunteers WHERE status='active';"); vols = cur.fetchone()["vols"]
        cur.close(); conn.close()
        return jsonify({"total_cases":total,"completed_cases":done,"active_cases":live,"total_volunteers":vols,"completion_rate":round(done/total*100,1) if total else 0})
    except Exception as e:
        print("API /stats error:", e); return jsonify({"error":"Could not fetch stats"}), 500

@app.route("/api/cases", methods=["GET"])
def api_cases():
    try:
        phone = request.args.get("phone","").strip().replace("+","").replace(" ","")
        conn = get_db(); cur = conn.cursor()
        if phone:
            cur.execute("SELECT case_id,reporter,animal,severity,location,urgency,status,volunteer,outcome,time_reported,time_accepted,time_completed FROM cases WHERE reporter=%s ORDER BY time_reported DESC LIMIT 20;", (phone,))
        else:
            cur.execute("SELECT case_id,reporter,animal,severity,location,urgency,status,volunteer,outcome,time_reported,time_accepted,time_completed FROM cases ORDER BY time_reported DESC LIMIT 30;")
        rows = cur.fetchall(); cur.close(); conn.close()
        cases = []
        for row in rows:
            d = dict(row); r = d.get("reporter","")
            d["reporter"] = f"+91 XXXXXX{r[-4:]}" if len(r)>=4 else "Unknown"
            cases.append(d)
        return jsonify({"cases":cases,"count":len(cases)})
    except Exception as e:
        print("API /cases error:", e); return jsonify({"error":"Could not fetch cases"}), 500

@app.route("/api/case/<case_id>", methods=["GET"])
def api_case(case_id):
    try:
        case = load_case(case_id)
        if not case: return jsonify({"error":"Case not found"}), 404
        r = case.get("reporter","")
        case["reporter"] = f"+91 XXXXXX{r[-4:]}" if len(r)>=4 else "Unknown"
        case.pop("alerted_volunteers",None)
        return jsonify(case)
    except Exception as e:
        print("API /case error:", e); return jsonify({"error":"Could not fetch case"}), 500

@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT name,city,tier,total_rescues,status FROM volunteers WHERE status='active' ORDER BY total_rescues DESC LIMIT 20;")
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify({"leaderboard":[dict(r) for r in rows]})
    except Exception as e:
        print("API /leaderboard error:", e); return jsonify({"error":"Could not fetch leaderboard"}), 500

@app.route("/api/register-volunteer", methods=["POST"])
def api_register_volunteer():
    """P1 FIX: Goes to volunteer_applications (pending), not volunteers table."""
    try:
        data  = request.get_json()
        name  = data.get("name","").strip()
        phone = data.get("phone","").strip().replace("+","").replace(" ","")
        city  = data.get("city","").strip()
        tier  = data.get("tier","community").strip()
        if not name or not phone: return jsonify({"error":"Name and phone required"}), 400
        if len(phone) < 10: return jsonify({"error":"Invalid phone number"}), 400
        existing = get_volunteer_status(phone)
        if existing == "active": return jsonify({"error":"Already a registered volunteer. Text VSTATUS to check your status."}), 400
        if existing == "pending": return jsonify({"error":"Your application is already pending review. Text VSTATUS to check."}), 400
        save_application(phone, name, city, tier)
        send_message(phone,
            f"🐾 Thank you for applying to Animitr, {name}!\n\n"
            "Your application has been received.\n\n"
            "Next steps:\n→ Our team will review your application\n→ We will contact you for a verification call\n→ Once verified, you will be approved\n\n"
            "This usually takes 1-3 days.\n\nText VSTATUS anytime to check your status.\n\nQuestions? contact.animitr@gmail.com"
        )
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"🆕 NEW VOLUNTEER APPLICATION\n\nName: {name}\nPhone: +{phone}\nCity: {city}\nTier: {tier}\n\n"
                f"After your KYC call:\nAPPROVE {phone}\nREJECT {phone}"
            )
        return jsonify({"success":True,"message":"Application received. We will contact you for verification."})
    except Exception as e:
        print("API /register-volunteer error:", e); return jsonify({"error":"Registration failed"}), 500


# ══════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    schedule_session_cleanup()
    app.run(port=5000, debug=False)
