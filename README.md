# NextQuestion

**Graph-Augmented RAG pipeline that turns PDF textbooks into a smart Obsidian knowledge base with natural-language querying and page-level citations.**

---

## What Is This?

NextQuestion is a tool for building a **personal knowledge base** from medical (and other) textbooks. It:

1. **Splits** PDFs into semantic fragments based on table of contents
2. **Generates** catalogue cards (summary + keywords + questions) via LLM
3. **Builds** Obsidian notes with automatic wikilinks between related topics
4. **Answers** your questions, citing specific pages from the textbook

**Result:** instead of flipping through a 1500-page textbook, you type a question in the terminal and get an answer with clickable links to the relevant Obsidian notes.

---

## ✨ Features

- **Semantic chunking** — smart PDF splitting by TOC or headings
- **LLM enrichment** — catalogue card generation with caching
- **Link graph** — automatic wikilinks between related fragments
- **Entity hubs** — merging mentions of the same term across chapters
- **HyDE search** — semantic search with hypothetical document generation
- **Interactive REPL** — conversational question-answering mode
- **TUI wizard** — interactive setup without manual JSON editing
- **Token-efficient** — LLM call caching, subsequent runs are free
- **Fault-tolerant** — FTS5 with LIKE fallback, API retries

---

## Quick Start (3 minutes)

### 1. Clone and install dependencies

```bash
git clone https://github.com/yourusername/nextquestion.git
cd nextquestion
pip install -r requirements.txt
```

### 2. Run the setup wizard

```bash
python nextquestion.py init
```

The wizard will walk you through:
- Choosing an LLM backend (OpenAI, DeepSeek, Ollama, etc.)
- Entering your API key (Check your API, model and base URL!!!!! You can change it manually)
- Specifying the Obsidian Vault path
- Creating `config.json`

### 3. Load your first PDF

```bash
python nextquestion.py chunk path/to/your/book.pdf
```

### 4. Generate catalogue cards

```bash
python nextquestion.py enrich
```

### 5. Build the note graph

```bash
python nextquestion.py link
```

### 6. Ask a question

```bash
python nextquestion.py query "what is glomerulonephritis"
```

**Done!** Open Obsidian → `RAG_Knowledge_Base` folder → you'll see the note structure with automatic links.

---

## Installation

### Requirements

- Python 3.10+
- Obsidian (for viewing results)
- Access to an LLM API (OpenAI, OpenRouter, DeepSeek, local model etc.)

### Dependencies

```bash
pip install pymupdf requests pydantic pyyaml
```

Or via `requirements.txt`:

```txt
pymupdf>=1.23.0
requests>=2.28.0
pydantic>=2.0.0
pyyaml>=6.0
```

### Optional: local model (Ollama)

```bash
# Install Ollama: https://ollama.ai
ollama pull qwen2.5:14b (For example. Check your PC capabilities)
```

In the wizard, choose `Ollama (local)` — no API key needed.

---

## Configuration

After running `init`, a `config.json` file is created. Here are all available fields:

```json
{
  "vault_path": "/path/to/obsidian/vault",
  "store_root": "RAG_Knowledge_Base",
  "db_path": "nextquestion.db",
  "cache_namespace": "nq-1.0",
  "chunk_max_chars": 9000,
  "chunk_min_chars": 400,
  "api_key": "sk-...",
  "base_url": "https://api.openai.com/v1", 
  "model": "gpt-4o-mini",
  "folders": {
    "excerpts": "Notes",
    "sections": "Sections",
    "entities": "Entities"
  },
  "labels": {
    "brief": "Brief",
    "answers": "Answers",
    "keywords": "Keywords",
    "full": "Full source text",
    "links": "Links",
    "chapter": "Chapter",
    "prev": "Previous",
    "next": "Next",
    "topics": "Related topics & entities"
  },
  "hub_labels": {
    "section": "Excerpts in this section",
    "entity": "Entity covered in",
    "entity_tail": "excerpts, possibly across different books"
  },
  "prompts": {
    "enrich": "...",
    "hyde": "...",
    "answer": "..."
  },
  "entity_patterns": [
    "\\bCD\\d+\\b",
    "\\bIL-\\d+\\b"
  ]
}
```

### Field descriptions

