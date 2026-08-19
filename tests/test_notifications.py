from email.message import EmailMessage
from pathlib import Path

import pytest

from wlc_manager.config import SmtpConfig
from wlc_manager.database import (
    Database,
    NotificationDeliveryStatus,
    NotificationRepository,
    PasswordRepository,
    PasswordState,
)
from wlc_manager.notifications import (
    NotificationError,
    NotificationNotAttemptedError,
    NotificationOutcome,
    NotificationService,
    SmtpRelay,
    notification_message_id,
)
from wlc_manager.scheduling import YearMonth


class RecordingRelay:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)
        if self.fail:
            raise NotificationError("simulated SMTP failure")


def _smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host="tenant-ee.mail.protection.outlook.com",
        port=25,
        starttls=True,
        timeout_seconds=15,
        max_attachment_bytes=1_000_000,
        sender="wifi@example.test",
        recipients=["owner@example.test"],
        subject_template="Wi-Fi {{MONTH}} - {{SSID}}",
        body_template="Manuses on {{MONTH}} võrgu {{SSID}} materjalid.",
    )


def _service(tmp_path: Path, relay: RecordingRelay) -> tuple[NotificationService, Database]:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    passwords = PasswordRepository(database)
    passwords.create(
        validity_month="2026-08",
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    png = tmp_path / "wifi-2026-08.png"
    pdf = tmp_path / "wifi-2026-08.pdf"
    png.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    pdf.write_bytes(b"%PDF-1.7 content")
    passwords.mark_materials_created("2026-08", png_path=png, pdf_path=pdf)
    return (
        NotificationService(
            passwords,
            NotificationRepository(database),
            relay,
            _smtp_config(),
            ssid="Public-WiFi",
        ),
        database,
    )


def test_notification_sends_two_attachments_once_and_marks_database(tmp_path: Path) -> None:
    relay = RecordingRelay()
    service, database = _service(tmp_path, relay)
    period = YearMonth(2026, 8)

    first = service.send(period)
    second = service.send(period)

    assert first.outcome is NotificationOutcome.SENT
    assert second.outcome is NotificationOutcome.ALREADY_SENT
    assert len(relay.messages) == 1
    message = relay.messages[0]
    assert message["Subject"] == "Wi-Fi 2026-08 - Public-WiFi"
    assert message["Message-ID"] == first.message_id
    assert message.get_body().get_content().strip() == (
        "Manuses on 2026-08 võrgu Public-WiFi materjalid."
    )
    assert [item.get_filename() for item in message.iter_attachments()] == [
        "wifi-2026-08.png",
        "wifi-2026-08.pdf",
    ]
    assert "markus123apple" not in message.get_body().get_content()
    record = PasswordRepository(database).get_by_month("2026-08")
    assert record is not None and record.state is PasswordState.NOTIFIED


def test_ambiguous_failure_is_not_retried_without_explicit_permission(tmp_path: Path) -> None:
    relay = RecordingRelay(fail=True)
    service, database = _service(tmp_path, relay)
    period = YearMonth(2026, 8)

    with pytest.raises(NotificationError, match="simulated SMTP failure"):
        service.send(period)
    uncertain = service.send(period)
    relay.fail = False
    retried = service.send(period, retry_uncertain=True)

    assert uncertain.outcome is NotificationOutcome.UNCERTAIN
    assert retried.outcome is NotificationOutcome.SENT
    assert len(relay.messages) == 2
    delivery = NotificationRepository(database).get("2026-08")
    assert delivery is not None and delivery.status is NotificationDeliveryStatus.SENT


def test_failure_before_smtp_data_releases_claim_for_safe_retry(tmp_path: Path) -> None:
    class PreDataFailureRelay:
        def send(self, message: EmailMessage) -> None:
            raise NotificationNotAttemptedError("connection failed before delivery")

    service, database = _service(tmp_path, RecordingRelay())
    service.relay = PreDataFailureRelay()
    period = YearMonth(2026, 8)

    with pytest.raises(NotificationNotAttemptedError):
        service.send(period)

    assert NotificationRepository(database).get("2026-08") is None
    working_relay = RecordingRelay()
    service.relay = working_relay
    assert service.send(period).outcome is NotificationOutcome.SENT
    assert len(working_relay.messages) == 1


def test_message_id_is_stable_and_does_not_contain_password() -> None:
    period = YearMonth(2026, 8)

    first = notification_message_id(period=period, sender="wifi@example.test", ssid="Public-WiFi")
    second = notification_message_id(period=period, sender="wifi@example.test", ssid="Public-WiFi")

    assert first == second
    assert "markus" not in first


def test_smtp_relay_requires_starttls_before_sending(monkeypatch) -> None:
    calls: list[str] = []

    class FakeSmtp:
        def __init__(self, host, port, timeout) -> None:
            assert host == "tenant-ee.mail.protection.outlook.com"
            assert port == 25
            assert timeout == 15

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def ehlo(self) -> None:
            calls.append("ehlo")

        def has_extn(self, name: str) -> bool:
            calls.append(f"has:{name}")
            return True

        def starttls(self, *, context) -> None:
            calls.append(f"starttls:{context.minimum_version.name}")

        def send_message(self, message, *, from_addr, to_addrs):
            calls.append("send")
            return {}

    monkeypatch.setattr("wlc_manager.notifications.smtplib.SMTP", FakeSmtp)
    message = EmailMessage()
    message["From"] = "wifi@example.test"
    message["To"] = "owner@example.test"
    message.set_content("test")

    SmtpRelay(_smtp_config()).send(message)

    assert calls == ["ehlo", "has:starttls", "starttls:TLSv1_2", "ehlo", "send"]
