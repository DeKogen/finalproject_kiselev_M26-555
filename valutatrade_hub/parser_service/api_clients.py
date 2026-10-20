"""Клиенты внешних API с единым форматом результата {"BTC_USD": 59337.21}."""

import logging
import time
from abc import ABC, abstractmethod

import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import RATE_PRECISION
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
        """GET-запрос с таймаутом; любая проблема превращается в ApiRequestError.

        Полный текст сетевой ошибки содержит адрес запроса (а в нём ключ),
        поэтому в сообщение попадает только класс ошибки, остальное — в журнал
        на уровне DEBUG.
        """
        started = time.perf_counter()
        try:
            response = requests.get(
                url, params=params, timeout=self.config.REQUEST_TIMEOUT
            )
        except requests.exceptions.Timeout:
            raise ApiRequestError("превышено время ожидания ответа") from None
        except requests.exceptions.RequestException as error:
            logger.debug("%s: %s", self.name, error)
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
        vs_currency = config.BASE_CURRENCY.lower()
        data = self._get_json(
            config.COINGECKO_URL,
            {"ids": ",".join(ids), "vs_currencies": vs_currency},
        )
        rates = {}
        for code in config.CRYPTO_CURRENCIES:
            entry = data.get(config.CRYPTO_ID_MAP[code])
            price = entry.get(vs_currency) if isinstance(entry, dict) else None
            if isinstance(price, (int, float)) and price > 0:
                rates[f"{code}_{config.BASE_CURRENCY}"] = float(price)
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
            error_type = str(data.get("error-type", "неизвестная ошибка"))
            raise ApiRequestError(ERROR_TYPES.get(error_type, error_type))
        table = data.get("conversion_rates") or data.get("rates") or {}
        base = self.config.BASE_CURRENCY
        rates = {}
        for code in self.config.FIAT_CURRENCIES:
            value = table.get(code)
            if isinstance(value, (int, float)) and value > 0:
                # API отдаёт USD→CODE, в кеше храним CODE→USD
                rates[f"{code}_{base}"] = round(1 / float(value), RATE_PRECISION)
            else:
                logger.warning("%s: нет курса для %s", self.name, code)
        if not rates:
            raise ApiRequestError("ответ не содержит курсов")
        return rates
