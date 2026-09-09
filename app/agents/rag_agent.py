"""
RAG agent.

Four stages in fixed order. Only the first, third and fourth involve a model;
the search itself is deterministic and always runs.

  1. query   [LLM]   rewrite the question as 2-3 differently-worded queries
  2. search  [code]  embed each, similarity search, sequential, merge, dedupe
  3. score   [LLM]   drop chunks that do not bear on the question
  4. answer  [LLM]   write the answer from what survived, with citations

Sequential search is deliberate. The embedding model shares one GPU with
everything else; three concurrent searches queue anyway and add memory
pressure without finishing sooner.

The answer is written here rather than in the composer because this is where
the passages are. The composer sees the finished answer afterwards and only
tightens it.
"""

from __future__ import annotations

from .. import llm
from ..audit import log_event
from ..rag import index
from .base import Agent, AgentInput, AgentResult

QUERY_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["keep"],
}

QUERY_PROMPT = """Rewrite this question as {n} short search queries for a document index.

Question: {question}

Each query must use different wording — a synonym, the formal term, or an
identifier that appears in the question. A few words each. Do not answer the
question. Return JSON only."""

SCORE_PROMPT = """Which of these passages actually bear on the question?

Question: {question}

{passages}

Return the numbers of the passages that contain information answering the
question. Exclude passages that merely mention the same subject without
answering. If none qualify, return an empty list. Return JSON only."""

ANSWER_SYSTEM = """You answer the question using only the passages provided.

Write a real answer in prose, the way a knowledgeable colleague would reply.
Not a report. No headings, no "Findings:", no numbered list unless the answer
genuinely is a list of things.

Rules:
- Every statement must come from a passage. Never use outside knowledge and
  never fill a gap with what is usually true.
- Cite inline after the statement it supports, in square brackets, giving the
  document name and page as shown at the top of each passage.
- Quote exact values, limits, identifiers and figures as written. Do not
  round or paraphrase a number.
- Answer the question asked. Do not summarise everything retrieved.
- If the passages only partly answer it, say what they cover and what is
  missing.
- If they do not answer it, say so in one sentence. Do not pad.
- No preamble, no closing offer of further help."""


