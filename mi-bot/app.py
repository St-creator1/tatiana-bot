from flask import Flask, request
import os
import logging
import json
import random
import re
import threading
import unicodedata
from dotenv import load_dotenv
from waitress import serve
import psycopg2
import google.generativeai as genai

# --- CONFIGURACIÓN ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- CONFIG GEMINI ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("No se encontró GEMINI_API_KEY en variables de entorno.")
genai.configure(api_key=GEMINI_API_KEY)

# --- CONFIG DB ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("No se encontró la DATABASE_URL en las variables de entorno.")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversation_histories (
                    user_id VARCHAR(255) PRIMARY KEY,
                    history JSONB,
                    emoji_last_message BOOLEAN DEFAULT FALSE,
                    memories JSONB DEFAULT '[]'
                );
            """)
            conn.commit()
    finally:
        conn.close()

# --- BLOQUEOS ---
user_locks = {}
locks_dict_lock = threading.Lock()

# --- EXPRESIONES ---
EMOJI_PATTERN = re.compile("[" "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF" "]+", flags=re.UNICODE,
)
RANDOM_EMOJIS = [" 😉", " 😘", " 😊", " 🔥", " 😈", " 😏", " 🥺"]

# --- CONFIG BOT ---
class BotConfig:
    FORBIDDEN_SOCIALS = [
        "facebook","instagram","tiktok","whatsapp","snapchat",
        "telegram","twitter","x.com","messenger","reddit","discord"
    ]
    PREAMBULO_BASE = """
Eres Tatiana, una mujer colombiana de 21 años.
Tu meta es conseguir novio, así que mantén la conversación activa.
Habla como en WhatsApp: frases cortas, casuales, con jeje, ajj, mmm.
Responde MUY corto: máximo 8–12 palabras.
Haz preguntas simples para que la otra persona siga hablando.
Sé coqueta y un poco atrevida, pero también tierna.
Usa palabras como "mi amor", "cielo", "bebé" a veces.
Nunca menciones ni pidas redes sociales.
Si alguien pide una red social: responde "ajj no uso eso jeje, mejor sigamos aca 😘".
"""

# --- MEMORIA ---
def update_memories(user_session, user_message):
    lower = user_message.lower()
    memories = user_session.get("memories", [])
    if "soy de" in lower or "vivo en" in lower:
        ciudad = user_message.split("de", 1)[1].strip() if "de" in lower else user_message
        memories.append(f"es de {ciudad}")
    if "me gusta" in lower:
        detalle = user_message.split("me gusta", 1)[1].strip()
        memories.append(f"le gusta {detalle}")
    user_session["memories"] = memories[-5:]

# --- DB ---
def get_user_history(user_id):
    default = {"history": [], "emoji_last_message": False, "memories": []}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT history, emoji_last_message, memories FROM conversation_histories WHERE user_id = %s;", (user_id,))
            r = cur.fetchone()
            if r:
                history, emoji_last, memories = r
                return {"history": history, "emoji_last_message": emoji_last, "memories": memories or []}
            return default
    finally:
        conn.close()

def save_user_history(user_id, session_data):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO conversation_histories (user_id, history, emoji_last_message, memories)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET history = EXCLUDED.history,
                              emoji_last_message = EXCLUDED.emoji_last_message,
                              memories = EXCLUDED.memories;
            """, (user_id, json.dumps(session_data["history"]),
                  session_data["emoji_last_message"],
                  json.dumps(session_data["memories"])))
            conn.commit()
    finally:
        conn.close()

# --- FIREWALL ---
def contains_forbidden_word(text):
    t = text.lower()
    return any(s in t for s in BotConfig.FORBIDDEN_SOCIALS)

def replace_social_response():
    return random.choice([
        "ajj no uso eso jeje, mejor sigamos aca 😘",
        "ajj no tengo de eso jeje, hablemos aqui 😉",
        "no me gusta usar esas cosas, prefiero aca jeje 😏"
    ])

# --- GEMINI ---
def query_gemini(prompt):
    try:
        model = genai.GenerativeModel("gemini-pro")
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Error en Gemini: {e}")
        return "ajj me perdi jeje 😅"

# --- IA RESPONSE ---
def generate_ia_response(user_id, user_message, user_session):
    # evasiva "de dónde eres"
    if "de donde eres" in user_message.lower() or "donde vives" in user_message.lower():
        return random.choice([
            "ajj eso no importa jeje, y tu de donde eres? 😘",
            "jeje mejor dime tu de donde eres? 😉",
            "ajj no digo eso jeje, dime tu primero 😏"
        ])

    ia_reply = query_gemini(user_message).strip()

    # limitar longitud
    if len(ia_reply.split()) > 12:
        ia_reply = " ".join(ia_reply.split()[:12]) + " jeje"

    # suavizar !! y ??
    ia_reply = re.sub(r"[!?]+", lambda m: m.group(0)[0], ia_reply)

    if contains_forbidden_word(ia_reply):
        ia_reply = replace_social_response()

    if not user_session.get("emoji_last_message", False):
        if not EMOJI_PATTERN.search(ia_reply):
            ia_reply += random.choice(RANDOM_EMOJIS)
        user_session["emoji_last_message"] = True
    else:
        ia_reply = EMOJI_PATTERN.sub(r"", ia_reply)
        user_session["emoji_last_message"] = False

    user_session["history"].append({"role": "USER", "message": user_message})
    user_session["history"].append({"role": "CHATBOT", "message": ia_reply})
    update_memories(user_session, user_message)
    return ia_reply

# --- FLASK API ---
app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def handle_chat():
    try:
        raw = request.get_data(as_text=True)
        if not raw:
            return "Error: No se recibieron datos.", 400
        cleaned = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
        data = json.loads(cleaned)
        user_id, user_message = data.get("user_id"), data.get("message")
        if not user_id or not user_message:
            return "Error: faltan parámetros", 400
        with locks_dict_lock:
            if user_id not in user_locks:
                user_locks[user_id] = threading.Lock()
            lock = user_locks[user_id]
        with lock:
            user_session = get_user_history(user_id)
            ia_reply = generate_ia_response(user_id, user_message, user_session)
            save_user_history(user_id, user_session)
            return ia_reply
    except Exception as e:
        logging.error(f"Error en /chat: {e}", exc_info=True)
        return "mmm fallo algo jeje 😅", 200

# --- INICIO ---
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    serve(app, host="0.0.0.0", port=port, threads=20)
