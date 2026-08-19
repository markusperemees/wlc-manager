from pathlib import Path

import pytest

from wlc_manager.database import Database, PasswordRepository
from wlc_manager.passwords import PasswordGenerationError, PasswordGenerator, load_dictionary
from wlc_manager.scheduling import YearMonth


class FirstRandom:
    def choice(self, values: list[str]) -> str:
        return values[0]

    def randrange(self, stop: int) -> int:
        assert stop == 1000
        return 7


def test_dictionary_ignores_non_ascii_and_duplicate_entries(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.txt"
    path.write_text(
        "apple\npää\npear\nbad word\nBANANA\nbanana\n123\n\ntrailing \n",
        encoding="utf-8",
    )

    result = load_dictionary(path)

    assert result.words == ("apple", "pear", "BANANA")
    assert result.stats.total_entries == 9
    assert result.stats.valid_entries == 3
    assert result.stats.invalid_entries == 4
    assert result.stats.blank_entries == 1
    assert result.stats.duplicate_entries == 1
    assert result.stats.skipped_entries == 6


def test_dictionary_with_no_valid_words_fails_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.txt"
    path.write_text("pää\nbad word\n123\n", encoding="utf-8")

    with pytest.raises(PasswordGenerationError, match="no valid ASCII-only words"):
        load_dictionary(path)


def test_generator_blocks_recent_words_case_insensitively_and_pads_number(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    repository = PasswordRepository(database)
    repository.create(
        validity_month="2026-08",
        password="markus123APPLE",
        dictionary_word="APPLE",
        run_id="old-run",
    )
    dictionary = tmp_path / "dictionary.txt"
    dictionary.write_text("apple\npear\n", encoding="utf-8")
    generator = PasswordGenerator(
        repository,
        prefix="markus",
        random_digits=3,
        history_size=12,
        random_source=FirstRandom(),
    )

    result = generator.generate(
        period=YearMonth(2026, 9),
        dictionary_path=dictionary,
        run_id="new-run",
    )

    assert result.created
    assert result.record.password == "markus007pear"
    assert result.record.dictionary_word == "pear"
    assert result.eligible_word_count == 1
    assert "markus007pear" not in repr(result.record)


def test_generation_is_idempotent_for_same_month(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    dictionary = tmp_path / "dictionary.txt"
    dictionary.write_text("apple\npear\n", encoding="utf-8")
    generator = PasswordGenerator(
        PasswordRepository(database),
        prefix="markus",
        random_source=FirstRandom(),
    )

    first = generator.generate(
        period=YearMonth(2026, 9), dictionary_path=dictionary, run_id="run-1"
    )
    second = generator.generate(
        period=YearMonth(2026, 9), dictionary_path=dictionary, run_id="run-2"
    )

    assert first.created
    assert not second.created
    assert second.record == first.record
    assert second.dictionary_stats is None


def test_generation_fails_when_all_words_are_in_recent_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "manager.db")
    database.migrate()
    repository = PasswordRepository(database)
    repository.create(
        validity_month="2026-08",
        password="markus123apple",
        dictionary_word="apple",
        run_id="old-run",
    )
    dictionary = tmp_path / "dictionary.txt"
    dictionary.write_text("APPLE\n", encoding="utf-8")
    generator = PasswordGenerator(
        repository,
        prefix="markus",
        random_source=FirstRandom(),
    )

    with pytest.raises(PasswordGenerationError, match="no unused ASCII-only word"):
        generator.generate(
            period=YearMonth(2026, 9),
            dictionary_path=dictionary,
            run_id="new-run",
        )
