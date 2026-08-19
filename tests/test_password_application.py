from datetime import date
from pathlib import Path

import pytest

from wlc_manager.database import (
    Database,
    NotificationRepository,
    PasswordRepository,
    PasswordState,
)
from wlc_manager.password_application import (
    PasswordApplicationError,
    PasswordApplicationOutcome,
    PasswordApplicationService,
)
from wlc_manager.scheduling import YearMonth
from wlc_manager.wlc import PskUpdateResult, WlcOperationError


class RecordingController:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.passwords: list[str] = []

    def update_psk(self, password: str) -> PskUpdateResult:
        self.passwords.append(password)
        if self.fail:
            raise WlcOperationError("simulated WLC rejection")
        return PskUpdateResult(wlan_id=1, wlan_was_enabled=True, wlan_is_enabled=True)


def _repository(tmp_path: Path, *, notified: bool = True) -> PasswordRepository:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    repository = PasswordRepository(database)
    repository.create(
        validity_month="2026-09",
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    if notified:
        repository.mark_materials_created(
            "2026-09",
            png_path=tmp_path / "wifi-2026-09.png",
            pdf_path=tmp_path / "wifi-2026-09.pdf",
        )
        notifications = NotificationRepository(database)
        message_id = "<wifi-2026-09@example.test>"
        notifications.claim(validity_month="2026-09", message_id=message_id)
        notifications.mark_sent(validity_month="2026-09", message_id=message_id)
    return repository


def test_due_password_is_applied_once_and_persisted(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RecordingController()
    service = PasswordApplicationService(repository, lambda: controller)

    first = service.apply(YearMonth(2026, 9), today=date(2026, 9, 1))
    second = service.apply(YearMonth(2026, 9), today=date(2026, 9, 1))

    assert first.outcome is PasswordApplicationOutcome.APPLIED
    assert first.wlan_was_enabled is True
    assert second.outcome is PasswordApplicationOutcome.ALREADY_APPLIED
    assert controller.passwords == ["markus123apple"]
    record = repository.get_by_month("2026-09")
    assert record is not None and record.state is PasswordState.APPLIED
    assert record.applied_at is not None


def test_future_password_is_not_applied_without_explicit_override(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller_created = False

    def controller_factory() -> RecordingController:
        nonlocal controller_created
        controller_created = True
        return RecordingController()

    service = PasswordApplicationService(repository, controller_factory)

    result = service.apply(YearMonth(2026, 9), today=date(2026, 8, 31))

    assert result.outcome is PasswordApplicationOutcome.NOT_DUE
    assert not controller_created


def test_early_override_still_requires_notification(tmp_path: Path) -> None:
    repository = _repository(tmp_path, notified=False)
    service = PasswordApplicationService(repository, RecordingController)

    with pytest.raises(PasswordApplicationError, match="requires notified state"):
        service.apply(
            YearMonth(2026, 9),
            today=date(2026, 8, 31),
            allow_early=True,
        )


def test_automatic_reconciliation_waits_until_notification_is_complete(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, notified=False)
    controller = RecordingController()
    service = PasswordApplicationService(repository, lambda: controller)

    result = service.reconcile(today=date(2026, 9, 1))

    assert result.outcome is PasswordApplicationOutcome.NOT_READY
    assert controller.passwords == []


def test_wlc_failure_does_not_mark_password_applied_or_leak_it(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RecordingController(fail=True)
    service = PasswordApplicationService(repository, lambda: controller)

    with pytest.raises(WlcOperationError) as error:
        service.apply(YearMonth(2026, 9), today=date(2026, 9, 1))

    record = repository.get_by_month("2026-09")
    assert record is not None and record.state is PasswordState.NOTIFIED
    assert "markus123apple" not in str(error.value)
