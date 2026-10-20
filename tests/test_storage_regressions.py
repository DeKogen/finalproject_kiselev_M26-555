"""Проверки сохранения данных и запуска CLI."""

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from valutatrade_hub.core import usecases, utils
from valutatrade_hub.core.exceptions import AuthError
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.locking import file_lock
from valutatrade_hub.infra.settings import SettingsLoader

PROJECT = Path(__file__).resolve().parents[1]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.directory = tempfile.TemporaryDirectory(prefix="valutatrade-storage-")
        os.chdir(self.directory.name)
        SettingsLoader().reload()
        usecases.reset_rates_cache()
        self.db = DatabaseManager()

    def tearDown(self):
        os.chdir(self.previous)
        SettingsLoader().reload()
        usecases.reset_rates_cache()
        self.directory.cleanup()

    def test_failed_transaction_leaves_existing_files_unchanged(self):
        self.db.write("users", [{"user_id": 1}])
        original = self.db.path("users").read_bytes()
        with self.assertRaises(ValueError), self.db.transaction():
            self.db.write("users", [{"user_id": 2}])
            with self.db.transaction():
                self.db.write("portfolios", [])
                self.assertEqual(self.db.read("users"), [{"user_id": 2}])
            self.assertEqual(self.db.path("users").read_bytes(), original)
            raise ValueError("abort")
        self.assertEqual(self.db.path("users").read_bytes(), original)
        self.assertFalse(self.db.path("portfolios").exists())

    def test_registration_rolls_back_after_second_file_write_fails(self):
        self.db.write("users", [])
        self.db.write("portfolios", [])
        before = {
            name: self.db.path(name).read_bytes() for name in ("users", "portfolios")
        }
        calls = 0

        def fail_second_write(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk failure")
            utils.write_bytes_atomic(path, content)

        with patch(
            "valutatrade_hub.infra.database.write_bytes_atomic",
            side_effect=fail_second_write,
        ):
            with self.assertRaises(OSError):
                usecases.register("alice", "1234")
        for name, content in before.items():
            self.assertEqual(self.db.path(name).read_bytes(), content)
        self.assertEqual(usecases.register("alice", "1234").user_id, 1)

    def test_login_cannot_read_a_registration_that_will_roll_back(self):
        self.db.write("users", [])
        self.db.write("portfolios", [])
        users_written = threading.Event()
        reader_at_lock = threading.Event()
        release_writer = threading.Event()
        results = {}

        def fail_portfolio_write(path, content):
            if path == self.db.path("portfolios"):
                users_written.set()
                if not release_writer.wait(5):
                    raise TimeoutError("writer barrier")
                raise OSError("portfolio write failed")
            utils.write_bytes_atomic(path, content)

        @contextmanager
        def observed_lock(path):
            if threading.current_thread().name == "login-reader":
                reader_at_lock.set()
            with file_lock(path):
                yield

        def run(name, action):
            try:
                results[name] = action("alice", "1234")
            except Exception as error:
                results[name] = error

        writer = threading.Thread(
            target=run,
            args=("register", usecases.register),
            name="registration-writer",
        )
        reader = threading.Thread(
            target=run,
            args=("login", usecases.login),
            name="login-reader",
        )
        with (
            patch(
                "valutatrade_hub.infra.database.write_bytes_atomic",
                side_effect=fail_portfolio_write,
            ),
            patch(
                "valutatrade_hub.infra.database.file_lock",
                side_effect=observed_lock,
            ),
        ):
            try:
                writer.start()
                self.assertTrue(users_written.wait(5), "writer did not reach barrier")
                reader.start()
                self.assertTrue(reader_at_lock.wait(5), "reader did not reach lock")
            finally:
                release_writer.set()
                writer.join(timeout=12)
                if reader.ident is not None:
                    reader.join(timeout=12)
            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
        self.assertIsInstance(results["register"], OSError)
        self.assertIsInstance(results["login"], AuthError)
        self.assertEqual(self.db.read("users"), [])
        self.assertEqual(self.db.read("portfolios"), [])
        self.assertFalse(self.db.path("session").exists())

    def test_invalid_json_value_does_not_replace_existing_file(self):
        utils.write_json_atomic("rates.json", {"rate": 2})
        before = Path("rates.json").read_bytes()
        with self.assertRaises(ValueError):
            utils.write_json_atomic("rates.json", {"rate": float("inf")})
        self.assertEqual(Path("rates.json").read_bytes(), before)
        self.assertFalse(list(Path(".").glob("*.tmp")))

    def test_atomic_writers_use_separate_temporary_files(self):
        def write(index):
            utils.write_json_atomic(
                "shared.json", {"writer": index, "text": "x" * 1000}
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(24)))
        stored = utils.read_json("shared.json")
        self.assertIn(stored["writer"], range(24))
        self.assertEqual(stored["text"], "x" * 1000)
        self.assertFalse(list(Path(".").glob("*.tmp")))

    def test_parallel_deposits_do_not_lose_money(self):
        user = usecases.register("alice", "1234")
        code = (
            "from valutatrade_hub.core import usecases; "
            "[usecases.deposit(1, 1) for _ in range(15)]"
        )
        environment = dict(os.environ, PYTHONPATH=str(PROJECT))
        children = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", code],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            for _ in range(2)
        ]
        try:
            for child in children:
                output, error = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, output + error)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        self.assertEqual(usecases.load_portfolio(user).get_wallet("USD").balance, 30)

    def test_cli_processes_return_exit_codes_and_preserve_session(self):
        environment = dict(os.environ, PYTHONPATH=str(PROJECT))

        def run(*args):
            return subprocess.run(
                [sys.executable, "-B", "-m", "valutatrade_hub", *args],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )

        result = run("deposit", "--amount", "10")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Сначала выполните login", result.stdout)
        self.assertEqual(
            run("register", "--username", "alice", "--password", "1234").returncode, 0
        )
        self.assertEqual(
            run("login", "--username", "alice", "--password", "1234").returncode, 0
        )
        result = run("deposit", "--amount", "10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Счёт пополнен: 10.00 USD", result.stdout)
        self.assertEqual(run("buy", "--currency", "BTC").returncode, 1)
        self.assertEqual(run("--help").returncode, 0)


if __name__ == "__main__":
    unittest.main()
