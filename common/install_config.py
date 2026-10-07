"""Plan selective installation and migrate cron without importing vendor code."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import read_config, service_ids


def plan_install(source, installed, requested):
    source, installed = Path(source), Path(installed)
    available = service_ids(source)
    selected = available if requested == ["all"] else sorted(set(requested))
    if not selected or any(name not in available for name in selected):
        raise ValueError("Select available service IDs or --all; available: " + ", ".join(available))
    migrated = []
    old_entry = installed / "rpm_repo_sync.py"
    if old_entry.is_file():
        tree = ast.parse(old_entry.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "APPS" for t in node.targets):
                old_ids = ast.literal_eval(node.value)
                migrated = sorted(name for name in old_ids if name in available and name not in service_ids(installed / "services"))
                break
    deploy = sorted(set(selected + migrated))
    configs = [read_config(source / name) for name in deploy]
    return {"selected": selected, "deploy": deploy, "migrated": migrated,
            "dependencies": sorted({dep for config in configs for dep in config.dependencies}),
            "schedules": {config.id: config.update_time for config in configs if config.id in selected}}


def migrate_cron(previous, schedules):
    selected = set(schedules)
    kept = []
    for line in previous.splitlines():
        match = re.search(r"/root/([a-z][a-z0-9_-]*)/update-\1-repo\.sh(?:\s|$)", line)
        if match and match[1] in selected and not line.lstrip().startswith("#"):
            continue
        kept.append(line)
    for name, schedule in sorted(schedules.items()):
        hour, minute = (int(part) for part in schedule.split(":"))
        kept.append(f"{minute} {hour} * * * /bin/bash /root/{name}/update-{name}-repo.sh >> /var/log/rpm-repo-sync/cron.log 2>&1")
    return "\n".join(kept) + "\n"


def deploy_code(source, target, services):
    """Install common code and selected modules; preserve all other service files."""
    source, target = Path(source), Path(target)
    files = [Path("rpm_repo_sync.py")] + [p.relative_to(source) for p in (source / "common").glob("*.py")]
    for name in services:
        read_config(source / "services" / name)
        files += [Path("services") / name / file for file in ("service.json", "service.py", "update.sh")]
    for relative in files:
        if not (source / relative).is_file():
            raise ValueError("Required source file is missing: " + str(relative))
    for relative in files:
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd, temporary = tempfile.mkstemp(prefix=".install-", dir=dest.parent)
        os.close(fd)
        try:
            shutil.copyfile(source / relative, temporary)
            os.chmod(temporary, 0o755 if dest.suffix == ".sh" else 0o644)
            os.replace(temporary, dest)
        finally:
            Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--source", type=Path, required=True)
    plan.add_argument("--installed", type=Path, required=True)
    plan.add_argument("services", nargs="+")
    cron = commands.add_parser("cron")
    cron.add_argument("plan", type=Path)
    cron.add_argument("previous", type=Path)
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--source", type=Path, required=True)
    deploy.add_argument("--installed", type=Path, required=True)
    deploy.add_argument("services", nargs="+")
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(plan_install(args.source, args.installed, args.services), indent=2))
    elif args.command == "cron":
        data = json.loads(args.plan.read_text())
        print(migrate_cron(args.previous.read_text(), data["schedules"]), end="")
    else:
        deploy_code(args.source, args.installed, args.services)


if __name__ == "__main__":
    main()
