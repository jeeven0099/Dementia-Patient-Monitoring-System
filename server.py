import os
import re
import json
import wave
import subprocess
import psycopg2
import numpy as np
import webrtcvad
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
from threading import Thread
from groq import Groq
from moonshine_onnx import MoonshineOnnxModel, load_audio as moonshine_load_audio, load_tokenizer
from cryptography.fernet import Fernet

app = Flask(__name__)

# ── configuration ─────────────────────────────────────────────────────────────

AUDIO_FOLDER      = "/home/osugiw/mcu_server/uploads"
TRANSCRIPT_FOLDER = "/home/osugiw/mcu_server/transcripts"
SUMMARY_FOLDER    = "/home/osugiw/mcu_server/summaries"
KEY_FILE          = "/home/osugiw/mcu_server/.encryption_key"

for folder in [AUDIO_FOLDER, TRANSCRIPT_FOLDER, SUMMARY_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# ── confidence fusion weights ─────────────────────────────────────────────────

WEIGHTS = {
    "semantic":  0.50,
    "temporal":  0.25,
    "cognitive": 0.25,
}

THRESHOLD_HIGH   = 0.75
THRESHOLD_MEDIUM = 0.50
THRESHOLD_LOW    = 0.30

# ── encryption setup ──────────────────────────────────────────────────────────

def load_or_create_key() -> Fernet:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read()
        print("[Encryption] Key loaded from disk.")
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)
        print(f"[Encryption] New key generated → {KEY_FILE}")
        print("[Encryption] ⚠️  BACK UP THIS KEY FILE!")
    return Fernet(key)

FERNET = load_or_create_key()

def encrypt_file(input_path: str) -> str:
    with open(input_path, "rb") as f:
        data = f.read()
    encrypted = FERNET.encrypt(data)
    enc_path  = input_path + ".enc"
    with open(enc_path, "wb") as f:
        f.write(encrypted)
    os.remove(input_path)
    return enc_path

def decrypt_file(enc_path: str) -> bytes:
    with open(enc_path, "rb") as f:
        encrypted = f.read()
    return FERNET.decrypt(encrypted)

# ── Groq API setup ────────────────────────────────────────────────────────────

GROQ_API_KEY  = "YOUR_GROQ_API_KEY_HERE"
client        = Groq(api_key=GROQ_API_KEY)
SUMMARY_MODEL = "llama-3.3-70b-versatile"
INTENT_MODEL  = "llama-3.3-70b-versatile"

# ── Moonshine setup ───────────────────────────────────────────────────────────

print("Loading Moonshine base model...")
MODEL     = MoonshineOnnxModel(model_name="moonshine/base")
TOKENIZER = load_tokenizer()
print("Moonshine base model loaded.")

# ── PostgreSQL config ─────────────────────────────────────────────────────────

DB_CONFIG = {
    "host":     "localhost",
    "dbname":   "stt_db",
    "user":     "jeeven",
    "password": "12345",
}

def get_db():
    return psycopg2.connect(**DB_CONFIG)

def db_insert_recording(audio_path: str, device_id: str) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recordings (audio_file, status, device_id) VALUES (%s, %s, %s) RETURNING id;",
                (audio_path, "uploaded", device_id)
            )
            return cur.fetchone()[0]

def db_insert_transcript(recording_id: int, transcript_path: str, device_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO transcripts (recording_id, text, device_id) VALUES (%s, %s, %s);",
                (recording_id, transcript_path, device_id)
            )

def db_insert_summary(recording_id: int, summary_path: str, device_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO summaries (recording_id, summary, device_id) VALUES (%s, %s, %s);",
                (recording_id, summary_path, device_id)
            )

def db_update_status(recording_id: int, status: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recordings SET status = %s WHERE id = %s;",
                (status, recording_id)
            )

