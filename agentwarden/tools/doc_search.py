"""Keyword document retrieval with a hand-written BM25 (Decision D8; vector RAG arrives in M2)."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agentwarden.gateway.spec import ToolContext, ToolOutput, ToolSpec, Trust

_STOP = set("a an the of to in on for and or is are be by with at from as it this that what which how do does "
            "can i we you our your".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    section: str
    text: str


def chunk_markdown(doc_id: str, md: str) -> list[Chunk]:
    title = next((ln[2:].strip() for ln in md.splitlines() if ln.startswith("# ")), doc_id)
    chunks, section, buf = [], "intro", []
    for line in md.splitlines():
        if line.startswith("## "):
            if "".join(buf).strip():
                chunks.append(Chunk(doc_id, title, section, "\n".join(buf).strip()))
            section, buf = line[3:].strip(), []
        elif not line.startswith("# "):
            buf.append(line)
    if "".join(buf).strip():
        chunks.append(Chunk(doc_id, title, section, "\n".join(buf).strip()))
    return chunks


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [tokenize(f"{c.title} {c.section} {c.text}") for c in chunks]
        self.tf = [Counter(d) for d in self.docs]
        self.avgdl = sum(map(len, self.docs)) / max(len(self.docs), 1)
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        # BM25 idf with +1 to keep it positive for very common terms.
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 3) -> list[tuple[float, Chunk]]:
        q = tokenize(query)
        scored = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                scored.append((s, self.chunks[i]))
        scored.sort(key=lambda x: (-x[0], x[1].doc_id, x[1].section))  # deterministic tie-break
        return scored[:k]


def load_index(docs_dir: Path) -> BM25Index:
    chunks: list[Chunk] = []
    for p in sorted(Path(docs_dir).glob("*.md")):
        chunks.extend(chunk_markdown(p.stem, p.read_text(encoding="utf-8")))
    return BM25Index(chunks)


class DocSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=2, max_length=300, description="Keywords describing what to find")
    k: int = Field(3, ge=1, le=5, description="Number of passages to return")


def make_doc_search_tool(docs_dir: Path) -> ToolSpec:
    index = load_index(docs_dir)

    def run(args: DocSearchArgs, ctx: ToolContext) -> ToolOutput:
        hits = index.search(args.query, args.k)
        if not hits:
            return ToolOutput(content="No matching passages.", data=[], meta={"hits": 0})
        parts = [f"[{c.doc_id} § {c.section}] (score {s:.2f})\n{c.text}" for s, c in hits]
        return ToolOutput(content="\n\n".join(parts), data=[c.doc_id for _, c in hits],
                          meta={"hits": len(hits), "doc_ids": [c.doc_id for _, c in hits]})

    return ToolSpec(
        name="doc_search",
        description="Search Lumora's internal documents (policies, runbooks, FAQs, guidelines). "
                    "Returns the most relevant passages with their document id.",
        args_model=DocSearchArgs,
        fn=run,
        required_capabilities=frozenset({"docs:read"}),
        output_trust=Trust.UNTRUSTED,
        timeout_s=5.0,
        max_calls_per_execution=8,
    )
