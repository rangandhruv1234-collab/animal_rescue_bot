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
pending_transfer            = {}   # volunteer_phone → {case_id, warned_at, urgency}
pending_completion_photo    = {}   # volunteer_phone → {case_id, note, deadline_timer}
pending_reporter_confirm    = {}   # reporter_phone  → {case_id, volunteer_name, vol_phone}
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
            phone_number    TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            status          TEXT DEFAULT 'active',
            city            TEXT,
            tier            TEXT,
            total_rescues   INTEGER DEFAULT 0,
            photo_warnings  INTEGER DEFAULT 0,
            registered_at   TIMESTAMP DEFAULT NOW()
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ngo_applications (
            id           SERIAL PRIMARY KEY,
            name         TEXT NOT NULL,
            city         TEXT,
            phone        TEXT,
            email        TEXT UNIQUE,
            website      TEXT,
            work_type    TEXT,
            description  TEXT,
            applied_at   TIMESTAMP DEFAULT NOW(),
            status       TEXT DEFAULT 'pending'
        );""")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ngos (
            id           SERIAL PRIMARY KEY,
            name         TEXT NOT NULL,
            city         TEXT,
            city_key     TEXT,
            phone        TEXT,
            email        TEXT,
            website      TEXT,
            work_type    TEXT,
            description  TEXT,
            tags         TEXT DEFAULT '[]',
            stat1_val    TEXT,
            stat1_label  TEXT,
            stat2_val    TEXT,
            stat2_label  TEXT,
            emoji        TEXT DEFAULT '🐾',
            color_theme  TEXT DEFAULT 'ct-teal',
            approved_at  TIMESTAMP DEFAULT NOW(),
            visible      BOOLEAN DEFAULT TRUE
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

def add_photo_warning(phone):
    """Add a photo warning to volunteer. Auto-ban at 5 warnings."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE volunteers SET photo_warnings = photo_warnings + 1
        WHERE phone_number = %s
        RETURNING photo_warnings, name;
    """, (phone,))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not row: return
    warnings = row["photo_warnings"]
    name     = row["name"]
    print(f"Photo warning {warnings}/5 for {name} ({phone})")
    if warnings >= 5:
        # Auto-ban
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE volunteers SET status='banned' WHERE phone_number=%s;", (phone,))
        conn.commit(); cur.close(); conn.close()
        send_message(phone,
            "🚫 Your Animitr volunteer account has been permanently banned.\n\n"
            "You have failed to provide a completion photo 5 times.\n\n"
            "This is a mandatory requirement for all volunteers.\n\n"
            "If you believe this is an error, contact:\ncontact.animitr@gmail.com"
        )
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"🚫 AUTO-BAN: {name} (+{phone})\n"
                "Reason: 5 completion photo warnings reached."
            )
    else:
        remaining = 5 - warnings
        send_message(phone,
            f"⚠️ Photo Warning {warnings}/5\n\n"
            "You did not send a completion photo for your last rescue case.\n\n"
            f"You have {remaining} warning{'s' if remaining > 1 else ''} remaining before your account is banned.\n\n"
            "All future rescues REQUIRE a completion photo before the case can close."
        )
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"⚠️ PHOTO WARNING {warnings}/5: {name} (+{phone})\n"
                "Did not submit completion photo."
            )


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
    if len(recent) > 30:
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
                    # New timings: warn at 10min HIGH (600s) or 25min MEDIUM/LOW (1500s)
                    warn_after = 600 if urgency == "HIGH" else 1500
                    remaining = warn_after - elapsed
                    if remaining > 0:
                        # Warning hasn't fired yet — restart with remaining time
                        t = threading.Timer(int(remaining), warn_ghost_volunteer, args=[case_id])
                        t.daemon = True; t.start()
                        print(f"Recovered ghost warning: {case_id} in {int(remaining)}s")
                    else:
                        # Warning overdue — fire immediately
                        threading.Thread(target=warn_ghost_volunteer, args=[case_id], daemon=True).start()
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

def warn_ghost_volunteer(case_id):
    """
    P5 — STEP 1: Warning shot before transfer.
    Fires after 10 min (HIGH) or 25 min (MEDIUM/LOW) of ACCEPTED with no COMPLETED.
    Sends volunteer a 2-minute warning. If they reply anything within 2 min → grace.
    If silent → reopen_stale_case() fires.
    """
    case = load_case(case_id)
    if not case or case["status"] != "ACCEPTED":
        return  # Already completed or reopened — do nothing

    vol_num  = case.get("volunteer_number")
    vol_name = case.get("volunteer", "Volunteer")
    urgency  = case.get("urgency", "MEDIUM")

    print(f"Ghost warning sent: {case_id} → {vol_name} ({vol_num})")

    # Mark case as "transfer_pending" so we can detect if they reply
    # We use pending_transfer dict (in-memory) keyed by volunteer phone
    pending_transfer[vol_num] = {
        "case_id":   case_id,
        "warned_at": datetime.now().isoformat(),
        "urgency":   urgency,
    }

    send_message(vol_num,
        f"⚠️ CASE UPDATE REQUIRED — {case_id}\n\n"
        f"You accepted this rescue {10 if urgency == 'HIGH' else 25} minutes ago "
        f"and no completion has been logged.\n\n"
        "Your case is being transferred in 2 minutes unless you reply.\n\n"
        "Reply anything right now to keep the case:\n"
        "• STILL_ON_SCENE — if you are still at location\n"
        f"• COMPLETED {case_id} — if rescue is done\n"
        "• Your outcome note — to log progress\n\n"
        "⏳ You have 2 minutes."
    )

    # Start 2-minute final countdown → reopen if still no reply
    t = threading.Timer(120, reopen_stale_case, args=[case_id])
    t.daemon = True
    t.start()
    print(f"Transfer countdown started: {case_id} in 120s")


def reopen_stale_case(case_id):
    """
    P5 — STEP 2: Actually transfer the case.
    Called after the 2-minute grace period expires with no reply.
    Also called directly if volunteer never replied to the initial warning.
    """
    case = load_case(case_id)
    if not case or case["status"] != "ACCEPTED":
        return  # Case was completed during grace period — do nothing

    stale_vol = case.get("volunteer", "The volunteer")
    stale_num = case.get("volunteer_number")
    urgency   = case.get("urgency", "MEDIUM")
    print(f"Reopening stale case {case_id} — {stale_vol} ghosted")

    # Clean up transfer warning state
    pending_transfer.pop(stale_num, None)

    # Reset case to PENDING
    case["status"]           = "PENDING"
    case["volunteer"]        = None
    case["volunteer_number"] = None
    case["time_accepted"]    = None
    save_case(case)

    if stale_num:
        active_cases.pop(stale_num, None)
        pending_outcome.pop(stale_num, None)
        send_message(stale_num,
            f"🔄 Case {case_id} has been transferred to another volunteer.\n\n"
            "You did not respond within the time window.\n\n"
            f"If you are still at the scene, please text RESPONDING to re-accept\n"
            f"or COMPLETED {case_id} if the rescue is already done."
        )

    send_message(case["reporter"],
        f"🔄 Update on {case_id}:\n\n"
        "Our volunteer was unable to complete the rescue in time.\n"
        "We are alerting backup volunteers now. Help is still on the way.\n\n"
        "We apologise for the delay."
    )

    # Re-alert all other volunteers
    volunteers = load_volunteers()
    for vol in volunteers:
        if vol == stale_num:
            continue
        send_message(vol,
            f"🔄 REACTIVATED CASE — {case_id}\n\n"
            f"Previous volunteer did not respond in time.\n\n"
            f"Animal: {case['animal']}\nSeverity: {case['severity']}/10\n"
            f"📍 {case['location']}\n\n"
            f"Reply RESPONDING if you can help now.\nWhen done: COMPLETED {case_id}"
        )
        active_cases[vol] = {"reporter": case["reporter"], "case_id": case_id}

    # Fresh escalation timer for the reopened case
    start_escalation_timer(case_id, delay_seconds=600)

    if ADMIN_NUMBER:
        send_message(ADMIN_NUMBER,
            f"⚠️ GHOST VOLUNTEER: Case {case_id} transferred.\n"
            f"Ghost: {stale_vol} (+{stale_num})\n"
            f"Animal: {case['animal']} | {urgency}\n"
            f"Location: {case['location']}"
        )


def handle_still_on_scene(sender, case_id):
    """
    Volunteer replied STILL_ON_SCENE during grace period.
    Give them one extension — same as original timeout duration.
    One extension only — no infinite stalling.
    """
    case = load_case(case_id)
    if not case or case["status"] != "ACCEPTED":
        send_message(sender, f"Case {case_id} is no longer active.")
        return

    urgency = case.get("urgency", "MEDIUM")

    # Clear the transfer warning
    pending_transfer.pop(sender, None)

    send_message(sender,
        f"✅ Got it. You have been given a one-time extension.\n\n"
        f"Extension time: {'10 minutes' if urgency == 'HIGH' else '25 minutes'}\n\n"
        "Please complete the rescue and send:\n"
        f"COMPLETED {case_id}\n\n"
        "No further extensions will be given after this."
    )

    send_message(case["reporter"],
        f"🔄 Update on {case_id}:\n\n"
        "Your volunteer has confirmed they are still at the scene.\n"
        "The rescue is in progress."
    )

    # One final timeout — no more warnings after this, goes straight to reopen
    extension = 600 if urgency == "HIGH" else 1500  # 10min / 25min
    t = threading.Timer(extension, reopen_stale_case, args=[case_id])
    t.daemon = True
    t.start()
    print(f"Extension granted: {case_id} ({urgency}) → {extension}s")


def start_escalation_timer(case_id, delay_seconds=600):
    t = threading.Timer(delay_seconds, escalate_case, args=[case_id])
    t.daemon = True; t.start()
    print(f"Escalation timer: {case_id} in {delay_seconds}s")


def start_acceptance_timeout(case_id, volunteer_name, urgency, delay_seconds=None):
    """
    P5 FIX — New timing:
      HIGH urgency:   10 minutes → warn → 2 min grace → transfer
      MEDIUM/LOW:     25 minutes → warn → 2 min grace → transfer
    After warn_ghost_volunteer fires, a 2-minute timer starts automatically.
    Total worst case: HIGH = 12 min, MEDIUM/LOW = 27 min before transfer.
    """
    if delay_seconds is None:
        delay_seconds = 600 if urgency == "HIGH" else 1500  # 10min / 25min
    t = threading.Timer(delay_seconds, warn_ghost_volunteer, args=[case_id])
    t.daemon = True
    t.start()
    print(f"Acceptance timeout ({urgency}): {case_id} — {delay_seconds}s until warning")


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
        f"Reporter: +{reporter}\n{rp_text}\n\n"
        f"📝 Send an outcome note anytime, then:\nCOMPLETED {case_id_found}\n\n"
        f"⚠️ You MUST send a completion photo when done.\nThis is mandatory for all Animitr volunteers."
    )

    # Reporter priming message 1 — immediate on volunteer accepting
    send_message(reporter,
        f"🐾 A volunteer has accepted your rescue case!\n\n"
        f"Volunteer: {volunteer_name}\nContact: +{sender}\n\n"
        "They are heading to the location now.\n\n"
        "📌 Important: When the rescue is complete, you will receive a photo "
        "from the volunteer. Please stay available — we will ask you to confirm "
        "that the animal looks safe. Your confirmation matters. 🐾"
    )

    pending_outcome[sender] = {"case_id": case_id_found, "note": None}
    active_cases.pop(sender, None); pending_volunteer_responses.pop(sender, None)

    # Reporter priming message 2 — 15 minutes into rescue
    def send_reminder():
        # Only send if case is still active
        c = load_case(case_id_found)
        if c and c["status"] == "ACCEPTED":
            send_message(reporter,
                f"🔔 Reminder for case {case_id_found}:\n\n"
                "Your rescue is in progress.\n\n"
                "When complete, you will receive a photo from the volunteer. "
                "Please reply YES or NO when asked if the animal looks safe.\n\n"
                "Your response helps us verify every rescue. Thank you."
            )
    t = threading.Timer(900, send_reminder); t.daemon = True; t.start()

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

def handle_photo_deadline(vol_phone, case_id):
    """
    30 minutes passed after COMPLETED — volunteer never sent photo.
    Issue warning, admin alert. Case still closes but rescue count not incremented.
    """
    if vol_phone not in pending_completion_photo:
        return  # Photo was received — deadline no longer relevant
    data = pending_completion_photo.pop(vol_phone, {})
    if data.get("case_id") != case_id:
        return

    print(f"Photo deadline missed: {case_id} by {vol_phone}")
    case = load_case(case_id)
    if not case or case["status"] == "COMPLETED":
        return

    # Close case WITHOUT incrementing rescue count
    note = data.get("note")
    case["status"]         = "COMPLETED"
    case["time_completed"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    if note: case["outcome"] = note
    save_case(case)
    pending_outcome.pop(vol_phone, None)

    # Issue photo warning — auto-ban at 5
    add_photo_warning(vol_phone)

    # Notify reporter with flag
    reporter = case["reporter"]
    send_message(reporter,
        f"🐾 Your rescue case {case_id} has been closed.\n\n"
        "Note: The volunteer did not provide a completion photo.\n"
        "Our admin team has been alerted and will follow up.\n\n"
        "If the animal did not receive help, please reply NO to this message."
    )
    pending_reporter_confirm[reporter] = {
        "case_id":       case_id,
        "volunteer_name": case.get("volunteer","Volunteer"),
        "vol_phone":     vol_phone,
        "photo_missing": True,
    }

    if ADMIN_NUMBER:
        send_message(ADMIN_NUMBER,
            f"⚠️ PHOTO MISSING: {case_id}\n"
            f"Volunteer: {case.get('volunteer')} (+{vol_phone})\n"
            f"Animal: {case['animal']} at {case['location']}\n"
            "Rescue count NOT incremented. Admin review required."
        )

    # Schedule photo cleanup (nothing to delete but keep consistent)
    threading.Timer(300, delete_case_photos, args=[case_id]).start()


def finalize_case_closure(vol_phone, case_id, note, was_accepted, photo_result):
    """
    Called after Gemini photo comparison is done.
    Closes the case, sends reporter the photo and confirmation question.
    """
    case = load_case(case_id)
    if not case or case["status"] == "COMPLETED":
        return

    case["status"]         = "COMPLETED"
    case["time_completed"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    case["completion_photo"] = True
    if note: case["outcome"] = note
    save_case(case)
    pending_outcome.pop(vol_phone, None)

    reporter     = case["reporter"]
    vol_name     = case.get("volunteer", "Volunteer")
    completion_p = f"completion_{case_id}.jpg"

    # Thank the volunteer
    send_message(vol_phone,
        f"✅ Case {case_id} — completion photo received.\n\n"
        "Thank you for showing up and for documenting the rescue. 🐾\n\n"
        "The reporter will confirm and your rescue count will be updated.\n\n"
        "You made a real difference. 💚"
    )

    # Forward completion photo to reporter with confirmation question
    upload_and_send_photo(reporter, completion_p,
        f"📸 Your volunteer {vol_name} has completed the rescue for case {case_id}."
    )
    send_message(reporter,
        f"🐾 Rescue update for case {case_id}:\n\n"
        f"Volunteer {vol_name} has marked this rescue as complete "
        "and sent the photo above.\n\n"
        "Please confirm:\n"
        "✅ Reply YES — animal looks safe and rescued\n"
        "❌ Reply NO — something looks wrong\n"
        "❓ Reply UNSURE — you cannot tell\n\n"
        "Your reply helps us verify every rescue. Thank you."
    )

    # Store confirmation state for reporter
    pending_reporter_confirm[reporter] = {
        "case_id":        case_id,
        "volunteer_name": vol_name,
        "vol_phone":      vol_phone,
        "photo_result":   photo_result,
        "was_accepted":   was_accepted,
        "photo_missing":  False,
    }

    # If Gemini flagged NO_MATCH, alert admin immediately too
    if photo_result == "NO_MATCH":
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"🚨 PHOTO MISMATCH: {case_id}\n"
                f"Volunteer: {vol_name} (+{vol_phone})\n"
                f"Gemini says completion photo does NOT match report photo.\n"
                f"Animal: {case['animal']} at {case['location']}\n"
                "Waiting for reporter confirmation."
            )

    # P4: Increment rescue count only if ACCEPTED→COMPLETED with photo provided
    # Tentatively increment now — will reverse if reporter says NO
    if was_accepted:
        increment_rescues(vol_phone)

    # Schedule 2-hour reporter confirmation deadline
    threading.Timer(7200, handle_reporter_confirmation_deadline,
                    args=[reporter, case_id, vol_phone, was_accepted]).start()

    # Schedule photo deletion after 2 hours regardless of outcome
    threading.Timer(7200, delete_case_photos, args=[case_id]).start()

    print(f"Case {case_id} closed. Photo result: {photo_result}. Waiting for reporter confirm.")


def handle_reporter_confirmation(reporter, reply):
    """
    Reporter replied YES / NO / UNSURE to completion confirmation.
    """
    data = pending_reporter_confirm.pop(reporter, None)
    if not data:
        return False  # Not in confirmation state

    case_id      = data["case_id"]
    vol_phone    = data["vol_phone"]
    vol_name     = data["volunteer_name"]
    was_accepted = data.get("was_accepted", True)
    photo_missing = data.get("photo_missing", False)

    reply_up = reply.strip().upper()

    if reply_up == "YES" or "YES" in reply_up:
        send_message(reporter,
            f"✅ Thank you for confirming.\n\n"
            f"Case {case_id} is now fully verified.\n\n"
            "Your report saved an animal today. 🐾"
        )
        # All good — rescue count already incremented in finalize_case_closure
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"✅ CONFIRMED: {case_id} — Reporter verified rescue.\nVolunteer: {vol_name}"
            )
        clear_reporter_session(reporter)
        return True

    elif reply_up == "NO" or "NO" in reply_up:
        send_message(reporter,
            f"⚠️ Thank you for letting us know.\n\n"
            "Our admin team has been alerted and will investigate immediately.\n\n"
            "If the animal is still in danger, please call:\n"
            "📞 Animal Helpline: 1962\n📞 SPCA: 011-23619027"
        )
        # Reverse rescue count — this rescue was fraudulent
        if was_accepted and not photo_missing:
            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                UPDATE volunteers SET total_rescues = GREATEST(total_rescues - 1, 0)
                WHERE phone_number = %s;
            """, (vol_phone,))
            conn.commit(); cur.close(); conn.close()
            print(f"Rescue count reversed for {vol_phone} — reporter denied.")

        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"🚨 REPORTER DENIED RESCUE: {case_id}\n"
                f"Volunteer: {vol_name} (+{vol_phone})\n"
                "Rescue count REVERSED. Immediate investigation required.\n"
                f"Use REMOVE_VOL {vol_phone} if fraud confirmed."
            )
        clear_reporter_session(reporter)
        return True

    elif "UNSURE" in reply_up:
        send_message(reporter,
            f"Understood. We have logged your uncertainty for case {case_id}.\n\n"
            "Our team will review the case. Thank you for responding."
        )
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"❓ REPORTER UNSURE: {case_id}\n"
                f"Volunteer: {vol_name} (+{vol_phone})\n"
                "Manual review recommended."
            )
        clear_reporter_session(reporter)
        return True

    return False  # Unknown reply — not handled


