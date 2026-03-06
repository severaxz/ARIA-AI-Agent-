import os
import logging
import time
from collections import defaultdict
from openai import OpenAI
from supabase import create_client
from telegram import Update, BusinessConnection
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, TypeHandler
from prompt import SYSTEM_PROMPT, ANALYSIS_PROMPT

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

# ── SUPABASE — ЛИЧНЫЕ ДИАЛОГИ ─────────────────────────────────────────
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

# ── SUPABASE — БИЗНЕС ДИАЛОГИ ─────────────────────────────────────────
def save_business_message(chat_id: int, sender_name: str, content: str, is_owner: bool):
    try:
        supabase.table("business_messages").insert({
            "chat_id": chat_id,
            "sender_name": sender_name,
            "content": content[:2000],
            "is_owner": is_owner
        }).execute()
    except Exception:
        logger.exception("Ошибка сохранения бизнес-сообщения chat_id=%s", chat_id)

def get_business_dialog(chat_id: int, limit: int = 50) -> list:
    try:
        res = supabase.table("business_messages")\
            .select("sender_name, content, is_owner, created_at")\
            .eq("chat_id", chat_id)\
            .order("created_at")\
            .limit(limit)\
            .execute()
        return res.data
    except Exception:
        logger.exception("Ошибка получения бизнес-диалога chat_id=%s", chat_id)
        return []

def get_recent_business_chats(limit: int = 10) -> list:
    try:
        res = supabase.table("business_messages")\
            .select("chat_id, sender_name, created_at")\
            .eq("is_owner", False)\
            .order("created_at", desc=True)\
            .limit(100)\
            .execute()
        seen: dict = {}
        for row in res.data:
            cid = row["chat_id"]
            if cid not in seen:
                seen[cid] = row
            if len(seen) >= limit:
                break
        return list(seen.values())
    except Exception:
        logger.exception("Ошибка получения списка чатов")
        return []

# ── ХЕНДЛЕРЫ — ЛИЧНЫЕ СООБЩЕНИЯ ───────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    name = update.effective_user.first_name or "оператор"
    await update.message.reply_text(
        f"Привет, {name}. Я ARIA.\n"
        f"Готова работать — спрашивай.\n\n"
        f"/chats — список активных чатов\n"
        f"/analyze <chat_id> — разобрать диалог"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    await update.message.reply_text(
        "Команды:\n"
        "/start — запустить\n"
        "/clear — очистить память\n"
        "/chats — список бизнес-чатов\n"
        "/analyze <chat_id> — анализ диалога\n"
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
        await update.message.reply_text("Что-то пошло не так. П��пробуй ещё раз.")

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

# ── ХЕНДЛЕРЫ — БИЗНЕС СООБЩЕНИЯ ───────────────────────────────────────
async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc:
        context.bot_data["business_owner_id"] = bc.user.id
        logger.info("Бизнес-подключение: owner_id=%s", bc.user.id)

async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message or not message.text:
        return

    chat_id = message.chat.id
    sender_name = message.from_user.first_name if message.from_user else "Неизвестный"
    owner_id = context.bot_data.get("business_owner_id")
    is_owner = bool(message.from_user and owner_id and message.from_user.id == owner_id)

    logger.info("Бизнес-сообщение chat_id=%s от %s (owner=%s)", chat_id, sender_name, is_owner)
    save_business_message(chat_id, sender_name, message.text, is_owner)

# ── КОМАНДЫ АНАЛИЗА ───────────────────────────────────────────────────
async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    chats = get_recent_business_chats()
    if not chats:
        await update.message.reply_text("Бизнес-диалогов пока нет.")
        return

    lines = ["Активные чаты:\n"]
    for chat in chats:
        lines.append(f"• {chat['sender_name']} — `{chat['chat_id']}`")

    await update.message.reply_text(
        "\n".join(lines) + "\n\nИспользуй /analyze <chat\\_id>",
        parse_mode="Markdown"
    )

async def analyze_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    if not context.args:
        await update.message.reply_text("Укажи chat_id: /analyze <chat_id>\nСписок: /chats")
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id должен быть числом.")
        return

    messages = get_business_dialog(chat_id)
    if not messages:
        await update.message.reply_text(f"Диалог {chat_id} не найден или пустой.")
        return

    dialog_text = "\n".join([
        f"{'[Я]' if m['is_owner'] else '[Клиент]'} {m['sender_name']}: {m['content']}"
        for m in messages
    ])

    await update.message.reply_text("Анализирую...")

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": f"Проанализируй диалог:\n\n{dialog_text}"}
            ],
            max_tokens=1500,
            temperature=0.7
        )
        await update.message.reply_text(response.choices[0].message.content)

    except Exception:
        logger.exception("Ошибка анализа chat_id=%s", chat_id)
        await update.message.reply_text("Не удалось проанализировать диалог.")

# ── ЗАПУСК ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("chats", list_chats))
    app.add_handler(CommandHandler("analyze", analyze_chat))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))
    app.add_handler(TypeHandler(BusinessConnection, handle_business_connection))

    logger.info("ARIA запущена")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
