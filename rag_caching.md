# RAG Caching — Types and Strategies
## Sysco RFP AIQ

---

## What is RAG Caching?

**RAG (Retrieval-Augmented Generation)** works as a pipeline:

```
Document → Parse → Chunk → Embed → Vector Store → Retrieve → LLM Prompt → Response
```

Every step has a cost — API calls, CPU time, disk I/O, dollars. The same document gets chunked every restart. The same query hits the vector store repeatedly. The same system prompt gets sent to Claude on every chat message.

**Caching** eliminates repeated work by storing the output of each step so that subsequent requests can skip it. The right cache at the right layer can reduce latency by 10x and cost by 90%.

---

## The Eight Caching Layers

RAG has eight distinct caching opportunities, from bottom (raw data) to top (final response):

```
┌───────────────────────────────────────────────────────────────┐
│  Layer 8 │ API Route Cache         ← full HTTP response       │
│  Layer 7 │ Provider Prompt Cache   ← Anthropic / OpenAI       │
│  Layer 6 │ LLM Response Cache      ← exact prompt match       │
│  Layer 5 │ Semantic Query Cache    ← fuzzy query match        │
│  Layer 4 │ Retrieval Result Cache  ← top-k chunks per query   │
│  Layer 3 │ Vector Index Cache      ← in-memory FAISS/Chroma   │
│  Layer 2 │ Embedding Cache         ← text → vector            │
│  Layer 1 │ Document Cache          ← raw → parsed → chunks    │
└───────────────────────────────────────────────────────────────┘
         ↑ document goes in here (bottom)
```

---

## Layer 1 — Document Preprocessing Cache

### What it caches
The output of parsing and chunking raw documents (PDFs, Excel, Word files).

Parsing a 50-page PDF with `pdfminer` or `pypdf` can take 2–5 seconds per file. Chunking 100 documents on every server restart wastes minutes of startup time.

### What to store
```
SHA-256(file_bytes) → {
  "chunks": ["chunk1 text...", "chunk2 text...", ...],
  "metadata": { "pages": 50, "parsed_at": "2026-07-13" }
}
```

### Where to store it
| Backend | Best for | Notes |
|---------|---------|-------|
| Disk (pickle / JSON) | Simplicity | Fast, no infra, portable |
| SQLite | Medium scale | ACID, queryable |
| Redis | High throughput | TTL built-in, shared across workers |
| `diskcache` (Python) | Drop-in disk cache | Handles serialization, eviction |

### TTL strategy
Long — days to weeks. Invalidate when the source file changes (compare file hash).

### Code example
```python
import diskcache
import hashlib

_cache = diskcache.Cache("data/.chunk_cache")

def chunk_with_cache(file_bytes: bytes, chunk_fn) -> list[str]:
    key = hashlib.sha256(file_bytes).hexdigest()
    if key in _cache:
        return _cache[key]
    chunks = chunk_fn(file_bytes)
    _cache.set(key, chunks, expire=7 * 24 * 3600)  # 7 days
    return chunks
```

### Savings
- Re-parse: **0 ms** instead of 2–5 s per file
- Re-chunk: **0 ms** instead of 500 ms per document

---

## Layer 2 — Embedding Cache

### What it caches
The vector representation of each text chunk: `"chunk text..." → [0.23, 0.11, -0.44, ...]`

Embedding is the **most expensive repeated operation** in RAG. OpenAI `text-embedding-ada-002` charges $0.0001 per 1K tokens. If you have 10,000 chunks of 200 tokens each, re-embedding costs ~$0.20 every time. At scale, this adds up fast.

### What to store
```
SHA-256(chunk_text + model_name) → float32 vector (1536 dims for ada-002)
```

### Where to store it
| Backend | Best for |
|---------|---------|
| Redis (hash map) | Production — fast, persistent, shared |
| SQLite BLOB | Simple deployment — no extra infra |
| FAISS on disk | Large vector sets |
| `CacheBackedEmbeddings` (LangChain) | Drop-in replacement |

### TTL strategy
Very long — weeks to months. Embeddings are deterministic: same text + same model = same vector. Only invalidate if you change the embedding model.

