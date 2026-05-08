"""Fetch, verify, and cache platform vendor packages from npm."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

NPM_REGISTRY_URL = "https://registry.npmjs.org"
_CHUNK_SIZE = 1024 * 1024


class VendorFetcherError(RuntimeError):
    """Base error for vendor fetch failures."""


class UnsupportedPlatformError(VendorFetcherError):
    """Raised when the current platform cannot be mapped to a vendor package."""


class PackageMetadataError(VendorFetcherError):
    """Raised when npm metadata is missing required package fields."""


class ChecksumMismatchError(VendorFetcherError):
    """Raised when downloaded or cached content fails checksum validation."""


@dataclass(frozen=True)
class VendorPlatform:
    system: str
    machine: str
    npm_platform: str
    npm_arch: str


@dataclass(frozen=True)
class VendorDownloadInfo:
    vendor: str
    root_package: str
    package_name: str
    version: str
    tarball_url: str
    integrity: str | None = None
    shasum: str | None = None


@dataclass(frozen=True)
class DownloadResponse:
    status: int
    headers: Mapping[str, str]
    chunks: Iterable[bytes]


@dataclass(frozen=True)
class VendorFetchResult:
    info: VendorDownloadInfo
    cache_path: Path
    from_cache: bool
    checksum_verified: bool
    resumed: bool


JsonFetcher = Callable[[str], Mapping[str, object]]
Downloader = Callable[[str, dict[str, str]], DownloadResponse]


def normalize_vendor_platform(system: str | None = None, machine: str | None = None) -> VendorPlatform:
    raw_system = system or platform.system()
    raw_machine = machine or platform.machine()
    system_key = raw_system.lower()
    machine_key = raw_machine.lower()

    platform_aliases = {
        "windows": "win32",
        "win32": "win32",
        "darwin": "darwin",
        "macos": "darwin",
        "linux": "linux",
    }
    arch_aliases = {
        "amd64": "x64",
        "x86_64": "x64",
        "x64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    npm_platform = platform_aliases.get(system_key)
    npm_arch = arch_aliases.get(machine_key)
    if not npm_platform or not npm_arch:
        raise UnsupportedPlatformError(f"不支持的平台或架构: {raw_system}-{raw_machine}")
    return VendorPlatform(
        system=raw_system,
        machine=raw_machine,
        npm_platform=npm_platform,
        npm_arch=npm_arch,
    )


def root_package_name(vendor: str) -> str:
    vendor_key = vendor.lower()
    if vendor_key == "codex":
        return "@openai/codex"
    if vendor_key == "opencode":
        return "@opencode/opencode"
    raise ValueError(f"未知 vendor: {vendor}")


def platform_package_name(vendor: str, *, system: str | None = None, machine: str | None = None) -> str:
    resolved = normalize_vendor_platform(system=system, machine=machine)
    vendor_key = vendor.lower()
    if vendor_key == "codex":
        return f"@openai/codex-{resolved.npm_platform}-{resolved.npm_arch}"
    if vendor_key == "opencode":
        return f"@opencode/opencode-{resolved.npm_platform}-{resolved.npm_arch}"
    raise ValueError(f"未知 vendor: {vendor}")


def resolve_download_info(
    vendor: str,
    *,
    version: str | None = None,
    system: str | None = None,
    machine: str | None = None,
    registry_url: str = NPM_REGISTRY_URL,
    json_fetcher: JsonFetcher | None = None,
) -> VendorDownloadInfo:
    """Resolve the platform npm package tarball and checksum metadata."""
    fetch_json = json_fetcher or default_json_fetcher
    root_name = root_package_name(vendor)
    package_name = platform_package_name(vendor, system=system, machine=machine)
    root_metadata = fetch_json(npm_package_metadata_url(root_name, registry_url=registry_url))
    root_version = _select_version(root_metadata, version)
    root_payload = _version_payload(root_metadata, root_version, root_name)
    platform_version = _dependency_version(root_payload, package_name) or root_version

    package_metadata = fetch_json(npm_package_metadata_url(package_name, registry_url=registry_url))
    package_version = _select_version(package_metadata, platform_version)
    package_payload = _version_payload(package_metadata, package_version, package_name)
    dist = package_payload.get("dist")
    if not isinstance(dist, Mapping) or not dist.get("tarball"):
        raise PackageMetadataError(f"{package_name}@{package_version} 缺少 dist.tarball")

    return VendorDownloadInfo(
        vendor=vendor.lower(),
        root_package=root_name,
        package_name=package_name,
        version=package_version,
        tarball_url=str(dist["tarball"]),
        integrity=str(dist["integrity"]) if dist.get("integrity") else None,
        shasum=str(dist["shasum"]) if dist.get("shasum") else None,
    )


def fetch_vendor_binary(
    info: VendorDownloadInfo,
    *,
    cache_dir: str | Path,
    downloader: Downloader | None = None,
) -> VendorFetchResult:
    """Download a vendor tarball into cache, validating cached and fetched files."""
    cache_path = cache_path_for(info, cache_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        if verify_file(cache_path, info):
            return VendorFetchResult(info=info, cache_path=cache_path, from_cache=True, checksum_verified=True, resumed=False)
        cache_path.unlink()

    tmp_path = _tmp_path_for(cache_path)
    headers: dict[str, str] = {}
    partial_size = tmp_path.stat().st_size if tmp_path.exists() else 0
    if partial_size > 0:
        headers["Range"] = f"bytes={partial_size}-"

    download = (downloader or default_downloader)(info.tarball_url, headers)
    if download.status < 200 or download.status >= 300:
        raise VendorFetcherError(f"下载失败: HTTP {download.status} {info.tarball_url}")

    resumed = partial_size > 0 and download.status == 206
    if partial_size > 0 and download.status != 206:
        partial_size = 0
    mode = "ab" if partial_size > 0 else "wb"
    try:
        with tmp_path.open(mode) as handle:
            for chunk in download.chunks:
                if chunk:
                    handle.write(chunk)
        if not verify_file(tmp_path, info):
            tmp_path.unlink(missing_ok=True)
            raise ChecksumMismatchError(f"{info.package_name}@{info.version} 校验和不匹配")
        os.replace(tmp_path, cache_path)
    except Exception:
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            tmp_path.unlink(missing_ok=True)
        raise

    return VendorFetchResult(info=info, cache_path=cache_path, from_cache=False, checksum_verified=True, resumed=resumed)


def cache_path_for(info: VendorDownloadInfo, cache_dir: str | Path) -> Path:
    parsed = urlparse(info.tarball_url)
    filename = Path(unquote(parsed.path)).name or f"{_safe_segment(info.package_name)}-{info.version}.tgz"
    return Path(cache_dir).expanduser().resolve() / info.vendor / _safe_segment(info.package_name) / info.version / filename


def verify_file(path: str | Path, info: VendorDownloadInfo) -> bool:
    target = Path(path)
    if not target.exists() or not target.is_file():
        return False
    if info.integrity:
        return _verify_integrity(target, info.integrity)
    if info.shasum:
        return _hash_file(target, "sha1") == info.shasum.lower()
    return True


def npm_package_metadata_url(package_name: str, *, registry_url: str = NPM_REGISTRY_URL) -> str:
    return f"{registry_url.rstrip('/')}/{quote(package_name, safe='@')}"


def default_json_fetcher(url: str) -> Mapping[str, object]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise PackageMetadataError(f"读取 npm 元数据失败: HTTP {exc.code} {url}") from exc


def default_downloader(url: str, headers: dict[str, str]) -> DownloadResponse:
    request = Request(url, headers=headers)
    try:
        response = urlopen(request, timeout=60)
    except HTTPError as exc:
        return DownloadResponse(status=exc.code, headers=dict(exc.headers.items()), chunks=[])

    def _chunks() -> Iterable[bytes]:
        with response:
            while True:
                chunk = response.read(_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    return DownloadResponse(status=response.status, headers=dict(response.headers.items()), chunks=_chunks())


def _select_version(metadata: Mapping[str, object], requested: str | None) -> str:
    versions = metadata.get("versions")
    if not isinstance(versions, Mapping) or not versions:
        raise PackageMetadataError("npm 元数据缺少 versions")

    normalized = _normalize_version_spec(requested)
    if normalized:
        if normalized not in versions:
            raise PackageMetadataError(f"npm 元数据缺少版本: {normalized}")
        return normalized

    dist_tags = metadata.get("dist-tags")
    latest = dist_tags.get("latest") if isinstance(dist_tags, Mapping) else None
    if isinstance(latest, str) and latest in versions:
        return latest
    return str(next(iter(versions)))


def _version_payload(metadata: Mapping[str, object], version: str, package_name: str) -> Mapping[str, object]:
    versions = metadata.get("versions")
    if not isinstance(versions, Mapping):
        raise PackageMetadataError(f"{package_name} 缺少 versions")
    payload = versions.get(version)
    if not isinstance(payload, Mapping):
        raise PackageMetadataError(f"{package_name}@{version} 缺少版本元数据")
    return payload


def _dependency_version(payload: Mapping[str, object], package_name: str) -> str | None:
    for key in ("optionalDependencies", "dependencies"):
        deps = payload.get(key)
        if not isinstance(deps, Mapping):
            continue
        raw = deps.get(package_name)
        if isinstance(raw, str):
            return _normalize_version_spec(raw)
    return None


def _normalize_version_spec(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value or value == "*":
        return None
    match = re.match(r"^[~^=v\s]*([0-9][0-9A-Za-z.+-]*)$", value)
    return match.group(1) if match else value


def _verify_integrity(path: Path, integrity: str) -> bool:
    for token in integrity.split():
        if "-" not in token:
            continue
        algorithm, encoded_digest = token.split("-", 1)
        algorithm = algorithm.lower()
        if algorithm not in hashlib.algorithms_available:
            continue
        try:
            expected = base64.b64decode(encoded_digest)
        except Exception:
            continue
        digest = hashlib.new(algorithm)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        actual = digest.digest()
        if actual == expected:
            return True
    return False


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_segment(value: str) -> str:
    return value.replace("@", "").replace("/", "_").replace("\\", "_")


def _tmp_path_for(cache_path: Path) -> Path:
    if cache_path.suffix:
        return cache_path.with_suffix(cache_path.suffix + ".tmp")
    return cache_path.with_name(cache_path.name + ".tmp")
