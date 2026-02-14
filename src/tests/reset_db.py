#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_dotenv_if_present(path: str = ".env") -> None:
    env_path = PROJECT_ROOT / path
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def resolve_database_url() -> str:
    candidates = (
        "DATABASE_URL",
        "POSTGRES_URL",
        "POSTGRESQL_URL",
        "RENDER_DATABASE_URL",
        "DB_URL",
    )
    for key in candidates:
        value = os.getenv(key)
        if value:
            return value.strip()
    raise RuntimeError(
        "No database URL configured. Set DATABASE_URL (or POSTGRES_URL/POSTGRESQL_URL/RENDER_DATABASE_URL/DB_URL)."
    )


def normalize_database_url(value: str) -> str:
    if value.startswith("postgres://"):
        return f"postgresql://{value[len('postgres://'):]}"
    return value


async def reset_db() -> None:
    load_dotenv_if_present()
    database_url = normalize_database_url(resolve_database_url())
    conn = await asyncpg.connect(database_url)
    try:
        tables = await conn.fetch(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename
            """
        )

        if not tables:
            print("No tables found in public schema. Nothing to reset.")
            return

        await conn.execute(
            """
            DO $$
            DECLARE r RECORD;
            BEGIN
                FOR r IN
                    SELECT schemaname, tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                LOOP
                    EXECUTE format(
                        'TRUNCATE TABLE %I.%I RESTART IDENTITY CASCADE',
                        r.schemaname,
                        r.tablename
                    );
                END LOOP;
            END $$;
            """
        )

        table_names = ", ".join(row["tablename"] for row in tables)
        print(f"Reset complete. Truncated tables: {table_names}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(reset_db())