### Code example — LangChain built-in
```python
from langchain.embeddings import CacheBackedEmbeddings
from langchain.storage import LocalFileStore
from langchain_openai import OpenAIEmbeddings

store = LocalFileStore("data/.embedding_cache")
base_embedder = OpenAIEmbeddings(model="text-embedding-ada-002")

# Drop-in replacement — caches automatically
cached_embedder = CacheBackedEmbeddings.from_bytes_store(
    base_embedder,
    store,
    namespace=base_embedder.model,
)
```

### Code example — Custom Redis cache
```python
import redis
import hashlib
import numpy as np

r = redis.Redis()

def embed_with_cache(text: str, embed_fn, model: str = "ada-002") -> list[float]:
    key = f"emb:{hashlib.sha256(f'{model}:{text}'.encode()).hexdigest()}"
    cached = r.get(key)
    if cached:
        return np.frombuffer(cached, dtype=np.float32).tolist()
    vector = embed_fn(text)
    r.set(key, np.array(vector, dtype=np.float32).tobytes(), ex=30 * 86400)
    return vector
```

### Savings
- Per-chunk: **0 ms / $0** instead of 50–200 ms / $0.00002
- At 100K chunks: saves ~$2 and 5+ minutes per re-index run

---

## Layer 3 — Vector Index Cache (In-Memory)

### What it caches
The loaded vector index (FAISS, Chroma, Annoy) kept in RAM so the server doesn't reload it from disk on every request.

Loading a FAISS index with 100K vectors from disk takes 500 ms–2 s. If you do this per request, your latency is terrible.

### How it works
Keep a module-level singleton. The index is loaded once at startup and reused for all queries.

### Code example
```python
# In dependencies.py or a services/vector_store.py
from pathlib import Path
import faiss

_index: faiss.Index | None = None
_chunks: list[str] = []

def get_vector_index() -> tuple[faiss.Index, list[str]]:
    global _index, _chunks
    if _index is None:
        _index = faiss.read_index(str(Path("data/faiss.index")))
        _chunks = json.loads(Path("data/chunks.json").read_text())
    return _index, _chunks

def invalidate_index():
    global _index, _chunks
    _index = None
    _chunks = []
```

Call `invalidate_index()` after adding or deleting documents so the next request reloads from disk.

### Where it applies
- **FAISS**: Always needed — FAISS is in-memory only
- **ChromaDB (persistent)**: Has its own internal cache, but still benefits from keeping the client alive
- **Pinecone / Weaviate / Qdrant**: Managed services — index lives on their servers, no in-process cache needed

### Savings
- Per query: **0–5 ms** instead of 500–2000 ms per cold load

---

## Layer 4 — Retrieval Result Cache

### What it caches
The top-k chunks returned by a vector similarity search for a given query.

Some queries are asked repeatedly — dashboards refreshing, multiple users asking the same thing. Re-running the ANN search (even against an in-memory index) costs 10–50 ms and CPU cycles.

### What to store
```
hash(query_text + filters + k + index_version) → [chunk1, chunk2, ..., chunkK]
```

### TTL strategy
Short — **30 seconds to 5 minutes**. RAG data changes: new documents uploaded, bids updated. A stale retrieval gives Claude wrong context.

### Code example — `cachetools` in-memory
```python
from cachetools import TTLCache
import hashlib
import json

_retrieval_cache: TTLCache = TTLCache(maxsize=500, ttl=120)  # 2-minute TTL

def retrieve_with_cache(
    query: str,
    index,
    k: int = 5,
    filters: dict | None = None,
) -> list[str]:
    key = hashlib.sha256(
        json.dumps({"q": query, "k": k, "f": filters}, sort_keys=True).encode()
    ).hexdigest()
    if key in _retrieval_cache:
        return _retrieval_cache[key]
    results = index.search(query, k=k, filters=filters)
    _retrieval_cache[key] = results
    return results
```

### Savings
- Hit rate typically 15–40% on repeated user questions
- Per hit: **1–2 ms** instead of 10–50 ms

---

## Layer 5 — Semantic Query Cache

