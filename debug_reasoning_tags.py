"""
TEMP debug script — not part of the app. Calls ask_with_tools() directly
against the Addams test circle to see the raw model output (before tag
extraction) without going through GroupMe/webhook/deploy at all.

Run from the repo root with your venv active:
    python3 debug_reasoning_tags.py

Delete this file once the <reasoning>/<reply> tag issue is diagnosed.
"""
import asyncio
import logging

# INFO-level logging to console so _extract_reply()'s
# logger.info("[ask_with_tools] Reasoning: ...") calls actually print here —
# by default only WARNING+ shows up without this.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from take_five.messages import ask_with_tools

# Addams Family test circle (internal test ensemble)
ADDAMS_CIRCLE_ID = "0bfb1e3e-0dbe-4192-8b53-702f06d94b49"


async def main():
    # Non-tool question — pure lookup, no medication write path involved.
    print("\n\n########## TEST 1: non-tool question ##########")
    reply = await ask_with_tools(
        question="@T5 what medications is Morticia taking?",
        circle_id=ADDAMS_CIRCLE_ID,
        response_format="text",
        channel="groupme",
    )
    print("\n>>> FINAL EXTRACTED REPLY:")
    print(reply)

    # Tool-relevant question — disambiguation case (Cefalexin already has
    # morning noted from earlier tests, so asking to add "evening" too
    # should trigger real reasoning about merging timing rather than a
    # no-op repeat of the earlier L-Lysine test).
    print("\n\n########## TEST 2: tool-relevant question ##########")
    reply2 = await ask_with_tools(
        question="@T5 can you also add evening timing for the Cefalexin",
        circle_id=ADDAMS_CIRCLE_ID,
        response_format="text",
        channel="groupme",
    )
    print("\n>>> FINAL EXTRACTED REPLY:")
    print(reply2)


if __name__ == "__main__":
    asyncio.run(main())
