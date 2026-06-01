"""Fetch, verify, and cache platform vendor packages from npm."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

NPM_REGISTRY_URL = "https://registry.npmjs.org"
_CHUNK_SIZE = 1024 * 1024
SUPPORTED_BUNDLED_CLI_PROVIDERS = ("opencode", "codex")
BUNDLED_VENDOR_MANIFEST = "manifest.json"


class VendorFetcherError(RuntimeError):
    """Base error for vendor fetch failures.
    同时兼容 ``except RuntimeError`` 和 ``except CodePilotError``。
    """
    pass


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


@dataclass(frozen=True)
class BundledVendorEntry:
    provider: str
    version: str
    platform: str
    arch: str
    path: str
    checksum: str
    package_name: str
    root_package: str


@dataclass(frozen=True)
class BundledVendorResult:
    vendor_dir: Path
    manifest_path: Path
    providers: list[BundledVendorEntry]


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
        return "opencode-ai"
    raise ValueError(f"未知 vendor: {vendor}")


def platform_package_name(vendor: str, *, system: str | None = None, machine: str | None = None) -> str:
    resolved = normalize_vendor_platform(system=system, machine=machine)
    vendor_key = vendor.lower()
    if vendor_key == "codex":
        return f"@openai/codex-{resolved.npm_platform}-{resolved.npm_arch}"
    if vendor_key == "opencode":
        npm_platform = "windows" if resolved.npm_platform == "win32" else resolved.npm_platform
        return f"opencode-{npm_platform}-{resolved.npm_arch}"
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

    try:
        package_metadata = fetch_json(npm_package_metadata_url(package_name, registry_url=registry_url))
    except PackageMetadataError:
        if vendor.lower() != "codex":
            raise
        package_metadata = root_metadata
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


def parse_bundle_cli_list(raw: str | None) -> list[str]:
    """Parse a comma-separated bundled CLI provider list."""
    if not raw:
        return []

    providers: list[str] = []
    seen: set[str] = set()
    unsupported: list[str] = []
    for item in raw.split(","):
        provider = item.strip().lower()
        if not provider:
            continue
        if provider not in SUPPORTED_BUNDLED_CLI_PROVIDERS:
            unsupported.append(provider)
            continue
        if provider not in seen:
            providers.append(provider)
            seen.add(provider)

    if unsupported:
        allowed = ", ".join(SUPPORTED_BUNDLED_CLI_PROVIDERS)
        raise ValueError(f"不支持的 --bundle-cli provider: {', '.join(unsupported)}。仅支持: {allowed}")
    return providers


def bundle_vendor_clis(
    providers: Iterable[str],
    *,
    output_dir: str | Path,
    platform_tag: str,
    cache_dir: str | Path,
    json_fetcher: JsonFetcher | None = None,
    downloader: Downloader | None = None,
) -> BundledVendorResult:
    """Fetch platform vendor packages and write their binaries under ``bin/vendor``."""
    normalized = parse_bundle_cli_list(",".join(providers))
    target_root = Path(output_dir).expanduser().resolve()
    vendor_dir = target_root / "bin" / "vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    platform_info = normalize_vendor_platform()
    entries: list[BundledVendorEntry] = []

    for provider in normalized:
        info = resolve_download_info(provider, json_fetcher=json_fetcher)
        fetched = fetch_vendor_binary(info, cache_dir=cache_dir, downloader=downloader)
        executable_path = extract_vendor_binary(fetched.cache_path, provider=provider, vendor_dir=vendor_dir, platform_tag=platform_tag)
        entries.append(
            BundledVendorEntry(
                provider=provider,
                version=info.version,
                platform=platform_info.npm_platform,
                arch=platform_info.npm_arch,
                path=_relative_posix(executable_path, target_root),
                checksum=_hash_file(executable_path, "sha256"),
                package_name=info.package_name,
                root_package=info.root_package,
            )
        )

    manifest_path = vendor_dir / BUNDLED_VENDOR_MANIFEST
    manifest_payload = {
        "schema": "codepilot-bundled-cli-v1",
        "platform_tag": platform_tag,
        "providers": [entry.__dict__ for entry in entries],
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return BundledVendorResult(vendor_dir=vendor_dir, manifest_path=manifest_path, providers=entries)


def update_installed_vendor_clis(
    providers: Iterable[str],
    *,
    target_dir: str | Path,
    cache_dir: str | Path,
    json_fetcher: JsonFetcher | None = None,
    downloader: Downloader | None = None,
) -> BundledVendorResult:
    """Refresh installed vendor CLI binaries under an install ``vendor`` dir."""
    normalized = parse_bundle_cli_list(",".join(providers))
    install_dir = Path(target_dir).expanduser().resolve()
    vendor_dir = install_dir / "vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    platform_info = normalize_vendor_platform()
    entries: list[BundledVendorEntry] = []

    for provider in normalized:
        info = resolve_download_info(provider, json_fetcher=json_fetcher)
        fetched = fetch_vendor_binary(info, cache_dir=cache_dir, downloader=downloader)
        executable_path = extract_vendor_binary(fetched.cache_path, provider=provider, vendor_dir=vendor_dir)
        entries.append(
            BundledVendorEntry(
                provider=provider,
                version=info.version,
                platform=platform_info.npm_platform,
                arch=platform_info.npm_arch,
                path=_relative_posix(executable_path, install_dir),
                checksum=_hash_file(executable_path, "sha256"),
                package_name=info.package_name,
                root_package=info.root_package,
            )
        )

    manifest_path = vendor_dir / BUNDLED_VENDOR_MANIFEST
    manifest_payload = {
        "schema": "codepilot-installed-cli-v1",
        "providers": [entry.__dict__ for entry in entries],
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return BundledVendorResult(vendor_dir=vendor_dir, manifest_path=manifest_path, providers=entries)


def extract_vendor_binary(
    archive_path: str | Path,
    *,
    provider: str,
    vendor_dir: str | Path,
    platform_tag: str | None = None,
) -> Path:
    """Extract the provider executable from an npm platform tarball."""
    provider_key = provider.lower()
    if provider_key not in SUPPORTED_BUNDLED_CLI_PROVIDERS:
        allowed = ", ".join(SUPPORTED_BUNDLED_CLI_PROVIDERS)
        raise ValueError(f"不支持的 --bundle-cli provider: {provider}。仅支持: {allowed}")

    target_dir = Path(vendor_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = Path(archive_path).expanduser().resolve()
    try:
        with tarfile.open(archive, "r:*") as bundle:
            member = _select_vendor_member(bundle.getmembers(), provider_key)
            if member is None:
                raise VendorFetcherError(f"{archive.name} 中没有找到 {provider_key} 可执行文件")
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise VendorFetcherError(f"{archive.name} 中无法读取 {member.name}")
            target_name = _target_vendor_name(provider_key, member.name, platform_tag)
            target_path = target_dir / target_name
            with extracted, target_path.open("wb") as handle:
                shutil.copyfileobj(extracted, handle)
    except tarfile.TarError as exc:
        raise VendorFetcherError(f"无法解包 vendor tarball: {archive}") from exc

    mode = target_path.stat().st_mode
    if not _is_windows_platform(platform_tag):
        target_path.chmod(mode | 0o755)
    return target_path


def _find_bundled_vendor_dir(source_binary: Path) -> Path | None:
    """Locate the vendor directory next to a onefile or onedir build artifact.

    For onefile ``<dist>/codepilot.exe`` the vendor sits at ``<dist>/bin/vendor``.
    For onedir  ``<dist>/codepilot/codepilot.exe`` it is one level up:
    ``<dist>/bin/vendor``.
    """
    candidates = [
        source_binary.parent / "bin" / "vendor",           # onefile or onedir sibling
        source_binary.parent.parent / "bin" / "vendor",    # onedir (exe is one level deeper)
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def install_bundled_vendor(source_binary: str | Path, *, target_dir: str | Path) -> list[Path]:
    """Copy bundled vendor files adjacent to a built binary into the install bin directory."""
    source = Path(source_binary).expanduser().resolve()
    source_vendor_dir = _find_bundled_vendor_dir(source)
    if source_vendor_dir is None:
        return []

    destination = Path(target_dir).expanduser().resolve() / "vendor"
    copied: list[Path] = []
    for item in source_vendor_dir.rglob("*"):
        if item.is_dir():
            continue
        relative = item.relative_to(source_vendor_dir)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        if item.name != BUNDLED_VENDOR_MANIFEST and not _is_windows_platform():
            target.chmod(target.stat().st_mode | 0o755)
        copied.append(target.resolve())
    return copied


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
    if value.startswith("npm:") and "@" in value[4:]:
        return value.rsplit("@", 1)[1]
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


def _select_vendor_member(members: Iterable[tarfile.TarInfo], provider: str) -> tarfile.TarInfo | None:
    candidates: list[tuple[int, tarfile.TarInfo]] = []
    names = {provider, f"{provider}.exe"}
    for member in members:
        if not member.isfile():
            continue
        normalized = member.name.replace("\\", "/").lower()
        basename = Path(normalized).name
        if basename not in names:
            continue
        score = 0
        if "/bin/" in normalized:
            score += 10
        if basename == provider or basename == f"{provider}.exe":
            score += 5
        candidates.append((score, member))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def _target_vendor_name(provider: str, member_name: str, platform_tag: str | None) -> str:
    basename = Path(member_name.replace("\\", "/")).name
    if basename.lower().endswith(".exe") or _is_windows_platform(platform_tag):
        return f"{provider}.exe"
    return provider


def _is_windows_platform(platform_tag: str | None = None) -> bool:
    if platform_tag:
        return platform_tag.lower().startswith(("windows", "win32"))
    return platform.system().lower() == "windows"


def _relative_posix(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
