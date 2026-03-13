#!/usr/bin/env python3
"""
One-time migration: SQLite (leads.db) → PostgreSQL (venue_scrapper).

Usage:
    DATABASE_URL=postgresql://venue_scrapper:<pw>@127.0.0.1:5432/venue_scrapper \
        python3 migrate_to_pg.py leads.db
"""

import os
import sys
import sqlite3
import psycopg2
from pathlib import Path

PG_SCHEMA = Path(__file__).with_name("schema_pg.sql").read_text()

TABLES = ["config", "sync_meta", "eventective_leads", "eventective_lead_activities"]


def migrate(sqlite_path: str, pg_url: str):
    # ── Connect to both databases ─────────────────────────────────────────
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(pg_url)
    dst.autocommit = False

    try:
        cur_pg = dst.cursor()

        # ── Create schema ─────────────────────────────────────────────────
        print("[migrate] creating PostgreSQL schema...")
        cur_pg.execute(PG_SCHEMA)
        dst.commit()

        # ── Migrate each table ────────────────────────────────────────────
        for table in TABLES:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"[migrate] {table}: 0 rows (skipped)")
                continue

            cols = rows[0].keys()
            # For eventective_lead_activities, skip the 'id' column (SERIAL in PG)
            if table == "eventective_lead_activities":
                cols = [c for c in cols if c != "id"]

            # Double-quote CamelCase column names for PostgreSQL case preservation
            # Lowercase-only columns (name, key, value, id, fub_*) don't need quoting
            def _q(c):
                if c != c.lower():
                    return f'"{c}"'
                return c

            col_list = ", ".join(_q(c) for c in cols)
            placeholders = ", ".join(["%s"] * len(cols))

            if table == "config":
                # ON CONFLICT DO UPDATE for config (upsert)
                upsert = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value"
                )
            elif table == "sync_meta":
                upsert = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                )
            elif table == "eventective_leads":
                # ON CONFLICT DO NOTHING for leads (keep existing)
                upsert = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f'ON CONFLICT ("EventId") DO NOTHING'
                )
            elif table == "eventective_lead_activities":
                upsert = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f'ON CONFLICT ("EventId", "DateTime", "ActivityTypeCd", "ResponseNum") DO NOTHING'
                )
            else:
                upsert = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

            batch = []
            for row in rows:
                if table == "eventective_lead_activities":
                    vals = tuple(row[c] for c in cols)
                else:
                    vals = tuple(row)
                batch.append(vals)

            # Insert in batches of 500
            BATCH_SIZE = 500
            inserted = 0
            for i in range(0, len(batch), BATCH_SIZE):
                chunk = batch[i : i + BATCH_SIZE]
                cur_pg.executemany(upsert, chunk)
                inserted += len(chunk)

            dst.commit()

            # Verify count
            cur_pg.execute(f"SELECT count(*) FROM {table}")
            pg_count = cur_pg.fetchone()[0]
            src_count = len(rows)
            status = "OK" if pg_count >= src_count else "MISMATCH"
            print(f"[migrate] {table}: {src_count} SQLite → {pg_count} PG  [{status}]")

        # ── Reset sequence for eventective_lead_activities ────────────────
        cur_pg.execute(
            "SELECT setval('eventective_lead_activities_id_seq', "
            "(SELECT COALESCE(MAX(id), 0) FROM eventective_lead_activities))"
        )
        dst.commit()

        # ── Summary ───────────────────────────────────────────────────────
        print("\n[migrate] === Summary ===")
        for table in TABLES:
            cur_pg.execute(f"SELECT count(*) FROM {table}")
            print(f"  {table}: {cur_pg.fetchone()[0]} rows")
        print("[migrate] Done.")

    except Exception:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <sqlite_db_path>")
        print("  Requires DATABASE_URL env var pointing to PostgreSQL")
        sys.exit(1)

    sqlite_path = sys.argv[1]
    pg_url = os.environ.get("DATABASE_URL")
    if not pg_url:
        print("ERROR: DATABASE_URL environment variable not set")
        sys.exit(1)

    if not Path(sqlite_path).exists():
        print(f"ERROR: SQLite file not found: {sqlite_path}")
        sys.exit(1)

    migrate(sqlite_path, pg_url)
