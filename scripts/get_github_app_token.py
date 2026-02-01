"""Generate a GitHub App installation token for Fabric deployments."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

try:
    import jwt
except ImportError as exc:
    raise RuntimeError(
        "PyJWT is required to generate GitHub App tokens. Install it via `pip install PyJWT`."
    ) from exc

if not hasattr(jwt, "encode"):
    raise RuntimeError(
        "The installed 'jwt' package does not expose `encode`. "
        "Make sure PyJWT is installed, not another package named 'jwt'."
    )

PROJECT_ROOT = Path(__file__).parent.parent


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env(PROJECT_ROOT / ".env")



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mint a GitHub App installation token for deployments."
    )
    parser.add_argument(
        "--app-id",
        type=int,
        default=os.environ.get("GITHUB_APP_ID"),
        help="The GitHub App ID (env GITHUB_APP_ID)",
    )
    parser.add_argument(
        "--installation-id",
        type=int,
        default=os.environ.get("GITHUB_APP_INSTALLATION_ID"),
        help="The installation ID for the repo (env GITHUB_APP_INSTALLATION_ID)",
    )
    parser.add_argument(
        "--private-key",
        type=Path,
        default=Path(os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "~/.ssh/optbot-app.pem")).expanduser(),
        help="Path to the GitHub App private key PEM file (env GITHUB_APP_PRIVATE_KEY_PATH)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.app_id or not args.installation_id:
        sys.exit("ERROR: app-id and installation-id are required.")

    if not args.private_key.is_file():
        sys.exit(f"ERROR: private key file not found at {args.private_key}")

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": args.app_id,
    }

    key = args.private_key.read_text()
    token = jwt.encode(payload, key, algorithm="RS256")

    resp = requests.post(
        f"https://api.github.com/app/installations/{args.installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    print(resp.json()["token"])


if __name__ == "__main__":
    main()
