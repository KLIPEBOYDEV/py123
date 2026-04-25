import sqlite3
conn = sqlite3.connect('dance_studio.db')
c = conn.cursor()
c.execute('SELECT s.id, s.date, s.time, g.name FROM schedule s JOIN groups g ON s.group_id = g.id WHERE s.date BETWEEN "2026-04-20" AND "2026-04-26" ORDER BY s.date, s.time')
print(c.fetchall())