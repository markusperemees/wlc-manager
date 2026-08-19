from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from netmiko import ConnectHandler

from wlc_manager.config import SecretsConfig, WlcConfig

_WLAN_ID_PATTERN = re.compile(
    r"^WLAN Identifier\.*\s+(?P<value>[0-9]+)\s*$", re.IGNORECASE | re.MULTILINE
)
_SSID_PATTERN = re.compile(
    r"^Network Name \(SSID\)\.*\s+(?P<value>.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_STATUS_PATTERN = re.compile(
    r"^Status\.*\s+(?P<value>Enabled|Disabled)\s*$", re.IGNORECASE | re.MULTILINE
)
_ERROR_PATTERN = re.compile(
    r"^\s*(?:ERROR\b|Command not recognized|Incorrect usage|Invalid command|Request failed)",
    re.IGNORECASE | re.MULTILINE,
)


class WlcError(RuntimeError):
    """Base error for safe WLC operations."""


class WlcConnectionError(WlcError):
    """Raised when a secure SSH session cannot be established."""


class WlcOperationError(WlcError):
    """Raised when AireOS rejects or does not apply an operation."""


class WlcResponseError(WlcError):
    """Raised when AireOS output cannot be safely interpreted."""


class WlcConnection(Protocol):
    def send_command(self, command_string: str, **kwargs: Any) -> str: ...

    def save_config(self, **kwargs: Any) -> str: ...

    def disconnect(self) -> None: ...


class ConnectionFactory(Protocol):
    def __call__(self, **kwargs: Any) -> WlcConnection: ...


class WlanController(Protocol):
    def get_wlan_status(self) -> WlanStatus: ...

    def set_wlan_enabled(self, enabled: bool) -> WlanStateChange: ...


class ManagedWlc(WlanController, Protocol):
    def update_psk(self, password: str) -> PskUpdateResult: ...


@dataclass(frozen=True, slots=True)
class WlcCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class WlanStatus:
    wlan_id: int
    ssid: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class WlanStateChange:
    before: WlanStatus
    after: WlanStatus
    changed: bool


@dataclass(frozen=True, slots=True)
class PskUpdateResult:
    wlan_id: int
    wlan_was_enabled: bool
    wlan_is_enabled: bool


