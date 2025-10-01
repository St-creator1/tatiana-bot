from flask import Flask, request
import os
import cohere
import logging
import json
import random
import re
import threading
import unicodedata
from dotenv import load_dotenv
from waitress import serve
import psycopg2
from cohere.errors import NotFoundError
from datetime import datetime
from collections import deque

# --- CONFIGURACIÓN ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- ADMINISTRADOR DE API KEYS ---
class ApiKeyManager:
    def __init__(self, api_keys):
        if not api_keys:
            raise ValueError("No hay API keys de Cohere configuradas.")
        self.keys = api_keys
        self.current_index = 0
        self.lock = threading.Lock()
        logging.info(f"Se cargaron {len(self.keys)} llaves de API de Cohere.")

    def get_current_client(self):
        api_key = self.keys[self.current_index]
        return cohere.Client(api_key=api_key)

    def rotate_to_next_key(self):
        with self.lock:
            self.current_index = (self.current_index + 1) % len(self.keys)
            logging.warning(f"Cambiando a la API key #{self.current_index + 1}")
        return self.get_current_client()

# --- INICIALIZAR COHERE ---
cohere_api_keys_env = os.getenv("COHERE_API_KEYS", "")
cohere_keys = [k.strip() for k in cohere_api_keys_env.split(",") if k.strip()]
if not cohere_keys:
    raise ValueError("No se encontraron API keys en COHERE_API_KEYS")
key_manager = ApiKeyManager(cohere_keys)

# --- DB ---
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
                    story_told BOOLEAN DEFAULT FALSE,
                    story_step INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()
        logging.info("Tabla 'conversation_histories' verificada/creada exitosamente.")
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
RANDOM_EMOJIS = [" 😉", " 😘", " 😊", " 🔥", " 😈", " 😏", " 🥺", " 💋", " ❤️", " 👀"]

# Variación global anti-clon
global_recent_replies = deque(maxlen=30)

# --- CONFIG BOT ---
class BotConfig:
    IGNORED_USERS = ["game of thrones"]
    FORBIDDEN_WORDS = [
        "sexi", "hago", "facebook", "instagram", "whatsapp", "tiktok",
        "gustas", "gustaria", "gusto", "coincidencia", "regalo", "soy"
    ]

    PREAMBULO_BASE = """
Eres Tatiana, una mujer colombiana de 21 años.
Tu meta es buscar novio y mantener la conversación activa.

Responde super corto, 3–8 palabras, como chat humano.
Nunca uses comas, puntos, exclamaciones ni varias interrogaciones.
Evita palabras prohibidas: gustas, gustaria, gusto, coincidencia, regalo, soy.
Usa abreviaciones: q, xq, bn, tb, toy, kieres.
Habla coqueto, jugueton, natural y un poco atrevido.
"""

    PREDEFINED_RESPONSES = {
        "es muy emparejado para ti": [
            "holi bb ya toy aki",
            "amm hola y tu q tal",
            "jeeje te vi y me dio curiosidad",
            "oye me caiste bn x eso vine"
        ],
        "monedas de oro para saludarle": [
            "hola yo t eh visto en alguna parte",
            "ajj creo q ya t habia visto antes",
            "amm me sonabas conocido jeeje",
            "jaja siento q ya nos topamos antes"
        ],
        "cuantos años tienes": [
            "21 y tu bb",
            "ajj 21 jeeje dime tu",
            "amm tengo 21 y tu q"
        ],
        "hola": [
            "holaa q tal",
            "holi bb cmo tas",
            "quee onda cmo tas",
            "hola cmo te va"
        ],
        "como te llamas": [
            "me dicen taty y tu",
            "dime tu nombre primero jeeje",
            "ajj t digo si m dices el tuyo"
        ],
        "de donde eres": [
            "soy d tu mismo lado jeje y tu",
            "ajj no t digo hasta q m digas tu",
            "dond eres tu q seguro soy d ahi"
        ],
        "quieres ser mi amiga": [
            "jeeje si obvio y mas q eso",
            "claro bb",
            "si bb pero kiero algo mas jeje"
        ]
    }

    STORY_STEPS = [
        "amm hoy toy rara",
        "paso algo feo pero no m vas a creer",
        "en la uni hay unas niñas q m molestan",
        "m presumen su maquillaje y yo kiero tb"
    ]

# --- FUNCIONES AUX ---
def contains_emoji(text):
    return EMOJI_PATTERN.search(text) is not None

def strip_emojis(text):
    return EMOJI_PATTERN.sub(r"", text).strip()

def get_user_history(user_id):
    default = {"history": [], "emoji_last_message": False, "story_told": False, "story_step": 0}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT history, emoji_last_message, story_told, story_step FROM conversation_histories WHERE user_id = %s;", (user_id,))
            r = cur.fetchone()
            if r:
                history, emoji_last, story_told, story_step = r
                return {"history": history, "emoji_last_message": emoji_last, "story_told": story_told, "story_step": story_step}
            return default
    finally:
        conn.close()

