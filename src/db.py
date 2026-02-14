from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
import uuid

import asyncpg

logger = logging.getLogger("pokestrator.db")

_POOL: asyncpg.Pool | None = None
_INITIALIZED = False
_INIT_LOCK = asyncio.Lock()


@dataclass
class Subagent:
    id: str
    name: str
    description: str
    system_prompt: str


def _resolve_database_url() -> str:
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
            return value
    raise RuntimeError(
        "No database URL configured. Set DATABASE_URL or one of POSTGRES_URL, POSTGRESQL_URL, RENDER_DATABASE_URL, DB_URL"
    )


def _normalize_database_url(value: str) -> str:
    value = value.strip()
    if value.startswith("postgres://"):
        # asyncpg accepts postgresql://, not all drivers handle postgres:// uniformly.
        return f"postgresql://{value[len('postgres://'):]}"
    return value


def _to_subagent(row: Any) -> Subagent:
    return Subagent(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"],
        system_prompt=row["system_prompt"],
    )


async def get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        await init_db()
    if _POOL is None:
        raise RuntimeError("PostgreSQL pool is unavailable")
    return _POOL


async def init_db() -> None:
    global _POOL
    global _INITIALIZED

    if _INITIALIZED and _POOL is not None:
        return

    async with _INIT_LOCK:
        if _INITIALIZED and _POOL is not None:
            return

        database_url = _normalize_database_url(_resolve_database_url())
        min_size = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
        max_size = int(os.getenv("DB_POOL_MAX_SIZE", "5"))

        _POOL = await asyncpg.create_pool(
            database_url,
            min_size=min_size,
            max_size=max_size,
        )

        async with _POOL.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subagents (
                    id UUID PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_subagents_lower_name
                ON subagents (LOWER(name))
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subagents_created_at
                ON subagents (created_at DESC)
                """
            )

        _INITIALIZED = True
        logger.info("PostgreSQL initialized and subagents table is ready")


async def close_db() -> None:
    global _POOL
    global _INITIALIZED
    if _POOL is None:
        return
    await _POOL.close()
    _POOL = None
    _INITIALIZED = False


async def get_all_subagents() -> list[Subagent]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, system_prompt
            FROM subagents
            ORDER BY created_at ASC
            """
        )
        return [_to_subagent(row) for row in rows]


async def get_subagent_by_id(subagent_id: str) -> Subagent | None:
    try:
        uuid.UUID(subagent_id)
    except (TypeError, ValueError):
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, description, system_prompt
            FROM subagents
            WHERE id = $1
            """,
            subagent_id,
        )
        return _to_subagent(row) if row else None


async def get_subagent_by_name(name: str) -> Subagent | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, description, system_prompt
            FROM subagents
            WHERE LOWER(name) = LOWER($1)
            LIMIT 1
            """,
            name,
        )
        return _to_subagent(row) if row else None


async def insert_subagent(name: str, description: str, system_prompt: str) -> Subagent:
    if not name or not description or not system_prompt:
        raise ValueError("name, description, and system_prompt are required")

    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("name cannot be empty")

    subagent_id = str(uuid.uuid4())
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id, name, description, system_prompt
            FROM subagents
            WHERE LOWER(name) = LOWER($1)
            LIMIT 1
            """,
            normalized_name,
        )
        if existing:
            return _to_subagent(existing)

        row = await conn.fetchrow(
            """
            INSERT INTO subagents (id, name, description, system_prompt)
            VALUES ($1, $2, $3, $4)
            RETURNING id, name, description, system_prompt
            """,
            subagent_id,
            normalized_name,
            description.strip(),
            system_prompt.strip(),
        )
        return _to_subagent(row)
