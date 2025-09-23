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

# --- INICIALIZAR COHERE CON ROTACIÓN ---
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
                    emoji_last_message BOOLEAN DEFAULT FALSE
                );
            """)
            conn.commit()
        logging.info("Tabla 'conversation_histories' verificada/creada exitosamente.")
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

# --- PREGUNTAS NATURALES ---
NATURAL_QUESTIONS = [
    "y tu que haces?",
    "mmm y cuentame mas",
    "ajaja en serio?",
    "tuuu que opinas?",
    "q estas haciendo ahorita?",
    "jeje y que mas?"
]

# --- CONFIG DEL BOT ---
class BotConfig:
    IGNORED_USERS = ["game of thrones"]
    FORBIDDEN_WORDS = ["sexi", "hago"]

    PREDEFINED_RESPONSES = {
        "[Recordatorio en línea]": "Hola cielo como estas",
        "es muy emparejado para ti": "Holis busco novio y tú estas lindo 🥺",
        "monedas de oro para saludarle": "Holis busco novio y tú estas lindo 🥺",
        "Te he seguido. Podemos ser amigos": "Te he seguido"
    }

    PREAMBULO_BASE = """
Eres Tatiana, una mujer colombiana de 21 años.
Tu estilo es natural, humano y cercano, como si chatearas en WhatsApp.
Hablas de forma espontánea y con calidez, evitando sonar como inteligencia artificial.
No abuses de los signos de exclamación o interrogación, úsalos con naturalidad.
Haz preguntas cortas y simples para invitar a seguir la conversación.
A veces usas emoticones o expresiones como 'jeje', 'mmm', 'aja' para sonar más auténtica.
Responde en frases muy cortas, no más de 15 palabras, como en un chat real.
Nunca pidas dinero ni regalos.
"""

# --- FUNCIONES AUXILIARES ---
def contains_emoji(text):
    return EMOJI_PATTERN.search(text) is not None

def strip_emojis(text):
    return EMOJI_PATTERN.sub(r"", text).strip()

def humanize_text(text):
    """Hace que el texto suene más humano con errores leves random."""
    text = text.lower()
    # 30% chance de cambiar palabras comunes
    if random.random() < 0.3:
        text = text.replace("que", "q")
        text = text.replace("tú", "tu")
        text = text.replace("estás", "estas")
    # 20% chance de repetir una letra
    if random.random() < 0.2 and len(text) > 4:
        pos = random.randint(1, len(text) - 2)
        text = text[:pos] + text[pos] * 2 + text[pos+1:]
    return text

def get_user_history(user_id):
    default_history = {"history": [], "emoji_last_message": False}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT history, emoji_last_message FROM conversation_histories WHERE user_id = %s;",
                (user_id,)
            )
            result = cur.fetchone()
            if result:
                history_data, emoji_last = result
                return {"history": history_data, "emoji_last_message": emoji_last}
            return default_history
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
                DO UPDATE SET history = EXCLUDED.history, emoji_last_message = EXCLUDED.emoji_last_message;
            """, (user_id, json.dumps(session_data["history"]), session_data["emoji_last_message"]))
            conn.commit()
    except Exception as e:
        logging.error(f"No se pudo guardar historial en la DB: {e}")
    finally:
        conn.close()

def contains_forbidden_word(text):
    text_lower = text.lower()
    for word in BotConfig.FORBIDDEN_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            logging.warning(f"FIREWALL: Palabra prohibida detectada: '{word}' en el texto: '{text}'")
            return True
    return False

def handle_system_message(message):
    for trigger, response in BotConfig.PREDEFINED_RESPONSES.items():
        if trigger in message:
            logging.info(f"SYSTEM_TRIGGER: Se detectó la frase '{trigger}'. Respondiendo con un mensaje predefinido.")
            return response
    return None

