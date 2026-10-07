"""Load only the selected service; the engine contains no application registry."""
import dataclasses
import hashlib
import importlib.util
from pathlib import Path
import sys
from common.config import read_config, service_ids


@dataclasses.dataclass
class Service:
    config: object
    module: object

    def discover(self, session):
        return self.module.discover(session, self.config)


def load_service(directory, name):
    if name not in service_ids(directory):
        raise ValueError("Service is not installed or available: " + name)
    folder = Path(directory) / name
    config = read_config(folder)
    path = folder / "service.py"
    module_id = "rpm_repo_service_" + name + "_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(module_id, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_id] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "discover", None)):
        raise ValueError("service.py must provide discover(session, config): " + name)
    return Service(config, module)
