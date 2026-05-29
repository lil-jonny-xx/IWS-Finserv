#!/usr/bin/env python3
"""
Gmail OAuth2 Setup — IWS MIS Portal
Run ONCE to authorise the central Gmail inbox that receives all CAS emails.
All 6 entity alias emails must have auto-forward configured to this inbox.

Usage:
  python workers/oauth_setup.py --token workers/gmail_token_central.json

Opens a browser — sign in with the central Gmail account.
Token saved to the specified file.
"""
import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(__file__).parent / "gmail_credentials.json"


def main():
    parser = argparse.ArgumentParser(description="Generate Gmail OAuth2 token")
    parser.add_argument(
        "--token",
        required=True,
        help="Path to save the token (e.g. workers/gmail_token_pan1.json)",
    )
    args = parser.parse_args()

    token_path = Path(args.token)

    if not CREDENTIALS_FILE.exists():
        print(
            f"\n❌  credentials.json not found at: {CREDENTIALS_FILE}\n"
            "   Download it from Google Cloud Console:\n"
            "   APIs & Services → Credentials → OAuth 2.0 Client ID → Download JSON\n"
            "   Save it as: workers/gmail_credentials.json\n"
        )
        raise SystemExit(1)

    print(f"\nOpening browser for Gmail authorisation...")
    print(f"Sign in with the Gmail account for this PAN.\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=8765, open_browser=False)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"\n✅  Token saved to: {token_path}")


if __name__ == "__main__":
    main()
