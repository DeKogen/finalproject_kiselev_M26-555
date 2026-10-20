"""Файлы Parser Service: снимок rates.json и история exchange_rates.json."""

import logging

from valutatrade_hub.core.utils import parse_iso, read_json, write_json_atomic
from valutatrade_hub.logging_config import PARSER_LOGGER
from valutatrade_hub.parser_service.config import ParserConfig

logger = logging.getLogger(PARSER_LOGGER)


def _is_newer(candidate, current):
    new = parse_iso(candidate)
    old = parse_iso(current)
    if new is None:
        return False
    return old is None or new >= old


def _records(mapping):
    """Оставить только записи вида {"rate": ...}."""
    return {
        key: value
        for key, value in mapping.items()
        if isinstance(value, dict) and "rate" in value
    }


class RatesStorage:
    """Чтение и атомарная запись файлов курсов."""

    def __init__(self, rates_path=None, history_path=None):
        config = ParserConfig()
        self.rates_path = rates_path or config.RATES_FILE_PATH
        self.history_path = history_path or config.HISTORY_FILE_PATH

    def load_snapshot(self):
        """Снимок {"pairs": {...}, "last_refresh": ...}; пустой, если файла нет.

        Понимается и плоский формат из первых версий, где пары лежали
        на верхнем уровне.
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
        """Записать пары, у которых updated_at не старше сохранённого."""
        snapshot = self.load_snapshot()
        for pair, record in pairs.items():
            current = snapshot["pairs"].get(pair)
            if current and not _is_newer(
                record.get("updated_at"), current.get("updated_at")
            ):
                continue
            snapshot["pairs"][pair] = record
        snapshot["last_refresh"] = last_refresh
        write_json_atomic(self.rates_path, snapshot)
        return snapshot

    def append_history(self, records):
        """Добавить измерения в историю, пропуская уже известные id."""
        history = self.load_history()
        known = {item.get("id") for item in history if isinstance(item, dict)}
        added = [record for record in records if record["id"] not in known]
        if added:
            write_json_atomic(self.history_path, history + added)
        return len(added)

    def load_history(self):
        """История измерений; нечитаемый файл считается пустым."""
        try:
            history = read_json(self.history_path, [])
        except ValueError as error:
            logger.warning("%s", error)
            return []
        return history if isinstance(history, list) else []