### What it caches
Responses to **semantically similar** past queries — not just exact matches.

"What is the response rate for BID-089?" and "How many suppliers responded to BID-089?" should return the same cached answer. A hash-based cache treats these as different queries. A semantic cache finds the match.

### How it works
1. Embed the incoming query: `q_vec = embed(query)`
2. Search a small "query cache" vector store for similar past queries
3. If cosine similarity > threshold (e.g. 0.95): return cached response
4. Otherwise: run the full RAG pipeline, store the result alongside the query embedding

### Threshold guidance
| Threshold | Behavior |
|-----------|---------|
| 0.99 | Very strict — near-identical phrasing only |
| 0.95 | **Recommended** — catches paraphrases, avoids false positives |
| 0.90 | Aggressive — risk of wrong answers for different intents |

### Code example
```python
import numpy as np
from collections import namedtuple

CachedEntry = namedtuple("CachedEntry", ["query_vec", "response"])
_sem_cache: list[CachedEntry] = []
SIM_THRESHOLD = 0.95

def cosine_sim(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

def semantic_cache_lookup(query: str, embed_fn) -> str | None:
    q_vec = embed_fn(query)
    for entry in _sem_cache:
        if cosine_sim(q_vec, entry.query_vec) >= SIM_THRESHOLD:
            return entry.response
    return None

def semantic_cache_store(query: str, response: str, embed_fn) -> None:
    q_vec = embed_fn(query)
    _sem_cache.append(CachedEntry(query_vec=q_vec, response=response))
```

### Production tool: GPTCache
```python
from gptcache import cache
from gptcache.adapter.langchain_models import LangChainLLMs

cache.init(similarity_evaluation_config={"threshold": 0.95})
llm = LangChainLLMs(llm=your_langchain_llm)
# LLM calls now go through semantic cache automatically
```

### When NOT to use it
- Queries with numbers or IDs where small differences matter ("BID-089" vs "BID-090")
- Real-time data where stale answers are harmful
- Personalised responses (user A vs user B)

---

## Layer 6 — LLM Response Cache (Exact Match)

### What it caches
The final LLM response for an exact prompt — hash of the full request.

Useful when the same complete prompt (system + retrieved context + user question) is sent repeatedly — e.g. a health check bot that asks the same status question every minute, or a scheduled report.

### What to store
```
hash(model + temperature + full_prompt_string) → response_text
```

### TTL strategy
Short — **60 seconds to 10 minutes**. LLM responses are data-dependent; stale responses with wrong bid data are worse than no cache.

### Code example — LangChain built-in (one line)
```python
from langchain.globals import set_llm_cache
from langchain.cache import SQLiteCache

# Add this once at startup — all LangChain LLM calls are cached automatically
set_llm_cache(SQLiteCache(database_path=".langchain_cache.db"))
```

Other backends:
```python
from langchain.cache import InMemoryCache, RedisCache
import redis

set_llm_cache(InMemoryCache())  # process lifetime, no persistence
set_llm_cache(RedisCache(redis_=redis.Redis(), ttl=300))  # 5-minute TTL
```

### When to use
- Scheduled reports / summaries that run on a fixed interval
- Dev/test: avoid burning API quota during development
- Static FAQ-style queries with no personalisation

### When NOT to use
- Conversational AI where the context changes per turn
- Queries that include real-time data (live bid status, email arrivals)

---

## Layer 7 — Provider Prompt Cache (Anthropic / OpenAI)

### What it caches
Repeated blocks of text in the prompt — system instructions, retrieved chunks — on the **provider's servers**.

This is the **most impactful cache** for the Sysco RFP AIQ chat feature. Every chat message currently sends 50,000+ tokens (all bids, all emails, all documents). With prompt caching, those tokens are sent once and cached for 5 minutes — subsequent messages in the same session pay only 10% of the input cost.

### Anthropic Prompt Caching

Mark any static content block with `cache_control`:

