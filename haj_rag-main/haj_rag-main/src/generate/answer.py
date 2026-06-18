"""
RAG answer stage: question -> retrieve -> ground a GPT-5.2 answer on the retrieval.

The full cycle:
  1. Retrieve the top-k chunks with the hybrid (vector + BM25) retriever.
  2. Build a context block from the retrieved chunks (display text + heading + pages).
  3. Ask gpt-5.2-chat-2 (Azure OpenAI) to answer the question USING ONLY that context.

The system prompt forbids answering from the model's own knowledge: if the
retrieved excerpts don't contain the answer, the model must say so rather than
guess. Every answer is in Arabic and cites the section heading + page span of
the excerpt it used, so the user can verify against the book.

Config (.env) — reuses the Azure OpenAI resource from the embed stage:
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_API_VERSION
  AZURE_OPENAI_CHAT_DEPLOYMENT   (default gpt-5.2-chat-2)

Run:  python -m src.generate.answer --ask                  (interactive loop)
      python -m src.generate.answer --question "…" -k 6    (one-shot)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.embed.embed import build_bm25, get_collection, hybrid_search, load_chunks, make_client

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHAT_DEPLOYMENT = "gpt-5.2-chat-2"
DEFAULT_K = 6

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    print(f"[answer] {msg}", flush=True)


# The grounding contract. Kept in Arabic so the model stays in-language and the
# rules read naturally alongside the Arabic context.
SYSTEM_PROMPT = """\
أنت مساعد فقهي متخصص في أحكام الحج والعمرة، تجيب اعتمادًا على كتاب «الحج والعمرة» الصادر عن دار الإفتاء المصرية.

قواعد صارمة يجب الالتزام بها:
1. أجب فقط من «المقاطع» المرفقة في السياق. لا تستعمل معلوماتك العامة أو أي مصدر خارج هذه المقاطع إطلاقًا.
2. إذا لم تكن الإجابة موجودة في المقاطع المرفقة، فقل صراحةً: «لا يتناول الكتاب هذه المسألة في المقاطع المتاحة» — ولا تخمّن ولا تستنتج من خارج النص.
3. اعتمد على نص المقاطع كما هو؛ لا تضف أحكامًا أو تفصيلات غير مذكورة فيها.
4. اذكر في نهاية الإجابة المصدر: عنوان القسم ورقم الصفحة (أو الصفحات) للمقطع الذي اعتمدت عليه، بين قوسين.
5. أجب باللغة العربية الفصحى، بتفاصيل ووضوح.
"""


def build_context(hits: list[dict]) -> str:
    """Format retrieved chunks as numbered sources the model can cite."""
    blocks = []
    for i, h in enumerate(hits, start=1):
        m = h["meta"]
        head = m["heading_path"] or m["topic_title"]
        pages = f"ص {m['page_start']}" + (f"-{m['page_end']}" if m["page_end"] != m["page_start"] else "")
        blocks.append(f"[مقطع {i}] (القسم: {head} — {pages})\n{h['text'].strip()}")
    return "\n\n---\n\n".join(blocks)


def make_chat_client():
    """Reuse the Azure OpenAI client builder from the embed stage, but return the
    chat deployment instead of the embedding one."""
    client, _ = make_client()  # validates endpoint/key, builds AzureOpenAI
    deployment = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", DEFAULT_CHAT_DEPLOYMENT)
    return client, deployment


def answer(question: str, k: int = DEFAULT_K, *, col=None, chunks=None, bm25=None,
           embed_client=None, embed_deployment: str | None = None,
           chat_client=None, chat_deployment: str | None = None) -> dict:
    """Retrieve, ground, and answer. Returns {answer, hits}.

    Pass warm handles (collection, bm25, both Azure clients) to reuse them across
    many questions; otherwise they're built per call.
    """
    if embed_client is None:
        embed_client, embed_deployment = make_client()
    hits = hybrid_search(question, k=k, col=col, client=embed_client,
                         deployment=embed_deployment, chunks=chunks, bm25=bm25)
    context = build_context(hits)

    if chat_client is None:
        chat_client, chat_deployment = make_chat_client()

    user_msg = (
        f"السياق (مقاطع من الكتاب):\n\n{context}\n\n"
        f"السؤال: {question}\n\n"
        "أجب اعتمادًا على المقاطع أعلاه فقط، مع ذكر المصدر."
    )
    # Note: gpt-5.2-chat-2 only supports the default temperature (1); grounding is
    # enforced by the system prompt + retrieved context, not by sampling temp.
    resp = chat_client.chat.completions.create(
        model=chat_deployment,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    return {"answer": resp.choices[0].message.content.strip(), "hits": hits}


def show(result: dict) -> None:
    print("\n" + "=" * 70)
    print(result["answer"])
    print("=" * 70)
    log("المقاطع المسترجَعة:")
    for i, h in enumerate(result["hits"], start=1):
        m = h["meta"]
        log(f"  [{i}] {h['id']}  ص{m['page_start']}-{m['page_end']}  {m['heading_path']}")


def run_interactive(k: int) -> None:
    """Stay open and answer questions until the user exits (handles stay warm)."""
    col = get_collection()
    chunks = load_chunks()
    bm25 = build_bm25(chunks)
    embed_client, embed_deployment = make_client()      # warm embedding client (retrieval)
    chat_client, chat_deployment = make_chat_client()   # warm chat client (generation)
    log(f"ready ({col.count()} chunks, model {chat_deployment}). Type an Arabic question and press Enter.")
    log("Blank line, 'exit', or Ctrl+C to quit.")
    while True:
        try:
            q = input("\n🕋 سؤال> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q or q.lower() in ("exit", "quit", "q"):
            break
        result = answer(q, k=k, col=col, chunks=chunks, bm25=bm25,
                        embed_client=embed_client, embed_deployment=embed_deployment,
                        chat_client=chat_client, chat_deployment=chat_deployment)
        show(result)
    log("bye 👋")


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG answer over the Hajj book (GPT-5.2 + Chroma).")
    ap.add_argument("--question", type=str, help="answer one question and exit")
    ap.add_argument("--ask", action="store_true", help="interactive answer loop (stays open)")
    ap.add_argument("-k", type=int, default=DEFAULT_K, help="number of chunks to retrieve")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")

    if args.ask:
        run_interactive(args.k)
    elif args.question:
        show(answer(args.question, k=args.k))
    else:
        ap.error("pass --question \"…\" or --ask")
    return 0


if __name__ == "__main__":
    sys.exit(main())
