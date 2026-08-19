from __future__ import annotations

import hashlib
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from wlc_manager.config import SmtpConfig
from wlc_manager.database import (
    NotificationClaimOutcome,
    NotificationRepository,
    PasswordRepository,
    PasswordState,
)
from wlc_manager.scheduling import YearMonth

_TEMPLATE_PATTERN = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
_ALLOWED_TEMPLATE_FIELDS = {"{{MONTH}}", "{{SSID}}"}


class NotificationError(RuntimeError):
    """Raised when a notification cannot be prepared or delivered safely."""


class NotificationNotAttemptedError(NotificationError):
    """Raised when SMTP failed before message data transmission started."""


class NotificationOutcome(StrEnum):
    SENT = "sent"
    ALREADY_SENT = "already_sent"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class NotificationResult:
    period: YearMonth
    outcome: NotificationOutcome
    message_id: str


@dataclass(frozen=True, slots=True)
class NotificationReconciliationResult:
    results: tuple[NotificationResult, ...]


class MessageRelay(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SmtpRelay:
    """Microsoft 365 connector-based SMTP relay with required STARTTLS."""

    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    def send(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        data_started = False
        try:
            with smtplib.SMTP(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout_seconds,
            ) as client:
                client.ehlo()
                if not client.has_extn("starttls"):
                    raise NotificationNotAttemptedError("SMTP relay does not advertise STARTTLS")
                client.starttls(context=context)
                client.ehlo()
                data_started = True
                refused = client.send_message(
                    message,
                    from_addr=self.config.sender,
                    to_addrs=self.config.recipients,
                )
                if refused:
                    raise NotificationError("SMTP relay refused one or more recipients")
        except NotificationError:
            raise
        except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
            error_type = type(exc).__name__
            if not data_started:
                raise NotificationNotAttemptedError(
                    f"SMTP connection failed before delivery ({error_type})"
                ) from None
            raise NotificationError(f"SMTP delivery failed ({error_type})") from None


class NotificationService:
    def __init__(
        self,
        password_repository: PasswordRepository,
        notification_repository: NotificationRepository,
        relay: MessageRelay,
        config: SmtpConfig,
        *,
        ssid: str,
    ) -> None:
        self.password_repository = password_repository
        self.notification_repository = notification_repository
        self.relay = relay
        self.config = config
        self.ssid = ssid

    def send(
        self,
        period: YearMonth,
        *,
        retry_uncertain: bool = False,
    ) -> NotificationResult:
        record = self.password_repository.get_by_month(str(period))
        if record is None:
            raise NotificationError(f"password record does not exist for {period}")

        message_id = notification_message_id(
            period=period,
            sender=self.config.sender,
            ssid=self.ssid,
        )
        if record.state in {PasswordState.NOTIFIED, PasswordState.APPLIED}:
            return NotificationResult(period, NotificationOutcome.ALREADY_SENT, message_id)
        if record.state is not PasswordState.MATERIALS_CREATED:
            raise NotificationError(f"notification requires materials_created state for {period}")
        if record.png_path is None or record.pdf_path is None:
            raise NotificationError(f"poster file paths are missing for {period}")

        message = build_notification_message(
            period=period,
            ssid=self.ssid,
            png_path=Path(record.png_path),
            pdf_path=Path(record.pdf_path),
            message_id=message_id,
            config=self.config,
        )
        claim = self.notification_repository.claim(
            validity_month=str(period),
            message_id=message_id,
            retry_uncertain=retry_uncertain,
        )
        if claim is NotificationClaimOutcome.ALREADY_SENT:
            return NotificationResult(period, NotificationOutcome.ALREADY_SENT, message_id)
        if claim is NotificationClaimOutcome.UNCERTAIN:
            return NotificationResult(period, NotificationOutcome.UNCERTAIN, message_id)

        try:
            self.relay.send(message)
        except NotificationNotAttemptedError:
            self.notification_repository.release_unsent_claim(
                validity_month=str(period),
                message_id=message_id,
            )
            raise
        except Exception as exc:
            self.notification_repository.mark_uncertain(
                validity_month=str(period),
                error_code=type(exc).__name__,
            )
            if isinstance(exc, NotificationError):
                raise
            raise NotificationError(
                f"notification delivery failed ({type(exc).__name__})"
            ) from None

        self.notification_repository.mark_sent(
            validity_month=str(period),
            message_id=message_id,
        )
        return NotificationResult(period, NotificationOutcome.SENT, message_id)


class MonthlyNotificationReconciler:
    def __init__(self, service: NotificationService) -> None:
        self.service = service

    def reconcile(self, *, current_period: YearMonth) -> NotificationReconciliationResult:
        results: list[NotificationResult] = []
        for period in (current_period, current_period.next()):
            record = self.service.password_repository.get_by_month(str(period))
            if record is None or record.state not in {
                PasswordState.MATERIALS_CREATED,
                PasswordState.NOTIFIED,
            }:
                continue
            results.append(self.service.send(period))
        return NotificationReconciliationResult(results=tuple(results))


def build_notification_message(
    *,
    period: YearMonth,
    ssid: str,
    png_path: Path,
    pdf_path: Path,
    message_id: str,
    config: SmtpConfig,
) -> EmailMessage:
    replacements = {"{{MONTH}}": str(period), "{{SSID}}": ssid}
    subject = _render_template(config.subject_template, replacements, field_name="subject")
    body = _render_template(config.body_template, replacements, field_name="body")
    png = _read_attachment(
        png_path,
        expected_name=f"wifi-{period}.png",
        signature=b"\x89PNG\r\n\x1a\n",
        max_bytes=config.max_attachment_bytes,
    )
    pdf = _read_attachment(
        pdf_path,
        expected_name=f"wifi-{period}.pdf",
        signature=b"%PDF-",
        max_bytes=config.max_attachment_bytes,
    )

    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = message_id
    message.set_content(body)
    message.add_attachment(png, maintype="image", subtype="png", filename=png_path.name)
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=pdf_path.name)
    return message


def notification_message_id(*, period: YearMonth, sender: str, ssid: str) -> str:
    domain = sender.rpartition("@")[2].lower()
    digest = hashlib.sha256(f"{period}\0{sender.casefold()}\0{ssid}".encode()).hexdigest()[:24]
    return f"<wlc-manager-{period}-{digest}@{domain}>"


def _render_template(template: str, replacements: dict[str, str], *, field_name: str) -> str:
    placeholders = set(_TEMPLATE_PATTERN.findall(template))
    unknown = placeholders - _ALLOWED_TEMPLATE_FIELDS
    if unknown:
        raise NotificationError(
            f"SMTP {field_name} template contains unknown placeholders: "
            f"{', '.join(sorted(unknown))}"
        )
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _read_attachment(
    path: Path,
    *,
    expected_name: str,
    signature: bytes,
    max_bytes: int,
) -> bytes:
    if path.name != expected_name:
        raise NotificationError(f"unexpected attachment filename for {expected_name}")
    if path.is_symlink() or not path.is_file():
        raise NotificationError(f"attachment is not a regular file: {expected_name}")
    size = path.stat().st_size
    if size <= len(signature) or size > max_bytes:
        raise NotificationError(f"attachment size is invalid: {expected_name}")
    data = path.read_bytes()
    if not data.startswith(signature):
        raise NotificationError(f"attachment signature is invalid: {expected_name}")
    return data
