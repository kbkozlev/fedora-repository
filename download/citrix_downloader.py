#!/usr/bin/env python3
import argparse
import hashlib
import os
import sys
import re
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup


PAGE_URL = "https://www.citrix.com/downloads/workspace-app/linux/workspace-app-for-linux-latest.html"


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "download.bin"
    return unquote(name)


def normalize_dl_url(page_url: str, raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/"):
        base = urlparse(page_url)
        return f"{base.scheme}://{base.netloc}{raw}"
    return raw


def container_has_rpm(container) -> bool:
    """
    True if the container looks like the RPM variant:
    - has dl-type "(.rpm)" OR
    - has any ctx-dl-link with ".rpm" in rel/href
    """
    # Check the explicit type label
    dl_type = container.find("span", class_="dl-type")
    if dl_type and ".rpm" in (dl_type.get_text(strip=True) or "").lower():
        return True

    # Fallback: check links
    for a in container.find_all("a", class_="ctx-dl-link"):
        raw = a.get("rel") or a.get("href")
        if isinstance(raw, list):
            raw = " ".join(raw)
        if raw and ".rpm" in raw.lower():
            return True

    return False


def find_rpm_section_by_heading(soup: BeautifulSoup, heading_text: str):
    """
    Finds the ctx-dl-details container that:
      - contains an <h4> with heading_text
      - and is the RPM variant (not DEB)
    """
    candidates = []
    for container in soup.find_all("div", class_="ctx-dl-details"):
        h4 = container.find("h4")
        if not h4:
            continue
        txt = (h4.get_text(strip=True) or "")
        if heading_text in txt:
            candidates.append(container)

    # Prefer the one that is explicitly RPM
    for c in candidates:
        if container_has_rpm(c):
            return c

    # As a fallback, return the first match
    return candidates[0] if candidates else None


def extract_rpm_link_and_sha256(container):
    """
    Extract ONLY .rpm download URL and SHA-256 from a section container.
    """
    chosen_raw = None
    for a in container.find_all("a", class_="ctx-dl-link"):
        raw = a.get("rel") or a.get("href")
        if isinstance(raw, list):
            raw = " ".join(raw)
        if not raw:
            continue
        raw = raw.strip()
        if ".rpm" in raw.lower():
            chosen_raw = raw
            break

    if not chosen_raw:
        return None, None

    text = container.get_text(" ", strip=True)
    m = re.search(r"SHA-256\s*-\s*([0-9a-fA-F]{64})", text)
    expected_sha256 = m.group(1).lower() if m else None

    return chosen_raw, expected_sha256


def download_file(
    url: str,
    out_path: str,
    expected_sha256: str | None,
    timeout: int,
    user_agent: str,
    skip_checksum: bool,
) -> bool:
    # If file exists and checksum matches, skip download
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
        description="Download Citrix Workspace RPMs (ICAClient + add-ons) from the Citrix 'latest' page (RPM-only)."
    )
    parser.add_argument("--url", default=PAGE_URL, help=f"Citrix 'latest' page URL (default: {PAGE_URL})")
    parser.add_argument("--out-dir", default=".", help="Output directory (default: current directory)")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30)")
    parser.add_argument("--no-checksum", action="store_true", help="Skip SHA-256 verification even if checksum is found")
    parser.add_argument(
        "--only",
        choices=["all", "main", "usb", "app-protection"],
        default="all",
        help="What to download (default: all)",
    )
    args = parser.parse_args()

    user_agent = "Mozilla/5.0 (X11; Linux x86_64) citrix-downloader/3.0 (rpm-only)"

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    r = requests.get(args.url, headers=headers, timeout=args.timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    targets = []
    if args.only in ("all", "main"):
        targets.append(("RedHat Full Package", "main"))
    if args.only in ("all", "usb"):
        targets.append(("USB Support Package", "usb"))
    if args.only in ("all", "app-protection"):
        targets.append(("App Protection Package", "app-protection"))

    os.makedirs(args.out_dir, exist_ok=True)

    ok = True
    for heading_text, tag in targets:
        container = find_rpm_section_by_heading(soup, heading_text)
        if not container:
            print(f"ERROR: Could not find a section matching '{heading_text}'.", file=sys.stderr)
            ok = False
            continue

        raw_url, expected_sha256 = extract_rpm_link_and_sha256(container)
        if not raw_url:
            print(
                f"ERROR: Found '{heading_text}' but no RPM download link was found in that section.",
                file=sys.stderr,
            )
            ok = False
            continue

        dl_url = normalize_dl_url(args.url, raw_url)
        filename = pick_filename_from_url(dl_url)
        out_path = os.path.join(args.out_dir, filename)

        print(f"\n=== {heading_text} ({tag}) ===")
        if expected_sha256 and not args.no_checksum:
            print(f"Checksum found: {expected_sha256}")
        else:
            print("Checksum not found (or checksum checking disabled).")

        if not download_file(
            url=dl_url,
            out_path=out_path,
            expected_sha256=expected_sha256,
            timeout=args.timeout,
            user_agent=user_agent,
            skip_checksum=args.no_checksum,
        ):
            ok = False

    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())