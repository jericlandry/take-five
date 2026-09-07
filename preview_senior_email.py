"""
preview_senior_email.py — Preview the senior-facing weekly email without
sending it or touching GroupMe.

Calls generate_senior_digest() only -- no send_email() call, no
main_summary.py loop, no GroupMe digest side effect. Renders the result to
HTML (same conversion send_email() itself does) and writes it to a local
file, opened automatically in your browser, so you see exactly how it'll
actually render rather than reading raw Markdown in the terminal.

Usage:
  python3 preview_senior_email.py --circle-id <uuid> [--person-id <uuid>]

If --person-id is omitted, previews the first senior-with-email found in
the circle (same selection get_seniors_in_circle/send_senior_emails uses).
"""
import argparse
import asyncio
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from take_five.integrations.sendgrid_email import render_senior_email_html, SIGNOFF_PLAIN
from take_five.repository import repo
from take_five.summaries import generate_senior_digest

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Preview the senior-facing weekly email.")
    parser.add_argument("--circle-id", required=True, help="Internal UUID of the care circle.")
    parser.add_argument("--person-id", default=None, help="Specific senior's UUID. Defaults to the first senior-with-email found.")
    args = parser.parse_args()

    seniors = repo.get_seniors_in_circle(args.circle_id)
    seniors_with_email = [s for s in seniors if s.get("email")]
    if not seniors_with_email:
        print("No seniors with an email on file in this circle.")
        return

    if args.person_id:
        senior = next((s for s in seniors_with_email if str(s["id"]) == args.person_id), None)
        if not senior:
            print(f"No senior with id {args.person_id} found with an email on file.")
            return
    else:
        senior = seniors_with_email[0]

    other_seniors = [s for s in seniors if s["id"] != senior["id"]]

    print(f"Generating preview for {senior['name']}...")
    body = asyncio.run(generate_senior_digest(args.circle_id, senior, other_seniors))

    if not body:
        print("generate_senior_digest() returned nothing -- LLM call likely failed. Check logs.")
        return

    print("\n--- Plain-text version (what non-HTML clients see) ---\n")
    print(f"{body}\n\n{SIGNOFF_PLAIN}")

    html_body = render_senior_email_html(body)
    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Senior email preview</title></head>
<body style="margin:40px 0;">{html_body}</body></html>"""

    out_path = Path("senior_email_preview.html")
    out_path.write_text(full_html)
    print(f"\nWrote rendered preview to {out_path.resolve()}")

    webbrowser.open(f"file://{out_path.resolve()}")


if __name__ == "__main__":
    main()
