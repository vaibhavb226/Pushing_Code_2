# How to Enable SQL Search in the RFP AIQ Chat

**Goal:** Let the RFP AIQ chat answer data questions directly from your PostgreSQL
database (GCP Cloud SQL) with a one-line human answer, instead of only from the
JSON files.

**Good news:** all the code is already written and wired in. You do **not** need to
change any Python files. Enabling it is purely configuration + restart. This guide
lists every change you make.

---

## Prerequisite (one-time, per machine)

Make sure the Postgres driver is installed. It is already listed in
`requirements.txt`, so from inside `backend_py/`:

```bash
pip install -r requirements.txt
```

(The package is `asyncpg` — if it's already installed, this does nothing.)

You also need **network access** from wherever the backend runs to your Cloud SQL
instance (the host/port must be reachable — VPC, Cloud SQL Auth Proxy, or an
authorised IP). If the backend can't reach the DB, the chat still works but silently
falls back to the old JSON-file answers.

---

## Step 1 — Edit `backend_py/.env`

This is the **only file you change**. Add these lines (fill in your real Cloud SQL
values):

```bash
# ── SQL Query Expert (RFP AIQ chat → PostgreSQL) ──────────────
SQL_EXPERT_ENABLED=true
SQL_EXPERT_DB_HOST=10.151.179.4        # your Cloud SQL host / private IP
SQL_EXPERT_DB_PORT=5432
SQL_EXPERT_DB_NAME=postgres            # your database name
SQL_EXPERT_DB_USER=postgres            # your DB user
SQL_EXPERT_DB_PASSWORD=your-password   # your DB password
SQL_EXPERT_DB_SCHEMA=public
SQL_EXPERT_MAX_ROWS=500                # safety cap on returned rows
```

| Setting | What it is | Default if omitted |
|---|---|---|
| `SQL_EXPERT_ENABLED` | Master on/off switch — **must be `true`** | `false` (off) |
| `SQL_EXPERT_DB_HOST` | Cloud SQL host or private IP | empty |
| `SQL_EXPERT_DB_PORT` | Postgres port | `5432` |
| `SQL_EXPERT_DB_NAME` | Database name | `postgres` |
| `SQL_EXPERT_DB_USER` | Database user | `postgres` |
| `SQL_EXPERT_DB_PASSWORD` | Database password | empty |
| `SQL_EXPERT_DB_SCHEMA` | Schema to read | `public` |
| `SQL_EXPERT_MAX_ROWS` | Max rows a query may return | `500` |

> There is already a template block for these in `backend_py/.env.example` you can
> copy from.

> ⚠️ **Security:** do not reuse the password that was hardcoded in the original
> notebook — rotate it in Cloud SQL and put the new one only in `.env` (which is
> git-ignored). The backend stores it as a `SecretStr` and never logs it.

---

## Step 2 — (Optional) Confirm the LLM provider

The SQL feature uses whatever LLM you already have configured — no separate setting.
Your `.env` already has one of these; the notebook used Vertex/Gemini:

```bash
LLM_PROVIDER=vertexai      # or anthropic / openai — any works
```

Nothing to change here unless you want a different model for the SQL reasoning.

---

## Step 3 — Restart the backend

```bash
cd backend_py
uvicorn main:app --reload --port 3001
```

That's it. No database migration, no frontend rebuild — the chat URL and screen are
unchanged.

---

## Step 4 — Try it in the chat

Open the **RFP AIQ** chat drawer (the "AIQ" assistant) and ask a data question:

- *"What is the total bid amount across all bids?"*
- *"Which supplier has the highest total amount and for which category?"*
- *"How many suppliers are in the historical data?"*

You should get a plain one-sentence answer, e.g.:

> *"Tidewater Frozen Holdings has the highest total bid amount at $470,688.78
> (Frozen Foods)."*

Then ask a **non-data** question to confirm the fallback still works:

- *"Summarize the email activity for BID-402"*

That one should answer from the normal chat (it isn't a database question).

---

## How to confirm it actually used SQL

The chat bubble looks the same either way (by design — the user never sees SQL). To
verify it hit Postgres, watch the backend terminal/logs for one of these entries:

| Log line | Meaning |
|---|---|
| `sql_expert.answered` | ✅ It queried Postgres — shows the tables used, row count, and the SQL that ran |
| `sql_expert.gate_refused` | The question wasn't a data question → fell back to normal chat |
| `ai_chat_v2.sql_expert_failed` | DB unreachable or errored → fell back to normal chat (check host/password/network) |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Every answer looks like the old JSON chat | `SQL_EXPERT_ENABLED` not `true`, or backend not restarted | Set it to `true`, restart |
| Log shows `sql_expert_failed` | Wrong host/password, or DB not reachable | Check the 5 `SQL_EXPERT_DB_*` values and network access to Cloud SQL |
| Answers use the wrong table (e.g. only current, ignoring history) | Business rule needs adjusting | Edit `backend_py/services/sql_expert/table_notes.py` (plain English), restart |
| "ModuleNotFoundError: asyncpg" | Driver not installed | `pip install -r requirements.txt` |
| Correct answer but "no data found" | The current table is empty; data is in the `_old` table | Already handled by `table_notes.py` (UNION ALL) — if not, add/adjust the note |

---

## What NOT to change

You do **not** need to edit any of these — they're already done:

- `routers/ai_chat_v2.py` — already the active chat router
- `services/sql_expert/*` — the pipeline, validators, schema reader
- `config.py`, `dependencies.py`, `main.py` — already wired
- The frontend — unchanged

Full technical change log (every file added/modified) is in
`docs/sql_expert_integration.md`.
```