| Field | Purpose | Example |
|-------|---------|---------|
| `vault_path` | Path to Obsidian Vault root | `"/home/user/Documents/Obsidian"` |
| `store_root` | Subfolder inside Vault for the knowledge base | `"RAG_Knowledge_Base"` |
| `db_path` | Path to SQLite database | `"nextquestion.db"` |
| `cache_namespace` | Cache prefix (change when updating prompts) | `"nq-1.0"` |
| `chunk_max_chars` | Maximum chunk size | `9000` |
| `chunk_min_chars` | Minimum chunk size | `400` |
| `api_key` | LLM API key | `"sk-..."` |
| `base_url` | API endpoint URL | `"https://api.openai.com/v1"` |
| `model` | Model name | `"gpt-4o-mini"` |
| `folders` | Folder names for notes | `{"excerpts": "Notes", ...}` |
| `labels` | Note labels (customizable) | `{"brief": "Summary", ...}` |
| `prompts` | System prompts for LLM | See below |
| `entity_patterns` | Regex patterns for entity extraction | `["\\bIL-\\d+\\b"]` |

### Environment variables

You can set these instead of editing `config.json`:

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export RAG_MODEL="gpt-4o-mini"
export RAG_VAULT="/path/to/vault"
```

---

## Workflow Stages

### 1. `chunk` — Split PDF

```bash
python nextquestion.py chunk path/to/book.pdf [--start-page 10] [--end-page 100] [--dry-run]
```

**What it does:**
- Reads PDF via PyMuPDF
- Extracts table of contents (TOC)
- Splits text into semantic fragments by headings
- Saves to `chunks_raw` table (0 LLM calls)

**Flags:**
- `--start-page`, `--end-page` — process only a page range
- `--dry-run` — preview what would be done without writing to DB

**Example:**
```bash
python nextquestion.py chunk ~/Books/Robbins.pdf --start-page 45 --end-page 120
```

### 2. `enrich` — Generate catalogue cards

```bash
python nextquestion.py enrich [--limit 10]
```

**What it does:**
- Takes chunks without cards (or with `src="fallback"`)
- Sends text to LLM with the `enrich` prompt
- Receives JSON: `{summary, keywords, questions}`
- Saves to `chunk_enrich` table
- Caches responses (subsequent calls are free)

**Flags:**
- `--limit N` — process only the first N chunks (for testing)

**Cost:** ~400 tokens per chunk (approximately $0.001 per chunk for GPT-4o-mini)

### 3. `link` — Build the graph

```bash
python nextquestion.py link [--alias-db path/to/aliases.db]
```

**What it does:**
- Creates Obsidian notes in `Notes/` folder
- Creates chapter hubs in `Sections/`
- Creates entity hubs in `Entities/`
- Generates wikilinks between related chunks
- Saves links to `links` table (0 LLM calls)

**Flags:**
- `--alias-db` — path to alias database for entity merging (optional)

### 4. `reindex` — Build search index

```bash
python nextquestion.py reindex
```

**What it does:**
- Creates FTS5 index for fast full-text search
- Runs automatically on first `query`

### 5. `query` — Search and answer

```bash
python nextquestion.py query "your question"
```

**What it does:**
- Runs HyDE (hypothetical document generation)
- Searches for relevant chunks via FTS5
- Traverses the link graph to expand context
- Generates an answer via LLM with streaming
- Outputs clickable links to Obsidian notes

**Without arguments** — launches interactive REPL:

```bash
python nextquestion.py query
```

### 6. `init` — Setup wizard

```bash
python nextquestion.py init
```

Launches the interactive TUI wizard with arrow-key navigation.

### 7. `example` — Config example

```bash
python nextquestion.py example
```

Prints a template `config.json` with comments.

---

## Advanced Usage

### Processing multiple books

```bash
# Book 1
python nextquestion.py chunk ~/Books/MadebyMisa.pdf
python nextquestion.py enrich
python nextquestion.py link

# Book 2
python nextquestion.py chunk ~/Books/MadebyMisa.pdf
python nextquestion.py enrich
python nextquestion.py link
```

Entities from different books automatically merge into shared hubs (e.g., "IL-6" from both Robbins and Harrison will appear in one `ENTITY__IL-6.md`).

### Localizing the interface

Edit `config.json` to change labels and prompts to your language:

```json
{
  "labels": {
    "brief": "Summary",
    "answers": "Answers these questions",
    "keywords": "Key terms",
    "full": "Full source text",
    "links": "Connections",
    "chapter": "Chapter",
    "prev": "Previous",
    "next": "Next",
    "topics": "Related topics"
  }
}
```

Then run `enrich` with a new `cache_namespace` to regenerate cards:

```json
{
  "cache_namespace": "nq-1.1-custom"
}
```

### Extracting specific entities

Add regex patterns to `entity_patterns`:

```json
{
  "entity_patterns": [
    "\\b(?:IL-\\d+|TNF-?[A-Za-z]?|CD\\d+)\\b",
    "\\b(?:HbA1c|CRP|NT-proBNP)\\b"
  ]
}
```

These entities will be extracted automatically and appear in `Entities/` hubs.

### Re-indexing after changes

If you've manually edited notes or changed prompts:

```bash
# Clear cache (delete nextquestion_cache.db)
rm nextquestion_cache.db

