"""
Anira — Telegram Animal Rescue Bot
Powered by Animitr Platform

Same PostgreSQL DB as WhatsApp bot (app.py)
Full inline button UI — no typing required for reporters
Volunteers get button prompts too

CHANGES V2:
  - Input validation: random text ignored during button-only stages
  - Volunteer registration: collects real WhatsApp number
  - Cross-platform approval: both WA + TG notify admin when TG volunteer applies
  - Approval sync: already approved on one platform = blocked on other
  - Real phone stored so volunteer appears on website leaderboard

Author: Dhruv Rangan
"""

import os, secrets, base64, json, threading, logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

import requests
import PIL.Image
import psycopg2
from psycopg2.extras import RealDictCursor
import google.generativeai as genai
from groq import Groq

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN  = os.getenv("Telegram_Token")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
DATABASE_URL    = os.getenv("DATABASE_URL")
ADMIN_NUMBER    = os.getenv("ADMIN_NUMBER", "")
ADMIN_TG_ID     = os.getenv("ADMIN_TG_ID", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")
groq_client  = Groq(api_key=GROQ_API_KEY)

# In-memory state
tg_sessions             = {}
tg_active_cases         = {}
tg_pending_outcome      = {}
tg_pending_transfer     = {}
tg_pending_photo        = {}
tg_pending_confirm      = {}
tg_waiting_reporters    = {}
tg_message_timestamps   = {}

# Stages where ONLY button input is valid — text is rejected
BUTTON_ONLY_STAGES = {
    "animal", "bleeding", "can_move", "severity",
    "wounds", "eating", "ground_support"
}


# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def load_all_active_volunteers():
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


def save_application(phone, name, city=None, tier=None, tg_id=None):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO volunteer_applications (phone_number,name,city,tier,status)
        VALUES (%s,%s,%s,%s,'pending')
        ON CONFLICT (phone_number) DO UPDATE SET
            name=EXCLUDED.name,
            city=COALESCE(EXCLUDED.city,volunteer_applications.city),
            tier=COALESCE(EXCLUDED.tier,volunteer_applications.tier),
            status='pending', applied_at=NOW();
    """, (phone, name, city, tier or "community"))
    conn.commit(); cur.close(); conn.close()

    # Store tg_id → phone mapping so APPROVE in app.py can notify via Telegram
    if tg_id:
        save_tg_mapping(phone, tg_id)


def save_tg_mapping(phone, tg_id):
    """
    Store phone → tg_id mapping using sessions table.
    This lets app.py's APPROVE command find the TG chat_id
    and send a Telegram notification when approving.
    """
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (phone_number, stage, session_data, updated_at)
        VALUES (%s, 'tg_mapping', %s, NOW())
        ON CONFLICT (phone_number) DO UPDATE SET
            stage='tg_mapping',
            session_data=EXCLUDED.session_data,
            updated_at=NOW();
    """, (f"tgmap_{phone}", json.dumps({"tg_id": tg_id, "phone": phone})))
    conn.commit(); cur.close(); conn.close()


def get_tg_id_for_phone(phone):
    """Look up Telegram chat_id for a real phone number"""
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "SELECT session_data FROM sessions WHERE phone_number=%s AND stage='tg_mapping';",
        (f"tgmap_{phone}",)
    )
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return None
    try:
        return json.loads(row["session_data"]).get("tg_id")
    except:
        return None


def get_tg_id_for_chat(chat_id):
    """Find the real phone number for a given telegram chat_id"""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT session_data FROM sessions
        WHERE stage='tg_mapping' AND session_data::text LIKE %s;
    """, (f'%"tg_id": {chat_id}%',))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return None
    try:
        return json.loads(row["session_data"]).get("phone")
    except:
        return None


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


def get_status_for_tg_user(chat_id):
    """
    Get volunteer status for a Telegram user.
    Checks both tg_{chat_id} format and real WA number via mapping.
    Returns (status, phone) tuple.
    """
    # Check tg_ format first
    tg_phone = f"tg_{chat_id}"
    status   = get_volunteer_status(tg_phone)
    if status != "not_found":
        return status, tg_phone

    # Check real phone via tg_mapping
    real_phone = get_tg_id_for_chat(chat_id)
    if real_phone:
        status = get_volunteer_status(real_phone)
        return status, real_phone

    return "not_found", None


def increment_rescues(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE volunteers SET total_rescues=total_rescues+1 WHERE phone_number=%s;", (phone,))
    conn.commit(); cur.close(); conn.close()


def add_photo_warning(phone):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE volunteers SET photo_warnings = photo_warnings + 1
        WHERE phone_number = %s RETURNING photo_warnings, name;
    """, (phone,))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not row: return
    if row["photo_warnings"] >= 5:
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE volunteers SET status='banned' WHERE phone_number=%s;", (phone,))
        conn.commit(); cur.close(); conn.close()


def is_blocked_tg(chat_id):
    phone = f"tg_{chat_id}"
    conn  = get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM blocked_numbers WHERE phone_number=%s;", (phone,))
    result = cur.fetchone() is not None; cur.close(); conn.close()
    return result


def load_case(case_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM cases WHERE case_id=%s;", (case_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return None
    d = dict(row)
    try: d["alerted_volunteers"] = json.loads(d.get("alerted_volunteers") or "[]")
    except: d["alerted_volunteers"] = []
    return d


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
    return f"CASE-{datetime.now().strftime('%d%m')}-{secrets.token_hex(3).upper()}"


def count_active_cases_for_reporter(reporter):
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM cases WHERE reporter=%s AND status IN ('PENDING','ACCEPTED');",
        (reporter,)
    )
    row = cur.fetchone(); cur.close(); conn.close()
    return row["cnt"] if row else 0


# ══════════════════════════════════════════════════════════════════
# RATE LIMITING
# ══════════════════════════════════════════════════════════════════

def is_rate_limited(chat_id):
    now        = datetime.now()
    timestamps = tg_message_timestamps.get(chat_id, [])
    recent     = [t for t in timestamps if (now - t).total_seconds() < 60]
    recent.append(now)
    tg_message_timestamps[chat_id] = recent
    return len(recent) > 30


# ══════════════════════════════════════════════════════════════════
# VISION AI — PROVIDER CHAIN
# ══════════════════════════════════════════════════════════════════

def encode_image_to_base64(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_vision_with_fallback(prompt, image_paths, default_response):
    try:
        images   = [PIL.Image.open(p) for p in image_paths]
        response = gemini_model.generate_content([prompt] + images)
        logger.info(f"GEMINI vision OK ({len(image_paths)} image(s))")
        return response.text
    except Exception as e:
        logger.warning(f"Gemini vision failed: {e} — switching to Groq")

    try:
        content = []
        for path in image_paths:
            b64 = encode_image_to_base64(path)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
        content.append({"type": "text", "text": prompt})
        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": content}],
            max_tokens=300, temperature=0.3
        )
        result = response.choices[0].message.content.strip()
        logger.info(f"GROQ vision OK: {result[:80]}")
        return result
    except Exception as e:
        logger.warning(f"Groq vision also failed: {e} — using default")

    return default_response


def analyze_photo(image_path, user_answers):
    prompt = (
        "You are an animal rescue triage assistant.\n\n"
        f"Reporter info:\n{user_answers}\n\n"
        "From the image:\n1. Animal seen?\n2. Matches description?\n3. Severity 1-10?\n"
        "4. Signs of distress?\n5. Urgency: HIGH / MEDIUM / LOW?\n\n"
        "IMPORTANT: Your entire response must be under 480 characters. "
        "Be extremely concise. No filler words. Facts only."
    )
    return call_vision_with_fallback(
        prompt=prompt,
        image_paths=[image_path],
        default_response="AI analysis unavailable. Urgency: HIGH"
    )


