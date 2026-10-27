#!/usr/bin/env python3
"""
Seed data loader for Render PostgreSQL / Local PostgreSQL.
Usage:
  python seed/load_seed.py
  OR
  cd seed && python load_seed.py
"""
import os
import sys
import psycopg2

def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # Fallback to local default if DATABASE_URL not supplied
        db_url = "postgresql://sewasetu:sewasetu123@localhost:5432/sewasetu"
        print(f"DATABASE_URL not found, using local default: {db_url}")
    else:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        print("Using DATABASE_URL from environment.")

    # Locate seed.sql relative to this script first, then fallback to relative working dir
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, "seed.sql"),
        "seed/seed.sql",
        "seed.sql",
        "../seed/seed.sql"
    ]
    seed_path = next((p for p in candidates if os.path.exists(p)), None)
    if not seed_path:
        print("Error: seed.sql file could not be found.")
        sys.exit(1)

    print(f"Reading {seed_path}...")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Check if already seeded
        cur.execute("SELECT count(*) FROM applications;")
        count = cur.fetchone()[0]
        if count > 1000:
            print(f"Database already contains {count} applications. Skipping seed.")
            cur.close()
            conn.close()
            return

        with open(seed_path, "r", encoding="utf-8") as f:
            cur.execute(f.read())
        conn.commit()
        
        cur.execute("SELECT count(*) FROM applications;")
        new_count = cur.fetchone()[0]
        print(f"Success! Database now has {new_count} applications.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error executing seed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
