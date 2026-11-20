# Ровно один воркер: SQLite-база, замок, кэш пересчёта и подписчики SSE живут в одном процессе.
# Больше воркеров = у каждого свой кэш и свои подписчики. uvicorn читает WEB_CONCURRENCY, поэтому --workers 1 явно.
web: uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
