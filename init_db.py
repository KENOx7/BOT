import sqlite3

DB_PATH = "database.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # students cədvəli (code sütunu ilə)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE,
        personal_number TEXT UNIQUE,
        full_name TEXT,
        group_name TEXT,
        code TEXT
    )
    """)

    # grades cədvəli
    cur.execute("""
    CREATE TABLE IF NOT EXISTS grades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        subject TEXT,
        grade INTEGER,
        FOREIGN KEY(student_id) REFERENCES students(id)
    )
    """)

    # attendance cədvəli
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        subject TEXT,
        date TEXT,
        status TEXT,
        FOREIGN KEY(student_id) REFERENCES students(id)
    )
    """)

    # sessions cədvəli
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        tg_id INTEGER PRIMARY KEY,
        student_id INTEGER,
        FOREIGN KEY(student_id) REFERENCES students(id)
    )
    """)

    conn.commit()
    conn.close()
    print("Database yaradıldı!")

if __name__ == "__main__":
    init_db()
