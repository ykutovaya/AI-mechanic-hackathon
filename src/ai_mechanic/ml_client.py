"""HTTP-клиент выданного сервиса диагностики."""

from http.client import HTTPException
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


FAULT_CODES = frozenset(f"F{i:02}" for i in range(1, 15))


class MLUnavailable(RuntimeError):
    """Сервис диагностики недоступен или вернул непригодные данные."""


def _validate(data, vehicle_id, session_id):
    if not isinstance(data, dict) or (data.get("vehicle_id"), data.get("session_id")) != (vehicle_id, session_id):
        raise ValueError("Идентификаторы в ответе ML не совпадают с запросом.")
    if data["status"] not in ("normal", "anomaly", "check_failed"):
        raise ValueError("Неизвестный статус ML.")
    if data["signal_quality"] not in ("good", "warning", "bad"):
        raise ValueError("Неизвестное качество записи.")
    code = data["anomaly_type"]
    if code is not None and (not isinstance(code, str) or code not in FAULT_CODES):
        raise ValueError("Неизвестный код отклонения ML.")
    if data["anomaly_title"] is not None and not isinstance(data["anomaly_title"], str):
        raise ValueError("Проверьте название отклонения ML.")
    for field in ("description", "model_version"):
        if not isinstance(data[field], str):
            raise ValueError(f"Проверьте поле ML {field}.")
    for field in ("anomaly_score", "confidence"):
        value = data[field]
        if value is not None and (type(value) not in (int, float) or not 0 <= value <= 1):
            raise ValueError(f"Проверьте значение {field}.")
    return data


def fetch_diagnosis(base_url, machine_id, check_id, timeout=5.0):
    """Получает и проверяет результат ML для выбранной машины и проверки."""
    if not all(isinstance(value, str) and value.strip() for value in (machine_id, check_id)):
        raise ValueError("Выберите машину и проверку.")
    base_url = base_url or os.environ.get("MOCK_ML_URL") or "http://127.0.0.1:8001"
    body = {"vehicle_id": machine_id, "session_id": check_id}
    try:
        request = Request(f"{base_url.rstrip('/')}/diagnose",
                          data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=timeout) as response:
            return _validate(json.load(response), machine_id, check_id)
    except HTTPError as exc:
        raise MLUnavailable(f"Сервис ML вернул HTTP {exc.code}. Проверьте выбранную проверку.") from exc
    except (OSError, HTTPException) as exc:
        raise MLUnavailable("Сервис ML недоступен. Проверьте его запуск и повторите запрос.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise MLUnavailable(f"Не удалось прочитать результат ML. {exc}") from exc
