"""Render download links and update an existing, validated GitHub release page."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def render(repository: str, version: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected a GitHub owner/repository")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Expected a numeric X.Y.Z version")
    template = (ROOT / "packaging/RELEASE_NOTES.md").read_text(encoding="utf-8")
    return template.replace("{{VERSION}}", version).replace("{{REPOSITORY}}", repository)


def installer_names(version: str) -> set[str]:
    return {f"VirtualKVM-{version}-windows-x64-setup.exe", *(
        f"VirtualKVM-{version}-macos-{architecture}.{extension}"
        for architecture in ("arm64", "x86_64") for extension in ("pkg", "dmg"))}


def validate_publication(version, assets, report, checksums, source):
    expected = installer_names(version)
    names = {asset["name"] for asset in assets}
    if not expected.union({"SHA256SUMS", "installation-checks.json"}) <= names:
        raise ValueError("The release is missing installers or validation metadata")
    if report.get("commit") != source:
        raise ValueError("Installation reports do not match the release tag's source commit")
    targets = report.get("targets", {})
    if set(targets) != {f"VirtualKVM-installer-{target}" for target in
                       ("windows-x64", "macos-arm64", "macos-x86_64")}:
        raise ValueError("All three native installation reports are required")
    for target, checks in targets.items():
        required = {"install", "offline_media", "interface", "upgrade", "user_state_preserved"}
        required |= {"uninstall"} if target.endswith("windows-x64") else {"dmg_verified", "dmg_layout"}
        if not required <= set(checks) or not all(value is True for value in checks.values()):
            raise ValueError(f"Native installation checks did not pass: {target}")
    hashed = []
    for line in checksums.splitlines():
        if not (match := re.fullmatch(r"[0-9a-f]{64}  ([^/\\]+)", line)):
            raise ValueError("Malformed installer checksum manifest")
        hashed.append(match[1])
    if set(hashed) != expected or len(hashed) != len(expected):
        raise ValueError("Checksum manifest must list each of the five installers exactly once")


def update_existing(repository, version, notes):
    tag = f"v{version}"
    def gh(*args):
        return subprocess.run(["gh", "release", *args, "--repo", repository],
                              check=True, capture_output=True, text=True)
    try:
        release = json.loads(gh("view", tag, "--json", "tagName,isDraft,assets").stdout)
    except subprocess.CalledProcessError as error:
        if error.stderr.strip().lower() == "release not found":
            print(f"{tag} has not been published yet; its tag workflow will create the release.")
            return
        raise
    if release.get("tagName") != tag or release.get("isDraft") is not False:
        raise ValueError("Expected the existing public release for this version")
    with tempfile.TemporaryDirectory(prefix="virtualkvm-release-page-") as directory:
        gh("download", tag, "--pattern", "installation-checks.json", "--pattern", "SHA256SUMS", "--dir", directory)
        source = subprocess.check_output(["git", "rev-parse", f"{tag}^{{}}"], text=True, cwd=ROOT).strip()
        report = json.loads((Path(directory) / "installation-checks.json").read_text())
        validate_publication(version, release["assets"], report, (Path(directory) / "SHA256SUMS").read_text(), source)
    gh("edit", tag, "--draft=false", "--prerelease=false", "--latest", "--title", f"VirtualKVM {version}",
       "--notes-file", str(notes.resolve()))
    print(f"Published Latest release: https://github.com/{repository}/releases/tag/{tag}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", help="Require this tag to match the application version")
    parser.add_argument("--update-existing", action="store_true")
    args = parser.parse_args(argv)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if args.tag is not None and args.tag != f"v{version}":
        raise ValueError("Release tag must match the application version")
    content = render(args.repository, version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    if args.update_existing:
        update_existing(args.repository, version, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
