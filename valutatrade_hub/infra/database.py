"""Доступ к JSON-файлам в папке data/."""

from valutatrade_hub.core.utils import read_json, write_json_atomic
from valutatrade_hub.infra.settings import SettingsLoader


class DatabaseManager:
    """Единственная точка чтения и записи файлов данных."""

    _instance = None

    # Тот же приём, что и в SettingsLoader: один экземпляр на всё приложение.
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._settings = SettingsLoader()

    def path(self, name):
        """Путь к файлу: users, portfolios, rates, history, session."""
        return self._settings.data_path(name)

    def read(self, name, default=None):
        """Содержимое файла или default, если файла ещё нет."""
        data = read_json(self.path(name), None)
        return default if data is None else data

    def write(self, name, data):
        """Атомарная запись: временный файл, затем замена."""
        write_json_atomic(self.path(name), data)

    def delete(self, name):
        path = self.path(name)
        if path.exists():
            path.unlink()
