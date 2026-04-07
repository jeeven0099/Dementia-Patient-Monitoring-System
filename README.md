# Dementia Patient Monitoring System — Software Documentation

## Overview

An edge AI pipeline running on a Raspberry Pi 4 that continuously processes audio from an ESP32 wearable device. The system transcribes speech, analyses it for patient safety and intent, encrypts all data at rest, and stores everything in a PostgreSQL database.

---

## System Architecture

```
ESP32 Wearable (microphone)
        ↓ HTTP POST (audio file)
Flask Server (server.py)
        ↓
[Step 1]  FFmpeg — convert AAC/MP3 → 16kHz mono WAV
        ↓
[Step 2]  WebRTC VAD — skip silent recordings
        ↓
[Step 3]  Moonshine ONNX — speech-to-text transcription (chunked, 25s windows)
        ↓
[Step 4]  Fernet encryption — encrypt transcript + audio files at rest
        ↓
[Step 5]  Keyword check — fast pre-screen for danger/forgetting keywords
        ↓
[Step 6]  Groq LLM (llama-3.3-70b) — multi-speaker aware summary + alert detection
        ↓
[Step 7]  Intent extraction + Confidence Fusion Layer + Decision Engine
        ↓
[Step 8]  Encrypt summary JSON
        ↓
[Step 9]  PostgreSQL — store all file paths, statuses, intents, reminders
```

---

## Requirements

### Hardware
- Raspberry Pi 4 (4GB RAM minimum)
- ESP32 wearable with microphone

### Software
- Python 3.13
- PostgreSQL 17
- FFmpeg
- Ollama (optional — for local LLM testing)

### Python packages
```bash
pip3 install flask werkzeug psycopg2-binary numpy \
             moonshine-onnx groq cryptography \
             webrtcvad-wheels soundfile
```

---

## Installation

### 1. Clone and set up virtual environment
```bash
python3 -m venv /home/osugiw/stt_env
source /home/osugiw/stt_env/bin/activate
pip3 install flask werkzeug psycopg2-binary numpy \
             moonshine-onnx groq cryptography \
             webrtcvad-wheels soundfile
```