# Re-index
python nextquestion.py reindex

# Re-enrich
python nextquestion.py enrich
```
ERRORS:

## Error Reference (what each message means and how to fix it)

NextQuestion never fails silently: every problem prints a readable message.
This table maps each message to its cause and its fix.

### Connection & API errors (enrich / query stages)

| You see | What it means | How to fix |
|---|---|---|
| `LLM failed: ... Connection refused` / `Max retries exceeded` | Endpoint unreachable: typo in `base_url`, or the local server is not running | Check the URL; for Ollama run `ollama serve`; verify the port (`11434` / `8080`) |
| `LLM failed: HTTP 404: ...` | Wrong path or wrong model name | OpenAI-compatible URLs usually end in `/v1`; verify the model name (`ollama list`, provider docs) |
| `LLM failed: HTTP 401 / 403: ...` | API key is invalid, expired, or belongs to another provider | Regenerate the key; update `api_key` in `config.json` or `OPENAI_API_KEY` |
| `LLM failed: HTTP 429: ...` | Rate limit or zero balance | Wait a minute / top up; the script retries with backoff automatically before giving up |
| `LLM failed: HTTP 400: ...` | Provider rejected a request parameter | The script auto-drops `response_format` → `temperature` → `reasoning_effort` and retries; if it still fails, check the model name |
| `LLM failed: empty response` | The model returned nothing (thinking models can burn all tokens on reasoning) | Retry, or switch model with `--model` |
| `Query expansion parsing failed: ...` | The HyDE step returned broken JSON | Simply re-ask the question; if it repeats, switch model |
| `⚠️ Stream interrupted (...)` | Network dropped mid-answer | The partial answer is printed; re-ask for the full one |
| `❌ Model returned no tokens...` | Provider returned an empty stream | Retry or `--model other-model` |

### Pipeline errors (chunk / enrich / link stages)

| You see | What it means | How to fix |
|---|---|---|
| `Config file not found: config.json` | No config yet | `python nextquestion_eng.py init` (wizard) or `example > config.json` |
| `'vault_path' is not set...` / `'api_key' is not set...` | A required field is empty | Fill it in `config.json`, or export `RAG_VAULT` / `OPENAI_API_KEY` |
| `File not found: ...` | Wrong path to the source | Check the path; wrap paths with spaces in quotes |
| `Unsupported format: .xyz` | Only `.pdf`, `.md`, `.markdown`, `.txt` are accepted | Convert the source first |
| `PDF is password-protected` | Encrypted PDF | Remove the protection (Acrobat, qpdf) |
| `Could not split source: no text (possibly a scan without OCR)` | The PDF is a picture-only scan | Run OCR first, or find a text version |
| `Could not decode text file: ...` | Unknown text encoding | Re-save the file as UTF-8 |
| `❌ Run 'chunk' first.` | You called `link`/`query` on an empty database | Follow the order: chunk → enrich → link |
| `⚠️ N excerpts missing cards. Run 'enrich' first.` | Some chunks have no LLM cards | Run `enrich` (already-cached chunks cost nothing) |
| `⚠️ FTS5 unavailable ... LIKE fallback` | Your SQLite was built without FTS5 | Everything still works, just slower; upgrade Python/SQLite for speed |
| `❌ Nothing found. Try rephrasing...` | Search returned zero chunks | Make sure `enrich` + `link` + `reindex` have run; rephrase or add a domain term |

### Obsidian issues

| You see / observe | What it means | How to fix |
|---|---|---|
| Notes don't appear in Obsidian | Wrong `vault_path` / `store_root` | Check both; notes live in `<vault>/<store_root>/Notes/...` |
| `obsidian://` link doesn't open | Obsidian is closed or another vault is active | Open Obsidian with that vault, or use the fallback path printed next to the link |
| Graph looks empty | The `link` stage hasn't run | Run `python nextquestion_eng.py link` |

### How to read the prefixes

- `❌` — fatal for this command; nothing was written, fix and re-run.
- `⚠️` — warning; the stage continued in degraded mode.
- `✅ / 💰 /  / 🕸️` — progress and statistics, all good.

