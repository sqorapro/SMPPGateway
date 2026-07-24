import sqlite3
import threading
from datetime import datetime


class Database:
    def __init__(self, db_path="smpp_gateway.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS sms_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                number TEXT,
                message TEXT,
                modem_port TEXT,
                status TEXT,
                source TEXT,
                details TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                number TEXT,
                modem_port TEXT,
                status TEXT,
                audio_file TEXT,
                duration TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )''')
            conn.commit()
            conn.close()

    def log_sms(self, number, message, modem_port, status, source="manual", details=""):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                "INSERT INTO sms_log (timestamp, number, message, modem_port, status, source, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), number, message, modem_port, status, source, details)
            )
            conn.commit()
            conn.close()

    def log_call(self, number, modem_port, status, audio_file="", duration=""):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                "INSERT INTO call_log (timestamp, number, modem_port, status, audio_file, duration) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), number, modem_port, status, audio_file, duration)
            )
            conn.commit()
            conn.close()

    def get_sms_log(self, limit=100):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT ?", (limit,))
            rows = c.fetchall()
            conn.close()
            return rows

    def get_call_log(self, limit=100):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT * FROM call_log ORDER BY id DESC LIMIT ?", (limit,))
            rows = c.fetchall()
            conn.close()
            return rows

    def get_setting(self, key, default=None):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = c.fetchone()
            conn.close()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
            conn.close()
