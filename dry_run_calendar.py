"""
One-off dry run for the Care Calendar context. Prints what ask() and the
weekly digest will see for a circle -- posts nothing. Delete after use.

    python dry_run_calendar.py 0bfb1e3e-0dbe-4192-8b53-702f06d94b49            # Addams Family
    python dry_run_calendar.py 0bfb1e3e-0dbe-4192-8b53-702f06d94b49 --digest   # + full digest text (LLM call, not posted)
    python dry_run_calendar.py d50b0b7c-982c-4c9c-b8d4-8a1479b0cb58            # Addams Family & Friends (no calendar)
"""
import argparse
import asyncio
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from take_five.repository import repo
from take_five.messages import ContextBuilder

parser = argparse.ArgumentParser()
parser.add_argument("circle_id", help="care_circles.id")
parser.add_argument("--digest", action="store_true", help="also generate the full digest text")
args = parser.parse_args()

row = repo._execute("SELECT name FROM care_circles WHERE id = %s;", (args.circle_id,))
if not row:
    raise SystemExit(f"No circle with id {args.circle_id}")
circle_id = args.circle_id
print(f"circle: {row['name']} ({circle_id})")
print(f"config: {repo.get_circle_calendar_config(circle_id)}")
print(f"full clinical access: {repo.circle_has_full_clinical_access(circle_id)}\n")

print("=== ask() Care Calendar context ===")
ctx = asyncio.run(ContextBuilder.create(circle_id, "When is the next appointment?"))
print(ctx.get_calendar() or "(empty -- section omitted)")

start, end = datetime.now() - timedelta(days=7), datetime.now() + timedelta(days=1)
print("=== digest Care Calendar context ===")
digest_ctx = ContextBuilder.create_for_digest(circle_id, start, end)
print(digest_ctx.get_calendar() or "(empty -- section omitted)")

if args.digest:
    from take_five.summaries import generate_weekly_digest
    print("=== full digest (NOT posted) ===")
    print(generate_weekly_digest(circle_id, response_format="text", start_date=start, end_date=end))
