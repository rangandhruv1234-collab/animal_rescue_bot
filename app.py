from flask import Flask, request
from ultralytics import YOLO
import google.generativeai as genai
from groq import Groq
from preprocess import smartcrop_all_animals
import requests
import PIL.Image
import json
import os
import random
import threading
from datetime import datetime

app = Flask(__name__)

# YOUR KEYS
from dotenv import load_dotenv
load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = "1008569229008784"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# LOAD AI MODELS
yolo_model = YOLO("yolov8n.pt")
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")
groq_client = Groq(api_key=GROQ_API_KEY)

# SESSION + VOLUNTEER DATABASE
user_sessions = {}
VOLUNTEER_DB = "volunteers.json"
CASES_DB = "cases.json"
waiting_reporters = {}
# active_cases now stores: {vol_number: {"reporter": number, "case_id": id}}
active_cases = {}
pending_volunteer_responses = {}

def load_volunteers():
    if os.path.exists(VOLUNTEER_DB):
        with open(VOLUNTEER_DB, "r") as f:
            return json.load(f)
    return {}

def save_volunteers(data):
    with open(VOLUNTEER_DB, "w") as f:
        json.dump(data, f, indent=2)
    print("Volunteers database updated")

def load_cases():
    if os.path.exists(CASES_DB):
        with open(CASES_DB, "r") as f:
            return json.load(f)
    return {}

def save_cases(data):
    with open(CASES_DB, "w") as f:
        json.dump(data, f, indent=2)
    print("Cases database updated")

# GENERATE CASE ID
def generate_case_id():
    now = datetime.now()
    day = now.strftime("%d")
    month = now.strftime("%m")
    number = random.randint(1000, 9999)
    return f"CASE-{day}{month}-{number}"

# CREATE CASE
def create_case(reporter, session, urgency):
    case_id = generate_case_id()
    now = datetime.now()

    cases = load_cases()
    cases[case_id] = {
        "case_id": case_id,
        "reporter": reporter,
        "animal": session.get("animal", "Unknown"),
        "severity": session.get("severity", "?"),
        "location": session.get("location", "Not shared"),
        "bleeding": session.get("bleeding", "?"),
        "can_move": session.get("can_move", "?"),
        "urgency": urgency,
        "status": "PENDING",
        "volunteer": None,
        "volunteer_number": None,
        "alerted_volunteers": [],
        "time_reported": now.strftime("%d %b %Y, %I:%M %p"),
        "time_accepted": None,
        "time_completed": None
    }
    save_cases(cases)

    if reporter in user_sessions:
        user_sessions[reporter]["case_id"] = case_id

    print(f"Case created: {case_id}")
    return case_id

# SEND WHATSAPP MESSAGE
def send_message(to, message):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message}
    }
    response = requests.post(url, headers=headers, json=data)
    print("SEND RESPONSE:", response.status_code)

# GET + DOWNLOAD IMAGE
def get_image_url(image_id):
    url = f"https://graph.facebook.com/v18.0/{image_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    response = requests.get(url, headers=headers)
    return response.json()["url"]

def download_image(image_url):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    response = requests.get(image_url, headers=headers)
    with open("received.jpg", "wb") as f:
        f.write(response.content)
    print("Image saved")

# SEND PHOTO TO VOLUNTEER
def send_photo_to_volunteer(to):
    if not os.path.exists("received.jpg"):
        print("No photo to forward")
        return

    upload_url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    with open("received.jpg", "rb") as f:
        files = {
            "file": ("received.jpg", f, "image/jpeg"),
            "messaging_product": (None, "whatsapp"),
            "type": (None, "image/jpeg")
        }
        upload_response = requests.post(upload_url, headers=headers, files=files)

    upload_data = upload_response.json()
    media_id = upload_data.get("id")
    if not media_id:
        print("Failed to upload photo")
        return

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    send_headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {
            "id": media_id,
            "caption": "📸 Photo reported by rescue reporter"
        }
    }
    response = requests.post(url, headers=send_headers, json=data)
    print("PHOTO SEND RESPONSE:", response.status_code)

