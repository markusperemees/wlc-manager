from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from wlc_manager.database import PasswordRepository
from wlc_manager.passwords import GenerationResult, PasswordGenerator
from wlc_manager.scheduling import YearMonth, generation_is_due


@dataclass(frozen=True, slots=True)
class PeriodReconciliation:
    period: YearMonth
    result: GenerationResult


@dataclass(frozen=True, slots=True)
class MonthlyReconciliationResult:
    checked_periods: tuple[YearMonth, ...]
    reconciled_periods: tuple[PeriodReconciliation, ...]


class MonthlyPasswordReconciler:
    """Ensure current and due next-month password records exist."""

    def __init__(
        self,
        repository: PasswordRepository,
        generator: PasswordGenerator,
        *,
        dictionary_path: Path,
    ) -> None:
        self.repository = repository
        self.generator = generator
        self.dictionary_path = dictionary_path

    def reconcile(self, *, today: date, run_id: str) -> MonthlyReconciliationResult:
        current = YearMonth.from_date(today)
        candidates = (current, current.next())
        checked: list[YearMonth] = []
        reconciled: list[PeriodReconciliation] = []

        for period in candidates:
            if not generation_is_due(today, period):
                continue
            checked.append(period)
            if self.repository.get_by_month(str(period)) is not None:
                continue
            result = self.generator.generate(
                period=period,
                dictionary_path=self.dictionary_path,
                run_id=run_id,
            )
            reconciled.append(PeriodReconciliation(period=period, result=result))

        return MonthlyReconciliationResult(
            checked_periods=tuple(checked),
            reconciled_periods=tuple(reconciled),
        )
