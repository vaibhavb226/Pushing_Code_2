# SQL Query Expert → RFP AIQ Chat Integration

**Date:** July 17, 2026
**Source:** `sql_query_expert_v5.1.ipynb` (Vertex AI + PostgreSQL text-to-SQL notebook)
**Goal:** When you ask a data question in the RFP AIQ chat, it is answered **from the
GCP Cloud SQL PostgreSQL database** with a **one-line human answer** — never raw SQL.
Non-data questions keep working exactly as before.

---

## 1. What was built

The notebook's pipeline was ported into the backend as a proper service module and
wired into the chat through a new `_v2` router (following this repo's existing
`_v2` convention — originals are never modified).

### Request flow

```
POST /api/ai/chat  (same URL, same request/response shape — no frontend change)
        │
        ▼
routers/ai_chat_v2.py
        │
        ├── SQL_EXPERT_ENABLED=false? ──────────────► original context chat (v1 behaviour)
        │
        ▼
services/sql_expert/pipeline.py — SqlExpert.try_answer(question)
        │
        1. Load schema from Postgres (once, then cached)
        2. GATE (LLM): "can this be answered from this schema?"
        │       └── NO → return None ────────────────► fall back to original context chat
        3. GENERATE (LLM): write ONE read-only SELECT   (≤3 attempts, errors fed back)
        4. VALIDATE (pure code): read-only? tables exist? columns exist?
        5. EXPLAIN dry-run: PostgreSQL plans the query without running it
        6. EXECUTE: read-only session, 15 s statement timeout, LIMIT 500 cap
        7. SUMMARIZE (LLM): question + result rows → ONE short sentence
        │
        ▼
Chat replies: "Tidewater Frozen Holdings has the highest total bid amount
               at $470,688.78 (Frozen Foods)."      ← never shows SQL
```

**Key safety property:** any failure anywhere in the SQL path (database unreachable,
gate refusal, validation failure, timeout) silently falls back to the normal chat.
The endpoint can never 500 because of this feature, and the user is never refused.

---

## 2. NEW files created

| File | What it is |
|---|---|
| `services/sql_expert/__init__.py` | Exports `SqlExpert`, `SqlExpertResult` |
| `services/sql_expert/validators.py` | Notebook Step 8 as pure functions: `validate_sql()` (read-only / single statement / forbidden keywords / table existence / column existence), `strip_literals()`, `scan_tables()`, `tables_in()`, `add_limit()`. No I/O — fully unit-testable. |
| `services/sql_expert/table_notes.py` | Notebook Step 4b — business rules injected into every prompt. Pre-filled for the split table families (`bids`/`bids_old`, `suppliers`/`suppliers_old`, etc.): *combine with UNION ALL for aggregates*. **This file is your main tuning knob** — edit it in plain English when the model picks the wrong table. |
| `services/sql_expert/schema.py` | Notebook Steps 3–4 with asyncpg — introspects tables/columns/PK/FK from `information_schema`, caches `schema_text` + `table_columns` in-process. `schema_context()` = schema + your table notes. |
| `services/sql_expert/pipeline.py` | Notebook Steps 6+10 — the `SqlExpert` class with `try_answer(question)`. Runs gate → generate → validate → EXPLAIN → execute → summarize. Returns `SqlExpertResult(answer, sql, tables_used, row_count)`; the router uses only `.answer`, the rest goes to structlog for debugging. |
| `routers/ai_chat_v2.py` | v2 of the chat endpoint. Tries the SQL path first (when enabled), falls back to v1's context chat. **Reuses** `_build_system_prompt` imported from `routers/ai_chat.py` — the 120-line context builder is not duplicated. |
| `tests/services/test_sql_validators.py` | The notebook's guardrail self-test as pytest — DELETE, hidden `;DROP`, unknown tables, `SELECT 2+2`, invented columns all rejected. |
| `tests/routers/test_ai_chat_v2.py` | Router tests — disabled→fallback, SQL hit→one-line answer, pipeline crash→graceful fallback. |
| `docs/sql_expert_integration.md` | This document. |

---

## 3. EXISTING files modified — exact changes

### `main.py`
- **Where:** router-registration block (the `try/except ImportError` for the AI router, ~line 274).
- **Change:** `from routers.ai_chat import router` → `from routers.ai_chat_v2 import router`, plus a comment pointing to this doc.
- **Why:** same activation pattern already used for `documents_v2` (line 243). `routers/ai_chat.py` itself is untouched — swap the import back to instantly revert.

### `config.py`
- **Where:** new block after the `Database` settings.
- **Change:** 8 new settings — `sql_expert_enabled` (default **False**), `sql_expert_db_host/port/name/user/password/schema`, `sql_expert_max_rows`. The password is a `pydantic.SecretStr`, matching the repo's security rule.
- **Why:** the notebook had the Cloud SQL host and password **hardcoded in plaintext**. They now live only in `.env`. Default-off means zero impact on anyone who doesn't set them.

