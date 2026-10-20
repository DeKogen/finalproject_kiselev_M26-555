"""Иерархия валют и реестр поддерживаемых кодов."""

import re
from abc import ABC, abstractmethod

from valutatrade_hub.core.exceptions import CurrencyNotFoundError

CODE_PATTERN = re.compile(r"^[A-Z0-9]{2,5}$")


class Currency(ABC):
    """Валюта с кодом и названием."""

    def __init__(self, name, code):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Название валюты не может быть пустым")
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            raise ValueError(
                f"Код '{code}' должен состоять из 2–5 заглавных букв или цифр"
            )
        self.name = name.strip()
        self.code = code

    @abstractmethod
    def get_display_info(self):
        """Строка для интерфейса и логов."""

    def __repr__(self):
        return f"{type(self).__name__}({self.code!r})"


class FiatCurrency(Currency):
    """Фиатная валюта с указанием эмитента."""

    def __init__(self, name, code, issuing_country):
        super().__init__(name, code)
        self.issuing_country = issuing_country

    def get_display_info(self):
        """Код, название и страна выпуска."""
        return f"[FIAT] {self.code} — {self.name} (Issuing: {self.issuing_country})"


class CryptoCurrency(Currency):
    """Криптовалюта с алгоритмом и последней известной капитализацией."""

    def __init__(self, name, code, algorithm, market_cap):
        super().__init__(name, code)
        self.algorithm = algorithm
        self.market_cap = float(market_cap)

    def get_display_info(self):
        """Код, название, алгоритм и капитализация."""
        market_cap = f"{self.market_cap:.2e}".replace("e+", "e")
        return (
            f"[CRYPTO] {self.code} — {self.name} "
            f"(Algo: {self.algorithm}, MCAP: {market_cap})"
        )


_CURRENCIES = (
    FiatCurrency("US Dollar", "USD", "United States"),
    FiatCurrency("Euro", "EUR", "Eurozone"),
    FiatCurrency("British Pound", "GBP", "United Kingdom"),
    FiatCurrency("Russian Ruble", "RUB", "Russia"),
    FiatCurrency("Japanese Yen", "JPY", "Japan"),
    FiatCurrency("Chinese Yuan", "CNY", "China"),
    CryptoCurrency("Bitcoin", "BTC", "SHA-256", 1.6e12),
    CryptoCurrency("Ethereum", "ETH", "Ethash", 2.9e11),
    CryptoCurrency("Solana", "SOL", "PoH", 5.7e10),
    CryptoCurrency("Litecoin", "LTC", "Scrypt", 6.1e9),
    CryptoCurrency("Dogecoin", "DOGE", "Scrypt", 1.5e10),
)
REGISTRY = {currency.code: currency for currency in _CURRENCIES}


def get_currency(code):
    """Вернуть валюту из реестра или бросить CurrencyNotFoundError."""
    key = code.strip().upper() if isinstance(code, str) else None
    if key not in REGISTRY:
        raise CurrencyNotFoundError(code)
    return REGISTRY[key]


def supported_codes():
    """Поддерживаемые коды в алфавитном порядке."""
    return sorted(REGISTRY)


def is_crypto(code):
    """Относится ли код к криптовалютам."""
    return isinstance(REGISTRY.get(code), CryptoCurrency)
