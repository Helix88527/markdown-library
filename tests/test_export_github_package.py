"""Tests for the publish whitelist and privacy gate."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_github_package.py"
SPEC = importlib.util.spec_from_file_location("export_github_package", SCRIPT)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class ExportGithubPackageTests(unittest.TestCase):
    """Exercise release safety without reading the user's installed skill."""

    def build_source(self, root: Path) -> Path:
        source = root / "source"
        for directory in ("agents", "scripts", "references", "tests", "assets"):
            (source / directory).mkdir(parents=True, exist_ok=True)
        root_files = {
            "SKILL.md": "---\nname: demo\ndescription: demo skill\n---\n",
            "README.md": "# Demo\n",
            "CHANGELOG.md": "# Changes\n",
            "VERSION": "2.2.0\n",
            "LICENSE": "All rights reserved.\n",
            ".gitignore": "config.json\n",
            "config.example.json": "{}\n",
            "requirements.txt": "",
            "requirements-dev.txt": "",
            "GITHUB_PUBLISHING.md": "# Publish\n",
        }
        for name, text in root_files.items():
            (source / name).write_text(text, encoding="utf-8")
        (source / "agents" / "openai.yaml").write_text(
            'interface:\n  display_name: "Demo"\n'
            '  icon_small: "./assets/private.png"\n',
            encoding="utf-8",
        )
        (source / "scripts" / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
        (source / "assets" / "private.png").write_bytes(b"not public")
        return source

    def test_export_excludes_assets_and_removes_icon_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.build_source(root)
            destination = root / "release"

            manifest = exporter.export_package(source, destination)

            self.assertEqual(manifest["version"], "2.2.0")
            self.assertFalse((destination / "assets").exists())
            yaml = (destination / "agents" / "openai.yaml").read_text(encoding="utf-8")
            self.assertNotIn("icon_small", yaml)
            self.assertTrue((destination / "EXPORT_MANIFEST.json").is_file())

    def test_existing_destination_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.build_source(root)
            destination = root / "release"
            destination.mkdir()

            with self.assertRaises(exporter.ExportError):
                exporter.export_package(source, destination)

    def test_concrete_user_path_is_reported_without_echoing_secret(self) -> None:
        findings = exporter.scan_text(
            Path("README.md"),
            "local path C:\\Users\\alice\\private\\file.txt",
        )
        self.assertEqual(findings, ["README.md:1: Windows user path"])


if __name__ == "__main__":
    unittest.main()
