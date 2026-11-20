import logging
import os
import time

import httpx


class ProviderError(Exception):
    pass


class GroqClient:
    def __init__(self, transport=None):
        self.key = os.getenv("GROQ_API_KEY", "").strip()
        self.model = os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"
        self.steps = max(1, min(20, int(os.getenv("ASSISTANT_MAX_STEPS", "5"))))
        self.http = httpx.Client(
            base_url=os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1",
            headers={"Authorization": f"Bearer {self.key}"},
            timeout=float(os.getenv("ASSISTANT_TIMEOUT_S", "30")),
            transport=transport,
        )

    def request(self, method, path, **kwargs):
        for attempt in range(2):
            try:
                r = self.http.request(method, path, **kwargs)
            except httpx.RequestError:
                raise ProviderError("ИИ не ответил вовремя. Проверьте подключение к интернету") from None
            logging.getLogger(__name__).info("Groq model=%s status=%d", self.model, r.status_code)
            if r.is_success:
                try:
                    return r.json()
                except ValueError:
                    break
            if r.status_code in (401, 403):
                raise ProviderError("Ассистент не настроен: проверьте ключ GROQ_API_KEY")
            if r.status_code == 404 or "model_decommissioned" in r.text or "model_not_found" in r.text:
                logging.getLogger(__name__).warning("Модель недоступна, проверьте GROQ_MODEL")
                raise ProviderError("Модель недоступна, проверьте GROQ_MODEL")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 0:
                    try:
                        delay = float(r.headers.get("retry-after", 2))
                    except ValueError:
                        delay = 2
                    time.sleep(max(0, min(delay, 10)))
                    continue
                if r.status_code == 429:
                    raise ProviderError("Слишком много запросов к ИИ, попробуйте через минуту")
            break
        raise ProviderError("Сервис ИИ вернул ошибку. Попробуйте позже")

    def complete(self, messages, tools):
        return self.request(
            "POST",
            "chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.2,
                "max_completion_tokens": 900,
            },
        )
