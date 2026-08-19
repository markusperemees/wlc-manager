from pathlib import Path

import pytest

from wlc_manager.database import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    Database,
    DatabaseError,
    HeartbeatRepository,
    LeaseLockRepository,
    NotificationClaimOutcome,
    NotificationDeliveryStatus,
    NotificationRepository,
    PasswordRepository,
    PasswordState,
)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "manager.db")

    assert database.current_version() == 0
    assert database.migrate() == LATEST_SCHEMA_VERSION
    assert database.migrate() == LATEST_SCHEMA_VERSION
    assert database.current_version() == LATEST_SCHEMA_VERSION


def test_existing_version_one_database_upgrades_to_latest(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            ) STRICT
            """
        )
        for statement in MIGRATIONS[0][1]:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-08-19')"
        )

    assert database.current_version() == 1
    assert database.migrate() == LATEST_SCHEMA_VERSION
    with database.connect() as connection:
        lock_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'application_locks'"
        ).fetchone()
    assert lock_table is not None


def test_password_repository_returns_recent_words(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    repository = PasswordRepository(database)

    first = repository.create(
        validity_month="2026-08",
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    repository.create(
        validity_month="2026-09",
        password="markus456pear",
        dictionary_word="pear",
        run_id="run-2",
    )

    assert first.state is PasswordState.GENERATED
    assert repository.recent_dictionary_words(limit=2) == ["pear", "apple"]


def test_only_one_password_can_exist_for_a_month(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    repository = PasswordRepository(database)
    values = {
        "validity_month": "2026-08",
        "password": "markus123apple",
        "dictionary_word": "apple",
        "run_id": "run-1",
    }
    repository.create(**values)

    with pytest.raises(DatabaseError, match="already exists"):
        repository.create(**values)


def test_material_files_are_recorded_without_exposing_password(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    repository = PasswordRepository(database)
    repository.create(
        validity_month="2026-08",
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    png_path = tmp_path / "artifacts" / "wifi-2026-08.png"
    pdf_path = tmp_path / "artifacts" / "wifi-2026-08.pdf"

    first = repository.mark_materials_created("2026-08", png_path=png_path, pdf_path=pdf_path)
    second = repository.mark_materials_created("2026-08", png_path=png_path, pdf_path=pdf_path)

    assert first.state is PasswordState.MATERIALS_CREATED
    assert first.materials_created_at is not None
    assert first.png_path == str(png_path.resolve())
    assert first.pdf_path == str(pdf_path.resolve())
    assert second.materials_created_at == first.materials_created_at
    assert "markus123apple" not in repr(first)


def test_notification_delivery_is_idempotent_and_marks_password_notified(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    passwords = PasswordRepository(database)
    passwords.create(
        validity_month="2026-08",
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    passwords.mark_materials_created(
        "2026-08",
        png_path=tmp_path / "wifi-2026-08.png",
        pdf_path=tmp_path / "wifi-2026-08.pdf",
    )
    notifications = NotificationRepository(database)
    message_id = "<wifi-2026-08@example.test>"

    assert notifications.claim(validity_month="2026-08", message_id=message_id) is (
        NotificationClaimOutcome.CLAIMED
    )
    notifications.mark_sent(validity_month="2026-08", message_id=message_id)
    assert notifications.claim(validity_month="2026-08", message_id=message_id) is (
        NotificationClaimOutcome.ALREADY_SENT
    )

    delivery = notifications.get("2026-08")
    password = passwords.get_by_month("2026-08")
    assert delivery is not None
    assert delivery.status is NotificationDeliveryStatus.SENT
    assert delivery.sent_at is not None
    assert password is not None
    assert password.state is PasswordState.NOTIFIED
    assert password.notified_at is not None


def test_interrupted_notification_requires_explicit_uncertain_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    passwords = PasswordRepository(database)
    passwords.create(
        validity_month="2026-08",
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    notifications = NotificationRepository(database)
    message_id = "<wifi-2026-08@example.test>"

    assert notifications.claim(validity_month="2026-08", message_id=message_id) is (
        NotificationClaimOutcome.CLAIMED
    )
    assert notifications.claim(validity_month="2026-08", message_id=message_id) is (
        NotificationClaimOutcome.UNCERTAIN
    )
    assert notifications.claim(validity_month="2026-08", message_id=message_id) is (
        NotificationClaimOutcome.UNCERTAIN
    )
    assert (
        notifications.claim(
            validity_month="2026-08",
            message_id=message_id,
            retry_uncertain=True,
        )
        is NotificationClaimOutcome.CLAIMED
    )


def test_applied_state_is_recorded_idempotently_after_notification(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    passwords = PasswordRepository(database)
    passwords.create(
        validity_month="2026-08",
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    passwords.mark_materials_created(
        "2026-08",
        png_path=tmp_path / "wifi-2026-08.png",
        pdf_path=tmp_path / "wifi-2026-08.pdf",
    )
    notifications = NotificationRepository(database)
    message_id = "<wifi-2026-08@example.test>"
    notifications.claim(validity_month="2026-08", message_id=message_id)
    notifications.mark_sent(validity_month="2026-08", message_id=message_id)

    first = passwords.mark_applied("2026-08")
    second = passwords.mark_applied("2026-08")

    assert first.state is PasswordState.APPLIED
    assert first.applied_at is not None
    assert second.applied_at == first.applied_at


def test_lease_lock_prevents_other_owner_until_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_time = 1000.0
    monkeypatch.setattr("wlc_manager.database.time.time", lambda: current_time)
    database = Database(tmp_path / "manager.db")
    database.migrate()
    locks = LeaseLockRepository(database)

    assert locks.acquire(name="monthly", owner="run-1", ttl_seconds=60)
    assert not locks.acquire(name="monthly", owner="run-2", ttl_seconds=60)
    assert not locks.release(name="monthly", owner="run-2")

    current_time = 1061.0
    assert locks.acquire(name="monthly", owner="run-2", ttl_seconds=60)
    assert locks.release(name="monthly", owner="run-2")


def test_same_lock_owner_can_renew_lease(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    locks = LeaseLockRepository(database)

    assert locks.acquire(name="monthly", owner="run-1", ttl_seconds=60)
    assert locks.acquire(name="monthly", owner="run-1", ttl_seconds=120)
    assert locks.release(name="monthly", owner="run-1")


def test_scheduler_heartbeat_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current_time = 2000.0
    monkeypatch.setattr("wlc_manager.database.time.time", lambda: current_time)
    database = Database(tmp_path / "manager.db")
    database.migrate()
    heartbeats = HeartbeatRepository(database)

    assert heartbeats.age_seconds("scheduler") is None
    heartbeats.beat("scheduler")
    current_time = 2012.5
    assert heartbeats.age_seconds("scheduler") == 12.5
