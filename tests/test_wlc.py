from pathlib import Path
from typing import Any

import pytest

from wlc_manager.config import SecretsConfig, WlcConfig
from wlc_manager.wlc import (
    AireOsWlcClient,
    WlcConnectionError,
    WlcOperationError,
    WlcResponseError,
    load_wlc_credentials,
    parse_wlan_status,
)


class FakeConnection:
    def __init__(self, *, enabled: bool = True, reject_psk: bool = False) -> None:
        self.enabled = enabled
        self.reject_psk = reject_psk
        self.commands: list[str] = []
        self.save_count = 0
        self.disconnected = False

    def send_command(self, command_string: str, **kwargs: Any) -> str:
        self.commands.append(command_string)
        if command_string.startswith("show wlan"):
            state = "Enabled" if self.enabled else "Disabled"
            return _status_output(state)
        if command_string.startswith("config wlan enable"):
            self.enabled = True
            return "OK"
        if command_string.startswith("config wlan disable"):
            self.enabled = False
            return "OK"
        if "set-key ascii" in command_string:
            return "ERROR: rejected for test" if self.reject_psk else "OK"
        raise AssertionError(f"unexpected command: {command_string}")

    def save_config(self, **kwargs: Any) -> str:
        self.save_count += 1
        return "Configuration saved!"

    def disconnect(self) -> None:
        self.disconnected = True


class FakeFactory:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> FakeConnection:
        self.kwargs = kwargs
        return self.connection


def _status_output(state: str, *, wlan_id: int = 1) -> str:
    return f"""
WLAN Identifier.................................. {wlan_id}
Profile Name..................................... public-wifi
Network Name (SSID).............................. Guest Error Network
Status........................................... {state}
MAC Filtering.................................... Disabled
"""


def _config(tmp_path: Path) -> WlcConfig:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("wlc ssh-rsa AAAATEST", encoding="utf-8")
    return WlcConfig(
        host="wlc.example.internal",
        wlan_id=1,
        ssid="Guest Error Network",
        known_hosts_file=known_hosts,
    )


def _client(tmp_path: Path, connection: FakeConnection) -> tuple[AireOsWlcClient, FakeFactory]:
    factory = FakeFactory(connection)
    client = AireOsWlcClient(
        _config(tmp_path),
        credentials=load_wlc_credentials(_secret_config(tmp_path)),
        connection_factory=factory,
    )
    return client, factory


def _secret_config(tmp_path: Path) -> SecretsConfig:
    username = tmp_path / "username"
    password = tmp_path / "password"
    username.write_text("wlc-user\n", encoding="utf-8")
    password.write_text("device-secret\n", encoding="utf-8")
    return SecretsConfig(wlc_username_file=username, wlc_password_file=password)


@pytest.mark.parametrize(("value", "expected"), [("Enabled", True), ("Disabled", False)])
def test_status_parser_extracts_aireos_fields(value: str, expected: bool) -> None:
    result = parse_wlan_status(_status_output(value), expected_wlan_id=1)

    assert result.wlan_id == 1
    assert result.ssid == "Guest Error Network"
    assert result.enabled is expected


def test_status_parser_rejects_wrong_wlan_and_missing_fields() -> None:
    with pytest.raises(WlcResponseError, match="while WLAN 1 was requested"):
        parse_wlan_status(_status_output("Enabled", wlan_id=2), expected_wlan_id=1)
    missing_status = _status_output("Enabled").replace(
        "Status........................................... Enabled\n", ""
    )
    with pytest.raises(WlcResponseError, match="missing WLAN status"):
        parse_wlan_status(missing_status, expected_wlan_id=1)


def test_credentials_are_loaded_without_trailing_newlines_and_hidden_from_repr(
    tmp_path: Path,
) -> None:
    credentials = load_wlc_credentials(_secret_config(tmp_path))

    assert credentials.username == "wlc-user"
    assert credentials.password == "device-secret"
    assert "wlc-user" not in repr(credentials)
    assert "device-secret" not in repr(credentials)


def test_connection_enforces_known_host_verification(tmp_path: Path) -> None:
    connection = FakeConnection()
    client, factory = _client(tmp_path, connection)

    status = client.get_wlan_status()

    assert status.enabled
    assert factory.kwargs is not None
    assert factory.kwargs["ssh_strict"] is True
    assert factory.kwargs["alt_host_keys"] is True
    assert factory.kwargs["alt_key_file"] == str(_config(tmp_path).known_hosts_file)
    assert factory.kwargs["allow_agent"] is False
    assert connection.disconnected


def test_idempotent_state_change_does_not_write_or_save(tmp_path: Path) -> None:
    connection = FakeConnection(enabled=True)
    client, _ = _client(tmp_path, connection)

    result = client.set_wlan_enabled(True)

    assert not result.changed
    assert connection.commands == ["show wlan 1"]
    assert connection.save_count == 0


def test_state_change_is_verified_and_saved(tmp_path: Path) -> None:
    connection = FakeConnection(enabled=True)
    client, _ = _client(tmp_path, connection)

    result = client.set_wlan_enabled(False)

    assert result.changed
    assert result.before.enabled
    assert not result.after.enabled
    assert connection.commands == ["show wlan 1", "config wlan disable 1", "show wlan 1"]
    assert connection.save_count == 1


def test_psk_update_temporarily_disables_and_restores_wlan(tmp_path: Path) -> None:
    connection = FakeConnection(enabled=True)
    client, _ = _client(tmp_path, connection)

    result = client.update_psk("markus123apple")

    assert result.wlan_was_enabled
    assert result.wlan_is_enabled
    assert connection.commands == [
        "show wlan 1",
        "config wlan disable 1",
        "show wlan 1",
        "config wlan security wpa akm psk set-key ascii markus123apple 1",
        "config wlan enable 1",
        "show wlan 1",
    ]
    assert connection.save_count == 1


def test_failed_psk_update_restores_state_without_leaking_password(tmp_path: Path) -> None:
    connection = FakeConnection(enabled=True, reject_psk=True)
    client, _ = _client(tmp_path, connection)

    with pytest.raises(WlcOperationError) as error:
        client.update_psk("markus123apple")

    assert connection.enabled
    assert connection.save_count == 0
    assert "markus123apple" not in str(error.value)


@pytest.mark.parametrize("password", ["short", "täpitäht123", "has space123"])
def test_invalid_psk_is_rejected_before_connection(tmp_path: Path, password: str) -> None:
    connection = FakeConnection()
    client, factory = _client(tmp_path, connection)

    with pytest.raises(WlcOperationError):
        client.update_psk(password)

    assert factory.kwargs is None


def test_missing_known_hosts_file_prevents_connection(tmp_path: Path) -> None:
    config = _config(tmp_path).model_copy(
        update={"known_hosts_file": tmp_path / "missing-known-hosts"}
    )
    factory = FakeFactory(FakeConnection())
    client = AireOsWlcClient(
        config,
        load_wlc_credentials(_secret_config(tmp_path)),
        connection_factory=factory,
    )

    with pytest.raises(WlcConnectionError, match="known-hosts"):
        client.get_wlan_status()

    assert factory.kwargs is None


def test_connection_failure_does_not_expose_credentials(tmp_path: Path) -> None:
    def failing_factory(**kwargs: Any):
        raise RuntimeError(f"authentication failed for {kwargs['password']}")

    client = AireOsWlcClient(
        _config(tmp_path),
        load_wlc_credentials(_secret_config(tmp_path)),
        connection_factory=failing_factory,
    )

    with pytest.raises(WlcConnectionError) as error:
        client.get_wlan_status()

    assert "device-secret" not in str(error.value)
