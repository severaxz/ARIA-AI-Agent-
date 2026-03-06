import os
import json
import logging
import time
from collections import defaultdict
from openai import OpenAI, AuthenticationError, RateLimitError
from supabase import create_client
from telegram import Update, BusinessConnection
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes, TypeHandler
)
from prompt import SYSTEM_PROMPT, ANALYSIS_PROMPT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
OWNER_ID       = int(os.environ.get("OWNER_ID", "0"))

ALLOWED_USERS: set[int] = set(
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
)

MAX_MESSAGE_LENGTH = 2000
MAX_HISTORY        = 20
RATE_LIMIT_COUNT   = 3
RATE_LIMIT_WINDOW  = 60
VALID_ROLES        = {"user", "assistant", "tool"}

client   = OpenAI(api_key=OPENAI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

user_timestamps: dict[int, list] = defaultdict(list)

# ── ИНСТРУМЕНТЫ ДЛЯ GPT ───────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_chats",
            "description": "Показать список всех людей, которые писали в бизнес-чат. Используй когда нужно узнать кто писал или найти нужного человека.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat",
            "description": "Прочитать переписку с конкретным человеком. Используй когда нужно посмотреть что пишет человек или проверить диалог.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Имя человека (или часть имени)"
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_reply",
            "description": "Отправить сообщение конкретному человеку в бизнес-чат от имени владельца.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Имя человека которому отправить"
                    },
                    "message": {
                        "type": "string",
                        "description": "Текст сообщения"
                    }
                },
                "required": ["name", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_dialog",
            "description": "Глубоко проанализировать диалог с человеком: тональность, что хочет, сценарии ответов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Имя человека"
                    }
                },
                "required": ["name"]
            }
        }
    }
]

# ── HELPERS ───────────────────────────────────────────────────────────
def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    user_timestamps[user_id] = [t for t in user_timestamps[user_id] if now - t < RATE_LIMIT_WINDOW]
    if len(user_timestamps[user_id]) >= RATE_LIMIT_COUNT:
        return True
    user_timestamps[user_id].append(now)
    return False

def is_allowed(user_id: int) -> bool:
    return True if not ALLOWED_USERS else user_id in ALLOWED_USERS

def get_owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.bot_data.get("business_owner_id") or OWNER_ID

# ── SUPABASE — ИСТОРИЯ АРИИ ───────────────────────────────────────────
def get_history(user_id: int) -> list:
    try:
        res = supabase.table("messages")\
            .select("role, content")\
            .eq("user_id", user_id)\
            .order("created_at")\
            .limit(MAX_HISTORY)\
            .execute()
        return [r for r in res.data if r.get("role") in VALID_ROLES]
    except Exception:
        logger.exception("Ошибка получения истории")
        return []

def save_message(user_id: int, role: str, content: str):
    if role not in VALID_ROLES:
        return
    try:
        supabase.table("messages").insert({
            "user_id": user_id,
            "role": role,
            "content": str(content)[:MAX_MESSAGE_LENGTH]
        }).execute()
    except Exception:
        logger.exception("Ошибка сохранения сообщения")

# ── SUPABASE — БИЗНЕС ЧАТЫ ────────────────────────────────────────────
def save_business_message(chat_id: int, sender_name: str, content: str, is_owner: bool, connection_id: str):
    try:
        supabase.table("business_messages").insert({
            "chat_id": chat_id,
            "sender_name": sender_name,
            "content": content[:2000],
            "is_owner": is_owner,
            "connection_id": connection_id
        }).execute()
    except Exception:
        logger.exception("Ошибка сохранения бизнес-сообщения")

def find_chat_by_name(name: str) -> dict | None:
    """Найти чат по имени (нечёткий поиск)."""
    try:
        res = supabase.table("business_messages")\
            .select("chat_id, sender_name, connection_id")\
            .eq("is_owner", False)\
            .ilike("sender_name", f"%{name}%")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        return res.data[0] if res.data else None
    except Exception:
        logger.exception("Ошибка поиска чата")
        return None

def get_chat_messages(chat_id: int, limit: int = 30) -> list:
    try:
        res = supabase.table("business_messages")\
            .select("sender_name, content, is_owner, created_at")\
            .eq("chat_id", chat_id)\
            .order("created_at")\
            .limit(limit)\
            .execute()
        return res.data
    except Exception:
        logger.exception("Ошибка получения сообщений чата")
        return []

def get_all_chats() -> list:
    try:
        res = supabase.table("business_messages")\
            .select("chat_id, sender_name, content, created_at")\
            .eq("is_owner", False)\
            .order("created_at", desc=True)\
            .limit(200)\
            .execute()
        seen: dict = {}
        for row in res.data:
            cid = row["chat_id"]
            if cid not in seen:
                seen[cid] = row
        return list(seen.values())
    except Exception:
        logger.exception("Ошибка получения списка чатов")
        return []

