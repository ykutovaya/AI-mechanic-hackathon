"""Запускает подготовленный локальный стенд в Windows и Ubuntu."""

import json
from http.client import HTTPException
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
WEB_PORT = 8765
WINDOWS = sys.platform == "win32"
QDRANT_PORT = 6335 if WINDOWS else 6333
QDRANT_URL = (os.getenv("QDRANT_URL") or f"http://{HOST}:{QDRANT_PORT}").rstrip("/")
COLLECTION = os.getenv("QDRANT_COLLECTION") or "mechanic_hackathon"
CONTAINER = "mechanic-hackathon-qdrant"
ML_PORT = 8001
TOIR_PORT = 8002


def get_json(url):
    """Возвращает объект по локальному HTTP или пустой словарь при сбое."""
    try:
        with urlopen(url, timeout=1) as response:
            data = json.load(response)
    except (OSError, ValueError, HTTPException):
        return {}
    return data if isinstance(data, dict) else {}


def port_open(port):
    """Проверяет, занят ли локальный порт."""
    try:
        with socket.create_connection((HOST, port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_until(ready, process=None):
    """Даёт сервису время запуститься и проверяет, что процесс не завершился."""
    deadline = monotonic() + 12
    while monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if ready():
            return True
        sleep(0.25)
    return False


def check_index():
    """При необходимости запускает созданный контейнер и проверяет рабочую коллекцию."""
    def ready():
        return get_json(f"{QDRANT_URL}/collections").get("status") == "ok"

    if not ready():
        try:
            subprocess.run(
                ["docker", "start", CONTAINER],
                stdout=subprocess.DEVNULL, check=True, timeout=20,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"Проверьте запуск Docker и контейнера {CONTAINER} по documents/setup.md."
            ) from exc
        if not wait_until(ready):
            raise RuntimeError(f"Qdrant не отвечает на {QDRANT_URL}. Проверьте порт контейнера.")
    if get_json(f"{QDRANT_URL}/collections/{COLLECTION}").get("status") != "ok":
        raise RuntimeError(f"Подготовьте индекс {COLLECTION} по documents/knowledge-base.md.")


def start_mock(service, app, port, started):
    """Использует здоровый сервис или добавляет свой процесс в список для остановки."""
    host_url = f"http://{HOST}:{port}"

    def ready():
        health = get_json(f"{host_url}/health")
        return health.get("status") == "ok" and health.get("service") == service

    if ready():
        return
    if port_open(port):
        raise RuntimeError(f"Порт {port} занят. Освободите его для {service}.")
    organizers = ROOT / "vendor" / "organizers"
    python = organizers / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")
    if not python.is_file() or not (organizers / "data" / "vehicles.json").is_file():
        raise RuntimeError("Подготовьте данные и окружение заглушек по documents/setup.md.")
    process = subprocess.Popen(
        [str(python), "-X", "utf8", "-m", "uvicorn", app, "--host", HOST, "--port", str(port)],
        cwd=organizers, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    started.append(process)
    if not wait_until(ready, process):
        raise RuntimeError(f"{service} не ответил на {host_url}/health. Проверьте вывод выше.")


def stop_process(process):
    """Завершает запущенный процесс и дочерний интерпретатор venv в Windows."""
    if process.poll() is not None:
        return
    if WINDOWS:
        taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    """Проверяет локальную установку и запускает чат в текущем терминале."""
    started = []
    try:
        web_url = f"http://{HOST}:{WEB_PORT}"
        if port_open(WEB_PORT):
            if isinstance(get_json(f"{web_url}/api/machines").get("machines"), list):
                print(f"Чат уже работает. Откройте {web_url}/")
                return 0
            raise RuntimeError(f"Порт {WEB_PORT} занят. Освободите его для веб-чата.")
        for name in ("data/raw/vehicles.json", "data/raw/fault_types.json", "data/annotations/manuals.json"):
            if not (ROOT / name).is_file():
                raise RuntimeError(f"Отсутствует {name}. Подготовьте данные по documents/setup.md.")
        check_index()
        start_mock("mock-ml", "mock_ml.app:app", ML_PORT, started)
        start_mock("mock-toir", "mock_toir.app:app", TOIR_PORT, started)
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "ai_mechanic.web",
             "--host", HOST, "--port", str(WEB_PORT),
             "--qdrant-url", QDRANT_URL, "--collection", COLLECTION,
             "--ml-url", f"http://{HOST}:{ML_PORT}", "--toir-url", f"http://{HOST}:{TOIR_PORT}"],
            cwd=ROOT,
        )
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Ошибка запуска. {exc}", file=sys.stderr)
        return 1
    finally:
        for process in reversed(started):
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