def handle_reporter_confirmation_deadline(reporter, case_id, vol_phone, was_accepted):
    """Reporter didn't reply to confirmation within 2 hours."""
    if reporter in pending_reporter_confirm:
        pending_reporter_confirm.pop(reporter, None)
        print(f"Reporter {reporter} didn't confirm case {case_id} — flagging to admin")
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"⏰ NO REPORTER REPLY: {case_id}\n"
                f"Reporter (+{reporter}) did not confirm rescue within 2 hours.\n"
                "Case remains closed. Rescue count stands. Manual review if needed."
            )
        clear_reporter_session(reporter)
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
    """
    P3 + P4 FIX: Auth check.
    NEW: Don't close case immediately. Ask volunteer for completion photo first.
    Case only closes after photo is received, compared, and reporter confirms.
    """
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
                accepted_at   = datetime.strptime(case["time_accepted"], "%d %b %Y, %I:%M %p")
                hours_elapsed = (datetime.now() - accepted_at).total_seconds() / 3600
            except: hours_elapsed = 0
            if hours_elapsed < 3:
                send_message(sender, "⏳ A volunteer is still assigned to your case.\nPlease wait for them to complete the rescue."); return

    # Save note from pending_outcome
    note_data = pending_outcome.get(sender, {})
    note      = note_data.get("note") if isinstance(note_data, dict) else None

    # ── NEW: Request completion photo before closing ──────────────
    # Put case in awaiting_photo state
    pending_completion_photo[sender] = {
        "case_id":     case_id,
        "note":        note,
        "was_accepted": (case["status"] == "ACCEPTED"),
    }

    send_message(sender,
        f"✅ Almost done — one last step.\n\n"
        f"Please send a photo of the animal NOW to close case {case_id}.\n\n"
        "📸 This is mandatory for all Animitr volunteers.\n"
        "It proves the rescue happened and reassures the reporter.\n\n"
        "Send the photo within 30 minutes or the case will be flagged."
    )

    # Start 30-minute photo deadline timer
    t = threading.Timer(1800, handle_photo_deadline, args=[sender, case_id])
    t.daemon = True; t.start()
    print(f"Photo deadline started: {case_id} — 30min")


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
            "4. Signs of distress?\n5. Urgency: HIGH / MEDIUM / LOW?\n\n"
            "IMPORTANT: Your entire response must be under 480 characters. "
            "Be extremely concise. No filler words. Facts only."
        )
        response = gemini_model.generate_content([prompt, img])
        print("GEMINI:", response.text[:120]); return response.text
    except Exception as e:
        print(f"Gemini error: {e}"); return "AI analysis unavailable. Urgency: HIGH"


