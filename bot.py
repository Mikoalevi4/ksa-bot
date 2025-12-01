import os
import asyncio
import urllib.parse
from datetime import date, timedelta
from dateutil.parser import parse as parse_date

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ---------- Config ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TIMETABLE_BASE = "http://rozklad.ksaeu.kherson.ua/cgi-bin/timetable_export.cgi"

if not TELEGRAM_TOKEN or not DATABASE_URL:
    raise RuntimeError("Please set TELEGRAM_TOKEN and DATABASE_URL environment variables.")

# ---------- DB helpers ----------
def get_conn():
    # psycopg2 will parse DATABASE_URL like postgresql://user:pass@host:port/db
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def ensure_telegram_users_table():
    sql = """
    CREATE TABLE IF NOT EXISTS telegram_users (
      telegram_id bigint PRIMARY KEY,
      user_id integer REFERENCES public.users(id) ON DELETE CASCADE,
      registered_at timestamp without time zone DEFAULT now()
    );
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()

def find_user_by_phone(phone):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM public.users WHERE phone = %s LIMIT 1;", (phone,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def bind_telegram_to_user(telegram_id: int, user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO telegram_users (telegram_id, user_id)
        VALUES (%s, %s)
        ON CONFLICT (telegram_id) DO UPDATE SET user_id = EXCLUDED.user_id;
    """, (telegram_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_user_by_telegram(telegram_id: int):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT u.* FROM public.users u
        JOIN telegram_users t ON t.user_id = u.id
        WHERE t.telegram_id = %s LIMIT 1;
    """, (telegram_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def get_group_code_by_id(group_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT code FROM public.groups WHERE id = %s LIMIT 1;", (group_id,))
    res = cur.fetchone()
    cur.close()
    conn.close()
    return res[0] if res else None

def get_teacher_api_id_by_id(teacher_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT api_id FROM public.teachers WHERE id = %s LIMIT 1;", (teacher_id,))
    res = cur.fetchone()
    cur.close()
    conn.close()
    return res[0] if res else None

# ---------- Timetable fetcher ----------
def build_timetable_url_by_group(group_code: str, begin_date: date, end_date: date, resp_format="json"):
    params = {
        "req_mode": "group",
        "req_type": "rozklad",
        "req_format": resp_format,
        "coding_mode": "UTF8",
        "OBJ_name": group_code,
        "begin_date": begin_date.isoformat(),
        "end_date": end_date.isoformat()
    }
    return TIMETABLE_BASE + "?" + urllib.parse.urlencode(params, safe='')

def build_timetable_url_by_teacher(teacher_api_id: int, begin_date: date, end_date: date, resp_format="json"):
    params = {
        "req_mode": "teacher",
        "req_type": "rozklad",
        "req_format": resp_format,
        "coding_mode": "UTF8",
        "OBJ_ID": teacher_api_id,
        "begin_date": begin_date.isoformat(),
        "end_date": end_date.isoformat()
    }
    return TIMETABLE_BASE + "?" + urllib.parse.urlencode(params, safe='')

def fetch_timetable(url: str, timeout=15):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    # API supports json; we requested json
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}

def format_timetable_json(json_data):
    # Basic formatter: if API returns error code — show it; otherwise try to present entries.
    if isinstance(json_data, dict) and "code" in json_data:
        code = json_data.get("code")
        err = json_data.get("error_message") or json_data.get("error") or ""
        return f"API error: code={code}. {err}"
    # The exact structure may vary; present a compact readable form.
    # If it's a list/dict with days -> lessons, we try to iterate.
    out_lines = []
    if isinstance(json_data, dict) and "days" in json_data:
        for day in json_data["days"]:
            out_lines.append(f"{day.get('date', '')} ({day.get('weekday', '')}):")
            lessons = day.get("lessons", [])
            if not lessons:
                out_lines.append("  — Пустий день")
            else:
                for L in lessons:
                    time = L.get("time", "")
                    subj = L.get("subject", L.get("name", ""))
                    room = L.get("room", "")
                    teacher = L.get("teacher", "")
                    out_lines.append(f"  {time} — {subj} ({teacher}) [{room}]")
    elif isinstance(json_data, dict) and "days_list" in json_data:
        # some variants
        for day in json_data["days_list"]:
            out_lines.append(str(day))
    elif isinstance(json_data, dict) and "raw" in json_data:
        return json_data["raw"][:4000]  # limit
    else:
        # fallback: pretty-print limited
        import json
        s = json.dumps(json_data, ensure_ascii=False, indent=2)
        return s[:4000]
    return "\n".join(out_lines)[:4000]  # limit to message size

# ---------- Command handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привіт! Я бот розкладу.\n\n"
        "Команди:\n"
        "/group <код групи> [begin YYYY-MM-DD] [end YYYY-MM-DD] — отримати розклад групи\n"
        "/teacher <api_id> [begin YYYY-MM-DD] [end YYYY-MM-DD] — отримати розклад викладача\n"
        "/register <phone> — зареєструвати свій аккаунт (пошук у users.phone) для команди /me\n"
        "/me [begin YYYY-MM-DD] [end YYYY-MM-DD] — отримати твій розклад (якщо зареєстровано у DB)\n"
        "/help — показати це повідомлення\n\n"
        "Приклад: /group 202-1-Д\n"
    )
    await update.message.reply_text(text)

def parse_optional_dates(args):
    # args: list of extra args; try to find date-looking strings
    today = date.today()
    default_begin = today
    default_end = today + timedelta(days=6)
    begin = default_begin
    end = default_end
    for a in args:
        try:
            d = parse_date(a).date()
            if begin == default_begin:
                begin = d
            else:
                end = d
        except Exception:
            continue
    return begin, end

async def cmd_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Використання: /group <код_групи> [begin YYYY-MM-DD] [end YYYY-MM-DD]")
        return
    group_code = args[0]
    begin, end = parse_optional_dates(args[1:])
    url = build_timetable_url_by_group(group_code, begin, end)
    await update.message.reply_text(f"Запитую розклад для групи {group_code} з {begin} по {end}...")
    try:
        data = await asyncio.to_thread(fetch_timetable, url)
        formatted = format_timetable_json(data)
        await update.message.reply_text(formatted)
    except requests.HTTPError as e:
        await update.message.reply_text(f"HTTP error при зверненні до розкладу: {e}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")

async def cmd_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Використання: /teacher <api_id> [begin YYYY-MM-DD] [end YYYY-MM-DD]")
        return
    try:
        teacher_api_id = int(args[0])
    except ValueError:
        await update.message.reply_text("api_id має бути числом.")
        return
    begin, end = parse_optional_dates(args[1:])
    url = build_timetable_url_by_teacher(teacher_api_id, begin, end)
    await update.message.reply_text(f"Запитую розклад для викладача {teacher_api_id} з {begin} по {end}...")
    try:
        data = await asyncio.to_thread(fetch_timetable, url)
        formatted = format_timetable_json(data)
        await update.message.reply_text(formatted)
    except requests.HTTPError as e:
        await update.message.reply_text(f"HTTP error при зверненні до розкладу: {e}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Використання: /register <phone>. Наприклад: /register +380501234567")
        return
    phone = args[0]
    user = await asyncio.to_thread(find_user_by_phone, phone)
    if not user:
        await update.message.reply_text("Користувача з таким телефоном не знайдено в БД.")
        return
    telegram_id = update.effective_user.id
    await asyncio.to_thread(bind_telegram_to_user, telegram_id, user["id"])
    await update.message.reply_text("Зв'язок встановлено. Тепер використай /me, щоб отримувати свій розклад.")

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = await asyncio.to_thread(get_user_by_telegram, telegram_id)
    if not user:
        await update.message.reply_text("Тебе не знайдено. Зареєструйся: /register <phone>")
        return
    # Determine if group or teacher
    begin, end = parse_optional_dates(context.args)
    # student (group_id)
    if user.get("group_id"):
        group_code = await asyncio.to_thread(get_group_code_by_id, user["group_id"])
        if not group_code:
            await update.message.reply_text("У твоєму профілі є group_id, але не знайдено відповідної групи.")
            return
        url = build_timetable_url_by_group(group_code, begin, end)
        await update.message.reply_text(f"Запитую розклад для твоєї групи {group_code} з {begin} по {end}...")
        try:
            data = await asyncio.to_thread(fetch_timetable, url)
            formatted = format_timetable_json(data)
            await update.message.reply_text(formatted)
        except Exception as e:
            await update.message.reply_text(f"Помилка: {e}")
        return
    # teacher
    if user.get("teacher_id"):
        teacher_api_id = await asyncio.to_thread(get_teacher_api_id_by_id, user["teacher_id"])
        if not teacher_api_id:
            await update.message.reply_text("У твоєму профілі є teacher_id, але не знайдено відповідного викладача.")
            return
        url = build_timetable_url_by_teacher(teacher_api_id, begin, end)
        await update.message.reply_text(f"Запитую розклад для тебе (teacher id={teacher_api_id}) з {begin} по {end}...")
        try:
            data = await asyncio.to_thread(fetch_timetable, url)
            formatted = format_timetable_json(data)
            await update.message.reply_text(formatted)
        except Exception as e:
            await update.message.reply_text(f"Помилка: {e}")
        return
    await update.message.reply_text("У твоєму профілі не вказано ні group_id, ні teacher_id.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ---------- Main ----------
def main():
    ensure_telegram_users_table()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("group", cmd_group))
    app.add_handler(CommandHandler("teacher", cmd_teacher))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("me", cmd_me))

    print("Bot started (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
