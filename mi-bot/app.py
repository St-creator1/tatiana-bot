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
                    emoji_last_message BOOLEAN DEFAULT FALSE
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
RANDOM_EMOJIS = [" 😉", " 😘", " 😊", " 🔥", " 😈", " 😏", " 🥺"]

# --- CONFIG BOT ---
class BotConfig:
    IGNORED_USERS = ["game of thrones"]
    FORBIDDEN_WORDS = ["sexi", "hago", "facebook", "instagram", "whatsapp", "tiktok"]

    PREAMBULO_BASE = """
Eres Tatiana, una mujer colombiana de 21 años.
Tu meta es buscar novio y mantener la conversación activa.
Responde muy corto (máx 8–12 palabras), como un chat real.
Evita signos excesivos (!, ?) y habla natural.
Usa expresividad: jeje, ajj, mmm, repite letras, mayúsculas ocasionales.
Sé coqueta, juguetona y un poco atrevida, pero también tierna.
Nunca menciones redes sociales ni pidas dinero.
"""

# --- FUNCIONES AUXILIARES ---
def contains_emoji(text): 
    return EMOJI_PATTERN.search(text) is not None

def strip_emojis(text): 
    return EMOJI_PATTERN.sub(r"", text).strip()

def get_user_history(user_id):
    default = {"history": [], "emoji_last_message": False}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT history, emoji_last_message FROM conversation_histories WHERE user_id = %s;", (user_id,))
            r = cur.fetchone()
            if r:
                history, emoji_last = r
                return {"history": history, "emoji_last_message": emoji_last}
            return default
    finally:
        conn.close()

def save_user_history(user_id, session_data):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO conversation_histories (user_id, history, emoji_last_message)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET history = EXCLUDED.history,
                              emoji_last_message = EXCLUDED.emoji_last_message;
            """, (user_id, json.dumps(session_data["history"]), session_data["emoji_last_message"]))
            conn.commit()
    finally:
        conn.close()

def contains_forbidden_word(text):
    return any(word in text.lower() for word in BotConfig.FORBIDDEN_WORDS)

# --- IA ---
def generate_ia_response(user_id, user_message, user_session):
    instrucciones_sistema = BotConfig.PREAMBULO_BASE
    cohere_history = []
    for msg in user_session.get("history", []):
        role = "USER" if msg.get("role") == "USER" else "CHATBOT"
        cohere_history.append({"role": role, "message": msg.get("message", "")})

    last_bot_message = next((m["message"] for m in reversed(cohere_history) if m["role"] == "CHATBOT"), None)

    ia_reply = ""
    try:
        client = key_manager.get_current_client()
        response = client.chat(
            model="command-a-03-2025",   # ✅ modelo vigente
            preamble=instrucciones_sistema,
            message=user_message,
            chat_history=cohere_history,
            temperature=0.9
        )
        ia_reply = response.text.strip()
    except NotFoundError as e:
        logging.error(f"Modelo no encontrado: {e}")
        ia_reply = "ese modelo ya no está jeje 😅"
    except Exception as e:
        logging.error(f"Error en Cohere: {e}")
        client = key_manager.rotate_to_next_key()
        try:
            response = client.chat(
                model="command-a-03-2025",
                preamble=instrucciones_sistema,
                message=user_message,
                chat_history=cohere_history,
                temperature=0.9
            )
            ia_reply = response.text.strip()
        except Exception as e2:
            logging.error(f"Error tras rotar: {e2}")
            ia_reply = "mmm fallo algo jeje 😅"

    # --- FILTROS ---
    if ia_reply == last_bot_message:
        ia_reply = "ajj dime otra cosita jeje"
    if contains_forbidden_word(ia_reply):
        ia_reply = "ajj mejor cambiemos de tema jeje 😉"
    if len(ia_reply.split()) > 12:
        ia_reply = " ".join(ia_reply.split()[:12]) + " jeje"

    # --- EMOJIS ALTERNADOS ---
    if not user_session.get("emoji_last_message", False):
        if not contains_emoji(ia_reply):
            ia_reply += random.choice(RANDOM_EMOJIS)
        user_session["emoji_last_message"] = True
    else:
        ia_reply = strip_emojis(ia_reply)
        user_session["emoji_last_message"] = False

    user_session["history"].append({"role": "USER", "message": user_message})
    user_session["history"].append({"role": "CHATBOT", "message": ia_reply})
    return ia_reply

# --- API ---
app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def handle_chat():
    try:
        raw = request.get_data(as_text=True)
        data = json.loads(raw)
        user_id, user_message = data.get("user_id"), data.get("message")
        if not user_id or not user_message:
            return "Error: faltan parámetros", 400
        if user_id.strip().lower() in BotConfig.IGNORED_USERS:
            return "Ignorado", 200

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
        return "Error en el servidor", 500

# --- INICIO ---
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    serve(app, host="0.0.0.0", port=port, threads=20)
