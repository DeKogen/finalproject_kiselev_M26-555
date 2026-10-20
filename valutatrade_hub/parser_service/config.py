"""Параметры Parser Service: ключ API, адреса, списки валют, пути к файлам."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import PARSER_LOGGER

logger = logging.getLogger(PARSER_LOGGER)
ENV_FILE = ".env"
API_KEY_VARIABLE = "EXCHANGERATE_API_KEY"


def load_env_file(path=ENV_FILE):
    """Подхватить переменные из .env, не трогая уже заданные в окружении."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError) as error:
        logger.warning("Файл %s не прочитан: %s", path, error)
        return
    for line in lines:
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not key.strip():
            continue
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _api_key():
    load_env_file()
    return os.getenv(API_KEY_VARIABLE) or None


def _data_file(name):
    return str(SettingsLoader().data_path(name))


@dataclass(frozen=True)
class ParserConfig:
    """Настройки парсера; ключ не хранится в коде, а берётся из окружения."""

    EXCHANGERATE_API_KEY: str | None = field(default_factory=_api_key, repr=False)

    COINGECKO_URL: str = "https://api.coingecko.com/api/v3/simple/price"
    EXCHANGERATE_API_URL: str = "https://v6.exchangerate-api.com/v6"
    # Открытый эндпоинт того же сервиса на случай, когда ключ не задан
    EXCHANGERATE_OPEN_URL: str = "https://open.er-api.com/v6/latest"

    BASE_CURRENCY: str = "USD"
    FIAT_CURRENCIES: tuple = ("EUR", "GBP", "RUB", "JPY", "CNY")
    CRYPTO_CURRENCIES: tuple = ("BTC", "ETH", "SOL", "LTC", "DOGE")
    CRYPTO_ID_MAP: dict = field(
        default_factory=lambda: {
            "BTC": "bitcoin",
            "ETH": "ethereum",
            "SOL": "solana",
            "LTC": "litecoin",
            "DOGE": "dogecoin",
        }
    )

    RATES_FILE_PATH: str = field(default_factory=lambda: _data_file("rates"))
    HISTORY_FILE_PATH: str = field(default_factory=lambda: _data_file("history"))

    REQUEST_TIMEOUT: int = 10
    UPDATE_INTERVAL_SECONDS: int = 300
