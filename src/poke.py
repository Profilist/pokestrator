import os
from typing import Any, Mapping

import requests
from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _webhook_url() -> str:
    return (
        os.getenv("POKE_WEBHOOK_URL")
        or "https://poke.com/api/v1/inbound-sms/webhook"
    )


def send_poke_message(message: str, metadata: Mapping[str, Any] | None = None) -> dict:
    payload = {"message": message}
    if metadata:
        payload["metadata"] = dict(metadata)

    if _bool_env("POKE_DRY_RUN", False):
        return {"ok": True, "dry_run": True, "payload": payload}

    api_key = os.getenv("POKE_API_KEY")
    if not api_key:
        raise ValueError("POKE_API_KEY is not set")

    response = requests.post(
        _webhook_url(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    return {
        "ok": True,
        "status_code": response.status_code,
        "response": response.text,
    }


if __name__ == "__main__":
    print(send_poke_message("This is a test message from the Poke API"))
