#!/usr/bin/env python3
"""
Packwiz Modpack Updater
=======================
A modpack/version-agnostic tool that:
  - Reads the modpack's pack.toml for name, modloader & current game version
  - Scans the filesystem for all mod/resourcepack .pw.toml metadata files
  - Queries the Modrinth API for versions compatible with a target Minecraft version + modloader
  - Generates a compatibility report
  - Optionally updates compatible mods, removes incompatible ones, and refreshes the index

Usage:
  python updater.py <target_version> [options]

Examples:
  python updater.py 26.2 --report
  python updater.py 26.2 --interactive
  python updater.py 26.2 --auto
  python updater.py 26.2 --auto --yes
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
API_BASE = "https://api.modrinth.com/v2"
PACKWIZ_CMD = "packwiz"
DEFAULT_PACK_FILE = "pack.toml"
DEFAULT_MODS_DIR = "mods"
DEFAULT_RP_DIR = "resourcepacks"
REPORT_FILE = "compat-report.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def info(msg: str):
    print(f"[ i ] {msg}")


def ok(msg: str):
    print(f"[ ✅ ] {msg}")


def warn(msg: str):
    print(f"[ ⚠ ] {msg}")


def fail(msg: str):
    print(f"[ ❌ ] {msg}")


def fetch_json(url: str, retries: int = 3) -> Optional[dict | list]:
    """Fetch JSON from a URL with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "PackwizUpdater/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                warn(f"Failed to fetch {url} after {retries} attempts: {e}")
                return None


# ---------------------------------------------------------------------------
# Modpack metadata
# ---------------------------------------------------------------------------


def read_pack_toml(path: str = DEFAULT_PACK_FILE) -> dict:
    """Parse pack.toml using simple TOML parsing (no dependency)."""
    import toml  # already used in build.py

    with open(path, "r") as f:
        return toml.load(f)


def write_pack_toml(data: dict, path: str = DEFAULT_PACK_FILE):
    """Write pack.toml (simple round-trip preserving order)."""
    import toml

    with open(path, "w") as f:
        toml.dump(data, f)


# ---------------------------------------------------------------------------
# Scan for mod metadata files
# ---------------------------------------------------------------------------


def scan_metadata_files() -> list[dict]:
    """
    Walk the workspace and find all .pw.toml metadata files.
    Returns a list of dicts with keys:
      - file:      relative path to the .pw.toml (e.g. "mods/sodium.pw.toml")
      - name:      display name from the metadata
      - filename:  the actual jar/zip filename
      - side:      client / server / both
      - mod_id:    Modrinth project ID
      - version:   Modrinth version ID
      - category:  inferred from parent folder name (mods, resourcepacks, …)
    """
    results = []
    pack_dir = Path.cwd()

    # Look in known metadata directories
    for folder in [DEFAULT_MODS_DIR, DEFAULT_RP_DIR]:
        folder_path = pack_dir / folder
        if not folder_path.is_dir():
            continue
        for meta_file in sorted(folder_path.glob("*.pw.toml")):
            entry = parse_metadata_file(meta_file, folder)
            if entry:
                results.append(entry)

    # Also look for .pw.toml in the root (e.g. for shader packs)
    for meta_file in sorted(pack_dir.glob("*.pw.toml")):
        if meta_file.name == "pack.toml":
            continue
        entry = parse_metadata_file(meta_file, ".")
        if entry:
            results.append(entry)

    return results


def parse_metadata_file(meta_path: Path, category: str) -> Optional[dict]:
    """Parse a single .pw.toml metadata file and return relevant fields."""
    import toml

    try:
        with open(meta_path, "r") as f:
            data = toml.load(f)
    except Exception as e:
        warn(f"Could not parse {meta_path}: {e}")
        return None

    name = data.get("name", meta_path.stem)
    filename = data.get("filename", "")
    side = data.get("side", "both")
    mod_id = None
    version = None

    # Modrinth update metadata
    update = data.get("update", {})
    mr = update.get("modrinth", {})
    mod_id = mr.get("mod-id")
    version = mr.get("version")

    if not mod_id:
        warn(f"No Modrinth mod-id found in {meta_path}, skipping")
        return None

    return {
        "file": str(meta_path.relative_to(Path.cwd()).as_posix()),
        "name": name,
        "filename": filename,
        "side": side,
        "mod_id": mod_id,
        "version": version,
        "category": category,
    }


# ---------------------------------------------------------------------------
# Modrinth API queries
# ---------------------------------------------------------------------------


