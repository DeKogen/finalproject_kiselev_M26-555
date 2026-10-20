"""Настройки приложения из секции [tool.valutatrade] файла pyproject.toml."""

import tomllib
from pathlib import Path

CONFIG_FILE = "pyproject.toml"
SECTION = "valutatrade"
DEFAULTS = {
    "data_dir": "data",
    "users_file": "users.json",
    "portfolios_file": "portfolios.json",
    "rates_file": "rates.json",
    "history_file": "exchange_rates.json",
    "session_file": "session.json",
    "rates_ttl_seconds": 300,
    "default_base_currency": "USD",
    "log_dir": "logs",
    "log_level": "INFO",
    "log_format": "text",
    "log_max_bytes": 1024 * 1024,
    "log_backup_count": 3,
}


class SettingsLoader:
    """Единственный на приложение объект с настройками."""

    _instance = None

    # Синглтон через __new__, а не через метакласс: короче, и читается
    # без знания метаклассов. Повторный вызов SettingsLoader() вернёт тот же
    # объект, конфигурация читается один раз.
    def __new__(cls, config_path=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._values = None
        return cls._instance

    def __init__(self, config_path=None):
        if self._values is None or config_path is not None:
            self.reload(config_path)

    def reload(self, config_path=None):
        """Перечитать конфигурацию; без пути берётся pyproject.toml из cwd."""
        path = Path(config_path or CONFIG_FILE)
        values = dict(DEFAULTS)
        if path.is_file():
            with path.open("rb") as stream:
                document = tomllib.load(stream)
            values.update(document.get("tool", {}).get(SECTION, {}))
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)

    @property
    def data_dir(self):
        return Path(self.get("data_dir"))

    @property
    def log_dir(self):
        return Path(self.get("log_dir"))

    def data_path(self, name):
        """Путь к файлу данных по логическому имени: users, rates и т.д."""
        return self.data_dir / self.get(f"{name}_file")
