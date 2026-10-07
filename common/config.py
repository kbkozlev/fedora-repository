"""Service manifests: safe metadata without importing downloader dependencies."""
import dataclasses
import json
from pathlib import Path
import re


@dataclasses.dataclass(frozen=True)
class ServiceConfig:
    id: str
    package_names: frozenset
    update_time: str
    dependencies: tuple = ()


def service_ids(directory):
    root = Path(directory)
    return sorted(p.parent.name for p in root.glob("*/service.json") if p.is_file())


def read_config(directory):
    directory = Path(directory)
    data = json.loads((directory / "service.json").read_text())
    service_id = data.get("id", "")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", service_id) or service_id == "all" or directory.name != service_id:
        raise ValueError("Service ID must match its directory and use lowercase letters, digits, _ or -")
    names = data.get("package_names")
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*", n) for n in names):
        raise ValueError("package_names must list the allowed RPM header names")
    schedule = data.get("update_time", "00:00")
    if not isinstance(schedule, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", schedule):
        raise ValueError("update_time must be HH:MM in the container's local timezone")
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*", n) for n in dependencies):
        raise ValueError("dependencies must list Fedora package names")
    if not (directory / "service.py").is_file():
        raise ValueError("Service has no service.py discovery module: " + service_id)
    return ServiceConfig(service_id, frozenset(n.casefold() for n in names), schedule, tuple(dependencies))
