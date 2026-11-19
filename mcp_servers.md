# MCP Servers — What They Are and How to Use Them
## Sysco RFP AIQ

---

## 1. What is MCP?

**MCP (Model Context Protocol)** is an open standard created by Anthropic (released November 2024) that defines a universal way for AI models to connect to external tools, data sources, and services.

Think of it like a **USB standard for AI**. Before USB, every device needed its own proprietary connector. USB created one universal plug that any device could use. MCP does the same thing for AI:

- Before MCP: every AI integration was custom — each LLM needed its own connector for each tool (Slack, databases, APIs, etc.)
- With MCP: you write one MCP server for your tool, and any MCP-compatible AI (Claude Desktop, Claude Code, Cursor, Zed, etc.) can use it automatically.

### The Problem MCP Solves

Without MCP, if you want Claude to work with your data, you must:
1. Fetch all the data yourself
2. Format it into a giant text block
3. Stuff it into the system prompt
4. Hope it fits in the context window
5. Accept that it becomes stale the moment data changes

With MCP, Claude can:
1. Ask for exactly the data it needs
2. Get a fresh answer from your live systems
3. Ask follow-up questions to drill deeper
4. The context stays small and always current

---

## 2. How MCP Works (Architecture)

MCP uses a **client-server architecture** with three roles:

```
┌────────────────────────────────────────┐
│  HOST (e.g. Claude Desktop, Claude Code)│
│                                         │
│  ┌──────────────┐  ┌──────────────┐    │
│  │ MCP Client 1 │  │ MCP Client 2 │    │
│  └──────┬───────┘  └──────┬───────┘    │
└─────────┼─────────────────┼────────────┘
          │ JSON-RPC         │ JSON-RPC
          │                  │
   ┌──────▼──────┐    ┌──────▼──────┐
   │  MCP Server │    │  MCP Server │
   │  (Bids DB)  │    │  (M365 Mail)│
   └──────┬──────┘    └──────┬──────┘
          │                  │
   ┌──────▼──────┐    ┌──────▼──────┐
   │ FastAPI     │    │  Graph API  │
   │ backend_py  │    │  (M365)     │
   └─────────────┘    └─────────────┘
```

| Role | What it is | Example |
|------|-----------|---------|
| **Host** | The AI application the user talks to | Claude Desktop, Claude Code, VS Code with AI |
| **Client** | A connector inside the host that speaks MCP protocol | Built into the host — you don't write this |
| **Server** | Your code that exposes tools/data via MCP | `backend_py/mcp_server.py` (you write this) |

### Transport: How Client and Server Talk

MCP supports two transports:

| Transport | When to use | How it works |
|-----------|------------|--------------|
| **stdio** | Local tools (dev, scripts) | Server process stdin/stdout — simplest |
| **HTTP + SSE** | Remote/cloud deployments | Server listens on HTTP endpoint |

For Sysco RFP AIQ:
- During development → **stdio** (run the server as a local process)
- In production → **HTTP + SSE** (mount on the FastAPI app)

---

## 3. The Three Things MCP Servers Expose

### Tools
Functions the AI can call. Think of them as API endpoints the AI controls.

```python
@mcp.tool()
async def get_bid(bid_id: str) -> dict:
    """Get full details for a bid by its ID."""
    # AI calls this when it needs bid info
    ...
```

The AI decides when to call a tool — you don't have to pre-fetch and inject data.

### Resources
Data the AI can read. Identified by a URI, like a file or database view.

```python
@mcp.resource("bids://active")
async def active_bids() -> str:
    """All bids currently in Active or Solicitation status."""
    ...
```

Resources are for larger, less dynamic data that the AI reads passively.

### Prompts
Reusable prompt templates stored on the server side.

```python
@mcp.prompt()
async def bid_analysis_prompt(bid_id: str) -> str:
    """Standard prompt for analysing a bid's supplier response rate."""
    ...
```

---

## 4. The Current Problem in This Project

Look at `routers/ai_chat.py` — the `_build_system_prompt()` function:

```
Every single chat message triggers:
  → Load ALL bids from disk
  → Load ALL line items from disk
  → Load ALL email threads from disk
  → Load ALL documents from disk
  → Read up to 10 uploaded files (PDFs, Excel)
  → Serialize everything into one giant string
  → Inject into Claude's system prompt
  → Send to Claude (potentially 50,000+ tokens)
```

**Problems with this approach:**

| Problem | Impact |
|---------|--------|
| Every request loads the entire database | Slow as data grows |
| Static snapshot in prompt | If a bid updates mid-conversation, Claude has wrong data |
| Token waste | Claude reads all 50 bids to answer a question about 1 |
| Context window limit | When you have 500+ bids, this will break |
| No live data | Uploaded files are re-read on every message |

**With MCP tools, the same chat would work like this:**

