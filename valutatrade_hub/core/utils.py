"""Вспомогательные функции: валидация, форматирование, JSON и даты."""

import json
import math
import os
from datetime import datetime, timezone

from valutatrade_hub.core.currencies import get_currency, is_crypto
from valutatrade_hub.core.exceptions import RateNotFoundError

ENCODING = "utf-8"
JSON_INDENT = 2
MIN_PASSWORD_LENGTH = 4
BASE_CODE = "USD"
AMOUNT_ERROR = "'amount' должен быть положительным числом"
RATE_PRECISION = 8
MIN_AMOUNT = 10**-RATE_PRECISION
MAX_AMOUNT = 10**12
BIG_RATE = 1000
CRYPTO_DIGITS = 4
SIGNIFICANT_DIGITS = 4
FIAT_DIGITS = 2
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
    """Сумма операции: конечное положительное число не меньше 1e-8."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError(AMOUNT_ERROR)
    if not math.isfinite(amount) or amount < MIN_AMOUNT:
        raise ValueError(AMOUNT_ERROR)
    if amount > MAX_AMOUNT:
        raise ValueError(f"'amount' не может быть больше {MAX_AMOUNT:,}")
    return float(amount)


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
    return f"{amount:.{amount_digits(code)}f}"


def format_money(value, code=BASE_CODE):
    """Сумма с разделителем тысяч для итогов и оценок."""
    return f"{value:,.{amount_digits(code)}f}"


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
    return f"{from_code}_{to_code}"


def find_rate(rates, from_code, to_code):
    """Курс from→to через пары X_USD; None, если данных нет."""
    if from_code == to_code:
        return 1.0
    from_usd = (
        1.0 if from_code == BASE_CODE else rates.get(pair_key(from_code, BASE_CODE))
    )
    to_usd = 1.0 if to_code == BASE_CODE else rates.get(pair_key(to_code, BASE_CODE))
    if not from_usd or not to_usd:
        return None
    return from_usd / to_usd


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
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    try:
        with open(temporary, "w", encoding=ENCODING) as stream:
            json.dump(data, stream, ensure_ascii=False, indent=JSON_INDENT)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


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
    return (utc_now() - moment).total_seconds() <= ttl_seconds


def format_moment(text, separator=" "):
    """Метка времени в локальной зоне: дата, разделитель, время."""
    moment = parse_iso(text)
    if moment is None:
        return "нет данных"
    return moment.astimezone().strftime(f"%Y-%m-%d{separator}%H:%M:%S")