def compare_photos(report_path, completion_path):
    prompt = (
        "You are verifying an animal rescue.\n\n"
        "Image 1 is the ORIGINAL photo when the animal was reported injured.\n"
        "Image 2 is the COMPLETION photo sent by the volunteer after rescue.\n\n"
        "1. Do both images show the same animal?\n"
        "2. Does the animal in Image 2 appear safer or better?\n\n"
        "Reply ONLY one word:\n"
        "MATCH — same animal, appears helped\n"
        "NO_MATCH — different animal or unrelated photo\n"
        "UNCERTAIN — cannot determine clearly"
    )
    result = call_vision_with_fallback(
        prompt=prompt,
        image_paths=[report_path, completion_path],
        default_response="UNCERTAIN"
    )
    result = result.strip().upper()
    if "NO_MATCH" in result:  return "NO_MATCH"
    if "UNCERTAIN" in result: return "UNCERTAIN"
    return "MATCH"


def extract_urgency(text):
    t = text.upper()
    if "HIGH" in t:   return "HIGH"
    if "MEDIUM" in t: return "MEDIUM"
    return "LOW"


def is_valid_location(text):
    t = text.strip().lower()
    if len(t) < 10: return False
    invalid = ["here","nearby","near me","idk","not sure","somewhere","outside",
               "there","this place","same place","abc","xyz","test","na","n/a"]
    if any(t == w or t.startswith(w + " ") for w in invalid): return False
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
    except:
        return len(t) >= 20


def delete_case_photos(case_id):
    for path in [f"tg_report_{case_id}.jpg", f"tg_completion_{case_id}.jpg"]:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"Deleted: {path}")
        except Exception as e:
            logger.warning(f"Photo delete error {path}: {e}")


# ══════════════════════════════════════════════════════════════════
# WHATSAPP HELPER (for cross-platform admin notifications)
# ══════════════════════════════════════════════════════════════════

def send_whatsapp_message(to, message):
    if not to or not os.getenv("ACCESS_TOKEN") or not os.getenv("PHONE_NUMBER_ID"):
        return
    url = f"https://graph.facebook.com/v18.0/{os.getenv('PHONE_NUMBER_ID')}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message[:4096]},
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=(5, 20))
        logger.info(f"WA send to {to}: {r.status_code}")
    except Exception as e:
        logger.error(f"WA send error to {to}: {e}")


# ══════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Report Injured Animal",   callback_data="action_report")],
        [InlineKeyboardButton("🐾 Volunteer — Join / Status", callback_data="action_volunteer")],
        [InlineKeyboardButton("📋 Check Case Status",       callback_data="action_status")],
        [InlineKeyboardButton("ℹ️ About Anira",             callback_data="action_about")],
    ])

def yes_no_keyboard(key):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes",       callback_data=f"yn_{key}_YES"),
        InlineKeyboardButton("❌ No",        callback_data=f"yn_{key}_NO"),
        InlineKeyboardButton("🤷 Not Sure",  callback_data=f"yn_{key}_NOT_SURE"),
    ]])

def animal_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐕 Dog",   callback_data="animal_dog"),
         InlineKeyboardButton("🐈 Cat",   callback_data="animal_cat")],
        [InlineKeyboardButton("🐄 Cow",   callback_data="animal_cow"),
         InlineKeyboardButton("🐴 Horse", callback_data="animal_horse")],
        [InlineKeyboardButton("🐾 Other", callback_data="animal_other")],
    ])

def severity_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Low — Minor injury",              callback_data="sev_LOW")],
        [InlineKeyboardButton("🟡 Medium — Needs attention",        callback_data="sev_MEDIUM")],
        [InlineKeyboardButton("🔴 High — Critical / Life threatening", callback_data="sev_HIGH")],
    ])

def stay_leave_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🙋 STAY — I'm waiting",  callback_data="stay_STAY"),
        InlineKeyboardButton("🚶 LEAVE — I have to go", callback_data="stay_LEAVE"),
    ]])

def volunteer_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Apply as Volunteer", callback_data="vol_apply")],
        [InlineKeyboardButton("📊 Check My Status",    callback_data="vol_status")],
    ])

def confirm_rescue_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ YES — Animal is safe",  callback_data="confirm_YES"),
         InlineKeyboardButton("❌ NO — Something wrong",  callback_data="confirm_NO")],
        [InlineKeyboardButton("❓ UNSURE",                callback_data="confirm_UNSURE")],
    ])

def volunteer_action_keyboard(case_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Accept Case {case_id}", callback_data=f"vol_accept_{case_id}")],
    ])

def still_on_scene_keyboard(case_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 STILL ON SCENE",         callback_data=f"scene_{case_id}")],
        [InlineKeyboardButton(f"✅ COMPLETED {case_id}",   callback_data=f"done_{case_id}")],
    ])


# ══════════════════════════════════════════════════════════════════
# SEND HELPERS
# ══════════════════════════════════════════════════════════════════

async def send_tg(bot, chat_id, text, keyboard=None, parse_mode=None):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text[:4096],
            reply_markup=keyboard,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.error(f"TG send error to {chat_id}: {e}")

async def send_tg_photo(bot, chat_id, photo_path, caption=""):
    try:
        with open(photo_path, "rb") as f:
            await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
    except Exception as e:
        logger.error(f"TG photo send error to {chat_id}: {e}")

def tg_id_from_phone(phone):
    if phone and phone.startswith("tg_"):
        try: return int(phone[3:])
        except: return None
    return None


# ══════════════════════════════════════════════════════════════════
# ALERT VOLUNTEERS
# ══════════════════════════════════════════════════════════════════

async def alert_all_volunteers(bot, reporter_id, session, urgency, ai_analysis, case_id):
    volunteers = load_all_active_volunteers()
    if not volunteers:
        await send_tg(bot, reporter_id,
            "⚠️ No volunteers registered yet.\n\n📞 Animal Helpline: 1962\n📞 SPCA: 011-23619027"
        )
        return

    line = ("🔴 URGENT — IMMEDIATE RESPONSE" if urgency == "HIGH"
            else "🟡 MEDIUM — RESPOND SOON"   if urgency == "MEDIUM"
            else "🟢 LOW — MONITOR SITUATION")

    message = (
        f"🚨 RESCUE ALERT 🚨\n{line}\n\n"
        f"📋 Case ID: {case_id}\n\n"
        f"Animal: {session.get('animal','?')}\n"
        f"Severity: {session.get('severity','?')}\n"
        f"Bleeding: {session.get('bleeding','?')}\n"
        f"Can move: {session.get('can_move','?')}\n"
        f"Ground support: {session.get('ground_support','?')}\n"
        f"📍 Location: {session.get('location','?')}\n\n"
        f"AI Analysis:\n{ai_analysis}\n\n"
        f"Reported via: Telegram"
    )

    alerted     = []
    report_path = f"tg_report_{case_id}.jpg"

    for vol_phone, vol_data in volunteers.items():
        tg_id = tg_id_from_phone(vol_phone)
        # Also check if real-phone volunteer has a tg_id mapping
        if not tg_id:
            tg_id = get_tg_id_for_phone(vol_phone)
        if tg_id:
            try:
                await send_tg(bot, tg_id, message, keyboard=volunteer_action_keyboard(case_id))
                if os.path.exists(report_path):
                    await send_tg_photo(bot, tg_id, report_path, "📸 Reported animal photo")
                tg_active_cases[tg_id] = {"reporter": str(reporter_id), "case_id": case_id}
                alerted.append(vol_phone)
            except Exception as e:
                logger.error(f"Failed to alert TG volunteer {tg_id}: {e}")
        else:
            alerted.append(vol_phone)

    case = load_case(case_id)
    if case:
        case["alerted_volunteers"] = alerted
        save_case(case)

    threading.Timer(600, lambda: escalate_case_tg(case_id)).start()

    if ADMIN_TG_ID:
        try:
            await send_tg(bot, int(ADMIN_TG_ID),
                f"📋 NEW CASE (Telegram): {case_id}\n"
                f"Animal: {session.get('animal','?')} | {urgency}\n"
                f"Location: {session.get('location','?')}\n"
                f"Volunteers alerted: {len(alerted)}"
            )
        except: pass


