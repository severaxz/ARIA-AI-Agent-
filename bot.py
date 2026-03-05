import os
import logging
import time
from collections import defaultdict
from openai import OpenAI
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from prompt import SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]

ALLOWED_USERS: set[int] = set(
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
)

# ── КОНСТАНТЫ БЕЗОПАСНОСТИ ───────────────────────────────────────────
MAX_MESSAGE_LENGTH = 1000
MAX_HISTORY        = 20
RATE_LIMIT_COUNT   = 10
RATE_LIMIT_WINDOW  = 60
VALID_ROLES        = {"user", "assistant"}

# ── КЛИЕНТЫ ──────────────────────────────────────────────────────────
client   = OpenAI(api_key=OPENAI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── RATE LIMITER ──────────────────────────────────────────────────────
user_timestamps: dict[int, list] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    user_timestamps[user_id] = [t for t in user_timestamps[user_id] if now - t < RATE_LIMIT_WINDOW]
    if len(user_timestamps[user_id]) >= RATE_LIMIT_COUNT:
        return True
    user_timestamps[user_id].append(now)
    return False

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

# ── SUPABASE ──────────────────────────────────────────────────────────
def get_history(user_id: int) -> list:
    try:
        res = supabase.table("messages")\
            .select("role, content")\
            .eq("user_id", user_id)\
            .order("created_at")\
            .limit(MAX_HISTORY)\
            .execute()
        return [
            {"role": r["role"], "content": r["content"]}
            for r in res.data
            if r.get("role") in VALID_ROLES
        ]
    except Exception:
        logger.exception("Ошибка получения истории user_id=%s", user_id)
        return []

def save_message(user_id: int, role: str, content: str):
    if role not in VALID_ROLES:
        return
    try:
        supabase.table("messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content[:MAX_MESSAGE_LENGTH]
        }).execute()
    except Exception:
        logger.exception("Ошибка сохранения user_id=%s", user_id)

# ── ХЕНДЛЕРЫ ──────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    name = update.effective_user.first_name or "оператор"
    await update.message.reply_text(
        f"Привет, {name}. Я ARIA.\nГотова работать — спрашивай."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    await update.message.reply_text(
        "Команды:\n"
        "/start — запустить\n"
        "/clear — очистить память\n"
        "/help — это сообщение\n\n"
        "Просто пиши — отвечу."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        logger.warning("Неавторизованный доступ: %s", user_id)
        return

    if is_rate_limited(user_id):
        await update.message.reply_text("Полегче. Подожди минуту.")
        return

    text = update.message.text or ""
    if len(text) > MAX_MESSAGE_LENGTH:
        await update.message.reply_text(f"Слишком длинно. Максимум {MAX_MESSAGE_LENGTH} символов.")
        return

    save_message(user_id, "user", text)
    history = get_history(user_id)

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
            max_tokens=1000,
            temperature=0.85
        )
        reply = response.choices[0].message.content
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)

    except Exception:
        logger.exception("Ошибка OpenAI user_id=%s", user_id)
        await update.message.reply_text("Что-то пошло не так. Попробуй ещё раз.")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    try:
        supabase.table("messages").delete().eq("user_id", user_id).execute()
        await update.message.reply_text("Память очищена. Начинаем с чистого листа.")
    except Exception:
        logger.exception("Ошибка очистки user_id=%s", user_id)
        await update.message.reply_text("Не удалось очистить память.")

# ── ЗАПУСК ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("ARIA запущена")
    app.run_polling()
