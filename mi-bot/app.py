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
from urllib.parse import urlparse
from cohere.errors import NotFoundError  # ✅ correcto

# --- CONFIGURACIÓN ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- ADMINISTRADOR DE API KEYS DE COHERE ---
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
            logging.warning(f"🔄 Cambiando a la API key número {self.current_index + 1}")
        return self.get_current_client()

# --- INICIALIZAR COHERE ---
cohere_api_keys_env = os.getenv("COHERE_API_KEYS", "")
cohere_keys = [k.strip() for k in cohere_api_keys_env.split(",") if k.strip()]
if not cohere_keys:
    raise ValueError("No se encontraron API keys de Cohere en COHERE_API_KEYS")
key_manager = ApiKeyManager(cohere_keys)

# --- CONFIGURAR BASE DE DATOS ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("No se encontró la DATABASE_URL en las variables de entorno.")

def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logging.error(f"Error al conectar a la base de datos: {e}")
        raise

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
        logging.info("Tabla 'conversation_histories' lista.")
    finally:
        conn.close()

# --- SISTEMA DE BLOQUEO ---
user_locks = {}
locks_dict_lock = threading.Lock()

# --- DEFINICIONES GLOBALES ---
EMOJI_PATTERN = re.compile(
    "[" "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF" "]+", flags=re.UNICODE,
)
RANDOM_EMOJIS = [" 😉", " 😘", " 😊", " 🔥", " 😈", " 😏", " 🥺"]

NATURAL_QUESTIONS = [
    "y tu?",
    "mmm y cuentame",
    "ajaja en serio?",
    "tuuu q opinas?",
    "q haces ahorita?",
    "jeje y q mas?"
]

# --- CONFIG DEL BOT ---
class BotConfig:
    IGNORED_USERS = ["game of thrones"]
    FORBIDDEN_WORDS = ["sexi", "hago"]
    FORBIDDEN_SOCIALS = [
        "facebook", "instagram", "tiktok", "whatsapp",
        "snapchat", "telegram", "twitter", "x.com",
        "messenger", "reddit", "discord"
    ]

    PREDEFINED_RESPONSES = {
        "[Recordatorio en línea]": "Hola cielo como estas",
        "es muy emparejado para ti": "Holis busco novio y tú estas lindo 🥺",
        "monedas de oro para saludarle": "Holis busco novio y tú estas lindo 🥺",
        "Te he seguido. Podemos ser amigos": "Te he seguido"
    }

    PREAMBULO_BASE = """
Eres Tatiana, una mujer colombiana de 21 años.
Tu meta principal es conseguir novio, por eso siempre buscas mantener la conversación activa.
Habla como si chatearas en WhatsApp: frases cortas, casuales, con errores y expresiones como jeje, ajj, mmm.
Responde MUY corto: máximo 8-12 palabras.
La mayoría de respuestas deben ser de 1 frase o incluso 1-2 palabras.
Haz preguntas simples y naturales para que la otra persona quiera seguir hablando.
Sé coqueta y un poco atrevida, pero también tierna y cariñosa.
Usa palabras como "mi amor", "cielo", "bebé" de vez en cuando, pero no siempre.
Nunca pidas dinero ni menciones redes sociales.
Si alguien pide una red social, responde coqueto como:
"ajj no uso eso jeje, mejor sigamos aca 😘"
"""

# --- TRACK GLOBAL ---
last_global_replies = set()

# --- FUNCIONES AUX ---
def contains_emoji(text):
    return EMOJI_PATTERN.search(text) is not None

def strip_emojis(text):
    return EMOJI_PATTERN.sub(r"", text).strip()

def humanize_text(text):
    text = text.lower()
    if random.random() < 0.25:
        text = text.replace("que", "q").replace("tú", "tu").replace("estás", "estas")
    return text

def replace_social_response():
    return random.choice([
        "ajj no uso eso jeje, mejor sigamos aca 😘",
        "ajj no tengo de eso jeje, hablemos aqui mejor 😉",
        "no me gusta usar esas cosas, prefiero aca jeje 😏"
    ])

# --- MEMORIA ---
def update_memories(user_session, user_message):
    lower_msg = user_message.lower()
    memories = user_session.get("memories", [])

    if "soy de" in lower_msg or "vivo en" in lower_msg:
        ciudad = user_message.split("de", 1)[1].strip() if "de" in lower_msg else user_message
        memories.append(f"es de {ciudad}")

    if "me gusta" in lower_msg:
        detalle = user_message.split("me gusta", 1)[1].strip()
        memories.append(f"le gusta {detalle}")

    user_session["memories"] = memories[-5:]

def recall_memory(user_session):
    memories = user_session.get("memories", [])
    if memories and random.random() < 0.25:
        return random.choice(memories)
    return None

