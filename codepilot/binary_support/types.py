"""Typed results and constants used by the binary/release helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BuildResult:
    """Result of a native binary build (always onedir)."""

    binary_path: Path
    dist_dir: Path
    build_dir: Path
    platform_tag: str
    source_dir: Path


@dataclass
class InstallResult:
    """Result of installing a built binary into the user command path."""

    source_path: Path
    installed_path: Path
    target_dir: Path
    path_registered: bool
    registration_message: str


@dataclass
class ReleaseArtifact:
    """One packaged binary inside a release bundle."""

    platform_tag: str
    source_path: Path
    staged_path: Path
    archive_path: Path
    archive_format: str
    binary_sha256: str
    archive_sha256: str


@dataclass
class ReleaseResult:
    """Result of assembling a release directory."""

    release_dir: Path
    manifest_path: Path
    checksum_path: Path
    guide_path: Path
    summary_path: Path
    ai_guide_path: Path
    ai_manifest_path: Path
    artifacts: list[ReleaseArtifact]


@dataclass
class VerificationResult:
    """Result of verifying a release bundle."""

    release_dir: Path
    manifest_path: Path
    checksum_path: Path
    checked_files: int
    issues: list[str]


VERSION_PATTERN = re.compile(r"^[0-9A-Za-z]+(?:[0-9A-Za-z._-]*[0-9A-Za-z])?$")
