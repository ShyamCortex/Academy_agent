"""First-time Google sign-in for the test calendar. Run this on your own laptop (it opens a browser).

    python -m email_assistant.academy.google_auth            sign in, then check the test calendar
    python -m email_assistant.academy.google_auth --check    only check that the saved sign-in works

You need two things first (see docs/agent/LAPTOP_SETUP.md):
- the OAuth "desktop app" client file from Google Cloud, saved as .secrets/academy_google_credentials.json
  (or the path in GOOGLE_CREDENTIALS_FILE)
- GOOGLE_CALENDAR_ID in .env: the id of the TEST calendar

The sign-in is saved in .secrets/academy_google_token.json (or GOOGLE_TOKEN_FILE). Both files are git-ignored.
This script never prints a token or a secret. The only permission asked for is `calendar.events`.
In Google's "testing" mode the sign-in expires after 7 days: run this command again.
"""

import argparse
import os
import sys

from dotenv import load_dotenv

from email_assistant.academy import settings
from email_assistant.academy.calendar_backend import SCOPES, credentials_path, load_credentials, token_path


def sign_in() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = credentials_path()
    if not secrets.exists():
        print(f"Cannot find the Google OAuth client file at {secrets}.")
        print("Download it from Google Cloud (APIs & Services > Credentials > your Desktop client > Download JSON)")
        print("and save it there. See docs/agent/LAPTOP_SETUP.md, part B.")
        return 1
    print("A browser window will open. Sign in with the TEST Google account and allow calendar access.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    target = token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(target, 0o600)  # only you can read it (no effect on Windows)
    except OSError:
        pass
    print(f"Signed in. Saved the sign-in to {target}.")
    return 0


def check() -> int:
    """Read one event from the test calendar to prove the sign-in and the calendar id work."""
    from googleapiclient.discovery import build

    try:
        calendar_id = settings.require_env(settings.CALENDAR_ID_VAR)
        service = build("calendar", "v3", credentials=load_credentials(), cache_discovery=False)
        service.events().list(calendarId=calendar_id, maxResults=1).execute(num_retries=2)
    except settings.AcademyConfigError as exc:
        print(f"Not ready: {exc}")
        return 1
    except Exception as exc:  # the Google client raises several kinds; show the kind only, never details
        print(f"The test calendar could not be read ({type(exc).__name__}). Check GOOGLE_CALENDAR_ID and that this account can edit that calendar.")
        return 1
    print("OK: the saved sign-in can read the test calendar.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="Sign in to Google for the test calendar.")
    parser.add_argument("--check", action="store_true", help="only check the saved sign-in; do not open a browser")
    args = parser.parse_args(argv)
    if not args.check:
        code = sign_in()
        if code:
            return code
    return check()


if __name__ == "__main__":
    sys.exit(main())