# ── ВЫПОЛНЕНИЕ ИНСТРУМЕНТОВ ───────────────────────────────────────────
async def execute_tool(name: str, args: dict, context: ContextTypes.DEFAULT_TYPE) -> str:
    if name == "list_chats":
        chats = get_all_chats()
        if not chats:
            return "Бизнес-чатов пока нет."
        lines = [f"• {c['sender_name']}: {c['content'][:60]}..." for c in chats]
        return "Активные чаты:\n" + "\n".join(lines)

    elif name == "read_chat":
        chat = find_chat_by_name(args["name"])
        if not chat:
            return f"Чат с '{args['name']}' не найден."
        messages = get_chat_messages(chat["chat_id"])
        if not messages:
            return "Диалог пустой."
        def fmt(m):
            label = "[Я]" if m["is_owner"] else f"[{m['sender_name']}]"
            return f"{label}: {m['content']}"
        return "\n".join(fmt(m) for m in messages)

    elif name == "send_reply":
        chat = find_chat_by_name(args["name"])
        if not chat:
            return f"Чат с '{args['name']}' не найден."
        try:
            await context.bot.send_message(
                chat_id=chat["chat_id"],
                text=args["message"],
                business_connection_id=chat["connection_id"]
            )
            save_business_message(chat["chat_id"], "Я", args["message"], True, chat["connection_id"])
            return f"Сообщение отправлено {args['name']}: «{args['message']}»"
        except Exception as e:
            logger.exception("Ошибка отправки сообщения")
            return f"Не удалось отправить: {e}"

    elif name == "analyze_dialog":
        chat = find_chat_by_name(args["name"])
        if not chat:
            return f"Чат с '{args['name']}' не найден."
        messages = get_chat_messages(chat["chat_id"])
        if not messages:
            return "Диалог пустой."
        def fmt2(m):
            label = "[Я]" if m["is_owner"] else f"[{m['sender_name']}]"
            return f"{label}: {m['content']}"
        dialog_text = "\n".join(fmt2(m) for m in messages)
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": f"Проанализируй диалог:\n\n{dialog_text}"}
            ],
            max_tokens=1500,
            temperature=0.7
        )
        return response.choices[0].message.content

    return "Неизвестный инструмент."

# ── АГЕНТ ─────────────────────────────────────────────────────────────
async def run_agent(user_id: int, user_text: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    save_message(user_id, "user", user_text)
    history = get_history(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    # Агентный цикл — GPT может вызывать инструменты несколько раз
    for _ in range(5):
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=1500,
            temperature=0.85
        )
        msg = response.choices[0].message

        # GPT хочет вызвать инструмент
        if msg.tool_calls:
            messages.append(msg)
            for tool_call in msg.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)
                logger.info("Вызов инструмента: %s(%s)", tool_name, tool_args)
                result = await execute_tool(tool_name, tool_args, context)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result
                })
        else:
            # GPT дал финальный ответ
            reply = msg.content or ""
            save_message(user_id, "assistant", reply)
            return reply

    return "Не смог выполнить задачу."

# ── ХЕНДЛЕРЫ ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    name = update.effective_user.first_name or "бро"
    await update.message.reply_text(
        f"Привет, {name}. Я ARIA.\n\n"
        f"Просто говори что нужно:\n"
        f"— «Кто мне писал?»\n"
        f"— «Что пишет Максим?»\n"
        f"— «Ответь Алине что перезвоню в 6»\n"
        f"— «Разбери диалог с Иваном»"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    if is_rate_limited(user_id):
        await update.message.reply_text("Подожди немного.")
        return

    text = update.message.text or ""
    if len(text) > MAX_MESSAGE_LENGTH:
        await update.message.reply_text(f"Слишком длинно.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    try:
        reply = await run_agent(user_id, text, context)
        await update.message.reply_text(reply)
    except RateLimitError:
        await update.message.reply_text("Кончились кредиты OpenAI. Пополни баланс: platform.openai.com/settings/billing")
    except AuthenticationError:
        await update.message.reply_text("Неверный API ключ OpenAI.")
    except Exception:
        logger.exception("Ошибка агента user_id=%s", user_id)
        await update.message.reply_text("Что-то пошло не так.")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    try:
        supabase.table("messages").delete().eq("user_id", update.effective_user.id).execute()
        await update.message.reply_text("Память очищена.")
    except Exception:
        await update.message.reply_text("Не удалось очистить память.")

# ── БИЗНЕС РЕЖИМ ─────────────────────────────────────────────────────
async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc:
        context.bot_data["business_owner_id"] = bc.user.id
        logger.info("Бизнес-подключение: owner_id=%s, connection_id=%s", bc.user.id, bc.id)

async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message or not message.text:
        return

    chat_id = message.chat.id
    sender_name = message.from_user.first_name if message.from_user else "Неизвестный"
    connection_id = message.business_connection_id or ""
    owner_id = get_owner_id(context)
    is_owner = bool(message.from_user and owner_id and message.from_user.id == owner_id)

    # Обновляем connection_id в bot_data
    if connection_id:
        context.bot_data[f"conn_{chat_id}"] = connection_id

    save_business_message(chat_id, sender_name, message.text, is_owner, connection_id)
    logger.info("Бизнес-сообщение от %s (owner=%s)", sender_name, is_owner)

# ── ЗАПУСК ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))
    app.add_handler(TypeHandler(BusinessConnection, handle_business_connection))

    logger.info("ARIA запущена")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
