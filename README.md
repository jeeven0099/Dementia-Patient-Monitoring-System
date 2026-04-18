# Sally — Personal Wearable AI Assistant

<table border="0">
<tr>
<td width="50%" align="center">
<img src="assets/images/sally_logo.svg" width="45%" alt="Sally Logo">
</td>
<td width="50%" align="center">
<img src="assets/images/RiceStackedHoriz_Blue.png" width="45%" alt="Rice University">
</td>
</tr>
</table>

> **Always-on ambient intelligence.** Sally is a wearable AI assistant built on a Raspberry Pi 4 + ESP32 that continuously listens, transcribes, summarizes, and extracts actionable intent from your spoken day — surfacing everything through an encrypted backend and a native Flutter mobile app.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Feature Highlights](#feature-highlights)
- [Tech Stack](#tech-stack)
- [Repository Layout](#repository-layout)
- [Data Flow](#data-flow)
- [API Reference](#api-reference)
- [Database Schema](#database-schema)
- [Flutter Mobile App](#flutter-mobile-app)
- [Deployment](#deployment)
- [Security Model](#security-model)
- [Performance](#performance)
- [Getting Started](#getting-started)
- [Team](#team)

---

## Overview

Sally captures ambient audio through an ESP32 microphone worn on the body, streams it over Wi-Fi to a Raspberry Pi 4 edge server, and runs a multi-stage AI pipeline:

1. **Voice Diarization** — MFCC-based fingerprinting isolates the owner's voice from background speakers before any transcription occurs.
2. **Speech-to-Text** — Groq Cloud `whisper-large-v3` produces low-latency transcripts.
3. **LLM Summarization & Intent Extraction** — `llama-3.3-70b-versatile` extracts structured summaries and intent signals (tasks, decisions, emotional cues) with fused-confidence scoring.
4. **RAG Memory** — `pgvector` embeds transcript chunks for semantic recall across sessions.
5. **Encrypted Storage** — Every file written to disk (audio, transcripts, summaries) is Fernet-encrypted (AES-128-CBC + HMAC-SHA256) at rest.
6. **Mobile Dashboard** — A Flutter app connects over Tailscale VPN to browse window summaries, manage reminders, and chat with Sally using retrieved memory context.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                          Wearable Hardware                             │
│  ┌──────────────┐   AAC audio   ┌─────────────────────────────────┐   │
│  │  ESP32-S3    │ ────────────► │         Raspberry Pi 4          │   │
│  │  MEMS mic    │   HTTP POST   │         (Edge Server)           │   │
│  └──────────────┘               │                                 │   │
│                                 │  ┌──────────────────────────┐   │   │
│                                 │  │   server_personal.py     │   │   │
│                                 │  │   (Flask  +  pipeline)   │   │   │
│                                 │  └────────────┬─────────────┘   │   │
│                                 │               │                 │   │
│                                 │   ┌───────────▼──────────────┐  │   │
│                                 │   │  Voice Diarization       │  │   │
│                                 │   │  WebRTC VAD + MFCC (120d)│  │   │
│                                 │   └───────────┬──────────────┘  │   │
│                                 │               │ owner segments  │   │
│                                 │   ┌───────────▼──────────────┐  │   │
│                                 │   │  Groq Whisper STT        │  │   │
│                                 │   │  whisper-large-v3        │  │   │
│                                 │   └───────────┬──────────────┘  │   │
│                                 │               │ transcript      │   │
│                                 │   ┌───────────▼──────────────┐  │   │
│                                 │   │  Groq LLM                │  │   │
│                                 │   │  llama-3.3-70b-versatile │  │   │
│                                 │   │  Summary + Intent        │  │   │
│                                 │   └───────────┬──────────────┘  │   │
│                                 │               │                 │   │
│                                 │   ┌───────────▼──────────────┐  │   │
│                                 │   │  PostgreSQL + pgvector   │  │   │
│                                 │   │  Fernet-encrypted files  │  │   │
│                                 │   └──────────────────────────┘  │   │
│                                 └─────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
                                        │  Tailscale VPN
                                        ▼
                               ┌─────────────────┐
                               │  Flutter App    │
                               │  iOS / Android  │
                               │  Summary · Chat │
                               │  Reminders      │
                               └─────────────────┘
```

---

## Feature Highlights

| Feature | Detail |
|---|---|
| **Owner-only transcription** | WebRTC VAD segments audio; per-segment MFCC cosine similarity filters out non-owner speech before any API call |
| **Voice enrollment** | One-shot enrollment via `/enroll` — 120-dim MFCC mean stored in `voice_profiles` table, persists across restarts |
| **Window summaries** | Aggregated views for Last 6 Hours, Today (since midnight UTC), and rolling 24 h via `/summary/window` |
| **Intent extraction** | Fused-confidence scoring (semantic 70 % · temporal 15 % · cognitive 15 %) classifies intent as `act / confirm / log / ignore` |
| **RAG memory** | `pgvector` nearest-neighbor search feeds relevant transcript chunks into Sally Q&A responses |
| **Reminders CRUD** | Full lifecycle: create, snooze, resolve — with optional `repeat_rule` and `scheduled_time` |
| **Encrypted at rest** | Fernet (AES-128-CBC + HMAC-SHA256) on every audio file, transcript, and summary |
| **Bearer token auth** | SHA-256 hashed tokens in `api_tokens` table; `last_used` timestamp updated on every request |
| **Scheduler daemon** | `scheduler.py` polls for pending intents and fires reminders on schedule |
| **Cross-platform Flutter** | Single codebase for iOS + Android; compile-time config via `--dart-define` |
| **GitHub Actions iOS build** | Unsigned IPA built on macOS runner, sideloadable via Sideloadly |

---

## Tech Stack

### Edge Server (Raspberry Pi 4)
| Layer | Technology |
|---|---|
| Runtime | Python 3.11 |
| HTTP Framework | Flask |
| Speech-to-Text | Groq Cloud — `whisper-large-v3` |
| LLM | Groq Cloud — `llama-3.3-70b-versatile` |
| Voice Diarization | WebRTC VAD + NumPy MFCC (no PyTorch) |
| Database | PostgreSQL 15 + `pgvector` extension |
| Encryption | `cryptography` — Fernet |
| Audio conversion | FFmpeg (AAC → WAV) |
| Scheduling | `schedule` library (APScheduler-style loop) |

### Mobile App (Flutter)
| Layer | Technology |
|---|---|
| Framework | Flutter 3.41.7 / Dart 3.x |
| HTTP client | `package:http` |
| Storage | `shared_preferences` |
| Config injection | `--dart-define` at build time |
| Connectivity | Tailscale VPN (private IP routing) |

### Infrastructure
| Component | Technology |
|---|---|
| Network | Tailscale mesh VPN |
| CI/CD | GitHub Actions (iOS IPA build) |
| Hardware | Raspberry Pi 4 (4 GB RAM) + ESP32-S3 |

---

## Repository Layout

```
.
├── server_personal.py        # Flask API + full AI pipeline
├── config.py                 # Environment-variable configuration
├── scheduler.py              # Background reminder/intent scheduler
├── schema.sql                # PostgreSQL DDL (idempotent, safe to re-run)
├── cleanup.py                # Utility: purge old encrypted files
├── mobile_intents_client.py  # CLI helper: list/update intents from terminal
│
├── lib/                      # Flutter source
│   ├── main.dart             # App entry — 5-tab navigation
│   ├── app_config.dart       # Compile-time --dart-define reader
│   ├── sally_api.dart        # Typed API client (all endpoints)
│   ├── login_page.dart       # Token-based authentication screen
│   ├── chat_page.dart        # Transcript browser + Sally Q&A
│   ├── summary_page.dart     # Per-recording detail view
│   ├── summary_dashboard_page.dart  # Window summaries + Reminders tabs
│   ├── device_page.dart      # Device status and settings
│   ├── profile_page.dart     # User profile
│   ├── intent_record.dart    # Intent data model
│   └── widgets/
│       ├── sally_nav_bar.dart # Animated 5-item bottom nav
│       ├── sally_button.dart  # Branded primary button
│       ├── sally_logo.dart    # SVG logo component
│       ├── home_card.dart     # Metric card widget
│       └── nav_item.dart      # Nav bar item
│
├── assets/images/            # SVG/PNG branding assets
│
├── .github/workflows/
│   └── build-ios.yml         # GitHub Actions: unsigned IPA build
│
└── schema.sql                # Database schema + migrations
```

---

## Data Flow

```
ESP32 records N seconds of AAC
        │
        ▼
POST /upload  (multipart, Bearer token)
        │
        ▼
[1] ffmpeg: AAC → 16 kHz mono WAV
        │
        ▼
[2] WebRTC VAD → speech segments
    Per segment: MFCC (120-dim) → cosine similarity vs enrolled profile
    Owner segments concatenated → filtered WAV
    (non-owner audio discarded; recording marked "no_owner_speech" and skipped)
        │
        ▼
[3] Groq Whisper STT → plain-text transcript
    Transcript Fernet-encrypted → .txt.enc written to disk
    Path stored in transcripts table
        │
        ▼
[4] Groq LLM → structured JSON
    {summary, topics, intent_signals, cognitive_flags, action_items}
    JSON Fernet-encrypted → _summary.json.enc written to disk
    Path stored in summaries table
        │
        ▼
[5] Intent extraction
    Each intent_signal scored (semantic · temporal · cognitive)
    Fused confidence → decision (act/confirm/log/ignore)
    Stored in intents table with scheduled_time if temporal cue detected
        │
        ▼
[6] RAG chunking
    Transcript split into ~200-token chunks
    all-MiniLM-L6-v2 embedding (384-dim) stored in memory_chunks (pgvector)
        │
        ▼
Recording marked "processed"
```

---

## API Reference

All endpoints (except `/health` and `/admin/tokens`) require:
```
Authorization: Bearer <token>
```

### Core

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check — returns `{"status":"ok"}` |
| `POST` | `/upload` | Upload AAC audio file; triggers async pipeline |
| `GET` | `/recordings` | List recordings for a device |
| `GET` | `/transcript/<id>` | Fetch decrypted transcript text |
| `GET` | `/summary/<id>` | Fetch decrypted summary JSON |

### Query Parameters
`device_id` is required on all data endpoints:
```
GET /recordings?device_id=esp32_01&limit=20
```

### Window Summaries

```
GET /summary/window?device_id=esp32_01&window=6h
GET /summary/window?device_id=esp32_01&window=day
GET /summary/window?device_id=esp32_01&window=24h
```

**Response:**
```json
{
  "window": "6h",
  "start": "2026-04-18T06:00:00Z",
  "end":   "2026-04-18T12:00:00Z",
  "recording_count": 14,
  "summary": "...",
  "topics": ["project planning", "lunch", "..."],
  "action_items": ["..."]
}
```

### Intents

| Method | Path | Description |
|---|---|---|
| `GET` | `/intents` | List intents (`?device_id=&status=pending`) |
| `PATCH` | `/intents/<id>` | Update status (`completed` / `ignored`) |

### Reminders

| Method | Path | Description |
|---|---|---|
| `GET` | `/reminders` | List reminders (`?device_id=&status=pending`) |
| `POST` | `/reminders` | Create reminder |
| `PATCH` | `/reminders/<id>` | Update (snooze, resolve, edit) |
| `DELETE` | `/reminders/<id>` | Delete reminder |

**Create reminder body:**
```json
{
  "device_id": "esp32_01",
  "title": "Call doctor",
  "notes": "Re: blood test results",
  "scheduled_time": "2026-04-19T09:00:00Z",
  "repeat_rule": "none"
}
```

### Sally Q&A (RAG)

```
POST /ask
{
  "device_id": "esp32_01",
  "question": "What did I decide about the project proposal?"
}
```

Uses `pgvector` nearest-neighbor search over `memory_chunks` to ground the LLM response in your actual transcripts.

### Voice Enrollment

```
POST /enroll
Content-Type: multipart/form-data
  file=<16kHz mono WAV>
  device_id=esp32_01
  label=owner
```

Computes 120-dim MFCC mean embedding from the WAV and persists it to `voice_profiles`. Enrollment survives server restarts — re-enrollment required only if the profile needs updating.

### Admin (localhost only)

```
POST /admin/tokens          # Create new API token
  { "device_id": "esp32_01", "label": "phone" }
```

Returns the raw token once — store it securely.

---

## Database Schema

Sally uses PostgreSQL 15 with the `pgvector` extension. `schema.sql` is fully idempotent (`IF NOT EXISTS` + `ALTER TABLE … ADD COLUMN IF NOT EXISTS`) and safe to re-run on an existing database.

### Tables

| Table | Purpose |
|---|---|
| `recordings` | Tracks every uploaded audio file and its pipeline status |
| `transcripts` | Stores encrypted file path for each transcript |
| `summaries` | Stores encrypted file path for each LLM summary |
| `intents` | Intent records with fused confidence scores and scheduling |
| `voice_profiles` | One row per device — MFCC enrollment embedding |
| `memory_chunks` | `vector(384)` embeddings for RAG retrieval |
| `sally_queries` | Q&A history with source chunk references |
| `hourly_summaries` | Pre-aggregated hourly summaries (cache) |
| `window_summaries` | 6h / day / 24h window summaries |
| `reminders` | User-managed reminders with repeat rules |
| `api_tokens` | SHA-256 hashed Bearer tokens |

---

## Flutter Mobile App

### Navigation (5 tabs)

| Tab | Page | Description |
|---|---|---|
| 0 | Home | Recent recordings and quick stats |
| 1 | Device | Device status and configuration |
| 2 | Chat | Transcript browser + Sally Q&A |
| 3 | Summary | Window summaries (6h / Today) + Reminders |
| 4 | Profile | User settings |

### Compile-time Configuration

The app receives all sensitive config at build time via `--dart-define` — no secrets in source:

```bash
flutter build apk \
  --dart-define=SALLY_API_BASE_URL=http://100.x.x.x:5000 \
  --dart-define=SALLY_API_TOKEN=<bearer-token> \
  --dart-define=SALLY_DEVICE_ID=esp32_01
```

`app_config.dart` reads these with `String.fromEnvironment(...)`.

---

## Deployment

### Prerequisites

- Raspberry Pi 4 (4 GB RAM recommended) running Raspberry Pi OS (64-bit)
- PostgreSQL 15 + `pgvector` extension
- Python 3.11 + pip
- FFmpeg
- Groq API key
- Tailscale installed on both Pi and phone

### Pi Server Setup

```bash
# 1. Clone repo
git clone https://github.com/<org>/sally-wearable.git
cd sally-wearable

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# edit .env — set GROQ_API_KEY, DB_*, folder paths

# 4. Initialise database (safe to re-run)
psql -h localhost -U <db_user> -d <db_name> -f schema.sql

# 5. Enroll your voice (once)
curl -X POST http://localhost:5000/enroll \
  -H "Authorization: Bearer <token>" \
  -F "file=@/path/to/voice_sample.wav" \
  -F "device_id=esp32_01"

# 6. Start server
python server_personal.py

# 7. (Optional) Start scheduler
python scheduler.py
```

### iOS IPA via GitHub Actions

1. Go to **Actions → Build iOS IPA → Run workflow**
2. Supply the Pi's Tailscale IP and your device ID
3. Set `SALLY_API_TOKEN` in **Settings → Secrets**
4. Download the artifact and sideload with [Sideloadly](https://sideloadly.io/)

### Android APK

```bash
flutter build apk \
  --dart-define=SALLY_API_BASE_URL=http://100.x.x.x:5000 \
  --dart-define=SALLY_API_TOKEN=<token> \
  --dart-define=SALLY_DEVICE_ID=esp32_01
# Output: build/app/outputs/flutter-apk/app-release.apk
```

---

## Security Model

| Threat | Mitigation |
|---|---|
| Network interception | All traffic routed over Tailscale WireGuard VPN (zero exposed ports) |
| Stolen device / disk | Fernet encryption on every file written to disk; key stored separately |
| Unauthorized API access | Bearer token required on all data endpoints; SHA-256 hash stored (never plaintext) |
| Third-party speech | MFCC voice diarization discards non-owner segments before transcription |
| Token leakage | Tokens issued once at creation; `last_used` auditing; localhost-only issuance |

---

## Performance

Measured on Raspberry Pi 4 (4 GB) with a 60-second AAC recording:

| Stage | Typical Latency |
|---|---|
| AAC → WAV conversion (FFmpeg) | ~0.3 s |
| Voice diarization (VAD + MFCC) | ~0.8 s |
| Groq Whisper STT | ~1.5 s |
| Groq LLM summary + intent | ~2.5 s |
| pgvector embedding + insert | ~0.4 s |
| **Total pipeline (p50)** | **~5.5 s** |

Voice diarization runs entirely on-device with pure NumPy/SciPy — no GPU or cloud dependency.

---

## Getting Started

### Local Development (Flutter)

```bash
git clone https://github.com/<org>/sally-wearable.git
cd sally-wearable
flutter pub get

# Run on connected device or emulator
flutter run \
  --dart-define=SALLY_API_BASE_URL=http://100.x.x.x:5000 \
  --dart-define=SALLY_API_TOKEN=<token> \
  --dart-define=SALLY_DEVICE_ID=esp32_01
```

### Environment Variables (`.env` on Pi)

```
GROQ_API_KEY=gsk_...
DB_HOST=localhost
DB_NAME=stt_db
DB_USER=jeeven
DB_PASSWORD=...
AUDIO_FOLDER=/home/osugiw/mcu_server/uploads
TRANSCRIPT_FOLDER=/home/osugiw/mcu_server/transcripts
SUMMARY_FOLDER=/home/osugiw/mcu_server/summaries
ENROLL_FOLDER=/home/osugiw/mcu_server/enrollments
KEY_FILE=/home/osugiw/mcu_server/.fernet_key
```

---

## Team

| Name | Role | Contact |
|---|---|---|
| **Jeeven Balasubramaniam** | Backend · AI Pipeline · Mobile | [jb310@rice.edu](mailto:jb310@rice.edu) |
| **Sugiarto Wibowo** | Hardware · Firmware · Infrastructure | [sw183@rice.edu](mailto:sw183@rice.edu) |

**Supervisor:** Nakul Garg — Rice University, ECE Department

---

<div align="center">
  <sub>Built at Rice University · Houston, Texas</sub>
</div>
