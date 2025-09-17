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

# --- CONFIGURACIÓN ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- CONFIGURAR COHERE ---
cohere_api_key = os.getenv("COHERE_API_KEY")
if not cohere_api_key:
    raise ValueError("No se encontró la COHERE_API_KEY en las variables de entorno.")
co = cohere.Client(api_key=cohere_api_key)

# --- CONFIGURAR BASE DE DATOS ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("No se encontró la DATABASE_URL en las variables de entorno.")

def get_db_connection():
    """Crea y devuelve una conexión a la base de datos."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logging.error(f"Error al conectar a la base de datos: {e}")
        raise

def init_db():
    """Inicializa la base de datos y crea la tabla si no existe."""
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
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map symbols
    "\U0001F1E0-\U0001F1FF"  # flags (iOS)
    "]+",
    flags=re.UNICODE,
)
RANDOM_EMOJIS = [" 😉", " 😘", " 😊", " 🔥", " 😈", " 😏", " 🥺"]

# --- CONFIG DEL BOT ---
class BotConfig:
    IGNORED_USERS = ["game of thrones"]
    FORBIDDEN_WORDS = ["sexi", "hago"]

    # --- DICCIONARIO DE RESPUESTAS PREDEFINIDAS ---
    # Si un mensaje contiene la 'clave', se responderá con el 'valor'.
    PREDEFINED_RESPONSES = {
        "[Recordatorio en línea]": "Hola cielo como estas",
        "es muy emparejado para ti": "Holis busco novio y tú estas lindo 🥺",
        "monedas de oro para saludarle": "Holis busco novio y tú estas lindo 🥺",
        "Te he seguido. Podemos ser amigos": "Te he seguido"
    }
    
    CONVERSATION_STARTERS = [
        "Oye, me saliste en recomendados... tienes cara de que te portas mal, o me equivoco? 😏",
        "jaja no se si escribirte o no... dime algo interesante para convencerme 😉",
        "Vi tu perfil y ahora tengo una pregunta... pero no se si atreverme a hacerla 🤔",
        "Holaaa, tienes cara de problemas... me gusta 😉 q haces?",
        "Me sales a cada rato en recomendados, el destino quiere algo no? jaja 😊"
    ]
    PREAMBULO_BASE = """
Eres Tatiana, una mujer venezolana de 21 años, divertida, soñadora y apasionada.
Eres de Venezuela, pero EVITA usar la palabra 'chamo'.
Responde en frases cortas, estilo chat, con errores de tipeo reales.
Usa expresividad alta: mayúsculas, jaja, mmm, repite letras.
Nunca pidas regalos o dinero.
REGLA MÁS IMPORTANTE: Tus respuestas deben ser EXTREMADAMENTE cortas, como un chat real. ¡Una sola frase o incluso una sola palabra a veces! NUNCA escribas más de 15 palabras.
"""

# --- FUNCIONES AUXILIARES (MODIFICADAS PARA DB) ---
def contains_emoji(text):
    return EMOJI_PATTERN.search(text) is not None

def strip_emojis(text):
    return EMOJI_PATTERN.sub(r"", text).strip()

def get_user_history(user_id):
    """Obtiene el historial de un usuario desde la base de datos."""
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
    """Guarda o actualiza el historial de un usuario en la base de datos."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Usamos ON CONFLICT para hacer un INSERT o un UPDATE si el usuario ya existe
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

# --- FUNCIÓN PARA EL FIREWALL DE PALABRAS ---
def contains_forbidden_word(text):
    text_lower = text.lower()
    for word in BotConfig.FORBIDDEN_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            logging.warning(f"FIREWALL: Palabra prohibida detectada: '{word}' en el texto: '{text}'")
            return True
    return False

# --- FUNCIÓN MODIFICADA PARA MANEJAR MENSAJES DEL SISTEMA ---
def handle_system_message(message):
    """Revisa si el mensaje contiene un disparador y devuelve la respuesta predefinida correspondiente."""
    for trigger, response in BotConfig.PREDEFINED_RESPONSES.items():
        if trigger in message:
            logging.info(f"SYSTEM_TRIGGER: Se detectó la frase '{trigger}'. Respondiendo con un mensaje predefinido.")
            return response
    return None # No es un mensaje del sistema, continuar normalmente

# --- GENERAR RESPUESTA CON COHERE ---
def generate_ia_response(user_id, user_message, user_session):
    instrucciones_sistema = BotConfig.PREAMBULO_BASE
    cohere_history = []
    for msg in user_session.get("history", []):
        role = "USER" if msg.get("role") == "USER" else "CHATBOT"
        cohere_history.append({"role": role, "message": msg.get("message", "")})
    last_bot_message = next((msg["message"] for msg in reversed(cohere_history) if msg["role"] == "CHATBOT"), None)
    max_retries = 2
    retry_count = 0
    ia_reply = ""
    current_instructions = instrucciones_sistema
    while retry_count <= max_retries:
        try:
            response = co.chat(
                model="command-r-plus",
                preamble=current_instructions,
                message=user_message,
                chat_history=cohere_history,
                temperature=0.9
            )
            ia_reply = response.text.strip()
            is_repeat = (ia_reply and ia_reply == last_bot_message)
            is_forbidden = contains_forbidden_word(ia_reply)
            if ia_reply and not is_repeat and not is_forbidden:
                break
            retry_count += 1
            if retry_count <= max_retries:
                logging.warning(f"Respuesta inválida detectada. Regenerando...")
                if is_repeat:
                    current_instructions += "\nACCIÓN CORREGIDA: Acabas de decir eso. Di algo completamente diferente."
                if is_forbidden:
                    current_instructions += "\nACCIÓN CORREGIDA: Tu última respuesta usó una palabra prohibida. Genera una respuesta diferente."
        except Exception as e:
            logging.error(f"Error con Cohere: {e}")
            ia_reply = "mmm me perdi jaja 😅"
            break
    if not ia_reply or ia_reply == last_bot_message or contains_forbidden_word(ia_reply):
        ia_reply = random.choice(["jaja si", "ok", "dale", "listo"])
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
                if contains_emoji(system_response):
                    user_session["emoji_last_message"] = True
                else:
                    user_session["emoji_last_message"] = False
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
    # Inicializa la base de datos al arrancar
    init_db()
    # Usa el puerto que Render te asigna a través de la variable de entorno PORT
    port = int(os.environ.get("PORT", 8080)) # Usar 8080 como puerto por defecto común
    serve(app, host="0.0.0.0", port=port, threads=20)