"""Локальная конфигурация без переопределения окружения сервера."""

import os
from pathlib import Path


def load_env(path=Path(__file__).resolve().parents[1] / ".env"):
    if path.is_file():
        for line in path.read_text(encoding="utf8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip().isidentifier():
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
