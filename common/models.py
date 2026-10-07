"""Stable data contract shared by service downloaders and the engine."""
import dataclasses
import json
from pathlib import Path
import re


class SyncError(Exception):
    pass


@dataclasses.dataclass
class Asset:
    filename: str
    url: str
    names: set
    checksum: str | None = None
    size: int | None = None
    revision: str = ""

    @property
    def source_id(self):
        # Signed URLs expire daily; their query tokens are NOT asset identities.
        return json.dumps([self.filename, self.checksum, self.size, self.revision])


@dataclasses.dataclass
class Package:
    path: Path
    name: str
    arch: str
    evr: tuple
    sha256: str
    fresh: bool = False

    @property
    def public_path(self):
        # Use RPM headers rather than the source filename so migrations and
        # upstream filename changes cannot accumulate checksum decorations.
        epoch, version, release = self.evr
        epoch_prefix = epoch + "_" if epoch != "0" else ""
        filename = f"{self.name}-{epoch_prefix}{version}-{release}.{self.arch}-{self.sha256[:16]}.rpm"
        filename = re.sub(r"[^A-Za-z0-9._+\-]", "_", filename)
        return Path("Packages") / filename

    @property
    def identity(self):
        return (self.name, self.arch, *self.evr)
