"""Проверки запуска локального стенда."""

import importlib
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch


class LocalLaunchTests(unittest.TestCase):
    def setUp(self):
        entry = Path(__file__).resolve().parents[1] / "scripts" / "start_local.py"
        self.assertTrue(entry.is_file(), "Не создан файл запуска локального стенда.")
        self.launcher = importlib.import_module("scripts.start_local")
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(self.launcher, "ROOT", self.root))
        self.enterContext(patch.object(self.launcher, "QDRANT_URL", "http://127.0.0.1:6335"))
        self.enterContext(patch.object(self.launcher, "COLLECTION", "mechanic_hackathon"))
        self.enterContext(patch("builtins.print"))

    def prepare(self):
        for name in (
            "data/raw/vehicles.json",
            "data/raw/fault_types.json",
            "data/annotations/manuals.json",
            "vendor/organizers/.venv/Scripts/python.exe" if self.launcher.WINDOWS else
            "vendor/organizers/.venv/bin/python",
            "vendor/organizers/data/vehicles.json",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def test_requires_data_before_starting_services(self):
        with (patch.object(self.launcher, "port_open", return_value=False),
              patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen):
            self.assertEqual(self.launcher.main(), 1)
        run.assert_not_called()
        popen.assert_not_called()

    def test_reuses_healthy_mocks_and_passes_index_to_web_chat(self):
        self.prepare()

        def response(url):
            return {
                "http://127.0.0.1:6335/collections": {"status": "ok"},
                "http://127.0.0.1:6335/collections/mechanic_hackathon": {"status": "ok"},
                "http://127.0.0.1:8001/health": {"status": "ok", "service": "mock-ml"},
                "http://127.0.0.1:8002/health": {"status": "ok", "service": "mock-toir"},
            }.get(url, {})

        with (patch.object(self.launcher, "get_json", side_effect=response),
              patch.object(self.launcher, "port_open", return_value=False),
              patch.object(subprocess, "Popen") as popen,
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run):
            self.assertEqual(self.launcher.main(), 0)
        popen.assert_not_called()
        command, = run.call_args.args
        self.assertEqual(command[:5], [sys.executable, "-X", "utf8", "-m", "ai_mechanic.web"])
        self.assertEqual(command[command.index("--collection") + 1], "mechanic_hackathon")
        self.assertEqual(run.call_args.kwargs["cwd"], self.root)

    def test_stops_started_mock_if_other_mock_port_is_in_use(self):
        self.prepare()
        ml = MagicMock()
        ml.poll.return_value = None

        def response(url):
            if url.startswith("http://127.0.0.1:6335/"):
                return {"status": "ok"}
            if "8001" in url and start.called:
                return {"status": "ok", "service": "mock-ml"}
            return {}

        with (patch.object(self.launcher, "get_json", side_effect=response),
              patch.object(self.launcher, "port_open", side_effect=lambda port: port == 8002),
              patch.object(subprocess, "Popen", return_value=ml) as start,
              patch.object(subprocess, "run") as run):
            self.assertEqual(self.launcher.main(), 1)
        start.assert_called_once()
        if sys.platform == "win32":
            command, = run.call_args.args
            self.assertEqual(Path(command[0]).name.lower(), "taskkill.exe")
            self.assertEqual(command[1:], ["/PID", str(ml.pid), "/T", "/F"])
        else:
            ml.terminate.assert_called_once()
            run.assert_not_called()

    def test_ubuntu_runs_mock_with_linux_venv(self):
        self.prepare()
        organizers = self.root / "vendor" / "organizers"
        unix_python = organizers / ".venv" / "bin" / "python"
        unix_python.parent.mkdir(exist_ok=True)
        unix_python.touch()
        started = []
        with (patch.object(self.launcher, "WINDOWS", False),
              patch.object(self.launcher, "get_json", side_effect=[{}, {"status": "ok", "service": "mock-ml"}]),
              patch.object(self.launcher, "port_open", return_value=False),
              patch.object(subprocess, "Popen") as popen):
            popen.return_value.poll.return_value = None
            self.launcher.start_mock("mock-ml", "mock_ml.app:app", 8001, started)
        command, = popen.call_args.args
        self.assertEqual(command[0], str(unix_python))
        self.assertEqual(started, [popen.return_value])