```python
# Using the Anthropic SDK directly
import anthropic

client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1024,
    system=[
        {
            "type": "text",
            "text": "You are the Sysco RFP AIQ assistant.",
        },
        {
            "type": "text",
            "text": retrieved_context,        # 10,000+ tokens of bid data
            "cache_control": {"type": "ephemeral"},  # ← cache this block
        }
    ],
    messages=conversation_history,
)
```

### Anthropic pricing with cache
| Token type | Cost (Sonnet) | Notes |
|-----------|--------------|-------|
| Input (uncached) | $3.00 / 1M tokens | Normal |
| Cache write | $3.75 / 1M tokens | First time, 25% premium |
| Cache read | $0.30 / 1M tokens | **90% discount** |
| Output | $15.00 / 1M tokens | Unchanged |

**Example**: 50K-token system prompt sent 20 times per session:
- Without cache: `50K × 20 × $3.00/1M = $3.00`
- With cache: `50K × $3.75/1M (write) + 50K × 19 × $0.30/1M (reads) = $0.19 + $0.29 = $0.48`
- **Saving: 84%**

### Cache TTL
- Default: **5 minutes** from last use
- Each time the cached block is read, the 5-minute window resets
- For long sessions, as long as the user asks one question every <5 minutes, the cache stays warm

### OpenAI Prompt Caching
Automatic for prompts longer than 1024 tokens — no special configuration. The same prefix is reused at 50% discount. Works with GPT-4o and o-series models.

```python
# OpenAI — nothing to configure; caching happens automatically
# Check cache usage in the response:
response = client.chat.completions.create(...)
print(response.usage.prompt_tokens_details)
# → PromptTokensDetails(cached_tokens=8192, audio_tokens=0)
```

### Integration into this project
In `routers/ai_chat.py`, replace the current approach:

```python
# BEFORE: build_system_prompt() loads 50K tokens every message
# AFTER: separate static context (cached) from dynamic user turn

async def chat(body: ChatRequest, llm=Depends(get_llm_service)):
    # Step 1: Build the retrieved context (can be cached at Layer 2-4)
    context = await retrieve_bid_context(body.bid_id, body.messages[-1].content)

    # Step 2: Send with cache_control on the context block
    response = await llm.chat_with_cache(
        static_context=context,   # marked cache_control
        messages=body.messages,   # the live conversation
    )
    return {"reply": response}
```

---

## Layer 8 — API Route Cache

### What it caches
The full HTTP response at the FastAPI route level — before any RAG or LLM work begins.

Dashboard-style endpoints that return aggregated bid data are called on every page refresh by every user. These are perfect candidates for short-lived API-level caches.

### What to store
```
hash(method + path + query_params + request_body) → HTTP response JSON
```

### TTL strategy
Very short — **30 seconds to 2 minutes**. Dashboard data must be reasonably fresh.

### Code example — `fastapi-cache2`
```bash
pip install fastapi-cache2[redis]
```

```python
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from fastapi_cache.decorator import cache
import redis.asyncio as aioredis

# In lifespan startup:
redis = aioredis.from_url("redis://localhost")
FastAPICache.init(RedisBackend(redis), prefix="rfpaiq-cache")

# On any route:
@router.get("/summary")
@cache(expire=60)  # 60-second TTL
async def get_bid_summary(bid_id: str) -> dict:
    # expensive aggregation...
    ...
```

### Lightweight alternative — in-memory with TTL
```python
from cachetools import TTLCache
import asyncio
import functools

_route_cache: TTLCache = TTLCache(maxsize=100, ttl=60)

def route_cache(ttl: int = 60):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = str(kwargs)
            if key in _route_cache:
                return _route_cache[key]
            result = await fn(*args, **kwargs)
            _route_cache[key] = result
            return result
        return wrapper
    return decorator
```

---

## Decision Matrix

