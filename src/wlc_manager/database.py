from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

LATEST_SCHEMA_VERSION = 4


class DatabaseError(RuntimeError):
    """Raised when a database operation cannot be completed safely."""


class PasswordState(StrEnum):
    GENERATED = "generated"
    MATERIALS_CREATED = "materials_created"
    NOTIFIED = "notified"
    APPLIED = "applied"
    EXPIRED = "expired"


class NotificationDeliveryStatus(StrEnum):
    SENDING = "sending"
    SENT = "sent"
    UNCERTAIN = "uncertain"


class NotificationClaimOutcome(StrEnum):
    CLAIMED = "claimed"
    ALREADY_SENT = "already_sent"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class PasswordRecord:
    id: int
    validity_month: str
    password: str = field(repr=False)
    dictionary_word: str
    state: PasswordState
    created_at: str
    run_id: str
    materials_created_at: str | None = None
    png_path: str | None = None
    pdf_path: str | None = None
    notified_at: str | None = None
    applied_at: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    validity_month: str
    message_id: str
    status: NotificationDeliveryStatus
    created_at: str
    sending_at: str
    sent_at: str | None
    last_error_code: str | None


class Database:
    def __init__(self, path: Path, *, busy_timeout_seconds: int = 30) -> None:
        self.path = path
        self.busy_timeout_seconds = busy_timeout_seconds

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_seconds * 1000}")
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                ) STRICT
                """
            )
            current = _current_version(connection)
            if current > LATEST_SCHEMA_VERSION:
                raise DatabaseError(
                    f"database schema {current} is newer than supported {LATEST_SCHEMA_VERSION}"
                )

            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                _apply_migration(connection, version, statements)

            return _current_version(connection)

    def current_version(self) -> int:
        if not self.path.exists():
            return 0
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            return _current_version(connection) if row else 0


class PasswordRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        validity_month: str,
        password: str,
        dictionary_word: str,
        run_id: str,
    ) -> PasswordRecord:
        created_at = _utc_now()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO password_records (
                        validity_month, password, dictionary_word, state, created_at, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validity_month,
                        password,
                        dictionary_word,
                        PasswordState.GENERATED.value,
                        created_at,
                        run_id,
                    ),
                )
                record_id = cursor.lastrowid
                connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(
                f"a password record already exists or is invalid for {validity_month}"
            ) from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"failed to create password record: {exc}") from exc

        if record_id is None:
            raise DatabaseError("database did not return a password record identifier")
        return PasswordRecord(
            id=record_id,
            validity_month=validity_month,
            password=password,
            dictionary_word=dictionary_word,
            state=PasswordState.GENERATED,
            created_at=created_at,
            run_id=run_id,
        )

    def get_by_month(self, validity_month: str) -> PasswordRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, validity_month, password, dictionary_word, state, created_at, run_id,
                       materials_created_at, png_path, pdf_path, notified_at, applied_at
                FROM password_records
                WHERE validity_month = ?
                """,
                (validity_month,),
            ).fetchone()
        return _password_record_from_row(row) if row is not None else None

    def mark_materials_created(
        self,
        validity_month: str,
        *,
        png_path: Path,
        pdf_path: Path,
    ) -> PasswordRecord:
        created_at = _utc_now()
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state FROM password_records WHERE validity_month = ?",
                    (validity_month,),
                ).fetchone()
                if row is None:
                    raise DatabaseError(f"password record does not exist for {validity_month}")

                state = PasswordState(str(row["state"]))
                if state is PasswordState.GENERATED:
                    connection.execute(
                        """
                        UPDATE password_records
                        SET state = ?, materials_created_at = ?, png_path = ?, pdf_path = ?
                        WHERE validity_month = ?
                        """,
                        (
                            PasswordState.MATERIALS_CREATED.value,
                            created_at,
                            str(png_path.resolve()),
                            str(pdf_path.resolve()),
                            validity_month,
                        ),
                    )
                elif state is PasswordState.MATERIALS_CREATED:
                    connection.execute(
                        """
                        UPDATE password_records
                        SET png_path = ?, pdf_path = ?
                        WHERE validity_month = ?
                        """,
                        (str(png_path.resolve()), str(pdf_path.resolve()), validity_month),
                    )
                else:
                    raise DatabaseError(
                        "cannot mark materials created from state "
                        f"{state.value} for {validity_month}"
                    )
                connection.execute("COMMIT")
            except DatabaseError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DatabaseError(
                    f"failed to mark materials created for {validity_month}: {exc}"
                ) from exc

        record = self.get_by_month(validity_month)
        if record is None:
            raise DatabaseError(f"password record disappeared for {validity_month}")
        return record

    def recent_dictionary_words(self, limit: int = 12) -> list[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT dictionary_word
                FROM password_records
                ORDER BY validity_month DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row["dictionary_word"]) for row in rows]

    def mark_applied(self, validity_month: str) -> PasswordRecord:
        applied_at = _utc_now()
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state FROM password_records WHERE validity_month = ?",
                    (validity_month,),
                ).fetchone()
                if row is None:
                    raise DatabaseError(f"password record does not exist for {validity_month}")
                state = PasswordState(str(row["state"]))
                if state is PasswordState.NOTIFIED:
                    connection.execute(
                        """
                        UPDATE password_records
                        SET state = ?, applied_at = ?
                        WHERE validity_month = ?
                        """,
                        (PasswordState.APPLIED.value, applied_at, validity_month),
                    )
                elif state is not PasswordState.APPLIED:
                    raise DatabaseError(f"cannot mark password applied from state {state.value}")
                connection.execute("COMMIT")
            except DatabaseError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DatabaseError(
                    f"failed to mark password applied for {validity_month}: {exc}"
                ) from exc

        record = self.get_by_month(validity_month)
        if record is None:
            raise DatabaseError(f"password record disappeared for {validity_month}")
        return record


class NotificationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def claim(
        self,
        *,
        validity_month: str,
        message_id: str,
        retry_uncertain: bool = False,
    ) -> NotificationClaimOutcome:
        now = _utc_now()
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                password_row = connection.execute(
                    "SELECT state FROM password_records WHERE validity_month = ?",
                    (validity_month,),
                ).fetchone()
                if password_row is None:
                    raise DatabaseError(f"password record does not exist for {validity_month}")

                row = connection.execute(
                    """
                    SELECT message_id, status
                    FROM notification_deliveries
                    WHERE validity_month = ?
                    """,
                    (validity_month,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO notification_deliveries (
                            validity_month, message_id, status, created_at, sending_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            validity_month,
                            message_id,
                            NotificationDeliveryStatus.SENDING.value,
                            now,
                            now,
                        ),
                    )
                    outcome = NotificationClaimOutcome.CLAIMED
                else:
                    if str(row["message_id"]) != message_id:
                        raise DatabaseError(
                            f"notification message identity changed for {validity_month}"
                        )
                    status = NotificationDeliveryStatus(str(row["status"]))
                    if status is NotificationDeliveryStatus.SENT:
                        outcome = NotificationClaimOutcome.ALREADY_SENT
                    elif status is NotificationDeliveryStatus.SENDING:
                        connection.execute(
                            """
                            UPDATE notification_deliveries
                            SET status = ?, last_error_code = ?
                            WHERE validity_month = ?
                            """,
                            (
                                NotificationDeliveryStatus.UNCERTAIN.value,
                                "interrupted_delivery",
                                validity_month,
                            ),
                        )
                        outcome = NotificationClaimOutcome.UNCERTAIN
                    elif retry_uncertain:
                        connection.execute(
                            """
                            UPDATE notification_deliveries
                            SET status = ?, sending_at = ?, sent_at = NULL,
                                last_error_code = NULL
                            WHERE validity_month = ?
                            """,
                            (
                                NotificationDeliveryStatus.SENDING.value,
                                now,
                                validity_month,
                            ),
                        )
                        outcome = NotificationClaimOutcome.CLAIMED
                    else:
                        outcome = NotificationClaimOutcome.UNCERTAIN
                connection.execute("COMMIT")
                return outcome
            except DatabaseError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DatabaseError(
                    f"failed to claim notification for {validity_month}: {exc}"
                ) from exc

    def mark_sent(self, *, validity_month: str, message_id: str) -> None:
        sent_at = _utc_now()
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                delivery = connection.execute(
                    """
                    SELECT message_id, status
                    FROM notification_deliveries
                    WHERE validity_month = ?
                    """,
                    (validity_month,),
                ).fetchone()
                if delivery is None or str(delivery["message_id"]) != message_id:
                    raise DatabaseError(f"notification claim is missing for {validity_month}")
                status = NotificationDeliveryStatus(str(delivery["status"]))
                if status is NotificationDeliveryStatus.SENT:
                    connection.execute("COMMIT")
                    return
                if status is not NotificationDeliveryStatus.SENDING:
                    raise DatabaseError(
                        f"notification is not in sending state for {validity_month}"
                    )

                password = connection.execute(
                    "SELECT state FROM password_records WHERE validity_month = ?",
                    (validity_month,),
                ).fetchone()
                if password is None:
                    raise DatabaseError(f"password record does not exist for {validity_month}")
                password_state = PasswordState(str(password["state"]))
                if password_state not in {
                    PasswordState.MATERIALS_CREATED,
                    PasswordState.NOTIFIED,
                }:
                    raise DatabaseError(
                        f"cannot mark notification sent from state {password_state.value}"
                    )

                connection.execute(
                    """
                    UPDATE notification_deliveries
                    SET status = ?, sent_at = ?, last_error_code = NULL
                    WHERE validity_month = ?
                    """,
                    (NotificationDeliveryStatus.SENT.value, sent_at, validity_month),
                )
                connection.execute(
                    """
                    UPDATE password_records
                    SET state = ?, notified_at = COALESCE(notified_at, ?)
                    WHERE validity_month = ?
                    """,
                    (PasswordState.NOTIFIED.value, sent_at, validity_month),
                )
                connection.execute("COMMIT")
            except DatabaseError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DatabaseError(
                    f"failed to mark notification sent for {validity_month}: {exc}"
                ) from exc

    def mark_uncertain(self, *, validity_month: str, error_code: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE notification_deliveries
                SET status = ?, last_error_code = ?
                WHERE validity_month = ? AND status = ?
                """,
                (
                    NotificationDeliveryStatus.UNCERTAIN.value,
                    error_code[:100],
                    validity_month,
                    NotificationDeliveryStatus.SENDING.value,
                ),
            )
        if cursor.rowcount != 1:
            raise DatabaseError(f"notification sending claim was not active for {validity_month}")

    def release_unsent_claim(self, *, validity_month: str, message_id: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM notification_deliveries
                WHERE validity_month = ? AND message_id = ? AND status = ?
                """,
                (
                    validity_month,
                    message_id,
                    NotificationDeliveryStatus.SENDING.value,
                ),
            )
        if cursor.rowcount != 1:
            raise DatabaseError(f"notification sending claim was not active for {validity_month}")

    def get(self, validity_month: str) -> NotificationDelivery | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT validity_month, message_id, status, created_at, sending_at,
                       sent_at, last_error_code
                FROM notification_deliveries
                WHERE validity_month = ?
                """,
                (validity_month,),
            ).fetchone()
        if row is None:
            return None
        return NotificationDelivery(
            validity_month=str(row["validity_month"]),
            message_id=str(row["message_id"]),
            status=NotificationDeliveryStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            sending_at=str(row["sending_at"]),
            sent_at=str(row["sent_at"]) if row["sent_at"] is not None else None,
            last_error_code=(
                str(row["last_error_code"]) if row["last_error_code"] is not None else None
            ),
        )


class LeaseLockRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def acquire(self, *, name: str, owner: str, ttl_seconds: int) -> bool:
        now = time.time()
        expires_at = now + ttl_seconds
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO application_locks(name, owner, acquired_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        owner = excluded.owner,
                        acquired_at = excluded.acquired_at,
                        expires_at = excluded.expires_at
                    WHERE application_locks.expires_at <= ?
                       OR application_locks.owner = excluded.owner
                    """,
                    (name, owner, now, expires_at, now),
                )
                acquired = cursor.rowcount == 1
                connection.execute("COMMIT")
                return acquired
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DatabaseError(f"failed to acquire application lock {name}: {exc}") from exc

    def release(self, *, name: str, owner: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM application_locks WHERE name = ? AND owner = ?",
                (name, owner),
            )
        return cursor.rowcount == 1


class HeartbeatRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def beat(self, service_name: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO service_heartbeats(service_name, updated_at)
                VALUES (?, ?)
                ON CONFLICT(service_name) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (service_name, time.time()),
            )

    def age_seconds(self, service_name: str) -> float | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM service_heartbeats WHERE service_name = ?",
                (service_name,),
            ).fetchone()
        if row is None:
            return None
        return max(0.0, time.time() - float(row["updated_at"]))


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE password_records (
                id INTEGER PRIMARY KEY,
                validity_month TEXT NOT NULL UNIQUE
                    CHECK (
                        validity_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
                        AND CAST(substr(validity_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
                    ),
                password TEXT NOT NULL,
                dictionary_word TEXT NOT NULL CHECK (dictionary_word <> ''),
                state TEXT NOT NULL CHECK (
                    state IN ('generated', 'materials_created', 'notified', 'applied', 'expired')
                ),
                created_at TEXT NOT NULL,
                materials_created_at TEXT,
                notified_at TEXT,
                applied_at TEXT,
                run_id TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE INDEX idx_password_records_recent
            ON password_records(validity_month DESC)
            """,
            """
            CREATE TABLE workflow_runs (
                run_id TEXT PRIMARY KEY,
                process_name TEXT NOT NULL,
                trigger TEXT NOT NULL CHECK (trigger IN ('scheduler', 'cli')),
                status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error_code TEXT,
                error_message TEXT
            ) STRICT
            """,
            """
            CREATE TABLE workflow_steps (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                step_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error_code TEXT,
                error_message TEXT
            ) STRICT
            """,
            """
            CREATE INDEX idx_workflow_steps_run_id ON workflow_steps(run_id)
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE application_locks (
                name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            ) STRICT
            """,
            """
            CREATE INDEX idx_application_locks_expires_at
            ON application_locks(expires_at)
            """,
            """
            CREATE TABLE service_heartbeats (
                service_name TEXT PRIMARY KEY,
                updated_at REAL NOT NULL
            ) STRICT
            """,
        ),
    ),
    (
        3,
        (
            "ALTER TABLE password_records ADD COLUMN png_path TEXT",
            "ALTER TABLE password_records ADD COLUMN pdf_path TEXT",
        ),
    ),
    (
        4,
        (
            """
            CREATE TABLE notification_deliveries (
                validity_month TEXT PRIMARY KEY
                    REFERENCES password_records(validity_month) ON DELETE CASCADE,
                message_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'uncertain')),
                created_at TEXT NOT NULL,
                sending_at TEXT NOT NULL,
                sent_at TEXT,
                last_error_code TEXT
            ) STRICT
            """,
        ),
    ),
)


def _apply_migration(
    connection: sqlite3.Connection, version: int, statements: tuple[str, ...]
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, _utc_now()),
        )
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise DatabaseError(f"failed to apply database migration {version}: {exc}") from exc


def _current_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"])


def _password_record_from_row(row: sqlite3.Row) -> PasswordRecord:
    return PasswordRecord(
        id=int(row["id"]),
        validity_month=str(row["validity_month"]),
        password=str(row["password"]),
        dictionary_word=str(row["dictionary_word"]),
        state=PasswordState(str(row["state"])),
        created_at=str(row["created_at"]),
        run_id=str(row["run_id"]),
        materials_created_at=(
            str(row["materials_created_at"]) if row["materials_created_at"] is not None else None
        ),
        png_path=str(row["png_path"]) if row["png_path"] is not None else None,
        pdf_path=str(row["pdf_path"]) if row["pdf_path"] is not None else None,
        notified_at=str(row["notified_at"]) if row["notified_at"] is not None else None,
        applied_at=str(row["applied_at"]) if row["applied_at"] is not None else None,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
