# bot.py
import logging
import sqlite3
import os
import re
from datetime import datetime, date, timedelta
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
SCHEDULE = []  # hər element: {"week_type", "group", "day_norm", "time", "subject", "teacher", "room"}

DAY_MAP = {
    "monday": "1", "mon": "1",
    "tuesday": "2", "tue": "2",
    "wednesday": "3", "wed": "3",
    "thursday": "4", "thu": "4",
    "friday": "5", "fri": "5",
    "saturday": "6", "sat": "6",
    "sunday": "7", "sun": "7",
    # Azərbaycan dilləri və translit variantları
    "bazar ertəsi": "1", "bazarertesi": "1", "bazarertesi": "1",
    "çərşənbə axşamı": "2", "çərsənbə axşamı": "2",
    "çərşənbə": "3", "cümə axşamı": "4", "cümə": "5",
    "şənbə": "6", "sebne": "6", "shenbe": "6",
    "bazar": "7", "bazar günü": "7", "cuma": "5", "cume": "5",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7"
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

def is_alt_week():
    """Həftənin alt və ya üst həftə olduğunu müəyyən edir."""
    # datetime.isocalendar() həftə nömrəsini qaytarır.
    # ISO 8601-ə görə, həftələr 1-dən başlayır. Tək həftə (1, 3, 5) alt, cüt həftə (2, 4, 6) üst həftədir.
    # Ayın birinci həftəsi alt həftə olaraq qəbul edilir.
    
    today = datetime.now()
    first_day_of_month = date(today.year, today.month, 1)
    
    # Ayın ilk həftəsinin nömrəsini tapırıq
    first_week_num = first_day_of_month.isocalendar()[1]
    
    # Cari həftənin nömrəsini tapırıq
    current_week_num = today.isocalendar()[1]
    
    # Fərq təkdirsə, bu ayın ilk həftəsi ilə eyni tipdir.
    week_diff = current_week_num - first_week_num
    
    # Ayın ilk həftəsi "alt" olaraq qəbul edildiyi üçün, 
    # həftənin növü fərqin cüt olub-olmamasından asılıdır.
    # fərq cütdürsə (0, 2, 4...), həftə növü ilkin həftə ilə eynidir (alt).
    # fərq təkdirsə (1, 3, 5...), həftə növü ilkin həftənin əksidir (üst).
    
    # Yəni, fərq cüt olarsa (fərq % 2 == 0), "alt" həftədir.
    # fərq tək olarsa (fərq % 2 != 0), "üst" həftədir.

    return week_diff % 2 == 0

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
        "detected": {"week_col": None, "group_col": None, "day_col": None, "subject_col": None},
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

    week_col, group_col, day_col, subject_col = None, None, None, None
    for i, h in enumerate(headers):
        hl = h.lower()
        if 'week' in hl:
            week_col = i
        elif 'group' in hl:
            group_col = i
        elif 'day' in hl:
            day_col = i
        elif 'subject' in hl:
            subject_col = i
    
    diagnostics["detected"]["week_col"] = week_col
    diagnostics["detected"]["group_col"] = group_col
    diagnostics["detected"]["day_col"] = day_col
    diagnostics["detected"]["subject_col"] = subject_col

    for idx, r in enumerate(rows[1:], start=2):
        def cell_at(i):
            return r[i] if i < len(r) and r[i] is not None else ""

        week_type = str(cell_at(week_col)).strip().lower() if week_col is not None else ""
        group = str(cell_at(group_col)).strip() if group_col is not None else ""
        day_raw = str(cell_at(day_col)).strip() if day_col is not None else ""
        subject_raw = str(cell_at(subject_col)).strip() if subject_col is not None else ""
        
        if not all([week_type, group, day_raw, subject_raw]):
            diagnostics["parsed_rows"].append({
                "row_index": idx,
                "raw": [str(x) if x is not None else "" for x in r],
                "skipped": True,
                "reason": "missing critical data",
                "data": {"week": week_type, "group": group, "day": day_raw, "subject": subject_raw}
            })
            continue

        day_norm = normalize_day_to_english(day_raw)
        
        # Subject sütunundakı məlumatları ayırmaq
        # Örnək: "1)IT əsasları (seminar) - Kazımov Ramin (08:00, otaq 02KM)"
        match = re.match(r'^(?:\d+\))?\s*(.*?)(?:\s+-\s+(.*?))?(?:\s+\((.*?)\))?$', subject_raw)
        
        subject = ""
        teacher = ""
        time_str = ""
        room = ""

        # Mətndə vaxtı və otağı tapmaq üçün regex
        time_room_match = re.search(r'\((\d{1,2}:\d{2})(?:,\s*(otaq\s+.*?))?\)', subject_raw)
        if time_room_match:
            time_str = time_room_match.group(1).strip()
            room_match_text = time_room_match.group(2)
            if room_match_text:
                room = room_match_text.strip()
        
        # Mətndə fənnin adını və müəllimi tapmaq üçün
        subject_teacher_match = re.match(r'^(?:\d+\))?\s*(.*?)(?:\s+\(.*?\))?\s+-\s+(.*?)\s+', subject_raw)
        if subject_teacher_match:
            subject = subject_teacher_match.group(1).strip()
            teacher = subject_teacher_match.group(2).strip()
        else: # əgər format fərqli olsa
            subject_no_extra = re.sub(r'\(.*?\)\s*-\s*.*', '', subject_raw).strip()
            subject = re.sub(r'^\d+\)\s*', '', subject_no_extra).strip()
            teacher_match = re.search(r'-\s*(.*?)\s*\(', subject_raw)
            if teacher_match:
                teacher = teacher_match.group(1).strip()
        
        if not time_str and not subject: # Fənn adı yoxdursa atla
            diagnostics["parsed_rows"].append({
                "row_index": idx,
                "raw": [str(x) if x is not None else "" for x in r],
                "skipped": True,
                "reason": "missing subject details",
                "data": {"subject_raw": subject_raw}
            })
            continue

        entry = {
            "week_type": week_type,
            "group": group.strip(),
            "day": day_raw.strip(),
            "day_norm": day_norm,
            "time": time_str,
            "subject": subject,
            "teacher": teacher,
            "room": room
        }
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
    
    current_week_is_alt = is_alt_week()
    expected_week_type = "alt" if current_week_is_alt else "ust"

    for l in SCHEDULE:
        if l["group"].strip().lower() != group_name.strip().lower():
            continue
        
        if l["week_type"].lower() != expected_week_type:
            continue
            
        dn = l.get("day_norm","")
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

def get_lessons_filtered(group=None, day=None, subject=None, week_type=None):
    res = []
    
    if week_type is None:
        current_week_is_alt = is_alt_week()
        week_type = "alt" if current_week_is_alt else "ust"

    for l in SCHEDULE:
        if l['week_type'].lower() != week_type.lower():
            continue
        if group and l['group'].strip().lower() != group.strip().lower():
            continue
        if day:
            rd = normalize_day_to_english(day)
            dn = l.get("day_norm","")
            if dn.strip().lower() != rd.strip().lower():
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

    # Telefon nömrəsini avtomatik formatla
    digits = re.sub(r'\D', '', personal)
    if digits.startswith("0") and len(digits) == 10:
        personal = "+994" + digits[1:]
    elif digits.startswith("5") and len(digits) == 9:
        personal = "+994" + digits
    elif digits.startswith("9940") and len(digits) == 12:
        personal = "+994" + digits[3:]
    elif digits.startswith("994") and len(digits) == 12:
        personal = "+" + digits
    elif not digits.startswith("994"):
        personal = "+994" + digits

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
        [InlineKeyboardButton("📅 Dərs cədvəlinə bax", callback_data="schedule_menu")],
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

    if data == "schedule_menu":
        kb = [
            [InlineKeyboardButton("📅 Bugün", callback_data="sched_today")],
            [InlineKeyboardButton("📅 Sabah", callback_data="sched_tomorrow")],
            [InlineKeyboardButton("📅 Bu həftə", callback_data="sched_week")]
        ]
        await query.message.reply_text("Zəhmət olmasa tarix seçin:", reply_markup=InlineKeyboardMarkup(kb))

    elif data in ["sched_today", "sched_tomorrow", "sched_week"]:
        today = datetime.now()
        
        if data == "sched_today":
            target_date = today
            lessons = get_lessons_filtered(group=student["group_name"], day=str(target_date.weekday() + 1))
            week_type_str = "alt" if is_alt_week() else "üst"
            if not lessons:
                message = f"{target_date.strftime('%d.%m.%Y')} — {week_type_str.capitalize()} həftə üçün dərs yoxdur."
            else:
                text_lines = [f"{target_date.strftime('%d.%m.%Y')} — {week_type_str.capitalize()} həftə, {student['group_name']}:"]
                for ls in lessons:
                    time_str = ls.get("time", "—")
                    subject_str = ls.get("subject", "—")
                    teacher_str = f"({ls.get('teacher', '—')})" if ls.get('teacher') else ""
                    room_str = f"[otaq {ls.get('room', '—')}]" if ls.get('room') else ""
                    text_lines.append(f"{time_str} - {subject_str} {teacher_str} {room_str}".strip())
                message = "\n".join(text_lines)
            await query.message.reply_text(message)

        elif data == "sched_tomorrow":
            target_date = today + timedelta(days=1)
            is_tomorrow_weekend = target_date.weekday() >= 5
            
            if is_tomorrow_weekend:
                week_type_str = "alt" if not is_alt_week() else "üst"
            else:
                week_type_str = "alt" if is_alt_week() else "üst"
            
            lessons = get_lessons_filtered(group=student["group_name"], day=str(target_date.weekday() + 1), week_type=week_type_str)

            if not lessons:
                message = f"{target_date.strftime('%d.%m.%Y')} — {week_type_str.capitalize()} həftə üçün dərs yoxdur."
            else:
                text_lines = [f"{target_date.strftime('%d.%m.%Y')} — {week_type_str.capitalize()} həftə, {student['group_name']}:"]
                for ls in lessons:
                    time_str = ls.get("time", "—")
                    subject_str = ls.get("subject", "—")
                    teacher_str = f"({ls.get('teacher', '—')})" if ls.get('teacher') else ""
                    room_str = f"[otaq {ls.get('room', '—')}]" if ls.get('room') else ""
                    text_lines.append(f"{time_str} - {subject_str} {teacher_str} {room_str}".strip())
                message = "\n".join(text_lines)
            await query.message.reply_text(message)

        elif data == "sched_week":
            current_day_of_week = today.weekday()
            
            # If today is Saturday (5) or Sunday (6), show next week's schedule
            if current_day_of_week >= 5:
                # The next week starts with the opposite week type
                week_type_str = "alt" if not is_alt_week() else "ust"
                week_start_date = today + timedelta(days=(7 - current_day_of_week))
                
                text_lines = [f"Növbəti həftə ({week_start_date.strftime('%d.%m.%Y')} tarixindən) — {week_type_str.capitalize()} həftə, {student['group_name']}:"]
                
                # Fetch lessons for the next week
                lessons = get_lessons_filtered(group=student["group_name"], week_type=week_type_str)
                
            else: # If it's a weekday, show the current week's schedule
                week_type_str = "alt" if is_alt_week() else "ust"
                text_lines = [f"Bu həftə — {week_type_str.capitalize()} həftə, {student['group_name']}:"]
                
                # Fetch lessons for the current week
                lessons = get_lessons_filtered(group=student["group_name"], week_type=week_type_str)
                
            
            if not lessons:
                message = f"{week_type_str.capitalize()} həftə üçün dərs yoxdur."
            else:
                # Sort lessons by day_norm and time
                sorted_lessons = sorted(lessons, key=lambda x: (x.get('day_norm', ''), x.get('time', '')))
                
                current_day_text = ""
                for ls in sorted_lessons:
                    day_norm_text = ls.get("day_norm", "—")
                    if day_norm_text != current_day_text:
                        text_lines.append(f"\n**{day_norm_text.capitalize()}**")
                        current_day_text = day_norm_text
                    
                    time_str = ls.get("time", "—")
                    subject_str = ls.get("subject", "—")
                    teacher_str = f"({ls.get('teacher', '—')})" if ls.get('teacher') else ""
                    room_str = f"[otaq {ls.get('room', '—')}]" if ls.get('room') else ""
                    text_lines.append(f"{time_str} - {subject_str} {teacher_str} {room_str}".strip())
                message = "\n".join(text_lines)
            
            await query.message.reply_text(message)

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
        await update.message.reply_text("İstifadə: /schedule <group> [day] [week_type]\nMəsələn: /schedule IT-101 1 alt")
        return
    group = args[0]
    day = args[1] if len(args) >= 2 else None
    week_type = args[2] if len(args) >= 3 and args[2].lower() in ["alt", "ust"] else None

    lessons = get_lessons_filtered(group=group, day=day, week_type=week_type)
    if not lessons:
        await update.message.reply_text("Uyğun dərs tapılmadı.")
        return

    lines = [f"Cədvəl — {group} {('' if not day else day)} {('' if not week_type else week_type)}:"]
    for ls in lessons:
        lines.append(f"{ls.get('day_norm') or ls.get('day','—')} ({ls.get('week_type','—')}) {ls.get('time','—')} — {ls.get('subject','—')}")
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
    parts.append("Detected cols: week=%s, group=%s, day=%s, subject=%s" % (
        diag["detected"]["week_col"], diag["detected"]["group_col"],
        diag["detected"]["day_col"], diag["detected"]["subject_col"]
    ))
    
    parts.append("Parsed (first 20) rows summary:")
    for pr in diag["parsed_rows"][:20]:
        if pr.get("skipped"):
            parts.append(f"  row {pr['row_index']}: SKIPPED reason={pr['reason']} raw={pr['raw']}")
        else:
            p = pr["parsed"]
            parts.append(f"  row {pr['row_index']}: week={p['week_type']} group={p['group']} day={p['day_norm']} time={p['time']} subject={p['subject']}")
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