def escalate_case_tg(case_id):
    case = load_case(case_id)
    if not case or case["status"] in ["ACCEPTED","COMPLETED"]:
        return
    logger.info(f"ESCALATING TG case {case_id}")


async def send_first_aid_tg(bot, chat_id, session):
    animal         = session.get("animal","animal").lower()
    bleeding       = session.get("bleeding","NO")
    can_move       = session.get("can_move","YES")
    severity_label = session.get("severity","MEDIUM")

    note = "⚠️ Serious case. Do not move the animal unless absolutely necessary.\n\n" if severity_label == "HIGH" else ""
    tips = []
    if bleeding == "YES": tips.append("🩸 Gentle pressure with clean cloth. Do not remove it.")
    if can_move == "NO":  tips.append("🚫 Do not lift or drag — can cause more injury.")
    if   animal == "dog":  tips += ["🐕 Keep people away.", "💧 Offer water only if conscious and calm."]
    elif animal == "cat":  tips += ["🐈 Very still and quiet near them.", "🧤 Loosely cover with cloth."]
    elif animal == "cow":  tips += ["🐄 Keep crowd away.", "☀️ Shade if in direct sun."]
    elif animal == "bird": tips += ["🐦 Loosely cover with cloth.", "🌡️ Keep warm — birds shock quickly."]
    else:                  tips += ["🐾 Stay calm, keep distance.", "👥 Ask bystanders to move away."]
    tips += ["📵 Low noise.", "🚫 No food or medicine without vet."]

    await send_tg(bot, chat_id,
        "🐾 *First Aid While You Wait:*\n\n" + note + "\n".join(tips) +
        "\n\nVolunteer being alerted. You'll hear when someone accepts.",
        parse_mode="Markdown"
    )
    await send_tg(bot, chat_id,
        "━━━━━━━━━━━━━━━\nCan you stay with the animal?",
        keyboard=stay_leave_keyboard()
    )


# ══════════════════════════════════════════════════════════════════
# GHOST VOLUNTEER TIMEOUT
# ══════════════════════════════════════════════════════════════════

async def warn_ghost_volunteer_tg(bot, case_id):
    case = load_case(case_id)
    if not case or case["status"] != "ACCEPTED":
        return

    vol_phone = case.get("volunteer_number")
    urgency   = case.get("urgency","MEDIUM")
    tg_id     = tg_id_from_phone(vol_phone) or get_tg_id_for_phone(vol_phone)
    if not tg_id:
        return

    tg_pending_transfer[tg_id] = {
        "case_id":   case_id,
        "warned_at": datetime.now().isoformat(),
        "urgency":   urgency,
    }

    await send_tg(bot, tg_id,
        f"⚠️ CASE UPDATE REQUIRED — {case_id}\n\n"
        f"You accepted this rescue {10 if urgency == 'HIGH' else 25} minutes ago "
        f"and no completion has been logged.\n\n"
        "Your case is being transferred in 2 minutes unless you reply.",
        keyboard=still_on_scene_keyboard(case_id)
    )

    import asyncio
    await asyncio.sleep(120)
    await reopen_stale_case_tg(bot, case_id)


async def reopen_stale_case_tg(bot, case_id):
    case = load_case(case_id)
    if not case or case["status"] != "ACCEPTED":
        return

    stale_phone = case.get("volunteer_number")
    stale_tg    = tg_id_from_phone(stale_phone) or get_tg_id_for_phone(stale_phone)
    reporter    = case.get("reporter")

    tg_pending_transfer.pop(stale_tg, None)

    case["status"]           = "PENDING"
    case["volunteer"]        = None
    case["volunteer_number"] = None
    case["time_accepted"]    = None
    save_case(case)

    if stale_tg:
        tg_active_cases.pop(stale_tg, None)
        tg_pending_outcome.pop(stale_tg, None)
        await send_tg(bot, stale_tg,
            f"🔄 Case {case_id} has been transferred.\nYou did not respond in time."
        )

    reporter_tg = tg_id_from_phone(reporter) if reporter and reporter.startswith("tg_") else None
    if reporter_tg:
        await send_tg(bot, reporter_tg,
            f"🔄 Update on {case_id}:\nOur volunteer was unable to complete in time.\n"
            "We are alerting backup volunteers now."
        )

    volunteers = load_all_active_volunteers()
    for vol_phone, vol_data in volunteers.items():
        if vol_phone == stale_phone:
            continue
        tg_id = tg_id_from_phone(vol_phone) or get_tg_id_for_phone(vol_phone)
        if tg_id:
            await send_tg(bot, tg_id,
                f"🔄 REACTIVATED CASE — {case_id}\n\n"
                f"Previous volunteer did not respond.\n\n"
                f"Animal: {case['animal']}\nSeverity: {case['severity']}\n"
                f"📍 {case['location']}",
                keyboard=volunteer_action_keyboard(case_id)
            )
            tg_active_cases[tg_id] = {"reporter": reporter, "case_id": case_id}


# ══════════════════════════════════════════════════════════════════
# FINALIZE CASE
# ══════════════════════════════════════════════════════════════════

async def finalize_case_tg(bot, vol_tg_id, case_id, note, was_accepted, photo_result):
    case = load_case(case_id)
    if not case or case["status"] == "COMPLETED":
        return

    case["status"]           = "COMPLETED"
    case["time_completed"]   = datetime.now().strftime("%d %b %Y, %I:%M %p")
    case["completion_photo"] = True
    if note: case["outcome"] = note
    save_case(case)
    tg_pending_outcome.pop(vol_tg_id, None)

    reporter  = case["reporter"]
    vol_name  = case.get("volunteer","Volunteer")
    comp_path = f"tg_completion_{case_id}.jpg"

    await send_tg(bot, vol_tg_id,
        f"✅ Case {case_id} — completion photo received.\n\n"
        "Thank you for showing up. 🐾\nYou made a real difference. 💚"
    )

    reporter_tg = tg_id_from_phone(reporter) if reporter and reporter.startswith("tg_") else None
    if reporter_tg:
        if os.path.exists(comp_path):
            await send_tg_photo(bot, reporter_tg, comp_path,
                f"📸 Volunteer {vol_name} completed rescue for case {case_id}."
            )
        await send_tg(bot, reporter_tg,
            f"🐾 Rescue update for case {case_id}:\n\n"
            f"Volunteer {vol_name} has marked this rescue as complete.\n\n"
            "Please confirm — does the animal look safe?",
            keyboard=confirm_rescue_keyboard()
        )
        tg_pending_confirm[reporter_tg] = {
            "case_id":        case_id,
            "volunteer_name": vol_name,
            "vol_tg_id":      vol_tg_id,
            "vol_phone":      case.get("volunteer_number"),
            "was_accepted":   was_accepted,
            "photo_result":   photo_result,
            "photo_missing":  False,
        }

    if photo_result == "NO_MATCH" and ADMIN_TG_ID:
        await send_tg(bot, int(ADMIN_TG_ID),
            f"🚨 PHOTO MISMATCH: {case_id}\n"
            f"Volunteer: {vol_name}\n"
            f"Animal: {case['animal']} at {case['location']}"
        )

    if was_accepted:
        increment_rescues(case.get("volunteer_number",""))

    threading.Timer(7200, delete_case_photos, args=[case_id]).start()