def db_save_intent(recording_id: int, intent: dict, device_id: str) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO intents (
                    recording_id, task, temporal_cue, raw_quote,
                    semantic_score, temporal_score, cognitive_score,
                    fused_confidence, decision, action,
                    cognitive_signals, status, scheduled_time, device_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id;
            """, (
                recording_id,
                intent.get("task", ""),
                intent.get("temporal_cue", ""),
                intent.get("raw_quote", ""),
                intent.get("semantic_score", 0.0),
                intent.get("temporal_score", 0.0),
                intent.get("cognitive_score", 0.0),
                intent.get("fused_confidence", 0.0),
                intent.get("decision", "log"),
                intent.get("action", "log_only"),
                json.dumps(intent.get("cognitive_signals", {})),
                "pending",
                intent.get("scheduled_time"),
                device_id,
            ))
            return cur.fetchone()[0]

# ── audio conversion ──────────────────────────────────────────────────────────

def convert_to_wav(input_path: str) -> str:
    ext = os.path.splitext(input_path)[1].lower()
    if ext == ".wav":
        return input_path
    wav_path = os.path.splitext(input_path)[0] + ".wav"
    print(f"[Pipeline] Converting {ext} → WAV ...")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", wav_path],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not os.path.exists(wav_path):
        print(f"[Pipeline] FFmpeg error: {result.stderr}")
        return input_path
    print(f"[Pipeline] Converted → {wav_path}")
    return wav_path

# ── voice activity detection ──────────────────────────────────────────────────

def has_speech(wav_path: str, aggressiveness: int = 2, min_speech_ratio: float = 0.1) -> bool:
    try:
        vad = webrtcvad.Vad(aggressiveness)
        with wave.open(wav_path, "rb") as wf:
            sample_rate  = wf.getframerate()
            n_channels   = wf.getnchannels()
            sample_width = wf.getsampwidth()
            if sample_rate not in (8000, 16000, 32000, 48000):
                print(f"[VAD] Unsupported sample rate {sample_rate} — assuming speech")
                return True
            if n_channels != 1 or sample_width != 2:
                print("[VAD] Not mono 16-bit — assuming speech")
                return True
            frame_ms          = 30
            samples_per_frame = int(sample_rate * frame_ms / 1000)
            bytes_per_frame   = samples_per_frame * sample_width
            speech_frames = 0
            total_frames  = 0
            while True:
                frame = wf.readframes(samples_per_frame)
                if len(frame) < bytes_per_frame:
                    break
                total_frames += 1
                if vad.is_speech(frame, sample_rate):
                    speech_frames += 1
        if total_frames == 0:
            return False
        speech_ratio = speech_frames / total_frames
        print(f"[VAD] Speech ratio: {speech_ratio:.2f} ({speech_frames}/{total_frames} frames)")
        return speech_ratio >= min_speech_ratio
    except Exception as e:
        print(f"[VAD] Error: {e} — assuming speech")
        return True

# ── repetition detection ──────────────────────────────────────────────────────

def is_repetitive(text: str, previous_parts: list, threshold: int = 5) -> bool:
    if not previous_parts or not text:
        return False
    phrases = [p.strip() for p in text.replace(",", ".").split(".") if p.strip()]
    for phrase in phrases:
        count = sum(1 for part in previous_parts if phrase.lower() in part.lower())
        if count >= threshold:
            return True
    return False

# ── transcription ─────────────────────────────────────────────────────────────

def transcribe_chunked(audio: np.ndarray, chunk_sec: int = 5, sr: int = 16000) -> str:
    """
    Split audio into 5s chunks — smaller chunks prevent empty transcription
    caused by Moonshine needing a few seconds to warm up on new audio.
    """
    chunk_samples = chunk_sec * sr
    total_samples = audio.shape[1]
    chunks = [audio[:, i:i+chunk_samples] for i in range(0, total_samples, chunk_samples)]
    parts  = []
    for idx, chunk in enumerate(chunks):
        rms = np.sqrt(np.mean(chunk**2))
        if rms < 0.001:
            print(f"    Chunk {idx+1}/{len(chunks)} skipped (silence)")
            continue
        print(f"    Transcribing chunk {idx+1}/{len(chunks)}...")
        tokens = MODEL.generate(chunk)
        text   = TOKENIZER.decode_batch(tokens)[0].strip()
        if not text:
            continue
        if is_repetitive(text, parts):
            print(f"    Chunk {idx+1} repetition detected — stopping early")
            break
        parts.append(text)
    return " ".join(parts)

# ── keyword check ─────────────────────────────────────────────────────────────

DANGER_KEYWORDS = [
    "help", "pain", "fall", "fell", "hurt", "emergency",
    "scared", "alone", "bleeding", "chest", "dizzy", "faint", "breathe",
]
FORGETTING_KEYWORDS = [
    "where am i", "who are you", "what day", "what year",
    "i forgot", "i don't remember", "i can't remember",
    "what is my name", "i don't know where",
]

def keyword_check(text: str) -> dict:
    text_lower = text.lower()
    return {
        "danger":     [kw for kw in DANGER_KEYWORDS     if kw in text_lower],
        "forgetting": [kw for kw in FORGETTING_KEYWORDS if kw in text_lower],
    }

# ── cognitive signal analysis ─────────────────────────────────────────────────

FILLER_WORDS   = ["uh", "um", "er", "ah", "like", "you know", "i mean", "so", "well", "basically", "actually"]
TEMPORAL_WORDS = ["later", "tomorrow", "tonight", "today", "afternoon", "morning", "evening", "next week", "soon", "at", "by"]

def cognitive_signals(text: str) -> dict:
    words          = text.lower().split()
    total_words    = len(words) if words else 1
    filler_count   = sum(1 for w in words if w in FILLER_WORDS)
    filler_density = round(filler_count / total_words, 3)
    phrases        = [" ".join(words[i:i+3]) for i in range(len(words)-2)]
    repeated       = len(phrases) - len(set(phrases))
    hesitation_score = min(1.0, round(filler_density * 3 + (repeated / total_words), 3))
    return {
        "filler_count":     filler_count,
        "filler_density":   filler_density,
        "repeated_phrases": repeated,
        "hesitation_score": hesitation_score,
    }

def temporal_score(text: str) -> float:
    text_lower = text.lower()
    found      = [t for t in TEMPORAL_WORDS if t in text_lower]
    if not found:
        return 0.0
    specific = [t for t in found if t in ["at", "by", "tomorrow", "tonight", "next week"]]
    return min(1.0, round(0.4 + (len(specific) * 0.2) + (len(found) * 0.1), 2))

# ── scheduled time parser ─────────────────────────────────────────────────────

def parse_scheduled_time(temporal_cue: str):
    if not temporal_cue:
        return None
    now = datetime.now()
    cue = temporal_cue.lower().strip()
    if "tonight" in cue or "this evening" in cue:
        return now.replace(hour=19, minute=0, second=0, microsecond=0)
    if "tomorrow morning" in cue:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if "tomorrow" in cue:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if "this afternoon" in cue or "afternoon" in cue:
        return now.replace(hour=14, minute=0, second=0, microsecond=0)
    if "this morning" in cue or "morning" in cue:
        return now.replace(hour=9, minute=0, second=0, microsecond=0)
    if "next week" in cue:
        return now + timedelta(weeks=1)
    if "later" in cue or "soon" in cue:
        return now + timedelta(hours=2)
    time_match = re.search(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", cue)
    if time_match:
        hour   = int(time_match.group(1))
        minute = int(time_match.group(2)) if time_match.group(2) else 0
        ampm   = time_match.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled < now:
            scheduled += timedelta(days=1)
        return scheduled
    return None

# ── confidence fusion layer ───────────────────────────────────────────────────

def fuse_confidence(semantic: float, temporal: float, cognitive: dict) -> float:
    hesitation    = cognitive.get("hesitation_score", 0.0)
    cognitive_scr = round(max(0.3, 1.0 - (hesitation * 0.7)), 3)
    fused = (
        WEIGHTS["semantic"]  * semantic     +
        WEIGHTS["temporal"]  * temporal     +
        WEIGHTS["cognitive"] * cognitive_scr
    )
    return round(min(1.0, fused), 3)

# ── decision engine ───────────────────────────────────────────────────────────

def decide(fused_confidence: float) -> dict:
    if fused_confidence >= THRESHOLD_HIGH:
        return {"decision": "act",     "action": "notify_caregiver",  "description": "✅ HIGH — Act immediately"}
    elif fused_confidence >= THRESHOLD_MEDIUM:
        return {"decision": "confirm", "action": "ask_caregiver",     "description": "⚠️  MEDIUM — Confirm with caregiver"}
    elif fused_confidence >= THRESHOLD_LOW:
        return {"decision": "log",     "action": "log_only",          "description": "🔹 LOW — Log only"}
    else:
        return {"decision": "ignore",  "action": "none",              "description": "❌ IGNORE — Below threshold"}

# ── intent extraction ─────────────────────────────────────────────────────────

def extract_intents(transcript: str, recording_id: int, device_id: str) -> list:
    cog = cognitive_signals(transcript)

    prompt = f"""You are an intent extraction system for a dementia patient monitoring device.

Analyse this transcript and extract any tasks, reminders or intentions the patient expresses.

Look for:
- Semantic intent: "I need to...", "I should...", "remind me to...", "don't forget to...", "I want to...", "I have to..."
- Temporal cues: "later", "tomorrow", "tonight", "at [time]", "this afternoon"
- Hesitant or incomplete sentences still count as valid intent

Cognitive signals detected:
- Filler density: {cog['filler_density']} (0=fluent, 1=very hesitant)
- Hesitation score: {cog['hesitation_score']}

IMPORTANT:
- Only extract genuine PATIENT intentions, not caregiver instructions
- A hesitant "uh... I think I need to... um... pick up medicine" IS valid
- Never extract vague statements with no clear action
- semantic_score: 0.0-1.0 — how clearly is this a genuine intent?
- If no intents found return empty list

Respond ONLY in valid JSON:
{{
  "intents": [
    {{
      "task": "plain text task e.g. cook dinner",
      "temporal_cue": "when e.g. tonight, or empty string",
      "semantic_score": 0.0 to 1.0,
      "raw_quote": "exact phrase from transcript"
    }}
  ]
}}

Transcript:
{transcript}
"""
    try:
        completion  = client.chat.completions.create(
            model=INTENT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        result      = json.loads(completion.choices[0].message.content)
        raw_intents = result.get("intents", [])
        processed   = []

        for intent in raw_intents:
            semantic  = float(intent.get("semantic_score", 0.0))
            temp_cue  = intent.get("temporal_cue", "")
            temp_scr  = temporal_score(temp_cue + " " + intent.get("task", ""))
            fused     = fuse_confidence(semantic, temp_scr, cog)
            decision  = decide(fused)
            scheduled_time = parse_scheduled_time(temp_cue)

            full_intent = {
                "task":             intent.get("task", ""),
                "temporal_cue":     temp_cue,
                "raw_quote":        intent.get("raw_quote", ""),
                "semantic_score":   round(semantic, 3),
                "temporal_score":   round(temp_scr, 3),
                "cognitive_score":  round(max(0.3, 1.0 - (cog["hesitation_score"] * 0.7)), 3),
                "fused_confidence": fused,
                "decision":         decision["decision"],
                "action":           decision["action"],
                "cognitive_signals": cog,
                "scheduled_time":   scheduled_time.isoformat() if scheduled_time else None,
            }

            intent_id = db_save_intent(recording_id, full_intent, device_id)
            full_intent["id"] = intent_id
            processed.append(full_intent)

            print(f"[Intent] {decision['description']}")
            print(f"         Task: '{full_intent['task']}'")
            print(f"         Temporal cue: '{temp_cue}'")
            print(f"         Scheduled: {scheduled_time or 'not scheduled'}")
            print(f"         Scores → semantic={semantic:.2f} temporal={temp_scr:.2f} fused={fused:.2f}")
            print(f"         Device: {device_id}")

        if not processed:
            print("[Intent] No intents detected.")

        return processed

    except Exception as e:
        print(f"  [Intent Error]: {e}")
        return []

# ── LLM summary analysis ──────────────────────────────────────────────────────

def llm_analyse(transcript: str, kw: dict = None) -> dict:
    if kw is None:
        kw = {"danger": [], "forgetting": []}

    prompt = f"""You are a medical assistant monitoring a dementia patient who is wearing a recording device.

IMPORTANT CONTEXT:
- The transcript may contain MULTIPLE speakers — the patient and other people
- Identify who is likely the PATIENT based on context
- Caregivers asking "are you okay?" or "do you remember?" are NOT patient distress
- Only flag danger or forgetting if the PATIENT themselves expresses it

Respond ONLY in valid JSON:
{{
  "speakers_identified": "brief description of who is in the conversation",
  "patient_identified_as": "how you identified the patient",
  "summary": "detailed paragraph of full conversation, mood, coherence and concerns",
  "danger_detected": true if the PATIENT is genuinely in distress or danger - otherwise false,
  "danger_details": "describe the moment in detail, or empty string",
  "forgetting_detected": true if the PATIENT shows clear confusion or memory loss - otherwise false,
  "forgetting_details": "describe the signs in detail, or empty string",
  "alert_needed": true if a caregiver genuinely needs to be notified right now - otherwise false
}}

Keywords detected (hints only): {kw}

Transcript:
{transcript}
"""
    try:
        completion = client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(completion.choices[0].message.content)
    except Exception as e:
        print(f"  [Groq Error]: {e}")
        return {
            "speakers_identified":   "Unknown",
            "patient_identified_as": "Unknown",
            "summary":               "Analysis unavailable.",
            "danger_detected":       False,
            "danger_details":        "",
            "forgetting_detected":   False,
            "forgetting_details":    "",
            "alert_needed":          False,
        }

# ── full pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(save_path: str, filename: str, recording_id: int, device_id: str):
    base_name = os.path.splitext(filename)[0]
    print(f"\n{'='*60}")
    print(f"[Pipeline] Starting for {filename} (id={recording_id}, device={device_id})")

    try:
        # Step 1 — Convert to WAV
        print("[Pipeline] Step 1 — Converting to WAV...")
        wav_path = convert_to_wav(save_path)

        # Step 2 — VAD
        print("[Pipeline] Step 2 — Voice Activity Detection...")
        if not has_speech(wav_path):
            print("[Pipeline] No speech detected — skipping transcription")
            db_update_status(recording_id, "no_speech")
            encrypt_file(wav_path)
            return
        print("[Pipeline] Speech detected — proceeding")

        # Step 3 — Transcribe
        print("[Pipeline] Step 3 — Transcribing...")
        audio           = moonshine_load_audio(wav_path)
        duration_sec    = audio.shape[1] / 16000
        print(f"[Pipeline] Duration: {duration_sec:.1f}s")
        transcript_text = transcribe_chunked(audio)

        if not transcript_text:
            print(f"[Pipeline] Empty transcript for {filename}")
            db_update_status(recording_id, "transcription_failed")
            return

        # Step 4 — Encrypt and save transcript
        print("[Pipeline] Step 4 — Saving encrypted transcript...")
        transcript_path     = os.path.join(TRANSCRIPT_FOLDER, base_name + ".txt")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript_text)
        enc_transcript_path = encrypt_file(transcript_path)
        db_insert_transcript(recording_id, enc_transcript_path, device_id)
        db_update_status(recording_id, "transcribed")
        print(f"[Pipeline] Encrypted transcript → {enc_transcript_path}")

        # Step 5 — Encrypt audio
        print("[Pipeline] Step 5 — Encrypting audio...")
        enc_audio_path = encrypt_file(wav_path)
        print(f"[Pipeline] Encrypted audio → {enc_audio_path}")

        # Step 6 — Keyword check
        print("[Pipeline] Step 6 — Keyword check...")
        kw = keyword_check(transcript_text)
        if kw["danger"]:
            print(f"[Pipeline] ⚠️  Danger keywords: {kw['danger']}")
        if kw["forgetting"]:
            print(f"[Pipeline] ⚠️  Forgetting keywords: {kw['forgetting']}")

        # Step 7 — LLM summary
        print("[Pipeline] Step 7 — LLM summary analysis...")
        analysis = llm_analyse(transcript_text, kw)
        print(f"[Pipeline] Speakers:  {analysis.get('speakers_identified', '')}")
        print(f"[Pipeline] Patient:   {analysis.get('patient_identified_as', '')}")
        print(f"[Pipeline] Summary:   {analysis.get('summary', '')[:120]}...")
        print(f"[Pipeline] Danger: {analysis.get('danger_detected', False)} | Forgetting: {analysis.get('forgetting_detected', False)}")

        # Step 8 — Intent extraction
        print("[Pipeline] Step 8 — Intent extraction + confidence fusion...")
        intents = extract_intents(transcript_text, recording_id, device_id)

        # Step 9 — Save encrypted summary
        print("[Pipeline] Step 9 — Saving encrypted summary...")
        alert_needed = (
            analysis.get("alert_needed",        False) or
            analysis.get("danger_detected",     False) or
            analysis.get("forgetting_detected", False)
        )
        high_confidence_intents = [i for i in intents if i.get("decision") == "act"]

        summary_data = {
            "recording_id":            recording_id,
            "device_id":               device_id,
            "transcript_file":         enc_transcript_path,
            "audio_file":              enc_audio_path,
            "alert_needed":            alert_needed,
            "keyword_check":           kw,
            "speakers_identified":     analysis.get("speakers_identified", ""),
            "patient_identified":      analysis.get("patient_identified_as", ""),
            "intents_extracted":       len(intents),
            "high_confidence_intents": len(high_confidence_intents),
            "intent_summary": [
                {
                    "task":             i.get("task"),
                    "temporal_cue":     i.get("temporal_cue"),
                    "fused_confidence": i.get("fused_confidence"),
                    "decision":         i.get("decision"),
                    "scheduled_time":   i.get("scheduled_time"),
                }
                for i in intents
            ],
            **analysis,
        }
        summary_path     = os.path.join(SUMMARY_FOLDER, base_name + "_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)
        enc_summary_path = encrypt_file(summary_path)
        db_insert_summary(recording_id, enc_summary_path, device_id)
        print(f"[Pipeline] Encrypted summary → {enc_summary_path}")

        # Step 10 — Flag status
        if alert_needed:
            db_update_status(recording_id, "alert_flagged")
            print(f"[Pipeline] 🚨 ALERT FLAGGED — device={device_id} recording_id={recording_id}")
            if analysis.get("danger_detected"):
                print(f"[Pipeline] DANGER: {analysis.get('danger_details') or kw['danger']}")
            if analysis.get("forgetting_detected"):
                print(f"[Pipeline] FORGETTING: {analysis.get('forgetting_details') or kw['forgetting']}")
        elif high_confidence_intents:
            db_update_status(recording_id, "intent_flagged")
            print(f"[Pipeline] 📋 {len(high_confidence_intents)} high-confidence intent(s) — device={device_id}")
        else:
            db_update_status(recording_id, "completed")
            print("[Pipeline] ✅ No alert needed.")

    except Exception as e:
        print(f"[Pipeline] Error: {e}")
        db_update_status(recording_id, "error")

    print(f"[Pipeline] Done for {filename} (device={device_id})")
    print(f"{'='*60}\n")

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return "MCU Flask server is running!"

@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    # Get device_id from form field — default to "unknown" if not provided
    device_id = request.form.get("device_id", "unknown").strip()

    filename  = secure_filename(file.filename)
    save_path = os.path.join(AUDIO_FOLDER, filename)
    file.save(save_path)
    print(f"File saved: {save_path} (device={device_id})")

    try:
        recording_id = db_insert_recording(save_path, device_id)
    except Exception as e:
        print(f"DB error: {e}")
        return jsonify({"error": "Database error"}), 500

    Thread(target=run_pipeline, args=(save_path, filename, recording_id, device_id)).start()

    return jsonify({
        "message":      f"File {filename} received. Pipeline running...",
        "recording_id": recording_id,
        "device_id":    device_id,
    }), 200

@app.route("/status/<filename>", methods=["GET"])
def check_status(filename):
    safe_name       = secure_filename(filename)
    base_name       = os.path.splitext(safe_name)[0]
    transcript_path = os.path.join(TRANSCRIPT_FOLDER, base_name + ".txt.enc")
    summary_path    = os.path.join(SUMMARY_FOLDER,    base_name + "_summary.json.enc")
    if os.path.exists(summary_path):
        return jsonify({"status": "done", "summary_path": summary_path}), 200
    elif os.path.exists(transcript_path):
        return jsonify({"status": "transcribed", "transcript_path": transcript_path}), 200
    return jsonify({"status": "pending"}), 200

@app.route("/recordings", methods=["GET"])
def list_recordings():
    """List all recordings. Optionally filter by device_id: /recordings?device_id=esp32_01"""
    try:
        device_id = request.args.get("device_id")
        with get_db() as conn:
            with conn.cursor() as cur:
                if device_id:
                    cur.execute("""
                        SELECT r.id, r.audio_file, r.status, r.created_at, r.device_id,
                               t.text AS transcript_path, s.summary AS summary_path
                        FROM recordings r
                        LEFT JOIN transcripts t ON t.recording_id = r.id
                        LEFT JOIN summaries   s ON s.recording_id = r.id
                        WHERE r.device_id = %s
                        ORDER BY r.created_at DESC;
                    """, (device_id,))
                else:
                    cur.execute("""
                        SELECT r.id, r.audio_file, r.status, r.created_at, r.device_id,
                               t.text AS transcript_path, s.summary AS summary_path
                        FROM recordings r
                        LEFT JOIN transcripts t ON t.recording_id = r.id
                        LEFT JOIN summaries   s ON s.recording_id = r.id
                        ORDER BY r.created_at DESC;
                    """)
                rows = cur.fetchall()
        return jsonify([
            {
                "id": row[0], "audio_file": row[1], "status": row[2],
                "created_at": str(row[3]), "device_id": row[4],
                "transcript_path": row[5], "summary_path": row[6],
            }
            for row in rows
        ]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/alerts", methods=["GET"])
def get_alerts():
    """Get alerts. Optionally filter by device_id: /alerts?device_id=esp32_01"""
    try:
        device_id = request.args.get("device_id")
        with get_db() as conn:
            with conn.cursor() as cur:
                if device_id:
                    cur.execute("""
                        SELECT r.id, r.audio_file, r.created_at, r.device_id, s.summary
                        FROM recordings r
                        JOIN summaries s ON s.recording_id = r.id
                        WHERE r.status IN ('alert_flagged', 'intent_flagged')
                        AND r.device_id = %s
                        ORDER BY r.created_at DESC;
                    """, (device_id,))
                else:
                    cur.execute("""
                        SELECT r.id, r.audio_file, r.created_at, r.device_id, s.summary
                        FROM recordings r
                        JOIN summaries s ON s.recording_id = r.id
                        WHERE r.status IN ('alert_flagged', 'intent_flagged')
                        ORDER BY r.created_at DESC;
                    """)
                rows = cur.fetchall()
        return jsonify([
            {
                "id": row[0], "audio_file": row[1],
                "created_at": str(row[2]), "device_id": row[3],
                "summary_path": row[4],
            }
            for row in rows
        ]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/intents", methods=["GET"])
def get_intents():
    """Get intents. Optionally filter by device_id: /intents?device_id=esp32_01"""
    try:
        device_id = request.args.get("device_id")
        with get_db() as conn:
            with conn.cursor() as cur:
                if device_id:
                    cur.execute("""
                        SELECT id, recording_id, task, temporal_cue, fused_confidence,
                               decision, action, status, scheduled_time, reminder_sent,
                               device_id, created_at
                        FROM intents WHERE device_id = %s
                        ORDER BY created_at DESC LIMIT 50;
                    """, (device_id,))
                else:
                    cur.execute("""
                        SELECT id, recording_id, task, temporal_cue, fused_confidence,
                               decision, action, status, scheduled_time, reminder_sent,
                               device_id, created_at
                        FROM intents ORDER BY created_at DESC LIMIT 50;
                    """)
                rows = cur.fetchall()
        return jsonify([
            {
                "id": row[0], "recording_id": row[1], "task": row[2],
                "temporal_cue": row[3], "fused_confidence": row[4],
                "decision": row[5], "action": row[6], "status": row[7],
                "scheduled_time": str(row[8]) if row[8] else None,
                "reminder_sent": row[9], "device_id": row[10],
                "created_at": str(row[11]),
            }
            for row in rows
        ]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/intents/<int:recording_id>", methods=["GET"])
def get_intents_for_recording(recording_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, task, temporal_cue, fused_confidence, decision,
                           status, scheduled_time, reminder_sent, device_id, created_at
                    FROM intents WHERE recording_id = %s ORDER BY created_at DESC;
                """, (recording_id,))
                rows = cur.fetchall()
        return jsonify([
            {
                "id": row[0], "task": row[1], "temporal_cue": row[2],
                "fused_confidence": row[3], "decision": row[4], "status": row[5],
                "scheduled_time": str(row[6]) if row[6] else None,
                "reminder_sent": row[7], "device_id": row[8], "created_at": str(row[9]),
            }
            for row in rows
        ]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reminders/pending", methods=["GET"])
def get_pending_reminders():
    """Pending reminders for app to poll. Filter by device_id: /reminders/pending?device_id=esp32_01"""
    try:
        device_id = request.args.get("device_id")
        with get_db() as conn:
            with conn.cursor() as cur:
                if device_id:
                    cur.execute("""
                        SELECT id, recording_id, task, temporal_cue, fused_confidence,
                               decision, scheduled_time, device_id, created_at
                        FROM intents
                        WHERE reminder_sent = FALSE AND status = 'pending'
                        AND decision IN ('act', 'confirm')
                        AND (scheduled_time IS NULL OR scheduled_time <= NOW())
                        AND device_id = %s
                        ORDER BY created_at ASC;
                    """, (device_id,))
                else:
                    cur.execute("""
                        SELECT id, recording_id, task, temporal_cue, fused_confidence,
                               decision, scheduled_time, device_id, created_at
                        FROM intents
                        WHERE reminder_sent = FALSE AND status = 'pending'
                        AND decision IN ('act', 'confirm')
                        AND (scheduled_time IS NULL OR scheduled_time <= NOW())
                        ORDER BY created_at ASC;
                    """)
                rows = cur.fetchall()
        return jsonify([
            {
                "id": row[0], "recording_id": row[1], "task": row[2],
                "temporal_cue": row[3], "fused_confidence": row[4],
                "decision": row[5],
                "scheduled_time": str(row[6]) if row[6] else None,
                "device_id": row[7], "created_at": str(row[8]),
            }
            for row in rows
        ]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reminders/<int:intent_id>/sent", methods=["POST"])
def mark_reminder_sent(intent_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE intents SET reminder_sent = TRUE WHERE id = %s;",
                    (intent_id,)
                )
        return jsonify({"message": f"Reminder {intent_id} marked as sent"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/intents/<int:intent_id>/feedback", methods=["POST"])
def intent_feedback(intent_id):
    try:
        data    = request.get_json()
        outcome = data.get("outcome", "ignored")
        if outcome not in ["accepted", "rejected", "ignored"]:
            return jsonify({"error": "outcome must be accepted, rejected or ignored"}), 400
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO intent_feedback (intent_id, outcome) VALUES (%s, %s);",
                    (intent_id, outcome)
                )
                cur.execute(
                    "UPDATE intents SET status = %s WHERE id = %s;",
                    (outcome, intent_id)
                )
        return jsonify({"message": f"Intent {intent_id} marked as {outcome}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/learning/stats", methods=["GET"])
def learning_stats():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT i.decision, COUNT(*) as total,
                           SUM(CASE WHEN f.outcome = 'accepted' THEN 1 ELSE 0 END) as accepted,
                           SUM(CASE WHEN f.outcome = 'rejected' THEN 1 ELSE 0 END) as rejected,
                           ROUND(AVG(i.fused_confidence)::numeric, 3) as avg_confidence
                    FROM intents i
                    LEFT JOIN intent_feedback f ON f.intent_id = i.id
                    GROUP BY i.decision;
                """)
                rows = cur.fetchall()
        return jsonify([
            {
                "decision": row[0], "total": row[1],
                "accepted": row[2], "rejected": row[3],
                "avg_confidence": float(row[4]) if row[4] else 0,
            }
            for row in rows
        ]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/decrypt/transcript/<int:recording_id>", methods=["GET"])
def get_transcript(recording_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT text FROM transcripts WHERE recording_id = %s;",
                    (recording_id,)
                )
                row = cur.fetchone()
        if not row:
            return jsonify({"error": "Transcript not found"}), 404
        enc_path = row[0]
        if not os.path.exists(enc_path):
            return jsonify({"error": "Encrypted file not found on disk"}), 404
        text = decrypt_file(enc_path).decode("utf-8")
        return jsonify({"recording_id": recording_id, "transcript": text}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/decrypt/summary/<int:recording_id>", methods=["GET"])
def get_summary(recording_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT summary FROM summaries WHERE recording_id = %s;",
                    (recording_id,)
                )
                row = cur.fetchone()
        if not row:
            return jsonify({"error": "Summary not found"}), 404
        enc_path = row[0]
        if not os.path.exists(enc_path):
            return jsonify({"error": "Encrypted file not found on disk"}), 404
        data = json.loads(decrypt_file(enc_path).decode("utf-8"))
        return jsonify({"recording_id": recording_id, "summary": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)