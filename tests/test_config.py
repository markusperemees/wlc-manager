from pathlib import Path

import pytest
import yaml

from wlc_manager.config import ConfigurationError, Weekday, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.yaml"


def test_example_configuration_is_valid_and_paths_are_resolved() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    assert settings.app.timezone == "Europe/Tallinn"
    assert settings.scheduler.work_hours[Weekday.FRIDAY] is not None
    assert settings.scheduler.work_hours[Weekday.SATURDAY] is None
    assert settings.database.path == (PROJECT_ROOT / "data/wlc-manager.db").resolve()
    assert settings.password.dictionary_path.is_absolute()
    assert settings.wlc.known_hosts_file.is_absolute()


def test_unknown_configuration_key_is_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["app"]["unexpected"] = True
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unexpected"):
        load_settings(path)


def test_missing_weekday_is_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    del data["scheduler"]["work_hours"]["sunday"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="sunday"):
        load_settings(path)


def test_unknown_timezone_is_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["app"]["timezone"] = "Nowhere/Invalid"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unknown IANA timezone"):
        load_settings(path)


def test_smtp_starttls_cannot_be_disabled(tmp_path: Path) -> None:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["smtp"]["starttls"] = False
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="starttls"):
        load_settings(path)
