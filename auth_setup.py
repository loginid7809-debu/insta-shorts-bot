"""ONE-TIME local helper: get a YouTube refresh token without copy-pasting codes.

Run this on your own computer (NOT in GitHub Actions):

    pip install -r requirements.txt
    python auth_setup.py

It opens your browser, you approve access with the Google account that owns the
channel, and it prints three values you paste into GitHub Secrets:
    YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN

It needs a downloaded OAuth *Desktop app* client JSON from Google Cloud:
    Google Cloud Console -> APIs & Services -> Credentials
    -> Create credentials -> OAuth client ID -> Desktop app -> Download JSON
Save it as client_secret.json next to this file (or pass --client-secret PATH).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def load_client(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    block = data.get("installed") or data.get("web") or {}
    client_id, client_secret = block.get("client_id"), block.get("client_secret")
    if not client_id or not client_secret:
        raise SystemExit(f"{path} is not a valid OAuth client JSON (missing client_id/secret).")
    if "installed" not in data:
        print("WARNING: this looks like a 'Web application' client. Create a *Desktop app* "
              "client instead, otherwise the redirect will fail.\n")
    project_id = data.get("installed", {}).get("project_id") or data.get("web", {}).get("project_id", "?")
    print(f"OAuth client belongs to project: {project_id}")
    print("If the browser later shows 'Error 403: access_denied / has not completed the "
          "Google verification\nprocess', that project's OAuth consent screen is missing you "
          "as a test user - see below.\n")
    return client_id, client_secret


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Get a YouTube API refresh token (one-time).")
    ap.add_argument("--client-secret", default="client_secret.json", help="OAuth desktop client JSON")
    ap.add_argument("--port", type=int, default=0, help="fixed local port for the redirect (default: random)")
    args = ap.parse_args(argv)

    path = Path(args.client_secret)
    if not path.exists():
        raise SystemExit(
            f"{path} not found.\nDownload it from Google Cloud Console -> Credentials -> "
            "OAuth client ID (Desktop app) and save it here."
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit("Missing dependency. Run: pip install google-auth-oauthlib")

    client_id, client_secret = load_client(path)

    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES)
    print("BEFORE the browser opens, confirm all three of these in Google Cloud Console")
    print("(APIs & Services -> OAuth consent screen):")
    print("  1. You are in the SAME project as the client_secret.json above.")
    print("  2. Your Gmail address is listed under 'Test users' (Audience tab).")
    print("  3. The browser is signed in with THAT account only - use an incognito window.")
    print("\nA browser window will open. Sign in with the Google account that OWNS the")
    print("YouTube channel, click Advanced -> 'Go to <app> (unsafe)' if warned, then Allow.\n")
    creds = flow.run_local_server(
        port=args.port,
        access_type="offline",
        prompt="consent",              # forces a refresh_token even on re-runs
        authorization_prompt_message="Open this URL if the browser does not open:\n{url}",
        success_message="Done. You can close this tab and go back to the terminal.",
    )

    refresh_token = creds.refresh_token
    if not refresh_token:
        raise SystemExit(
            "No refresh token returned. Remove the app's access at "
            "https://myaccount.google.com/permissions and run this script again."
        )

    secrets = {
        "YT_CLIENT_ID": client_id,
        "YT_CLIENT_SECRET": client_secret,
        "YT_REFRESH_TOKEN": refresh_token,
    }
    out = Path("youtube_secrets.json")
    out.write_text(json.dumps(secrets, indent=2), encoding="utf-8")
    out.chmod(0o600)

    print("=" * 72)
    print("SUCCESS. Add these three as GitHub repo secrets")
    print("(Settings -> Secrets and variables -> Actions -> New repository secret):")
    print("=" * 72)
    for key, value in secrets.items():
        print(f"\n{key}\n{value}")
    print("\n" + "=" * 72)
    print(f"Also saved locally to {out.resolve()} - do NOT commit or upload this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
