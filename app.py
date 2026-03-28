"""
Animitr — WhatsApp AI Animal Rescue Bot
PostgreSQL edition — replaces all JSON file operations
Sessions stored in DB — no more in-memory loss on restart
API routes added for website integration
Author: Dhruv Rangan
"""

import os
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "0"
os.environ["MPLBACKEND"] = "Agg"
os.environ["DISPLAY"] = ""

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
from groq import Groq

import requests
import PIL.Image
import json
import random
import threading
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

# ── CREDENTIALS ──────────────────────────────────────────
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1008569229008784")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
DATABASE_URL    = os.getenv("DATABASE_URL")   # Add this in Railway → Variables

# ── AI MODELS ─────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")
groq_client  = Groq(api_key=GROQ_API_KEY)

# ── IN-MEMORY (fast-moving dispatch state only) ────────────
# These reset on restart but that is acceptable —
# they only track the active 10-minute dispatch window
waiting_reporters           = {}
active_cases                = {}
pending_volunteer_responses = {}
pending_outcome             = {}


# ════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ════════════════════════════════════════════════════════════

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Create tables on startup. Safe to run repeatedly."""
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS volunteers (
            phone_number   TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            status         TEXT DEFAULT 'active',
            city           TEXT,
            tier           TEXT,
            total_rescues  INTEGER DEFAULT 0,
            registered_at  TIMESTAMP DEFAULT NOW()
        );
    """)

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
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone_number TEXT PRIMARY KEY,
            stage        TEXT DEFAULT 'warning',
            session_data TEXT DEFAULT '{}',
            updated_at   TIMESTAMP DEFAULT NOW()
        );
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("DB initialised.")


# ── VOLUNTEER CRUD ────────────────────────────────────────

def load_volunteers():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM volunteers;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row["phone_number"]: dict(row) for row in rows}


def save_volunteer(phone, name, city=None, tier=None):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO volunteers (phone_number, name, city, tier)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (phone_number) DO UPDATE SET
            name   = EXCLUDED.name,
            city   = COALESCE(EXCLUDED.city,   volunteers.city),
            tier   = COALESCE(EXCLUDED.tier,   volunteers.tier),
            status = 'active';
    """, (phone, name, city, tier))
    conn.commit()
    cur.close()
    conn.close()


def increment_rescues(phone):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE volunteers SET total_rescues = total_rescues + 1 WHERE phone_number = %s;",
        (phone,)
    )
    conn.commit()
    cur.close()
    conn.close()


# ── CASE CRUD ─────────────────────────────────────────────

def load_case(case_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM cases WHERE case_id = %s;", (case_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["alerted_volunteers"] = json.loads(d.get("alerted_volunteers") or "[]")
    except:
        d["alerted_volunteers"] = []
    return d


def load_cases():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM cases;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = {}
    for row in rows:
        d = dict(row)
        try:
            d["alerted_volunteers"] = json.loads(d.get("alerted_volunteers") or "[]")
        except:
            d["alerted_volunteers"] = []
        result[d["case_id"]] = d
    return result


def save_case(c):
    alerted_json = json.dumps(c.get("alerted_volunteers", []))
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO cases (
            case_id, reporter, animal, severity, location,
            bleeding, can_move, urgency, status, volunteer,
            volunteer_number, alerted_volunteers, outcome,
            completion_photo, time_reported, time_accepted, time_completed
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (case_id) DO UPDATE SET
            status             = EXCLUDED.status,
            volunteer          = EXCLUDED.volunteer,
            volunteer_number   = EXCLUDED.volunteer_number,
            alerted_volunteers = EXCLUDED.alerted_volunteers,
            outcome            = EXCLUDED.outcome,
            completion_photo   = EXCLUDED.completion_photo,
            time_accepted      = EXCLUDED.time_accepted,
            time_completed     = EXCLUDED.time_completed;
    """, (
        c.get("case_id"),      c.get("reporter"),         c.get("animal"),
        str(c.get("severity","?")), c.get("location"),    c.get("bleeding"),
        c.get("can_move"),     c.get("urgency"),          c.get("status","PENDING"),
        c.get("volunteer"),    c.get("volunteer_number"), alerted_json,
        c.get("outcome"),      c.get("completion_photo",False),
        c.get("time_reported"),c.get("time_accepted"),    c.get("time_completed"),
    ))
    conn.commit()
    cur.close()
    conn.close()


def generate_case_id():
    now = datetime.now()
    return f"CASE-{now.strftime('%d%m')}-{random.randint(1000,9999)}"


def create_case(reporter, session, urgency):
    case_id = generate_case_id()
    case = {
        "case_id":            case_id,
        "reporter":           reporter,
        "animal":             session.get("animal", "Unknown"),
        "severity":           session.get("severity", "?"),
        "location":           session.get("location", "Not shared"),
        "bleeding":           session.get("bleeding", "?"),
        "can_move":           session.get("can_move", "?"),
        "urgency":            urgency,
        "status":             "PENDING",
        "volunteer":          None,
        "volunteer_number":   None,
        "alerted_volunteers": [],
        "outcome":            None,
        "completion_photo":   False,
        "time_reported":      datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "time_accepted":      None,
        "time_completed":     None,
    }
    save_case(case)
    s = load_session(reporter)
    s["case_id"] = case_id
    save_session(reporter, s)
    print(f"Case created: {case_id}")
    return case_id


