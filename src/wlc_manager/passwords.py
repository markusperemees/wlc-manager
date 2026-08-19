from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from wlc_manager.database import DatabaseError, PasswordRecord, PasswordRepository
from wlc_manager.scheduling import YearMonth

_ASCII_WORD = re.compile(r"^[A-Za-z]+$", re.ASCII)
_MIN_WPA_PASSPHRASE_LENGTH = 8
_MAX_WPA_PASSPHRASE_LENGTH = 63


class PasswordGenerationError(RuntimeError):
    """Raised when a safe password cannot be generated."""


class RandomSource(Protocol):
    def choice(self, values: list[str]) -> str: ...

    def randrange(self, stop: int) -> int: ...


@dataclass(frozen=True, slots=True)
class DictionaryStats:
    total_entries: int
    valid_entries: int
    invalid_entries: int
    blank_entries: int
    duplicate_entries: int

    @property
    def skipped_entries(self) -> int:
        return self.invalid_entries + self.blank_entries + self.duplicate_entries


@dataclass(frozen=True, slots=True)
class DictionaryWords:
    words: tuple[str, ...]
    stats: DictionaryStats


@dataclass(frozen=True, slots=True)
class GenerationResult:
    record: PasswordRecord
    created: bool
    dictionary_stats: DictionaryStats | None
    eligible_word_count: int | None


class PasswordGenerator:
    def __init__(
        self,
        repository: PasswordRepository,
        *,
        prefix: str,
        random_digits: int = 3,
        history_size: int = 12,
        random_source: RandomSource | None = None,
    ) -> None:
        self.repository = repository
        self.prefix = prefix
        self.random_digits = random_digits
        self.history_size = history_size
        self.random_source = random_source or secrets.SystemRandom()

    def generate(
        self,
        *,
        period: YearMonth,
        dictionary_path: Path,
        run_id: str,
    ) -> GenerationResult:
        period_text = str(period)
        existing = self.repository.get_by_month(period_text)
        if existing is not None:
            return GenerationResult(
                record=existing,
                created=False,
                dictionary_stats=None,
                eligible_word_count=None,
            )

        dictionary = load_dictionary(dictionary_path)
        blocked = {
            word.casefold()
            for word in self.repository.recent_dictionary_words(limit=self.history_size)
        }
        eligible = [
            word
            for word in dictionary.words
            if word.casefold() not in blocked and self._has_valid_password_length(word)
        ]
        if not eligible:
            raise PasswordGenerationError(
                "dictionary has no unused ASCII-only word that produces a valid passphrase"
            )

        word = self.random_source.choice(eligible)
        number = self.random_source.randrange(10**self.random_digits)
        password = f"{self.prefix}{number:0{self.random_digits}d}{word}"
        try:
            record = self.repository.create(
                validity_month=period_text,
                password=password,
                dictionary_word=word,
                run_id=run_id,
            )
        except DatabaseError:
            # A concurrent idempotent run may have inserted the same month first.
            existing = self.repository.get_by_month(period_text)
            if existing is None:
                raise
            return GenerationResult(
                record=existing,
                created=False,
                dictionary_stats=dictionary.stats,
                eligible_word_count=len(eligible),
            )

        return GenerationResult(
            record=record,
            created=True,
            dictionary_stats=dictionary.stats,
            eligible_word_count=len(eligible),
        )

    def _has_valid_password_length(self, word: str) -> bool:
        length = len(self.prefix) + self.random_digits + len(word)
        return _MIN_WPA_PASSPHRASE_LENGTH <= length <= _MAX_WPA_PASSPHRASE_LENGTH


def load_dictionary(path: Path) -> DictionaryWords:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise PasswordGenerationError(f"cannot read UTF-8 dictionary {path}: {exc}") from exc

    words: list[str] = []
    seen: set[str] = set()
    invalid_entries = 0
    blank_entries = 0
    duplicate_entries = 0
    entries = content.splitlines()

    for entry in entries:
        if not entry:
            blank_entries += 1
            continue
        if _ASCII_WORD.fullmatch(entry) is None:
            invalid_entries += 1
            continue
        normalized = entry.casefold()
        if normalized in seen:
            duplicate_entries += 1
            continue
        seen.add(normalized)
        words.append(entry)

    stats = DictionaryStats(
        total_entries=len(entries),
        valid_entries=len(words),
        invalid_entries=invalid_entries,
        blank_entries=blank_entries,
        duplicate_entries=duplicate_entries,
    )
    if not words:
        raise PasswordGenerationError("dictionary contains no valid ASCII-only words")
    return DictionaryWords(words=tuple(words), stats=stats)
