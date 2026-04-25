import csv
import sqlite3
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "dance_studio.db"
CSV_PATH = Path(r"c:\Users\user\Downloads\clients_export.csv")


def clean(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def build_name(row: dict[str, str]) -> str:
    first_name = clean(row.get("Имя"))
    last_name = clean(row.get("Фамилия"))
    return " ".join(part for part in (first_name, last_name) if part).strip()


def build_notes(row: dict[str, str]) -> str:
    # Import only user comment into notes; ignore auxiliary export fields.
    return clean(row.get("Комментарий"))


def client_exists(conn: sqlite3.Connection, name: str, phone: str, email: str, birthdate: str) -> bool:
    conditions: list[str] = []
    params: list[str] = []

    if phone:
        conditions.append("phone = ?")
        params.append(phone)
    if email:
        conditions.append("email = ?")
        params.append(email)
    if name and birthdate:
        conditions.append("(name = ? AND birthdate = ?)")
        params.extend([name, birthdate])

    if not conditions:
        conditions.append("name = ?")
        params.append(name)

    query = f"SELECT 1 FROM clients WHERE {' OR '.join(conditions)} LIMIT 1"
    return conn.execute(query, params).fetchone() is not None


def main() -> None:
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV file not found: {CSV_PATH}")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database file not found: {DB_PATH}")

    inserted = 0
    skipped = 0

    with sqlite3.connect(DB_PATH) as conn:
        with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)

            for row in reader:
                name = build_name(row)
                if not name:
                    skipped += 1
                    continue

                phone = clean(row.get("Телефон"))
                email = clean(row.get("Email"))
                birthdate = clean(row.get("ДеньРождения"))
                notes = build_notes(row)

                if client_exists(conn, name=name, phone=phone, email=email, birthdate=birthdate):
                    skipped += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO clients (name, phone, email, birthdate, parent_id, notes)
                    VALUES (?, ?, ?, ?, NULL, ?)
                    """,
                    (name, phone, email, birthdate, notes),
                )
                inserted += 1

        conn.commit()

    print(f"Импорт завершён. Добавлено: {inserted}, пропущено: {skipped}.")


if __name__ == "__main__":
    main()