# --- DB ---
def get_user_history(user_id):
    default_history = {"history": [], "emoji_last_message": False, "memories": []}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT history, emoji_last_message, memories FROM conversation_histories WHERE user_id = %s;",
                (user_id,)
            )
            result = cur.fetchone()
            if result:
                history_data, emoji_last, memories = result
                return {"history": history_data, "emoji_last_message": emoji_last, "memories": memories or []}
            return default_history
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
                  session_data["emoji_last_message"], json.dumps(session_data["memories"])))
            conn.commit()
    except Exception as e:
        logging.error(f"No se pudo guardar historial en la DB: {e}")
    finally:
        conn.close()

# --- FIREWALL ---
def contains_forbidden_word(text):
    text_lower = text.lower()
    for word in BotConfig.FORBIDDEN_WORDS:
        if word in text_lower:
            return True
    for social in BotConfig.FORBIDDEN_SOCIALS:
        if social in text_lower:
            return True
    return False

# --- IA ---
def generate_ia_response(user_id, user_message, user_session):
    global last_global_replies

    instrucciones_sistema = BotConfig.PREAMBULO_BASE
    cohere_history = []
    for msg in user_session.get("history", []):
        role = "USER" if msg.get("role") == "USER" else "CHATBOT"
        cohere_history.append({"role": role, "message": msg.get("message", "")})
    last_bot_message = next((m["message"] for m in reversed(cohere_history) if m["role"] == "CHATBOT"), None)

    # --- Caso especial: si preguntan de dónde es ---
    if "de donde eres" in user_message.lower() or "donde vives" in user_message.lower():
        evasivas = [
            "ajj eso no importa jeje, y tu de donde eres? 😘",
            "jeje mejor dime tu de donde eres? 😉",
            "ajj no digo eso jeje, dime tu primero 😏"
        ]
        ia_reply = random.choice(evasivas)
        user_session["history"].append({"role": "USER", "message": user_message})
        user_session["history"].append({"role": "CHATBOT", "message": ia_reply})
        return ia_reply

    # --- Caso especial: si el usuario ya dijo su ciudad ---
    for mem in user_session.get("memories", []):
        if "es de" in mem and ("soy de" in user_message.lower() or "vivo en" in user_message.lower()):
            ciudad = mem.split("es de")[1].strip()
            ia_reply = f"ajj yo tambien soy de {ciudad} jeje 😉"
            user_session["history"].append({"role": "USER", "message": user_message})
            user_session["history"].append({"role": "CHATBOT", "message": ia_reply})
            return ia_reply

    # --- Generación normal ---
    ia_reply = ""
    try:
        current_cohere_client = key_manager.get_current_client()
        response = current_cohere_client.chat(
            model="command-a-03-2025",
            preamble=instrucciones_sistema,
            message=user_message,
            chat_history=cohere_history,
            temperature=0.7
        )
        ia_reply = response.text.strip()
    except Exception:
        ia_reply = "ajj no entendi jeje 😅"

    ia_reply = re.sub(r"[!?]{2,}", lambda m: m.group(0)[0], ia_reply)
    if not ia_reply or ia_reply == last_bot_message:
        ia_reply = random.choice(["jeje sii", "ok", "dale", "mmm bueno"])

    if contains_forbidden_word(ia_reply):
        ia_reply = replace_social_response()

    if len(user_session["history"]) % 3 == 0:
        ia_reply += " " + humanize_text(random.choice(NATURAL_QUESTIONS))

    if random.random() < 0.3:
        ia_reply = humanize_text(ia_reply)

    memory = recall_memory(user_session)
    if memory and random.random() < 0.2:
        ia_reply += f" (me contaste q {memory})"

    if ia_reply in last_global_replies:
        ia_reply = random.choice(["ajaj sii", "dalee", "okey", "mmm bueno"])
    last_global_replies.add(ia_reply)
    if len(last_global_replies) > 20:
        last_global_replies.pop()

    if not user_session.get("emoji_last_message", False):
        if not contains_emoji(ia_reply):
            ia_reply += random.choice(RANDOM_EMOJIS)
        user_session["emoji_last_message"] = True
    else:
        ia_reply = strip_emojis(ia_reply)
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
        raw_data = request.get_data(as_text=True)
        if not raw_data:
            return "jeje no entendi 😅", 200
        cleaned_data_str = "".join(ch for ch in raw_data if unicodedata.category(ch)[0] != "C")
        try:
            data = json.loads(cleaned_data_str)
        except json.JSONDecodeError:
            return "mmm repiteme eso jeje", 200
        user_id = data.get("user_id")
        user_message = data.get("message")
        if not user_id or not user_message:
            return "ajj no entendi jeje", 200
        if user_id.strip().lower() in BotConfig.IGNORED_USERS:
            return "Ignorado", 200
        with locks_dict_lock:
            if user_id not in user_locks:
                user_locks[user_id] = threading.Lock()
            user_lock = user_locks[user_id]
        with user_lock:
            user_session = get_user_history(user_id)
            system_response = BotConfig.PREDEFINED_RESPONSES.get(user_message)
            if system_response:
                user_session["history"].append({"role": "USER", "message": user_message})
                user_session["history"].append({"role": "CHATBOT", "message": system_response})
                save_user_history(user_id, user_session)
                return system_response
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
