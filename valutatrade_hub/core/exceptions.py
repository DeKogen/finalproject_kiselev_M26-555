"""Ошибки ValutaTrade Hub."""


def _amount_text(value, digits):
    text = f"{value:.{digits}f}"
    return f"{value:.8g}" if value and float(text) == 0 else text


class ValutaTradeError(Exception):
    """Базовый класс ошибок приложения."""


class InsufficientFundsError(ValutaTradeError):
    """Недостаточно средств на кошельке."""

    def __init__(self, available, required, code, digits=4):
        self.available = available
        self.required = required
        self.code = code
        available_text = _amount_text(available, digits)
        required_text = _amount_text(required, digits)
        if available != required and available_text == required_text:
            available_text = str(available)
            required_text = str(required)
        super().__init__(
            f"Недостаточно средств: доступно {available_text} {code}, "
            f"требуется {required_text} {code}"
        )


class CurrencyNotFoundError(ValutaTradeError):
    """Код валюты отсутствует в реестре."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"Неизвестная валюта '{code}'")


class ApiRequestError(ValutaTradeError):
    """Сбой при обращении к внешнему API."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Ошибка при обращении к внешнему API: {reason}")


class RateNotFoundError(ValutaTradeError):
    """В кеше нет курса для пары валют."""

    def __init__(self, from_code, to_code):
        self.from_code = from_code
        self.to_code = to_code
        super().__init__(f"Курс {from_code}→{to_code} недоступен")


class WalletNotFoundError(ValutaTradeError):
    """В портфеле нет кошелька с таким кодом."""

    def __init__(self, code):
        self.code = code
        super().__init__(
            f"У вас нет кошелька '{code}'. Добавьте валюту: "
            "она создаётся автоматически при первой покупке."
        )


class AuthError(ValutaTradeError):
    """Ошибка регистрации, входа или отсутствующая сессия."""
