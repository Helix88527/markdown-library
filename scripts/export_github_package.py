"""Create a GitHub-ready copy from an explicit, auditable whitelist.

The installed skill may sit beside private configuration, real research
materials, model files, state, caches, and locally licensed artwork.  A broad
directory copy is therefore unsafe.  This exporter only copies named root files
and narrowly defined source globs.  It also removes icon references because the
public package deliberately excludes artwork whose redistribution rights have
not been established.

The destination must not already exist.  This makes the command recoverable and
prevents an export refresh from recursively deleting a user-chosen directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT_FILES = (
    "SKILL.md",
    "README.md",
    "CHANGELOG.md",
    "VERSION",
    "LICENSE",
    ".gitignore",
    "config.example.json",
    "requirements.txt",
    "requirements-dev.txt",
    "GITHUB_PUBLISHING.md",
)

GLOBS = (
    "agents/*.yaml",
    "scripts/*.py",
    "references/*.md",
    "templates/*.md",
    "templates/*.json",
    "tests/*.py",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)

# Match concrete home-directory identities, not documentation placeholders such
# as C:\Users\ or /home/<user>/ that are useful in the upload checklist.
SENSITIVE_PATTERNS = {
    "Windows user path": re.compile(r"(?i)[A-Z]:\\Users\\[\w.-]+\\"),
    "macOS user path": re.compile(r"/Users/[\w.-]+/"),
    "Linux home path": re.compile(r"/home/[\w.-]+/"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

TEXT_SUFFIXES = {
    "",
    ".md",
    ".txt",
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".gitignore",
}


class ExportError(RuntimeError):
    """Raised when the whitelist or privacy audit cannot be satisfied."""


def sha256_file(path: Path) -> str:
    """Return a streaming SHA256 digest without loading a whole file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_whitelist(source: Path) -> list[Path]:
    """Resolve every publishable source file and reject missing essentials."""

    selected: set[Path] = set()
    for relative in ROOT_FILES:
        candidate = source / relative
        if not candidate.is_file():
            raise ExportError(f"Required release file is missing: {relative}")
        selected.add(candidate.resolve())

    for pattern in GLOBS:
        for candidate in source.glob(pattern):
            if candidate.is_file() and "__pycache__" not in candidate.parts:
                selected.add(candidate.resolve())

    required_skill = (source / "agents" / "openai.yaml").resolve()
    if required_skill not in selected:
        raise ExportError("agents/openai.yaml is required for a usable skill package.")

    return sorted(selected, key=lambda item: item.relative_to(source).as_posix())


def sanitize_openai_yaml(text: str) -> str:
    """Remove icon paths when the corresponding artwork is intentionally absent."""

    kept = [
        line
        for line in text.splitlines()
        if not re.match(r"^\s*icon_(?:small|large)\s*:", line)
    ]
    return "\n".join(kept).rstrip() + "\n"


def scan_text(relative: Path, text: str) -> list[str]:
    """Return human-readable privacy findings for one exported text file."""

    findings: list[str] = []
    for label, pattern in SENSITIVE_PATTERNS.items():
        match = pattern.search(text)
        if match:
            # Report only the category and location.  Never print a possible
            # credential or user path back into logs.
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{relative.as_posix()}:{line}: {label}")
    return findings


def is_text_file(path: Path) -> bool:
    """Identify whitelist files that should be decoded and privacy-scanned."""

    if path.name == ".gitignore":
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def export_package(source: Path, destination: Path) -> dict[str, object]:
    """Copy the whitelist, scan it, and write a deterministic hash manifest."""

    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise ExportError(f"Skill source does not exist: {source}")
    if destination.exists():
        raise ExportError(
            "Destination already exists; choose a new empty path so no prior files are overwritten."
        )
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ExportError("Destination must be outside the skill source directory.")

    selected = collect_whitelist(source)
    destination.mkdir(parents=True)
    copied: list[dict[str, object]] = []
    privacy_findings: list[str] = []

    for source_file in selected:
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        if is_text_file(source_file):
            text = source_file.read_text(encoding="utf-8-sig")
            if relative.as_posix() == "agents/openai.yaml":
                text = sanitize_openai_yaml(text)
            privacy_findings.extend(scan_text(relative, text))
            target.write_text(text, encoding="utf-8", newline="\n")
        else:
            shutil.copy2(source_file, target)

        copied.append(
            {
                "path": relative.as_posix(),
                "size_bytes": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        )

    if privacy_findings:
        # Leave the bounded export in place for inspection, but do not issue a
        # success manifest that could be mistaken for approval to publish.
        raise ExportError(
            "Privacy scan found possible sensitive content:\n- "
            + "\n- ".join(privacy_findings)
        )

    version = (source / "VERSION").read_text(encoding="utf-8-sig").strip()
    manifest = {
        "package": "markdown-library",
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root_included": False,
        "artwork_included": False,
        "file_count": len(copied),
        "files": copied,
    }
    manifest_path = destination / "EXPORT_MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    """Build the small command-line interface used by maintainers."""

    default_source = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Export an auditable, sanitized GitHub package by whitelist."
    )
    parser.add_argument(
        "destination",
        type=Path,
        help="A new destination directory; existing paths are refused.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=default_source,
        help="Skill source root; defaults to this script's parent skill.",
    )
    parser.add_argument("--json", action="store_true", help="Print the manifest summary as JSON.")
    return parser


def main() -> None:
    """Run the exporter and present a concise, non-sensitive result."""

    parser = build_parser()
    args = parser.parse_args()
    try:
        manifest = export_package(args.source, args.destination)
    except ExportError as error:
        parser.exit(2, f"Error: {error}\n")
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(
            f"Exported {manifest['file_count']} whitelisted files "
            f"for version {manifest['version']} to {args.destination.resolve()}"
        )


if __name__ == "__main__":
    main()
