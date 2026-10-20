"""Настройка журналов: формат, уровень, ротация и вывод парсера в консоль."""

import json
import logging
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from valutatrade_hub.infra.settings import SettingsLoader

ACTIONS_LOGGER = "valutatrade.actions"
PARSER_LOGGER = "valutatrade.parser"
LOG_FILES = {ACTIONS_LOGGER: "actions.log", PARSER_LOGGER: "parser.log"}
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
TEXT_FORMAT = "%(levelname)s %(asctime)s %(message)s"
CONSOLE_FORMAT = "%(levelname)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """Одна JSON-строка на запись журнала."""

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, ensure_ascii=False, default=str)


def _file_handler(path, settings):
    handler = RotatingFileHandler(
        path,
        maxBytes=int(settings.get("log_max_bytes")),
        backupCount=int(settings.get("log_backup_count")),
        encoding="utf-8",
    )
    if str(settings.get("log_format")).lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT, DATE_FORMAT))
    return handler


def setup_logging():
    """Подключить logs/actions.log и logs/parser.log; повторный вызов безвреден."""
    settings = SettingsLoader()
    log_dir = settings.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    level_name = str(settings.get("log_level")).upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    for name, filename in LOG_FILES.items():
        logger = logging.getLogger(name)
        if logger.handlers:
            continue
        logger.setLevel(level)
        logger.propagate = False
        logger.addHandler(_file_handler(log_dir / filename, settings))


@contextmanager
def parser_console():
    """Дублировать журнал парсера в консоль, пока идёт обновление курсов."""
    logger = logging.getLogger(PARSER_LOGGER)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
