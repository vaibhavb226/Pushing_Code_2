"""
POST /api/ai/chat — v2 of routers/ai_chat.py (original left untouched).

Adds the SQL Query Expert path: when SQL_EXPERT_ENABLED=true, each user
question is first offered to the text-to-SQL pipeline (gate → generate →
validate → EXPLAIN → read-only execute → one-line summary). If the gate
refuses, the pipeline gives up, or anything at all goes wrong, the request
falls back to the original context-stuffed chat — the endpoint never fails
because of the SQL path, and the user never sees SQL.

Request/response shapes are identical to v1 — no frontend changes needed.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from config import get_settings
from dependencies import (
    get_bids_repo,
    get_documents_repo,
    get_email_threads_repo,
    get_line_items_repo,
    get_llm_service,
    get_sql_expert,
)
from repositories.bids_repo import BidsRepository
from repositories.documents_repo import DocumentsRepository
from repositories.email_threads_repo import EmailThreadsRepository
from repositories.line_items_repo import LineItemsRepository
from routers.ai_chat import _build_system_prompt  # reuse v1's context builder
from services.llm.provider import LLMService

log = structlog.get_logger()
router = APIRouter(tags=["ai"])


class ChatRequest(BaseModel):
    messages: list[dict[str, str]]  # [{"role": "user"|"assistant", "content": str}]


class ChatResponse(BaseModel):
    role: str = "assistant"
    content: str


def _last_user_message(messages: list[dict[str, str]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", "")).strip()
    return ""


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    llm: LLMService = Depends(get_llm_service),
    bids_repo: BidsRepository = Depends(get_bids_repo),
    line_items_repo: LineItemsRepository = Depends(get_line_items_repo),
    email_threads_repo: EmailThreadsRepository = Depends(get_email_threads_repo),
    documents_repo: DocumentsRepository = Depends(get_documents_repo),
) -> ChatResponse:
    settings = get_settings()
    question = _last_user_message(body.messages)

    # ── SQL Query Expert path ──────────────────────────────────
    if settings.sql_expert_enabled and question:
        try:
            result = await get_sql_expert().try_answer(question)
            if result is not None:
                log.info(
                    "ai_chat_v2.sql_answer",
                    tables=result.tables_used,
                    rows=result.row_count,
                )
                return ChatResponse(role="assistant", content=result.answer)
        except Exception as exc:
            # DB unreachable, timeout, anything — fall back, never 500
            log.warning("ai_chat_v2.sql_expert_failed", error=str(exc)[:300])

    # ── Fallback: original context-stuffed chat (v1 behaviour) ─
    system_prompt = await _build_system_prompt(
        bids_repo, line_items_repo, email_threads_repo, documents_repo
    )
    result_llm = await llm.chat_with_history(
        system=system_prompt,
        messages=body.messages,
        max_tokens=1024,
    )
    log.info("ai_chat_v2.response", chars=len(result_llm.content))
    return ChatResponse(role="assistant", content=result_llm.content)
