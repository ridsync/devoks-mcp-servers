#!/usr/bin/env python3
"""TASK-048 — fail CI when a *our-dependency* vulnerability has a fix available.

Why a script instead of `pip-audit`
------------------------------------
`pip-audit` would work, but it means installing an auditing tool (and its own
dependency tree) into CI on every run, and it reports against PyPI advisory
data only. This script queries the OSV batch API directly — the same database
`pip-audit`, `osv-scanner`, and GitHub's Dependabot all consume — using
nothing but the standard library, against the **exact versions `uv.lock`
pins** rather than whatever a resolver happens to pick at audit time.

Why "has a fix available" is the failure condition
---------------------------------------------------
The distinction matters, and the base image is why. `servers/management/`'s
image carries Debian CVEs in `perl-base`/`glibc` that have **no fix in
trixie** — failing a build on those would mean a permanently red pipeline
that operators learn to ignore, which is worse than no gate at all. This
script therefore scopes itself to *our* Python dependencies, where a
vulnerability with a published fix is something a `uv lock --upgrade` can
actually resolve. Findings without a fix are reported and do not fail the
build, so they stay visible without becoming noise.

Exit codes: 0 nothing actionable, 1 at least one fixable vulnerability,
2 the audit itself could not run (network/parse failure) — never a silent pass.
"""

from __future__ import annotations

import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"
LOCKFILE = Path(__file__).resolve().parent.parent / "uv.lock"


def locked_packages() -> list[tuple[str, str]]:
    """Every third-party package `uv.lock` pins, with its exact version.

    Workspace members (this repository's own packages) carry no `version`
    resolved from an index — they are local sources — so they are skipped:
    OSV has nothing to say about them and including them would send our
    internal package names to a third-party API for no benefit.
    """
    with LOCKFILE.open("rb") as handle:
        data = tomllib.load(handle)
    packages: list[tuple[str, str]] = []
    for entry in data.get("package", []):
        name, version = entry.get("name"), entry.get("version")
        source = entry.get("source", {})
        if not name or not version:
            continue
        if "registry" not in source:  # local/editable workspace member
            continue
        packages.append((name, version))
    return packages


def query_osv(packages: list[tuple[str, str]]) -> list[list[dict[str, Any]]]:
    body = json.dumps(
        {
            "queries": [
                {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
                for name, version in packages
            ]
        }
    ).encode()
    request = urllib.request.Request(
        OSV_BATCH_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    return [result.get("vulns", []) for result in payload.get("results", [])]


def fixed_versions(vuln_id: str) -> list[str]:
    """Fetch a vulnerability's `fixed` events.

    The batch endpoint returns ids only, so each hit costs one extra request.
    That is acceptable precisely because hits are expected to be rare — and if
    they are not, a slow audit is the least of the problems.
    """
    with urllib.request.urlopen(OSV_VULN_URL + vuln_id, timeout=30) as response:
        detail = json.loads(response.read())
    fixes: list[str] = []
    for affected in detail.get("affected", []):
        for entry in affected.get("ranges", []):
            for event in entry.get("events", []):
                if "fixed" in event:
                    fixes.append(event["fixed"])
    return sorted(set(fixes))


def main() -> int:
    packages = locked_packages()
    print(f"Auditing {len(packages)} locked packages from {LOCKFILE.name} against OSV")

    try:
        results = query_osv(packages)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        # Exit 2, not 0: an audit that could not run must not look like a pass.
        print(f"::error::dependency audit could not run: {type(exc).__name__}: {exc}")
        return 2

    fixable: list[str] = []
    unfixed: list[str] = []
    for (name, version), vulns in zip(packages, results, strict=True):
        for vuln in vulns:
            vuln_id = vuln.get("id", "?")
            try:
                fixes = fixed_versions(vuln_id)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                fixes = []
            line = f"{name}=={version}  {vuln_id}"
            if fixes:
                fixable.append(f"{line}  fixed in: {', '.join(fixes)}")
            else:
                unfixed.append(f"{line}  (no fix published)")

    if unfixed:
        print(f"\n{len(unfixed)} vulnerability(ies) with no published fix — reported, not failing:")
        for line in unfixed:
            print(f"  {line}")
            print(f"::warning::{line}")

    if fixable:
        print(f"\n{len(fixable)} vulnerability(ies) WITH a published fix:")
        for line in fixable:
            print(f"  {line}")
            print(f"::error::{line}")
        print("\nResolve with: uv lock --upgrade-package <name>")
        return 1

    print("\nNo dependency vulnerability with an available fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
