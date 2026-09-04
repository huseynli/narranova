from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ContainerDistributionTests(unittest.TestCase):
    def test_image_runs_unprivileged_with_healthcheck_and_ffmpeg(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ffmpeg", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("NARRANOVA_DATA_DIR=/data", dockerfile)
        self.assertIn("/healthz", dockerfile)
        self.assertNotIn("moss-tts-server", dockerfile.lower())

    def test_compose_persists_data_and_keeps_moss_external(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("narranova-data:/data", compose)
        self.assertIn('"8787:8787"', compose)
        self.assertIn("NARRANOVA_DATA_DIR: /data", compose)
        self.assertNotIn("host.docker.internal", compose)
        self.assertNotIn("openmoss:", compose.lower())


if __name__ == "__main__":
    unittest.main()
