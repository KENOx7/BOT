# bot.py
import logging
import sqlite3
import os
import re
from datetime import datetime
from dotenv import load_dotenv

import openpyxl  # pip install openpyxl

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ================= Konfiqurasiya =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CODE = os.getenv("ADMIN_CODE", "supersecret123")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN tapılmadı. .env faylını yoxlayın.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_PATH = "database.db"
SCHEDULE_XLSX = "schedule.xlsx"

# Conversation states
ASK_PERSONAL_NUMBER = 1
SET_NEW_CODE = 2
ASK_CODE = 3
CHANGE_CODE = 4

# ================= DB köməkçiləri =================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_student_by_personal(personal_number):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE personal_number = ?", (personal_number,))
    row = cur.fetchone()
    conn.close()
    return row

def get_student_by_tg_id(tg_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_student_tg_id(student_id, tg_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE students SET tg_id = ? WHERE id = ?", (tg_id, student_id))
    conn.commit()
    conn.close()

def create_session(tg_id, student_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO sessions (tg_id, student_id) VALUES (?, ?)", (tg_id, student_id))
    conn.commit()
    conn.close()

# ================= Schedule parsing və saxlanma (diagnostika daxil) =================
SCHEDULE = []  # hər element: {"group","day","time","subject","day_norm"}

DAY_MAP = {
    "monday": "Monday", "mon": "Monday",
    "tuesday": "Tuesday", "tue": "Tuesday",
    "wednesday": "Wednesday", "wed": "Wednesday",
    "thursday": "Thursday", "thu": "Thursday",
    "friday": "Friday", "fri": "Friday",
    "saturday": "Saturday", "sat": "Saturday",
    "sunday": "Sunday", "sun": "Sunday",
    # Azərbaycan dilləri və translit variantları
    "bazar ertəsi": "Monday", "bazarertesi": "Monday", "bazarertesi": "Monday",
    "çərşənbə axşamı": "Tuesday", "çərsənbə axşamı": "Tuesday",
    "çərşənbə": "Wednesday", "cümə axşamı": "Thursday", "cümə": "Friday",
    "şənbə": "Saturday", "sebne": "Saturday", "shenbe": "Saturday",
    "bazar": "Sunday", "bazar günü": "Sunday", "cuma": "Friday", "cume": "Friday"
}
WEEKDAYS_EN = set(["monday","tuesday","wednesday","thursday","friday","saturday","sunday"])

def normalize_day_to_english(raw):
    if not raw:
        return ""
    s = str(raw).strip().lower()
    s = s.replace("\u00A0"," ").strip()
    # remove weird punctuation except az chars and digits, spaces, hyphen
    s = re.sub(r'[^0-9a-zA-Zçəğıöşüıə\s\-]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    if not s:
        return ""
    if s in DAY_MAP:
        return DAY_MAP[s]
    for k,v in DAY_MAP.items():
        if k in s or s in k:
            return v
    return s.capitalize()

def load_schedule_from_xlsx(path=SCHEDULE_XLSX):
    """
    Güclü diagnostika ilə schedule yükləyir.
    Return: (ok: bool, diagnostics: dict)
    """
    global SCHEDULE
    SCHEDULE = []
    diagnostics = {
        "path": path,
        "found_file": False,
        "num_rows": 0,
        "headers": [],
        "ncols": 0,
        "weekday_counts": [],
        "time_counts": [],
        "text_counts": [],
        "detected": {"group_col": None, "day_col": None, "time_col": None, "subject_col": None},
        "parsed_rows": []
    }

    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        diagnostics["found_file"] = True
    except FileNotFoundError:
        logger.error("Schedule faylı tapılmadı: %s", path)
        return False, diagnostics
    except Exception as e:
        logger.exception("Schedule faylı oxunarkən xəta: %s", e)
        return False, diagnostics

    sheet = wb.active
    rows = list(sheet.iter_rows(values_only=True))
    diagnostics["num_rows"] = len(rows)
    if not rows or len(rows) < 2:
        headers = [str(c) if c is not None else "" for c in (rows[0] if rows else [])]
        diagnostics["headers"] = headers
        diagnostics["ncols"] = len(headers)
        logger.warning("Schedule faylı boş və ya yetərsizdir: %s", path)
        return True, diagnostics

    headers = [str(c).strip() if c is not None else "" for c in rows[0]]
    diagnostics["headers"] = headers
    ncols = len(headers)
    diagnostics["ncols"] = ncols

    weekday_counts = [0]*ncols
    time_counts = [0]*ncols
    text_counts = [0]*ncols

    for r in rows[1:]:
        for i in range(ncols):
            cell = r[i] if i < len(r) else None
            if cell is None:
                continue
            s = str(cell).strip()
            sl = s.lower().replace("\u00A0"," ").strip()
            if sl in WEEKDAYS_EN or sl in DAY_MAP:
                weekday_counts[i] += 1
            if re.match(r'^\d{1,2}:\d{2}$', sl):
                time_counts[i] += 1
            if sl:
                text_counts[i] += 1

    diagnostics["weekday_counts"] = weekday_counts
    diagnostics["time_counts"] = time_counts
    diagnostics["text_counts"] = text_counts

    day_col = weekday_counts.index(max(weekday_counts)) if max(weekday_counts) > 0 else None
    time_col = time_counts.index(max(time_counts)) if max(time_counts) > 0 else None

    group_col = None
    subject_col = None
    for i,h in enumerate(headers):
        hl = h.lower()
        if 'group' in hl and group_col is None:
            group_col = i
        if any(k in hl for k in ("subject","lesson","fənn","fenn")) and subject_col is None:
            subject_col = i

    if group_col is None:
        best = None; best_cnt = -1
        for i in range(ncols):
            if i == day_col or i == time_col:
                continue
            if text_counts[i] > best_cnt:
                best_cnt = text_counts[i]; best = i
        group_col = best if best is not None else 0

    if subject_col is None:
        candidate = None; max_texts=-1
        for i in range(ncols):
            if i in (group_col, day_col, time_col):
                continue
            if text_counts[i] > max_texts:
                max_texts = text_counts[i]; candidate = i
        subject_col = candidate if candidate is not None else max(0, ncols-1)

    diagnostics["detected"]["group_col"] = group_col
    diagnostics["detected"]["day_col"] = day_col
    diagnostics["detected"]["time_col"] = time_col
    diagnostics["detected"]["subject_col"] = subject_col

    for idx, r in enumerate(rows[1:], start=2):
        def cell_at(i):
            return r[i] if i < len(r) and r[i] is not None else ""
        group = str(cell_at(group_col)).strip() if group_col is not None else ""
        day_raw = str(cell_at(day_col)).strip() if day_col is not None else ""
        time = str(cell_at(time_col)).strip() if time_col is not None else ""
        subject = str(cell_at(subject_col)).strip() if subject_col is not None else ""

        if not day_raw and len(r) > 1:
            c1 = r[1]
            if c1 and str(c1).strip().lower() in WEEKDAYS_EN.union(set(DAY_MAP.keys())):
                day_raw = str(c1).strip()

        if not group or not subject:
            diagnostics["parsed_rows"].append({
                "row_index": idx,
                "raw": [str(x) if x is not None else "" for x in r],
                "skipped": True,
                "reason": "missing group or subject",
                "group": group,
                "subject": subject
            })
            continue

        day_norm = normalize_day_to_english(day_raw)
        entry = {"group": group.strip(), "day": day_raw.strip(), "time": time.strip(), "subject": subject.strip(), "day_norm": day_norm}
        SCHEDULE.append(entry)
        diagnostics["parsed_rows"].append({
            "row_index": idx,
            "raw": [str(x) if x is not None else "" for x in r],
            "skipped": False,
            "parsed": entry
        })

    logger.info("Schedule yükləndi: %d sətir.", len(SCHEDULE))
    return True, diagnostics

def get_lessons_for_group_on_day(group_name, day_name):
    result = []
    if not group_name or not day_name:
        return result
    for l in SCHEDULE:
        if l["group"].strip().lower() != group_name.strip().lower():
            continue
        dn = l.get("day_norm","") or l.get("day","")
        if not dn:
            continue
        if dn.strip().lower() == day_name.strip().lower():
            result.append(l)
    def time_key(x):
        t = x.get("time","")
        m = re.match(r'(\d{1,2}):(\d{2})', t)
        if m:
            return int(m.group(1))*60 + int(m.group(2))
        return 0
    result.sort(key=time_key)
    return result

def get_lessons_filtered(group=None, day=None, subject=None):
    res = []
    for l in SCHEDULE:
        if group and l['group'].strip().lower() != group.strip().lower():
            continue
        if day:
            rd = normalize_day_to_english(day)
            dn = l.get("day_norm","") or l.get("day","")
            if dn.strip().lower() != rd.strip().lower() and dn.strip().lower() != day.strip().lower():
                continue
        if subject and subject.strip().lower() not in l.get('subject','').strip().lower():
            continue
        res.append(l)
    res.sort(key=lambda x: (re.match(r'(\d{1,2}):(\d{2})', x.get('time','')) and
                            (int(re.match(r'(\d{1,2}):(\d{2})', x.get('time','')).group(1))*60 +
                             int(re.match(r'(\d{1,2}):(\d{2})', x.get('time','')).group(2)))) or 0)
    return res

# ================= Bot əmrləri və axınları =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Salam! Zəhmət olmasa şəxsi nömrənizi daxil edin:")
    return ASK_PERSONAL_NUMBER

async def personal_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personal = update.message.text.strip()
    student = get_student_by_personal(personal)
    if not student:
        await update.message.reply_text("Bu nömrə tapılmadı. Yenidən şəxsi nömrənizi daxil edin:")
        return ASK_PERSONAL_NUMBER

    context.user_data["personal_number"] = personal

    if not student["code"] or student["code"].strip() == "":
        await update.message.reply_text("Zəhmət olmasa yeni kodunuzu yazın (ilk dəfə giriş üçün):")
        return SET_NEW_CODE
    else:
        await update.message.reply_text("Zəhmət olmasa mövcud kodunuzu daxil edin:")
        return ASK_CODE

async def set_new_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_code = update.message.text.strip()
    personal = context.user_data.get("personal_number")
    student = get_student_by_personal(personal)
    if not student:
        await update.message.reply_text("Sistem xətası. Yenidən /start ilə başlayın.")
        return ConversationHandler.END

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE students SET code = ? WHERE id = ?", (new_code, student["id"]))
    conn.commit()
    conn.close()

    tg_id = update.effective_user.id
    update_student_tg_id(student["id"], tg_id)
    create_session(tg_id, student["id"])

    await update.message.reply_text(f"Xoş gəldiniz, {student['full_name']}!\nMenyu üçün /menu yazın.")
    return ConversationHandler.END

async def code_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    personal = context.user_data.get("personal_number")
    student = get_student_by_personal(personal)
    if not student:
        await update.message.reply_text("Sistem xətası. Yenidən /start ilə başlayın.")
        return ConversationHandler.END

    if student["code"] != code:
        await update.message.reply_text("Kod düzgün deyil. Yenidən daxil edin:")
        return ASK_CODE

    tg_id = update.effective_user.id
    update_student_tg_id(student["id"], tg_id)
    create_session(tg_id, student["id"])

    await update.message.reply_text(f"Xoş gəldiniz, {student['full_name']}!\nMenyu üçün /menu yazın.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Əməliyyat ləğv edildi.")
    return ConversationHandler.END

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Söhbət sıfırlandı. Zəhmət olmasa şəxsi nömrənizi yenidən daxil edin:")
    return ASK_PERSONAL_NUMBER

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    student = get_student_by_tg_id(tg_id)
    if not student:
        await update.message.reply_text("Əvvəlcə /start yazıb daxil olun.")
        return

    kb = [
        [InlineKeyboardButton("📅 Bugünkü dərslər", callback_data="today")],
        [InlineKeyboardButton("📊 Qiymətlər", callback_data="grades")],
        [InlineKeyboardButton("🚫 Qayıblar", callback_data="attendance")],
        [InlineKeyboardButton("🔒 Şifrəni dəyiş", callback_data="change_code")],
        [InlineKeyboardButton("Çıxış", callback_data="logout")]
    ]
    await update.message.reply_text("Seçim edin:", reply_markup=InlineKeyboardMarkup(kb))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    tg_id = query.from_user.id
    student = get_student_by_tg_id(tg_id)
    if not student:
        await query.message.reply_text("Əvvəl qeydiyyatdan keçin. /start yazın.")
        return

    if data == "today":
        today_eng = datetime.now().strftime("%A")
        lessons = get_lessons_for_group_on_day(student["group_name"], today_eng)
        if not lessons:
            await query.message.reply_text("Bu gün üçün dərs yoxdur.")
        else:
            text_lines = [f"Bugün — {student['group_name']}:"]
            for ls in lessons:
                t = ls.get("time","—")
                subj = ls.get("subject","—")
                text_lines.append(f"{t} — {subj}")
            await query.message.reply_text("\n".join(text_lines))
    elif data == "grades":
        await query.message.reply_text("Qiymətlər funksiyası hazırlanır.")
    elif data == "attendance":
        await query.message.reply_text("Qayıblar funksiyası hazırlanır.")
    elif data == "change_code":
        context.user_data["awaiting_new_code"] = True
        await query.message.reply_text("Yeni şifrənizi daxil edin:")
    elif data == "logout":
        await query.message.reply_text("Çıxış etdiniz. Yenidən daxil olmaq üçün /start yazın.")

async def change_code_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_code = update.message.text.strip()
    tg_id = update.effective_user.id
    student = get_student_by_tg_id(tg_id)
    if not student:
        await update.message.reply_text("Qeydiyyatdan keçməmisiniz. /start yazın.")
        return ConversationHandler.END

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE students SET code = ? WHERE id = ?", (new_code, student["id"]))
    conn.commit()
    conn.close()

    await update.message.reply_text("Şifrəniz uğurla dəyişdirildi!")
    return ConversationHandler.END

async def addstudent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 5:
        await update.message.reply_text(
            "İstifadə: /addstudent ADMIN_CODE personal_number full_name group initial_code\n"
            "Məsələn: /addstudent Keno2007pm@ +99450766263 \"Kenan Ehmedov\" IT-101 1234567"
        )
        return

    admin_code = args[0]
    if admin_code != ADMIN_CODE:
        await update.message.reply_text("Yanlış admin kodu.")
        return

    personal_number = args[1]
    full_name = " ".join(args[2:-2])
    group = args[-2]
    initial_code = args[-1]

    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO students (personal_number, full_name, group_name, code) VALUES (?, ?, ?, ?)",
            (personal_number, full_name, group, initial_code)
        )
        conn.commit()
        await update.message.reply_text(f"Tələbə əlavə edildi: {full_name}")
    except sqlite3.IntegrityError:
        await update.message.reply_text("Bu nömrə artıq mövcuddur.")
    except Exception as e:
        await update.message.reply_text(f"Xəta: {e}")
    finally:
        conn.close()

async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("İstifadə: /schedule <group> [day] [subject]\nMəsələn: /schedule IT-101 Monday Programming")
        return
    group = args[0]
    day = args[1] if len(args) >= 2 else None
    subject = " ".join(args[2:]) if len(args) >= 3 else None

    lessons = get_lessons_filtered(group=group, day=day, subject=subject)
    if not lessons:
        await update.message.reply_text("Uyğun dərs tapılmadı.")
        return

    lines = [f"Cədvəl — {group} {('' if not day else day)} {('' if not subject else subject)}:"]
    for ls in lessons:
        lines.append(f"{ls.get('day_norm') or ls.get('day','—')} {ls.get('time','—')} — {ls.get('subject','—')}")
    await update.message.reply_text("\n".join(lines))

async def reload_schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, diag = load_schedule_from_xlsx()
    if not ok:
        await update.message.reply_text("Schedule faylı tapılmadı və ya oxunmadı. Serverdə faylın adını və yerini yoxlayın.")
        return
    await update.message.reply_text(f"Cədvəl yükləndi. {len(SCHEDULE)} sətir parse olundu.")

# Shows diagnostics and first parsed rows
def _chunk_text(s, limit=3900):
    return [s[i:i+limit] for i in range(0, len(s), limit)]

async def showschedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, diag = load_schedule_from_xlsx()
    if not ok:
        await update.message.reply_text("Schedule faylı tapılmadı və ya oxunmadı. Bot serverində faylın adını və mövcudluğunu yoxla.")
        return
    parts = []
    parts.append(f"Schedule faylı: {diag['path']}")
    parts.append(f"Rows (including header): {diag['num_rows']}")
    parts.append(f"Ncols: {diag['ncols']}")
    parts.append("Headers: " + ", ".join([h or "<empty>" for h in diag["headers"]]))
    parts.append("Detected cols: group=%s, day=%s, time=%s, subject=%s" % (
        diag["detected"]["group_col"], diag["detected"]["day_col"],
        diag["detected"]["time_col"], diag["detected"]["subject_col"]
    ))
    parts.append("Weekday counts per column: " + ", ".join(map(str, diag["weekday_counts"])))
    parts.append("Time counts per column: " + ", ".join(map(str, diag["time_counts"])))
    parts.append("Parsed (first 20) rows summary:")
    for pr in diag["parsed_rows"][:20]:
        if pr.get("skipped"):
            parts.append(f"  row {pr['row_index']}: SKIPPED reason={pr['reason']} raw={pr['raw']}")
        else:
            p = pr["parsed"]
            parts.append(f"  row {pr['row_index']}: group={p['group']} day={p['day_norm'] or p['day']} time={p['time']} subject={p['subject']}")
    grp_counts = {}
    for e in SCHEDULE:
        g = e['group'].strip()
        grp_counts[g] = grp_counts.get(g, 0) + 1
    parts.append("Group counts: " + (", ".join(f"{k}={v}" for k,v in grp_counts.items()) if grp_counts else "No parsed lessons"))

    text = "\n".join(parts)
    chunks = _chunk_text(text)
    for c in chunks:
        await update.message.reply_text(c)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Bağışlayın, bu əmri tanımıram. /start və ya /menu istifadə edin.")
    elif update.callback_query:
        await update.callback_query.message.reply_text("Bağışlayın, bu əmri tanımıram. /start və ya /menu istifadə edin.")

async def generic_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_new_code"):
        context.user_data.pop("awaiting_new_code", None)
        return await change_code_received(update, context)
    await update.message.reply_text("Mesaj alındı. /menu və ya /start istifadə edin.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Botda xəta baş verdi. Zəhmət olmasa bir az sonra yenidən cəhd edin.")
    except Exception:
        logger.exception("Error while sending error message to user")

# ================= Main =================
def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PERSONAL_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, personal_number_received)],
            SET_NEW_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_new_code)],
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_received)],
            CHANGE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_code_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("reset", reset)],
        per_user=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("addstudent", addstudent_cmd))
    application.add_handler(CommandHandler("schedule", schedule_cmd))
    application.add_handler(CommandHandler("reloadschedule", reload_schedule_cmd))
    application.add_handler(CommandHandler("showschedule", showschedule_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, generic_text_handler))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    application.add_error_handler(error_handler)

    # startup: cədvəl yüklə və log göstər
    ok, diag = load_schedule_from_xlsx()
    if ok:
        logger.info("Startup: schedule loaded, parsed rows = %d", len(SCHEDULE))
    else:
        logger.warning("Startup: schedule not loaded or file missing.")

    print("Bot işləyir...")
    application.run_polling()

if __name__ == "__main__":
    main()
