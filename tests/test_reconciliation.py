from datetime import date
from pathlib import Path

from wlc_manager.database import Database, PasswordRepository
from wlc_manager.passwords import PasswordGenerator
from wlc_manager.reconciliation import MonthlyPasswordReconciler


class FirstRandom:
    def choice(self, values: list[str]) -> str:
        return values[0]

    def randrange(self, stop: int) -> int:
        return 123


def _reconciler(tmp_path: Path) -> tuple[MonthlyPasswordReconciler, PasswordRepository]:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    repository = PasswordRepository(database)
    dictionary = tmp_path / "dictionary.txt"
    dictionary.write_text("apple\npear\nplum\n", encoding="utf-8")
    generator = PasswordGenerator(repository, prefix="markus", random_source=FirstRandom())
    return (
        MonthlyPasswordReconciler(
            repository,
            generator,
            dictionary_path=dictionary,
        ),
        repository,
    )


def test_reconcile_generates_current_month_on_startup(tmp_path: Path) -> None:
    reconciler, repository = _reconciler(tmp_path)

    result = reconciler.reconcile(today=date(2026, 8, 19), run_id="run-1")

    assert [str(item) for item in result.checked_periods] == ["2026-08"]
    assert [str(item.period) for item in result.reconciled_periods] == ["2026-08"]
    assert repository.get_by_month("2026-08") is not None
    assert repository.get_by_month("2026-09") is None


def test_reconcile_generates_next_month_when_lead_date_is_reached(tmp_path: Path) -> None:
    reconciler, repository = _reconciler(tmp_path)

    first = reconciler.reconcile(today=date(2026, 8, 27), run_id="run-1")
    second = reconciler.reconcile(today=date(2026, 8, 27), run_id="run-2")

    assert [str(item) for item in first.checked_periods] == ["2026-08", "2026-09"]
    assert [str(item.period) for item in first.reconciled_periods] == ["2026-08", "2026-09"]
    assert second.reconciled_periods == ()
    assert repository.recent_dictionary_words(2) == ["pear", "apple"]
