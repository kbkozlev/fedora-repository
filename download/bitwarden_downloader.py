#!/usr/bin/env python3
import argparse
import os
import sys
from urllib.parse import urlparse, unquote

import requests


DOWNLOAD_URL = "https://bitwarden.com/download/?app=desktop&platform=linux&variant=rpm"


def pick_filename_from_response(resp: requests.Response) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    if "filename=" in cd:
        filename = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
        if filename:
            return filename

    parsed = urlparse(resp.url)
    name = os.path.basename(parsed.path)
    if name:
        return unquote(name)

    return "bitwarden-latest.rpm"


def download_file(url: str, out_path: str, timeout: int, user_agent: str) -> bool:
    print(f"Downloading:\n  {url}\nTo:\n  {out_path}")

    with requests.get(
        url,
        headers={"User-Agent": user_agent},
        stream=True,
        timeout=timeout,
        allow_redirects=True,
    ) as resp:
        resp.raise_for_status()

        total = int(resp.headers.get("Content-Length", "0") or "0")
        downloaded = 0

        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100 / total
                    print(f"\r  {downloaded}/{total} bytes ({pct:.1f}%)", end="", flush=True)

    if total > 0:
        print()

    return True


def resolve_final_filename(url: str, timeout: int, user_agent: str) -> tuple[str, str]:
    with requests.get(
        url,
        headers={"User-Agent": user_agent},
        stream=True,
        timeout=timeout,
        allow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        filename = pick_filename_from_response(resp)
        final_url = resp.url

    return filename, final_url


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download the latest Bitwarden Linux RPM."
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Output directory (default: current directory)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--filename",
        default=None,
        help="Optional output filename override",
    )

    args = parser.parse_args()

    user_agent = "Mozilla/5.0 (X11; Linux x86_64) bitwarden-downloader/1.0"

    os.makedirs(args.out_dir, exist_ok=True)

    try:
        resolved_name, final_url = resolve_final_filename(DOWNLOAD_URL, args.timeout, user_agent)
    except requests.RequestException as e:
        print(f"ERROR: Failed to resolve Bitwarden RPM URL: {e}", file=sys.stderr)
        return 2

    filename = args.filename or resolved_name
    out_path = os.path.join(args.out_dir, filename)

    if os.path.exists(out_path):
        print(f"Already present, skipping:\n  {out_path}")
        return 0

    try:
        ok = download_file(DOWNLOAD_URL, out_path, args.timeout, user_agent)
    except requests.RequestException as e:
        print(f"ERROR: Download failed: {e}", file=sys.stderr)
        return 3

    if ok:
        print(f"Saved as: {out_path}")
        print(f"Final URL: {final_url}")
        return 0

    return 5


if __name__ == "__main__":
    raise SystemExit(main())