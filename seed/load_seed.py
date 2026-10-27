#!/usr/bin/env python3
"""
Seed data loader for Supabase PostgreSQL / Local PostgreSQL.
Usage:
  DATABASE_URL="<supabase_connection_url>" python3 seed/load_seed.py
"""
import os
import sys
import psycopg2

class CopyStream:
    """Wrapper that streams data rows until the '\\.' end-of-copy delimiter."""
    def __init__(self, f):
        self.f = f
        self.ended = False

    def read(self, size=-1):
        if self.ended:
            return ""
        chunk = []
        bytes_read = 0
        while True:
            line = self.f.readline()
            if not line:
                self.ended = True
                break
            if line.strip() == r"\.":
                self.ended = True
                break
            chunk.append(line)
            bytes_read += len(line)
            if size > 0 and bytes_read >= size:
                break
        return "".join(chunk)

    def readline(self):
        if self.ended:
            return ""
        line = self.f.readline()
        if not line or line.strip() == r"\.":
            self.ended = True
            return ""
        return line


from urllib.parse import quote_plus, unquote

def format_db_url(url):
    if not url:
        return url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    prefix, _, rest = url.partition("://")
    if "@" in rest:
        auth_part, host_part = rest.rsplit("@", 1)
        if ":" in auth_part:
            user, password = auth_part.split(":", 1)
            clean_pw = unquote(password)
            encoded_pw = quote_plus(clean_pw)
            url = f"{prefix}://{user}:{encoded_pw}@{host_part}"
    return url


def main():
    raw_db_url = os.environ.get("DATABASE_URL")
    if not raw_db_url:
        db_url = "postgresql://sewasetu:sewasetu123@localhost:5432/sewasetu"
        print(f"DATABASE_URL not found, using local default: {db_url}")
    else:
        db_url = format_db_url(raw_db_url)
        print("Using DATABASE_URL from environment.")

    # Locate files
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)

    seed_candidates = [
        os.path.join(base_dir, "seed.sql"),
        "seed/seed.sql",
        "seed.sql"
    ]
    seed_path = next((p for p in seed_candidates if os.path.exists(p)), None)
    if not seed_path:
        print("Error: seed.sql file could not be found.")
        sys.exit(1)

    schema_candidates = [
        os.path.join(project_root, "app", "schema.sql"),
        os.path.join(base_dir, "schema.sql"),
        "app/schema.sql"
    ]
    schema_path = next((p for p in schema_candidates if os.path.exists(p)), None)

    print(f"Connecting to database...")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()

        # 1. Initialize schema if not present
        cur.execute("SELECT to_regclass('applications');")
        res = cur.fetchone()
        if not res or not res[0]:
            if schema_path and os.path.exists(schema_path):
                print(f"Tables not found. Initializing schema from {schema_path}...")
                with open(schema_path, "r", encoding="utf-8") as sf:
                    cur.execute(sf.read())
                conn.commit()
                print("Schema initialized successfully.")
            else:
                print("Notice: schema.sql not found; assuming tables exist.")

        # 2. Check if already seeded
        cur.execute("SELECT count(*) FROM applications;")
        count = cur.fetchone()[0]
        if count > 1000:
            print(f"Database already contains {count} applications. Skipping seed.")
            cur.close()
            conn.close()
            return

        print(f"Streaming data from {seed_path}...")
        with open(seed_path, "r", encoding="utf-8") as f:
            while True:
                line = f.readline()
                if not line:
                    break
                s = line.strip()
                if not s or s.startswith("--"):
                    continue
                if s.upper().startswith("COPY ") and "FROM STDIN" in s.upper():
                    cmd = s.rstrip(";")
                    table_name = cmd.split()[1]
                    print(f"Importing table {table_name}...")
                    stream = CopyStream(f)
                    cur.copy_expert(cmd, stream)
                    conn.commit()
                elif s.upper().startswith("SELECT SETVAL"):
                    cur.execute(s)
                    conn.commit()

        cur.execute("SELECT count(*) FROM applications;")
        new_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM portal_users;")
        users_count = cur.fetchone()[0]
        print(f"Success! Database now has {new_count} applications and {users_count} portal users.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error executing seed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