```
User: "What's the response rate for BID-2024-089?"

Claude thinks: I need the bid details and its solicited suppliers.
Claude calls:  get_bid("BID-2024-089")
Claude gets:   { id, customer, solicitedSuppliers: [...], ... }
Claude answers: "BID-2024-089 for Riverside USD has contacted 8 suppliers,
                 5 have responded (62.5% response rate)."

Total tokens used: ~500 instead of 50,000
Data accuracy:     Live, not a stale snapshot
```

---

## 5. Specific MCP Servers for Sysco RFP AIQ

### Server 1 — Bids MCP Server

The most important one. Gives Claude direct, on-demand access to bid data.

**Tools to expose:**

| Tool | Parameters | What it returns |
|------|-----------|----------------|
| `list_bids` | `segment`, `status`, `region` | Filtered list of bids |
| `get_bid` | `bid_id` | Full bid object + line items + suppliers |
| `get_bid_line_items` | `bid_id` | All line items for a bid |
| `get_bid_emails` | `bid_id` | Email thread history |
| `update_bid_status` | `bid_id`, `status` | Update bid status |
| `get_bid_summary` | `bid_id` | Response rate, at-risk status, supplier counts |

**Example code (`backend_py/mcp_server.py`):**

```python
from mcp.server.fastmcp import FastMCP
from repositories.bids_repo import BidsRepository
from repositories.line_items_repo import LineItemsRepository

mcp = FastMCP("sysco-rfpaiq-bids")

@mcp.tool()
async def get_bid(bid_id: str) -> dict:
    """Get full details for a bid including line items and solicited suppliers."""
    repo = BidsRepository("data/bids.json")
    bid = await repo.find_one(lambda b: b.get("id") == bid_id)
    if not bid:
        return {"error": f"Bid {bid_id!r} not found"}
    return bid

@mcp.tool()
async def list_bids(
    segment: str = "",
    status: str = "",
    region: str = "",
) -> list[dict]:
    """List bids with optional filters for segment, status, or region."""
    repo = BidsRepository("data/bids.json")
    bids = await repo.load()
    if segment:
        bids = [b for b in bids if b.get("segment") == segment]
    if status:
        bids = [b for b in bids if b.get("status") == status]
    if region:
        bids = [b for b in bids if b.get("region") == region]
    # Return a lean summary — Claude can call get_bid() for full details
    return [{"id": b["id"], "customer": b.get("customer"), "status": b.get("status"),
             "segment": b.get("segment"), "customerDue": b.get("customerDue")} for b in bids]

@mcp.tool()
async def get_bid_summary(bid_id: str) -> dict:
    """Get response rate, at-risk status, and supplier activity summary for a bid."""
    repo = BidsRepository("data/bids.json")
    bid = await repo.find_one(lambda b: b.get("id") == bid_id)
    if not bid:
        return {"error": f"Bid {bid_id!r} not found"}
    solicited = bid.get("solicitedSuppliers", [])
    responded = sum(1 for s in solicited
                    if s.get("status") in {"Responded", "Issues Found", "Complete"})
    return {
        "bid_id": bid_id,
        "customer": bid.get("customer"),
        "status": bid.get("status"),
        "total_solicited": len(solicited),
        "total_responded": responded,
        "response_rate": f"{round(responded / len(solicited) * 100)}%" if solicited else "0%",
        "customer_due": bid.get("customerDue"),
        "internal_due": bid.get("internalDue"),
        "at_risk": bid.get("customerDue", "") < bid.get("internalDue", ""),
    }
```

---

### Server 2 — Suppliers MCP Server

Lets Claude search and reason about the supplier database.

**Tools to expose:**

| Tool | Parameters | What it returns |
|------|-----------|----------------|
| `search_suppliers` | `query`, `category`, `segment` | Matching suppliers |
| `get_supplier` | `supplier_id` | Full supplier record |
| `auto_populate_suppliers` | `item_categories` | Recommended suppliers for a bid |

---

### Server 3 — Document Vault MCP Server

Gives Claude access to uploaded documents as readable resources.

**Resources to expose:**

```
documents://all           → all document metadata
documents://bid/{bid_id}  → documents for a specific bid
documents://file/{doc_id} → actual file content (extracted text)
```

**Why this matters:** Right now, `ai_chat.py` reads up to 10 files on every request regardless of whether Claude needs them. With an MCP resource, Claude only reads a document when it specifically asks for it.

---

### Server 4 — Email MCP Server

Connects Claude directly to the email thread history and optionally to the Microsoft Graph API live inbox.

**Tools to expose:**

| Tool | Parameters | What it returns |
|------|-----------|----------------|
| `get_email_threads` | `bid_id` | All email threads for a bid |
| `get_unmatched_emails` | — | Emails that didn't match any bid |
| `search_emails` | `supplier`, `subject`, `date_from` | Filtered email search |

---

### Server 5 — RFP Analysis MCP Server

Wraps the AI-powered analysis routes as callable tools.

**Tools to expose:**

| Tool | What it does |
|------|-------------|
| `analyse_supplier_response` | Score a supplier's pricing document |
| `extract_pricing` | Pull line items and prices from a document |
| `chat_with_bid_context` | Ask a question with a specific bid pre-loaded as context |

