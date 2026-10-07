"""Optional timezone configuration; tests never modify the real server timezone."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TimezoneTests(unittest.TestCase):
    def configure(self, zone, timedated_status=0):
        script = '''set -euo pipefail
source "$1"
timedatectl() { printf 'timedatectl'; printf ' <%s>' "$@"; printf '\\n'; return "$3_status"; }
ln() { printf 'ln'; printf ' <%s>' "$@"; printf '\\n'; }
configure_timezone "$2"
'''.replace('"$3_status"', str(timedated_status))
        return subprocess.run(["bash", "-c", script, "timezone-test", str(ROOT / "common/timezone.sh"), zone], capture_output=True, text=True)

    def test_default_preserves_timezone_without_calling_system_tools(self):
        result = self.configure("")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_explicit_timezone_is_applied(self):
        result = self.configure("UTC")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "timedatectl <set-timezone> <UTC>\n")

    def test_minimal_container_fallback_only_runs_when_requested(self):
        result = self.configure("UTC", timedated_status=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ln <-sfn> </usr/share/zoneinfo/UTC> </etc/localtime>", result.stdout)

    def test_invalid_timezone_is_rejected_without_system_changes(self):
        result = self.configure("../etc/passwd")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Invalid or unavailable timezone", result.stderr)
