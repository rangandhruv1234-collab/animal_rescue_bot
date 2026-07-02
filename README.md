# Animitr Rescue Bot

The backend service powering **Animitr**, India's Animal Identity Infrastructure.

This service transforms a single report of an injured street animal into a structured rescue workflow—from AI-powered triage to volunteer assignment and case closure.

**Live Bot:** https://t.me/AnimitrRescueBot  
**Production API:** https://api.animitr.org

---

## Overview

Animal rescue in India is often coordinated through informal messaging groups, where reports can be missed, duplicated, or forgotten.

The Animitr Rescue Bot provides a structured alternative by introducing a complete rescue workflow with AI-assisted triage, volunteer assignment, and permanent case tracking.

Every rescue is treated as a trackable case rather than a chat message.

---

## How It Works

1. A user reports an injured animal through Telegram or WhatsApp.
2. The submitted image is analyzed using YOLO to detect the animal and identify visible injuries.
3. Report details, location, and detection results are sent to Gemini 2.5 Flash for severity assessment.
4. If Gemini is unavailable, the request automatically falls back to Groq Llama 4 Scout.
5. The nearest available verified volunteer (Aniratna) is assigned.
6. The volunteer receives the rescue details and estimated response information.
7. Once completed, the rescue is closed and permanently stored in the database.

---

## Architecture

The system is designed as two independent Flask services sharing a common backend.

```
                    +----------------------+
                    | PostgreSQL Database  |
                    +----------+-----------+
                               ^
                               |
                +--------------+--------------+
                |                             |
        Telegram Service              WhatsApp Service
             (Flask)                      (Flask)
                |                             |
                +--------------+--------------+
                               |
                        AI Triage Module
                               |
             +-----------------+-----------------+
             |                                   |
      Gemini 2.5 Flash              Groq Llama 4 Scout
            (Primary)                     (Fallback)
```

Both services run on a single EC2 instance behind Nginx. SSL termination is handled at the reverse proxy, while `systemd` ensures each service automatically restarts if it exits unexpectedly.

---

## Features

- AI-assisted rescue triage
- Automatic volunteer assignment
- Multi-platform support (Telegram & WhatsApp)
- Shared PostgreSQL database
- YOLO-powered image analysis
- AI provider failover
- Permanent rescue history
- Modular service architecture

---

## Case Lifecycle

Each rescue progresses through four states:

```
Reported
    ↓
Triaged
    ↓
Assigned
    ↓
Closed
```

Every transition is stored in the database, providing a complete audit trail for each rescue.

---

## Tech Stack

### Backend

- Python
- Flask
- PostgreSQL

### AI

- Gemini 2.5 Flash
- Groq Llama 4 Scout
- YOLO

### Infrastructure

- AWS EC2
- Nginx
- systemd

### Messaging

- Telegram Bot API
- WhatsApp Business API

---

## Project Structure

```
animal_rescue_bot/

├── app.py
├── whatsapp/
├── telegram/
├── triage/
├── models/
├── migrations/
├── requirements.txt
├── .env.example
└── README.md
```

---

## Environment Variables

Copy `.env.example` to `.env` and configure the required credentials.

```env
TELEGRAM_BOT_TOKEN=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=

GEMINI_API_KEY=
GROQ_API_KEY=

DATABASE_URL=
```

---

## Running Locally

Clone the repository:

```bash
git clone https://github.com/rangandhruv1234-collab/animal_rescue_bot.git
cd animal_rescue_bot
```

Create a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure environment variables:

```bash
cp .env.example .env
```

Run the application:

```bash
python app.py
```

For WhatsApp webhook testing, expose your local server using a tunneling service such as ngrok. Telegram can be tested locally using polling mode.

---

## Database

The database stores three primary entities:

- Cases
- Volunteers
- Case History

Each rescue maintains a complete history of state changes, allowing every case to be tracked from reporting to closure.

---

## Deployment

Production deployment runs both services as independent `systemd` units behind Nginx.

```bash
sudo systemctl restart whatsapp.service
sudo systemctl restart telegram.service

sudo systemctl status whatsapp.service
sudo systemctl status telegram.service
```

Logs can be viewed using:

```bash
sudo journalctl -u whatsapp.service -f
```

---

## Roadmap

Upcoming work includes:

- Full WhatsApp Business API rollout
- Community Rescue Pools
- NGO management tools
- Volunteer dashboard
- Rescue analytics
- On-device pre-filtering before AI inference
- Performance improvements for large-scale deployments

---

## About Animitr

Animitr is building the digital infrastructure for animal welfare in India.

The long-term vision includes rescue coordination, ethical breeder verification, lifetime health records through Vetovo, adoption services, and community-funded animal care.

---

## Contact

Email: contact.animitr@gmail.com

Website: www.animitr.org