# ESCALATION SYSTEM
def escalate_case(case_id):
    cases = load_cases()

    if case_id not in cases:
        return

    case = cases[case_id]

    if case["status"] in ["ACCEPTED", "COMPLETED"]:
        print(f"Case {case_id} already {case['status']} — skipping escalation")
        return

    print(f"ESCALATING case {case_id} — no volunteer responded in 10 minutes")

    volunteers = load_volunteers()
    alerted = case.get("alerted_volunteers", [])
    remaining = [v for v in volunteers if v not in alerted]

    if remaining:
        for vol_number in remaining:
            send_message(vol_number,
                f"🚨 ESCALATION ALERT 🚨\n"
                f"⚠️ No volunteer responded for 10 minutes!\n\n"
                f"📋 Case ID: {case_id}\n"
                f"Animal: {case['animal']}\n"
                f"Severity: {case['severity']}/10\n"
                f"📍 Location: {case['location']}\n\n"
                f"Reporter: +{case['reporter']}\n\n"
                f"URGENT — Reply RESPONDING immediately.\n"
                f"When done: COMPLETED {case_id}"
            )
            active_cases[vol_number] = {
                "reporter": case["reporter"],
                "case_id": case_id
            }
            alerted.append(vol_number)
            print(f"Escalation alert sent to: {vol_number}")

        case["alerted_volunteers"] = alerted
        save_cases(cases)

        send_message(case["reporter"],
            f"⏰ Update on Case {case_id}:\n\n"
            "We are still looking for an available volunteer.\n"
            "Additional rescue team members have been alerted.\n\n"
            "Please stay with the animal if possible.\n"
            "Help is coming."
        )
    else:
        send_message(case["reporter"],
            f"⚠️ Update on Case {case_id}:\n\n"
            "All available volunteers have been alerted.\n"
            "If you need immediate help, please contact:\n\n"
            "📞 Animal Helpline: 1962\n"
            "📞 SPCA: 011-23619027\n\n"
            "We are doing our best to get help to you."
        )
        print(f"No more volunteers for case {case_id} — sent NGO fallback")

def start_escalation_timer(case_id, delay_seconds=600):
    timer = threading.Timer(delay_seconds, escalate_case, args=[case_id])
    timer.daemon = True
    timer.start()
    print(f"Escalation timer started for {case_id} — fires in {delay_seconds}s")

# GROQ — SILENT ANSWER INTERPRETER
def interpret_answer(question_type, user_answer):
    prompt = f"""
You are interpreting a WhatsApp message from someone reporting an animal emergency.

Question type: {question_type}
User's answer: "{user_answer}"

Based on the question type, interpret the answer and return ONLY one of these exact outputs:

If question_type is "yes_no":
- Return YES if they mean yes in any way (yess, yep, yeah, correct, true, confirmed, haan, ji, ok yes, "yes but only for a bit", "yes for some time", "yes but leaving soon", etc.)
- Even if they add conditions or time limits like "yes but for some time" — still return YES
- Return NO if they clearly mean no (nope, nahi, not really, no it cant, cant stay, leaving, etc.)
- Return UNCLEAR only if truly impossible to determine

If question_type is "animal":
- Return the animal name in ONE word: dog, cat, cow, horse, bird, or other
- Return UNCLEAR if you cannot determine

If question_type is "severity":
- Return just the number (1-10)
- If they say something like "very serious" return 8
- If they say "a bit hurt" return 4
- If they say "minor" return 2
- Return UNCLEAR if you cannot determine

If question_type is "text":
- Return the cleaned up version of their answer
- Return UNCLEAR if the answer is completely irrelevant

Return ONLY the interpreted value, nothing else.
"""
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
        temperature=0
    )
    result = response.choices[0].message.content.strip()
    print(f"GROQ INTERPRETED: '{user_answer}' → '{result}'")
    return result

# QUESTION LOOKUP TABLE
ALL_QUESTION_MAP = {
    "animal": ("animal", "Which animal is it?\n1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other"),
    "bleeding": ("yes_no", "Is the animal bleeding? (YES/NO)"),
    "can_move": ("yes_no", "Can the animal move? (YES/NO)"),
    "wounds": ("yes_no", "Are there any visible wounds? (YES/NO)"),
    "eating": ("yes_no", "Is it eating or drinking? (YES/NO)"),
    "duration": ("text", "How long has the animal been there?"),
    "behavior": ("text", "Is the animal aggressive or calm?"),
    "ground_support": ("yes_no", "Is anyone with the animal right now?\nYES — please share their number\nNO — rescuer will be dispatched urgently"),
}

# LOCATION VALIDATION
INVALID_LOCATION_WORDS = [
    "here", "nearby", "near me", "idk", "don't know",
    "dont know", "not sure", "somewhere", "outside",
    "there", "this place", "same place"
]