# ── SESSION CRUD ──────────────────────────────────────────

def load_session(phone):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT session_data FROM sessions WHERE phone_number = %s;", (phone,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {}
    try:
        return json.loads(row["session_data"])
    except:
        return {}


def save_session(phone, data):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (phone_number, stage, session_data, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (phone_number) DO UPDATE SET
            stage        = EXCLUDED.stage,
            session_data = EXCLUDED.session_data,
            updated_at = NOW();
    """, (phone, data.get("stage","warning"), json.dumps(data)))
    conn.commit()
    cur.close()
    conn.close()


def delete_session(phone):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE phone_number = %s;", (phone,))
    conn.commit()
    cur.close()
    conn.close()


def session_exists(phone):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT 1 FROM sessions WHERE phone_number = %s;", (phone,))
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists


def clear_reporter_session(sender):
    delete_session(sender)
    waiting_reporters.pop(sender, None)
    print(f"Session cleared for {sender}")


# ════════════════════════════════════════════════════════════
# WHATSAPP HELPERS
# ════════════════════════════════════════════════════════════

def send_message(to, message):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":    to,
        "type":  "text",
        "text":  {"preview_url": False, "body": message},
    }
    response = requests.post(url, headers=headers, json=data)
    print("SEND:", response.status_code)


def get_image_url(image_id):
    url     = f"https://graph.facebook.com/v18.0/{image_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    return requests.get(url, headers=headers).json()["url"]


def download_image(image_url, save_path="received.jpg"):
    try:
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        r = requests.get(image_url, headers=headers, timeout=15)
        r.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(r.content)
        print(f"Image saved: {save_path}")
        return True
    except Exception as e:
        print(f"Image download error: {e}")
        return False


def upload_and_send_photo(to, photo_path, caption=""):
    if not os.path.exists(photo_path):
        return
    upload_url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/media"
    headers    = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    with open(photo_path, "rb") as f:
        files = {
            "file":              (photo_path, f, "image/jpeg"),
            "messaging_product": (None, "whatsapp"),
            "type":              (None, "image/jpeg"),
        }
        upload_r = requests.post(upload_url, headers=headers, files=files)
    media_id = upload_r.json().get("id")
    if not media_id:
        return
    url  = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    hdrs = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":    to,
        "type":  "image",
        "image": {"id": media_id, "caption": caption},
    }
    requests.post(url, headers=hdrs, json=data)
    print(f"Photo sent to {to}")


def send_photo_to_volunteer(to):
    upload_and_send_photo(to, "received.jpg", "📸 Photo reported by rescue reporter")


# ════════════════════════════════════════════════════════════
# ESCALATION
# ════════════════════════════════════════════════════════════

def escalate_case(case_id):
    case = load_case(case_id)
    if not case or case["status"] in ["ACCEPTED","COMPLETED"]:
        return
    print(f"ESCALATING {case_id}")
    volunteers = load_volunteers()
    alerted    = case.get("alerted_volunteers", [])
    remaining  = [v for v in volunteers if v not in alerted]
    if remaining:
        for vol in remaining:
            send_message(vol,
                f"🚨 ESCALATION ALERT 🚨\n"
                f"No volunteer responded for 10 minutes!\n\n"
                f"Case ID: {case_id}\nAnimal: {case['animal']}\n"
                f"Severity: {case['severity']}/10\n📍 {case['location']}\n\n"
                f"Reporter: +{case['reporter']}\n\n"
                f"Reply RESPONDING immediately.\nCompleted: COMPLETED {case_id}"
            )
            active_cases[vol] = {"reporter": case["reporter"], "case_id": case_id}
            alerted.append(vol)
        case["alerted_volunteers"] = alerted
        save_case(case)
        send_message(case["reporter"],
            f"⏰ Update on {case_id}:\n"
            "Still looking for a volunteer. Additional team alerted. Help is coming."
        )
    else:
        send_message(case["reporter"],
            f"⚠️ Update on {case_id}:\n"
            "All volunteers alerted.\n\n"
            "📞 Animal Helpline: 1962\n📞 SPCA: 011-23619027"
        )


def start_escalation_timer(case_id, delay_seconds=600):
    t = threading.Timer(delay_seconds, escalate_case, args=[case_id])
    t.daemon = True
    t.start()
    print(f"Escalation timer started: {case_id}")


# ════════════════════════════════════════════════════════════
# GROQ INTERPRETER
# ════════════════════════════════════════════════════════════

def interpret_answer(question_type, user_answer):
    prompt = f"""
You are interpreting a WhatsApp message from someone reporting an animal emergency.
Question type: {question_type}
User answer: "{user_answer}"

Return ONLY one value:
- yes_no  → YES / NO / UNCLEAR
- animal  → dog / cat / cow / horse / bird / other / UNCLEAR
- severity → number 1-10 / UNCLEAR
- text → cleaned answer / UNCLEAR

Rules:
- "yes but for some time", "haan", "ji", "yep" etc. → YES
- "nahi", "nope", "no" etc. → NO
- "bahut bura hai" → 8, "minor" → 2, "very serious" → 8

Return ONLY the value. Nothing else.
"""
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role":"user","content":prompt}],
        max_tokens=20, temperature=0
    )
    result = r.choices[0].message.content.strip()
    print(f"GROQ: '{user_answer}' → '{result}'")
    return result


# ════════════════════════════════════════════════════════════
# QUESTION FLOW
# ════════════════════════════════════════════════════════════

ALL_QUESTION_MAP = {
    "animal":        ("animal",  "Which animal is it?\n\n1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other — please specify"),
    "bleeding":      ("yes_no",  "Is the animal bleeding?\n\nReply YES or NO"),
    "can_move":      ("yes_no",  "Can the animal move on its own?\n\nReply YES or NO"),
    "wounds":        ("yes_no",  "Are there any visible wounds or injuries?\n\nReply YES or NO"),
    "eating":        ("yes_no",  "Is the animal eating or drinking?\n\nReply YES or NO"),
    "duration":      ("text",    "How long has the animal been in this condition?\n\n(Example: 1 hour, since morning, not sure)"),
    "behavior":      ("text",    "Is the animal aggressive or calm?\n\n(Example: calm, scared, aggressive, unconscious)"),
    "ground_support":("yes_no",  "Is there anyone with the animal right now?\n\nYES — please share their WhatsApp number\nNO — rescuer will be dispatched urgently"),
}

INVALID_LOCATION_WORDS = [
    "here","nearby","near me","idk","don't know","dont know",
    "not sure","somewhere","outside","there","this place","same place"
]

def is_valid_location(text):
    t = text.strip().lower()
    if len(t) < 15:
        return False
    return not any(t == w or t.startswith(w) for w in INVALID_LOCATION_WORDS)


def get_next_question(session):
    severity = session.get("severity", 0)
    answered = session.get("answered", [])
    base = [
        ("animal",   "animal", "Which animal is it?\n\n1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other — please specify"),
        ("bleeding", "yes_no", "Is the animal bleeding?\n\nReply YES or NO"),
        ("can_move", "yes_no", "Can the animal move on its own?\n\nReply YES or NO"),
    ]
    moderate = [
        ("wounds",   "yes_no", "Are there any visible wounds or injuries?\n\nReply YES or NO"),
        ("eating",   "yes_no", "Is the animal eating or drinking?\n\nReply YES or NO"),
        ("duration", "text",   "How long has the animal been in this condition?\n\n(Example: 1 hour, since morning, not sure)"),
    ]
    mild_extra = moderate + [("behavior","text","Is the animal aggressive or calm?\n\n(Example: calm, scared, aggressive, unconscious)")]
    support  = ("ground_support","yes_no",
                "Is there anyone with the animal right now?\n\n"
                "YES — please share their WhatsApp number\n"
                "NO — our rescuer will be dispatched urgently")
    location = ("location","text",
                "Please share the exact location of the animal 📍\n\n"
                "Option 1 — Live location (recommended):\n"
                "Tap the 📎 attachment icon\n"
                "→ Select Location\n"
                "→ Share Live Location\n\n"
                "Option 2 — Type address:\n"
                "Include area name + landmark + city\n\n"
                "Example: Near Sector 5 Metro, Rohini, New Delhi\n\n"
                "⚠️ Accurate location = faster rescue.")

    if   severity >= 7: all_q = base + [support] + [location]
    elif severity >= 4: all_q = base + moderate   + [support] + [location]
    else:               all_q = base + mild_extra + [support] + [location]

    for key, qtype, question in all_q:
        if key not in answered:
            return key, qtype, question
    return "photo","text",(
        "Almost done! Please send a clear photo of the animal 📸\n\n"
        "Tips:\n"
        "• Get as close as safely possible\n"
        "• Make sure the animal is clearly in frame\n"
        "• Good lighting helps our AI analyse the injury\n\n"
        "Send the photo now 👇"
    )


def advance_to_next(sender, session):
    next_key, next_qtype, next_question = get_next_question(session)
    if next_key == "location":
        session["stage"] = "location"
    elif next_key == "photo":
        session["stage"] = "photo"
    else:
        session["pending_key"]   = next_key
        session["pending_qtype"] = next_qtype
        if next_key not in session.get("answered",[]):
            session["answered"].append(next_key)
    save_session(sender, session)
    send_message(sender, next_question)


# ════════════════════════════════════════════════════════════
# FIRST AID
# ════════════════════════════════════════════════════════════

def send_first_aid(sender, session):
    animal   = session.get("animal","animal").lower()
    bleeding = session.get("bleeding","NO")
    can_move = session.get("can_move","YES")
    severity = session.get("severity", 5)

    note = "⚠️ Serious case. Do not move the animal unless absolutely necessary.\n\n" if severity >= 7 else ""
    tips = []
    if bleeding == "YES":
        tips.append("🩸 Gentle pressure with clean cloth. Do not remove it.")
    if can_move == "NO":
        tips.append("🚫 Do not lift or drag — can cause more injury.")
    if   animal == "dog":
        tips += ["🐕 Keep people away.", "💧 Offer water only if conscious and calm."]
    elif animal == "cat":
        tips += ["🐈 Very still and quiet near them.", "🧤 Loosely cover with cloth — reduces panic."]
    elif animal == "cow":
        tips += ["🐄 Keep crowd away.", "☀️ Shade if in direct sun."]
    elif animal == "bird":
        tips += ["🐦 Loosely cover with cloth.", "🌡️ Keep warm — birds shock quickly."]
    else:
        tips += ["🐾 Stay calm, keep distance.", "👥 Ask bystanders to move away."]
    tips += ["📵 Low noise.", "🚫 No food or medicine without vet."]

    send_message(sender,
        "🐾 First Aid While You Wait:\n\n"
        + note + "\n".join(tips) +
        "\n\nVolunteer being alerted. You'll be notified when someone accepts.\n\n"
        "━━━━━━━━━━━━━━━\n"
        "Can you stay with the animal?\n\n"
        "Reply STAY — waiting\n"
        "Reply LEAVE — leaving"
    )


# ════════════════════════════════════════════════════════════
# VOLUNTEER FLOW
# ════════════════════════════════════════════════════════════

def handle_responding(sender, volunteer_name, case_data):
    reporter      = case_data["reporter"]
    case_id_found = case_data["case_id"]
    case          = load_case(case_id_found)
    if not case:
        send_message(sender, "Case not found. Please check your rescue alert and try again.")
        return

    case["status"]           = "ACCEPTED"
    case["volunteer"]        = volunteer_name
    case["volunteer_number"] = sender
    case["time_accepted"]    = datetime.now().strftime("%d %b %Y, %I:%M %p")
    save_case(case)

    rp = waiting_reporters.get(reporter)
    rp_text = ("✅ Reporter IS waiting." if rp is True
               else "⚠️ Reporter is NOT at location." if rp is False
               else "ℹ️ Reporter presence unknown.")

    send_message(sender,
        f"✅ Case accepted.\n\n"
        f"📋 {case_id_found}\n📍 {case['location']}\n"
        f"Reporter: +{reporter}\n{rp_text}\n\n"
        f"When done: COMPLETED {case_id_found}"
    )
    send_message(reporter,
        f"🐾 Volunteer accepted your case!\n\n"
        f"Volunteer: {volunteer_name}\nContact: +{sender}\n\n"
        "They are heading to the location."
    )
    pending_outcome[sender] = {"case_id": case_id_found, "note": None}
    send_message(sender,
        "📝 Send outcome note anytime before completing.\n"
        "Example: \"Taken to vet, stable\"\n\n"
        f"Then: COMPLETED {case_id_found}"
    )
    active_cases.pop(sender, None)
    pending_volunteer_responses.pop(sender, None)


def handle_outcome_note(sender, text):
    data = pending_outcome.get(sender)
    if not data or not isinstance(data, dict):
        return False
    data["note"] = text.strip()
    pending_outcome[sender] = data
    send_message(sender, f"✅ Note saved.\nWhen done: COMPLETED {data['case_id']}")
    return True


def connect_reporter_volunteer(reporter, volunteer_number, volunteer_name):
    cd            = pending_volunteer_responses.get(volunteer_number, {})
    case_id_found = cd.get("case_id") if isinstance(cd, dict) else None
    if not case_id_found:
        for cid, c in load_cases().items():
            if c["reporter"] == reporter and c["status"] in ["PENDING","ACCEPTED"]:
                case_id_found = cid
                break
    if case_id_found:
        case = load_case(case_id_found)
        if case:
            case.update({
                "status":"ACCEPTED","volunteer":volunteer_name,
                "volunteer_number":volunteer_number,
                "time_accepted":datetime.now().strftime("%d %b %Y, %I:%M %p")
            })
            save_case(case)

    send_message(reporter,
        f"🙏 Thank you for staying.\n\n"
        f"Volunteer {volunteer_name} is on the way.\nContact: +{volunteer_number}"
    )
    send_message(volunteer_number,
        f"✅ Reporter is waiting.\n📋 {case_id_found}\nReporter: +{reporter}\n\n"
        f"COMPLETED {case_id_found}"
    )
    clear_reporter_session(reporter)
    active_cases.pop(volunteer_number, None)
    pending_volunteer_responses.pop(volunteer_number, None)


def handle_status(sender, text):
    parts   = text.strip().upper().replace(" -","-").replace("- ","-").split()
    case_id = parts[1] if len(parts) >= 2 else None

    if not case_id:
        conn = get_db(); cur = conn.cursor()
        cur.execute(
            "SELECT case_id FROM cases WHERE reporter=%s ORDER BY time_reported DESC LIMIT 1;",
            (sender,)
        )
        row = cur.fetchone(); cur.close(); conn.close()
        if row: case_id = row["case_id"]

    if not case_id:
        send_message(sender, "No case found.\nReply: STATUS CASE-XXXX")
        return

    case = load_case(case_id)
    if not case:
        send_message(sender, f"Case {case_id} not found.")
        return

    status_text = (
        "⏳ Waiting for volunteer" if case["status"] == "PENDING" else
        f"🚑 Volunteer {case['volunteer']} is on the way" if case["status"] == "ACCEPTED" else
        "✅ Rescue completed"
    )
    msg = (
        f"📋 CASE STATUS\n\nCase ID: {case_id}\n"
        f"Animal: {case['animal']}\nLocation: {case['location']}\n"
        f"Severity: {case['severity']}/10\nReported: {case['time_reported']}\n\n"
        f"Status: {status_text}\n"
    )
    if case.get("time_accepted"):   msg += f"Accepted: {case['time_accepted']}\n"
    if case.get("time_completed"):  msg += f"Completed: {case['time_completed']}\n"
    if case.get("outcome"):         msg += f"\n📝 Outcome: \"{case['outcome']}\""
    send_message(sender, msg)


def handle_completed(sender, text):
    parts = text.strip().upper().replace(" -","-").replace("- ","-").split()
    if len(parts) < 2:
        send_message(sender, "Include Case ID.\nExample: COMPLETED CASE-XXXX")
        return
    case_id = parts[1]
    case    = load_case(case_id)
    if not case:
        send_message(sender, f"Case {case_id} not found.")
        return
    if case["status"] == "COMPLETED":
        send_message(sender, f"Case {case_id} is already marked as completed. Thank you for your service! 🐾")
        return

    note = pending_outcome.get(sender, {})
    note = note.get("note") if isinstance(note, dict) else None

    case["status"]         = "COMPLETED"
    case["time_completed"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    if note: case["outcome"] = note
    save_case(case)
    pending_outcome.pop(sender, None)
    increment_rescues(sender)

    send_message(sender,
        f"✅ Case {case_id} has been marked as completed.\n\n"
        "Thank you for showing up today. 🐾\n\n"
        "Every rescue you complete is logged on the Animitr leaderboard.\n"
        "You made a real difference to an animal that had no voice. 💚"
    )
    reporter = case["reporter"]
    clear_reporter_session(reporter)

    reporter_msg = f"🐾 Your rescue case is complete.\n\nCase ID: {case_id}\n"
    if note: reporter_msg += f"Volunteer note: \"{note}\"\n\n"
    reporter_msg += "Thank you for reporting and helping save an animal 💚"
    send_message(reporter, reporter_msg)
    print(f"Case {case_id} completed by {sender}")


# ════════════════════════════════════════════════════════════
# GEMINI
# ════════════════════════════════════════════════════════════

def analyze_with_gemini(image_path, user_answers):
    try:
        img    = PIL.Image.open(image_path)
        prompt = (
            "You are an animal rescue triage assistant.\n\n"
            f"Reporter info:\n{user_answers}\n\n"
            "From the image:\n"
            "1. Animal seen?\n2. Matches description?\n3. Severity 1-10?\n"
            "4. Signs of distress?\n5. Urgency: HIGH / MEDIUM / LOW?\n\nBe concise."
        )
        response = gemini_model.generate_content([prompt, img])
        print("GEMINI:", response.text[:120])
        return response.text
    except Exception as e:
        print(f"Gemini error: {e}")
        return "AI analysis unavailable. Urgency: HIGH"  # fail safe to HIGH


def extract_urgency(text):
    t = text.upper()
    if "HIGH"   in t: return "HIGH"
    if "MEDIUM" in t: return "MEDIUM"
    return "LOW"


def alert_volunteers(sender, session, urgency, gemini_analysis, case_id):
    volunteers = load_volunteers()
    if not volunteers:
        print("No volunteers registered.")
        return
    line = ("🔴 URGENT — IMMEDIATE RESPONSE" if urgency=="HIGH" else
            "🟡 MEDIUM — RESPOND SOON"       if urgency=="MEDIUM" else
            "🟢 LOW — MONITOR SITUATION")
    message = (
        f"🚨 RESCUE ALERT 🚨\n{line}\n\n"
        f"📋 Case ID: {case_id}\n\n"
        f"Animal: {session.get('animal','?')}\n"
        f"Severity: {session.get('severity','?')}/10\n"
        f"Bleeding: {session.get('bleeding','?')}\n"
        f"Can move: {session.get('can_move','?')}\n"
        f"Ground support: {session.get('ground_support','?')}\n"
        f"📍 Location: {session.get('location','?')}\n\n"
        f"AI Analysis:\n{gemini_analysis}\n\n"
        f"Reported by: +{sender}\n\n"
        f"Reply RESPONDING to accept.\n"
        f"When done: COMPLETED {case_id}"
    )
    alerted = []
    for vol in volunteers:
        send_message(vol, message)
        send_photo_to_volunteer(vol)
        active_cases[vol] = {"reporter": sender, "case_id": case_id}
        alerted.append(vol)
        print(f"Alerted: {vol}")
    case = load_case(case_id)
    if case:
        case["alerted_volunteers"] = alerted
        save_case(case)
    start_escalation_timer(case_id)


# ════════════════════════════════════════════════════════════
# PROCESS ANSWER
# ════════════════════════════════════════════════════════════

def process_answer(sender, text):
    session = load_session(sender)
    stage   = session.get("stage", "warning")

    if stage == "warning":
        if interpret_answer("yes_no", text) == "YES":
            session["stage"] = "severity"
            save_session(sender, session)
            send_message(sender,
                "On a scale of 1 to 10, how serious is the animal's condition?\n\n"
                "1 = Minor injury, alert but moving\n"
                "5 = Moderate, needs attention soon\n"
                "10 = Critical, life threatening\n\n"
                "Please reply with a number between 1 and 10.")
        else:
            send_message(sender,
                "🚨 ANIMAL RESCUE SYSTEM 🚨\n\n"
                "Your number is registered.\n"
                "False reports = legal action.\n\n"
                "Genuine emergency only. Reply YES to proceed."
            )

    elif stage == "severity":
        interpreted = interpret_answer("severity", text)
        try:
            severity = int(interpreted)
            if 1 <= severity <= 10:
                session.update({"severity":severity,"stage":"questions","answered":[],"unclear_count":0})
                save_session(sender, session)
                level = "CRITICAL" if severity>=7 else "MODERATE" if severity>=4 else "MILD"
                send_message(sender,
                    f"Severity {severity}/10 — {level}\n\n"
                    "Which animal is it?\n\n"
                    "1. Dog\n"
                    "2. Cat\n"
                    "3. Cow\n"
                    "4. Horse\n"
                    "5. Other — please specify")
                session["answered"].append("animal")
                session["pending_key"]   = "animal"
                session["pending_qtype"] = "animal"
                save_session(sender, session)
            else:
                send_message(sender, "Enter a number 1-10.")
        except:
            send_message(sender, "Enter a number 1-10.")

    elif stage == "questions":
        pending_key   = session.get("pending_key")
        pending_qtype = session.get("pending_qtype","text")
        if pending_key:
            interpreted = interpret_answer(pending_qtype, text)
            if interpreted == "UNCLEAR":
                unclear = session.get("unclear_count",0) + 1
                session["unclear_count"] = unclear
                if unclear >= 3 and session.get("severity",5) >= 4:
                    session[pending_key]     = "Not provided"
                    session["pending_key"]   = None
                    session["unclear_count"] = 0
                    save_session(sender, session)
                    advance_to_next(sender, session)
                else:
                    save_session(sender, session)
                    _, q = ALL_QUESTION_MAP.get(pending_key, ("text","Clarify?"))
                    send_message(sender, f"Sorry, I didn't quite understand your answer.\n\nCould you please clarify?\n\n{q}")
                return
            session["unclear_count"] = 0
            if pending_key == "animal":
                am = {"1":"Dog","2":"Cat","3":"Cow","4":"Horse","5":"Other",
                      "dog":"Dog","cat":"Cat","cow":"Cow","horse":"Horse","bird":"Bird","other":"Other"}
                session["animal"] = am.get(interpreted.lower(), interpreted.capitalize())
            elif pending_key == "ground_support":
                if interpreted == "YES":
                    session["ground_support"] = "YES"
                    num = None
                    for p in text.strip().split():
                        c = p.replace("+","").replace(" ","")
                        if c.isdigit() and len(c) >= 10: num = c; break
                    if num:
                        session["support_number"] = num
                        session["answered"].append("ground_support")
                        session["pending_key"] = None
                        session["unclear_count"] = 0
                        save_session(sender, session)
                    else:
                        session["stage"] = "support_number"
                        save_session(sender, session)
                        send_message(sender, "Share their WhatsApp number:")
                        return
                else:
                    session["ground_support"] = "NO"
            else:
                session[pending_key] = interpreted
            session["pending_key"]   = None
            session["unclear_count"] = 0
            save_session(sender, session)
        advance_to_next(sender, session)

    elif stage == "support_number":
        session["support_number"] = text.strip()
        session["stage"] = "questions"
        session["answered"].append("ground_support")
        save_session(sender, session)
        advance_to_next(sender, session)

    elif stage == "location":
        if not is_valid_location(text):
            send_message(sender,
                "⚠️ Location not accepted.\n\n"
                "Please provide a proper address that includes:\n"
                "• Area or colony name\n"
                "• Nearby landmark or street name\n"
                "• City name\n\n"
                "Example: Near Sector 5 Metro, Rohini, New Delhi\n\n"
                "Accurate location = faster rescue. Please try again:"
            )
            return
        session["location"] = text.strip()
        session["stage"]    = "photo"
        save_session(sender, session)
        send_message(sender,
            "📍 Location confirmed!\n\n"
            "Now please send a clear photo of the animal.\n\n"
            "Tips for a good photo:\n"
            "• Get as close as safely possible\n"
            "• Make sure the animal is clearly visible\n"
            "• Good lighting helps the AI analyse the injury better\n\n"
            "Send the photo now 📸")

    elif stage == "waiting":
        interpreted = interpret_answer("yes_no", text)
        if text.upper() == "STAY" or interpreted == "YES":
            waiting_reporters[sender] = True
            vol_w = vol_name_w = None
            for vn, cd in list(pending_volunteer_responses.items()):
                if isinstance(cd,dict) and cd.get("reporter") == sender:
                    vol_w = vn
                    vols  = load_volunteers()
                    vol_name_w = vols.get(vn,{}).get("name","Volunteer")
                    break
            if vol_w:
                connect_reporter_volunteer(sender, vol_w, vol_name_w)
            else:
                send_message(sender, "🙏 Thank you for staying.\nYou'll be notified when a volunteer accepts.")
            clear_reporter_session(sender)

        elif text.upper() == "LEAVE" or interpreted == "NO":
            waiting_reporters[sender] = False
            send_message(sender, "Understood. Thank you for reporting. Help is on the way 🐾")
            for vn, cd in list(pending_volunteer_responses.items()):
                if isinstance(cd,dict) and cd.get("reporter") == sender:
                    send_message(vn, f"⚠️ Reporter left.\nGo to location fast.\nReporter: +{sender}")
                    pending_volunteer_responses.pop(vn, None)
                    active_cases.pop(vn, None)
                    break
            clear_reporter_session(sender)
        else:
            send_message(sender, "Reply STAY or LEAVE.")

    else:
        save_session(sender, {"stage":"warning"})
        send_message(sender,
            "🚨 ANIMAL RESCUE SYSTEM 🚨\n\n"
            "Your number is registered.\nGenuine emergency only. Reply YES to proceed."
        )


# ════════════════════════════════════════════════════════════
# WEBHOOK
# ════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]
        if "messages" not in value:
            return "OK", 200

        message = value["messages"][0]
        sender  = message["from"]

        if message["type"] == "text":
            text = message["text"]["body"].strip()

            if text.upper().startswith("STATUS"):
                handle_status(sender, text); return "OK", 200

            if text.upper().startswith("COMPLETED"):
                handle_completed(sender, text); return "OK", 200

            if sender in pending_outcome:
                if handle_outcome_note(sender, text): return "OK", 200

            if text.upper() == "JOIN":
                session = load_session(sender)
                session["stage"] = "volunteer_name"
                save_session(sender, session)
                send_message(sender,
                "🐾 Welcome to the Animal Rescue Volunteer Network!\n\n"
                "Thank you for choosing to make a difference.\n\n"
                "Please enter your full name to complete registration:")
                return "OK", 200

            session = load_session(sender)
            if session.get("stage") == "volunteer_name":
                save_volunteer(sender, text)
                delete_session(sender)
                send_message(sender,
                    f"✅ Welcome {text}!\n\n"
                    "You are now registered as an Animitr rescue volunteer. 🐾\n\n"
                    "When you receive a rescue alert:\n"
                    "→ Reply RESPONDING to accept the case\n\n"
                    "When the rescue is complete:\n"
                    "→ Reply COMPLETED CASE-XXXX\n\n"
                    "You will receive WhatsApp alerts when animals near you need help.\n"
                    "Thank you for joining. You are going to save lives. 💚"
                )
                return "OK", 200

            if text.upper() == "RESPONDING":
                vols = load_volunteers()
                if sender in vols:
                    cd = active_cases.get(sender)
                    if cd and isinstance(cd, dict):
                        handle_responding(sender, vols[sender]["name"], cd)
                    else:
                        send_message(sender,
                        "No active rescue case found for your number.\n\n"
                        "Please wait for a rescue alert to come through.\n"
                        "You will be notified when an animal needs help in your area.")
                else:
                    send_message(sender,
                        "You are not registered as a volunteer.\n\n"
                        "To join the rescue network, reply:\n"
                        "JOIN")
                return "OK", 200

            if not session_exists(sender):
                save_session(sender, {"stage":"warning"})
                send_message(sender,
                    "🚨 ANIMAL RESCUE SYSTEM 🚨\n\n"
                    "Your number is registered.\n"
                    "Genuine emergency only. Reply YES to proceed."
                )
            else:
                process_answer(sender, text)

        elif message["type"] == "location":
            session = load_session(sender)
            if session.get("stage") != "location":
                send_message(sender, "Please complete questions first.")
                return "OK", 200
            lat = message["location"]["latitude"]
            lng = message["location"]["longitude"]
            session["location"] = f"https://maps.google.com/?q={lat},{lng}"
            session["stage"]    = "photo"
            save_session(sender, session)
            send_message(sender, "📍 Location received!\n\nSend a clear photo 📸")

        elif message["type"] == "image":
            session = load_session(sender)
            vols    = load_volunteers()

            # Completion photo from volunteer
            if sender in vols and sender in pending_outcome:
                od = pending_outcome.get(sender, {})
                cid = od.get("case_id") if isinstance(od, dict) else None
                if cid:
                    path = f"completion_{cid}.jpg"
                    download_image(get_image_url(message["image"]["id"]), path)
                    case = load_case(cid)
                    if case:
                        upload_and_send_photo(
                            case["reporter"], path,
                            "📸 Photo from volunteer after rescue"
                        )
                        case["completion_photo"] = True
                        save_case(case)
                        send_message(sender, "✅ Photo shared with reporter.")
                    return "OK", 200

            if sender in vols and session.get("stage") != "photo":
                return "OK", 200

            if session.get("stage") == "location":
                send_message(sender, "Please share location first 📍")
                return "OK", 200

            if session.get("stage") != "photo":
                send_message(sender, "Please answer all questions first.")
                return "OK", 200

            send_message(sender, "📸 Photo received. Sending to rescue team...")
            ok = download_image(get_image_url(message["image"]["id"]))
            if not ok:
                send_message(sender, "⚠️ Could not download your photo. Please try sending it again.")
                return "OK", 200

            user_answers = (
                f"Animal: {session.get('animal','?')}\n"
                f"Severity: {session.get('severity','?')}/10\n"
                f"Bleeding: {session.get('bleeding','?')}\n"
                f"Can move: {session.get('can_move','?')}\n"
                f"Wounds: {session.get('wounds','?')}\n"
                f"Eating: {session.get('eating','?')}\n"
                f"Duration: {session.get('duration','?')}\n"
                f"Behavior: {session.get('behavior','?')}\n"
                f"Ground support: {session.get('ground_support','?')}"
            )
            gemini_analysis = analyze_with_gemini("received.jpg", user_answers)
            urgency         = extract_urgency(gemini_analysis)
            case_id         = create_case(sender, session, urgency)

            send_message(sender,
                f"📋 Your Case ID: {case_id}\n\n"
                "Please save this. You can check status anytime by replying:\n"
                f"STATUS {case_id}")

            if urgency == "HIGH":
                send_message(sender,
                    "🚨 HIGH URGENCY case created.\n\n"
                    "Your report has been dispatched to our rescue team.\n"
                    "Help is on the way.\n\n"
                    "🙏 This is a serious case — your presence with the animal\n"
                    "can make a real difference while the volunteer reaches you.")
            elif urgency == "MEDIUM":
                send_message(sender,
                    "✅ Your report has been dispatched to our rescue team.\n\n"
                    "A volunteer will respond to you soon.\n"
                    "Please stay close to the animal if possible.")
            else:
                send_message(sender,
                    "✅ Your report has been noted and sent to our rescue team.\n\n"
                    "A volunteer will check on the animal when available.\n"
                    "Thank you for reporting.")

            session["stage"] = "waiting"
            save_session(sender, session)
            send_first_aid(sender, session)
            alert_volunteers(sender, session, urgency, gemini_analysis, case_id)

    except Exception as e:
        print("Webhook error:", e)
    return "OK", 200


@app.route("/webhook", methods=["GET"])
def verify():
    mode, token, challenge = (
        request.args.get("hub.mode"),
        request.args.get("hub.verify_token"),
        request.args.get("hub.challenge"),
    )
    if mode == "subscribe" and token == "12345":
        print("Webhook verified!")
        return challenge, 200
    return "Verification failed", 403


# ════════════════════════════════════════════════════════════
# API ROUTES  (called by website JS)
# ════════════════════════════════════════════════════════════

@app.route("/api/stats", methods=["GET"])
def api_stats():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM cases;")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) AS done FROM cases WHERE status='COMPLETED';")
        done = cur.fetchone()["done"]
        cur.execute("SELECT COUNT(*) AS live FROM cases WHERE status IN ('PENDING','ACCEPTED');")
        live = cur.fetchone()["live"]
        cur.execute("SELECT COUNT(*) AS vols FROM volunteers WHERE status='active';")
        vols = cur.fetchone()["vols"]
        cur.close(); conn.close()
        return jsonify({
            "total_cases":      total,
            "completed_cases":  done,
            "active_cases":     live,
            "total_volunteers": vols,
            "completion_rate":  round(done/total*100,1) if total else 0,
        })
    except Exception as e:
        print("API /stats error:", e)
        return jsonify({"error": "Could not fetch stats"}), 500


@app.route("/api/cases", methods=["GET"])
def api_cases():
    """Recent cases. ?phone=91XXXXXXXXXX for reporter lookup."""
    try:
        phone = request.args.get("phone","").strip().replace("+","").replace(" ","")
        conn  = get_db(); cur = conn.cursor()
        if phone:
            cur.execute("""
                SELECT case_id, reporter, animal, severity, location,
                       urgency, status, volunteer, outcome,
                       time_reported, time_accepted, time_completed
                FROM cases WHERE reporter=%s
                ORDER BY time_reported DESC LIMIT 20;
            """, (phone,))
        else:
            cur.execute("""
                SELECT case_id, reporter, animal, severity, location,
                       urgency, status, volunteer, outcome,
                       time_reported, time_accepted, time_completed
                FROM cases
                ORDER BY time_reported DESC LIMIT 30;
            """)
        rows = cur.fetchall(); cur.close(); conn.close()
        cases = []
        for row in rows:
            d = dict(row)
            r = d.get("reporter","")
            d["reporter"] = f"+91 XXXXXX{r[-4:]}" if len(r)>=4 else "Unknown"
            cases.append(d)
        return jsonify({"cases": cases, "count": len(cases)})
    except Exception as e:
        print("API /cases error:", e)
        return jsonify({"error": "Could not fetch cases"}), 500


@app.route("/api/case/<case_id>", methods=["GET"])
def api_case(case_id):
    """Single case — for shareable links."""
    try:
        case = load_case(case_id)
        if not case:
            return jsonify({"error": "Case not found"}), 404
        r = case.get("reporter","")
        case["reporter"] = f"+91 XXXXXX{r[-4:]}" if len(r)>=4 else "Unknown"
        case.pop("alerted_volunteers", None)
        return jsonify(case)
    except Exception as e:
        print("API /case error:", e)
        return jsonify({"error": "Could not fetch case"}), 500


@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
    """Top volunteers by rescue count."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT name, city, tier, total_rescues, status
            FROM volunteers WHERE status='active'
            ORDER BY total_rescues DESC LIMIT 20;
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify({"leaderboard": [dict(r) for r in rows]})
    except Exception as e:
        print("API /leaderboard error:", e)
        return jsonify({"error": "Could not fetch leaderboard"}), 500


@app.route("/api/register-volunteer", methods=["POST"])
def api_register_volunteer():
    """Website volunteer form → DB + WhatsApp confirmation."""
    try:
        data  = request.get_json()
        name  = data.get("name","").strip()
        phone = data.get("phone","").strip().replace("+","").replace(" ","")
        city  = data.get("city","").strip()
        tier  = data.get("tier","community").strip()
        if not name or not phone:
            return jsonify({"error": "Name and phone required"}), 400
        save_volunteer(phone, name, city, tier)
        send_message(phone,
            f"🐾 Welcome to Animitr, {name}!\n\n"
            "You are now registered as a rescue volunteer.\n\n"
            "How it works:\n"
            "→ You will receive WhatsApp alerts when an animal needs help near you\n"
            "→ Reply RESPONDING to accept a case\n"
            "→ Rescue the animal\n"
            "→ Reply COMPLETED CASE-XXXX when done\n\n"
            "Your rescue count is tracked on our public leaderboard.\n\n"
            "Thank you for joining. You are going to save lives. 💚"
        )
        return jsonify({"success": True})
    except Exception as e:
        print("API /register-volunteer error:", e)
        return jsonify({"error": "Registration failed"}), 500


# ════════════════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    app.run(port=5000, debug=False)
