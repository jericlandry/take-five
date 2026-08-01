"""
Manual debug script — not part of the app, not a pytest suite. Tests
whether "concurrent" ask_with_tools() calls actually run concurrently, or
serialize despite asyncio.gather().

Run from anywhere (repo root recommended) with your venv active:
    python3 tests/manual/debug_concurrency.py

History: originally written to test langchain's ChatAnthropic.invoke()
(synchronous — blocks the event loop for the full LLM round-trip), which
measured a 1.03x "speedup" on 5 concurrent calls, i.e. no real concurrency
at all. Swapping ask_with_tools()'s two LLM calls to .ainvoke() fixed
this — same test then measured 2.95x. Kept here as a regression check:
if this ever drops back toward 1.0x, something reintroduced a blocking
call on the hot path (most likely a .invoke() slipping back in, or a new
synchronous call added to ask_with_tools() without checking).

The DB layer (psycopg2) still has no async mode, so some serialization on
the DB round-trip + embedding call remains even with .ainvoke() in place —
2.95x, not 5x, on 5 concurrent calls is expected, not a bug. Closing that
last gap would mean asyncio.to_thread() around the DB calls, a separate
piece of work.

All five questions are read-only, safe to re-run.
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

# Allow running this script from a subdirectory (tests/manual/) while still
# importing the take_five package from the repo root — Python only
# auto-adds the *script's own* directory to sys.path, not the repo root,
# so without this the `from take_five.messages import ...` below would
# fail with ModuleNotFoundError regardless of current working directory.
# This file lives at <repo_root>/tests/manual/debug_concurrency.py, so
# parents[2] is <repo_root>: parents[0]=manual, [1]=tests, [2]=repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from take_five.messages import ask_with_tools

ADDAMS_CIRCLE_ID = "0bfb1e3e-0dbe-4192-8b53-702f06d94b49"

QUESTIONS = [
    "@T5 what medications is Morticia taking?",
    "@T5 what medications is Gomez taking?",
    "@T5 can you give a mobility report for Gomez",
    "@T5 what timing does the Cefalexin have?",
    "@T5 what supplements is Morticia on?",
]


async def timed_call(i: int, question: str, t_ref: float):
    print(f"[t+{time.monotonic() - t_ref:5.2f}s] CALL {i} STARTING: {question!r}")
    t0 = time.monotonic()
    reply = await ask_with_tools(
        question=question,
        circle_id=ADDAMS_CIRCLE_ID,
        response_format="text",
        channel="groupme",
    )
    elapsed = time.monotonic() - t0
    print(f"[t+{time.monotonic() - t_ref:5.2f}s] CALL {i} DONE  (took {elapsed:.2f}s)")
    return i, elapsed


async def main():
    print("=== SEQUENTIAL BASELINE ===")
    t_ref = time.monotonic()
    seq_start = time.monotonic()
    for i, q in enumerate(QUESTIONS, start=1):
        await timed_call(i, q, t_ref)
    seq_total = time.monotonic() - seq_start
    print(f"\nSEQUENTIAL TOTAL: {seq_total:.2f}s for {len(QUESTIONS)} calls\n")

    print("=== CONCURRENT (asyncio.gather) ===")
    t_ref = time.monotonic()
    conc_start = time.monotonic()
    results = await asyncio.gather(
        *[timed_call(i, q, t_ref) for i, q in enumerate(QUESTIONS, start=1)]
    )
    conc_total = time.monotonic() - conc_start
    print(f"\nCONCURRENT TOTAL: {conc_total:.2f}s for {len(QUESTIONS)} calls")

    speedup = seq_total / conc_total
    print("\n=== SUMMARY ===")
    print(f"Sequential total: {seq_total:.2f}s")
    print(f"Concurrent total: {conc_total:.2f}s")
    print(f"Speedup: {speedup:.2f}x")

    if speedup < 1.5:
        print(
            "\nSpeedup near 1.0x means calls are serializing despite gather() —\n"
            "check that ask_with_tools()'s two LLM calls are still using\n"
            "llm.ainvoke() and not llm.invoke(). A sync call there blocks the\n"
            "single event loop thread for the full LLM round-trip, which\n"
            "defeats gather() regardless of how many tasks are scheduled."
        )
    else:
        print(
            "\nReal concurrency confirmed (expect ~2.5-3x with the current\n"
            "architecture — psycopg2 has no async mode, so some serialization\n"
            "on DB round-trips and the embedding call remains even with\n"
            "ainvoke() in place)."
        )


if __name__ == "__main__":
    asyncio.run(main())