def check_mod_compat(mod_id: str, target_version: str, target_loader: str) -> tuple:
    """
    Check if a mod has a version supporting the target Minecraft version + loader.

    Returns: (supports: bool,
              compatible_version: str or None,
              compatible_version_id: str or None,
              latest_version_overall: str or None)
    """
    url = f"{API_BASE}/project/{mod_id}/version"
    versions = fetch_json(url)

    if versions is None:
        return (False, None, None, None)

    # Normalise loader name (Modrinth uses lowercase e.g. "fabric", "neoforge", "forge")
    target_loader = target_loader.strip().lower()

    compatible_version = None
    compatible_version_id = None
    latest_version = None

    for ver in versions:
        game_versions = [v.lower() for v in ver.get("game_versions", [])]
        loaders = [v.lower() for v in ver.get("loaders", [])]
        version_number = ver.get("version_number", "?")
        version_id = ver.get("id", "")

        # Track latest overall (first in list is usually newest)
        if latest_version is None:
            latest_version = version_number

        # Check version match
        version_match = target_version.lower() in game_versions
        if not version_match:
            continue

        # If the version declares loaders, they must include our target loader
        # (resource packs, data packs, etc. use "minecraft" as loader — skip check for those)
        if loaders and "minecraft" not in loaders and target_loader not in loaders:
            continue

        # Found a compatible version
        if compatible_version is None:
            compatible_version = version_number
            compatible_version_id = version_id

    return (
        compatible_version is not None,
        compatible_version,
        compatible_version_id,
        latest_version,
    )


def get_version_download_info(mod_id: str, version_id: str) -> Optional[dict]:
    """
    Get download info for a specific version of a project.
    Returns dict with filename, url, hash-format, hash, or None.
    """
    url = f"{API_BASE}/project/{mod_id}/version/{version_id}"
    data = fetch_json(url)
    if data is None:
        return None

    version_number = data.get("version_number", "?")  # type: ignore
    files = data.get("files", [])  # type: ignore
    primary = None
    for f in files:
        if f.get("primary", False):
            primary = f
            break
    if not primary and files:
        primary = files[0]  # fallback to first file
    if not primary:
        return None

    hashes = primary.get("hashes", {})
    # Prefer sha512, fallback to sha256
    hash_format = None
    hash_val = None
    for hf in ("sha512", "sha256", "sha1", "murmur2"):
        if hf in hashes:
            hash_format = hf
            hash_val = hashes[hf]
            break

    return {
        "version_number": version_number,
        "filename": primary.get("filename", ""),
        "url": primary.get("url", ""),
        "hash_format": hash_format,
        "hash": hash_val,
    }


# ---------------------------------------------------------------------------
# Updating / removing via packwiz CLI
# ---------------------------------------------------------------------------


