#!/usr/bin/env python3
"""Collect allowlisted Debian packages from the latest stable GitHub releases."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class CollectionError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CollectionError("Duplicate JSON key")
        result[key] = value
    return result


def load_config(path):
    config = json.loads(Path(path).read_text(), object_pairs_hook=unique_object)
    if not isinstance(config, dict) or set(config) != {"apps"}:
        raise CollectionError("Configuration must contain only apps")
    apps = config["apps"]
    if not isinstance(apps, list) or not apps:
        raise CollectionError("Allowlist must not be empty")
    repositories, packages = set(), set()
    for app in apps:
        if not isinstance(app, dict) or set(app) != {"repository", "package", "architecture"}:
            raise CollectionError("Unexpected application fields")
        repo, package = app["repository"], app["package"]
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise CollectionError("Invalid repository")
        if not isinstance(package, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", package):
            raise CollectionError("Invalid Debian package")
        if app["architecture"] != "all":
            raise CollectionError("Only Architecture: all is supported")
        if repo.lower() in repositories or package in packages:
            raise CollectionError("Duplicate repository or package")
        repositories.add(repo.lower())
        packages.add(package)
    return apps


def validate_url(url, api=False):
    parsed = urllib.parse.urlsplit(url)
    hosts = {"api.github.com"} if api else {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise CollectionError("Untrusted download URL")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        # Asset requests carry no API credentials, including across redirects.
        if req.has_header("Authorization"):
            raise CollectionError("Authenticated API redirects are not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_bytes(url, token=None, api=False):
    validate_url(url, api=api)
    headers = {"User-Agent": "masternaka-repository", "Accept": "application/vnd.github+json" if api else "application/octet-stream"}
    if token and api:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=60) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as error:
        raise CollectionError(f"Download failed for {urllib.parse.urlsplit(url).hostname}: {error}") from error


def package_identity(path):
    result = subprocess.run(["dpkg-deb", "-f", str(path), "Package", "Architecture", "Version"], check=True, text=True, capture_output=True)
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(": ")
        if separator:
            fields[key] = value
    if set(fields) != {"Package", "Architecture", "Version"}:
        raise CollectionError("Invalid Debian metadata")
    return fields


def collect(config, output, token=None, fetch=request_bytes, identity=package_identity):
    apps = load_config(config)
    output = Path(output)
    if output.exists():
        raise CollectionError("Output directory must not exist")
    output.mkdir(parents=True)
    manifest = []
    try:
        for app in apps:
            repo, package = app["repository"], app["package"]
            release = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest", token=token, api=True))
            if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
                raise CollectionError(f"{repo}: latest stable release unavailable")
            tag = release.get("tag_name")
            assets = release.get("assets")
            if not isinstance(tag, str) or not tag or not isinstance(assets, list):
                raise CollectionError(f"{repo}: malformed release")
            pattern = re.compile(rf"{re.escape(package)}_([^/\\\s]+)_all\.deb")
            candidates = [asset for asset in assets if isinstance(asset, dict) and isinstance(asset.get("name"), str) and pattern.fullmatch(asset["name"])]
            if len(candidates) != 1:
                raise CollectionError(f"{repo}: expected exactly one matching .deb asset")
            asset = candidates[0]
            digest = asset.get("digest")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
                raise CollectionError(f"{repo}: mandatory SHA-256 digest missing")
            url = validate_url(asset.get("browser_download_url", ""))
            # Initial download must refer to this repository's release assets.
            expected_path = f"/{repo}/releases/download/"
            if urllib.parse.urlsplit(url).hostname != "github.com" or not urllib.parse.urlsplit(url).path.startswith(expected_path):
                raise CollectionError(f"{repo}: unexpected asset provenance")
            data = fetch(url, api=False)
            if hashlib.sha256(data).hexdigest() != digest.split(":")[1].lower():
                raise CollectionError(f"{repo}: SHA-256 mismatch")
            path = output / asset["name"]
            path.write_bytes(data)
            metadata = identity(path)
            filename_version = pattern.fullmatch(asset["name"]).group(1)
            if metadata.get("Package") != package or metadata.get("Architecture") != "all" or metadata.get("Version") != filename_version:
                raise CollectionError(f"{repo}: package identity/version mismatch")
            manifest.append({"repository": repo, "tag": tag, "package": package, "architecture": "all", "version": filename_version, "asset": asset["name"], "url": url, "digest": digest.lower()})
        (output / "manifest.json").write_text(json.dumps({"apps": manifest}, indent=2) + "\n")
    except Exception:
        # A failed app must never leave a publishable partial snapshot.
        import shutil
        shutil.rmtree(output)
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="apps.json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    collect(args.config, args.output, token=os.environ.get("GH_TOKEN"))


if __name__ == "__main__":
    main()