### 2. Install system dependencies
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib ffmpeg -y
```

### 3. Set up PostgreSQL database
```bash
sudo -u postgres psql -c "CREATE USER jeeven WITH PASSWORD '12345';"
sudo -u postgres psql -c "CREATE DATABASE stt_db OWNER jeeven;"
```

Create tables:
```bash
sudo -u postgres psql -d stt_db -c "
CREATE TABLE recordings (
    id          SERIAL PRIMARY KEY,
    audio_file  TEXT NOT NULL,
    status      TEXT DEFAULT 'uploaded',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE transcripts (
    id            SERIAL PRIMARY KEY,
    recording_id  INTEGER REFERENCES recordings(id) ON DELETE CASCADE,
    text          TEXT
);

CREATE TABLE summaries (
    id            SERIAL PRIMARY KEY,
    recording_id  INTEGER REFERENCES recordings(id) ON DELETE CASCADE,
    summary       TEXT
);

CREATE TABLE intents (
    id               SERIAL PRIMARY KEY,
    recording_id     INTEGER REFERENCES recordings(id) ON DELETE CASCADE,
    task             TEXT NOT NULL,
    temporal_cue     TEXT,
    raw_quote        TEXT,
    semantic_score   REAL,
    temporal_score   REAL,
    cognitive_score  REAL,
    fused_confidence REAL,
    decision         TEXT,
    action           TEXT,
    cognitive_signals TEXT,
    status           TEXT DEFAULT 'pending',
    reminder_sent    BOOLEAN DEFAULT FALSE,
    scheduled_time   TIMESTAMP,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE intent_feedback (
    id         SERIAL PRIMARY KEY,
    intent_id  INTEGER REFERENCES intents(id) ON DELETE CASCADE,
    outcome    TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO jeeven;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO jeeven;
"
```

### 4. Set up folder structure
```bash
mkdir -p /home/osugiw/mcu_server/uploads
mkdir -p /home/osugiw/mcu_server/transcripts
mkdir -p /home/osugiw/mcu_server/summaries
mkdir -p /home/osugiw/backup
```

### 5. Configure API key
Edit `server.py` and set your Groq API key:
```python
GROQ_API_KEY = "your_groq_api_key_here"
```
Get a free key at: https://console.groq.com

### 6. Run the server
```bash
source /home/osugiw/stt_env/bin/activate
python3 /home/osugiw/mcu_server/server.py
```

### 7. Back up the encryption key
The encryption key is auto-generated on first run. Back it up immediately:
```bash
cp /home/osugiw/mcu_server/.encryption_key /home/osugiw/backup/.encryption_key
```
> ⚠️ **CRITICAL: Losing this key means losing access to all patient data permanently.**

---

## Folder Structure

```
/home/osugiw/mcu_server/
├── server.py                  # Main Flask application
├── .encryption_key            # Fernet encryption key (keep private)
├── uploads/                   # Incoming audio files (encrypted after processing)
│   └── 2026-03-27_172001.wav.enc
├── transcripts/               # Encrypted transcript files
│   └── 2026-03-27_172001.txt.enc
└── summaries/                 # Encrypted summary JSON files
    └── 2026-03-27_172001_summary.json.enc

/home/osugiw/backup/
└── .encryption_key            # Backup of encryption key
```

---

## API Endpoints

### Upload audio
```
POST /upload
Content-Type: multipart/form-data
Body: file=<audio_file>
```
```bash
curl -X POST -F "file=@/path/to/audio.aac" http://localhost:5000/upload
```

### Check pipeline status
```
GET /status/<filename>
```
```bash
curl http://localhost:5000/status/2026-03-27_172001.aac
```

### List all recordings
```
GET /recordings
```
```bash
curl http://localhost:5000/recordings
```

### Get alert-flagged recordings
```
GET /alerts
```
```bash
curl http://localhost:5000/alerts
```

### Decrypt and read a transcript
```
GET /decrypt/transcript/<recording_id>
```
```bash
curl http://localhost:5000/decrypt/transcript/51
```

### Decrypt and read a summary
```
GET /decrypt/summary/<recording_id>
```
```bash
curl http://localhost:5000/decrypt/summary/51
```

### Get all intents
```
GET /intents
```
```bash
curl http://localhost:5000/intents
```

### Get intents for a specific recording
```
GET /intents/<recording_id>
```
```bash
curl http://localhost:5000/intents/51
```

### Get pending reminders (for caregiver app to poll)
```
GET /reminders/pending
```
```bash
curl http://localhost:5000/reminders/pending
```

### Mark reminder as sent
```
POST /reminders/<intent_id>/sent
```
```bash
curl -X POST http://localhost:5000/reminders/1/sent
```

### Submit caregiver feedback on intent (learning loop)
```
POST /intents/<intent_id>/feedback
Body: {"outcome": "accepted"} | {"outcome": "rejected"} | {"outcome": "ignored"}
```
```bash
curl -X POST http://localhost:5000/intents/1/feedback \
     -H "Content-Type: application/json" \
     -d '{"outcome": "accepted"}'
```

### View learning loop statistics
```
GET /learning/stats
```
```bash
curl http://localhost:5000/learning/stats
```

---

## Pipeline Status Values

| Status | Meaning |
|---|---|
| `uploaded` | File received, pipeline starting |
| `no_speech` | VAD detected no speech — skipped |
| `transcribed` | Moonshine transcription complete |
| `transcription_failed` | Transcription returned empty |
| `completed` | Full pipeline done, no alerts |
| `alert_flagged` | Danger or forgetting detected |
| `intent_flagged` | High-confidence patient intent detected |
| `error` | Pipeline error |

---

## Intent Decision Engine

Intents are scored using three signals combined into a fused confidence score:

| Signal | Weight | Description |
|---|---|---|
| Semantic score | 50% | How clearly is this a genuine intent (from LLM) |
| Temporal score | 25% | Presence and specificity of time cues |
| Cognitive score | 25% | Reduced when hesitation/filler words are high |

### Decision thresholds

| Fused score | Decision | Action |
|---|---|---|
| ≥ 0.75 | `act` | Notify caregiver immediately |
| ≥ 0.50 | `confirm` | Caregiver to confirm with patient |
| ≥ 0.30 | `log` | Log quietly, no disturbance |
| < 0.30 | `ignore` | Too uncertain |

### Reminder scheduling

When an intent is detected with a temporal cue, two reminders are stored:
- **Immediate** — appears in `/reminders/pending` right away (caregiver knows intent was heard)
- **Scheduled** — appears in `/reminders/pending` when the stated time arrives

Temporal cue parsing examples:

| Patient says | Scheduled time |
|---|---|
| "tonight" | Today at 7:00 PM |
| "tomorrow" | Tomorrow at 9:00 AM |
| "this afternoon" | Today at 2:00 PM |
| "at 5" | Today at 5:00 PM |
| "later" | 2 hours from now |

---

## Encryption

All patient data is encrypted at rest using **Fernet symmetric encryption** (AES-128-CBC + HMAC-SHA256).

- Audio files: encrypted to `.wav.enc`
- Transcripts: encrypted to `.txt.enc`
- Summaries: encrypted to `.json.enc`
- Encryption key stored at `/home/osugiw/mcu_server/.encryption_key`

To read any encrypted file via the API:
```bash
curl http://localhost:5000/decrypt/transcript/<recording_id>
curl http://localhost:5000/decrypt/summary/<recording_id>
```

---

## Models Used

| Component | Model | Where it runs |
|---|---|---|
| Speech-to-text | Moonshine ONNX base (~74MB) | Local on Pi |
| LLM summary + alerts | llama-3.3-70b-versatile | Groq API (free) |
| Intent extraction | llama-3.3-70b-versatile | Groq API (free) |
| Voice Activity Detection | WebRTC VAD | Local on Pi |

---

## Running as a System Service

To run the server automatically on boot:

```bash
sudo nano /etc/systemd/system/mcu_server.service
```

```ini
[Unit]
Description=MCU Flask Server
After=network.target postgresql.service

[Service]
User=osugiw
WorkingDirectory=/home/osugiw/mcu_server
Environment="PATH=/home/osugiw/stt_env/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/home/osugiw/stt_env/bin/python3 /home/osugiw/mcu_server/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable mcu_server
sudo systemctl start mcu_server
```

---

## Database Access via DBeaver

Connect DBeaver on your laptop to the Pi database using Tailscale:

- **Host:** `100.120.159.54` (Tailscale IP)
- **Port:** `5432`
- **Database:** `stt_db`
- **Username:** `jeeven`
- **Password:** `12345`

---

## Future Roadmap

- [ ] Caregiver mobile app (React Native) with push notifications via Ntfy
- [ ] HTTPS/SSL for encrypted data in transit
- [ ] Upgrade to Pi 5 with llama3.2:3b for better local LLM quality
- [ ] Speaker diarization to better isolate patient voice
- [ ] Throat microphone hardware for single-speaker isolation
- [ ] Personalised threshold adjustment via learning loop data
- [ ] Daily report generation for medical records
