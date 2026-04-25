import sqlite3
import os
import hashlib
from flask import g

DATABASE = 'dance_studio.db'

def get_db():
    from app import app
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    c.executescript('''
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL, -- admin | trainer
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            trainer_id INTEGER UNIQUE REFERENCES trainers(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS trainers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            specialization TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            trainer_id INTEGER REFERENCES trainers(id) ON DELETE SET NULL,
            age_range TEXT,
            level TEXT,
            max_capacity INTEGER DEFAULT 20,
            description TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            birthdate TEXT,
            parent_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS client_groups (
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
            joined_date TEXT DEFAULT (date('now','localtime')),
            PRIMARY KEY (client_id, group_id)
        );

        CREATE TABLE IF NOT EXISTS group_trainers (
            group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
            trainer_id INTEGER REFERENCES trainers(id) ON DELETE CASCADE,
            PRIMARY KEY (group_id, trainer_id)
        );

        CREATE TABLE IF NOT EXISTS subscription_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            lessons_count INTEGER NOT NULL,
            price REAL NOT NULL,
            validity_days INTEGER NOT NULL,
            carry_over INTEGER DEFAULT 0,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            type_id INTEGER REFERENCES subscription_types(id),
            group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            lessons_total INTEGER NOT NULL,
            lessons_left INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            price_paid REAL NOT NULL,
            payment_method TEXT DEFAULT 'cash',
            cash_amount REAL DEFAULT 0,
            non_cash_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            duration_minutes INTEGER DEFAULT 60,
            room TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER REFERENCES schedule(id) ON DELETE CASCADE,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            present INTEGER DEFAULT 0,
            charged_lessons INTEGER DEFAULT 0,
            charged_subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
            marked_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS intensives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            trainer_id INTEGER REFERENCES trainers(id) ON DELETE SET NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            hours REAL NOT NULL,
            payment_type TEXT NOT NULL, -- cash | subscription
            payment_method TEXT, -- cash | card for money payments
            amount REAL DEFAULT 0,
            subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
            lessons_written_off INTEGER DEFAULT 0,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS intensive_clients (
            intensive_id INTEGER REFERENCES intensives(id) ON DELETE CASCADE,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            payment_type TEXT DEFAULT 'cash', -- cash | subscription
            payment_method TEXT DEFAULT 'cash', -- cash | card | mixed
            hours REAL DEFAULT 1,
            amount REAL DEFAULT 0,
            cash_amount REAL DEFAULT 0,
            non_cash_amount REAL DEFAULT 0,
            subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
            lessons_written_off INTEGER DEFAULT 0,
            PRIMARY KEY (intensive_id, client_id)
        );
    ''')

    # Default admin
    pw = hashlib.sha256('admin123'.encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO admins (username, password, name) VALUES ('admin', ?, 'Администратор')", (pw,))
    c.execute("INSERT OR IGNORE INTO users (role, username, password, name, trainer_id) VALUES ('admin', 'admin', ?, 'Администратор', NULL)", (pw,))

    # Default subscription types
    c.execute("SELECT COUNT(*) as c FROM subscription_types")
    if c.fetchone()[0] == 0:
        defaults = [
            ('Стандарт 8 занятий', 8, 3200.0, 28, 0, '8 занятий, действует 28 дней. Пропущенные занятия не сохраняются. Только одна группа.'),
            ('Стандарт 4 занятия', 4, 1800.0, 28, 0, '4 занятия, действует 28 дней. Пропущенные занятия не сохраняются. Только одна группа.'),
            ('Стандарт+ 8 занятий', 8, 4500.0, 60, 1, '8 занятий, действует 60 дней. Пропущенные занятия сохраняются. Только одна группа.'),
            ('Разовое занятие', 1, 600.0, 1, 0, 'Одно разовое занятие. 600 рублей.'),
        ]
        c.executemany('INSERT INTO subscription_types (name, lessons_count, price, validity_days, carry_over, description) VALUES (?,?,?,?,?,?)', defaults)

    # Migration for existing databases: payment method tracking (cash / card)
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN payment_method TEXT DEFAULT 'cash'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN cash_amount REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN non_cash_amount REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migration for existing intensives table
    try:
        c.execute("ALTER TABLE intensives ADD COLUMN group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensives ADD COLUMN trainer_id INTEGER REFERENCES trainers(id) ON DELETE SET NULL")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensives ADD COLUMN payment_method TEXT")
    except sqlite3.OperationalError:
        pass

    # Migration for detailed intensive payments per participant
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN payment_type TEXT DEFAULT 'cash'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN payment_method TEXT DEFAULT 'cash'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN hours REAL DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN amount REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN cash_amount REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN non_cash_amount REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE intensive_clients ADD COLUMN lessons_written_off INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migration for attendance charging control
    try:
        c.execute("ALTER TABLE attendance ADD COLUMN charged_lessons INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE attendance ADD COLUMN charged_subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL")
    except sqlite3.OperationalError:
        pass

    # Backfill trainer accounts (default password: trainer123)
    trainer_pw = hashlib.sha256('trainer123'.encode()).hexdigest()
    trainers_rows = c.execute("SELECT id, name FROM trainers").fetchall()
    for tr in trainers_rows:
        c.execute(
            "INSERT OR IGNORE INTO users (role, username, password, name, trainer_id) VALUES ('trainer', ?, ?, ?, ?)",
            (f"trainer{tr['id']}", trainer_pw, tr['name'], tr['id'])
        )

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

if __name__ == '__main__':
    init_db()
