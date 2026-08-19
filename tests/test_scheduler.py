from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from wlc_manager.artifacts import ArtifactFiles
from wlc_manager.config import load_settings
from wlc_manager.database import Database, LeaseLockRepository, PasswordRepository, PasswordState
from wlc_manager.scheduler import WLC_MUTATION_LOCK_NAME, SchedulerRuntime
from wlc_manager.scheduling import YearMonth
from wlc_manager.wlc import PskUpdateResult, WlanStateChange, WlanStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecordingWlcClient:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.requests: list[bool] = []
        self.passwords: list[str] = []

    def set_wlan_enabled(self, enabled: bool) -> WlanStateChange:
        self.requests.append(enabled)
        before = WlanStatus(wlan_id=1, ssid="public-wifi", enabled=self.enabled)
        changed = self.enabled is not enabled
        self.enabled = enabled
        after = WlanStatus(wlan_id=1, ssid="public-wifi", enabled=self.enabled)
        return WlanStateChange(before=before, after=after, changed=changed)

    def get_wlan_status(self) -> WlanStatus:
        return WlanStatus(wlan_id=1, ssid="public-wifi", enabled=self.enabled)

    def update_psk(self, password: str) -> PskUpdateResult:
        self.passwords.append(password)
        return PskUpdateResult(
            wlan_id=1,
            wlan_was_enabled=self.enabled,
            wlan_is_enabled=self.enabled,
        )


class FakePosterGenerator:
    def __init__(self, config) -> None:
        self.config = config

    def generate(self, record, *, ssid, current_period) -> ArtifactFiles:
        self.config.output_directory.mkdir(parents=True, exist_ok=True)
        png_path = self.config.output_directory / f"wifi-{record.validity_month}.png"
        pdf_path = self.config.output_directory / f"wifi-{record.validity_month}.pdf"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
        pdf_path.write_bytes(b"%PDF-1.5 content")
        return ArtifactFiles(
            period=YearMonth.parse(record.validity_month),
            png_path=png_path,
            pdf_path=pdf_path,
            created=True,
        )


class RecordingNotificationRelay:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


def _settings(tmp_path: Path):
    dictionary = tmp_path / "dictionary.txt"
    dictionary.write_text("apple\npear\nplum\n", encoding="utf-8")
    data = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["database"]["path"] = str(tmp_path / "manager.db")
    data["password"]["dictionary_path"] = str(dictionary)
    data["artifacts"]["svg_template_path"] = str(PROJECT_ROOT / "templates/wifi-poster.svg")
    data["artifacts"]["output_directory"] = str(tmp_path / "artifacts")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_settings(config_path)


def test_runtime_registers_restart_safe_interval_jobs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.migrate()
    now = datetime(2026, 8, 27, 8, 0, tzinfo=ZoneInfo("Europe/Tallinn"))

    runtime = SchedulerRuntime(
        settings,
        database,
        now=lambda timezone: now,
        poster_generator=FakePosterGenerator(settings.artifacts),
    )

    assert runtime.scheduled_job_ids() == {
        "monthly-password-reconciliation",
        "scheduler-heartbeat",
        "wlan-state-reconciliation",
    }


def test_runtime_startup_reconciliation_and_heartbeat_are_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.migrate()
    now = datetime(2026, 8, 27, 8, 0, tzinfo=ZoneInfo("Europe/Tallinn"))
    relay = RecordingNotificationRelay()
    controller = RecordingWlcClient(enabled=False)
    runtime = SchedulerRuntime(
        settings,
        database,
        now=lambda timezone: now,
        poster_generator=FakePosterGenerator(settings.artifacts),
        notification_relay=relay,
        wlc_client_factory=lambda: controller,
    )

    runtime.heartbeat_once()
    runtime.reconcile_monthly_once()
    runtime.reconcile_monthly_once()

    repository = PasswordRepository(database)
    current = repository.get_by_month("2026-08")
    following = repository.get_by_month("2026-09")
    assert current is not None and current.state is PasswordState.APPLIED
    assert following is not None and following.state is PasswordState.NOTIFIED
    assert len(relay.messages) == 2
    assert len(controller.passwords) == 1
    assert controller.passwords[0] == current.password


def test_runtime_reconciles_wlan_to_local_work_schedule(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.migrate()
    now = datetime(2026, 8, 27, 8, 0, tzinfo=ZoneInfo("Europe/Tallinn"))
    client = RecordingWlcClient(enabled=False)
    runtime = SchedulerRuntime(
        settings,
        database,
        now=lambda timezone: now,
        wlc_client_factory=lambda: client,
    )

    runtime.reconcile_wlan_once()

    assert client.requests == [True]
    assert client.enabled


def test_runtime_skips_wlan_when_mutation_lock_is_owned(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.migrate()
    locks = LeaseLockRepository(database)
    assert locks.acquire(name=WLC_MUTATION_LOCK_NAME, owner="manual-run", ttl_seconds=300)
    now = datetime(2026, 8, 27, 8, 0, tzinfo=ZoneInfo("Europe/Tallinn"))
    client = RecordingWlcClient(enabled=False)
    runtime = SchedulerRuntime(
        settings,
        database,
        now=lambda timezone: now,
        wlc_client_factory=lambda: client,
    )

    runtime.reconcile_wlan_once()

    assert client.requests == []
