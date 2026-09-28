"""
One-off smoke test: confirm the take-five-calendar service account can read
the Addams sandbox calendar and that recurring events expand correctly.
Delete after use.
"""
import json, os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()
CAL_ID = "777d9a5eaddafd0ab32ccc33571c9b3de1c89de5ee63def5333d70c4dc899dbc@group.calendar.google.com"

creds = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
    scopes=["https://www.googleapis.com/auth/calendar.readonly"],
)
svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

now = datetime.now(timezone.utc)
resp = svc.events().list(
    calendarId=CAL_ID,
    timeMin=(now - timedelta(days=14)).isoformat(),
    timeMax=(now + timedelta(days=30)).isoformat(),
    singleEvents=True, orderBy="startTime", timeZone="America/Chicago",
).execute()

for e in resp.get("items", []):
    start = e["start"].get("dateTime") or e["start"].get("date")
    print(start, "|", e["summary"])
