"""Файлы Parser Service: снимок rates.json и история exchange_rates.json."""

import logging
import re

from valutatrade_hub.core.utils import (
    is_valid_rate,
    parse_iso,
    read_json,
    write_json_atomic,
)
from valutatrade_hub.infra.locking import file_lock
from valutatrade_hub.logging_config import PARSER_LOGGER
from valutatrade_hub.parser_service.config import ParserConfig

logger = logging.getLogger(PARSER_LOGGER)
PAIR_PATTERN = re.compile(r"[A-Z]{2,5}_[A-Z]{2,5}")


def _is_newer(candidate, current):
    new = parse_iso(candidate)
    old = parse_iso(current)
    if new is None:
        return False
    return old is None or new > old


def _records(mapping):
    """Оставить пары с конечным положительным курсом."""
    return {
        key: value
        for key, value in mapping.items()
        if isinstance(key, str)
        and PAIR_PATTERN.fullmatch(key)
        and isinstance(value, dict)
        and is_valid_rate(value.get("rate"))
    }


class RatesStorage:
    """Чтение и атомарная запись файлов курсов."""

    def __init__(self, rates_path=None, history_path=None):
        config = ParserConfig()
        self.rates_path = rates_path or config.RATES_FILE_PATH
        self.history_path = history_path or config.HISTORY_FILE_PATH
        self.lock_path = f"{self.rates_path}.lock"

    def load_snapshot(self):
        """Снимок {"pairs": {...}, "last_refresh": ...}; пустой, если файла нет.

        Поддерживает и плоский формат, где пары лежат на верхнем уровне.
        """
        try:
            data = read_json(self.rates_path, None)
        except ValueError as error:
            logger.warning("%s", error)
            data = None
        if not isinstance(data, dict):
            return {"pairs": {}, "last_refresh": None}
        source = data.get("pairs") if "pairs" in data else data
        pairs = _records(source) if isinstance(source, dict) else {}
        return {"pairs": pairs, "last_refresh": data.get("last_refresh")}

    def save_snapshot(self, pairs, last_refresh):
        """Под блокировкой сохранить только более свежие корректные пары."""
        with file_lock(self.lock_path):
            snapshot, _ = self._save_snapshot(pairs, last_refresh)
            return snapshot

    def _save_snapshot(self, pairs, last_refresh):
        snapshot = self.load_snapshot()
        updated = 0
        for pair, record in _records(pairs).items():
            if parse_iso(record.get("updated_at")) is None:
                continue
            current = snapshot["pairs"].get(pair)
            if current and not _is_newer(
                record.get("updated_at"), current.get("updated_at")
            ):
                continue
            snapshot["pairs"][pair] = record
            updated += 1
        if _is_newer(last_refresh, snapshot["last_refresh"]):
            snapshot["last_refresh"] = last_refresh
        write_json_atomic(self.rates_path, snapshot)
        return snapshot, updated

    def append_history(self, records):
        """Добавить уникальные измерения под общей блокировкой хранилища."""
        with file_lock(self.lock_path):
            return self._append_history(records)

    def _append_history(self, records):
        history = self.load_history()
        known = {item.get("id") for item in history if isinstance(item, dict)}
        added = []
        for record in records:
            if not isinstance(record, dict) or not is_valid_rate(record.get("rate")):
                continue
            pair = f"{record.get('from_currency')}_{record.get('to_currency')}"
            stamp = record.get("timestamp")
            if not PAIR_PATTERN.fullmatch(pair) or parse_iso(stamp) is None:
                continue
            identifier = f"{pair}_{stamp}"
            if record.get("id") != identifier or identifier in known:
                continue
            added.append(record)
            known.add(identifier)
        if added:
            write_json_atomic(self.history_path, history + added)
        return len(added)

    def save_update(self, pairs, last_refresh, history):
        """Сохранить историю и снимок одной критической секцией."""
        with file_lock(self.lock_path):
            self._append_history(history)
            return self._save_snapshot(pairs, last_refresh)

    def load_history(self):
        """История измерений; нечитаемый файл считается пустым."""
        try:
            history = read_json(self.history_path, [])
        except ValueError as error:
            logger.warning("%s", error)
            return []
        return history if isinstance(history, list) else []
