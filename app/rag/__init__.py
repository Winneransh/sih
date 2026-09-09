"""RAG: deterministic ingestion and resolution, agentic retrieval."""

from .index import (chunk_and_embed, ingest_file, ingest_text, search,
                    stats, clear, documents)
from .resolver import resolve_documents

__all__ = ["chunk_and_embed", "ingest_file", "ingest_text", "search", "stats",
           "clear", "documents", "resolve_documents"]
