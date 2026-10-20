"""Доступ к JSON-файлам в папке data/."""

import json
import threading
from contextlib import contextmanager
from copy import deepcopy

from valutatrade_hub.core.utils import (
    ENCODING,
    JSON_INDENT,
    read_json,
    write_bytes_atomic,
)
from valutatrade_hub.infra.locking import file_lock
from valutatrade_hub.infra.settings import SettingsLoader

DELETED = object()


class DatabaseManager:
    """Единственная точка чтения и записи файлов данных."""

    _instance = None

    # Тот же приём, что и в SettingsLoader: один экземпляр на всё приложение.
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._local = threading.local()
        return cls._instance

    def __init__(self):
        self._settings = SettingsLoader()

    def path(self, name):
        """Путь к файлу: users, portfolios, rates, history, session."""
        return self._settings.data_path(name)

    def read(self, name, default=None):
        """Содержимое файла или default, если файла ещё нет."""
        pending = getattr(self._local, "pending", None)
        if pending is None:
            with self.transaction():
                return self.read(name, default)
        if name in pending:
            value = pending[name]
            return default if value is DELETED else deepcopy(value)
        data = read_json(self.path(name), None)
        return default if data is None else data

    def write(self, name, data):
        """Записать JSON при завершении текущей операции."""
        with self.transaction():
            self._local.pending[name] = deepcopy(data)

    def delete(self, name):
        """Удалить файл при завершении текущей операции."""
        with self.transaction():
            self._local.pending[name] = DELETED

    @contextmanager
    def transaction(self):
        """Защитить чтение и запись; при ошибке отменить изменения."""
        if getattr(self._local, "pending", None) is not None:
            yield self
            return
        with file_lock(self._settings.data_dir / ".database.lock"):
            self._local.pending = {}
            try:
                yield self
                self._commit()
            finally:
                self._local.pending = None

    def _commit(self):
        payloads = {}
        for name, data in self._local.pending.items():
            content = None
            if data is not DELETED:
                text = (
                    json.dumps(
                        data, ensure_ascii=False, indent=JSON_INDENT, allow_nan=False
                    )
                    + "\n"
                )
                content = text.encode(ENCODING)
            payloads[self.path(name)] = content
        originals = {
            path: path.read_bytes() if path.exists() else None for path in payloads
        }
        written = []
        try:
            for path, content in payloads.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(path, content)
                written.append(path)
        except OSError:
            for path in reversed(written):
                content = originals[path]
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(path, content)
            raise
