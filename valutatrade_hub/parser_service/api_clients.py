"""Клиенты внешних API с единым форматом результата {"BTC_USD": 59337.21}."""

import logging
import time
from abc import ABC, abstractmethod

import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import BASE_CODE, is_valid_rate
from valutatrade_hub.logging_config import PARSER_LOGGER
from valutatrade_hub.parser_service.config import API_KEY_VARIABLE, ParserConfig

logger = logging.getLogger(PARSER_LOGGER)
HTTP_OK = 200
STATUS_MESSAGES = {
    401: "неверный API-ключ (401)",
    403: "доступ запрещён (403)",
    404: "адрес или ключ не найдены (404)",
    429: "превышен лимит запросов (429)",
}
ERROR_TYPES = {
    "invalid-key": "неверный API-ключ",
    "inactive-account": "аккаунт ExchangeRate-API не активирован",
    "quota-reached": "исчерпана квота запросов ExchangeRate-API",
    "unsupported-code": "базовая валюта не поддерживается",
}


class BaseApiClient(ABC):
    """Общий код запроса: таймаут, проверка статуса, разбор JSON."""

    name = "API"

    def __init__(self, config=None):
        self.config = config or ParserConfig()
        self.last_meta = {}

    @abstractmethod
    def fetch_rates(self):
        """Вернуть словарь пар CODE_USD → курс."""

    def _get_json(self, url, params=None):
        """Получить JSON, не раскрывая URL с ключом в сообщениях и логах."""
        started = time.perf_counter()
        try:
            response = requests.get(
                url, params=params, timeout=self.config.REQUEST_TIMEOUT
            )
        except requests.exceptions.Timeout:
            raise ApiRequestError("превышено время ожидания ответа") from None
        except requests.exceptions.RequestException as error:
            logger.debug("%s: %s", self.name, type(error).__name__)
            raise ApiRequestError(f"сетевая ошибка ({type(error).__name__})") from None
        self.last_meta = {
            "request_ms": round((time.perf_counter() - started) * 1000),
            "status_code": response.status_code,
            "etag": response.headers.get("ETag"),
        }
        if response.status_code != HTTP_OK:
            raise ApiRequestError(
                STATUS_MESSAGES.get(
                    response.status_code, f"HTTP {response.status_code}"
                )
            )
        try:
            data = response.json()
        except ValueError:
            raise ApiRequestError("ответ не является JSON") from None
        if not isinstance(data, dict):
            raise ApiRequestError("неожиданный формат ответа")
        return data


class CoinGeckoClient(BaseApiClient):
    """Курсы криптовалют к USD через CoinGecko (ключ не нужен)."""

    name = "CoinGecko"

    def fetch_rates(self):
        config = self.config
        ids = [config.CRYPTO_ID_MAP[code] for code in config.CRYPTO_CURRENCIES]
        vs_currency = BASE_CODE.lower()
        data = self._get_json(
            config.COINGECKO_URL,
            {"ids": ",".join(ids), "vs_currencies": vs_currency},
        )
        rates = {}
        for code in config.CRYPTO_CURRENCIES:
            entry = data.get(config.CRYPTO_ID_MAP[code])
            price = entry.get(vs_currency) if isinstance(entry, dict) else None
            if is_valid_rate(price):
                rates[f"{code}_{BASE_CODE}"] = float(price)
            else:
                logger.warning("CoinGecko: нет цены для %s", code)
        if not rates:
            raise ApiRequestError("ответ не содержит курсов")
        return rates


class ExchangeRateApiClient(BaseApiClient):
    """Курсы фиатных валют через ExchangeRate-API."""

    name = "ExchangeRate-API"

    def _url(self):
        config = self.config
        if config.EXCHANGERATE_API_KEY:
            return (
                f"{config.EXCHANGERATE_API_URL}/{config.EXCHANGERATE_API_KEY}"
                f"/latest/{config.BASE_CURRENCY}"
            )
        logger.warning("%s не задан, используется открытый эндпоинт", API_KEY_VARIABLE)
        return f"{config.EXCHANGERATE_OPEN_URL}/{config.BASE_CURRENCY}"

    def fetch_rates(self):
        data = self._get_json(self._url())
        if data.get("result") != "success":
            error_type = data.get("error-type")
            reason = (
                ERROR_TYPES.get(error_type, "неизвестная ошибка ExchangeRate-API")
                if isinstance(error_type, str)
                else "неожиданный формат ошибки ExchangeRate-API"
            )
            raise ApiRequestError(reason)
        table = data.get("conversion_rates") or data.get("rates") or {}
        if not isinstance(table, dict):
            raise ApiRequestError("неожиданный формат таблицы курсов")
        base = self.config.BASE_CURRENCY
        usd_per_base = 1.0 if base == BASE_CODE else table.get(BASE_CODE)
        if not is_valid_rate(usd_per_base):
            raise ApiRequestError(f"ответ не содержит корректного курса {base}→USD")
        rates = {}
        for code in self.config.FIAT_CURRENCIES:
            value = 1.0 if code == base else table.get(code)
            if not is_valid_rate(value):
                logger.warning("%s: нет курса для %s", self.name, code)
                continue
            rate = float(usd_per_base) / float(value)
            if is_valid_rate(rate):
                rates[f"{code}_{BASE_CODE}"] = rate
            else:
                logger.warning("%s: некорректный курс %s→USD", self.name, code)
        if not rates:
            raise ApiRequestError("ответ не содержит курсов")
        return rates
