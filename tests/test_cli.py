from pathlib import Path

import yaml
from typer.testing import CliRunner

from wlc_manager.artifacts import ArtifactFiles, PosterGenerator
from wlc_manager.cli import app
from wlc_manager.database import (
    Database,
    HeartbeatRepository,
    LeaseLockRepository,
    NotificationRepository,
    PasswordRepository,
    PasswordState,
)
from wlc_manager.scheduler import MONTHLY_LOCK_NAME, SCHEDULER_SERVICE_NAME, SchedulerRuntime
from wlc_manager.scheduling import YearMonth
from wlc_manager.wlc import PskUpdateResult, WlanStateChange, WlanStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


class FakeCliWlc:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.passwords: list[str] = []

    def get_wlan_status(self) -> WlanStatus:
        return WlanStatus(wlan_id=1, ssid="public-wifi", enabled=self.enabled)

    def set_wlan_enabled(self, enabled: bool) -> WlanStateChange:
        before = self.get_wlan_status()
        changed = self.enabled is not enabled
        self.enabled = enabled
        return WlanStateChange(before=before, after=self.get_wlan_status(), changed=changed)

    def update_psk(self, password: str) -> PskUpdateResult:
        self.passwords.append(password)
        return PskUpdateResult(
            wlan_id=1,
            wlan_was_enabled=self.enabled,
            wlan_is_enabled=self.enabled,
        )


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_example_configuration_validates() -> None:
    result = runner.invoke(
        app,
        ["--config", str(PROJECT_ROOT / "config.example.yaml"), "config", "validate"],
    )

    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    assert '"run_id"' in result.stdout


def test_database_migration_and_status_commands(tmp_path: Path) -> None:
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(tmp_path / "manager.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    migration = runner.invoke(
        app,
        ["--config", str(config_path), "db", "migrate"],
    )
    status = runner.invoke(
        app,
        ["--config", str(config_path), "status"],
    )

    assert migration.exit_code == 0
    assert "Database schema version: 4" in migration.stdout
    assert status.exit_code == 0
    assert '"schema_version": 4' in status.stdout


def test_password_generation_command_is_idempotent_and_does_not_print_password(
    tmp_path: Path,
) -> None:
    dictionary_path = tmp_path / "dictionary.txt"
    dictionary_path.write_text("apple\npää\npear\n", encoding="utf-8")
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(tmp_path / "manager.db")
    data["password"]["dictionary_path"] = str(dictionary_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner.invoke(app, ["--config", str(config_path), "db", "migrate"])

    first = runner.invoke(
        app,
        ["--config", str(config_path), "password", "generate", "--month", "2026-09"],
    )
    second = runner.invoke(
        app,
        ["--config", str(config_path), "password", "generate", "--month", "2026-09"],
    )

    assert first.exit_code == 0
    assert "Password for 2026-09: generated" in first.stdout
    assert "markus" not in first.stdout
    assert '"invalid_entries":1' in first.stdout
    assert second.exit_code == 0
    assert "Password for 2026-09: already existed" in second.stdout


def test_healthcheck_requires_recent_scheduler_heartbeat(tmp_path: Path) -> None:
    database_path = tmp_path / "manager.db"
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(database_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner.invoke(app, ["--config", str(config_path), "db", "migrate"])

    missing = runner.invoke(app, ["--config", str(config_path), "healthcheck"])
    HeartbeatRepository(Database(database_path)).beat(SCHEDULER_SERVICE_NAME)
    healthy = runner.invoke(app, ["--config", str(config_path), "healthcheck"])

    assert missing.exit_code == 1
    assert "missing or stale" in missing.stderr
    assert healthy.exit_code == 0
    assert healthy.stdout.strip() == "healthy"


def test_run_command_initializes_long_lived_scheduler(tmp_path: Path, monkeypatch) -> None:
    dictionary_path = tmp_path / "dictionary.txt"
    dictionary_path.write_text("apple\npear\n", encoding="utf-8")
    database_path = tmp_path / "manager.db"
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(database_path)
    data["password"]["dictionary_path"] = str(dictionary_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(SchedulerRuntime, "run_forever", lambda self: None)

    result = runner.invoke(app, ["--config", str(config_path), "run"])

    assert result.exit_code == 0
    assert '"event":"scheduler_initialized"' in result.stdout
    assert (
        HeartbeatRepository(Database(database_path)).age_seconds(SCHEDULER_SERVICE_NAME) is not None
    )


def test_password_command_respects_scheduler_process_lock(tmp_path: Path) -> None:
    dictionary_path = tmp_path / "dictionary.txt"
    dictionary_path.write_text("apple\npear\n", encoding="utf-8")
    database_path = tmp_path / "manager.db"
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(database_path)
    data["password"]["dictionary_path"] = str(dictionary_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner.invoke(app, ["--config", str(config_path), "db", "migrate"])
    locks = LeaseLockRepository(Database(database_path))
    assert locks.acquire(name=MONTHLY_LOCK_NAME, owner="scheduler-run", ttl_seconds=300)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "password", "generate", "--month", "2026-09"],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)


def test_artifact_generation_command_records_png_and_pdf(tmp_path: Path, monkeypatch) -> None:
    dictionary_path = tmp_path / "dictionary.txt"
    dictionary_path.write_text("apple\npear\n", encoding="utf-8")
    database_path = tmp_path / "manager.db"
    output_directory = tmp_path / "artifacts"
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(database_path)
    data["password"]["dictionary_path"] = str(dictionary_path)
    data["artifacts"]["svg_template_path"] = str(PROJECT_ROOT / "templates/wifi-poster.svg")
    data["artifacts"]["output_directory"] = str(output_directory)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner.invoke(app, ["--config", str(config_path), "db", "migrate"])
    current_period = YearMonth.from_date(__import__("datetime").date.today())
    runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "password",
            "generate",
            "--month",
            str(current_period),
        ],
    )

    def fake_generate(self, record, *, ssid, current_period):
        output_directory.mkdir(parents=True, exist_ok=True)
        png_path = output_directory / f"wifi-{record.validity_month}.png"
        pdf_path = output_directory / f"wifi-{record.validity_month}.pdf"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
        pdf_path.write_bytes(b"%PDF-1.5 content")
        return ArtifactFiles(
            period=YearMonth.parse(record.validity_month),
            png_path=png_path,
            pdf_path=pdf_path,
            created=True,
        )

    monkeypatch.setattr(PosterGenerator, "generate", fake_generate)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "artifacts",
            "generate",
            "--month",
            str(current_period),
        ],
    )

    record = PasswordRepository(Database(database_path)).get_by_month(str(current_period))
    assert result.exit_code == 0
    assert f"Poster files for {current_period}: created" in result.stdout
    assert record is not None
    assert record.password not in result.stdout
    assert record.state is PasswordState.MATERIALS_CREATED
    assert record.png_path == str((output_directory / f"wifi-{current_period}.png").resolve())
    assert record.pdf_path == str((output_directory / f"wifi-{current_period}.pdf").resolve())


