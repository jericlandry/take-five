"""
One-off migration script: pulls the current text of every LangSmith Hub prompt
Take Five depends on and writes each one to take_five/prompts/{name}.md.

Run this once, review the output files, then take_five/utils.py's get_prompt()
can be pointed at the local files instead of Hub. Safe to delete this script
(or leave it around) after the migration — it's not imported by the app.

Usage:
    python export_prompts_from_langsmith.py
"""
import os

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

# Hub name -> local filename (snake_case, matching get_prompt()'s lookup convention)
PROMPTS_TO_EXPORT = {
    "t5-system-prompt":       "t5_system_prompt",
    "t5-week-summary":        "t5_week_summary",
    "chunk-context-summary":  "chunk_context_summary",
}

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "take_five", "prompts")


def extract_template_text(pulled_prompt) -> str:
    """
    Pulls the raw template string out of whatever LangChain object
    ls_client.pull_prompt() returns.

    Mirrors the extraction already used in take_five/messages.py
    (fetch_prompt("t5-system-prompt").messages[0].prompt.template) —
    assumes a single-message ChatPromptTemplate. If a prompt turns out to
    have more than one message, this prints a warning and still only
    exports the first message's text, so you don't lose the rest silently
    without noticing.
    """
    messages = getattr(pulled_prompt, "messages", None)
    if messages is None:
        # Some Hub prompts are plain PromptTemplate (not Chat), which has
        # .template directly rather than .messages[0].prompt.template
        template = getattr(pulled_prompt, "template", None)
        if template is not None:
            return template
        raise TypeError(
            f"Don't know how to extract template text from {type(pulled_prompt)}"
        )

    if len(messages) > 1:
        print(
            f"  WARNING: this prompt has {len(messages)} messages — "
            f"only exporting messages[0]. Check the Hub UI if the rest matters."
        )

    return messages[0].prompt.template


def main():
    client = Client()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for hub_name, local_name in PROMPTS_TO_EXPORT.items():
        print(f"Pulling '{hub_name}' from LangSmith Hub...")
        try:
            pulled = client.pull_prompt(hub_name)
        except Exception as e:
            print(f"  FAILED: {e}")
            print(f"  Skipping '{hub_name}' — check the name/workspace and try again.")
            continue

        try:
            text = extract_template_text(pulled)
        except Exception as e:
            print(f"  FAILED to extract template text: {e}")
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{local_name}.md")
        with open(out_path, "w") as f:
            f.write(text)

        print(f"  Wrote {len(text)} chars to take_five/prompts/{local_name}.md")

    print("\nDone. Review the files in take_five/prompts/ before wiring up get_prompt().")


if __name__ == "__main__":
    main()