# --- GENERAR RESPUESTA CON COHERE ---
def generate_ia_response(user_id, user_message, user_session):
    instrucciones_sistema = BotConfig.PREAMBULO_BASE
    cohere_history = []
    for msg in user_session.get("history", []):
        role = "USER" if msg.get("role") == "USER" else "CHATBOT"
        cohere_history.append({"role": role, "message": msg.get("message", "")})
    last_bot_message = next((msg["message"] for msg in reversed(cohere_history) if msg["role"] == "CHATBOT"), None)

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

    except NotFoundError as e:
        logging.error(f"Modelo no encontrado o removido: {e}.")
        ia_reply = "el modelo ya no está disponible 😅"

    except Exception as e:
        logging.error(f"Error inesperado con Cohere: {e}. Rotando a la siguiente key...")
        try:
            current_cohere_client = key_manager.rotate_to_next_key()
            response = current_cohere_client.chat(
                model="command-a-03-2025",
                preamble=instrucciones_sistema,
                message=user_message,
                chat_history=cohere_history,
                temperature=0.7
            )
            ia_reply = response.text.strip()
        except Exception as e2:
            logging.error(f"Error tras rotar key: {e2}")
            ia_reply = "mmm tuve un problemita, intenta de nuevo 😅"

    # --- Ajustes estilo humano ---
    ia_reply = re.sub(r"[!?]{2,}", lambda m: m.group(0)[0], ia_reply)  # no abusar de ! ?
    is_repeat = (ia_reply and ia_reply == last_bot_message)
    is_forbidden = contains_forbidden_word(ia_reply)
    if not ia_reply or is_repeat or is_forbidden:
        ia_reply = random.choice(["jeje sii", "ok", "dale", "mmm bueno"])

    # --- Cada 3 respuestas, mete una pregunta random natural ---
    chatbot_msgs = [m for m in user_session["history"] if m["role"] == "CHATBOT"]
    if len(chatbot_msgs) > 0 and len(chatbot_msgs) % 3 == 0:
        question = random.choice(NATURAL_QUESTIONS)
        question = humanize_text(question)  # 👉 errores humanos
        ia_reply += " " + question

    # --- Humanizar respuestas normales con chance ---
    if random.random() < 0.4:  # 40% chance
        ia_reply = humanize_text(ia_reply)

    # --- Emojis ---
    should_have_emoji = not user_session.get("emoji_last_message", False)
    if should_have_emoji:
        if not contains_emoji(ia_reply):
            ia_reply += random.choice(RANDOM_EMOJIS)
        user_session["emoji_last_message"] = True
    else:
        ia_reply = strip_emojis(ia_reply)
        user_session["emoji_last_message"] = False

    user_session["history"].append({"role": "USER", "message": user_message})
    user_session["history"].append({"role": "CHATBOT", "message": ia_reply})
    return ia_reply

# --- FLASK API ---
app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def handle_chat():
    try:
        raw_data = request.get_data(as_text=True)
        if not raw_data:
            return "Error: No se recibieron datos.", 400
        cleaned_data_str = "".join(ch for ch in raw_data if unicodedata.category(ch)[0] != "C")
        try:
            data = json.loads(cleaned_data_str)
        except json.JSONDecodeError:
            logging.error(f"Error de decodificación JSON. Datos crudos: '{raw_data}', Limpios: '{cleaned_data_str}'")
            return "Error: Formato JSON inválido.", 400
        user_id = data.get("user_id")
        user_message = data.get("message")
        if not user_id or not user_message:
            return "Error: faltan parámetros", 400
        if user_id.strip().lower() in BotConfig.IGNORED_USERS:
            logging.info(f"Mensaje de '{user_id}' ignorado.")
            return "Ignorado", 200
        with locks_dict_lock:
            if user_id not in user_locks:
                user_locks[user_id] = threading.Lock()
            user_lock = user_locks[user_id]

        with user_lock:
            user_session = get_user_history(user_id)

            system_response = handle_system_message(user_message)
            if system_response:
                user_session["history"].append({"role": "USER", "message": user_message})
                user_session["history"].append({"role": "CHATBOT", "message": system_response})
                user_session["emoji_last_message"] = contains_emoji(system_response)
                save_user_history(user_id, user_session)
                return system_response

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
