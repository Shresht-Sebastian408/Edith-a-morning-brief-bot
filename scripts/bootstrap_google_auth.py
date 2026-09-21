"""One-time local OAuth consent. Prints a refresh token for .env / CI secrets.

    python scripts/bootstrap_google_auth.py

BEFORE RUNNING THIS, in Google Cloud Console:

  1. APIs & Services > Library: enable the Gmail API and the Google Calendar API.
  2. APIs & Services > OAuth consent screen:
       - User type: External
       - Add yourself under "Test users"
       - PUBLISHING STATUS: click "PUBLISH APP" so it reads "In production".
         This is the important one. While the status is "Testing", Google expires
         every refresh token after exactly 7 days, and your daily brief dies each
         week with an opaque "invalid_grant". Publishing does NOT require
         verification for personal use under 100 users - you just click through
         an "unverified app" warning once, during this script.
  3. APIs & Services > Credentials > Create OAuth client ID > Desktop app.
     Put the client id and secret in .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from brief.config import GOOGLE_SCOPES, get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    client_id = settings.google_client_id or os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = settings.google_client_secret or os.environ.get(
        "GOOGLE_CLIENT_SECRET", ""
    )

    if not client_id or not client_secret:
        print(
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.\n"
            "See the docstring at the top of this file for where to get them.",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=GOOGLE_SCOPES,
    )

    print("Opening your browser for consent...")
    print("You will see an 'unverified app' warning. Click Advanced > Go to ...")
    print()

    # access_type=offline is what makes Google issue a refresh token at all;
    # prompt=consent forces a NEW one even if you have consented before.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print(
            "\nNo refresh token returned. Revoke the app at "
            "https://myaccount.google.com/permissions and run this again.",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 70)
    print("Add this to .env (and to GitHub repo secrets as GOOGLE_REFRESH_TOKEN):")
    print("=" * 70)
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 70)
    print("\nScopes granted:")
    for scope in creds.scopes or []:
        print(f"  {scope}")
    print("\nIf this token stops working in ~7 days, your OAuth consent screen")
    print("is still in 'Testing'. Publish it and re-run this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
