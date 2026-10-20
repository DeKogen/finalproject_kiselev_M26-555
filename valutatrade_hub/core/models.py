"""Пользователь, кошелёк и портфель."""

import hashlib
import math
import secrets
from datetime import datetime
from decimal import Decimal, localcontext

from valutatrade_hub.core.exceptions import InsufficientFundsError, WalletNotFoundError
from valutatrade_hub.core.utils import (
    DEFAULT_RATES,
    amount_digits,
    convert,
    format_amount,
    normalize_code,
    validate_amount,
    validate_password,
    validate_username,
)

SALT_BYTES = 8
DECIMAL_PRECISION = 40


class User:
    """Пользователь системы; пароль хранится как SHA-256 от пароля и соли."""

    def __init__(
        self, user_id, username, hashed_password, salt, registration_date=None
    ):
        self._user_id = int(user_id)
        self._username = validate_username(username)
        self._hashed_password = hashed_password
        self._salt = salt
        if isinstance(registration_date, str):
            registration_date = datetime.fromisoformat(registration_date)
        self._registration_date = registration_date or datetime.now().replace(
            microsecond=0
        )

    @classmethod
    def create(cls, user_id, username, password):
        """Новый пользователь со свежей солью."""
        validate_password(password)
        salt = secrets.token_hex(SALT_BYTES)
        return cls(user_id, username, cls.hash_password(password, salt), salt)

    @staticmethod
    def hash_password(password, salt):
        """Односторонний хеш: sha256(password + salt)."""
        return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

    @property
    def user_id(self):
        return self._user_id

    @property
    def username(self):
        return self._username

    @username.setter
    def username(self, value):
        self._username = validate_username(value)

    @property
    def hashed_password(self):
        return self._hashed_password

    @property
    def salt(self):
        return self._salt

    @property
    def registration_date(self):
        return self._registration_date

    def get_user_info(self):
        """Данные пользователя без пароля и соли."""
        return {
            "user_id": self._user_id,
            "username": self._username,
            "registration_date": self._registration_date.isoformat(timespec="seconds"),
        }

    def change_password(self, new_password):
        """Сменить пароль: новая соль и новый хеш."""
        validate_password(new_password)
        self._salt = secrets.token_hex(SALT_BYTES)
        self._hashed_password = self.hash_password(new_password, self._salt)

    def verify_password(self, password):
        """Совпадает ли хеш введённого пароля с сохранённым."""
        return self.hash_password(str(password), self._salt) == self._hashed_password

    def to_dict(self):
        """Запись для users.json с датой в формате ISO."""
        return {
            "user_id": self._user_id,
            "username": self._username,
            "hashed_password": self._hashed_password,
            "salt": self._salt,
            "registration_date": self._registration_date.isoformat(timespec="seconds"),
        }

    @classmethod
    def from_dict(cls, data):
        """Пользователь из записи users.json."""
        try:
            return cls(
                data["user_id"],
                data["username"],
                data["hashed_password"],
                data.get("salt", ""),
                data.get("registration_date"),
            )
        except (KeyError, TypeError, AttributeError):
            raise ValueError("Повреждена запись пользователя в users.json") from None


class Wallet:
    """Кошелёк пользователя для одной валюты."""

    def __init__(self, currency_code, balance=0.0):
        self.currency_code = normalize_code(currency_code)
        self.balance = balance

    @property
    def balance(self):
        return self._balance

    @balance.setter
    def balance(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("Баланс должен быть числом")
        try:
            value = float(value)
        except OverflowError:
            raise ValueError("Баланс не может быть бесконечным") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError("Баланс не может быть отрицательным или бесконечным")
        self._balance = value

    def deposit(self, amount):
        """Пополнить кошелёк на положительную сумму."""
        self._change_balance(self.validate_amount(amount))

    @staticmethod
    def validate_amount(amount):
        """Проверить положительную конечную сумму."""
        return validate_amount(amount)

    def _change_balance(self, amount):
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            updated = Decimal(str(self._balance)) + Decimal(str(amount))
        value = float(updated)
        if not math.isfinite(value):
            raise ValueError("Баланс не может быть бесконечным")
        if value == self._balance:
            raise ValueError("Сумма меньше точности текущего баланса")
        self.balance = value

    def withdraw(self, amount):
        """Списать сумму, если хватает средств."""
        amount = validate_amount(amount)
        if amount > self._balance:
            raise InsufficientFundsError(
                self._balance,
                amount,
                self.currency_code,
                amount_digits(self.currency_code),
            )
        self._change_balance(-amount)

    def get_balance_info(self):
        """Строка вида «BTC: 0.0500»."""
        return (
            f"{self.currency_code}: {format_amount(self._balance, self.currency_code)}"
        )

    def to_dict(self):
        """Запись кошелька для portfolios.json."""
        return {"currency_code": self.currency_code, "balance": self._balance}

    @classmethod
    def from_dict(cls, data, currency_code=None):
        """Кошелёк из JSON; код можно передать отдельно."""
        code = data.get("currency_code", currency_code)
        return cls(code, data.get("balance", 0.0))


class Portfolio:
    """Все кошельки одного пользователя."""

    def __init__(self, user, wallets=None):
        self._user = user
        self._user_id = user.user_id
        self._wallets = {}
        for code, wallet in (wallets or {}).items():
            self._wallets[normalize_code(code)] = wallet

    @property
    def user(self):
        return self._user

    @property
    def user_id(self):
        return self._user_id

    @property
    def wallets(self):
        """Копия словаря кошельков."""
        return dict(self._wallets)

    def add_currency(self, currency_code):
        """Создать кошелёк для новой валюты."""
        code = normalize_code(currency_code)
        if code in self._wallets:
            raise ValueError(f"Кошелёк '{code}' уже есть в портфеле")
        wallet = Wallet(code)
        self._wallets[code] = wallet
        return wallet

    def get_wallet(self, currency_code):
        """Кошелёк по коду валюты или WalletNotFoundError."""
        code = normalize_code(currency_code)
        if code not in self._wallets:
            raise WalletNotFoundError(code)
        return self._wallets[code]

    def get_total_value(self, base_currency="USD", rates=None):
        """Стоимость всех кошельков в базовой валюте по переданным курсам.

        Без rates используются условные курсы DEFAULT_RATES.
        """
        base = normalize_code(base_currency)
        rates = DEFAULT_RATES if rates is None else rates
        total = 0.0
        for code, wallet in self._wallets.items():
            if wallet.balance:
                total += convert(wallet.balance, code, base, rates)
        return total

    def to_dict(self):
        """Запись для portfolios.json."""
        return {
            "user_id": self._user_id,
            "wallets": {code: w.to_dict() for code, w in self._wallets.items()},
        }

    @classmethod
    def from_dict(cls, data, user):
        """Портфель из записи portfolios.json; кошельки могут быть без кода."""
        try:
            wallets = {
                code: Wallet.from_dict(item, code)
                for code, item in data.get("wallets", {}).items()
            }
        except (TypeError, AttributeError):
            raise ValueError("Повреждена запись портфеля в portfolios.json") from None
        return cls(user, wallets)
