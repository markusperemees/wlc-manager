from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from wlc_manager.database import PasswordRepository, PasswordState
from wlc_manager.scheduling import YearMonth, password_application_is_due
from wlc_manager.wlc import ManagedWlc


class PasswordApplicationError(RuntimeError):
    """Raised when a stored password is not eligible for WLC application."""


class PasswordApplicationOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    NOT_DUE = "not_due"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class PasswordApplicationResult:
    period: YearMonth
    outcome: PasswordApplicationOutcome
    wlan_id: int | None = None
    wlan_was_enabled: bool | None = None
    wlan_is_enabled: bool | None = None


class PasswordApplicationService:
    def __init__(
        self,
        repository: PasswordRepository,
        controller_factory: Callable[[], ManagedWlc],
    ) -> None:
        self.repository = repository
        self.controller_factory = controller_factory

    def apply(
        self,
        period: YearMonth,
        *,
        today: date,
        allow_early: bool = False,
    ) -> PasswordApplicationResult:
        record = self.repository.get_by_month(str(period))
        if record is None:
            raise PasswordApplicationError(f"password record does not exist for {period}")
        if record.state is PasswordState.APPLIED:
            return PasswordApplicationResult(
                period=period,
                outcome=PasswordApplicationOutcome.ALREADY_APPLIED,
            )
        if not allow_early and not password_application_is_due(today, period):
            return PasswordApplicationResult(
                period=period,
                outcome=PasswordApplicationOutcome.NOT_DUE,
            )
        if record.state is not PasswordState.NOTIFIED:
            raise PasswordApplicationError(
                f"password application requires notified state for {period}"
            )

        update = self.controller_factory().update_psk(record.password)
        self.repository.mark_applied(str(period))
        return PasswordApplicationResult(
            period=period,
            outcome=PasswordApplicationOutcome.APPLIED,
            wlan_id=update.wlan_id,
            wlan_was_enabled=update.wlan_was_enabled,
            wlan_is_enabled=update.wlan_is_enabled,
        )

    def reconcile(self, *, today: date) -> PasswordApplicationResult:
        period = YearMonth.from_date(today)
        record = self.repository.get_by_month(str(period))
        if record is None or record.state not in {
            PasswordState.NOTIFIED,
            PasswordState.APPLIED,
        }:
            return PasswordApplicationResult(
                period=period,
                outcome=PasswordApplicationOutcome.NOT_READY,
            )
        return self.apply(period, today=today)
