"""Логирование операций и кэширование результатов."""

import functools
import inspect
import logging
import time
from decimal import Decimal

from valutatrade_hub.core.utils import format_rate
from valutatrade_hub.logging_config import ACTIONS_LOGGER

logger = logging.getLogger(ACTIONS_LOGGER)
QUOTED_KEYS = ("user", "currency", "base", "error_message")


def _format_number(value):
    number = Decimal(str(value))
    if not number.is_finite():
        return str(value)
    whole, _, fraction = format(number, "f").partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(4, '0')}"


def _format_value(key, value):
    if key == "rate":
        return format_rate(value)
    if isinstance(value, float):
        return _format_number(value)
    if isinstance(value, str) and key in QUOTED_KEYS:
        return f"'{value}'"
    return str(value)


def _format_line(action, fields):
    parts = [action]
    for key, value in fields.items():
        if value is not None:
            parts.append(f"{key}={_format_value(key, value)}")
    return " ".join(parts)


def log_action(action, verbose=False):
    """Записать параметры и результат операции в лог.

    Ошибки тоже записываются и передаются вызывающему коду.
    verbose=True добавляет баланс до и после операции.
    """

    def decorator(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            arguments = signature.bind_partial(*args, **kwargs).arguments
            fields = {
                "user": arguments.get("username", arguments.get("user_id")),
                "currency": arguments.get("currency_code"),
                "amount": arguments.get("amount"),
                "rate": None,
                "base": None,
            }
            try:
                result = func(*args, **kwargs)
            except Exception as error:
                fields["result"] = "ERROR"
                fields["error_type"] = type(error).__name__
                fields["error_message"] = str(error)
                logger.info(
                    _format_line(action, fields),
                    extra={"fields": {"action": action, **fields}},
                )
                raise
            fields["user"] = getattr(result, "username", fields["user"])
            fields["currency"] = getattr(result, "currency_code", fields["currency"])
            fields["rate"] = getattr(result, "rate", None)
            fields["base"] = getattr(result, "base", None)
            if verbose and hasattr(result, "old_balance"):
                fields["balance"] = (
                    f"{_format_number(result.old_balance)}"
                    f"->{_format_number(result.new_balance)}"
                )
            fields["result"] = "OK"
            logger.info(
                _format_line(action, fields),
                extra={"fields": {"action": action, **fields}},
            )
            return result

        return wrapper

    return decorator


def cached(ttl_seconds):
    """Кэшировать результат на ttl_seconds секунд в замыкании.

    Метод cache_clear() сбрасывает кэш.
    """

    def decorator(func):
        storage = {}

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            entry = storage.get(key)
            if entry and time.monotonic() - entry[0] < ttl_seconds:
                return entry[1]
            value = func(*args, **kwargs)
            storage[key] = (time.monotonic(), value)
            return value

        wrapper.cache_clear = storage.clear
        return wrapper

    return decorator