def compare_photos_with_gemini(report_path, completion_path):
    """
    Compare the original report photo with the volunteer's completion photo.
    Returns: 'MATCH' | 'NO_MATCH' | 'UNCERTAIN'
    """
    try:
        report_img     = PIL.Image.open(report_path)
        completion_img = PIL.Image.open(completion_path)
        prompt = (
            "You are verifying an animal rescue.\n\n"
            "Image 1 is the ORIGINAL photo sent when the animal was reported injured.\n"
            "Image 2 is the COMPLETION photo sent by the volunteer after the rescue.\n\n"
            "Answer these two questions:\n"
            "1. Do both images appear to show the same animal? "
            "(Same species, similar markings, same injury location if visible)\n"
            "2. Does the animal in Image 2 appear to be in a safer or better situation? "
            "(Being held, in a vehicle, at a clinic, or visibly attended to)\n\n"
            "Reply with ONLY one word:\n"
            "MATCH — same animal, appears helped\n"
            "NO_MATCH — different animal or clearly unrelated photo\n"
            "UNCERTAIN — cannot determine clearly"
        )
        response = gemini_model.generate_content([prompt, report_img, completion_img])
        result = response.text.strip().upper()
        print(f"Gemini photo compare: {result}")
        if "NO_MATCH" in result:   return "NO_MATCH"
        if "UNCERTAIN" in result:  return "UNCERTAIN"
        return "MATCH"
    except Exception as e:
        print(f"Gemini photo compare error: {e}")
        return "UNCERTAIN"