# ══════════════════════════════════════════════════════════════════
# QUESTION FLOW
# ══════════════════════════════════════════════════════════════════

SEVERITY_MAP = {"LOW": 3, "MEDIUM": 5, "HIGH": 9}

async def ask_next_question(bot, chat_id, session):
    stage = session.get("flow_stage","animal")

    if stage == "animal":
        await send_tg(bot, chat_id,
            "Which animal is it?\n\n👇 Please tap a button below:",
            keyboard=animal_keyboard()
        )
    elif stage == "bleeding":
        await send_tg(bot, chat_id,
            "Is the animal bleeding?\n\n👇 Please tap a button below:",
            keyboard=yes_no_keyboard("bleeding")
        )
    elif stage == "can_move":
        await send_tg(bot, chat_id,
            "Can the animal move on its own?\n\n👇 Please tap a button below:",
            keyboard=yes_no_keyboard("can_move")
        )
    elif stage == "severity":
        await send_tg(bot, chat_id,
            "How serious is the animal's condition?\n\n👇 Please tap a button below:",
            keyboard=severity_keyboard()
        )
    elif stage == "wounds":
        sev = session.get("severity_num", 5)
        if sev >= 4:
            await send_tg(bot, chat_id,
                "Are there any visible wounds or injuries?\n\n👇 Please tap a button below:",
                keyboard=yes_no_keyboard("wounds")
            )
        else:
            session["wounds"]     = "Not checked"
            session["flow_stage"] = "eating"
            tg_sessions[chat_id]  = session
            await ask_next_question(bot, chat_id, session)
    elif stage == "eating":
        sev = session.get("severity_num", 5)
        if sev < 7:
            await send_tg(bot, chat_id,
                "Is the animal eating or drinking?\n\n👇 Please tap a button below:",
                keyboard=yes_no_keyboard("eating")
            )
        else:
            session["eating"]     = "Not checked"
            session["flow_stage"] = "ground_support"
            tg_sessions[chat_id]  = session
            await ask_next_question(bot, chat_id, session)
    elif stage == "ground_support":
        await send_tg(bot, chat_id,
            "Is there anyone with the animal right now?\n\n👇 Please tap a button below:",
            keyboard=yes_no_keyboard("ground_support")
        )
    elif stage == "location":
        await send_tg(bot, chat_id,
            "📍 Please share the exact location of the animal.\n\n"
            "You can:\n"
            "• Send your *live location* using the 📎 attachment button\n"
            "• Or type the address with area name + landmark + city\n\n"
            "Example: Near Sector 5 Metro, Rohini, New Delhi\n\n"
            "⚠️ Accurate location = faster rescue.",
            parse_mode="Markdown"
        )
        session["flow_stage"] = "location"
        tg_sessions[chat_id]  = session
    elif stage == "photo":
        await send_tg(bot, chat_id,
            "📸 Almost done!\n\nPlease send a clear photo of the animal.\n\n"
            "Tips:\n• Get as close as safely possible\n"
            "• Make sure the animal is clearly in frame\n"
            "• Good lighting helps AI analysis"
        )
        session["flow_stage"] = "photo"
        tg_sessions[chat_id]  = session


def get_next_flow_stage(session):
    sev  = session.get("severity_num", 5)
    flow = ["animal","bleeding","can_move","severity"]
    if sev >= 4:
        flow += ["wounds","eating"]
    flow += ["ground_support","location","photo"]
    current = session.get("flow_stage","animal")
    try:
        idx = flow.index(current)
        if idx + 1 < len(flow):
            return flow[idx + 1]
    except ValueError:
        pass
    return "photo"