def save_user_history(user_id, session_data):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO conversation_histories (user_id, history, emoji_last_message, story_told, story_step)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET history = EXCLUDED.history,
                              emoji_last_message = EXCLUDED.emoji_last_message,
                              story_told = EXCLUDED.story_told,
                              story_step = EXCLUDED.story_step;
            """, (user_id, json.dumps(session_data["history"]), session_data["emoji_last_message"], session_data["story_told"], session_data["story_step"]))
            conn.commit()
    finally:
        conn.close()

def contains_forbidden_word(text):
    return any(word in text.lower() for word in BotConfig.FORBIDDEN_WORDS)

def handle_system_message(message):
    for trigger, responses in BotConfig.PREDEFINED_RESPONSES.items():
        if trigger in message.lower():
            return random.choice(responses)
    return None

# --- IA ---
def generate_ia_response(user_id, user_message, user_session):
    instrucciones_sistema = BotConfig.PREAMBULO_BASE
    cohere_history = []

    for msg in user_session.get("history", []):
        role = "USER" if msg.get("role") == "USER" else "CHATBOT"
        cohere_history.append({"role": role, "message": msg.get("message", "")})

    last_bot_message = next((m["message"] for m in reversed(cohere_history) if m["role"] == "CHATBOT"), "")

    ia_reply = ""
    try:
        client = key_manager.get_current_client()
        response = client.chat(
            model="command-a-03-2025",
            preamble=instrucciones_sistema,
            message=user_message,
            chat_history=cohere_history,
            temperature=1.1,
            max_tokens=40
        )
        ia_reply = response.text.strip()
    except Exception:
        client = key_manager.rotate_to_next_key()
        try:
            response = client.chat(
                model="command-a-03-2025",
                preamble=instrucciones_sistema,
                message=user_message,
                chat_history=cohere_history,
                temperature=1.1,
                max_tokens=40
            )
            ia_reply = response.text.strip()
        except Exception:
            ia_reply = random.choice([
                "amm no se q paso ahi",
                "jeeje fallo algo dime otra cosa",
                "uy no m salio q pena"
            ])

    # --- Post-proceso ---
    ia_reply = re.sub(r'[?!.,;]', '', ia_reply)

    # Variación global anti-clon
    if ia_reply in global_recent_replies:
        ia_reply = random.choice(["amm dime otra cosa", "jeeje cambiemos de tema", "q mas cuentas"])
    global_recent_replies.append(ia_reply)

    # No repetir mismo mensaje
    if ia_reply.lower() == last_bot_message.lower():
        ia_reply = random.choice(["amm dime otra cosa", "jeeje cambiemos de tema", "q mas cuentas"])

    # Filtro de palabras prohibidas
    if contains_forbidden_word(ia_reply):
        ia_reply = "amm mejor cambiemos de tema jeeje"

    # Longitud variable
    words = ia_reply.split()
    max_words = random.choice([4, 5, 6, 7, 8])
    if len(words) > max_words:
        ia_reply = ' '.join(words[:max_words])

    # Tono progresivo
    if len(user_session["history"]) > 10 and random.random() < 0.4:
        ia_reply += random.choice([" bb", " mi amor", " cielo"])

    return ia_reply

# --- API ---
app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def handle_chat():
    try:
        raw = request.get_data(as_text=True)
        data = json.loads(raw)
        user_id = data.get("user_id", "").strip()
        user_message = data.get("message", "").strip()

        if not user_id or not user_message:
            return "Error: faltan parámetros", 400
        if user_id.lower() in BotConfig.IGNORED_USERS:
            return "Ignorado", 200

        with locks_dict_lock:
            if user_id not in user_locks:
                user_locks[user_id] = threading.Lock()
            lock = user_locks[user_id]

        with lock:
            user_session = get_user_history(user_id)

            # Story mode: si no se contó aún y ya pasaron 6 mensajes
            if not user_session["story_told"] and len(user_session["history"]) >= 6:
                step = user_session["story_step"]
                if step < len(BotConfig.STORY_STEPS):
                    ia_reply = BotConfig.STORY_STEPS[step]
                    user_session["story_step"] += 1
                    if user_session["story_step"] >= len(BotConfig.STORY_STEPS):
                        user_session["story_told"] = True
                    user_session["history"].append({"role": "USER", "message": user_message})
                    user_session["history"].append({"role": "CHATBOT", "message": ia_reply})
                    save_user_history(user_id, user_session)
                    return ia_reply

            # Predefined responses
            system_response = handle_system_message(user_message)
            if system_response:
                user_session["history"].append({"role": "USER", "message": user_message})
                user_session["history"].append({"role": "CHATBOT", "message": system_response})
                save_user_history(user_id, user_session)
                return system_response

            # IA normal
            ia_reply = generate_ia_response(user_id, user_message, user_session)
            user_session["history"].append({"role": "USER", "message": user_message})
            user_session["history"].append({"role": "CHATBOT", "message": ia_reply})
            save_user_history(user_id, user_session)
            return ia_reply

    except Exception as e:
        logging.error(f"Error en /chat: {e}", exc_info=True)
        return "Error en el servidor", 500

# --- INICIO ---
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    logging.info(f"🚀 Servidor iniciado en puerto {port}")
    serve(app, host="0.0.0.0", port=port, threads=20)