def is_valid_location(text):
    text_clean = text.strip().lower()
    if len(text_clean) < 15:
        return False
    for word in INVALID_LOCATION_WORDS:
        if text_clean == word or text_clean.startswith(word):
            return False
    return True

# CLEAR REPORTER SESSION
def clear_reporter_session(sender):
    if sender in user_sessions:
        del user_sessions[sender]
    if sender in waiting_reporters:
        del waiting_reporters[sender]
    print(f"Session cleared for {sender}")

# HANDLE VOLUNTEER RESPONDING
def handle_responding(sender, volunteer_name, case_data):
    reporter = case_data["reporter"]
    case_id_found = case_data["case_id"]

    cases = load_cases()

    if case_id_found not in cases:
        send_message(sender, "Case not found. Please check your rescue alert.")
        return

    case = cases[case_id_found]
    location = case.get("location", "Check rescue alert")

    case["status"] = "ACCEPTED"
    case["volunteer"] = volunteer_name
    case["volunteer_number"] = sender
    case["time_accepted"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    save_cases(cases)

    reporter_status = waiting_reporters.get(reporter)

    if reporter_status == True:
        reporter_present = "✅ Reporter IS waiting at the location."
    elif reporter_status == False:
        reporter_present = "⚠️ Reporter is NOT at the location."
    else:
        reporter_present = "ℹ️ Reporter presence unknown — proceed to location."

    # Always send volunteer full details immediately
    send_message(sender,
        f"✅ Case accepted.\n\n"
        f"📋 Case ID: {case_id_found}\n"
        f"📍 Location: {location}\n"
        f"Reporter contact: +{reporter}\n"
        f"{reporter_present}\n\n"
        f"When rescue is complete:\n"
        f"COMPLETED {case_id_found}"
    )

    # Always send reporter volunteer details immediately
    send_message(reporter,
        f"🐾 A volunteer has accepted your case!\n\n"
        f"Volunteer: {volunteer_name}\n"
        f"Contact: +{sender}\n\n"
        "They are heading to the location.\n"
        "You can contact them directly if needed."
    )

    if sender in active_cases:
        del active_cases[sender]
    if sender in pending_volunteer_responses:
        del pending_volunteer_responses[sender]

    print(f"Volunteer {sender} assigned to case {case_id_found}")

# CONNECT REPORTER AND VOLUNTEER (STAY path)
def connect_reporter_volunteer(reporter, volunteer_number, volunteer_name):
    case_data = pending_volunteer_responses.get(volunteer_number, {})
    case_id_found = case_data.get("case_id") if isinstance(case_data, dict) else None

    if not case_id_found:
        cases = load_cases()
        for cid, case in cases.items():
            if case["reporter"] == reporter and case["status"] in ["PENDING", "ACCEPTED"]:
                case_id_found = cid
                break

    cases = load_cases()
    if case_id_found and case_id_found in cases:
        cases[case_id_found]["status"] = "ACCEPTED"
        cases[case_id_found]["volunteer"] = volunteer_name
        cases[case_id_found]["volunteer_number"] = volunteer_number
        cases[case_id_found]["time_accepted"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        save_cases(cases)

    send_message(reporter,
        "🙏 Thank you for staying.\n"
        "You are making a real difference.\n\n"
        f"🐾 Great news — a volunteer is on the way!\n\n"
        f"Volunteer: {volunteer_name}\n"
        f"Contact: +{volunteer_number}\n\n"
        "Please stay calm and keep the animal as still as possible.\n"
        "They will reach you shortly."
    )
    send_message(volunteer_number,
        f"✅ Case accepted.\n\n"
        f"📋 Case ID: {case_id_found}\n"
        f"Reporter is waiting at the location.\n"
        f"Reporter contact: +{reporter}\n\n"
        f"Please reach them as soon as possible.\n\n"
        f"When rescue is complete:\n"
        f"COMPLETED {case_id_found}"
    )

    clear_reporter_session(reporter)

    if volunteer_number in active_cases:
        del active_cases[volunteer_number]
    if volunteer_number in pending_volunteer_responses:
        del pending_volunteer_responses[volunteer_number]

    print(f"Reporter {reporter} connected with volunteer {volunteer_number}")

# GET NEXT QUESTION
def get_next_question(session):
    severity = session.get("severity", 0)
    answered = session.get("answered", [])

    base_questions = [
        ("animal", "animal", "Which animal is it?\n1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other — please specify"),
        ("bleeding", "yes_no", "Is the animal bleeding? (YES/NO)"),
        ("can_move", "yes_no", "Can the animal move? (YES/NO)"),
    ]

    moderate_questions = [
        ("wounds", "yes_no", "Are there any visible wounds? (YES/NO)"),
        ("eating", "yes_no", "Is it eating or drinking? (YES/NO)"),
        ("duration", "text", "How long has the animal been there?"),
    ]

    mild_extra_questions = [
        ("wounds", "yes_no", "Are there any visible wounds? (YES/NO)"),
        ("eating", "yes_no", "Is it eating or drinking? (YES/NO)"),
        ("duration", "text", "How long has the animal been there?"),
        ("behavior", "text", "Is the animal aggressive or calm?"),
    ]

    support_question = ("ground_support", "yes_no",
        "Is anyone with the animal right now?\n"
        "YES — please share their number\n"
        "NO — rescuer will be dispatched urgently")

    location_question = ("location", "text",
        "Please share your location 📍\n\n"
        "Option 1 — Live location (recommended):\n"
        "Tap the 📎 attachment icon\n"
        "→ Location → Share Live Location\n\n"
        "Option 2 — Type your address:\n"
        "Must include area name + landmark + city\n"
        "Example: Near Sector 5 Metro, Rohini, New Delhi\n\n"
        "⚠️ Vague locations will not be accepted.\n"
        "Accurate location = faster rescue."
    )

    if severity >= 7:
        all_questions = base_questions + [support_question] + [location_question]
    elif severity >= 4:
        all_questions = base_questions + moderate_questions + [support_question] + [location_question]
    else:
        all_questions = base_questions + mild_extra_questions + [support_question] + [location_question]

    for key, qtype, question in all_questions:
        if key not in answered:
            return key, qtype, question

    return "photo", "text", "Please send a clear photo of the animal 📸"

# ADVANCE TO NEXT QUESTION
def advance_to_next(sender, session):
    next_key, next_qtype, next_question = get_next_question(session)
    if next_key == "location":
        session["stage"] = "location"
        user_sessions[sender] = session
        send_message(sender, next_question)
    elif next_key == "photo":
        session["stage"] = "photo"
        user_sessions[sender] = session
        send_message(sender, next_question)
    else:
        session["pending_key"] = next_key
        session["pending_qtype"] = next_qtype
        if next_key not in session.get("answered", []):
            session["answered"].append(next_key)
        user_sessions[sender] = session
        send_message(sender, next_question)

# HANDLE STATUS CHECK
def handle_status(sender, text):
    cases = load_cases()
    parts = text.strip().upper().replace(" -", "-").replace("- ", "-").split()

    if len(parts) >= 2:
        case_id = parts[1]
    else:
        case_id = None
        latest_time = None
        for cid, case in cases.items():
            if case["reporter"] == sender:
                if latest_time is None or case["time_reported"] > latest_time:
                    latest_time = case["time_reported"]
                    case_id = cid

    if not case_id or case_id not in cases:
        send_message(sender,
            "❓ No case found.\n\n"
            "To check a specific case:\n"
            "Reply: STATUS CASE-2203-0047\n\n"
            "Or make sure you have an active case."
        )
        return

    case = cases[case_id]

    if case["status"] == "PENDING":
        status_emoji = "⏳"
        status_text = "Waiting for volunteer to accept"
    elif case["status"] == "ACCEPTED":
        status_emoji = "🚑"
        status_text = f"Volunteer {case['volunteer']} is on the way"
    elif case["status"] == "COMPLETED":
        status_emoji = "✅"
        status_text = "Rescue completed"
    else:
        status_emoji = "❓"
        status_text = "Unknown"

    send_message(sender,
        f"📋 CASE STATUS\n\n"
        f"Case ID: {case_id}\n"
        f"Animal: {case['animal']}\n"
        f"Location: {case['location']}\n"
        f"Severity: {case['severity']}/10\n"
        f"Reported: {case['time_reported']}\n\n"
        f"{status_emoji} Status: {status_text}\n"
        + (f"Accepted: {case['time_accepted']}\n" if case['time_accepted'] else "")
        + (f"Completed: {case['time_completed']}\n" if case['time_completed'] else "")
    )

# HANDLE COMPLETED
def handle_completed(sender, text):
    cases = load_cases()
    parts = text.strip().upper().replace(" -", "-").replace("- ", "-").split()

    if len(parts) < 2:
        send_message(sender,
            "Please include the Case ID.\n"
            "Example: COMPLETED CASE-2203-XXXX\n\n"
            "Check the rescue alert message for your Case ID."
        )
        return

    case_id = parts[1]

    if case_id not in cases:
        send_message(sender, f"Case {case_id} not found. Please check your Case ID.")
        return

    case = cases[case_id]

    if case["status"] == "COMPLETED":
        send_message(sender, f"Case {case_id} is already marked as completed.")
        return

    case["status"] = "COMPLETED"
    case["time_completed"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    save_cases(cases)

    send_message(sender,
        f"✅ Case {case_id} marked as completed.\n"
        "Thank you for your service 🐾\n"
        "You made a real difference today."
    )

    reporter = case["reporter"]
    clear_reporter_session(reporter)

    send_message(reporter,
        f"🐾 Great news!\n\n"
        f"Your rescue case has been completed.\n"
        f"Case ID: {case_id}\n\n"
        "Thank you for reporting and helping save an animal.\n"
        "You made a difference today 💚"
    )

    print(f"Case {case_id} completed by volunteer {sender}")

# PROCESS USER ANSWER
def process_answer(sender, text):
    session = user_sessions.get(sender, {})
    stage = session.get("stage", "warning")

    if stage == "warning":
        interpreted = interpret_answer("yes_no", text)
        if interpreted == "YES":
            session["stage"] = "severity"
            user_sessions[sender] = session
            send_message(sender,
                "On a scale of 1-10, how serious is the animal's condition?\n"
                "(1 = minor, 10 = critical/life threatening)"
            )
        else:
            send_message(sender,
                "🚨 ANIMAL RESCUE SYSTEM 🚨\n\n"
                "Your number has been registered.\n"
                "Any false report will result in immediate legal action.\n\n"
                "Genuine emergency only. Reply YES to proceed."
            )

    elif stage == "severity":
        interpreted = interpret_answer("severity", text)
        try:
            severity = int(interpreted)
            if 1 <= severity <= 10:
                session["severity"] = severity
                session["stage"] = "questions"
                session["answered"] = []
                session["unclear_count"] = 0
                user_sessions[sender] = session

                if severity >= 7:
                    level = "CRITICAL"
                elif severity >= 4:
                    level = "MODERATE"
                else:
                    level = "MILD"

                send_message(sender,
                    f"Severity {severity}/10 — {level}\n\n"
                    "Which animal is it?\n"
                    "1. Dog\n2. Cat\n3. Cow\n4. Horse\n5. Other — please specify"
                )
                session["answered"].append("animal")
                session["pending_key"] = "animal"
                session["pending_qtype"] = "animal"
                user_sessions[sender] = session
            else:
                send_message(sender, "Please enter a number between 1 and 10.")
        except:
            send_message(sender, "Please enter a number between 1 and 10.")

    elif stage == "questions":
        pending_key = session.get("pending_key")
        pending_qtype = session.get("pending_qtype", "text")

        if pending_key:
            interpreted = interpret_answer(pending_qtype, text)

            if interpreted == "UNCLEAR":
                unclear_count = session.get("unclear_count", 0) + 1
                session["unclear_count"] = unclear_count
                severity = session.get("severity", 5)

                if unclear_count >= 3 and severity >= 4:
                    print(f"Skipping {pending_key} after 3 unclear attempts")
                    session[pending_key] = "Not provided"
                    session["pending_key"] = None
                    session["unclear_count"] = 0
                    user_sessions[sender] = session
                    advance_to_next(sender, session)
                else:
                    user_sessions[sender] = session
                    qtype, question = ALL_QUESTION_MAP.get(
                        pending_key,
                        ("text", "Could you please clarify your answer?")
                    )
                    send_message(sender,
                        f"Sorry, I didn't quite understand.\n\n{question}"
                    )
                return

            session["unclear_count"] = 0

            if pending_key == "animal":
                animal_map = {
                    "1": "Dog", "2": "Cat", "3": "Cow",
                    "4": "Horse", "5": "Other",
                    "dog": "Dog", "cat": "Cat", "cow": "Cow",
                    "horse": "Horse", "bird": "Bird", "other": "Other"
                }
                session["animal"] = animal_map.get(
                    interpreted.lower(),
                    interpreted.capitalize()
                )

            elif pending_key == "ground_support":
                if interpreted == "YES":
                    session["ground_support"] = "YES"
                    parts = text.strip().split()
                    number_found = None
                    for part in parts:
                        cleaned = part.replace("+", "").replace(" ", "")
                        if cleaned.isdigit() and len(cleaned) >= 10:
                            number_found = cleaned
                            break
                    if number_found:
                        session["support_number"] = number_found
                        session["answered"].append("ground_support")
                        session["pending_key"] = None
                        session["unclear_count"] = 0
                        user_sessions[sender] = session
                    else:
                        session["stage"] = "support_number"
                        user_sessions[sender] = session
                        send_message(sender, "Please share their WhatsApp number:")
                        return
                else:
                    session["ground_support"] = "NO"

            else:
                session[pending_key] = interpreted

            session["pending_key"] = None
            session["unclear_count"] = 0
            user_sessions[sender] = session

        advance_to_next(sender, session)

    elif stage == "support_number":
        session["support_number"] = text.strip()
        session["stage"] = "questions"
        session["answered"].append("ground_support")
        user_sessions[sender] = session
        advance_to_next(sender, session)

    elif stage == "location":
        if not is_valid_location(text):
            send_message(sender,
                "⚠️ Location not accepted.\n\n"
                "Please provide a proper address including:\n"
                "• Area or colony name\n"
                "• Nearby landmark or street\n"
                "• City name\n\n"
                "Example: Near Sector 5 Metro, Rohini, New Delhi\n\n"
                "Accurate location = faster rescue. Please try again:"
            )
            return

        session["location"] = text.strip()
        session["stage"] = "photo"
        user_sessions[sender] = session
        send_message(sender,
            "📍 Location noted!\n\n"
            "Now please send a clear photo of the animal 📸"
        )

    elif stage == "waiting":
        interpreted = interpret_answer("yes_no", text)

        if text.upper() == "STAY" or interpreted == "YES":
            waiting_reporters[sender] = True

            volunteer_waiting = None
            volunteer_name_waiting = None
            for vol_num, case_data in list(pending_volunteer_responses.items()):
                if isinstance(case_data, dict) and case_data.get("reporter") == sender:
                    volunteer_waiting = vol_num
                    volunteers = load_volunteers()
                    volunteer_name_waiting = volunteers.get(
                        vol_num, {}
                    ).get("name", "Volunteer")
                    break

            if volunteer_waiting:
                connect_reporter_volunteer(
                    sender, volunteer_waiting, volunteer_name_waiting
                )
            else:
                send_message(sender,
                    "🙏 Thank you for staying.\n"
                    "You are making a real difference.\n\n"
                    "Your case is with our rescue team.\n"
                    "You will be notified as soon as a volunteer accepts.\n\n"
                    "Please keep the animal calm and still if possible."
                )
            clear_reporter_session(sender)

        elif text.upper() == "LEAVE" or interpreted == "NO":
            waiting_reporters[sender] = False
            send_message(sender,
                "Understood. Thank you for reporting.\n"
                "Our rescue team is on the way.\n"
                "You have already helped by reporting this."
            )
            for vol_num, case_data in list(pending_volunteer_responses.items()):
                if isinstance(case_data, dict) and case_data.get("reporter") == sender:
                    send_message(vol_num,
                        "⚠️ The reporter has left the location.\n"
                        "Please proceed to the location as fast as possible.\n\n"
                        f"Reporter contact: +{sender}"
                    )
                    if vol_num in pending_volunteer_responses:
                        del pending_volunteer_responses[vol_num]
                    if vol_num in active_cases:
                        del active_cases[vol_num]
                    break
            clear_reporter_session(sender)

        else:
            send_message(sender,
                "Please reply STAY if you can wait with the animal,\n"
                "or LEAVE if you need to go."
            )

    else:
        user_sessions[sender] = {"stage": "warning"}
        send_message(sender,
            "🚨 ANIMAL RESCUE SYSTEM 🚨\n\n"
            "Your number has been registered.\n"
            "Any false report will result in immediate legal action.\n\n"
            "Genuine emergency only. Reply YES to proceed."
        )

# GEMINI IMAGE ANALYSIS
def analyze_with_gemini(crop_path, user_answers):
    img = PIL.Image.open(crop_path)
    prompt = f"""
You are an animal rescue triage assistant.

A person has reported an injured animal with the following information:
{user_answers}

Look at this image and answer:
1. What animal do you see?
2. Does the image support the reporter's description?
3. How serious does this look on a scale of 1-10?
4. What specific signs of distress or injury do you see?
5. What is your urgency recommendation: HIGH, MEDIUM or LOW?

Be concise and direct. This is an emergency system.
"""
    response = gemini_model.generate_content([prompt, img])
    print("GEMINI RESPONSE:", response.text)
    return response.text

def extract_urgency(gemini_response):
    text = gemini_response.upper()
    if "HIGH" in text:
        return "HIGH"
    elif "MEDIUM" in text:
        return "MEDIUM"
    else:
        return "LOW"

# ALERT ALL VOLUNTEERS
def alert_volunteers(sender, session, urgency, gemini_analysis, case_id):
    volunteers = load_volunteers()
    if not volunteers:
        print("No volunteers registered yet")
        return

    severity = session.get("severity", "?")
    animal = session.get("animal", "Unknown")
    bleeding = session.get("bleeding", "?")
    can_move = session.get("can_move", "?")
    ground_support = session.get("ground_support", "?")
    support_number = session.get("support_number", "None")
    location = session.get("location", "Not shared")

    if urgency == "HIGH":
        urgency_line = "🔴 URGENT — IMMEDIATE RESPONSE NEEDED"
    elif urgency == "MEDIUM":
        urgency_line = "🟡 MEDIUM — RESPOND SOON"
    else:
        urgency_line = "🟢 LOW — MONITOR SITUATION"

    message = (
        f"🚨 RESCUE ALERT 🚨\n"
        f"{urgency_line}\n\n"
        f"📋 Case ID: {case_id}\n\n"
        f"Animal: {animal}\n"
        f"Severity (reported): {severity}/10\n"
        f"Bleeding: {bleeding}\n"
        f"Can move: {can_move}\n"
        f"Ground support: {ground_support}\n"
        f"Support contact: {support_number}\n"
        f"📍 Location: {location}\n\n"
        f"AI Analysis:\n{gemini_analysis}\n\n"
        f"Reported by: +{sender}\n\n"
        f"Reply RESPONDING to accept this case.\n"
        f"When done: COMPLETED {case_id}\n"
        f"⚠️ Other animals may be nearby — stay alert"
    )

    alerted = []
    for vol_number in volunteers:
        send_message(vol_number, message)
        send_photo_to_volunteer(vol_number)
        # Store both reporter AND case_id together
        active_cases[vol_number] = {
            "reporter": sender,
            "case_id": case_id
        }
        alerted.append(vol_number)
        print(f"Alerted volunteer: {vol_number}")

    cases = load_cases()
    if case_id in cases:
        cases[case_id]["alerted_volunteers"] = alerted
        save_cases(cases)

    start_escalation_timer(case_id)

# WEBHOOK
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" in value:
            message = value["messages"][0]
            sender = message["from"]

            if message["type"] == "text":
                text = message["text"]["body"].strip()

                if text.upper().startswith("STATUS"):
                    handle_status(sender, text)
                    return "OK", 200

                if text.upper().startswith("COMPLETED"):
                    handle_completed(sender, text)
                    return "OK", 200

                if text.upper() == "JOIN":
                    session = user_sessions.get(sender, {})
                    session["stage"] = "volunteer_name"
                    user_sessions[sender] = session
                    send_message(sender,
                        "🐾 Welcome to the Animal Rescue Volunteer Network!\n\n"
                        "Please enter your full name to complete registration:"
                    )
                    return "OK", 200

                if user_sessions.get(sender, {}).get("stage") == "volunteer_name":
                    volunteers = load_volunteers()
                    volunteers[sender] = {"name": text, "status": "active"}
                    save_volunteers(volunteers)
                    del user_sessions[sender]
                    send_message(sender,
                        f"✅ Welcome {text}!\n\n"
                        "You are now registered as a rescue volunteer.\n"
                        "You will receive alerts when animals need help.\n\n"
                        "When you receive an alert, reply RESPONDING to accept.\n"
                        "When rescue is done, reply COMPLETED CASE-XXXX"
                    )
                    return "OK", 200

                if text.upper() == "RESPONDING":
                    volunteers = load_volunteers()
                    if sender in volunteers:
                        volunteer_name = volunteers[sender]["name"]
                        case_data = active_cases.get(sender)

                        if case_data and isinstance(case_data, dict):
                            handle_responding(sender, volunteer_name, case_data)
                        else:
                            send_message(sender,
                                "No active case found for your number.\n"
                                "Please wait for a rescue alert."
                            )
                    else:
                        send_message(sender,
                            "You are not registered as a volunteer.\n"
                            "Text JOIN to register."
                        )
                    return "OK", 200

                if sender not in user_sessions:
                    user_sessions[sender] = {"stage": "warning"}
                    send_message(sender,
                        "🚨 ANIMAL RESCUE SYSTEM 🚨\n\n"
                        "Your number has been registered.\n"
                        "Any false report will result in immediate legal action.\n\n"
                        "Genuine emergency only. Reply YES to proceed."
                    )
                else:
                    process_answer(sender, text)

            elif message["type"] == "location":
                session = user_sessions.get(sender, {})

                if session.get("stage") != "location":
                    send_message(sender,
                        "Please complete the questions first before sharing location."
                    )
                    return "OK", 200

                lat = message["location"]["latitude"]
                lng = message["location"]["longitude"]
                maps_link = f"https://maps.google.com/?q={lat},{lng}"

                session["location"] = maps_link
                session["stage"] = "photo"
                user_sessions[sender] = session

                print(f"Location received: {maps_link}")
                send_message(sender,
                    "📍 Location received!\n\n"
                    "Now please send a clear photo of the animal 📸"
                )

            elif message["type"] == "image":
                session = user_sessions.get(sender, {})

                volunteers = load_volunteers()
                if sender in volunteers and session.get("stage") != "photo":
                    print(f"Ignoring image from volunteer {sender}")
                    return "OK", 200

                if session.get("stage") == "location":
                    send_message(sender,
                        "Please share your location first 📍\n"
                        "Tap attachment → Location → Share Live Location\n"
                        "Or type your full address."
                    )
                    return "OK", 200

                if session.get("stage") != "photo":
                    send_message(sender,
                        "Please answer all questions first before sending a photo."
                    )
                    return "OK", 200

                send_message(sender,
                    "📸 Photo received. Sending details to rescue team..."
                )

                image_id = message["image"]["id"]
                image_url = get_image_url(image_id)
                download_image(image_url)

                found_animals = smartcrop_all_animals("received.jpg")

                if not found_animals:
                    send_message(sender,
                        "❓ No animal detected in the image.\n"
                        "Please send a clearer photo."
                    )
                    return "OK", 200

                user_answers = (
                    f"Animal type: {session.get('animal', 'Unknown')}\n"
                    f"Severity: {session.get('severity', '?')}/10\n"
                    f"Bleeding: {session.get('bleeding', '?')}\n"
                    f"Can move: {session.get('can_move', '?')}\n"
                    f"Visible wounds: {session.get('wounds', '?')}\n"
                    f"Eating/drinking: {session.get('eating', '?')}\n"
                    f"Duration: {session.get('duration', '?')}\n"
                    f"Behavior: {session.get('behavior', '?')}\n"
                    f"Ground support: {session.get('ground_support', '?')}"
                )

                best_crop = found_animals[0]["crop_path"]
                gemini_analysis = analyze_with_gemini(best_crop, user_answers)
                urgency = extract_urgency(gemini_analysis)

                case_id = create_case(sender, session, urgency)

                send_message(sender,
                    f"📋 Your Case ID: {case_id}\n\n"
                    "Save this to track your rescue anytime.\n"
                    f"Reply: STATUS {case_id}"
                )

                if urgency == "HIGH":
                    send_message(sender,
                        "✅ Your report has been dispatched to our rescue team.\n"
                        "Help is on the way.\n\n"
                        "🙏 This is a serious case. If it is safe to do so —\n"
                        "can you stay with the animal until help arrives?\n"
                        "Your presence can make a real difference.\n\n"
                        "Reply STAY if you can wait.\n"
                        "Reply LEAVE if you need to go."
                    )
                elif urgency == "MEDIUM":
                    send_message(sender,
                        "✅ Your report has been dispatched to our rescue team.\n"
                        "A volunteer will respond soon.\n\n"
                        "Can you stay with the animal until help arrives?\n\n"
                        "Reply STAY if you can wait.\n"
                        "Reply LEAVE if you need to go."
                    )
                else:
                    send_message(sender,
                        "✅ Your report has been noted.\n"
                        "A volunteer will check when available.\n\n"
                        "Can you keep an eye on the animal?\n\n"
                        "Reply STAY if you can wait.\n"
                        "Reply LEAVE if you need to go."
                    )

                session["stage"] = "waiting"
                user_sessions[sender] = session

                alert_volunteers(sender, session, urgency, gemini_analysis, case_id)

    except Exception as e:
        print("Error:", e)

    return "OK", 200

# WEBHOOK VERIFICATION
@app.route("/webhook", methods=["GET"])
def verify():
    VERIFY_TOKEN = "12345"
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verified!")
        return challenge, 200
    return "Verification failed", 403

if __name__ == "__main__":
    app.run(port=5000)
