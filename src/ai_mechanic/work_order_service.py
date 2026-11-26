"""HTTP-клиент выданной тестовой ТОиР."""

from http.client import HTTPException
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .work_orders import validate_order


class OrderServiceError(RuntimeError):
    """ТОиР не подтвердила создание заявки."""


def submit_order(base_url, payload, key, timeout=5.0):
    """Отправляет подтверждённую версию и возвращает идентификатор заявки."""
    validate_order(payload)
    base_url = base_url or os.environ.get("MOCK_TOIR_URL") or "http://127.0.0.1:8002"
    # Ключ отправки хранится в приложении. Контракт ТОиР не принимает его в составе заявки.
    body = {
        "vehicle_id": payload["machine_id"],
        "session_id": payload["check_id"],
        "anomaly_type": payload["fault_code"],
        "conclusion": payload["diagnosis"],
        "urgency": payload["priority"],
        "operations": payload["works"],
        "spare_parts": payload["parts"],
        "references": [
            {"manual": source.get("manual", source["document_id"]), "pdf_page": source["page"]}
            for source in payload["sources"]
        ],
    }
    try:
        request = Request(f"{base_url.rstrip('/')}/tickets",
                          data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=timeout) as response:
            value = json.load(response)
        if (not isinstance(value, dict) or value.get("status") != "created"
                or not isinstance(value.get("ticket_id"), str) or not value["ticket_id"].strip()):
            raise ValueError("В ответе отсутствует номер новой заявки.")
        return value["ticket_id"]
    except HTTPError as exc:
        raise OrderServiceError(f"ТОиР вернула HTTP {exc.code}. Результат отправки неизвестен.") from exc
    except (OSError, ValueError, HTTPException) as exc:
        raise OrderServiceError("ТОиР не подтвердила создание заявки. Результат отправки неизвестен.") from exc
