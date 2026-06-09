#!/usr/bin/env python3
"""
Gmail OAuth2 Setup — IWS MIS Portal
Run ONCE to authorise the central Gmail inbox that receives all CAS emails.
All 6 entity alias emails must have auto-forward configured to this inbox.

Usage (run on the server, with an SSH tunnel open for port 8080):
  python workers/oauth_setup.py --token workers/gmail_token_central.json

Step 1 — Open a SEPARATE terminal on your local machine and run:
  ssh -L 8080:localhost:8080 <your_server_user>@<your_server_ip>
  (keep this terminal open)

Step 2 — Back in this session, run this script. It will print a URL.

Step 3 — Open that URL in your local browser, sign in, and approve.

Step 4 — The token is saved automatically. Done.
"""
import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
CREDENTIALS_FILE = Path(__file__).parent / "gmail_credentials.json"
PORT = 8080


def main():
    parser = argparse.ArgumentParser(description="Generate Gmail OAuth2 token")
    parser.add_argument(
        "--token",
        required=True,
        help="Path to save the token (e.g. workers/gmail_token_central.json)",
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

    print("\n──────────────────────────────────────────────────────")
    print("BEFORE CONTINUING: open a new local terminal and run:")
    print(f"\n  ssh -L {PORT}:localhost:{PORT} <user>@<server_ip>\n")
    print("Then come back here and press Enter.")
    print("──────────────────────────────────────────────────────")
    input("Press Enter when the SSH tunnel is open...")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(
        port=PORT,
        open_browser=False,
        success_message="Authorised! You can close this tab.",
    )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"\n✅  Token saved to: {token_path}")


if __name__ == "__main__":
    main()
