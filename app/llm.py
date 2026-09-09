"""
The one place that talks to a model server.

Three call shapes, all against llama-server's OpenAI-compatible API:

    chat()    -> /v1/chat/completions with text content
    vision()  -> /v1/chat/completions with a text+image content list
    embed()   -> /v1/embeddings          (needs --embedding at spawn)
    rerank()  -> /v1/rerank              (needs --reranking at spawn)

Callers pass a capability and optionally a preferred model; the router
resolves that to a port. No agent ever hardcodes a port or a model name.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path

import httpx

from .audit import log_event
from .config import REQUEST_TIMEOUT
from .router import route


# ------------------------------------------------------------------ helpers

def _image_block(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"image not found: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _post(port: int, path: str, payload: dict) -> dict:
    r = httpx.post(f"http://127.0.0.1:{port}{path}",
                   json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def extract_json(text: str) -> dict | list:
    """
    Small models wrap JSON in prose or fences no matter how firmly you ask.
    Strip the wrapper, then fall back to the outermost brace/bracket pair.
    """
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no parsable JSON in model output:\n{text[:500]}")


# ------------------------------------------------------------------ calls

def chat(prompt: str,
         *,
         capability: str = "text",
         model: str | None = None,
         system: str | None = None,
         messages: list[dict] | None = None,
         max_tokens: int = 2048,
         temperature: float = 0.3,
         json_schema: dict | None = None,
         grammar: str | None = None,
         session_id: str | None = None) -> str:
    """
    Text completion. `messages` overrides `prompt` when a full history
    is needed. `json_schema` uses llama-server's constrained decoding,
    which is how we guarantee the planner emits valid JSON on a small model.
    """
    actual_model, port = route(model, capability)

    if messages is None:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": json_schema, "strict": True},
        }
    if grammar is not None:
        payload["grammar"] = grammar

    data = _post(port, "/v1/chat/completions", payload)
    text = data["choices"][0]["message"]["content"]

    log_event("model.chat", model=actual_model, port=port,
              capability=capability, session_id=session_id,
              usage=data.get("usage", {}))
    return text


def vision(prompt: str,
           images: list[str | Path],
           *,
           model: str | None = None,
           system: str | None = None,
           max_tokens: int = 2048,
           temperature: float = 0.2,
           json_schema: dict | None = None,
           session_id: str | None = None) -> str:
    """Same endpoint as chat, but the content is a text+image list."""
    actual_model, port = route(model, "vision")

    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append(_image_block(img))

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    payload: dict = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Small vision models loop when the prompt is long or ambiguous —
        # they start echoing the instructions back. This bounds the damage.
        "repeat_penalty": 1.15,
    }
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": json_schema, "strict": True},
        }

    data = _post(port, "/v1/chat/completions", payload)
    text = data["choices"][0]["message"]["content"]

    log_event("model.vision", model=actual_model, port=port,
              n_images=len(images), session_id=session_id,
              usage=data.get("usage", {}))
    return text


def embed(texts: list[str],
          *,
          model: str | None = None,
          session_id: str | None = None) -> list[list[float]]:
    """Vectors out. No generation — this model cannot answer anything."""
    actual_model, port = route(model, "embedding")
    data = _post(port, "/v1/embeddings", {"input": texts})

    vectors = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
    log_event("model.embed", model=actual_model, port=port,
              n_texts=len(texts), dims=len(vectors[0]) if vectors else 0,
              session_id=session_id)
    return vectors


def rerank(query: str,
           documents: list[str],
           *,
           top_n: int | None = None,
           model: str | None = None,
           session_id: str | None = None) -> list[dict]:
    """Returns [{index, relevance_score}] sorted best-first."""
    actual_model, port = route(model, "reranker")
    payload = {"query": query, "documents": documents}
    if top_n:
        payload["top_n"] = top_n

    data = _post(port, "/v1/rerank", payload)
    results = data.get("results", [])
    log_event("model.rerank", model=actual_model, port=port,
              n_docs=len(documents), session_id=session_id)
    return results


def chat_json(prompt: str, schema: dict, **kw) -> dict | list:
    """Constrained JSON with a parse fallback for servers that ignore the schema."""
    raw = chat(prompt, json_schema=schema, **kw)
    return extract_json(raw)


def converse(message: str,
             history: list[str] | None = None,
             *,
             model: str | None = None,
             session_id: str | None = None) -> str:
    """
    Plain conversation. No agents, no tools, no plan.

    Used when a request needs no work done — a greeting, a question about the
    system itself, a follow-up that touches no files. Routing those through an
    agent chain produces a five-step plan for "hello", which is worse than
    useless and makes the system look broken.
    """
    system = (
        "You are the assistant in a self-hosted AI workbench used in "
        "industrial and government settings. Everything runs on local models "
        "on this machine; nothing is sent anywhere.\n\n"
        "You can read scanned documents, drawings and photographs; search the "
        "organisation's local knowledge base; write and run code; perform "
        "engineering calculations; and produce Word, PowerPoint and Excel "
        "files.\n\n"
        "Reply briefly and plainly. Never invent facts about the user's "
        "documents, data or organisation. If they ask about something you "
        "have not been shown, say you need them to attach it rather than "
        "describing what it probably contains."
    )
    msgs = [{"role": "system", "content": system}]
    for h in (history or [])[-6:]:
        role, _, content = h.partition(": ")
        msgs.append({"role": "assistant" if role == "assistant" else "user",
                     "content": content or h})
    msgs.append({"role": "user", "content": message})

    return chat("", messages=msgs, model=model, capability="text",
                session_id=session_id, max_tokens=600, temperature=0.5)