def packwiz_update_all():
    """Update all mods via packwiz."""
    try:
        subprocess.run(
            [PACKWIZ_CMD, "update", "--all", "-y"],
            cwd=Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        warn(f"packwiz update --all failed:\n{e.stderr}")
        return False


def packwiz_update_mod(mod_name: str) -> bool:
    """Update a single mod via packwiz."""
    try:
        subprocess.run(
            [PACKWIZ_CMD, "update", mod_name, "-y"],
            cwd=Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        warn(f"packwiz update {mod_name} failed:\n{e.stderr}")
        return False


def packwiz_remove(mod_name: str) -> bool:
    """Remove a single mod via packwiz."""
    try:
        subprocess.run(
            [PACKWIZ_CMD, "remove", mod_name, "-y"],
            cwd=Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        warn(f"packwiz remove {mod_name} failed:\n{e.stderr}")
        return False


def packwiz_refresh() -> bool:
    """Refresh the packwiz index."""
    try:
        subprocess.run(
            [PACKWIZ_CMD, "refresh"],
            cwd=Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        warn(f"packwiz refresh failed:\n{e.stderr}")
        return False


def packwiz_set_acceptable_versions(versions: list[str]) -> bool:
    """Set acceptable Minecraft versions for the modpack."""
    try:
        subprocess.run(
            [PACKWIZ_CMD, "settings", "acceptable-versions", ",".join(versions), "-y"],
            cwd=Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        warn(f"packwiz settings acceptable-versions failed:\n{e.stderr}")
        return False


# ---------------------------------------------------------------------------
# Manual metadata file update (fallback if packwiz update doesn't work well)
# ---------------------------------------------------------------------------


def update_metadata_file(entry: dict, download_info: dict) -> bool:
    """
    Update a .pw.toml metadata file with new version info from the API.
    """
    import toml

    meta_path = Path.cwd() / entry["file"]
    try:
        with open(meta_path, "r") as f:
            data = toml.load(f)
    except Exception as e:
        warn(f"Could not read {meta_path}: {e}")
        return False

    # Update filename
    data["filename"] = download_info["filename"]

    # Update download section
    if "download" not in data:
        data["download"] = {}
    data["download"]["url"] = download_info["url"]
    if download_info["hash_format"]:
        data["download"]["hash-format"] = download_info["hash_format"]
        data["download"]["hash"] = download_info["hash"]

    # Update update.modrinth version
    if "update" not in data:
        data["update"] = {}
    if "modrinth" not in data["update"]:
        data["update"]["modrinth"] = {"mod-id": entry["mod_id"]}
    # We need the version ID from the compatible version; it's the version_number
    # Actually, the 'version' field in update.modrinth is the version ID (UUID)
    # We don't have the UUID - but we can get it from the download_info
    # Let's re-fetch with more details later if needed
    # For now, keep the original approach: use packwiz update which handles this

    try:
        with open(meta_path, "w") as f:
            toml.dump(data, f)
        return True
    except Exception as e:
        warn(f"Could not write {meta_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report(
    compatible: list,
    incompatible: list,
    pack_info: dict,
    target_version: str,
    output_path: str = REPORT_FILE,
) -> str:
    """Generate a compatibility report markdown file."""
    report = f"""# {pack_info.get('name', 'Modpack')} — Minecraft {target_version} Compatibility Report
**Generated:** {time.strftime("%Y-%m-%d %H:%M:%S")}
**Current Pack Version:** {pack_info.get('version', '?')}
**Current Minecraft Version:** {pack_info.get('current_mc_version', '?')}
**Target Minecraft Version:** {target_version}
**Modloader:** {pack_info.get('loader_name', '?')} {pack_info.get('loader_version', '?')}

---

## Summary
- **Total mods/resourcepacks checked:** {len(compatible) + len(incompatible)}
- **Compatible with {target_version}:** {len(compatible)}
- **Not yet compatible with {target_version}:** {len(incompatible)}

---

## ✅ Compatible with Minecraft {target_version}
| # | Name | Version | Category |
|---|------|---------|----------|
"""
    for i, entry in enumerate(compatible, 1):
        report += f"| {i} | {entry['name']} | {entry['compat_version'] or '✓'} | {entry['category']} |\n"

    report += f"""
---

## ❌ NOT Yet Compatible with Minecraft {target_version}
| # | Name | Latest Available Version | Category |
|---|------|------------------------|----------|
"""
    for i, entry in enumerate(incompatible, 1):
        report += f"| {i} | {entry['name']} | {entry['latest_version'] or '?'} | {entry['category']} |\n"

    report += f"""
---

## Notes
- This report was generated by querying the Modrinth API.
- A mod marked as "not compatible" means no version on Modrinth explicitly supports Minecraft {target_version} with the {pack_info.get('loader_name', 'current')} modloader.
- Some mods may work even if not explicitly marked — always verify on the project page.
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    return output_path


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------


def ask_yes_no(prompt: str, default: Optional[bool] = None) -> bool:
    """Ask the user a yes/no question and return the answer."""
    suffix = ""
    if default is True:
        suffix = " [Y/n]: "
    elif default is False:
        suffix = " [y/N]: "
    else:
        suffix = " [y/n]: "

    while True:
        answer = input(prompt + suffix).strip().lower()
        if not answer and default is not None:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------

def parse_version(version: str) -> tuple:
    """
    Parse a SemVer-ish version string into (major, minor, patch, prerelease).
    Supports formats: "1.0.1", "1.0.1-alpha.1", "1.0.1-rc.2".
    Returns (major, minor, patch, prerelease) where prerelease is "" if absent.
    """
    prerelease = ""
    # Strip pre-release suffix if present
    if "-" in version:
        base, prerelease = version.split("-", 1)
    else:
        base = version
    parts = base.split(".")
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch = int(parts[2]) if len(parts) > 2 else 0
    return (major, minor, patch, prerelease)


def bump_version(current: str, bump_type: str, prerelease: str = "") -> str:
    """
    Bump a SemVer version string.
    
    Args:
        current: Current version (e.g. "1.0.1")
        bump_type: "major", "minor", or "patch"
        prerelease: Optional pre-release tag (e.g. "alpha.1")
    
    Returns:
        New version string.
    """
    major, minor, patch, _ = parse_version(current)
    
    if bump_type == "major":
        major += 1
        minor = 0
        patch = 0
    elif bump_type == "minor":
        minor += 1
        patch = 0
    elif bump_type == "patch":
        patch += 1
    
    base = f"{major}.{minor}.{patch}"
    if prerelease:
        return f"{base}-{prerelease}"
    return base


def suggest_version_bump(current: str, mc_version_changed: bool) -> str:
    """
    Suggest a version bump type based on the change.
    If MC version changed, suggest "minor"; otherwise "patch".
    """
    return "minor" if mc_version_changed else "patch"


# ---------------------------------------------------------------------------
# Subfolder / isolation
# ---------------------------------------------------------------------------


def copy_modpack_to_subfolder(subfolder_name: str) -> Path:
    """
    Copy the entire modpack directory into a subfolder and change into it.
    Excludes common build artifacts and the subfolder itself (if it already
    exists from a previous run). Returns the path to the new subfolder.
    """
    src = Path.cwd().resolve()
    dst = src / subfolder_name

    if dst.exists():
        fail(f"Target subfolder '{subfolder_name}' already exists.")
        sys.exit(1)

    info(f"Copying modpack to subfolder '{subfolder_name}'...")

    # Patterns / directories to exclude from the copy
    def _ignore(src_dir, names):
        ignored = set()
        for name in names:
            full = Path(src_dir) / name
            # Skip the subfolder we're about to create, build dirs, cache, etc.
            if full == dst:
                ignored.add(name)
            elif full.is_dir() and name in ("build", "__pycache__", ".git", ".github"):
                ignored.add(name)
            elif name.endswith(".mrpack"):
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=_ignore, symlinks=False)
    os.chdir(dst)
    ok(f"Now working in: {dst}")
    return dst


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Packwiz modpack updater — check and update mods for a new Minecraft version.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 26.2                                 # Interactive mode (default)
  %(prog)s 26.2 --report                        # Generate report only
  %(prog)s 26.2 --interactive                   # Explicitly use interactive mode
  %(prog)s 26.2 --auto                          # Automatically update everything
  %(prog)s 26.2 --auto --yes                    # Auto-update, skip confirmations
  %(prog)s 26.2 --auto --modpack-version 1.1.0  # Set specific modpack version
  %(prog)s 26.2 --auto --bump minor             # Auto-bump minor version
  %(prog)s 26.2 --auto --bump minor --pre-release alpha.1  # Bump + pre-release
  %(prog)s 26.2 --report -o report.md           # Custom output path
        """,
    )
    parser.add_argument(
        "target_version",
        help="Target Minecraft version to check compatibility for (e.g. 26.2)",
    )
    parser.add_argument(
        "--loader",
        help="Override modloader detection (e.g. fabric, neoforge, forge, quilt)",
        default=None,
    )
    parser.add_argument(
        "--loader-version",
        help="Override modloader version (e.g. 0.19.2)",
        default=None,
    )
    parser.add_argument(
        "--pack-file",
        help=f"Path to pack.toml (default: {DEFAULT_PACK_FILE})",
        default=DEFAULT_PACK_FILE,
    )

    # Mode arguments
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--report",
        "-r",
        action="store_true",
        help="Generate compatibility report only",
    )
    mode_group.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Interactive mode: show results and ask before updating (default)",
    )
    mode_group.add_argument(
        "--auto",
        "-a",
        action="store_true",
        help="Automatic mode: update compatible mods, remove incompatible ones",
    )

    # Version management
    version_group = parser.add_argument_group("Version management")
    version_group.add_argument(
        "--modpack-version",
        help="Explicitly set the new modpack version (e.g. 1.1.0)",
        default=None,
    )
    version_group.add_argument(
        "--bump",
        choices=["major", "minor", "patch"],
        help="Auto-increment the modpack version (major.minor.patch). "
             "If the MC version changes, default is 'minor'.",
        default=None,
    )
    version_group.add_argument(
        "--pre-release",
        help="Set a pre-release tag on the version (e.g. 'alpha.1', 'rc.1'). "
             "The version becomes <base>-<tag>.",
        default=None,
    )

    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip all confirmation prompts (use with --auto)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=REPORT_FILE,
        help=f"Output file for the report (default: {REPORT_FILE})",
    )
    parser.add_argument(
        "--keep-report",
        action="store_true",
        help="Keep the report file even when proceeding with update",
    )

    # Subfolder / isolation
    parser.add_argument(
        "--subfolder",
        "-s",
        action="store_true",
        help="Copy the modpack into a subfolder (named <target-version>-<new-version>) before making changes",
    )
    parser.add_argument(
        "--subfolder-name",
        help="Custom name for the subfolder when using --subfolder (default: auto-generated from target version + new modpack version)",
        default=None,
    )

    args = parser.parse_args()

    target_version = args.target_version
    # Determine mode: default is interactive
    mode = "interactive"
    if args.report:
        mode = "report"
    elif args.auto:
        mode = "auto"

    # -----------------------------------------------------------------------
    # Step 1: Read modpack metadata
    # -----------------------------------------------------------------------
    print("=" * 64)
    print(f"  Packwiz Modpack Updater — target: Minecraft {target_version}")
    print("=" * 64)

    if not os.path.isfile(args.pack_file):
        fail(
            f"Pack file '{args.pack_file}' not found. Run this script from the pack root directory."
        )
        sys.exit(1)

    pack_info_raw = read_pack_toml(args.pack_file)
    pack_name = pack_info_raw.get("name", "Unnamed Pack")
    pack_version = pack_info_raw.get("version", "?")
    versions_section = pack_info_raw.get("versions", {})
    current_mc_version = versions_section.get("minecraft", "?")

    # Detect modloader from pack.toml
    loader_name = None
    loader_version = None
    if args.loader:
        loader_name = args.loader
    else:
        for key in ("fabric", "neoforge", "forge", "quilt", "liteloader"):
            if key in versions_section:
                loader_name = key
                loader_version = versions_section[key]
                break

    if args.loader_version:
        loader_version = args.loader_version

    if not loader_name:
        fail("Could not detect modloader from pack.toml. Use --loader to specify.")
        sys.exit(1)

    pack_info = {
        "name": pack_name,
        "version": pack_version,
        "current_mc_version": current_mc_version,
        "loader_name": loader_name,
        "loader_version": loader_version,
    }

    info(f"Modpack: {pack_name} v{pack_version}")
    info(f"Current MC: {current_mc_version}  →  Target: {target_version}")
    info(f"Modloader: {loader_name} {loader_version or '(version unknown)'}")

    # -----------------------------------------------------------------------
    # Step 2: Scan metadata files
    # -----------------------------------------------------------------------
    print()
    info("Scanning for mod/resourcepack metadata files...")
    all_entries = scan_metadata_files()
    if not all_entries:
        fail("No mod/resourcepack metadata files found!")
        sys.exit(1)
    info(f"Found {len(all_entries)} mod(s)/resourcepack(s).")

    # -----------------------------------------------------------------------
    # Step 3: Query Modrinth API for each entry
    # -----------------------------------------------------------------------
    print()
    info("Querying Modrinth API for version compatibility...")
    print()

    compatible = []
    incompatible = []

    for i, entry in enumerate(all_entries, 1):
        mod_id = entry["mod_id"]
        display = entry["name"]
        num_spaces_name = max(len(e["name"]) for e in all_entries)
        num_spaces_indx = len(str(len(all_entries)))
        print(
            f"  [{i:>{num_spaces_indx}}/{len(all_entries)}] {display} {'.' * (num_spaces_name - len(display))}",
            end=" ",
            flush=True,
        )

        supports, compat_ver, compat_ver_id, latest_ver = check_mod_compat(
            mod_id, target_version, loader_name
        )

        if supports:
            print(f"✅  {compat_ver or 'supported'}")
            entry["compat_version"] = compat_ver
            entry["compat_version_id"] = compat_ver_id
            compatible.append(entry)
        else:
            print(f"❌  (latest: {latest_ver or '?'})")
            entry["latest_version"] = latest_ver
            incompatible.append(entry)

        # Rate limiting
        if i < len(all_entries):
            time.sleep(0.25)

    # -----------------------------------------------------------------------
    # Step 4: Summary
    # -----------------------------------------------------------------------
    print()
    print("=" * 64)
    print(f"  Results: {len(compatible)} compatible, {len(incompatible)} incompatible")
    print("=" * 64)

    # Generate report
    report_path = generate_report(
        compatible, incompatible, pack_info, target_version, args.output
    )
    info(f"Compatibility report saved to: {report_path}")

    # -----------------------------------------------------------------------
    # Step 5: Decide on action
    # -----------------------------------------------------------------------
    should_update = False

    if mode == "report":
        info(
            "Report mode: no changes made. Use --interactive or --auto to perform updates."
        )
        return

    elif mode == "interactive":
        print()
        print("─" * 48)
        print(f"  Compatible mods:     {len(compatible)}")
        print(f"  Incompatible mods:   {len(incompatible)}")
        print("─" * 48)

        if args.yes or ask_yes_no("Proceed with updating the modpack?", default=False):
            should_update = True
        else:
            info("Update cancelled by user. Report saved.")

    elif mode == "auto":
        should_update = True

    if not should_update:
        return

    # -----------------------------------------------------------------------
    # Step 6: Perform the update
    # -----------------------------------------------------------------------
    print()
    print("=" * 64)
    print("  Performing update...")
    print("=" * 64)
    print()

    # 6a. Determine new modpack version
    mc_version_changed = current_mc_version != target_version
    new_version = None

    if args.modpack_version:
        # Explicit version provided
        new_version = args.modpack_version
        if args.pre_release:
            new_version = f"{new_version}-{args.pre_release}"
    else:
        # Determine bump type: explicit --bump or sensible default
        bump_type = args.bump or (suggest_version_bump(pack_version, mc_version_changed) if mc_version_changed else "patch")
        new_version = bump_version(pack_version, bump_type, args.pre_release or "")

    # If in interactive mode and version changed, confirm with user
    if mode == "interactive" and new_version != pack_version and not args.yes:
        if not ask_yes_no(f"Update modpack version from '{pack_version}' to '{new_version}'?", default=True):
            new_version = pack_version  # keep current version

    # 6b. Update pack.toml with new versions
    info(f"Updating pack.toml: {pack_name} v{pack_version} → v{new_version}")
    info(f"  Minecraft: {current_mc_version} → {target_version}")
    pack_info_raw["version"] = new_version
    pack_info_raw["versions"]["minecraft"] = target_version
    # Also add the new version to acceptable-game-versions if not present
    options = pack_info_raw.setdefault("options", {})
    acceptable = options.get("acceptable-game-versions", [])
    if target_version not in acceptable:
        acceptable.append(target_version)
        options["acceptable-game-versions"] = acceptable
    write_pack_toml(pack_info_raw)
    ok("pack.toml updated.")

    # 6c. Set acceptable versions via packwiz
    if acceptable:
        packwiz_set_acceptable_versions(acceptable)

    # 6d. Update compatible mods
    if compatible:
        info(f"Updating {len(compatible)} compatible mod(s)...")
        for entry in compatible:
            mod_name = entry["name"]
            print(f"  Updating {mod_name}...", end=" ", flush=True)
            success = packwiz_update_mod(mod_name)
            if success:
                ok(f"{mod_name} updated.")
            else:
                # Fallback: try packwiz update --all
                warn(f"Could not update {mod_name} individually.")
    else:
        info("No compatible mods to update.")

    # 6e. Remove incompatible mods
    if incompatible:
        warn(f"Removing {len(incompatible)} incompatible mod(s)...")
        for entry in incompatible:
            mod_name = entry["name"]
            print(f"  Removing {mod_name}...", end=" ", flush=True)
            success = packwiz_remove(mod_name)
            if success:
                ok(f"{mod_name} removed.")
            else:
                # Manual fallback: delete metadata file
                meta_path = Path.cwd() / entry["file"]
                if meta_path.exists():
                    meta_path.unlink()
                    warn(
                        f"Deleted metadata file for {mod_name} (packwiz remove failed)."
                    )
    else:
        info("No incompatible mods to remove.")

    # 6f. Refresh index
    print()
    info("Refreshing packwiz index...")
    packwiz_refresh()
    ok("Index refreshed.")

    # Update pack.toml hash
    pack_info_raw_updated = read_pack_toml(args.pack_file)
    pack_info_raw_updated["index"]["hash"] = ""
    # Re-hash: run packwiz refresh to update the hash
    packwiz_refresh()

    # -----------------------------------------------------------------------
    # Final
    # -----------------------------------------------------------------------
    print()
    print("=" * 64)
    print(f"  Update complete!")
    print(f"  Updated {len(compatible)} mod(s), removed {len(incompatible)} mod(s).")
    print("=" * 64)

    # Clean up report if not keeping it
    if not args.keep_report and os.path.exists(args.output):
        if (
            mode == "auto"
            or args.yes
            or ask_yes_no("Delete the report file?", default=True)
        ):
            os.remove(args.output)
            info("Report deleted.")
        else:
            info(f"Report kept at: {args.output}")


if __name__ == "__main__":
    main()
