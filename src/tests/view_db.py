#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
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


async def view_subagents() -> None:
    load_dotenv_if_present()
    database_url = normalize_database_url(resolve_database_url())
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            """
            SELECT id, name, description, system_prompt, created_at, updated_at
            FROM subagents
            ORDER BY created_at ASC
            """
        )
    finally:
        await conn.close()

    payload = [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "description": row["description"],
            "system_prompt": row["system_prompt"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]

    print(f"subagents count: {len(payload)}")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    asyncio.run(view_subagents())
