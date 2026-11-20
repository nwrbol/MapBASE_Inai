"""Серверный чат с ограниченным набором действий карты."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coverage.assistant.agent import answer
from coverage.assistant.groq_client import ProviderError
from coverage.assistant.prompt import OFF_TOPIC_REPLY
from coverage.assistant.tools import Context, Id, Layer, Tab

from .field_routes import field_map_view
from .ratelimit import RateLimitExceeded
from .routes import _pick, _state

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
SUGGESTIONS = [
    "Почему выбрана эта пара?",
    "Что сейчас на экране?",
    "Покажи худшую клетку",
    "Где сигнал хуже ночью?",
    "Что такое RSRP?",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Message(Input):
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("content")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Введите вопрос")
        return value.strip()


class View(Input):
    bounds: tuple[float, float, float, float] | None = None
    zoom: float | None = None
    center: tuple[float, float] | None = None


class Plan(Input):
    pair: Annotated[list[Id], Field(max_length=2)] | None = None
    covered: int | None = None
    source: Annotated[str, Field(max_length=32)] = "tz"


class Excluded(Input):
    id: Id
    category: Annotated[str, Field(max_length=32)] = "other"
    reason: Annotated[str, Field(max_length=200)] = ""


class Restrictions(Input):
    excluded: Annotated[list[Excluded], Field(max_length=100)] = []
    required: Annotated[list[Id], Field(max_length=2)] = []


class Click(Input):
    type: Literal["cell", "candidate", "tower"]
    id: Id


class MapState(Input):
    view: View = View()
    active_layers: Annotated[list[Layer], Field(max_length=20)] = []
    open_tab: Tab = "assistant"
    plan: Plan = Plan()
    constraints: Restrictions = Restrictions()
    selected_pair: Annotated[list[Id], Field(max_length=2)] | None = None
    last_clicked: Click | None = None
    field_included: bool = False
    panel_width: int | None = None
    is_mobile: bool = False


class Chat(Input):
    messages: Annotated[list[Message], Field(min_length=1, max_length=24)]
    map_state: MapState = MapState()
    analysis_id: Id | None = None
    include_field: bool = False

    @model_validator(mode="after")
    def validate_turn(self):
        if self.messages[-1].role != "user":
            raise ValueError("Последнее сообщение должно быть вопросом")
        if len(self.map_state.model_dump_json().encode()) > 8192:
            raise ValueError("Слишком большое состояние карты")
        if self.analysis_id and self.include_field:
            raise ValueError("Полевые замеры доступны для исходного набора")
        return self


@router.get("/status")
def status(request: Request):
    client = request.app.state.groq
    return {
        "enabled": bool(client.key),
        "reason": None if client.key else "Не задан GROQ_API_KEY",
        "model": client.model,
        "off_topic_reply": OFF_TOPIC_REPLY,
        "suggestions": SUGGESTIONS,
    }


@router.post("/chat")
def chat(request: Request, body: Chat):
    client = request.app.state.groq
    if not client.key:
        raise HTTPException(503, "Ассистент не настроен")
    try:
        request.app.state.chat_limit.check(
            request.headers.get("X-Device-Id", "")[:64], request.client.host if request.client else ""
        )
    except RateLimitExceeded as exc:
        raise HTTPException(
            429, "Слишком много вопросов подряд, подождите минуту", headers={"Retry-After": str(exc.retry_after)}
        ) from None
    base = _pick(request, body.analysis_id)
    st = _state(request)

    def dataset(include_field):
        if include_field:
            view = field_map_view(request)
            return view.pr, view.map_data
        return base.pr, base.map_data

    pr, data = dataset(body.include_field)
    state = body.map_state.model_dump(exclude_none=True)
    state.update(uploaded=bool(body.analysis_id), field_included=body.include_field)
    ctx = Context(pr, data, state, st.field_store.count() if not body.analysis_id else {}, dataset)
    try:
        result = answer(client, [m.model_dump() for m in body.messages], ctx)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from None
    if client.key in json.dumps(result, ensure_ascii=False):
        raise HTTPException(502, "Некорректный ответ сервиса ИИ")
    return result
