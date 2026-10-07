"""CLI for one service, or every service in the installed services directory."""
import argparse
import fcntl
import logging
from pathlib import Path
import re
import shutil
import sys
import time
from common.config import read_config, service_ids
from common.http import clean_url
from common.registry import load_service

LOG = logging.getLogger("rpm-repo-sync")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mirror RPMs using separate service downloaders and a shared engine.")
    parser.add_argument("--repo", default="all", help="Service ID, or all installed services")
    parser.add_argument("--list", action="store_true", help="List available service IDs without running downloads")
    parser.add_argument("--services-dir", type=Path, default=Path(__file__).resolve().parents[1] / "services")
    parser.add_argument("--web-root", type=Path, default=Path("/var/www/html"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/rpm-repo-sync"))
    parser.add_argument("--log-dir", type=Path, default=Path("/var/log/rpm-repo-sync"))
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="Validate in staging without publishing or pruning")
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    available = service_ids(args.services_dir)
    if args.list:
        for name in available:
            config = read_config(args.services_dir / name)
            print(f"{name}\t{config.update_time}\t{', '.join(sorted(config.package_names))}")
        return 0
    selected = available if args.repo == "all" else [args.repo]
    if not selected or any(name not in available for name in selected):
        parser.error("No matching installed service; use --list to see available IDs")
    for command in ("rpmkeys", "createrepo_c"):
        if not shutil.which(command):
            parser.error("Missing command: " + command)
    formatter = logging.Formatter("%(asctime)s UTC %(levelname)s %(message)s")
    formatter.converter = time.gmtime
    LOG.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    args.log_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    failed = False
    try:
        # Shared lock permits independent service jobs, but prevents deployment
        # of half-updated shared code while this process is using it.
        with (args.state_dir / "install.lock").open("a") as install_lock:
            fcntl.flock(install_lock, fcntl.LOCK_SH)
            from common import engine
            for name in selected:
                file_handler = logging.FileHandler(args.log_dir / (name + ".log"))
                file_handler.setFormatter(formatter)
                LOG.addHandler(file_handler)
                try:
                    LOG.info("Starting %s repository update", name)
                    service = load_service(args.services_dir, name)
                    engine.sync(service, args.web_root, args.state_dir, args.keep, args.dry_run)
                    LOG.info("Completed %s repository update", name)
                except Exception as exc:
                    failed = True
                    message = re.sub(r"https?://[^\s]+", lambda m: clean_url(m[0]), str(exc))
                    LOG.error("%s update failed: %s", name, message)
                finally:
                    LOG.removeHandler(file_handler)
                    file_handler.close()
    finally:
        LOG.removeHandler(console)
        console.close()
    return int(failed)
