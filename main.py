"""Точка входа для деплоя: uvicorn main:app --host 0.0.0.0 --port $PORT.

Само приложение собирается в api/main.py; этот файл лишь отдаёт его из корня репозитория,
чтобы платформа (Render, Railway, Heroku и т. п.) нашла `main:app` без дополнительных настроек.
"""

from api.main import app

__all__ = ["app"]