def delete_case_photos(case_id):
    """Delete report and completion photos after case closes. Privacy compliance."""
    import os
    for path in [f"report_{case_id}.jpg", f"completion_{case_id}.jpg"]:
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"Deleted: {path}")
        except Exception as e:
            print(f"Photo delete error {path}: {e}")

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

    # Gemini is prompted to stay under 480 chars so the full alert stays well within WhatsApp's 4096 limit.
    ai_text = gemini_analysis

    message = (
        f"🚨 RESCUE ALERT 🚨\n{line}\n\n📋 Case ID: {case_id}\n\n"
        f"Animal: {session.get('animal','?')}\nSeverity: {session.get('severity','?')}/10\n"
        f"Bleeding: {session.get('bleeding','?')}\nCan move: {session.get('can_move','?')}\n"
        f"Ground support: {session.get('ground_support','?')}\n📍 Location: {session.get('location','?')}\n\n"
        f"AI Analysis:\n{ai_text}\n\nReported by: +{sender}\n\n"
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
            "⚠️ MANDATORY REQUIREMENT:\nAfter every rescue, you MUST send a photo of the animal "
            "before the case closes. This is non-negotiable.\n"
            "5 missed completion photos = permanent ban from the network.\n\n"
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

    elif cmd == "APPROVE_NGO" and target:
        # target is the NGO's email
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM ngo_applications WHERE LOWER(email)=%s;", (target.lower(),))
        app_row = cur.fetchone(); cur.close(); conn.close()
        if not app_row: return f"No NGO application found for {target}"
        city_key = (app_row["city"] or "").lower().replace(" ","")
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO ngos (name,city,city_key,phone,email,website,work_type,description,visible)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
            ON CONFLICT (email) DO UPDATE SET visible=TRUE;
        """, (app_row["name"], app_row["city"], city_key, app_row["phone"],
              app_row["email"], app_row["website"], app_row["work_type"], app_row["description"]))
        cur.execute("UPDATE ngo_applications SET status='approved' WHERE LOWER(email)=%s;", (target.lower(),))
        conn.commit(); cur.close(); conn.close()
        return f"✅ NGO approved and now visible on website: {app_row['name']}"

    elif cmd == "REJECT_NGO" and target:
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE ngo_applications SET status='rejected' WHERE LOWER(email)=%s;", (target.lower(),))
        conn.commit(); cur.close(); conn.close()
        return f"❌ NGO application rejected: {target}"
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
                "BLOCK 91XXXXXXXXXX reason\nUNBLOCK 91XXXXXXXXXX\nCLOSE_CASE CASE-XXXX\n"
                "APPROVE_NGO email@ngo.com\nREJECT_NGO email@ngo.com\nADMIN_STATS\nADMIN_CASES")


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
                admin_cmds = ["APPROVE","REJECT","REMOVE_VOL","BLOCK","UNBLOCK","CLOSE_CASE","ADMIN_STATS","ADMIN_CASES","APPROVE_NGO","REJECT_NGO"]
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
                    "not_found":"You are not registered as a volunteer.\n\nTo apply, visit:\nanimitr.org → Volunteer page\n\nText VSTATUS after applying to check your status.",
                }
                send_message(sender, msgs.get(vstatus, msgs["not_found"])); return "OK", 200

            if text_up.startswith("STATUS"):
                handle_status(sender, text); return "OK", 200

            if text_up.startswith("COMPLETED"):
                handle_completed(sender, text); return "OK", 200

            # ── REPORTER CONFIRMATION (YES/NO/UNSURE after completion photo) ──
            if sender in pending_reporter_confirm:
                if handle_reporter_confirmation(sender, text):
                    return "OK", 200

            # ── GRACE PERIOD INTERCEPT ─────────────────────────────────────
            # Volunteer is in the 2-minute transfer window — any reply saves them
            if sender in pending_transfer:
                transfer_data = pending_transfer[sender]
                cid = transfer_data.get("case_id")
                urgency = transfer_data.get("urgency", "MEDIUM")

                if text_up == "STILL_ON_SCENE":
                    handle_still_on_scene(sender, cid)
                elif text_up.startswith("COMPLETED"):
                    # Let handle_completed() run normally — it will mark case done
                    pending_transfer.pop(sender, None)
                    handle_completed(sender, text)
                else:
                    # Any other reply = they are active. Ask for proper update.
                    pending_transfer.pop(sender, None)
                    send_message(sender,
                        f"✅ Confirmed — you are still active on case {cid}.\n\n"
                        "Please send us an update:\n"
                        "• STILL_ON_SCENE — still at location, rescue in progress\n"
                        f"• COMPLETED {cid} — rescue is complete\n"
                        "• Your outcome note (e.g. 'Taken to vet, stable')\n\n"
                        f"Case will be monitored. Complete with: COMPLETED {cid}"
                    )
                    # Restart the full timeout from scratch — one more chance
                    extension = 600 if urgency == "HIGH" else 1500
                    t = threading.Timer(extension, warn_ghost_volunteer, args=[cid])
                    t.daemon = True; t.start()
                return "OK", 200

            if sender in pending_outcome:
                if not text_up.startswith("COMPLETED") and not text_up.startswith("STATUS"):
                    if handle_outcome_note(sender, text): return "OK", 200

            if text_up == "RESPONDING":
                vols = load_volunteers()
                if sender not in vols:
                    send_message(sender, "You are not a registered volunteer.\n\nTo apply, visit:\nanimitr.org → Volunteer page"); return "OK", 200
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
                    "Visit: animitr.org → Volunteer page\n\n"
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

            # ── COMPLETION PHOTO from volunteer awaiting photo submission ──
            if sender in pending_completion_photo:
                data    = pending_completion_photo.pop(sender, {})
                cid     = data.get("case_id")
                note    = data.get("note")
                was_acc = data.get("was_accepted", True)
                if cid:
                    comp_path = f"completion_{cid}.jpg"
                    ok = download_image(get_image_url(message["image"]["id"]), comp_path)
                    if not ok:
                        # Put them back and let them retry
                        pending_completion_photo[sender] = data
                        send_message(sender, "⚠️ Could not download your photo. Please try sending it again."); return "OK", 200
                    send_message(sender, "📸 Photo received. Running verification...")
                    # Compare with original report photo
                    report_path = f"report_{cid}.jpg"
                    import os as _os
                    if _os.path.exists(report_path):
                        photo_result = compare_photos_with_gemini(report_path, comp_path)
                    else:
                        # Report photo not found (e.g. after restart) — proceed with UNCERTAIN
                        photo_result = "UNCERTAIN"
                        print(f"Report photo missing for {cid} — skipping comparison")
                    finalize_case_closure(sender, cid, note, was_acc, photo_result)
                return "OK", 200

            # ── EXTRA PHOTO from volunteer already in pending_outcome (pre-COMPLETED) ──
            if sender in vols and sender in pending_outcome:
                od  = pending_outcome.get(sender, {}); cid = od.get("case_id") if isinstance(od,dict) else None
                if cid:
                    path = f"completion_{cid}.jpg"
                    download_image(get_image_url(message["image"]["id"]), path)
                    case = load_case(cid)
                    if case:
                        upload_and_send_photo(case["reporter"], path, "📸 Progress photo from your volunteer")
                        send_message(sender, "✅ Photo shared with the reporter as a progress update.")
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

            # Save report photo with case-specific filename for later comparison
            import shutil
            report_path = f"report_{case_id}.jpg"
            try:
                shutil.copy("received.jpg", report_path)
                print(f"Report photo saved: {report_path}")
            except Exception as e:
                print(f"Report photo copy error: {e}")
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


@app.route("/api/check-volunteer", methods=["GET"])
def api_check_volunteer():
    phone = request.args.get("phone","").strip().replace("+","").replace(" ","").replace("-","")
    if not phone: return jsonify({"status":"not_found"})
    return jsonify({"status": get_volunteer_status(phone)})


@app.route("/api/check-ngo", methods=["GET"])
def api_check_ngo():
    email = request.args.get("email","").strip().lower()
    if not email: return jsonify({"status":"not_found"})
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT status FROM ngo_applications WHERE LOWER(email)=%s;", (email,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT 1 FROM ngos WHERE LOWER(email)=%s AND visible=TRUE;", (email,))
            row2 = cur.fetchone()
            cur.close(); conn.close()
            return jsonify({"status":"approved" if row2 else "not_found"})
        cur.close(); conn.close()
        return jsonify({"status": row["status"]})
    except Exception as e:
        print("check-ngo error:", e); return jsonify({"status":"not_found"})


@app.route("/api/register-ngo", methods=["POST"])
def api_register_ngo():
    try:
        data = request.get_json()
        name        = data.get("name","").strip()
        city        = data.get("city","").strip()
        phone       = data.get("phone","").strip().replace("+","").replace(" ","")
        email       = data.get("email","").strip().lower()
        website     = data.get("website","").strip()
        work_type   = data.get("work_type","").strip()
        description = data.get("description","").strip()
        if not name or not email:
            return jsonify({"error":"Name and email required"}), 400
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT status FROM ngo_applications WHERE LOWER(email)=%s;", (email,))
        existing = cur.fetchone(); cur.close(); conn.close()
        if existing:
            msg = "This NGO is already listed." if existing["status"]=="approved" else "An application for this email is already under review."
            return jsonify({"error": msg}), 400
        conn = get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO ngo_applications (name,city,phone,email,website,work_type,description) VALUES (%s,%s,%s,%s,%s,%s,%s);",
                    (name, city, phone, email, website, work_type, description))
        conn.commit(); cur.close(); conn.close()
        if ADMIN_NUMBER:
            send_message(ADMIN_NUMBER,
                f"🏢 NEW NGO APPLICATION\n\nName: {name}\nCity: {city}\nEmail: {email}\n"
                f"Website: {website or 'Not provided'}\nWork: {work_type}\n\n"
                f"After KYC:\nAPPROVE_NGO {email}\nREJECT_NGO {email}"
            )
        return jsonify({"success": True})
    except Exception as e:
        print("register-ngo error:", e); return jsonify({"error":"Registration failed"}), 500


@app.route("/api/ngos", methods=["GET"])
def api_ngos():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT name,city,city_key,website,description,tags,stat1_val,stat1_label,stat2_val,stat2_label,emoji,color_theme,work_type FROM ngos WHERE visible=TRUE ORDER BY approved_at ASC;")
        rows = cur.fetchall(); cur.close(); conn.close()
        ngos = []
        for r in rows:
            d = dict(r)
            try: d["tags"] = json.loads(d.get("tags") or "[]")
            except: d["tags"] = []
            ngos.append(d)
        return jsonify({"ngos": ngos})
    except Exception as e:
        print("api-ngos error:", e); return jsonify({"error":"Could not fetch NGOs"}), 500


def seed_ngos():
    """Seed the 9 original NGOs into DB on first run."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM ngos;")
        if cur.fetchone()["cnt"] > 0:
            cur.close(); conn.close(); return
        ngos = [
            ("SPCA India","Delhi","delhi","https://spcaindia.org","Society for Prevention of Cruelty to Animals. India's oldest animal welfare organisation. Runs shelters, mobile vet units, and rescue operations since 1861.",'["Rescue","Shelter","Vet Care"]',"160+","Years active","Pan India","Reach","🏥","ct-green"),
            ("People For Animals","Pan India","pan","https://peopleforanimalsindia.org","Founded by Maneka Gandhi. India's largest animal welfare organisation with chapters in 26 states. Ambulances, hospitals, and rehabilitation centres nationwide.",'["Ambulance","Hospital","Rehab"]',"26","States","1992","Founded","🐕","ct-orange"),
            ("FIAPO","Pan India","pan","https://fiapo.org","Federation of Indian Animal Protection Organisations. An umbrella body connecting 100+ animal welfare organisations. Advocacy, capacity building, and policy work.",'["Advocacy","Policy","Network"]',"100+","Member orgs","2007","Founded","🐄","ct-blue"),
            ("Friendicoes SECA","Delhi","delhi","https://friendicoes.org","One of Delhi's oldest and most active shelters. Rescues, treats, and rehomes stray dogs and cats. Runs a 24/7 rescue ambulance service across Delhi-NCR.",'["Shelter","24/7 Rescue","Adoption"]',"24/7","Ambulance","1979","Founded","🐾","ct-teal"),
            ("Welfare of Stray Dogs","Mumbai","mumbai","https://wsd.ngo","Mumbai-based organisation focused entirely on stray dog welfare. City-wide ABC programs, rescue, treatment, and vaccination drives. Founded 1999.",'["ABC Program","Vaccination","Treatment"]',"25yr+","Operating","Mumbai","Focus","🐕","ct-purple"),
            ("Blue Cross of India","Chennai","chennai","https://bluecrossofindia.org","South India's most established animal welfare organisation. Full veterinary hospital, ambulance service, adoption programs, and school education initiatives. Est. 1959.",'["Vet Hospital","Education","Adoption"]',"65yr+","Operating","Chennai","Base","🏥","ct-red"),
            ("Humane Society India","Pan India","pan","https://hsi.org/world/india","Indian affiliate of Humane Society International. Street animal welfare, disaster response, and policy campaigns at the national level.",'["Disaster Response","Policy","Welfare"]',"Int\'l","Affiliate","Policy","Focus","🐾","ct-blue"),
            ("Karuna Society","Hyderabad","hyderabad","https://karunasociety.org","Rural animal welfare in AP. Mobile vet clinics, sterilisation camps, and rescue operations in areas urban NGOs don\'t reach. Est. 1994.",'["Rural Rescue","Mobile Vet","Sterilisation"]',"Rural","Focus","1994","Founded","🐄","ct-green"),
            ("Wildlife SOS","Delhi","delhi","https://wildlifesos.org","Dedicated to protecting India\'s wildlife. Rescue elephants, bears, leopards, and other wild animals from exploitation. Over 5,000 wild rescues.",'["Wildlife","Sanctuary","Policy"]',"5,000+","Wild rescues","1995","Founded","🦁","ct-teal"),
        ]
        for n in ngos:
            cur.execute("INSERT INTO ngos (name,city,city_key,website,description,tags,stat1_val,stat1_label,stat2_val,stat2_label,emoji,color_theme,visible) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE);", n)
        conn.commit(); cur.close(); conn.close()
        print(f"Seeded {len(ngos)} NGOs.")
    except Exception as e:
        print(f"NGO seed error: {e}")


# ══════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    seed_ngos()
    schedule_session_cleanup()
    app.run(port=5000, debug=False)
