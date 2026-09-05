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
        self.assertIn("COPY pyproject.toml README.md LICENSE ./", dockerfile)
        self.assertNotIn("moss-tts-server", dockerfile.lower())

    def test_compose_uses_published_image_and_keeps_moss_external(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("ghcr.io/huseynli/narranova:latest", compose)
        self.assertNotIn("build:", compose)
        self.assertIn("narranova-data:/data", compose)
        self.assertIn('"8787:8787"', compose)
        self.assertIn("NARRANOVA_DATA_DIR: /data", compose)
        self.assertIn("host.docker.internal:host-gateway", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertNotIn("openmoss:", compose.lower())

    def test_development_compose_builds_local_image(self) -> None:
        compose = (ROOT / "compose.dev.yaml").read_text(encoding="utf-8")

        self.assertIn("narranova:local", compose)
        self.assertIn("build:", compose)
        self.assertNotIn("ports:", compose)
        self.assertNotIn("volumes:", compose)


if __name__ == "__main__":
    unittest.main()
