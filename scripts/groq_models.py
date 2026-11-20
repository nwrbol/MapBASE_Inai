"""Список моделей, доступных с настроенным серверным ключом."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coverage.assistant.groq_client import GroqClient, ProviderError
from coverage.env import load_env

if __name__ == "__main__":
    load_env()
    client = GroqClient()
    try:
        if not client.key:
            sys.exit("Не задан GROQ_API_KEY")
        for model in client.request("GET", "models")["data"]:
            print(model["id"])
    except ProviderError as exc:
        sys.exit(str(exc))
    finally:
        client.http.close()