class RagAgent(Agent):
    name = "rag"
    capability = "text"
    description = (
        "Answers questions from the organisation's indexed documents — "
        "manuals, procedures, specifications, past correspondence, and any "
        "text document the user has added. Expands the question into several "
        "queries, searches, discards irrelevant passages, and answers with "
        "document and page citations. Use this for any question about a "
        "document that is indexed. Nothing leaves the machine."
    )
    input_kind = "question"
    output_kind = "grounded answer with citations"

    N_QUERIES = 3
    PER_QUERY = 5
    MAX_SCORED = 14

    def execute(self, inp: AgentInput) -> AgentResult:
        question = inp.task or inp.payload.get("question", "")
        if not question:
            return AgentResult(self.name, False, "no question supplied",
                               error="rag agent needs a question")

        if index.stats()["n_chunks"] == 0:
            return AgentResult(
                self.name, False,
                "No documents are indexed. Add them on the Knowledge tab, or "
                "attach one to this chat.",
                error="empty index")

        # The resolver may have established which documents this is about;
        # searching the rest of the corpus would only add noise.
        scope = inp.options.get("documents") or None

        # 1 ------------------------------------------------------- queries
        queries = self._queries(question, int(inp.options.get(
            "n_queries", self.N_QUERIES)), inp)

        # 2 -------------------------------------------------------- search
        hits, seen = [], set()
        for q in queries:
            for h in index.search(q, top_k=self.PER_QUERY, docs=scope,
                                  session_id=inp.session_id):
                key = h["text"][:200]
                if key not in seen:
                    seen.add(key)
                    hits.append(h)

        log_event("rag.retrieved", session_id=inp.session_id,
                  n_queries=len(queries), n_unique=len(hits), scope=scope)

        if not hits:
            return AgentResult(
                self.name, True,
                "Nothing in the indexed documents matched this question.",
                data={"question": question, "queries": queries,
                      "answer": "Nothing in the indexed documents matched "
                                "this question.", "passages": []},
                sources=[])

        hits.sort(key=lambda h: -(h.get("score") or 0))
        hits = hits[:self.MAX_SCORED]

        # 3 --------------------------------------------------------- score
        kept = self._score(question, hits, inp)

        if not kept:
            return AgentResult(
                self.name, True,
                "The indexed documents contain related material but nothing "
                "that answers this question.",
                data={"question": question, "queries": queries,
                      "answer": "The indexed documents contain related "
                                "material but nothing that answers this "
                                "question.",
                      "passages": [], "n_retrieved": len(hits)},
                sources=[])

        # 4 -------------------------------------------------------- answer
        answer = self._answer(question, kept, inp)

        docs = sorted({h["doc"] for h in kept})
        return AgentResult(
            agent=self.name, ok=True,
            summary=answer,
            data={
                "question": question,
                "queries": queries,
                "answer": answer,
                "n_retrieved": len(hits),
                "n_used": len(kept),
                "passages": [{"doc": h["doc"], "page": h["page"],
                              "section": h.get("section")} for h in kept],
                "documents": docs,
            },
            sources=[{"doc": h["doc"], "page": h["page"],
                      "section": h.get("section")} for h in kept],
            model_used=inp.model,
        )

    # ------------------------------------------------------------- stages

    def _queries(self, question: str, n: int, inp: AgentInput) -> list[str]:
        """The original wording always goes first — the rewrite may be worse."""
        out = [question]
        try:
            res = llm.chat_json(
                QUERY_PROMPT.format(n=n, question=question), QUERY_SCHEMA,
                model=inp.model, capability="text",
                session_id=inp.session_id, max_tokens=250, temperature=0.4)
            for q in (res.get("queries") or []):
                q = str(q).strip()
                if q and q.lower() != question.lower() and q not in out:
                    out.append(q)
        except Exception as e:
            log_event("rag.query_expansion_failed",
                      session_id=inp.session_id, error=str(e))
        return out[:n]

    def _score(self, question: str, hits: list[dict],
               inp: AgentInput) -> list[dict]:
        """
        Drop passages that mention the subject without answering it.

        On failure everything is kept — a noisy answer beats no answer, and
        the answering stage is told to use only what bears on the question.
        """
        listing = "\n\n".join(
            f"{i}. [{h['doc']}"
            + (f", p.{h['page']}" if h.get("page") else "")
            + f"] {h['text'][:400]}"
            for i, h in enumerate(hits))

        try:
            res = llm.chat_json(
                SCORE_PROMPT.format(question=question, passages=listing),
                SCORE_SCHEMA, model=inp.model, capability="text",
                session_id=inp.session_id, max_tokens=200, temperature=0.1)
            keep = [i for i in (res.get("keep") or [])
                    if isinstance(i, int) and 0 <= i < len(hits)]
            log_event("rag.scored", session_id=inp.session_id,
                      n_in=len(hits), n_kept=len(keep))
            if keep:
                return [hits[i] for i in keep]
            # An empty list is a real verdict: nothing here answers it.
            return []
        except Exception as e:
            log_event("rag.scoring_failed", session_id=inp.session_id,
                      error=str(e))
            return hits

    def _answer(self, question: str, passages: list[dict],
                inp: AgentInput) -> str:
        """
        The citation marker sits immediately above each passage rather than in
        a separate legend — a small model asked to match numbered sources to
        text it read earlier gets it wrong often enough to matter.
        """
        blocks = []
        for h in passages:
            cite = h["doc"] + (f", p.{h['page']}" if h.get("page") else "")
            sect = f" — {h['section']}" if h.get("section") else ""
            blocks.append(f"[{cite}{sect}]\n{h['text']}")

        prompt = ("Passages:\n\n" + "\n\n".join(blocks)
                  + f"\n\nThe user asked: {question}\n\nAnswer it.")

        return llm.chat(prompt, system=ANSWER_SYSTEM, model=inp.model,
                        capability="text", session_id=inp.session_id,
                        max_tokens=1500, temperature=0.2).strip()
