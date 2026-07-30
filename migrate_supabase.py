#!/usr/bin/env python3
"""
Supabase Migration Script for Campus Whispers
Run this once to set up all tables in Supabase Postgres.

Usage:
    DATABASE_URL="postgresql://..." python migrate_supabase.py
"""
import os
import sys

def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: Set DATABASE_URL environment variable")
        print("Example: export DATABASE_URL='postgresql://postgres.xxx:PASS@host:6543/postgres'")
        sys.exit(1)

    try:
        import psycopg2
    except ImportError:
        print("Installing psycopg2...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary"])
        import psycopg2

    print(f"Connecting to Supabase...")
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    cur = conn.cursor()

    # Read and execute the schema file
    schema_path = os.path.join(os.path.dirname(__file__), "supabase_schema.sql")
    with open(schema_path, "r") as f:
        schema = f.read()

    # Split by semicolon and execute each statement
    statements = [s.strip() for s in schema.split(";") if s.strip()]
    
    for i, stmt in enumerate(statements):
        try:
            cur.execute(stmt)
            print(f"  [{i+1}/{len(statements)}] OK")
        except Exception as e:
            if "already exists" in str(e) or "duplicate key" in str(e):
                print(f"  [{i+1}/{len(statements)}] SKIP (already exists)")
            else:
                print(f"  [{i+1}/{len(statements)}] ERROR: {e}")

    cur.close()
    conn.close()
    print("\n✅ Migration complete!")

if __name__ == "__main__":
    main()