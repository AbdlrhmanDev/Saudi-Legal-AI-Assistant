from fastapi import APIRouter, Request

from api.deps import AnswerModelDep, Db, EmbedderDep, SettingsDep, client_key, ok, rate_limiter
from api.schemas import ChatRequest, ChatResponse
from legal_core.answer import answer_question

router = APIRouter(prefix="/api/v1", tags=["chat"])


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
