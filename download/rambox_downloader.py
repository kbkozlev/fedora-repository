#!/usr/bin/env python3
import argparse
import hashlib
import os
import sys

import requests


REPO = "ramboxapp/download"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def find_rpm_asset(release: dict) -> dict | None:
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".rpm"):
            return asset
    return None


def extract_expected_sha256(asset: dict) -> str | None:
    digest = asset.get("digest", "") or ""
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].strip().lower()
    return None


def download_file(
    url: str,
    out_path: str,
    expected_sha256: str | None,
    timeout: int,
    user_agent: str,
    skip_checksum: bool,
) -> bool:
    if os.path.exists(out_path) and expected_sha256 and not skip_checksum:
        actual = sha256_file(out_path)
        if actual.lower() == expected_sha256.lower():
            print(f"Already present and checksum OK, skipping:\n  {out_path}")
            return True

    print(f"Downloading:\n  {url}\nTo:\n  {out_path}")

    with requests.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=timeout) as resp:
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

    if expected_sha256 and not skip_checksum:
        print(f"Verifying SHA-256...\n  Expected: {expected_sha256}")
        actual = sha256_file(out_path)
        print(f"  Actual:   {actual}")
        if actual.lower() != expected_sha256.lower():
            print("ERROR: SHA-256 mismatch!", file=sys.stderr)
            return False
        print("SHA-256 OK.")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download the latest Rambox RPM from GitHub releases."
    )
    parser.add_argument("--repo", default=REPO, help=f"GitHub repository in owner/name format (default: {REPO})")
    parser.add_argument("--out-dir", default=".", help="Output directory (default: current directory)")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30)")
    parser.add_argument("--no-checksum", action="store_true", help="Skip SHA-256 verification even if checksum is found")
    args = parser.parse_args()

    api_url = f"https://api.github.com/repos/{args.repo}/releases/latest"
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) rambox-downloader/1.0"

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
    }

    os.makedirs(args.out_dir, exist_ok=True)

    try:
        response = requests.get(api_url, headers=headers, timeout=args.timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch latest release metadata: {e}", file=sys.stderr)
        return 2

    release = response.json()
    tag = release.get("tag_name", "unknown")
    rpm_asset = find_rpm_asset(release)

    if not rpm_asset:
        print("ERROR: No RPM asset found in the latest release.", file=sys.stderr)
        return 3

    filename = rpm_asset["name"]
    url = rpm_asset["browser_download_url"]
    out_path = os.path.join(args.out_dir, filename)
    expected_sha256 = extract_expected_sha256(rpm_asset)

    print(f"Latest release: {tag}")
    print(f"Selected asset: {filename}")
    if expected_sha256 and not args.no_checksum:
        print(f"Checksum found: {expected_sha256}")
    else:
        print("Checksum not found (or checksum checking disabled).")

    ok = download_file(
        url=url,
        out_path=out_path,
        expected_sha256=expected_sha256,
        timeout=args.timeout,
        user_agent=user_agent,
        skip_checksum=args.no_checksum,
    )

    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())