### `dependencies.py`
- **Where:** bottom of file, after the email service factory.
- **Change:** new lazy singleton `get_sql_expert()` — identical pattern to `get_llm_service()`.
- **Why:** routers must depend on factories, never construct services directly (repo convention).

### `routers/prompts.py`
- **Where:** appended after `RFPAIQ_IDENTITY`.
- **Change:** four new constants — `SQL_GATE_PROMPT`, `SQL_GENERATION_SYSTEM`, `SQL_FEW_SHOT_EXAMPLES`, `SQL_SUMMARIZER_PROMPT`.
- **Why:** repo rule — all LLM prompt strings live in this file, never inline in routers/services. The summarizer prompt is the new piece that turns query results into the one-line human answer.

### `.env.example`
- **Where:** after the Database block.
- **Change:** the 8 `SQL_EXPERT_*` variables with safe placeholder values and a comment.

### `CLAUDE.md`
- **Change:** new row in the `_v2` File Convention table (`ai_chat.py` → `ai_chat_v2.py`) and a short SQL Query Expert subsection pointing here.

### NOT changed
- `routers/ai_chat.py` — untouched (v2 convention).
- `requirements.txt` — nothing added. The pipeline uses `asyncpg` (already present for
  `DB_BACKEND=postgres`) and the existing LangChain `LLMService` instead of the
  notebook's `psycopg2` + `google-genai`.
- Frontend — nothing. The endpoint URL and JSON shapes are identical.

---

## 4. How the notebook maps to the code

| Notebook step | Where it lives now | Changes made |
|---|---|---|
| Step 2 config (hardcoded creds) | `config.py` + `.env` | Password → `SecretStr`; host/user no longer in code |
| Step 3 connect + health check | `pipeline._connect()` / `schema.py` | psycopg2 → asyncpg; read-only session enforced at connection level |
| Step 4 schema extraction | `schema.py → SchemaCache` | Same SQL queries, async; cached with an `asyncio.Lock` |
| Step 4b table notes | `table_notes.py` | Pre-filled for your `_old` split families |
| Step 5 Vertex client | *(gone)* | Uses `services/llm/` — provider chosen by `LLM_PROVIDER` in `.env` (set `vertexai` for Gemini like the notebook) |
| Step 6 answerability gate | `pipeline._gate()` | Same prompt; refusal = silent fallback instead of "🚫 REFUSED" |
| Step 6 split-table menu | *(replaced)* | Chat can't show an `input()` menu — TABLE_NOTES decide automatically (the notebook's own recommended fix) |
| Step 7 SQL prompt + few-shots | `routers/prompts.py` | Same text, prompts-file convention |
| Step 8 validators | `validators.py` | Same logic; catalogs passed as parameters (testable) |
| Step 10 `ask()` pipeline | `pipeline.try_answer()` | Same retry loop; `display(df)` → LLM summarization into one sentence |
| `_parse_json_reply` | *(gone)* | Reuses existing `services/llm/json_parser.extract_json` |

---

## 5. How to enable it

1. In `backend_py/.env` add (values from your GCP Cloud SQL instance):

   ```bash
   SQL_EXPERT_ENABLED=true
   SQL_EXPERT_DB_HOST=10.151.179.4
   SQL_EXPERT_DB_PORT=5432
   SQL_EXPERT_DB_NAME=postgres
   SQL_EXPERT_DB_USER=postgres
   SQL_EXPERT_DB_PASSWORD=********
   ```

2. Restart the backend. Ask in the RFP AIQ chat:
   - *"Which supplier has the highest total bid amount?"* → one-line answer from Postgres
   - *"Summarize the email activity for BID-402"* → normal context chat (gate refuses, falls back)

3. Watch the logs — every SQL answer logs the generated SQL, tables used, and row
   count under `sql_expert.answered`, so you can audit what ran without the user ever
   seeing it.

> ⚠️ **Security note:** the notebook file in your Downloads folder contains the real
> database password in plaintext. Since it has been shared/stored, consider **rotating
> that password** in Cloud SQL, then putting the new one only in `.env` (which is
> git-ignored).

---

## 6. Current limitations & future work

- **No interactive ambiguity menu.** The notebook could ask *"current, historical, or
  both?"*. A chat turn can't pause for input, so `table_notes.py` decides (default:
  combine with UNION ALL). Future: return the options as a chat message and parse the
  user's next reply.
- **One question per turn.** Only the latest user message goes to the SQL pipeline;
  conversation history is not used for follow-ups like "and the second highest?".
  Future: pass recent turns into the gate/generation prompts.
- **Schema cache lives until restart.** New tables/columns appear after a backend
  restart (or call `SchemaCache.refresh()` from a future admin endpoint).
- **Tests mock the LLM and DB.** The full path needs the real Cloud SQL + LLM —
  exercise it with the two questions in section 5.