# ══════════════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_blocked_tg(chat_id): return
    if is_rate_limited(chat_id): return

    await send_tg(context.bot, chat_id,
        "🐾 *Welcome to Anira*\n\n"
        "Anira is an AI-powered animal rescue network.\n"
        "Report injured animals. Connect with volunteers. Save lives.\n\n"
        "What would you like to do?",
        keyboard=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await send_tg(context.bot, chat_id,
        "🐾 *Anira Help*\n\n"
        "Commands:\n"
        "/start — Main menu\n"
        "/report — Report an injured animal\n"
        "/status — Check your case status\n"
        "/vstatus — Check volunteer status\n"
        "/help — Show this message",
        parse_mode="Markdown",
        keyboard=main_menu_keyboard()
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()
    text_up = text.upper()

    if is_blocked_tg(chat_id): return
    if is_rate_limited(chat_id): return

    session = tg_sessions.get(chat_id, {})
    stage   = session.get("stage","")
    flow    = session.get("flow_stage","")

    # ── V2 INPUT VALIDATION: reject text during button-only stages ──
    if flow in BUTTON_ONLY_STAGES:
        await send_tg(context.bot, chat_id,
            "⚠️ Please use the buttons to answer — don't type.\n\nTap one of the options below:"
        )
        await ask_next_question(context.bot, chat_id, session)
        return

    # ── STATUS ──
    if text_up.startswith("STATUS"):
        await handle_status_command(update, context)
        return

    # ── COMPLETED ──
    if text_up.startswith("COMPLETED"):
        parts = text_up.split()
        if len(parts) >= 2:
            await handle_completed_command(chat_id, parts[1], context.bot)
        else:
            await send_tg(context.bot, chat_id,
                "Please include Case ID.\nExample: COMPLETED CASE-XXXX"
            )
        return

    # ── RESPONDING ──
    if text_up.startswith("RESPONDING"):
        parts            = text_up.split()
        case_id_provided = parts[1] if len(parts) >= 2 else None
        await handle_responding_text(chat_id, case_id_provided, context.bot)
        return

    # ── Reporter confirmation ──
    if chat_id in tg_pending_confirm:
        reply_up = text_up.strip(".,!?")
        handled  = await handle_reporter_confirm(chat_id, reply_up, context.bot)
        if handled: return

    # ── Grace period intercept ──
    if chat_id in tg_pending_transfer:
        data    = tg_pending_transfer[chat_id]
        cid     = data.get("case_id")
        urgency = data.get("urgency","MEDIUM")
        tg_pending_transfer.pop(chat_id, None)
        await send_tg(context.bot, chat_id,
            f"✅ Got it — you are still active on case {cid}.\n\n"
            f"Please complete the rescue and send:\nCOMPLETED {cid}",
            keyboard=still_on_scene_keyboard(cid)
        )
        ext = 600 if urgency == "HIGH" else 1500
        threading.Timer(ext,
            lambda: context.application.create_task(
                warn_ghost_volunteer_tg(context.bot, cid)
            )
        ).start()
        return

    # ── Outcome note ──
    if chat_id in tg_pending_outcome:
        data = tg_pending_outcome[chat_id]
        if not text_up.startswith("COMPLETED"):
            note = text.strip()[:500]
            if len(note) >= 5:
                data["note"]              = note
                tg_pending_outcome[chat_id] = data
                await send_tg(context.bot, chat_id,
                    f"✅ Note saved.\nWhen done: COMPLETED {data['case_id']}"
                )
                return

    # ── Location stage ──
    if flow == "location":
        if not is_valid_location(text):
            await send_tg(context.bot, chat_id,
                "⚠️ Location not accepted.\n\n"
                "Please provide a proper address with:\n"
                "• Area or colony name\n• Nearby landmark\n• City name\n\n"
                "Example: Near Sector 5 Metro, Rohini, New Delhi\n\n"
                "Or tap the 📎 button and send your live location."
            )
            return
        session["location"]   = text
        session["flow_stage"] = "photo"
        tg_sessions[chat_id]  = session
        await send_tg(context.bot, chat_id,
            "📍 Location confirmed!\n\nNow send a clear photo of the animal 📸"
        )
        return

    # ── Photo stage ──
    if flow == "photo":
        await send_tg(context.bot, chat_id,
            "📸 Please send a photo of the animal to continue.\n"
            "Use the attachment button to send an image."
        )
        return

    # ── Volunteer registration: name ──
    if stage == "vol_entering_name":
        name = text.strip()
        if len(name) < 2:
            await send_tg(context.bot, chat_id, "Please enter your full name.")
            return
        session["vol_name"] = name
        session["stage"]    = "vol_entering_city"
        tg_sessions[chat_id] = session
        await send_tg(context.bot, chat_id,
            f"Got it, {name}!\n\nWhich city are you based in?\n\n(Type your city name)"
        )
        return

    # ── Volunteer registration: city ──
    elif stage == "vol_entering_city":
        session["vol_city"] = text.strip()
        session["stage"]    = "vol_entering_phone"
        tg_sessions[chat_id] = session
        await send_tg(context.bot, chat_id,
            "Please share your *WhatsApp number* so we can contact you for verification.\n\n"
            "Format: 91XXXXXXXXXX (with country code, no + or spaces)\n\n"
            "Example: 919876543210",
            parse_mode="Markdown"
        )
        return

    # ── Volunteer registration: WhatsApp number ──
    elif stage == "vol_entering_phone":
        wa_number = text.strip().replace("+","").replace(" ","").replace("-","")
        if not wa_number.isdigit() or len(wa_number) < 10:
            await send_tg(context.bot, chat_id,
                "⚠️ Invalid number. Please enter your WhatsApp number with country code.\n\n"
                "Example: 919876543210"
            )
            return

        name = session.get("vol_name","")
        city = session.get("vol_city","")

        # V2: Approval sync — check if already registered or pending
        existing_status = get_volunteer_status(wa_number)
        if existing_status == "active":
            session["stage"] = ""
            tg_sessions[chat_id] = session
            await send_tg(context.bot, chat_id,
                "✅ This WhatsApp number is already an approved volunteer!\n\n"
                "You will receive rescue alerts on WhatsApp.\n\n"
                "If you want Telegram alerts too, contact: contact.animitr@gmail.com",
                keyboard=main_menu_keyboard()
            )
            return
        if existing_status == "pending":
            session["stage"] = ""
            tg_sessions[chat_id] = session
            await send_tg(context.bot, chat_id,
                "⏳ An application for this WhatsApp number is already under review.\n\n"
                "You'll be contacted for verification. Usually 1-3 days.",
                keyboard=main_menu_keyboard()
            )
            return

        # Save application with real WA phone + tg_id mapping
        save_application(wa_number, name, city, "community", tg_id=chat_id)
        session["stage"] = ""
        tg_sessions[chat_id] = session

        await send_tg(context.bot, chat_id,
            f"🐾 Thank you for applying, {name}!\n\n"
            "Your application has been received.\n\n"
            "Next steps:\n"
            "→ Our team will review your application\n"
            "→ We will call you on your WhatsApp for verification\n"
            "→ Once verified, you will be approved and added to the network\n\n"
            "This usually takes 1-3 days.\n\n"
            "Use /vstatus anytime to check your application status.",
            keyboard=main_menu_keyboard()
        )

        # V2: Notify admin on BOTH WhatsApp AND Telegram
        admin_msg = (
            f"🆕 NEW VOLUNTEER APPLICATION (Telegram)\n\n"
            f"Name: {name}\n"
            f"City: {city}\n"
            f"WhatsApp: +{wa_number}\n"
            f"Telegram ID: {chat_id}\n\n"
            f"After KYC call:\n"
            f"APPROVE {wa_number}\n"
            f"REJECT {wa_number}"
        )

        if ADMIN_NUMBER:
            send_whatsapp_message(ADMIN_NUMBER, admin_msg)

        if ADMIN_TG_ID:
            await send_tg(context.bot, int(ADMIN_TG_ID), admin_msg)

        return

    # ── Case ID for status ──
    if stage == "waiting_case_id":
        await show_case_status(chat_id, text.strip().upper(), context.bot)
        session["stage"] = ""
        tg_sessions[chat_id] = session
        return

    # ── Default ──
    await send_tg(context.bot, chat_id,
        "Tap a button below or use /start to see the menu.",
        keyboard=main_menu_keyboard()
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_blocked_tg(chat_id): return
    if is_rate_limited(chat_id): return

    session    = tg_sessions.get(chat_id, {})
    flow       = session.get("flow_stage","")
    tg_phone   = f"tg_{chat_id}"
    real_phone = get_tg_id_for_chat(chat_id)

    # ── Completion photo ──
    if chat_id in tg_pending_photo:
        data    = tg_pending_photo.pop(chat_id, {})
        cid     = data.get("case_id")
        note    = data.get("note")
        was_acc = data.get("was_accepted", True)
        if cid:
            comp_path = f"tg_completion_{cid}.jpg"
            photo     = update.message.photo[-1]
            file      = await context.bot.get_file(photo.file_id)
            await file.download_to_drive(comp_path)
            await send_tg(context.bot, chat_id, "📸 Photo received. Running verification...")
            report_path  = f"tg_report_{cid}.jpg"
            photo_result = compare_photos(report_path, comp_path) if os.path.exists(report_path) else "UNCERTAIN"
            await finalize_case_tg(context.bot, chat_id, cid, note, was_acc, photo_result)
        return

    # ── Progress photo from volunteer ──
    volunteers = load_all_active_volunteers()
    is_vol     = tg_phone in volunteers or (real_phone and real_phone in volunteers)
    if is_vol and chat_id in tg_pending_outcome:
        od  = tg_pending_outcome.get(chat_id, {})
        cid = od.get("case_id")
        if cid:
            path  = f"tg_completion_{cid}.jpg"
            photo = update.message.photo[-1]
            file  = await context.bot.get_file(photo.file_id)
            await file.download_to_drive(path)
            case = load_case(cid)
            if case:
                reporter    = case["reporter"]
                reporter_tg = tg_id_from_phone(reporter) if reporter.startswith("tg_") else None
                if reporter_tg:
                    await send_tg_photo(context.bot, reporter_tg, path,
                        "📸 Progress photo from your volunteer"
                    )
            await send_tg(context.bot, chat_id, "✅ Progress photo shared with the reporter.")
        return

    # ── V2: Block photo during button stages ──
    if flow in BUTTON_ONLY_STAGES:
        await send_tg(context.bot, chat_id,
            "⚠️ Please answer the current question using the buttons first."
        )
        await ask_next_question(context.bot, chat_id, session)
        return

    if flow != "photo":
        await send_tg(context.bot, chat_id,
            "Please answer all questions first before sending a photo."
        )
        return

    await send_tg(context.bot, chat_id, "📸 Photo received. Analysing with AI...")

    photo     = update.message.photo[-1]
    file      = await context.bot.get_file(photo.file_id)
    temp_path = f"tg_temp_{chat_id}.jpg"
    await file.download_to_drive(temp_path)

    user_answers = (
        f"Animal: {session.get('animal','?')}\n"
        f"Severity: {session.get('severity','?')}\n"
        f"Bleeding: {session.get('bleeding','?')}\n"
        f"Can move: {session.get('can_move','?')}\n"
        f"Wounds: {session.get('wounds','?')}\n"
        f"Eating: {session.get('eating','?')}\n"
        f"Ground support: {session.get('ground_support','?')}"
    )

    ai_analysis    = analyze_photo(temp_path, user_answers)
    urgency        = extract_urgency(ai_analysis)
    reporter_phone = tg_phone  # Reporter stored as tg_{chat_id}

    if count_active_cases_for_reporter(reporter_phone) >= 1:
        conn = get_db(); cur = conn.cursor()
        cur.execute(
            "SELECT case_id FROM cases WHERE reporter=%s AND status IN ('PENDING','ACCEPTED') ORDER BY time_reported DESC LIMIT 1;",
            (reporter_phone,)
        )
        row = cur.fetchone(); cur.close(); conn.close()
        existing = row["case_id"] if row else "your existing case"
        await send_tg(context.bot, chat_id,
            f"⚠️ You already have an active rescue case: {existing}\n\n"
            "Please wait for it to complete before reporting another."
        )
        if os.path.exists(temp_path): os.remove(temp_path)
        return

    sev_num = session.get("severity_num", 5)
    case_id = generate_case_id()
    case    = {
        "case_id": case_id, "reporter": reporter_phone,
        "animal":  session.get("animal","Unknown"),
        "severity": str(sev_num),
        "location": session.get("location","Not shared"),
        "bleeding": session.get("bleeding","?"),
        "can_move": session.get("can_move","?"),
        "urgency":  urgency,
        "status": "PENDING", "volunteer": None, "volunteer_number": None,
        "alerted_volunteers": [], "outcome": None, "completion_photo": False,
        "time_reported": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "time_accepted": None, "time_completed": None,
    }
    save_case(case)

    import shutil
    report_path = f"tg_report_{case_id}.jpg"
    try:
        shutil.copy(temp_path, report_path)
        os.remove(temp_path)
    except Exception as e:
        logger.warning(f"Report photo copy error: {e}")

    session["case_id"]    = case_id
    session["stage"]      = "waiting"
    session["flow_stage"] = "done"
    tg_sessions[chat_id]  = session

    await send_tg(context.bot, chat_id,
        f"📋 *Your Case ID: {case_id}*\n\nSave this to check status anytime.\n\nUse: /status",
        parse_mode="Markdown"
    )

    urgency_msgs = {
        "HIGH":   "🚨 HIGH URGENCY case created.\n\nDispatched to rescue team immediately.\nPlease stay with the animal if safe.",
        "MEDIUM": "✅ Report dispatched to rescue team.\n\nA volunteer will respond soon.",
        "LOW":    "✅ Report sent to rescue team.\n\nA volunteer will check on the animal.",
    }
    await send_tg(context.bot, chat_id, urgency_msgs.get(urgency, urgency_msgs["MEDIUM"]))
    await send_first_aid_tg(context.bot, chat_id, session)

    import asyncio
    asyncio.create_task(
        alert_all_volunteers(context.bot, chat_id, session, urgency, ai_analysis, case_id)
    )
    threading.Timer(86400, delete_case_photos, args=[case_id]).start()


async def handle_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = tg_sessions.get(chat_id, {})
    flow    = session.get("flow_stage","")

    if flow != "location":
        await send_tg(context.bot, chat_id, "Please complete the questions first.")
        return

    lat = update.message.location.latitude
    lng = update.message.location.longitude
    session["location"]   = f"https://maps.google.com/?q={lat},{lng}"
    session["flow_stage"] = "photo"
    tg_sessions[chat_id]  = session

    await send_tg(context.bot, chat_id,
        "📍 Live location received!\n\nNow send a clear photo of the animal 📸"
    )


# ══════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    data    = query.data

    await query.answer()

    if is_blocked_tg(chat_id): return
    if is_rate_limited(chat_id): return

    session = tg_sessions.get(chat_id, {})

    # ── MAIN MENU ──
    if data == "action_report":
        session = {"stage": "reporting", "flow_stage": "animal"}
        tg_sessions[chat_id] = session
        await send_tg(context.bot, chat_id,
            "🚨 *ANIMAL RESCUE REPORT*\n\n"
            "Your report is being registered. False reports result in action.\n\n"
            "Genuine emergency only. Let's get started.\n\n"
            "Please use the buttons to answer each question:",
            parse_mode="Markdown"
        )
        await ask_next_question(context.bot, chat_id, session)
        return

    if data == "action_volunteer":
        await send_tg(context.bot, chat_id,
            "🐾 *Volunteer with Anira*\n\nWhat would you like to do?",
            keyboard=volunteer_menu_keyboard(),
            parse_mode="Markdown"
        )
        return

    if data == "action_status":
        session["stage"] = "waiting_case_id"
        tg_sessions[chat_id] = session
        await send_tg(context.bot, chat_id,
            "Please type your Case ID.\nExample: CASE-1704-AB12CD"
        )
        return

    if data == "action_about":
        await send_tg(context.bot, chat_id,
            "🐾 *About Anira*\n\n"
            "Anira is part of the Animitr platform — an AI-powered animal rescue network.\n\n"
            "• Report injured animals via WhatsApp or Telegram\n"
            "• AI photo analysis for triage\n"
            "• Volunteer dispatch system\n"
            "• Real-time case tracking\n\n"
            "Website: animitr.org",
            parse_mode="Markdown",
            keyboard=main_menu_keyboard()
        )
        return

    # ── ANIMAL ──
    if data.startswith("animal_"):
        animal_map = {
            "animal_dog": "Dog", "animal_cat": "Cat",
            "animal_cow": "Cow", "animal_horse": "Horse", "animal_other": "Other"
        }
        animal               = animal_map.get(data, "Other")
        session["animal"]    = animal
        session["flow_stage"] = get_next_flow_stage(session)
        tg_sessions[chat_id] = session
        await send_tg(context.bot, chat_id, f"✅ Animal: {animal}")
        await ask_next_question(context.bot, chat_id, session)
        return

    # ── YES/NO ──
    if data.startswith("yn_"):
        parts     = data.split("_", 2)
        key       = parts[1]
        value     = parts[2]
        label_map = {"YES":"Yes ✅", "NO":"No ❌", "NOT_SURE":"Not Sure 🤷"}
        session[key]          = value
        session["flow_stage"] = get_next_flow_stage(session)
        tg_sessions[chat_id]  = session
        await send_tg(context.bot, chat_id,
            f"✅ {key.replace('_',' ').title()}: {label_map.get(value,value)}"
        )
        await ask_next_question(context.bot, chat_id, session)
        return

    # ── SEVERITY ──
    if data.startswith("sev_"):
        sev_label               = data.replace("sev_","")
        sev_num                 = SEVERITY_MAP.get(sev_label, 5)
        session["severity"]     = sev_label
        session["severity_num"] = sev_num
        session["flow_stage"]   = get_next_flow_stage(session)
        tg_sessions[chat_id]    = session
        await send_tg(context.bot, chat_id, f"✅ Severity: {sev_label}")
        await ask_next_question(context.bot, chat_id, session)
        return

    # ── STAY/LEAVE ──
    if data.startswith("stay_"):
        choice = data.replace("stay_","")
        if choice == "STAY":
            tg_waiting_reporters[chat_id] = True
            await send_tg(context.bot, chat_id,
                "🙏 Thank you for staying with the animal.\n"
                "You'll be notified the moment a volunteer accepts."
            )
        else:
            tg_waiting_reporters[chat_id] = False
            await send_tg(context.bot, chat_id,
                "Understood. Help is on the way. 🐾\n"
                "You'll receive an update when a volunteer reaches the animal."
            )
        session["stage"]      = "waiting"
        session["flow_stage"] = "done"
        tg_sessions[chat_id]  = session
        return

    # ── VOLUNTEER MENU ──
    if data == "vol_apply":
        status, _ = get_status_for_tg_user(chat_id)
        if status == "active":
            await send_tg(context.bot, chat_id,
                "✅ You are already an active Anira volunteer!\n\n"
                "You will receive rescue alerts.\n\n"
                "Commands:\n• RESPONDING CASE-XXXX — accept a case\n"
                "• COMPLETED CASE-XXXX — close a case"
            )
            return
        if status == "pending":
            await send_tg(context.bot, chat_id,
                "⏳ Your application is already under review.\n\n"
                "We'll call you on WhatsApp for verification. Usually 1-3 days."
            )
            return
        session["stage"] = "vol_entering_name"
        tg_sessions[chat_id] = session
        await send_tg(context.bot, chat_id,
            "Great! Let's get you registered as a volunteer.\n\n"
            "First, what is your *full name*?",
            parse_mode="Markdown"
        )
        return

    if data == "vol_status":
        status, _ = get_status_for_tg_user(chat_id)
        msgs = {
            "active":    "✅ You are an active Anira volunteer.\n\nYou will receive rescue alerts.",
            "pending":   "⏳ Your application is under review.\n\nWe'll call you on WhatsApp for verification. Usually 1-3 days.",
            "rejected":  "Your application was not approved.\n\nContact: contact.animitr@gmail.com",
            "inactive":  "Your account is inactive.\n\nContact: contact.animitr@gmail.com",
            "not_found": "You are not registered.\n\nTap 'Apply as Volunteer' to get started.",
        }
        await send_tg(context.bot, chat_id,
            msgs.get(status, msgs["not_found"]),
            keyboard=main_menu_keyboard()
        )
        return

    # ── VOLUNTEER ACCEPT CASE ──
    if data.startswith("vol_accept_"):
        case_id    = data.replace("vol_accept_","")
        volunteers = load_all_active_volunteers()

        # Find volunteer phone for this TG user
        tg_phone   = f"tg_{chat_id}"
        real_phone = get_tg_id_for_chat(chat_id)
        vol_phone  = None
        vol_name   = None

        if tg_phone in volunteers:
            vol_phone = tg_phone
            vol_name  = volunteers[tg_phone]["name"]
        elif real_phone and real_phone in volunteers:
            vol_phone = real_phone
            vol_name  = volunteers[real_phone]["name"]

        if not vol_phone:
            await send_tg(context.bot, chat_id,
                "You are not a registered volunteer.\n\nUse /start to apply."
            )
            return

        case = load_case(case_id)
        if not case:
            await send_tg(context.bot, chat_id, f"Case {case_id} not found.")
            return
        if case["status"] == "COMPLETED":
            await send_tg(context.bot, chat_id, f"Case {case_id} is already completed. 🐾")
            return
        if case["status"] == "ACCEPTED" and case.get("volunteer_number") != vol_phone:
            await send_tg(context.bot, chat_id,
                f"Case {case_id} has already been accepted by another volunteer."
            )
            return

        case["status"]           = "ACCEPTED"
        case["volunteer"]        = vol_name
        case["volunteer_number"] = vol_phone
        case["time_accepted"]    = datetime.now().strftime("%d %b %Y, %I:%M %p")
        save_case(case)

        tg_pending_outcome[chat_id] = {"case_id": case_id, "note": None}
        tg_active_cases.pop(chat_id, None)

        await send_tg(context.bot, chat_id,
            f"✅ Case accepted — you are now the assigned rescuer.\n\n"
            f"📋 {case_id}\n"
            f"📍 {case['location']}\n"
            f"Animal: {case['animal']} | Severity: {case['severity']}\n\n"
            f"📝 Send an outcome note anytime.\n"
            f"When done: COMPLETED {case_id}\n\n"
            f"⚠️ You MUST send a completion photo when done."
        )

        reporter    = case["reporter"]
        reporter_tg = tg_id_from_phone(reporter) if reporter and reporter.startswith("tg_") else None
        if reporter_tg:
            await send_tg(context.bot, reporter_tg,
                f"🐾 A volunteer has accepted your rescue case!\n\n"
                f"Volunteer: {vol_name}\n\n"
                "They are heading to the location now.\n\n"
                "You will receive a photo when the rescue is complete."
            )

        urgency = case.get("urgency","MEDIUM")
        delay   = 600 if urgency == "HIGH" else 1500
        threading.Timer(delay,
            lambda: context.application.create_task(
                warn_ghost_volunteer_tg(context.bot, case_id)
            )
        ).start()
        return

    # ── STILL ON SCENE ──
    if data.startswith("scene_"):
        case_id = data.replace("scene_","")
        case    = load_case(case_id)
        if not case or case["status"] != "ACCEPTED":
            await send_tg(context.bot, chat_id, f"Case {case_id} is no longer active.")
            return
        urgency = case.get("urgency","MEDIUM")
        tg_pending_transfer.pop(chat_id, None)
        await send_tg(context.bot, chat_id,
            f"✅ Got it — one-time extension granted.\n\n"
            f"Extension: {'10 minutes' if urgency == 'HIGH' else '25 minutes'}\n\n"
            f"Complete with: COMPLETED {case_id}"
        )
        reporter    = case["reporter"]
        reporter_tg = tg_id_from_phone(reporter) if reporter and reporter.startswith("tg_") else None
        if reporter_tg:
            await send_tg(context.bot, reporter_tg,
                f"🔄 Update on {case_id}:\nYour volunteer confirmed they are still at the scene."
            )
        ext = 600 if urgency == "HIGH" else 1500
        threading.Timer(ext,
            lambda: context.application.create_task(
                reopen_stale_case_tg(context.bot, case_id)
            )
        ).start()
        return

    # ── DONE via button ──
    if data.startswith("done_"):
        case_id = data.replace("done_","")
        tg_pending_transfer.pop(chat_id, None)
        await handle_completed_command(chat_id, case_id, context.bot)
        return

    # ── REPORTER CONFIRMATION ──
    if data.startswith("confirm_"):
        reply = data.replace("confirm_","")
        await handle_reporter_confirm(chat_id, reply, context.bot)
        return


# ══════════════════════════════════════════════════════════════════
# STATUS / COMPLETED / RESPONDING
# ══════════════════════════════════════════════════════════════════

async def handle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    phone   = f"tg_{chat_id}"
    conn    = get_db(); cur = conn.cursor()
    cur.execute(
        "SELECT case_id FROM cases WHERE reporter=%s ORDER BY time_reported DESC LIMIT 1;",
        (phone,)
    )
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        await send_tg(context.bot, chat_id,
            "No case found for your account.\n\nUse /report to report an animal."
        )
        return
    await show_case_status(chat_id, row["case_id"], context.bot)


async def show_case_status(chat_id, case_id, bot):
    case = load_case(case_id)
    if not case:
        await send_tg(bot, chat_id, f"Case {case_id} not found.")
        return

    status_text = (
        "⏳ Waiting for volunteer" if case["status"] == "PENDING"
        else f"🚑 Volunteer {case['volunteer']} is on the way" if case["status"] == "ACCEPTED"
        else "✅ Rescue completed"
    )
    msg = (
        f"📋 *CASE STATUS*\n\n"
        f"Case ID: `{case_id}`\n"
        f"Animal: {case['animal']}\n"
        f"Location: {case['location']}\n"
        f"Severity: {case['severity']}\n"
        f"Reported: {case['time_reported']}\n\n"
        f"Status: {status_text}\n"
    )
    if case.get("time_accepted"):  msg += f"Accepted: {case['time_accepted']}\n"
    if case.get("time_completed"): msg += f"Completed: {case['time_completed']}\n"
    if case.get("outcome"):        msg += f"\n📝 Outcome: {case['outcome']}"

    await send_tg(bot, chat_id, msg, parse_mode="Markdown")


async def handle_completed_command(chat_id, case_id, bot):
    tg_phone   = f"tg_{chat_id}"
    real_phone = get_tg_id_for_chat(chat_id)
    volunteers = load_all_active_volunteers()

    vol_phone = None
    if tg_phone in volunteers:
        vol_phone = tg_phone
    elif real_phone and real_phone in volunteers:
        vol_phone = real_phone

    case = load_case(case_id)
    if not case:
        await send_tg(bot, chat_id, f"Case {case_id} not found.")
        return
    if case["status"] == "COMPLETED":
        await send_tg(bot, chat_id, f"Case {case_id} is already completed. 🐾")
        return

    assigned = case.get("volunteer_number")
    if assigned not in [tg_phone, vol_phone]:
        await send_tg(bot, chat_id,
            "❌ You are not the assigned volunteer for this case."
        )
        return

    note_data = tg_pending_outcome.get(chat_id, {})
    note      = note_data.get("note") if isinstance(note_data, dict) else None

    tg_pending_photo[chat_id] = {
        "case_id":      case_id,
        "note":         note,
        "was_accepted": (case["status"] == "ACCEPTED"),
    }

    await send_tg(bot, chat_id,
        f"✅ Almost done — one last step.\n\n"
        f"Please send a photo of the animal to close case {case_id}.\n\n"
        "📸 This is mandatory. Send within 30 minutes."
    )

    threading.Timer(1800, lambda: context_photo_deadline(chat_id, case_id)).start()


def context_photo_deadline(chat_id, case_id):
    if chat_id not in tg_pending_photo:
        return
    data = tg_pending_photo.pop(chat_id, {})
    if data.get("case_id") != case_id:
        return
    case = load_case(case_id)
    if not case or case["status"] == "COMPLETED":
        return
    note = data.get("note")
    case["status"]         = "COMPLETED"
    case["time_completed"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    if note: case["outcome"] = note
    save_case(case)
    tg_pending_outcome.pop(chat_id, None)
    add_photo_warning(f"tg_{chat_id}")
    logger.warning(f"Photo deadline missed: {case_id} by tg_{chat_id}")


async def handle_responding_text(chat_id, case_id_provided, bot):
    tg_phone   = f"tg_{chat_id}"
    real_phone = get_tg_id_for_chat(chat_id)
    volunteers = load_all_active_volunteers()

    vol_phone = None
    vol_name  = None
    if tg_phone in volunteers:
        vol_phone = tg_phone
        vol_name  = volunteers[tg_phone]["name"]
    elif real_phone and real_phone in volunteers:
        vol_phone = real_phone
        vol_name  = volunteers[real_phone]["name"]

    if not vol_phone:
        await send_tg(bot, chat_id,
            "You are not a registered volunteer.\n\nUse /start → Volunteer to apply."
        )
        return

    if not case_id_provided:
        await send_tg(bot, chat_id,
            "Please specify the Case ID.\nExample: RESPONDING CASE-1704-AB12CD"
        )
        return

    case = load_case(case_id_provided)
    if not case:
        await send_tg(bot, chat_id, f"Case {case_id_provided} not found.")
        return
    if case["status"] == "COMPLETED":
        await send_tg(bot, chat_id, f"Case {case_id_provided} is already completed. 🐾")
        return
    if case["status"] == "ACCEPTED" and case.get("volunteer_number") != vol_phone:
        await send_tg(bot, chat_id, "This case has already been accepted by another volunteer.")
        return

    case["status"]           = "ACCEPTED"
    case["volunteer"]        = vol_name
    case["volunteer_number"] = vol_phone
    case["time_accepted"]    = datetime.now().strftime("%d %b %Y, %I:%M %p")
    save_case(case)
    tg_pending_outcome[chat_id] = {"case_id": case_id_provided, "note": None}

    await send_tg(bot, chat_id,
        f"✅ Case {case_id_provided} accepted!\n\n"
        f"📍 {case['location']}\n"
        f"Animal: {case['animal']}\n\n"
        f"When done: COMPLETED {case_id_provided}"
    )


async def handle_reporter_confirm(chat_id, reply, bot):
    data = tg_pending_confirm.get(chat_id)
    if not data:
        return False

    case_id       = data["case_id"]
    vol_phone     = data.get("vol_phone","")
    vol_name      = data["volunteer_name"]
    was_accepted  = data.get("was_accepted", True)
    photo_missing = data.get("photo_missing", False)

    if reply in ("YES","Y"):
        tg_pending_confirm.pop(chat_id, None)
        await send_tg(bot, chat_id,
            f"✅ Thank you for confirming.\n\nCase {case_id} is now fully verified.\n\n"
            "Your report saved an animal today. 🐾",
            keyboard=main_menu_keyboard()
        )
        if ADMIN_TG_ID:
            await send_tg(bot, int(ADMIN_TG_ID),
                f"✅ CONFIRMED: {case_id} — Reporter verified rescue.\nVolunteer: {vol_name}"
            )
        return True

    elif reply in ("NO","N"):
        tg_pending_confirm.pop(chat_id, None)
        await send_tg(bot, chat_id,
            f"⚠️ Thank you for letting us know.\n\n"
            "Our team has been alerted and will investigate.\n\n"
            "If the animal is still in danger:\n"
            "📞 Animal Helpline: 1962\n📞 SPCA: 011-23619027",
            keyboard=main_menu_keyboard()
        )
        if was_accepted and not photo_missing:
            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                UPDATE volunteers SET total_rescues = GREATEST(total_rescues - 1, 0)
                WHERE phone_number = %s;
            """, (vol_phone,))
            conn.commit(); cur.close(); conn.close()
        if ADMIN_TG_ID:
            await send_tg(bot, int(ADMIN_TG_ID),
                f"🚨 REPORTER DENIED RESCUE: {case_id}\nVolunteer: {vol_name}\n"
                "Rescue count REVERSED."
            )
        return True

    elif reply in ("UNSURE",):
        tg_pending_confirm.pop(chat_id, None)
        await send_tg(bot, chat_id,
            f"Understood. We've logged your uncertainty for case {case_id}.\n\n"
            "Our team will review. Thank you.",
            keyboard=main_menu_keyboard()
        )
        return True

    return False


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("report", lambda u, c: handle_callback(
        type('obj', (object,), {
            'callback_query': type('q', (object,), {
                'message': type('m', (object,), {'chat_id': u.effective_chat.id})(),
                'data': 'action_report',
                'answer': lambda: None
            })()
        })(), c
    )))
    application.add_handler(CommandHandler("status", handle_status_command))
    application.add_handler(CommandHandler("vstatus", lambda u, c: handle_callback(
        type('obj', (object,), {
            'callback_query': type('q', (object,), {
                'message': type('m', (object,), {'chat_id': u.effective_chat.id})(),
                'data': 'vol_status',
                'answer': lambda: None
            })()
        })(), c
    )))

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Anira Telegram bot starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
