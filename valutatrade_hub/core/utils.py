"""Вспомогательные функции: валидация, форматирование, JSON и даты."""

import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from valutatrade_hub.core.currencies import get_currency, is_crypto
from valutatrade_hub.core.exceptions import RateNotFoundError

ENCODING = "utf-8"
JSON_INDENT = 2
MIN_PASSWORD_LENGTH = 4
BASE_CODE = "USD"
AMOUNT_ERROR = "'amount' должен быть положительным числом"
RATE_PRECISION = 8
BIG_RATE = 1000
CRYPTO_DIGITS = 4
SIGNIFICANT_DIGITS = 4
FIAT_DIGITS = 2
REPLACE_TIMEOUT_SECONDS = 2
REPLACE_RETRY_SECONDS = 0.01
DEFAULT_RATES = {
    "EUR_USD": 1.0786,
    "GBP_USD": 1.27,
    "RUB_USD": 0.01016,
    "JPY_USD": 0.0067,
    "CNY_USD": 0.138,
    "BTC_USD": 59337.21,
    "ETH_USD": 3720.0,
    "SOL_USD": 145.12,
    "LTC_USD": 70.0,
    "DOGE_USD": 0.11,
}


def normalize_code(code):
    """Привести код валюты к верхнему регистру без пробелов."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("Код валюты не может быть пустым")
    return code.strip().upper()


def validate_currency_code(code):
    """Нормализовать код и убедиться, что валюта есть в реестре."""
    return get_currency(normalize_code(code)).code


def validate_amount(amount):
    """Сумма операции: конечное положительное число."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError(AMOUNT_ERROR)
    try:
        amount = float(amount)
    except OverflowError:
        raise ValueError(AMOUNT_ERROR) from None
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(AMOUNT_ERROR)
    return amount


def is_valid_rate(value):
    """Положительный конечный курс, включая корректный обратный курс."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return (
            math.isfinite(value)
            and value > 0
            and math.isfinite(1 / value)
            and 1 / value > 0
        )
    except (OverflowError, ZeroDivisionError):
        return False


def parse_amount(text):
    """Разобрать сумму из аргумента командной строки."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError(AMOUNT_ERROR) from None
    return validate_amount(value)


def validate_username(username):
    """Имя пользователя без внешних пробелов, не пустое."""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Имя пользователя не может быть пустым")
    return username.strip()


def validate_password(password):
    """Пароль не короче MIN_PASSWORD_LENGTH символов."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    return password


def amount_digits(code):
    """Крипта показывается с четырьмя знаками после запятой, фиат с двумя."""
    return CRYPTO_DIGITS if is_crypto(code) else FIAT_DIGITS


def format_amount(amount, code):
    """Баланс с точностью вывода для указанной валюты."""
    digits = amount_digits(code)
    if amount and round(amount, digits) == 0:
        if abs(amount) < 10**-RATE_PRECISION:
            return str(amount)
        return f"{amount:.{RATE_PRECISION}f}".rstrip("0")
    return f"{amount:.{digits}f}"


def format_money(value, code=BASE_CODE):
    """Сумма с разделителем тысяч для итогов и оценок."""
    digits = amount_digits(code)
    if value and round(value, digits) == 0:
        if abs(value) < 10**-RATE_PRECISION:
            return str(value)
        return f"{value:,.{RATE_PRECISION}f}".rstrip("0")
    return f"{value:,.{digits}f}"


def format_rate(rate):
    """Крупные курсы с двумя знаками, мелкие — не меньше четырёх значащих цифр."""
    if rate >= BIG_RATE:
        return f"{rate:.2f}"
    if rate >= 1:
        return f"{rate:.4f}"
    if rate <= 0:
        return f"{rate:.4f}"
    decimals = max(
        RATE_PRECISION, SIGNIFICANT_DIGITS - 1 - math.floor(math.log10(rate))
    )
    return f"{rate:.{decimals}f}".rstrip("0").rstrip(".")


def pair_key(from_code, to_code):
    """Ключ пары для JSON-хранилища."""
    return f"{from_code}_{to_code}"


def find_rate(rates, from_code, to_code):
    """Курс from→to через пары X_USD; None, если данных нет."""
    if from_code == to_code:
        return 1.0
    from_usd = (
        1.0 if from_code == BASE_CODE else rates.get(pair_key(from_code, BASE_CODE))
    )
    to_usd = 1.0 if to_code == BASE_CODE else rates.get(pair_key(to_code, BASE_CODE))
    if not is_valid_rate(from_usd) or not is_valid_rate(to_usd):
        return None
    rate = from_usd / to_usd
    return rate if is_valid_rate(rate) else None


def convert(amount, from_code, to_code, rates):
    """Пересчитать сумму в другую валюту или бросить RateNotFoundError."""
    rate = find_rate(rates, from_code, to_code)
    if rate is None:
        raise RateNotFoundError(from_code, to_code)
    return amount * rate


def read_json(path, default=None):
    """Прочитать JSON; отсутствующий или пустой файл даёт default."""
    try:
        with open(path, encoding=ENCODING) as stream:
            text = stream.read()
    except FileNotFoundError:
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Повреждён файл {path}: {error}") from None


def write_json_atomic(path, data):
    """Записать во временный файл и заменить им целевой."""
    text = (
        json.dumps(data, ensure_ascii=False, indent=JSON_INDENT, allow_nan=False) + "\n"
    )
    write_bytes_atomic(path, text.encode(ENCODING))


def write_bytes_atomic(path, content):
    """Заменить файл, используя отдельный временный файл в той же папке."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + REPLACE_TIMEOUT_SECONDS
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(REPLACE_RETRY_SECONDS)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def utc_now():
    """Текущее время UTC."""
    return datetime.now(timezone.utc)


def to_iso(moment):
    """ISO 8601 в UTC с суффиксом Z."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(text):
    """Разобрать ISO-строку; наивное время считается UTC."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def is_fresh(text, ttl_seconds):
    """Моложе ли метка времени заданного TTL."""
    moment = parse_iso(text)
    if moment is None:
        return False
    return 0 <= (utc_now() - moment).total_seconds() <= ttl_seconds


def format_moment(text, separator=" "):
    """Метка времени в локальной зоне: дата, разделитель, время."""
    moment = parse_iso(text)
    if moment is None:
        return "нет данных"
    return moment.astimezone().strftime(f"%Y-%m-%d{separator}%H:%M:%S")
