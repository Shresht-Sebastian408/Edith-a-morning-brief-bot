"""Shared Google credential construction.

There is no interactive consent here and no token.json on disk. The refresh
token is supplied via configuration, which is what lets this run unattended in
CI. Getting that token is a one-time local step: scripts/bootstrap_google_auth.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from brief.config import GOOGLE_SCOPES, Settings

if TYPE_CHECKING:  # heavy google imports stay out of module import time
    from google.oauth2.credentials import Credentials

TOKEN_URI = "https://oauth2.googleapis.com/token"


def build_credentials(settings: Settings) -> "Credentials":
    from google.oauth2.credentials import Credentials

    settings.require("google_client_id", "google_client_secret", "google_refresh_token")
    # token=None is intentional: the library exchanges the refresh token for a
    # fresh access token on first use.
    return Credentials(
        token=None,
        refresh_token=settings.google_refresh_token,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        token_uri=TOKEN_URI,
        scopes=GOOGLE_SCOPES,
    )


def build_service(name: str, version: str, settings: Settings) -> Any:
    from googleapiclient.discovery import build

    # cache_discovery=False silences a noisy oauth2client warning and avoids
    # writing a discovery cache into a read-only CI filesystem.
    return build(
        name,
        version,
        credentials=build_credentials(settings),
        cache_discovery=False,
    )