def test_notification_command_sends_materials_once(tmp_path: Path, monkeypatch) -> None:
    dictionary_path = tmp_path / "dictionary.txt"
    dictionary_path.write_text("apple\npear\n", encoding="utf-8")
    database_path = tmp_path / "manager.db"
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(database_path)
    data["password"]["dictionary_path"] = str(dictionary_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner.invoke(app, ["--config", str(config_path), "db", "migrate"])
    current_period = YearMonth.from_date(__import__("datetime").date.today())
    runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "password",
            "generate",
            "--month",
            str(current_period),
        ],
    )
    png = tmp_path / f"wifi-{current_period}.png"
    pdf = tmp_path / f"wifi-{current_period}.pdf"
    png.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    pdf.write_bytes(b"%PDF-1.7 content")
    PasswordRepository(Database(database_path)).mark_materials_created(
        str(current_period), png_path=png, pdf_path=pdf
    )

    sent_messages = []

    class FakeRelay:
        def __init__(self, config) -> None:
            pass

        def send(self, message) -> None:
            sent_messages.append(message)

    monkeypatch.setattr("wlc_manager.cli.SmtpRelay", FakeRelay)
    first = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "notifications",
            "send",
            "--month",
            str(current_period),
        ],
    )
    second = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "notifications",
            "send",
            "--month",
            str(current_period),
        ],
    )

    record = PasswordRepository(Database(database_path)).get_by_month(str(current_period))
    assert first.exit_code == 0
    assert f"Notification for {current_period}: sent" in first.stdout
    assert second.exit_code == 0
    assert f"Notification for {current_period}: already sent" in second.stdout
    assert len(sent_messages) == 1
    assert record is not None and record.state is PasswordState.NOTIFIED
    assert record.password not in first.stdout


def test_wlc_password_command_applies_notified_password_once(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "manager.db"
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(database_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner.invoke(app, ["--config", str(config_path), "db", "migrate"])
    current_period = YearMonth.from_date(__import__("datetime").date.today())
    database = Database(database_path)
    passwords = PasswordRepository(database)
    passwords.create(
        validity_month=str(current_period),
        password="markus123apple",
        dictionary_word="apple",
        run_id="run-1",
    )
    passwords.mark_materials_created(
        str(current_period),
        png_path=tmp_path / f"wifi-{current_period}.png",
        pdf_path=tmp_path / f"wifi-{current_period}.pdf",
    )
    notifications = NotificationRepository(database)
    message_id = f"<wifi-{current_period}@example.test>"
    notifications.claim(validity_month=str(current_period), message_id=message_id)
    notifications.mark_sent(validity_month=str(current_period), message_id=message_id)
    client = FakeCliWlc(enabled=True)
    monkeypatch.setattr("wlc_manager.cli._wlc_client", lambda settings: client)

    first = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "wlc",
            "apply-password",
            "--month",
            str(current_period),
        ],
    )
    second = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "wlc",
            "apply-password",
            "--month",
            str(current_period),
        ],
    )

    record = passwords.get_by_month(str(current_period))
    assert first.exit_code == 0
    assert f"WLC password for {current_period}: applied" in first.stdout
    assert second.exit_code == 0
    assert f"WLC password for {current_period}: already applied" in second.stdout
    assert client.passwords == ["markus123apple"]
    assert record is not None and record.state is PasswordState.APPLIED
    assert "markus123apple" not in first.stdout


def test_wlc_status_and_state_commands_use_verified_adapter(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "manager.db"
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(database_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner.invoke(app, ["--config", str(config_path), "db", "migrate"])
    client = FakeCliWlc(enabled=True)
    monkeypatch.setattr("wlc_manager.cli._wlc_client", lambda settings: client)

    status = runner.invoke(app, ["--config", str(config_path), "wlc", "status"])
    changed = runner.invoke(
        app,
        ["--config", str(config_path), "wlc", "set-state", "disabled"],
    )
    unchanged = runner.invoke(
        app,
        ["--config", str(config_path), "wlc", "set-state", "disabled"],
    )

    assert status.exit_code == 0
    assert "WLAN 1 (public-wifi): enabled" in status.stdout
    assert changed.exit_code == 0
    assert "disabled; changed" in changed.stdout
    assert unchanged.exit_code == 0
    assert "disabled; already correct" in unchanged.stdout
