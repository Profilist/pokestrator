from __future__ import annotations

import asyncio
import logging
import os
import re
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
    status: str = "ready"
    required_provider: str | None = None


@dataclass
class ProviderApiKey:
    provider: str
    api_key: str


def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    return re.sub(r"[^a-z0-9_]+", "_", value).strip("_")


def _normalize_subagent_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"ready", "needs_api_key"}:
        raise ValueError("status must be one of: ready, needs_api_key")
    return normalized



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
    status = row["status"] if "status" in row and row["status"] else "ready"
    required_provider = row["required_provider"] if "required_provider" in row else None
    return Subagent(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"],
        system_prompt=row["system_prompt"],
        status=status,
        required_provider=required_provider,
    )


def _to_provider_api_key(row: Any) -> ProviderApiKey:
    return ProviderApiKey(
        provider=row["provider"],
        api_key=row["api_key"],
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
                    status VARCHAR(32) NOT NULL DEFAULT 'ready',
                    required_provider VARCHAR(128),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                """
                ALTER TABLE subagents
                ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'ready'
                """
            )
            await conn.execute(
                """
                ALTER TABLE subagents
                ADD COLUMN IF NOT EXISTS required_provider VARCHAR(128)
                """
            )
            await conn.execute(
                """
                UPDATE subagents
                SET status = 'ready'
                WHERE status IS NULL
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
                CREATE INDEX IF NOT EXISTS idx_subagents_required_provider
                ON subagents (LOWER(required_provider))
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subagents_status
                ON subagents (status)
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subagents_created_at
                ON subagents (created_at DESC)
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    provider VARCHAR(128) PRIMARY KEY,
                    api_key TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
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
            SELECT id, name, description, system_prompt, status, required_provider
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
            SELECT id, name, description, system_prompt, status, required_provider
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
            SELECT id, name, description, system_prompt, status, required_provider
            FROM subagents
            WHERE LOWER(name) = LOWER($1)
            LIMIT 1
            """,
            name,
        )
        return _to_subagent(row) if row else None


async def insert_subagent(
    name: str,
    description: str,
    system_prompt: str,
    *,
    status: str = "ready",
    required_provider: str | None = None,
) -> Subagent:
    if not name or not description or not system_prompt:
        raise ValueError("name, description, and system_prompt are required")

    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("name cannot be empty")

    normalized_status = _normalize_subagent_status(status)
    normalized_provider = _normalize_provider(required_provider) if required_provider else None
    if required_provider and not normalized_provider:
        raise ValueError("required_provider cannot be empty")

    subagent_id = str(uuid.uuid4())
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id, name, description, system_prompt, status, required_provider
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
            INSERT INTO subagents (id, name, description, system_prompt, status, required_provider)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, name, description, system_prompt, status, required_provider
            """,
            subagent_id,
            normalized_name,
            description.strip(),
            system_prompt.strip(),
            normalized_status,
            normalized_provider,
        )
        return _to_subagent(row)


async def update_subagent_auth(
    subagent_id: str,
    *,
    status: str,
    required_provider: str | None,
) -> Subagent | None:
    try:
        uuid.UUID(subagent_id)
    except (TypeError, ValueError):
        return None

    normalized_status = _normalize_subagent_status(status)
    normalized_provider = _normalize_provider(required_provider) if required_provider else None
    if required_provider and not normalized_provider:
        raise ValueError("required_provider cannot be empty")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE subagents
            SET status = $2,
                required_provider = $3,
                updated_at = NOW()
            WHERE id = $1
            RETURNING id, name, description, system_prompt, status, required_provider
            """,
            subagent_id,
            normalized_status,
            normalized_provider,
        )
        return _to_subagent(row) if row else None


async def get_api_key(provider: str) -> str | None:
    normalized_provider = _normalize_provider(provider)
    if not normalized_provider:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT provider, api_key
            FROM api_keys
            WHERE provider = $1
            LIMIT 1
            """,
            normalized_provider,
        )
        return row["api_key"] if row else None


async def upsert_api_key(provider: str, api_key: str) -> ProviderApiKey:
    normalized_provider = _normalize_provider(provider)
    if not normalized_provider:
        raise ValueError("provider is required")

    normalized_api_key = str(api_key or "").strip()
    if not normalized_api_key:
        raise ValueError("api_key is required")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO api_keys (provider, api_key)
            VALUES ($1, $2)
            ON CONFLICT (provider)
            DO UPDATE SET api_key = EXCLUDED.api_key, updated_at = NOW()
            RETURNING provider, api_key
            """,
            normalized_provider,
            normalized_api_key,
        )
        return _to_provider_api_key(row)


async def mark_subagents_ready_for_provider(provider: str) -> int:
    normalized_provider = _normalize_provider(provider)
    if not normalized_provider:
        return 0

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE subagents
            SET status = 'ready',
                updated_at = NOW()
            WHERE LOWER(required_provider) = LOWER($1)
              AND status <> 'ready'
            RETURNING id
            """,
            normalized_provider,
        )
        return len(rows)
