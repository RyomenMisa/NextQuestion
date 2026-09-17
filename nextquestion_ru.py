#!/usr/bin/env python3
"""nextquestion — graph-augmented RAG pipeline: documents → SQLite → Obsidian graph → streamed answers.

Stages:
  chunk   — semantic ingestion of PDF/MD/TXT into chunks_raw (0 LLM calls)
  enrich  — LLM catalogue cards (summary, keywords, questions) into chunk_enrich (cached)
  link    — Obsidian notes, chapter hubs, entity hubs, wikilinks (0 LLM calls)
  reindex — build FTS5 search index over chunks
  query   — HyDE + graph traversal + streamed answer (1+ LLM calls)
  init    — interactive config wizard (TUI)
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

try:
    import pymupdf as fitz
except ImportError:
    import fitz

import requests
import yaml
from pydantic import BaseModel, Field, model_validator

try:
    import termios
    import tty
    import select
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False

LOG = logging.getLogger("nextquestion")
ALLOWED_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}
HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*)?\s*[A-ZА-ЯЁ][A-Za-zА-Яа-яё0-9 ,\-/()]{3,70}$")
FTS_STOP_WORDS = {"this", "that", "with", "from", "which", "their", "может", "быть", "если",
                   "того", "чем", "как", "and", "or", "not", "near", "also", "into", "over"}
SEED_LIMIT = 5

DEFAULT_LABELS = {
    "brief": "Brief", "answers": "Answers", "keywords": "Keywords",
    "full": "Full source text", "links": "Links", "chapter": "Chapter",
    "prev": "Previous", "next": "Next", "topics": "Related topics & entities",
}
DEFAULT_HUB_LABELS = {
    "section": "Excerpts in this section",
    "entity": "Entity covered in",
    "entity_tail": "excerpts, possibly across different books",
}

DEFAULT_ENRICH_PROMPT = """You are a library analyst. Create a catalogue card for the document fragment.
Return ONLY compact JSON with keys: summary, keywords, questions.
- summary: 2-3 sentences about the main ideas, entities, mechanisms, figures, conclusions.
- keywords: 6-10 key terms in the original language of the source text.
- questions: 2-4 questions this fragment actually answers (from the point of view of a practitioner or student).
IMPORTANT: the JSON values MUST be in the same language as the source text. No backslashes, no ideograms."""

HYDE_PROMPT = """You are an expert subject librarian.
User question: "{query}"
1. Fix typos and rephrase in precise, professional domain language.
2. Extract 3-5 key terms, mechanisms, syndromes, or markers.
3. Generate 2 hypothetical sentences (HyDE) that an authoritative textbook would contain to answer this question.
Return ONLY JSON: {{"corrected_query": "...", "terms": ["..."], "hyde": ["..."]}}"""

ANSWER_SYSTEM = """You are an expert consultant in the subject area of the provided context. Answer STRICTLY based on context.
1. Explain mechanisms and logical connections between the mentioned phenomena.
2. Cite sources in the format: [[Note Name]] (pp. X-Y).
3. If information is missing, say so honestly."""

CONFIG_EXAMPLE = """{
  "vault_path": "/path/to/obsidian/vault",
  "store_root": "RAG_Knowledge_Base",
  "db_path": "nextquestion.db",
  "cache_namespace": "nq-1.0",
  "chunk_max_chars": 9000,
  "chunk_min_chars": 400,
  "api_key": "sk-...",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "folders": {"excerpts": "Notes", "sections": "Sections", "entities": "Entities"},
  "labels": {"brief": "Brief", "answers": "Answers", "keywords": "Keywords",
             "full": "Full source text", "links": "Links", "chapter": "Chapter",
             "prev": "Previous", "next": "Next", "topics": "Related topics & entities"},
  "hub_labels": {"section": "Excerpts in this section",
                 "entity": "Entity covered in",
                 "entity_tail": "excerpts, possibly across different books"},
  "prompts": {
    "enrich": "<system prompt for catalogue cards — language of JSON output follows this>",
    "hyde":   "<system prompt for query expansion>",
    "answer": "<system prompt for final answer synthesis>"
  },
  "entity_patterns": ["\\\\bCD\\\\d+\\\\b", "\\\\bIL-\\\\d+\\\\b"]
}
"""

BACKEND_PRESETS = {
    "OpenRouter": {"base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-3.5-sonnet"},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "Ollama (local)": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:14b"},
    "llama.cpp (local)": {"base_url": "http://localhost:8080/v1", "model": "local-model"},
}


class NextQuestionError(RuntimeError):
    """Base package error."""


class ConfigError(NextQuestionError):
    """Missing or invalid configuration."""


class SourceError(NextQuestionError):
    """Source unreadable or unsupported format."""


class LLMError(NextQuestionError):
    """LLM endpoint unreachable or returned invalid response."""


class EnrichCard(BaseModel):
    """Catalogue card for a document fragment."""
    summary: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def coerce(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"summary": data}
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict):
                    inner = cls.coerce(value)
                    if inner.get("summary"):
                        return inner

            def as_list(x: Any) -> list[str]:
                if isinstance(x, str):
                    return [x]
                return [str(i) for i in x] if isinstance(x, list) else []

            return {
                "summary": str(data.get("summary") or data.get("description") or "").strip(),
                "keywords": as_list(data.get("keywords") or data.get("terms")),
                "questions": as_list(data.get("questions") or data.get("answers")),
            }
        return {"summary": ""}


class QueryExpansion(BaseModel):
    """Result of HyDE query expansion."""
    corrected_query: str = ""
    terms: list[str] = Field(default_factory=list)
    hyde: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def coerce(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"corrected_query": data}
        if not isinstance(data, dict):
            return {}

        def as_list(x: Any) -> list[str]:
            if isinstance(x, str):
                return [x]
            return [str(i) for i in x] if isinstance(x, list) else []

        return {
            "corrected_query": str(data.get("corrected_query") or data.get("query") or ""),
            "terms": as_list(data.get("terms")),
            "hyde": as_list(data.get("hyde")),
        }


class AppConfig(BaseModel):
    """Pipeline configuration with environment overrides."""
    vault_path: str = ""
    store_root: str = "RAG_Knowledge_Base"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    db_path: str = "nextquestion.db"
    cache_namespace: str = "nq-1.0"
    chunk_max_chars: int = 9000
    chunk_min_chars: int = 400
    enrich_prompt: str = DEFAULT_ENRICH_PROMPT
    entity_patterns: list[str] = Field(default_factory=list)
    folders: dict[str, str] = Field(default_factory=lambda: {"excerpts": "Notes",
                                                             "sections": "Sections",
                                                             "entities": "Entities"})
    labels: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_LABELS))
    hub_labels: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_HUB_LABELS))
    prompts: dict[str, str] = Field(default_factory=lambda: {
        "enrich": DEFAULT_ENRICH_PROMPT,
        "hyde": HYDE_PROMPT,
        "answer": ANSWER_SYSTEM,
    })

    @property
    def raw_db(self) -> Path:
        return Path(self.db_path).expanduser().resolve()

    @property
    def cache_db(self) -> Path:
        p = self.raw_db
        return p.parent / f"{p.stem}_cache.db"

    @property
    def vault_root(self) -> Path:
        return Path(self.vault_path).expanduser() / self.store_root

    def label(self, key: str) -> str:
        return self.labels.get(key, DEFAULT_LABELS[key])

    def hub_label(self, key: str) -> str:
        return self.hub_labels.get(key, DEFAULT_HUB_LABELS[key])

    def folder(self, key: str) -> str:
        fallback = {"excerpts": "Notes", "sections": "Sections", "entities": "Entities"}
        return self.folders.get(key, fallback[key])

    def prompt(self, key: str, default: str = "") -> str:
        return self.prompts.get(key) or default


def load_config(path: Path) -> AppConfig:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("config read error: %s", exc)
    cfg = AppConfig(**{k: v for k, v in data.items() if k in AppConfig.model_fields})
    for key, env in (("api_key", "OPENAI_API_KEY"),
                     ("base_url", "OPENAI_BASE_URL"),
                     ("model", "RAG_MODEL"),
                     ("vault_path", "RAG_VAULT")):
        if not getattr(cfg, key):
            value = os.environ.get(env, "")
            if value:
                setattr(cfg, key, value)
    cfg.db_path = str(Path(cfg.db_path).expanduser())
    return cfg


def clean_text(value: str) -> str:
    return re.sub(r" {2,}", " ", re.sub(r"\s+", " ", (value or "").replace("\x00", ""))).strip()


def normalize_text(value: str) -> str:
    if not value:
        return ""
    value = value.lower().replace("ё", "е").replace("\\", " ")
    value = re.sub(r"[^\w\s-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", clean_text(value))
    return re.sub(r"\s+", " ", value).strip(" .")[:80] or "chunk"


def first_json_blob(text: str) -> str:
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    if fence:
        return fence.group(1)
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start:i + 1]
    return ""


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def wikilink(filename: str, label: str) -> str:
    return f"[[{filename[:-3]}|{label}]]"


@dataclass(slots=True)
class Chunk:
    hash: str
    book: str
    chapter: str
    title: str
    page_start: int
    page_end: int
    text: str
    ord: int


class LLMClient:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {cfg.api_key}"}

    def complete_raw(self, messages: list[dict], max_tokens: int = 400) -> str:
        body: dict[str, Any] = {"model": self.cfg.model, "messages": messages, "temperature": 0.0,
                                "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                                "reasoning_effort": "low"}
        drop_order = ["response_format", "temperature", "reasoning_effort"]
        last: Exception | None = None
        for attempt in range(4):
            try:
                res = requests.post(self.url, headers=self.headers, json=body, timeout=(30, 180))
                if res.status_code == 200:
                    msg = ((res.json().get("choices") or [{}])[0].get("message") or {})
                    raw = msg.get("content") or ""
                    if not raw.strip():
                        raw = first_json_blob(msg.get("reasoning_content") or "")
                    if raw.strip():
                        return raw
                    last = LLMError("empty response")
                else:
                    last = LLMError(f"HTTP {res.status_code}: {res.text[:200]}")
                    if res.status_code in (401, 403):
                        raise last
                    if res.status_code == 400:
                        while drop_order:
                            param = drop_order.pop(0)
                            if param in body:
                                body.pop(param)
                                break
                time.sleep(min(2 ** attempt, 10))
            except requests.exceptions.RequestException as exc:
                last = exc
                time.sleep(min(2 ** attempt, 10))
        raise LLMError(f"LLM failed: {last}")

    def stream_text(self, messages: list[dict], max_tokens: int = 2000) -> Iterator[str]:
        body = {"model": self.cfg.model, "messages": messages, "temperature": 0.1,
                "max_tokens": max_tokens, "stream": True, "reasoning_effort": "low"}
        with requests.post(self.url, headers=self.headers, json=body, stream=True,
                           timeout=(15, 180)) as res:
            res.raise_for_status()
            for line in res.iter_lines():
                if not line:
                    continue
                text = line.decode("utf-8", errors="ignore")
                if not text.startswith("data: "):
                    continue
                payload = text[6:]
                if payload == "[DONE]":
                    return
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choice = (parsed.get("choices") or [{}])[0]
                delta = choice.get("delta") or choice.get("message") or {}
                content = delta.get("content") or ""
                if content:
                    yield content


class Store:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS chunks_raw (hash TEXT PRIMARY KEY, book TEXT NOT NULL, chapter TEXT NOT NULL,
        title TEXT NOT NULL, page_start INTEGER, page_end INTEGER, text TEXT NOT NULL, ord INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS chunk_enrich (hash TEXT PRIMARY KEY, summary TEXT NOT NULL,
        keywords TEXT NOT NULL, questions TEXT NOT NULL, src TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS catalog (hash TEXT PRIMARY KEY, book TEXT, chapter TEXT, title TEXT,
        page_start INTEGER, page_end INTEGER, summary TEXT, keywords TEXT, questions TEXT, entities TEXT, path TEXT);
    CREATE TABLE IF NOT EXISTS links (a TEXT NOT NULL, b TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY (a, b, kind));
    CREATE TABLE IF NOT EXISTS entities (name TEXT PRIMARY KEY, norm TEXT NOT NULL, chunks TEXT NOT NULL);
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.path = cfg.raw_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.conn.close()

    def fts_available(self) -> bool:
        try:
            self.conn.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(x)")
            self.conn.execute("DROP TABLE temp.fts_probe")
            return True
        except Exception:
            return False


class Cache:
    def __init__(self, cfg: AppConfig):
        self.path = cfg.cache_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.execute("CREATE TABLE IF NOT EXISTS llm_cache "
                          "(key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at TEXT NOT NULL)")
        self.conn.commit()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.conn.close()

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return row["response"] if row else None

    def put(self, key: str, response: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache (key, response, created_at) VALUES (?, ?, ?)",
            (key, response, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()


class Ingestor:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def extract_pages(self, path: Path, start: int | None, end: int | None):
        if path.suffix.lower() == ".pdf":
            doc = fitz.open(str(path))
            try:
                if doc.needs_pass:
                    doc.authenticate("")
                if doc.needs_pass:
                    raise SourceError("PDF is password-protected")
                toc = doc.get_toc() or []
                total = len(doc)
                if start is not None and end is not None and start > end:
                    start, end = end, start
                first = max(0, start - 1) if start is not None else 0
                last = min(total, end) if end is not None else total
                pages = [(i + 1, doc[i].get_text("text").replace("\x00", "")) for i in range(first, last)]
                return pages, toc, first + 1
            finally:
                doc.close()
        raw = path.read_bytes()
        for enc in ("utf-8", "cp1251"):
            try:
                return [(1, raw.decode(enc).replace("\x00", ""))], [], 1
            except UnicodeDecodeError:
                continue
        return [(1, raw.decode("utf-8", errors="ignore").replace("\x00", ""))], [], 1

    def segment(self, pages, toc, s_page: int, e_page: int, book: str) -> list[Chunk]:
        max_chars, min_chars = self.cfg.chunk_max_chars, self.cfg.chunk_min_chars
        chapter_bounds = sorted((t[2], clean_text(t[1])) for t in toc if t[0] == 1)
        bounds = [(t[2], clean_text(t[1])) for t in toc if s_page <= t[2] <= e_page and t[0] >= 2]
        if not bounds:
            bounds = [(t[2], clean_text(t[1])) for t in toc if s_page <= t[2] <= e_page]
        if not bounds:
            bounds = []
            for page_no, raw in pages:
                for line in raw.splitlines():
                    line = line.strip()
                    if 5 < len(line) < 75 and not line.endswith((".", ",", ";", ":")) and HEADING_RE.match(line):
                        bounds.append((page_no, clean_text(line)))
        bounds = [b for b in bounds if b[1]]
        raw_chunks: list[dict] = []
        cur_title, cur_start, cur_parts, cur_chapter = None, None, [], None
        ci = 0

        def flush(end_page: int) -> None:
            nonlocal cur_title, cur_start, cur_parts
            text = clean_text("".join(cur_parts))
            if text and cur_title:
                raw_chunks.append({
                    "title": cur_title,
                    "chapter": cur_chapter or "Untitled section",
                    "text": text,
                    "page_start": cur_start,
                    "page_end": end_page,
                })
            cur_title, cur_start, cur_parts = None, None, []

        bi = 0
        for page_no, raw in pages:
            while ci < len(chapter_bounds) and chapter_bounds[ci][0] <= page_no:
                cur_chapter = chapter_bounds[ci][1]
                ci += 1
            while bi < len(bounds) and bounds[bi][0] < page_no:
                bi += 1
            if bi < len(bounds) and bounds[bi][0] == page_no:
                flush(page_no - 1 if cur_start else page_no)
                cur_title, cur_start = bounds[bi][1], page_no
                bi += 1
            if cur_title is None:
                cur_title, cur_start = f"Untitled fragment (p. {page_no})", page_no
            cur_parts.append(f"\n[PAGE {page_no}]\n{raw}\n")
        flush(pages[-1][0] if pages else (cur_start or s_page))
        merged: list[dict] = []
        for ch in raw_chunks:
            if merged and len(ch["text"]) < min_chars and merged[-1]["chapter"] == ch["chapter"]:
                merged[-1]["text"] += "\n\n" + ch["text"]
                merged[-1]["page_end"] = ch["page_end"]
            else:
                merged.append(dict(ch))
        final: list[dict] = []
        for ch in merged:
            if len(ch["text"]) <= max_chars:
                final.append(ch)
                continue
            pieces, piece = [], ""
            for para in ch["text"].split("\n\n"):
                if len(piece) + len(para) + 2 > max_chars and piece:
                    pieces.append(piece.strip())
                    piece = ""
                piece += para + "\n\n"
            if piece.strip():
                pieces.append(piece.strip())
            for i, pc in enumerate(pieces, 1):
                final.append({
                    "title": f"{ch['title']} (part {i}/{len(pieces)})",
                    "chapter": ch["chapter"],
                    "text": pc,
                    "page_start": ch["page_start"],
                    "page_end": ch["page_end"],
                })
        out = []
        for i, c in enumerate(c for c in final if len(c["text"]) >= 200):
            out.append(Chunk(
                hash=hashlib.sha256(c["text"].encode("utf-8")).hexdigest(),
                book=book,
                chapter=c["chapter"],
                title=c["title"],
                page_start=c["page_start"],
                page_end=c["page_end"],
                text=c["text"],
                ord=i,
            ))
        return out

    def run(self, path: Path, start: int | None, end: int | None, dry: bool) -> None:
        path = path.expanduser().resolve()
        if not path.exists():
            raise SourceError(f"File not found: {path}")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise SourceError(f"Unsupported format: {path.suffix}")
        book = sanitize_filename(path.stem)
        pages, toc, s_page = self.extract_pages(path, start, end)
        e_page = pages[-1][0] if pages else s_page
        chunks = self.segment(pages, toc, s_page, e_page, book)
        if not chunks:
            raise SourceError("Could not split source: no text (possibly a scan without OCR)")
        estimate = sum(min(len(c.text), 12000) for c in chunks) // 4 + 400 * len(chunks)
        LOG.info("📖 %s: %d excerpts (pp. %d-%d)", book, len(chunks), s_page, e_page)
        for c in chunks[:12]:
            LOG.info("   • [%s] %s — pp. %d-%d, %d chars",
                     c.chapter[:28], c.title[:52], c.page_start, c.page_end, len(c.text))
        if len(chunks) > 12:
            LOG.info("   … and %d more", len(chunks) - 12)
        if dry:
            LOG.info("🧪 DRY-RUN: enrich ≈ %d tokens (%d cached calls). No DB writes.",
                     estimate, len(chunks))
            return
        with Store(self.cfg) as store:
            new = 0
            for c in chunks:
                if store.conn.execute("SELECT 1 FROM chunks_raw WHERE hash = ?", (c.hash,)).fetchone():
                    continue
                store.conn.execute(
                    "INSERT INTO chunks_raw "
                    "(hash, book, chapter, title, page_start, page_end, text, ord) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (c.hash, c.book, c.chapter, c.title, c.page_start, c.page_end, c.text, c.ord))
                new += 1
            store.conn.commit()
            LOG.info("💾 Added excerpts: %d (already existed: %d). Zero LLM calls.",
                     new, len(chunks) - new)


class Enricher:
    def __init__(self, cfg: AppConfig, llm: LLMClient):
        self.cfg, self.llm = cfg, llm
        self.stats = {"llm_calls": 0, "cache_hits": 0}

    def _cache_key(self, text: str) -> str:
        payload = {"m": self.cfg.model, "v": self.cfg.cache_namespace, "t": text[:12000]}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def card_for(self, cache: Cache, text: str) -> EnrichCard:
        key = self._cache_key(text)
        raw = cache.get(key)
        if raw is not None:
            self.stats["cache_hits"] += 1
        else:
            messages = [{"role": "system", "content": self.cfg.prompt("enrich", DEFAULT_ENRICH_PROMPT)},
                        {"role": "user", "content": text[:12000]}]
            raw = self.llm.complete_raw(messages, max_tokens=400)
            self.stats["llm_calls"] += 1
            cache.put(key, raw)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned
        try:
            return EnrichCard.model_validate_json(cleaned.strip())
        except Exception:
            repair = [{"role": "system", "content": self.cfg.prompt("enrich", DEFAULT_ENRICH_PROMPT)},
                      {"role": "user", "content": text[:12000]},
                      {"role": "assistant", "content": raw},
                      {"role": "user", "content": "Your JSON is invalid. Return ONLY compact valid JSON, close all braces."}]
            raw2 = self.llm.complete_raw(repair, max_tokens=400)
            self.stats["llm_calls"] += 1
            cleaned2 = raw2.strip().strip("`")
            if cleaned2.lower().startswith("json"):
                cleaned2 = cleaned2[4:]
            card = EnrichCard.model_validate_json(first_json_blob(cleaned2) or cleaned2)
            cache.put(key, raw2)
            return card

    def run(self, limit: int) -> None:
        with Store(self.cfg) as store, Cache(self.cfg) as cache:
            rows = []
            for r in store.conn.execute("SELECT hash, title, text FROM chunks_raw ORDER BY book, ord"):
                er = store.conn.execute("SELECT src FROM chunk_enrich WHERE hash = ?", (r["hash"],)).fetchone()
                if er is None or er["src"] == "fallback":
                    rows.append(r)
            if limit > 0:
                rows = rows[:limit]
            if not rows:
                LOG.info("✅ All excerpts have proper catalogue cards.")
                return
            LOG.info("🧠 Enriching %d excerpts (1 cached call per excerpt)...", len(rows))
            for i, r in enumerate(rows, 1):
                try:
                    card = self.card_for(cache, r["text"])
                    src = "llm"
                except Exception as exc:
                    LOG.warning("   ⚠️ %s: %s — local fallback (re-enrich later)", r["title"][:40], exc)
                    card, src = EnrichCard(summary=clean_text(r["text"])[:300]), "fallback"
                store.conn.execute(
                    "INSERT OR REPLACE INTO chunk_enrich (hash, summary, keywords, questions, src) VALUES (?,?,?,?,?)",
                    (r["hash"], card.summary, json.dumps(card.keywords, ensure_ascii=False),
                     json.dumps(card.questions, ensure_ascii=False), src))
                store.conn.commit()
                LOG.info("[%d/%d] ✅ %s", i, len(rows), r["title"][:60])
            LOG.info("💰 Paid calls: %d, cache hits: %d", self.stats["llm_calls"], self.stats["cache_hits"])


class GraphBuilder:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def extract_entities(self, text: str, keywords: list[str]) -> list[str]:
        found = {k.strip() for k in keywords if k and len(k.strip()) >= 3}
        for pattern in self.cfg.entity_patterns:
            try:
                found.update(re.findall(pattern, text))
            except re.error as exc:
                LOG.warning("⚠️ Invalid entity pattern %r: %s", pattern, exc)
        return sorted(found)

    def load_alias_map(self, alias_db: Path | None) -> dict[str, str]:
        if not alias_db or not Path(alias_db).expanduser().exists():
            return {}
        p = Path(alias_db).expanduser().resolve()
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15)
        except sqlite3.OperationalError as exc:
            LOG.warning("⚠️ Could not open alias-db %s: %s", p, exc)
            return {}
        try:
            out: dict[str, str] = {}
            for r in conn.execute("SELECT a.normalized, e.canonical_name "
                                  "FROM aliases a JOIN entities e ON e.id = a.entity_id"):
                out.setdefault(r["normalized"], r["canonical_name"])
            return out
        except Exception:
            return {}
        finally:
            conn.close()

    def run(self, alias_db: Path | None) -> None:
        cfg = self.cfg
        root = cfg.vault_root
        with Store(cfg) as store:
            rows = store.conn.execute(
                "SELECT r.hash, r.book, r.chapter, r.title, r.page_start, r.page_end, r.text, "
                "e.summary, e.keywords, e.questions "
                "FROM chunks_raw r LEFT JOIN chunk_enrich e ON e.hash = r.hash "
                "ORDER BY r.book, r.page_start").fetchall()
            if not rows:
                LOG.error("❌ Run 'chunk' first.")
                return
            missing = sum(1 for r in rows if not r["summary"])
            if missing:
                LOG.warning("⚠️ %d excerpts missing cards. Run 'enrich' first.", missing)
                return
            old_norm2canon = self.load_alias_map(alias_db)
            data = []
            for r in rows:
                kw = json.loads(r["keywords"]) if r["keywords"] else []
                qs = json.loads(r["questions"]) if r["questions"] else []
                data.append({
                    "hash": r["hash"], "book": r["book"], "chapter": r["chapter"],
                    "title": r["title"], "ps": r["page_start"], "pe": r["page_end"],
                    "text": r["text"], "summary": clean_text(r["summary"]),
                    "kw": kw, "qs": qs, "ents": self.extract_entities(r["text"], kw),
                    "kw_norm": {normalize_text(k) for k in kw},
                    "fname": (f"{sanitize_filename(r['book'])}__p{r['page_start']:04d}-"
                              f"{r['page_end']:04d}__{sanitize_filename(r['title'])}.md"),
                })
            by_hash = {d["hash"]: d for d in data}
            kw_index: dict[str, list[str]] = {}
            for d in data:
                for kn in d["kw_norm"]:
                    kw_index.setdefault(kn, []).append(d["hash"])
            ent_index: dict[str, dict[str, Any]] = {}
            for d in data:
                for en in d["ents"]:
                    kn = normalize_text(en)
                    canon = old_norm2canon.get(kn)
                    if canon:
                        kn, en = normalize_text(canon), canon
                    rec = ent_index.setdefault(kn, {"name": en, "chunks": []})
                    rec["chunks"].append(d["hash"])
            related: dict[str, list[str]] = {}
            for d in data:
                cnt: Counter = Counter()
                for kn in d["kw_norm"]:
                    df = len(kw_index.get(kn, []))
                    weight = 1.0 / df if df else 0.0
                    for other in kw_index.get(kn, []):
                        if other != d["hash"]:
                            cnt[other] += weight
                related[d["hash"]] = [h for h, sc in cnt.most_common(8) if sc >= 0.5][:6]
            store.conn.execute("DELETE FROM links")
            store.conn.execute("DELETE FROM catalog")
            store.conn.execute("DELETE FROM entities")
            books = sorted({d["book"] for d in data})
            for book in books:
                (root / cfg.folder("excerpts") / book).mkdir(parents=True, exist_ok=True)
                (root / cfg.folder("sections") / book).mkdir(parents=True, exist_ok=True)
            (root / cfg.folder("entities")).mkdir(parents=True, exist_ok=True)
            chapter_files = {(book, ch): f"HUB__{sanitize_filename(book)}__{sanitize_filename(ch)}.md"
                             for book in books
                             for ch in sorted({d["chapter"] for d in data if d["book"] == book})}
            ent_hubs = {norm: f"ENTITY__{sanitize_filename(rec['name'])}.md"
                        for norm, rec in ent_index.items() if len(rec["chunks"]) >= 3}
            written: set[Path] = set()
            for d in data:
                same = sorted((x for x in data if x["book"] == d["book"] and x["chapter"] == d["chapter"]),
                              key=lambda x: (x["ps"], x["pe"], x["title"]))
                idx = same.index(d)
                prev = same[idx - 1] if idx > 0 else None
                nxt = same[idx + 1] if idx + 1 < len(same) else None
                hub = chapter_files[(d["book"], d["chapter"])]
                rel = related.get(d["hash"], [])
                for h in rel:
                    store.conn.execute("INSERT OR IGNORE INTO links (a, b, kind) VALUES (?, ?, ?)",
                                       (d["hash"], h, "topic"))
                if prev:
                    store.conn.execute("INSERT OR IGNORE INTO links (a, b, kind) VALUES (?, ?, ?)",
                                       (d["hash"], prev["hash"], "prev"))
                if nxt:
                    store.conn.execute("INSERT OR IGNORE INTO links (a, b, kind) VALUES (?, ?, ?)",
                                       (d["hash"], nxt["hash"], "next"))
                meta = {"type": "excerpt", "book": d["book"], "chapter": d["chapter"],
                        "pages": [d["ps"], d["pe"]], "summary": d["summary"],
                        "keywords": d["kw"], "questions": d["qs"], "entities": d["ents"],
                        "chunk_hash": d["hash"]}
                head = [f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()}\n---\n",
                        f"# {d['title']}\n",
                        f"> **{cfg.label('brief')}:** {d['summary']}\n>"]
                if d["qs"]:
                    head.append("> **" + cfg.label("answers") + ":**\n"
                                + "\n".join(f"> - {q}" for q in d["qs"]) + "\n>")
                if d["kw"]:
                    head.append(f"> **{cfg.label('keywords')}:** {', '.join(d['kw'])}\n")
                head.append(f"\n## {cfg.label('full')} ({d['book']}, pp. {d['ps']}–{d['pe']})\n\n{d['text'].strip()}\n")
                conn_lines = [f"\n## {cfg.label('links')}\n- {cfg.label('chapter')}: {wikilink(hub, d['chapter'])}"]
                if prev:
                    conn_lines.append(f"- {cfg.label('prev')}: {wikilink(prev['fname'], prev['title'])}")
                if nxt:
                    conn_lines.append(f"- {cfg.label('next')}: {wikilink(nxt['fname'], nxt['title'])}")
                my_ents = []
                for en in d["ents"]:
                    kn = normalize_text(en)
                    canon = old_norm2canon.get(kn)
                    key = normalize_text(canon) if canon else kn
                    if key in ent_hubs:
                        my_ents.append(wikilink(ent_hubs[key], ent_index[key]["name"]))
                if rel or my_ents:
                    rel_links = [wikilink(by_hash[h]["fname"], by_hash[h]["title"]) for h in rel]
                    conn_lines.append(f"- {cfg.label('topics')}: " + ", ".join(rel_links + my_ents))
                conn_lines.append("")
                fpath = root / cfg.folder("excerpts") / d["book"] / d["fname"]
                write_atomic(fpath, "\n".join(head) + "\n".join(conn_lines))
                written.add(fpath)
                store.conn.execute(
                    "INSERT OR REPLACE INTO catalog "
                    "(hash, book, chapter, title, page_start, page_end, summary, keywords, questions, entities, path) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (d["hash"], d["book"], d["chapter"], d["title"], d["ps"], d["pe"], d["summary"],
                     json.dumps(d["kw"], ensure_ascii=False), json.dumps(d["qs"], ensure_ascii=False),
                     json.dumps(d["ents"], ensure_ascii=False), str(fpath)))
            for (book, ch), fname in chapter_files.items():
                members = sorted((d for d in data if d["book"] == book and d["chapter"] == ch),
                                 key=lambda x: (x["ps"], x["pe"]))
                items = [f"- {wikilink(d['fname'], d['title'])} (pp. {d['ps']}–{d['pe']}) — {d['summary'][:110]}…"
                         for d in members]
                body = (f"---\ntype: chapter_hub\nbook: {book}\nchapter: {ch}\n---\n\n"
                        f"# {book} — {ch}\n\n## {cfg.hub_label('section')}\n"
                        + "\n".join(items) + "\n")
                hp = root / cfg.folder("sections") / book / fname
                write_atomic(hp, body)
                written.add(hp)
            for norm, fname in ent_hubs.items():
                rec = ent_index[norm]
                chs = sorted((by_hash[h] for h in rec["chunks"]), key=lambda x: (x["book"], x["ps"]))
                items = [f"- {wikilink(d['fname'], d['title'])} ({d['book']}, pp. {d['ps']}–{d['pe']})" for d in chs]
                body = (f"---\ntype: entity_hub\nentity: {rec['name']}\nmentions: {len(chs)}\n---\n\n"
                        f"# {rec['name']}\n\n"
                        f"{cfg.hub_label('entity')} {len(chs)} {cfg.hub_label('entity_tail')}:\n"
                        + "\n".join(items) + "\n")
                ep = root / cfg.folder("entities") / fname
                write_atomic(ep, body)
                written.add(ep)
                store.conn.execute("INSERT OR REPLACE INTO entities (name, norm, chunks) VALUES (?,?,?)",
                                   (rec["name"], norm, json.dumps(rec["chunks"], ensure_ascii=False)))
            stale = 0
            for book in books:
                for f in (root / cfg.folder("excerpts") / book).glob("*.md"):
                    if f in written:
                        continue
                    head = f.read_text(encoding="utf-8", errors="ignore")[:600]
                    m = re.search(r"chunk_hash:\s*([0-9a-f]{64})", head)
                    if m and m.group(1) not in by_hash:
                        f.unlink()
                        stale += 1
            store.conn.commit()
            LOG.info("🕸️ Excerpts: %d, chapter hubs: %d, entity hubs: %d, stale removed: %d",
                     len(data), len(chapter_files), len(ent_hubs), stale)
            LOG.info("📁 Vault: %s", root)
            LOG.info("   Zero LLM calls on this stage.")


class SearchIndex:
    def __init__(self, store: Store):
        self.store = store
        self.available = store.fts_available()
        if not self.available:
            LOG.warning("⚠️ FTS5 unavailable in this SQLite — using slower LIKE fallback")
        else:
            row = store.conn.execute("SELECT name FROM sqlite_master "
                                     "WHERE type='table' AND name='search_idx'").fetchone()
            if not row:
                LOG.warning("⚠️ Search index missing. Building...")
                self.rebuild()

    def rebuild(self) -> None:
        if not self.available:
            LOG.warning("⚠️ Reindex skipped: FTS5 unavailable, LIKE fallback active.")
            return
        conn = self.store.conn
        conn.execute("DROP TABLE IF EXISTS search_idx")
        conn.execute("""CREATE VIRTUAL TABLE search_idx USING fts5(
            hash, title, summary, keywords, questions, text, tokenize='unicode61')""")
        conn.execute("""INSERT INTO search_idx (hash, title, summary, keywords, questions, text)
            SELECT r.hash, r.title, COALESCE(e.summary,''), COALESCE(e.keywords,''),
                   COALESCE(e.questions,''), r.text
            FROM chunks_raw r LEFT JOIN chunk_enrich e ON r.hash = e.hash""")
        conn.commit()
        total = conn.execute("SELECT COUNT(*) c FROM search_idx").fetchone()["c"]
        LOG.info("✅ Index built: %d chunks.", total)

    def like_fallback(self, words: list[str], limit: int) -> list:
        conds, params = [], []
        for w in words[:6]:
            conds.append("(r.title LIKE ? OR COALESCE(e.summary,'') LIKE ? OR r.text LIKE ?)")
            params += [f"%{w}%"] * 3
        if not conds:
            return []
        query = (f"SELECT r.hash, r.title, e.summary AS summary, e.keywords AS keywords, "
                 f"e.questions AS questions, r.text AS text, 0 AS score "
                 f"FROM chunks_raw r LEFT JOIN chunk_enrich e ON e.hash = r.hash "
                 f"WHERE {' OR '.join(conds)} LIMIT ?")
        params.append(limit)
        return self.store.conn.execute(query, params).fetchall()

    def search(self, terms: list[str], words: list[str], limit: int) -> list:
        if not self.available:
            return self.like_fallback(terms + words, limit)
        base = ("SELECT hash, title, summary, keywords, questions, text, bm25(search_idx) AS score "
                "FROM search_idx WHERE search_idx MATCH ? ORDER BY score ASC LIMIT ?")
        quoted = [f'"{t}"' for t in terms if len(t) > 2]
        attempts = []
        full = " OR ".join(quoted + words)
        if full:
            attempts.append(full)
        if quoted:
            attempts.append(" OR ".join(quoted))
        if words:
            attempts.append(" OR ".join(words[:5]))
        for q in attempts:
            try:
                rows = self.store.conn.execute(base, (q, limit)).fetchall()
                if rows:
                    return rows
            except sqlite3.OperationalError:
                continue
        LOG.warning("⚠️ FTS5 query failed — switching to LIKE fallback.")
        return self.like_fallback(terms + words, limit)


class Retriever:
    def __init__(self, cfg: AppConfig, store: Store, llm: LLMClient, index: SearchIndex):
        self.cfg, self.store, self.llm, self.index = cfg, store, llm, index

    def expand(self, query: str) -> QueryExpansion:
        LOG.info("⚙️ Analyzing query and generating HyDE...")
        prompt = self.cfg.prompt("hyde", HYDE_PROMPT).format(query=query)
        raw = self.llm.complete_raw([{"role": "user", "content": prompt}], max_tokens=1200)
        blob = first_json_blob(raw.strip().strip("`").removeprefix("json"))
        try:
            return QueryExpansion.model_validate_json(blob or "{}")
        except Exception as exc:
            raise LLMError(f"Query expansion parsing failed: {exc}")

    def seeds(self, exp: QueryExpansion, limit: int = SEED_LIMIT) -> list:
        LOG.info("⚙️ Retrieving seed chunks...")
        terms = [re.sub(r"[^\w\s-]", "", t).strip() for t in exp.terms]
        words = re.findall(r"\b[A-Za-zА-Яа-яЁё]{4,}\b", " ".join(exp.hyde))
        uniq = list({w.lower() for w in words if w.lower() not in FTS_STOP_WORDS})[:15]
        return self.index.search([t for t in terms if t], uniq, limit=limit)

    def satellites(self, seed_hashes: list[str]) -> list[dict]:
        LOG.info("⚙️ Traversing graph and shared entities...")
        if not seed_hashes:
            return []
        seen = set(seed_hashes)
        sats: list[dict] = []
        ph = ",".join(["?"] * len(seed_hashes))
        try:
            rows = self.store.conn.execute(
                f"SELECT DISTINCT r.hash, r.title, c.summary AS summary, r.text, r.book, "
                f"r.chapter, r.page_start, r.page_end "
                f"FROM links l JOIN catalog c ON l.b = c.hash JOIN chunks_raw r ON l.b = r.hash "
                f"WHERE l.a IN ({ph}) AND l.kind IN ('topic', 'prev', 'next') LIMIT 3",
                seed_hashes).fetchall()
            for r in rows:
                if r["hash"] not in seen:
                    sats.append(dict(r))
                    seen.add(r["hash"])
        except sqlite3.OperationalError as exc:
            LOG.warning("⚠️ Graph traversal via 'links' unavailable (%s) — trying entities.", exc)
        if len(sats) < 2:
            try:
                ents: set[str] = set()
                for h in seed_hashes:
                    row = self.store.conn.execute("SELECT entities FROM catalog WHERE hash=?", (h,)).fetchone()
                    if row and row["entities"]:
                        ents.update(json.loads(row["entities"]))
                norms = [normalize_text(e) for e in ents if normalize_text(e)]
                if norms:
                    ph2 = ",".join(["?"] * len(norms))
                    cnt: Counter = Counter()
                    for r in self.store.conn.execute(f"SELECT chunks FROM entities WHERE norm IN ({ph2})", norms):
                        for h in json.loads(r["chunks"]):
                            if h not in seen:
                                cnt[h] += 1
                    for h, _ in cnt.most_common(2):
                        row = self.store.conn.execute(
                            "SELECT hash, title, text, book, chapter, page_start, page_end "
                            "FROM chunks_raw WHERE hash=?", (h,)).fetchone()
                        if row:
                            item = dict(row)
                            er = self.store.conn.execute("SELECT summary FROM chunk_enrich WHERE hash=?",
                                                       (h,)).fetchone()
                            item["summary"] = er["summary"] if er else ""
                            sats.append(item)
                            seen.add(h)
            except Exception as exc:
                LOG.warning("⚠️ Entity-overlap retrieval unavailable (%s) — seeds only.", exc)
        return sats[:3]


class Answerer:
    def __init__(self, cfg: AppConfig, store: Store, llm: LLMClient):
        self.cfg, self.store, self.llm = cfg, store, llm

    def answer(self, query: str, seeds: list, satellites: list[dict], catalog_rows: dict[str, dict]) -> None:
        print(f"\n{'='*60}\n 🧠 Synthesized answer: {len(seeds)} seeds + {len(satellites)} satellites\n{'='*60}")
        blocks = []
        for i, chunk in enumerate(seeds, 1):
            cat = catalog_rows.get(chunk["hash"], {})
            blocks.append(f"[SOURCE {i}: {cat.get('book', 'DB')}, {cat.get('chapter', '')}, "
                          f"pp. {cat.get('page_start', '?')}-{cat.get('page_end', '?')}]\n"
                          f"Title: {chunk['title']}\nSummary: {chunk['summary'] or ''}\n"
                          f"Text:\n{(chunk['text'] or '')[:2000]}...")
        for i, chunk in enumerate(satellites, 1):
            blocks.append(f"[RELATED {i}: {chunk['book']}, {chunk['chapter']}, "
                          f"pp. {chunk['page_start']}-{chunk['page_end']}]\n"
                          f"Title: {chunk['title']}\nFragment: {(chunk['text'] or '')[:1000]}...")
        messages = [{"role": "system", "content": self.cfg.prompt("answer", ANSWER_SYSTEM)},
                    {"role": "user", "content": f"Question: {query}\n\nCONTEXT:\n" + "\n\n---\n\n".join(blocks)}]
        accumulated = ""
        try:
            print()
            for piece in self.llm.stream_text(messages):
                accumulated += piece
                print(piece, end="", flush=True)
            print()
        except requests.exceptions.RequestException as exc:
            print(f"\n⚠️ Stream interrupted ({exc}). Showing partial output:\n{accumulated}")
        if not accumulated:
            LOG.error("❌ Model returned no tokens. Try again or override model with --model.")

    def print_links(self, seeds: list, catalog_rows: dict[str, dict]) -> None:
        print(f"\n{'='*60}\n 🔗 Sources in Obsidian\n{'='*60}")
        vault_path = Path(self.cfg.vault_path).expanduser().resolve()
        vault_name = vault_path.name
        shown = 0
        for chunk in seeds:
            cat = catalog_rows.get(chunk["hash"])
            if not cat or not cat.get("path"):
                continue
            shown += 1
            abs_path = Path(cat["path"]).resolve()
            try:
                rel = abs_path.relative_to(vault_path)
                # Patch: POSIX paths for Obsidian URI compatibility + safe slash
                clean_rel = rel.as_posix()
                url = f"obsidian://open?vault={quote(vault_name)}&file={quote(clean_rel, safe='/')}"
            except ValueError:
                rel, url = abs_path, str(abs_path)
            print(f"📄 {cat['title']} ({cat['book']}, pp. {cat['page_start']}-{cat['page_end']})")
            print(f"   {url}  (fallback: {rel})")
        if not shown:
            LOG.warning("⚠️ No links: catalog is empty. Run 'link' to build notes and catalog.")


def run_query(cfg: AppConfig, query: str) -> None:
    with Store(cfg) as store:
        llm = LLMClient(cfg)
        index = SearchIndex(store)
        retriever = Retriever(cfg, store, llm, index)
        exp = retriever.expand(query)
        if exp.corrected_query:
            LOG.info("Understood as: %s", exp.corrected_query)
        seeds = retriever.seeds(exp)
        if not seeds:
            LOG.error("❌ Nothing found. Try rephrasing or adding a domain term.")
            return
        seed_hashes = [s["hash"] for s in seeds]
        ph = ",".join(["?"] * len(seed_hashes))
        catalog_rows = {r["hash"]: dict(r) for r in store.conn.execute(
            f"SELECT hash, book, chapter, title, page_start, page_end, path "
            f"FROM catalog WHERE hash IN ({ph})", seed_hashes).fetchall()}
        satellites = retriever.satellites(seed_hashes)
        answerer = Answerer(cfg, store, llm)
        answerer.answer(query, seeds, satellites, catalog_rows)
        answerer.print_links(seeds, catalog_rows)


class TerminalUI:
    @staticmethod
    def box(title: str, lines: list[str], width: int = 72) -> None:
        top = "┌" + "─" * (width - 2) + "┐"
        mid = "├" + "─" * (width - 2) + "┤"
        bot = "└" + "─" * (width - 2) + "┘"
        print(top)
        print(f"│ {title:<{width - 4}} │")
        print(mid)
        for line in lines:
            if len(line) > width - 4:
                line = line[:width - 7] + "..."
            print(f"│ {line:<{width - 4}} │")
        print(bot)

    @staticmethod
    def prompt_with_default(prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            try:
                value = input(f"{prompt}{suffix}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return default
            if not value:
                return default
            return value

    @staticmethod
    def prompt_path(prompt: str, default: str = "", must_exist: bool = True) -> str:
        while True:
            value = TerminalUI.prompt_with_default(prompt, default)
            if not value:
                continue
            path = Path(value).expanduser()
            if must_exist and not path.exists():
                print(f"  ⚠️ Path does not exist: {path}")
                continue
            if must_exist and not path.is_dir():
                print(f"  ⚠️ Not a directory: {path}")
                continue
            return str(path)

    @staticmethod
    def prompt_secret(prompt: str) -> str:
        import getpass
        return getpass.getpass(f"{prompt}: ")

    @staticmethod
    def select_from_list(title: str, options: list[str], default_idx: int = 0) -> int:
        if not HAS_TERMIOS or not sys.stdin.isatty():
            print(f"\n{title}")
            for i, opt in enumerate(options):
                marker = "*" if i == default_idx else " "
                print(f"  {marker} {i + 1}. {opt}")
            while True:
                try:
                    raw = input(f"Enter number [1-{len(options)}] (default {default_idx + 1}): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return default_idx
                if not raw:
                    return default_idx
                try:
                    idx = int(raw) - 1
                    if 0 <= idx < len(options):
                        return idx
                except ValueError:
                    pass
                print("  ⚠️ Invalid input.")

        print(f"\n{title}")
        print("(↑/↓ to move, Enter to select, q to quit with default)\n")
        idx = default_idx
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                sys.stdout.write("\033[J")
                for i, opt in enumerate(options):
                    marker = "▶ " if i == idx else "  "
                    sys.stdout.write(f"{marker}{opt}\r\n")
                sys.stdout.write(f"\033[{len(options)}A")
                sys.stdout.flush()
                rlist, _, _ = select.select([sys.stdin], [], [], None)
                if rlist:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n"):
                        sys.stdout.write("\033[J")
                        return idx
                    if ch in ("q", "Q", "\x03"):
                        sys.stdout.write("\033[J")
                        return default_idx
                    if ch == "\x1b":
                        r_esc, _, _ = select.select([sys.stdin], [], [], 0.05)
                        if r_esc:
                            seq = sys.stdin.read(2)
                            if seq == "[A":
                                idx = (idx - 1) % len(options)
                            elif seq == "[B":
                                idx = (idx + 1) % len(options)
                        else:
                            sys.stdout.write("\033[J")
                            return default_idx
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\033[J")
            sys.stdout.flush()


def print_banner(cfg: AppConfig, store: Store) -> None:
    try:
        chunks_count = store.conn.execute("SELECT COUNT(*) FROM chunks_raw").fetchone()[0]
        links_count = store.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    except Exception:
        chunks_count, links_count = 0, 0

    vault_name = Path(cfg.vault_path).name or "Vault"
    
    C = "\033[38;5;51m"
    M = "\033[38;5;141m"
    P = "\033[38;5;213m"
    W = "\033[1;37m"
    D = "\033[2m"
    R = "\033[0m"

    logo = [
        f"{C}            .:::-------::.           {R}",
        f"{C}         .=+=============+-.        {R}",
        f"{C}       :++====={M}+++++{C}========+:.     {R}",
        f"{C}     -*+===={M}+***++++**+{C}=======+:    {R}",
        f"{C}    =*+++=={M}**+:{C}     .:{M}+**+{C}======:   {R}",
        f"{C}    ===--={M}**-{C}          {M}-**+{C}=====:   {R}",
        f"{C}         .:             {M}+**+{C}====:   {R}",
        f"{C}                        {M}+**+{C}====.   {R}",
        f"{C}                       {M}=**+{C}====:    {R}",
        f"{C}                    .-{M}+**+{C}====:     {R}",
        f"{C}                .-={M}***+{C}====-:       {R}",
        f"{C}              .-{M}+***+{C}====-:         {R}",
        f"{C}              {M}=***+{C}====-.           {R}",
        f"{C}              {M}+***+{C}===:             {R}",
        f"{M}              :*****+:              {R}",
        f"{M}               :===:                {R}",
        f"                                    ",
        f"{P}               .---.                {R}",
        f"{P}              :*****:               {R}",
        f"{P}              =*****=               {R}",
        f"{P}               :===:                {R}",
    ]

    info = [
        f"{W}NEXTQUESTION{R} {D}v1.0.0{R}",
        f"{D}──────────────────────────────────────────────{R}",
        f"{W}Engine{R}     : Graph-Augmented Clinical & Academic RAG",
        f"{W}Pipeline{R}   : HyDE ➔ SQLite-FTS5 (BM25) ➔ Graph Walk",
        f"{W}Model{R}      : {P}{cfg.model}{R}",
        f"{W}Knowledge{R}  : {M}{chunks_count}{R} chunks {D}|{R} {M}{links_count}{R} links in graph",
        f"{W}Vault{R}      : {C}{vault_name}{R} {D}(root: {cfg.store_root or '.'}){R}",
        f"{W}Database{R}   : {cfg.raw_db.name}",
        f"{D}──────────────────────────────────────────────{R}",
        f"{D}Type your question, or 'exit' / Ctrl+C to leave.{R}",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ]

    print("\n")
    for l_line, i_line in zip(logo, info):
        print(f"  {l_line}   {i_line}")
    print("\n")


def init_wizard(cfg_path: Path) -> None:
    print("\n🔧 nextquestion — initialization wizard\n")
    TerminalUI.box("Welcome", [
        "This wizard will create a config.json for nextquestion.",
        "You'll need: an LLM API key (or local server) and an Obsidian vault.",
    ])

    backends = list(BACKEND_PRESETS.keys())
    backend_idx = TerminalUI.select_from_list("Choose LLM backend:", backends)
    backend = backends[backend_idx]
    preset = BACKEND_PRESETS[backend]

    print(f"\n✓ Selected: {backend}\n")
    base_url = TerminalUI.prompt_with_default("API base URL", preset["base_url"])
    model = TerminalUI.prompt_with_default("Model name", preset["model"])

    if backend in ("OpenRouter", "OpenAI", "DeepSeek"):
        api_key = TerminalUI.prompt_secret("API key (will be stored in config.json)")
    else:
        api_key = "ollama" if backend == "Ollama (local)" else "local"
        print(f"  (local backend, using placeholder key: {api_key})")

    vault_path = TerminalUI.prompt_path("Obsidian vault path", must_exist=True)
    store_root = TerminalUI.prompt_with_default(
        "Vault subfolder (empty = vault root)", "RAG_Knowledge_Base"
    )
    db_path = TerminalUI.prompt_with_default("Database path", str(cfg_path.parent / "nextquestion.db"))

    config = {
        "vault_path": vault_path,
        "store_root": store_root,
        "db_path": db_path,
        "cache_namespace": "nq-1.0",
        "chunk_max_chars": 9000,
        "chunk_min_chars": 400,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "folders": {"excerpts": "Notes", "sections": "Sections", "entities": "Entities"},
        "labels": DEFAULT_LABELS,
        "hub_labels": DEFAULT_HUB_LABELS,
        "prompts": {
            "enrich": DEFAULT_ENRICH_PROMPT,
            "hyde": HYDE_PROMPT,
            "answer": ANSWER_SYSTEM,
        },
        "entity_patterns": [],
    }

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✅ Config written to {cfg_path}")
    print("   Next: run 'nextquestion.py chunk <document.pdf>' to start building your knowledge base.\n")


def repl(cfg: AppConfig) -> None:
    width = 72
    with Store(cfg) as store:
        print_banner(cfg, store)

    while True:
        try:
            print(f"{'─' * width}")
            query = input("❓ NextQuestion: ").strip()
            if not query:
                continue
            if query.lower() in ("exit", "quit"):
                break
            print(f"{'─' * width}")
            run_query(cfg, query)
            print()
        except (KeyboardInterrupt, EOFError):
            print("\nExit.")
            break
        except NextQuestionError as exc:
            LOG.error("❌ Error: %s", exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextquestion",
        description="NextQuestion — graph-augmented RAG: documents → Obsidian → answers."
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"),
                        help="Path to config.json (default: ./config.json)")
    parser.add_argument("--model", type=str, default=None, help="Override model from config")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--reindex", action="store_true", help="Rebuild FTS5 search index")
    sub = parser.add_subparsers(dest="command")
    pc = sub.add_parser("chunk", help="Semantic ingestion (0 LLM calls)")
    pc.add_argument("document", type=Path)
    pc.add_argument("--start-page", type=int, default=None)
    pc.add_argument("--end-page", type=int, default=None)
    pc.add_argument("--dry-run", action="store_true")
    pe = sub.add_parser("enrich", help="Catalogue cards (1 cached LLM call per excerpt)")
    pe.add_argument("--limit", type=int, default=0)
    pl = sub.add_parser("link", help="Notes, hubs, links, catalog (0 LLM calls)")
    pl.add_argument("--alias-db", type=Path, default=None)
    sub.add_parser("reindex", help="Rebuild FTS5 search index")
    pq = sub.add_parser("query", help="Single query or REPL if no text given")
    pq.add_argument("text", nargs="?", default=None)
    sub.add_parser("init", help="Interactive config wizard")
    sub.add_parser("example", help="Print example config.json and exit")
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    known = {"chunk", "enrich", "link", "reindex", "query", "init", "example"}
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        argv = ["query"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    if not args.config.exists() and not argv:
        print("👋 Config not found. Starting interactive setup...\n")
        init_wizard(args.config)
        return

    if args.command == "init":
        init_wizard(args.config)
        return

    if args.command == "example":
        print(CONFIG_EXAMPLE)
        return

    if not args.config.exists():
        LOG.error("❌ Config file not found: %s", args.config)
        LOG.info("💡 Run 'python nextquestion.py init' for interactive setup, "
                 "or 'python nextquestion.py example' to see the template.")
        sys.exit(1)

    cfg = load_config(args.config)
    if args.model:
        cfg.model = args.model

    if args.reindex or args.command == "reindex":
        with Store(cfg) as store:
            SearchIndex(store).rebuild()
        return

    if args.command in ("link", "query") or args.command is None:
        if not cfg.vault_path:
            raise ConfigError("'vault_path' is not set in config or RAG_VAULT env var.")
    if args.command in ("enrich", "query") or args.command is None:
        if not cfg.api_key:
            raise ConfigError("'api_key' is not set in config or OPENAI_API_KEY env var.")

    try:
        if args.command == "chunk":
            Ingestor(cfg).run(args.document, args.start_page, args.end_page, args.dry_run)
        elif args.command == "enrich":
            Enricher(cfg, LLMClient(cfg)).run(args.limit)
        elif args.command == "link":
            GraphBuilder(cfg).run(args.alias_db)
        elif args.command == "query":
            if args.text:
                run_query(cfg, args.text)
            else:
                repl(cfg)
        else:
            repl(cfg)
    except NextQuestionError as exc:
        LOG.error("❌ Fatal: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
