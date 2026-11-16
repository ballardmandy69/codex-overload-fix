"""Endpoint switching regressions. @author lyt @date 2026-11-16"""

import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "switch_config", Path(__file__).resolve().parents[1] / "scripts" / "switch_config.py")
SWITCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SWITCH)


class SwitchConfigTest(unittest.TestCase):
    """Verify scope, rejection and recoverability. @author lyt @date 2026-11-16"""

    def fixture(self):
        return (b'\xef\xbb\xbfmodel_provider = "openai-retry"\r\n'
                b'[model_providers.custom]\r\nbase_url = "http://127.0.0.1:8787"\r\n'
                b'[model_providers.openai-retry]\r\nname = "local retry"\r\n'
                b'base_url = "http://127.0.0.1:8787" # preserve\r\n'
                b'requires_openai_auth = true\r\n[other]\r\nvalue = "untouched"\r\n')

    def test_round_trip_preserves_other_provider_bom_comments_and_newlines(self):
        original = self.fixture()
        direct = SWITCH.endpoint_update(original, "direct", 8787)
        self.assertIn(b'base_url = "http://127.0.0.1:8787"', direct)
        self.assertIn(SWITCH.DIRECT.encode(), direct)
        self.assertEqual(SWITCH.endpoint_update(direct, "relay", 8787), original)

    def test_rejects_unknown_active_provider_endpoint_and_duplicate_section(self):
        for content in (
            self.fixture().replace(b'model_provider = "openai-retry"', b'model_provider = "custom"'),
            self.fixture().replace(b'8787" #', b'9999" #'),
            self.fixture() + b'[model_providers.openai-retry]\r\n',
            self.fixture().replace(b'# preserve', b'\r\nbase_url = "duplicate"'),
        ):
            with self.subTest(content=content), self.assertRaises(ValueError):
                SWITCH.endpoint_update(content, "direct", 8787)

    def test_dry_run_backup_atomic_write_and_idempotency(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / "config.toml"
            path.write_bytes(self.fixture())
            SWITCH.switch_config(path, "direct", 8787, check=True)
            self.assertEqual(path.read_bytes(), self.fixture())
            self.assertEqual(len(list(path.parent.iterdir())), 1)
            SWITCH.switch_config(path, "direct", 8787)
            backups = list(path.parent.glob("*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), self.fixture())
            SWITCH.switch_config(path, "direct", 8787)
            self.assertEqual(len(list(path.parent.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