---

## 6. How to Build the MCP Server

### Step 1: Install the SDK

```bash
cd backend_py
pip install mcp
```

### Step 2: Create `mcp_server.py`

```python
# backend_py/mcp_server.py
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="sysco-rfpaiq",
    instructions="Tools for the Sysco RFP AIQ bid management platform."
)

# ── Import and register tools from each domain ──
from mcp_tools.bids import register_bid_tools
from mcp_tools.suppliers import register_supplier_tools
from mcp_tools.documents import register_document_tools
from mcp_tools.emails import register_email_tools

register_bid_tools(mcp)
register_supplier_tools(mcp)
register_document_tools(mcp)
register_email_tools(mcp)

if __name__ == "__main__":
    mcp.run()   # runs via stdio — Claude Code connects to this process
```

### Step 3: Run it locally (stdio mode)

```bash
cd backend_py
.\.venv\Scripts\python.exe mcp_server.py
```

### Step 4: Connect to Claude Code

Add this to `.claude/settings.json` in your project root:

```json
{
  "mcpServers": {
    "sysco-rfpaiq": {
      "command": "python",
      "args": ["backend_py/mcp_server.py"],
      "cwd": "c:/Sysco/feature_vaibhav_python/feature_vaibhav_python"
    }
  }
}
```

After saving, Claude Code can use all the tools automatically. You'll see them appear in the Claude Code interface.

### Step 5: Connect to Claude Desktop

Open Claude Desktop → Settings → Developer → Edit Config:

```json
{
  "mcpServers": {
    "sysco-rfpaiq": {
      "command": "C:/Sysco/feature_vaibhav_python/feature_vaibhav_python/backend_py/.venv/Scripts/python.exe",
      "args": ["C:/Sysco/feature_vaibhav_python/feature_vaibhav_python/backend_py/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop. You'll see a hammer icon (🔨) in the chat — that means MCP tools are active.

---

## 7. Integrating MCP into the Existing AI Chat Router

The biggest improvement: rewrite `routers/ai_chat.py` to use MCP tool calling instead of a pre-built mega-prompt.

### Before (current approach):
```
Request arrives
  → Load 5 data files (all bids, all items, all emails...)
  → Build 50,000-token system prompt
  → Send everything to Claude
  → Claude reads the whole thing, answers one question
```

### After (MCP tool-calling approach):
```
Request arrives
  → Send a lightweight system prompt ("You are RFP AIQ assistant")
  → Claude reads user question
  → Claude calls get_bid("BID-089") — fetches only what it needs
  → Claude calls get_bid_emails("BID-089") — follows up
  → Claude answers with live, precise data
  → ~500 tokens used instead of 50,000
```

**How to wire it up with LangChain (already used in this project):**

```python
# In routers/ai_chat.py — updated approach
from langchain_core.tools import tool
from langchain_anthropic import ChatAnthropic

@tool
async def get_bid_tool(bid_id: str) -> str:
    """Get full bid details by ID."""
    # calls the repository directly
    ...

@tool
async def list_bids_tool(status: str = "", segment: str = "") -> str:
    """List bids with optional filters."""
    ...

# Bind tools to the model
llm_with_tools = llm.bind_tools([get_bid_tool, list_bids_tool, search_suppliers_tool])
```

The LLM then decides autonomously when to call each tool — you just define what they do.

---

## 8. Production: HTTP + SSE Transport

For a deployed server (not localhost), use the streamable HTTP transport:

```python
# backend_py/main.py — add to existing FastAPI app
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.transport.sse import add_sse_routes

mcp = FastMCP("sysco-rfpaiq")
# ... register tools ...

# Mount MCP onto existing FastAPI app at /api/mcp
add_sse_routes(app, mcp, path="/api/mcp")
```

Claude Code can then connect via HTTP:

```json
{
  "mcpServers": {
    "sysco-rfpaiq": {
      "url": "https://rfpaiq.sysco.com/api/mcp"
    }
  }
}
```

---

## 9. Summary: What MCP Unlocks for This Project

| Current limitation | With MCP |
|-------------------|----------|
| All bids loaded on every chat message | Claude fetches only the bid it needs |
| Context window fills up with 500+ bids | Context stays small regardless of data size |
| Stale data snapshot in every prompt | Live data on every tool call |
| `ai_chat.py` reads 10 files every request | Claude reads a document only if relevant |
| Claude Code cannot inspect live bid data | Claude Code can call `get_bid()` during development |
| No way to let Claude update bid status | MCP tool `update_bid_status()` enables it safely |

---

## 10. Required Package

```bash
pip install mcp
```

MCP is published by Anthropic at `pypi.org/project/mcp`. The Python SDK includes:
- `mcp.server.fastmcp.FastMCP` — high-level server builder (recommended)
- `mcp.server.Server` — low-level server for custom control
- `mcp.client` — for writing MCP clients (usually not needed)

Full documentation: https://modelcontextprotocol.io
