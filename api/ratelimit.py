"""Простой лимит частоты записи в памяти процесса (скользящее окно, без внешних сервисов).

Ключ — пара (device_id, IP): не больше `limit` записей в минуту. Это защита от случайного зацикливания
клиента, а не от злоумышленника: тот, кто умеет менять device_id, обойдёт лимит. В продукте лимиты ставят на шлюзе.
"""

from __future__ import annotations

import math
import time
from collections import deque
from threading import Lock

DEFAULT_LIMIT = 60
WINDOW_S = 60.0
MAX_KEYS = 10_000  # чтобы словарь не рос бесконечно при переборе device_id


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int, limit: int) -> None:
        self.retry_after = retry_after
        self.limit = limit
        super().__init__(f"Слишком много запросов: не больше {limit} в минуту. Повторите через {retry_after} с.")


class RateLimiter:
    def __init__(self, limit: int = DEFAULT_LIMIT, window_s: float = WINDOW_S, clock=time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = Lock()

    def check(self, device_id: str, ip: str) -> None:
        """Засчитывает запрос; RateLimitExceeded, если лимит окна исчерпан. limit <= 0 отключает проверку."""
        if self.limit <= 0:
            return
        now = self._clock()
        key = (device_id, ip)
        with self._lock:
            if len(self._hits) > MAX_KEYS:
                self._hits = {k: q for k, q in self._hits.items() if q and now - q[-1] < self.window_s}
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                raise RateLimitExceeded(max(1, math.ceil(self.window_s - (now - q[0]))), self.limit)
            q.append(now)
