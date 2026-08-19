from __future__ import annotations

import re
from datetime import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigurationError(ValueError):
    """Raised when the application configuration cannot be loaded or validated."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Weekday(StrEnum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class AppConfig(StrictModel):
    timezone: str = "Europe/Tallinn"

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value


class WorkWindow(StrictModel):
    start: time
    end: time

    @model_validator(mode="after")
    def end_must_follow_start(self) -> WorkWindow:
        if self.end <= self.start:
            raise ValueError("work window end must be later than start")
        return self


class SchedulerConfig(StrictModel):
    wlan_check_seconds: int = Field(default=60, ge=30, le=3600)
    monthly_check_seconds: int = Field(default=3600, ge=60, le=86400)
    work_hours: dict[Weekday, WorkWindow | None]

    @field_validator("work_hours")
    @classmethod
    def all_weekdays_must_be_present(
        cls, value: dict[Weekday, WorkWindow | None]
    ) -> dict[Weekday, WorkWindow | None]:
        missing = set(Weekday) - set(value)
        if missing:
            names = ", ".join(sorted(day.value for day in missing))
            raise ValueError(f"work_hours is missing weekdays: {names}")
        return value


class DatabaseConfig(StrictModel):
    path: Path
    busy_timeout_seconds: int = Field(default=30, ge=1, le=300)


class PasswordConfig(StrictModel):
    prefix: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    dictionary_path: Path
    history_size: int = Field(default=12, ge=1, le=120)
    random_digits: int = Field(default=3, ge=1, le=8)


class ArtifactConfig(StrictModel):
    svg_template_path: Path
    output_directory: Path
    security_label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = "WPA2"
    qr_auth_type: Literal["WPA"] = "WPA"
    qr_size_mm: int = Field(default=60, ge=20, le=120)
    png_dpi: int = Field(default=300, ge=72, le=600)


class WlcConfig(StrictModel):
    host: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    port: int = Field(default=22, ge=1, le=65535)
    device_type: Literal["cisco_wlc_ssh"] = "cisco_wlc_ssh"
    wlan_id: int = Field(ge=1, le=512)
    ssid: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    known_hosts_file: Path
    connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    read_timeout_seconds: int = Field(default=30, ge=5, le=300)


class SmtpConfig(StrictModel):
    host: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    port: int = Field(default=25, ge=1, le=65535)
    starttls: Literal[True] = True
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    max_attachment_bytes: int = Field(default=10_000_000, ge=1024, le=100_000_000)
    sender: str
    recipients: list[str] = Field(min_length=1)
    subject_template: Annotated[str, StringConstraints(min_length=1, max_length=255)] = (
        "Avaliku Wi-Fi andmed - {{MONTH}}"
    )
    body_template: Annotated[str, StringConstraints(min_length=1, max_length=5000)] = (
        "Tere\n\nManuses on {{MONTH}} avaliku Wi-Fi ühendusmaterjalid võrgu {{SSID}} jaoks."
        "\n\nSee kiri on saadetud automaatselt."
    )

    @field_validator("sender")
    @classmethod
    def sender_must_look_like_email(cls, value: str) -> str:
        return _validate_email_like(value)

    @field_validator("recipients")
    @classmethod
    def recipients_must_look_like_email(cls, value: list[str]) -> list[str]:
        recipients = [_validate_email_like(item) for item in value]
        if len({item.casefold() for item in recipients}) != len(recipients):
            raise ValueError("smtp recipients must be unique")
        return recipients

    @field_validator("subject_template")
    @classmethod
    def subject_must_be_single_line(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("smtp subject_template must be a single line")
        return value


class GraylogConfig(StrictModel):
    enabled: bool = False
    host: str | None = None
    port: int = Field(default=12201, ge=1, le=65535)
    protocol: Literal["udp"] = "udp"

    @model_validator(mode="after")
    def host_is_required_when_enabled(self) -> GraylogConfig:
        if self.enabled and not self.host:
            raise ValueError("graylog host is required when Graylog is enabled")
        return self


class SecretsConfig(StrictModel):
    wlc_username_file: Path
    wlc_password_file: Path


class Settings(StrictModel):
    app: AppConfig
    scheduler: SchedulerConfig
    database: DatabaseConfig
    password: PasswordConfig
    artifacts: ArtifactConfig
    wlc: WlcConfig
    smtp: SmtpConfig
    graylog: GraylogConfig
    secrets: SecretsConfig


def load_settings(path: str | Path) -> Settings:
    """Load, validate, and normalize settings from a YAML file."""
    config_path = Path(path).expanduser().resolve()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration {config_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigurationError(f"configuration root in {config_path} must be a mapping")

    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc

    base = config_path.parent
    return settings.model_copy(
        update={
            "database": settings.database.model_copy(
                update={"path": _resolve_path(settings.database.path, base)}
            ),
            "password": settings.password.model_copy(
                update={"dictionary_path": _resolve_path(settings.password.dictionary_path, base)}
            ),
            "artifacts": settings.artifacts.model_copy(
                update={
                    "svg_template_path": _resolve_path(settings.artifacts.svg_template_path, base),
                    "output_directory": _resolve_path(settings.artifacts.output_directory, base),
                }
            ),
            "wlc": settings.wlc.model_copy(
                update={
                    "known_hosts_file": _resolve_path(settings.wlc.known_hosts_file, base),
                }
            ),
            "secrets": settings.secrets.model_copy(
                update={
                    "wlc_username_file": _resolve_path(settings.secrets.wlc_username_file, base),
                    "wlc_password_file": _resolve_path(settings.secrets.wlc_password_file, base),
                }
            ),
        }
    )


def _resolve_path(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (base / expanded).resolve()


def _validate_email_like(value: str) -> str:
    stripped = value.strip()
    if not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
        stripped,
    ):
        raise ValueError(f"invalid email address: {value}")
    return stripped