| Layer | Cache Type | TTL | Storage | Saves | Use when |
|-------|-----------|-----|---------|-------|---------|
| 1 | Document chunks | 7–30 days | Disk / SQLite | Parse time | Documents re-indexed on restart |
| 2 | Embeddings | 30–90 days | Redis / Disk | API cost + time | Same chunks re-embedded repeatedly |
| 3 | Vector index | Session | RAM | Load latency | FAISS or local index |
| 4 | Retrieval results | 1–5 min | RAM / Redis | Search CPU | Repeated identical queries |
| 5 | Semantic results | 1–10 min | RAM / Vector DB | Full RAG pipeline | Paraphrased queries from many users |
| 6 | LLM responses | 1–10 min | SQLite / Redis | LLM cost + latency | Static / scheduled queries |
| 7 | Provider prompt | 5 min | Provider servers | 90% input cost | Long repeated context blocks |
| 8 | API responses | 30–120 s | Redis | All of the above | Dashboard / analytics endpoints |

---

## Cache Invalidation Strategies

Cache invalidation is famously the hardest problem in computer science. Here is how to handle it per layer:

### Event-driven invalidation (preferred)
Trigger cache clears on specific data events:
```python
async def upload_document(...):
    # ... save document ...
    invalidate_retrieval_cache()   # Layer 4
    invalidate_index()             # Layer 3
    # Layer 2 and 1 get new entries; old ones expire naturally
```

### TTL-based expiry (simplest)
Let cache entries expire automatically. Choose TTL based on how often data changes:
- Bids update: several times per day → TTL 2–5 min for retrieval, 30 s for API
- Embeddings never change (same model): TTL 90 days

### Version tag invalidation
Append a version counter to every cache key. Bump the counter on any data write — all old keys become orphans and expire naturally:
```python
_cache_version = 0

def get_cache_key(query: str) -> str:
    return f"v{_cache_version}:{hashlib.sha256(query.encode()).hexdigest()}"

def invalidate_all():
    global _cache_version
    _cache_version += 1  # all old keys now unreachable
```

### Do NOT do
- Don't try to delete individual keys from a distributed Redis cache under concurrent writes — race conditions
- Don't cache without TTL in production — stale entries accumulate forever
- Don't cache at Layer 6 (LLM response) for queries that include live data

---

## Implementation Plan for Sysco RFP AIQ

### Priority order (highest ROI first)

**1. Anthropic Prompt Cache (Layer 7) — implement immediately**
This project sends 50K-token prompts on every chat message. Adding `cache_control` to the retrieved context block cuts costs by ~84% on multi-turn conversations. One-line change in `services/llm/provider.py`.

**2. Embedding Cache (Layer 2) — implement before scaling**
Before adding semantic search to the project, cache embeddings to disk. LangChain `CacheBackedEmbeddings` is a one-line drop-in.

**3. Vector Index In-Memory (Layer 3) — implement with vector store**
When FAISS or Chroma is added, keep the loaded index as a module-level singleton in `dependencies.py` — consistent with the existing repository singleton pattern.

**4. Retrieval Result Cache (Layer 4) — implement with AI chat upgrade**
When `ai_chat.py` is upgraded to use tool calling (see `mcp_servers.md`), wrap the retrieval call with a 2-minute `cachetools.TTLCache`.

**5. API Route Cache (Layer 8) — implement for dashboard endpoints**
Add `fastapi-cache2` or a custom TTLCache decorator to `GET /api/bids` for dashboard loads. 60-second TTL.

### Packages needed
```bash
pip install diskcache        # Layer 1
pip install cachetools       # Layer 3, 4
pip install fastapi-cache2   # Layer 8 (optional — Redis backend)
# Layer 2: LangChain CacheBackedEmbeddings (already in langchain)
# Layer 7: Anthropic SDK cache_control (already in anthropic SDK)
```

---

## Quick Reference

```
COST SAVINGS (per 1M tokens, Claude Sonnet):
  No cache:          $3.00 input
  Prompt cache hit:  $0.30 input  ← 90% saving

LATENCY SAVINGS (per request):
  Layer 1 (doc parse):  save 2–5 s
  Layer 2 (embed):      save 50–200 ms per chunk
  Layer 3 (index load): save 500 ms–2 s
  Layer 4 (retrieval):  save 10–50 ms
  Layer 6 (LLM):        save 500 ms–3 s

RULE OF THUMB:
  If the input doesn't change → cache it.
  If the data underneath changes frequently → use a short TTL.
  If it's a provider API call with repeated context → use prompt cache.
```
