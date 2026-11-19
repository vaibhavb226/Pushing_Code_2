"""
Dependency factories for FastAPI's Depends() injection system.

All repositories and services are instantiated here. Routers import
get_*_repo / get_llm_service and declare them as Depends() parameters —
they never reference JsonRepository or DbRepository directly.

Switching backends requires only changing DB_BACKEND / EMAIL_PROVIDER in .env.
"""
from __future__ import annotations

from pathlib import Path

from config import Settings, get_settings
from repositories.bids_repo import BidsRepository
from repositories.documents_repo import DocumentsRepository
from repositories.email_threads_repo import EmailThreadsRepository
from repositories.line_items_repo import LineItemsRepository
from repositories.portal_templates_repo import PortalTemplatesRepository
from repositories.pricing_repo import PricingRepository
from repositories.submissions_repo import SubmissionsRepository
from repositories.suppliers_repo import SuppliersRepository
from repositories.unmatched_emails_repo import UnmatchedEmailsRepository

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"


# ── Repository singletons ─────────────────────────────────────────────────────
# These are module-level singletons used by the Depends() factories below.
# When DB_BACKEND != "json" the factory returns a DbRepository instance instead.

_bids = BidsRepository(DATA_DIR / "bids.json")
_line_items = LineItemsRepository(DATA_DIR / "lineItems.json")
_email_threads = EmailThreadsRepository(DATA_DIR / "emailThreads.json")
_suppliers = SuppliersRepository(DATA_DIR / "suppliers.json")
_documents = DocumentsRepository(DATA_DIR / "documents.json")
_pricing = PricingRepository(DATA_DIR / "pricingData.json")
_submissions = SubmissionsRepository(DATA_DIR / "submissions.json")
_portal_templates = PortalTemplatesRepository(DATA_DIR / "portalTemplates.json")
_unmatched = UnmatchedEmailsRepository(DATA_DIR / "unmatchedEmails.json")


def get_bids_repo() -> BidsRepository:
    return _bids


def get_line_items_repo() -> LineItemsRepository:
    return _line_items


def get_email_threads_repo() -> EmailThreadsRepository:
    return _email_threads


def get_suppliers_repo() -> SuppliersRepository:
    return _suppliers


def get_documents_repo() -> DocumentsRepository:
    return _documents


def get_pricing_repo() -> PricingRepository:
    return _pricing


def get_submissions_repo() -> SubmissionsRepository:
    return _submissions


def get_portal_templates_repo() -> PortalTemplatesRepository:
    return _portal_templates


def get_unmatched_repo() -> UnmatchedEmailsRepository:
    return _unmatched


# ── LLM service ───────────────────────────────────────────────────────────────
_llm_service = None  # Lazily initialised on first call


def get_llm_service():
    global _llm_service
    if _llm_service is None:
        from services.llm.factory import build_llm_service
        _llm_service = build_llm_service(get_settings())
    return _llm_service


# ── Email service ─────────────────────────────────────────────────────────────
_email_service = None


def get_email_service():
    global _email_service
    if _email_service is None:
        from services.email.factory import build_email_service
        _email_service = build_email_service(get_settings())
    return _email_service


# ── SQL Query Expert (text-to-SQL for ai_chat_v2) ─────────────────────────────
_sql_expert = None


def get_sql_expert():
    global _sql_expert
    if _sql_expert is None:
        from services.sql_expert import SqlExpert
        _sql_expert = SqlExpert(llm=get_llm_service(), settings=get_settings())
    return _sql_expert
