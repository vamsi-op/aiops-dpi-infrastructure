"""Async TimescaleDB access.

Design choice: SQLAlchemy Core with explicit SQL rather than a full ORM. Most of the
work here is time-series aggregation (`time_bucket`, continuous aggregates, window
functions) which reads far better as SQL, and the ORM adds nothing for append-only
metric tables.

Usage:
    from aiops_common.db import Database

    db = Database()
    await db.connect()
    rows = await db.fetch_all(
        "SELECT service, avg(tps) AS tps FROM service_metrics "
        "WHERE timestamp > now() - interval '1 hour' GROUP BY service"
    )
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Self

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import PostgresSettings, get_settings
from .errors import StorageError
from .log import get_logger

log = get_logger(__name__)

# Guard for the few places where an object name must be interpolated into SQL
# because PostgreSQL will not accept it as a bind parameter.
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Database:
    """Thin async wrapper around a SQLAlchemy engine pointed at TimescaleDB."""

    def __init__(self, settings: PostgresSettings | None = None) -> None:
        self._settings = settings or get_settings().postgres
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> Self:
        """Create the engine and verify the database answers."""
        if self._engine is None:
            self._engine = create_async_engine(
                self._settings.async_dsn,
                pool_size=self._settings.pool_size,
                max_overflow=self._settings.max_overflow,
                pool_pre_ping=True,
                pool_recycle=1_800,
                echo=self._settings.echo_sql,
                connect_args={
                    "timeout": 10,
                    "server_settings": {
                        "application_name": get_settings().service_name,
                        "statement_timeout": str(self._settings.statement_timeout_ms),
                    },
                },
            )
            self._session_factory = async_sessionmaker(
                self._engine, expire_on_commit=False, class_=AsyncSession
            )
        await self.ping()
        log.info(
            "db.connected",
            host=self._settings.host,
            port=self._settings.port,
            database=self._settings.database,
        )
        return self

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._session_factory = None

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise StorageError("Database used before connect(); call `await db.connect()`")
        return self._engine

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as exc:
            raise StorageError(
                "Database ping failed",
                host=self._settings.host,
                database=self._settings.database,
            ) from exc

    # -- connections & sessions --------------------------------------------
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """Transactional connection. Commits on success, rolls back on error."""
        async with self.engine.begin() as conn:
            yield conn

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._session_factory is None:
            raise StorageError("Database used before connect(); call `await db.connect()`")
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # -- query helpers -----------------------------------------------------
    async def fetch_all(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(text(sql), params or {})
                return [dict(row) for row in result.mappings().all()]
        except SQLAlchemyError as exc:
            raise StorageError("Query failed", sql=_truncate(sql)) from exc

    async def fetch_one(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def fetch_value(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        row = await self.fetch_one(sql, params)
        return next(iter(row.values())) if row else None

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        """Run a write statement in its own transaction; returns affected row count."""
        try:
            async with self.connection() as conn:
                result = await conn.execute(text(sql), params or {})
                return result.rowcount or 0
        except SQLAlchemyError as exc:
            raise StorageError("Statement failed", sql=_truncate(sql)) from exc

    async def call_procedure(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        """Run a statement that must NOT be inside a transaction block.

        Several TimescaleDB routines refuse to run in a transaction, notably
        `CALL refresh_continuous_aggregate(...)` and `CALL run_job(...)`. `execute()`
        always opens one, so those need this autocommit path instead.
        """
        try:
            async with self.engine.connect() as conn:
                autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await autocommit.execute(text(sql), params or {})
        except SQLAlchemyError as exc:
            raise StorageError("Procedure call failed", sql=_truncate(sql)) from exc

    async def refresh_continuous_aggregate(
        self, view: str, start: datetime | None = None, end: datetime | None = None
    ) -> None:
        """Force a continuous aggregate to materialise a window immediately.

        Policies refresh on a schedule; tests and backfill jobs cannot wait for that.
        `None` for either bound means "open ended" in TimescaleDB.

        The explicit `CAST(... AS timestamptz)` is required, not cosmetic: the
        procedure's window arguments are declared polymorphic, so asyncpg cannot infer
        a type for a plain bind parameter and raises IndeterminateDatatypeError.
        """
        if not _SAFE_IDENTIFIER.fullmatch(view):
            # The view name is interpolated (it cannot be a bind parameter here), so
            # it is validated rather than trusted.
            raise StorageError("Invalid continuous aggregate name", view=view)
        await self.call_procedure(
            f"CALL refresh_continuous_aggregate('{view}', "
            "CAST(:start AS timestamptz), CAST(:end AS timestamptz))",
            {"start": start, "end": end},
        )

    async def execute_many(self, sql: str, param_sets: Sequence[Mapping[str, Any]]) -> int:
        """Batch write — one round trip, one transaction. Empty input is a no-op.

        Returns the number of rows written. asyncpg's `executemany` reports no row
        count (SQLAlchemy surfaces that as -1), so in that case we return the number of
        parameter sets submitted: the transaction either committed all of them or it
        raised, so the two are equivalent.
        """
        if not param_sets:
            return 0
        try:
            async with self.connection() as conn:
                result = await conn.execute(text(sql), list(param_sets))
                rowcount = result.rowcount
                return len(param_sets) if rowcount is None or rowcount < 0 else rowcount
        except SQLAlchemyError as exc:
            raise StorageError(
                "Batch statement failed", sql=_truncate(sql), batch_size=len(param_sets)
            ) from exc

    # -- introspection -----------------------------------------------------
    async def table_exists(self, table: str, schema: str = "public") -> bool:
        return bool(
            await self.fetch_value(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = :table",
                {"schema": schema, "table": table},
            )
        )

    async def timescale_version(self) -> str | None:
        """Returns the TimescaleDB extension version, or None if it is not installed."""
        return await self.fetch_value(
            "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
        )


def _truncate(sql: str, limit: int = 160) -> str:
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}..."


# A module-level instance is convenient for single-purpose services; services that
# need explicit lifecycle control should construct their own `Database()`.
_default_db: Database | None = None


def get_database() -> Database:
    """Return the process-wide `Database` (not yet connected on first call)."""
    global _default_db
    if _default_db is None:
        _default_db = Database()
    return _default_db
