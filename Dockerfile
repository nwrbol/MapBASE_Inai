FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
# По умолчанию: расчёт + веб-интерфейс. Только расчёт: docker run IMAGE python run.py --out /out
# Один процесс uvicorn (без --workers): база, кэш и подписчики SSE живут в одном процессе.
CMD ["python", "run.py", "--serve", "--host", "0.0.0.0", "--port", "8000"]
