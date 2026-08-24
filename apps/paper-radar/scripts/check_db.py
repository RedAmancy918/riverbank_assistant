import sqlite3, json

conn = sqlite3.connect('data/papers.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('tables:', cur.fetchall())
cur.execute('PRAGMA table_info(papers)')
print('cols:', cur.fetchall())
cur.execute("SELECT arxiv_id, reported_on, substr(title,1,70) FROM papers WHERE reported_on IS NOT NULL ORDER BY reported_on DESC, arxiv_id")
rows = cur.fetchall()
print('reported count:', len(rows))
for r in rows:
    print(r)
