import json
import logging

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from api.deps import AnswerModelDep, Db, EmbedderDep, SettingsDep, client_key, ok, rate_limiter
from api.schemas import ChatAnswer, ChatRequest, ChatResponse
from legal_core.answer import answer_question
from legal_core.db import session_scope

router = APIRouter(prefix="/api/v1", tags=["chat"])
log = logging.getLogger("api.chat")


def _ndjson(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


def _answer_in_own_session(
    body: ChatRequest, settings, embedder, llm
) -> dict:
    # A streaming response outlives the route function, so it owns its database session instead
    # of relying on the normal request dependency lifecycle.
    with session_scope() as db:
        answer = answer_question(
            db,
            query=body.query,
            domain=body.domain,
            as_of_date=body.as_of_date,
            settings=settings,
            embedder=embedder,
            llm=llm,
        )
    return ChatAnswer.model_validate(answer).model_dump(mode="json")


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Ask a legal-information question (public)",
    description=(
        "Answers only from reviewed legal text in force on `as_of_date` (default: today, Riyadh time). "
        "Returns `insufficient_evidence` instead of guessing when the indexed corpus does not support an answer."
    ),
)
def chat(
    body: ChatRequest, request: Request, db: Db, settings: SettingsDep, embedder: EmbedderDep, llm: AnswerModelDep
) -> dict:
    rate_limiter.check(client_key(request, "chat"), settings.rate_limit_chat_per_minute)
    answer = answer_question(
        db,
        query=body.query,
        domain=body.domain,
        as_of_date=body.as_of_date,
        settings=settings,
        embedder=embedder,
        llm=llm,
    )
    request.state.trace_id = answer["trace_id"]
    return ok(answer)


@router.post(
    "/chat/stream",
    summary="Stream a validated legal-information answer (public)",
    description=(
        "Returns newline-delimited JSON events: `start`, zero or more `delta` events, then `complete`. "
        "The legal answer is fully validated before any answer text is emitted, so streaming cannot expose "
        "an unsupported draft."
    ),
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def chat_stream(
    body: ChatRequest, request: Request, settings: SettingsDep, embedder: EmbedderDep, llm: AnswerModelDep
) -> StreamingResponse:
    rate_limiter.check(client_key(request, "chat"), settings.rate_limit_chat_per_minute)

    async def events():
        yield _ndjson({"type": "start"})
        try:
            answer = await anyio.to_thread.run_sync(_answer_in_own_session, body, settings, embedder, llm)
            request.state.trace_id = answer["trace_id"]
            text = answer["answer_ar"]
            for offset in range(0, len(text), 32):
                yield _ndjson({"type": "delta", "delta": text[offset : offset + 32]})
                await anyio.sleep(0)
            yield _ndjson({"type": "complete", "data": answer})
        except Exception:
            log.exception("chat_stream_failed", extra={"request_id": getattr(request.state, "request_id", None)})
            yield _ndjson(
                {"type": "error", "error": {"code": "STREAM_FAILED", "message": "تعذّر إكمال الإجابة."}}
            )

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
