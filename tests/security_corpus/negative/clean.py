import os
import subprocess


def load(name):
    return os.getenv(f"DB_{name.upper()}")


password = os.getenv("DB_PASSWORD")
api_key = os.getenv("DB_API_KEY")


def query(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    return cur.fetchall()


def run_backup():
    subprocess.run(["pg_dump", "app"], check=True)