class AireOsWlcClient:
    def __init__(
        self,
        config: WlcConfig,
        credentials: WlcCredentials,
        *,
        connection_factory: ConnectionFactory = ConnectHandler,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.connection_factory = connection_factory

    @classmethod
    def from_secret_files(
        cls,
        config: WlcConfig,
        secrets: SecretsConfig,
        *,
        connection_factory: ConnectionFactory = ConnectHandler,
    ) -> AireOsWlcClient:
        return cls(
            config,
            load_wlc_credentials(secrets),
            connection_factory=connection_factory,
        )

    def get_wlan_status(self) -> WlanStatus:
        with self._connection() as connection:
            return self._read_wlan_status(connection)

    def set_wlan_enabled(self, enabled: bool) -> WlanStateChange:
        with self._connection() as connection:
            before = self._read_wlan_status(connection)
            if before.enabled is enabled:
                return WlanStateChange(before=before, after=before, changed=False)

            self._set_wlan_state(connection, enabled)
            after = self._read_wlan_status(connection)
            if after.enabled is not enabled:
                raise WlcOperationError("WLC did not reach the requested WLAN state")
            self._save(connection)
            return WlanStateChange(before=before, after=after, changed=True)

    def update_psk(self, password: str) -> PskUpdateResult:
        _validate_ascii_psk(password)
        with self._connection() as connection:
            original = self._read_wlan_status(connection)
            try:
                if original.enabled:
                    self._set_wlan_state(connection, False)
                    disabled = self._read_wlan_status(connection)
                    if disabled.enabled:
                        raise WlcOperationError("WLC did not disable the WLAN before PSK update")
                command = (
                    "config wlan security wpa akm psk set-key ascii "
                    f"{password} {self.config.wlan_id}"
                )
                self._send(connection, command)
            except WlcError:
                self._restore_wlan_state_after_failure(connection, original.enabled)
                raise

            if original.enabled:
                try:
                    self._set_wlan_state(connection, True)
                except WlcError:
                    raise WlcOperationError(
                        "PSK was updated but the original WLAN state could not be restored"
                    ) from None

            final = self._read_wlan_status(connection)
            if final.enabled is not original.enabled:
                raise WlcOperationError(
                    "PSK was updated but WLAN state verification did not match the original state"
                )
            self._save(connection)
            return PskUpdateResult(
                wlan_id=self.config.wlan_id,
                wlan_was_enabled=original.enabled,
                wlan_is_enabled=final.enabled,
            )

    @contextmanager
    def _connection(self) -> Iterator[WlcConnection]:
        if not self.config.known_hosts_file.is_file():
            raise WlcConnectionError("configured SSH known-hosts file does not exist")
        try:
            connection = self.connection_factory(
                device_type=self.config.device_type,
                host=self.config.host,
                port=self.config.port,
                username=self.credentials.username,
                password=self.credentials.password,
                conn_timeout=self.config.connect_timeout_seconds,
                auth_timeout=self.config.connect_timeout_seconds,
                banner_timeout=self.config.connect_timeout_seconds,
                read_timeout_override=self.config.read_timeout_seconds,
                ssh_strict=True,
                system_host_keys=False,
                alt_host_keys=True,
                alt_key_file=str(self.config.known_hosts_file),
                allow_agent=False,
                use_keys=False,
                keepalive=30,
                fast_cli=False,
            )
        except Exception as exc:
            raise WlcConnectionError(
                f"secure WLC SSH connection failed ({type(exc).__name__})"
            ) from None

        try:
            yield connection
        finally:
            with suppress(Exception):
                connection.disconnect()

    def _read_wlan_status(self, connection: WlcConnection) -> WlanStatus:
        output = self._send(connection, f"show wlan {self.config.wlan_id}")
        status = parse_wlan_status(output, expected_wlan_id=self.config.wlan_id)
        if status.ssid != self.config.ssid:
            raise WlcResponseError(
                f"WLC returned SSID {status.ssid!r} while {self.config.ssid!r} was configured"
            )
        return status

    def _set_wlan_state(self, connection: WlcConnection, enabled: bool) -> None:
        action = "enable" if enabled else "disable"
        self._send(connection, f"config wlan {action} {self.config.wlan_id}")

    def _restore_wlan_state_after_failure(
        self, connection: WlcConnection, original_enabled: bool
    ) -> None:
        try:
            current = self._read_wlan_status(connection)
            if current.enabled is not original_enabled:
                self._set_wlan_state(connection, original_enabled)
                restored = self._read_wlan_status(connection)
                if restored.enabled is not original_enabled:
                    raise WlcOperationError("WLAN recovery verification failed")
        except WlcError:
            raise WlcOperationError(
                "PSK update failed and the original WLAN state could not be restored"
            ) from None

    def _send(self, connection: WlcConnection, command: str) -> str:
        try:
            output = connection.send_command(
                command,
                read_timeout=self.config.read_timeout_seconds,
            )
        except Exception as exc:
            raise WlcOperationError(f"WLC command failed ({type(exc).__name__})") from None
        if not isinstance(output, str):
            raise WlcResponseError("WLC returned an unexpected response type")
        if _contains_error(output):
            raise WlcOperationError("WLC rejected the requested command")
        return output

    def _save(self, connection: WlcConnection) -> None:
        try:
            output = connection.save_config()
        except Exception as exc:
            raise WlcOperationError(
                f"WLC configuration save failed ({type(exc).__name__})"
            ) from None
        if not isinstance(output, str) or _contains_error(output):
            raise WlcOperationError("WLC configuration save was not successful")


def load_wlc_credentials(config: SecretsConfig) -> WlcCredentials:
    return WlcCredentials(
        username=_read_secret_file(config.wlc_username_file, "WLC username"),
        password=_read_secret_file(config.wlc_password_file, "WLC password"),
    )


def parse_wlan_status(output: str, *, expected_wlan_id: int) -> WlanStatus:
    identifier = _extract(_WLAN_ID_PATTERN, output, "WLAN identifier")
    ssid = _extract(_SSID_PATTERN, output, "WLAN SSID")
    status = _extract(_STATUS_PATTERN, output, "WLAN status")
    wlan_id = int(identifier)
    if wlan_id != expected_wlan_id:
        raise WlcResponseError(
            f"WLC returned WLAN {wlan_id} while WLAN {expected_wlan_id} was requested"
        )
    return WlanStatus(
        wlan_id=wlan_id,
        ssid=ssid,
        enabled=status.casefold() == "enabled",
    )


def _read_secret_file(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise WlcConnectionError(f"cannot read {label} file ({type(exc).__name__})") from None
    if not value:
        raise WlcConnectionError(f"{label} file is empty")
    return value


def _extract(pattern: re.Pattern[str], output: str, label: str) -> str:
    match = pattern.search(output)
    if match is None:
        raise WlcResponseError(f"WLC response is missing {label}")
    return match.group("value")


def _contains_error(output: str) -> bool:
    return _ERROR_PATTERN.search(output) is not None


def _validate_ascii_psk(password: str) -> None:
    if not password.isascii() or not 8 <= len(password) <= 63:
        raise WlcOperationError("WPA PSK must contain 8 to 63 ASCII characters")
    if any(character.isspace() for character in password):
        raise WlcOperationError("WPA PSK cannot contain whitespace")
