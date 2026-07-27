"""KB build report — scans markers and prints a human-readable summary.

Called automatically at the end of knowledge_acquisition.py.
Output: console + {kb_path}/report/kb_report.md + batch_status.jsonl + batch_summary.md.
"""

import json
import os
import time


def generate(kb_path):
    """Scan the KB and emit a build report to console and kb_report.md.

    *kb_report.md* is overwritten only when the aggregate counts change.
    After the report, *batch_summary.md* is regenerated from *batch_status.jsonl*.
    """
    r = _scan(kb_path)
    ts = time.strftime("%Y%m%d_%H%M%S")
    md = _format_markdown(r, ts)
    print(md)

    report_dir = os.path.join(kb_path, "report")
    os.makedirs(report_dir, exist_ok=True)

    report_path = os.path.join(report_dir, "kb_report.md")
    _write_if_changed(report_path, md)

    write_summary(kb_path)

    return r


def append_status(kb_path, status_line):
    """Append one per-config status line to batch_status.jsonl.

    *status_line* is a dict with keys like ``config``, ``status``, ``elapsed``.
    """
    report_dir = os.path.join(kb_path, "report")
    os.makedirs(report_dir, exist_ok=True)
    jsonl = os.path.join(report_dir, "batch_status.jsonl")
    with open(jsonl, "a") as f:
        f.write(json.dumps(status_line, sort_keys=True) + "\n")


def write_summary(kb_path):
    """Generate batch_summary.md from batch_status.jsonl (overwrite).

    Duplicate config entries keep only the last occurrence.
    """
    report_dir = os.path.join(kb_path, "report")
    jsonl = os.path.join(report_dir, "batch_status.jsonl")
    if not os.path.exists(jsonl):
        return

    entries = []
    try:
        with open(jsonl) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return

    if not entries:
        return

    # Keep last occurrence of each config
    seen = {}
    for e in entries:
        seen[e.get("config", "")] = e
    unique = list(seen.values())

    # Stats
    total = len(unique)
    ok_count = sum(1 for e in unique if e.get("status") == "ok")
    fail_count = total - ok_count
    total_elapsed = sum(e.get("elapsed", 0) for e in unique)

    # By project + library
    by_proj_lib = {}
    for e in unique:
        config = e.get("config", "")
        parts = config.split("/")
        proj = parts[0] if len(parts) > 0 else config
        lib = parts[1] if len(parts) > 1 else "-"
        ver = parts[2] if len(parts) > 2 else "-"
        key = f"{proj}/{lib}"
        entry = by_proj_lib.setdefault(key, {"total": 0, "ok": 0, "fail": 0, "versions": []})
        entry["total"] += 1
        entry["versions"].append(ver)
        if e.get("status") == "ok":
            entry["ok"] += 1
        else:
            entry["fail"] += 1

    def _vers_range(versions):
        """Return version range string, e.g. '3.1.2 ~ 3.2.0' or just the version."""
        if len(versions) <= 1:
            return versions[0] if versions else "-"
        from packaging.version import parse as pv
        sv = sorted(versions, key=pv)
        return f"{sv[0]} ~ {sv[-1]}"

    failures = [e for e in unique if e.get("status") != "ok"]

    ts = time.strftime("%Y%m%d_%H%M%S")
    lines = []
    lines.append("# Batch Summary")
    lines.append(f"_{ts}_")
    lines.append("")
    lines.append("## Overall")
    lines.append(f"{total} configs | {ok_count} ok | {fail_count} failed"
                 f" | {total_elapsed:.0f}s")
    lines.append("")
    lines.append("## By Project")
    lines.append("| Project | Library | Versions | Total | OK | Fail |")
    lines.append("|---------|---------|----------|-------|----|------|")
    for key in sorted(by_proj_lib):
        bp = by_proj_lib[key]
        proj, lib = key.split("/", 1)
        vers = _vers_range(bp["versions"])
        lines.append(f"| {proj} | {lib} | {vers} | {bp['total']} | {bp['ok']} | {bp['fail']} |")

    if failures:
        lines.append("")
        lines.append("## Failures")
        lines.append("| Config | Elapsed |")
        lines.append("|--------|---------|")
        for e in failures:
            lines.append(f"| {e.get('config', '?')} | {e.get('elapsed', '?')}s |")

    lines.append("")
    summary_path = os.path.join(report_dir, "batch_summary.md")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines))


def _write_if_changed(path, content):
    """Write *content* to *path* only if the file doesn't exist or differs."""
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = f.read()
        except OSError:
            existing = None
        if existing == content:
            return
    with open(path, "w") as f:
        f.write(content)


def _scan(kb_path):
    libs_dir = os.path.join(kb_path, "libraries")
    api_dir = os.path.join(kb_path, "library_api")

    total = ok = skipped = failed = 0
    by_library = {}
    issues = []

    if not os.path.isdir(libs_dir):
        return {"total": 0, "ok": 0, "skipped": 0, "failed": 0,
                "by_library": {}, "issues": []}

    for lib in sorted(os.listdir(libs_dir)):
        lib_dir = os.path.join(libs_dir, lib)
        if not os.path.isdir(lib_dir):
            continue

        lib_ok = lib_skipped = lib_failed = 0
        lib_issues = []

        for ver_dir_name in sorted(os.listdir(lib_dir)):
            ver_path = os.path.join(lib_dir, ver_dir_name)
            if not os.path.isdir(ver_path):
                continue

            ver = ver_dir_name[len(lib):] if ver_dir_name.startswith(lib) else ver_dir_name
            total += 1

            no_source = os.path.join(ver_path, ".no_source")
            json_path = os.path.join(api_dir, lib, f"{ver}.json")
            json_failed = json_path + ".failed"

            # Check source: any Python package or .py file exists
            has_source = _has_source(ver_path)

            # Failure/abnormal markers checked FIRST — they override success
            if os.path.exists(os.path.join(ver_path, ".building")) and \
                    not os.path.exists(os.path.join(ver_path, ".complete")):
                # .building without .complete → interrupted build
                lib_failed += 1
                failed += 1
                lib_issues.append({"library": lib, "version": ver,
                                   "status": "interrupted",
                                   "reason": "build interrupted — .building marker left",
                                   "fix": "delete directory and re-download"})
            elif os.path.exists(no_source):
                lib_skipped += 1
                skipped += 1
            elif os.path.exists(json_failed):
                lib_failed += 1
                failed += 1
                lib_issues.append({"library": lib, "version": ver,
                                   "status": "empty_extraction",
                                   "reason": "API extraction produced empty modules",
                                   "fix": "check source structure and re-download"})
            elif os.path.exists(os.path.join(ver_path, ".complete")):
                # Build completed — API may be pending or package is metadata-only
                lib_ok += 1
                ok += 1
            elif os.path.exists(json_path):
                lib_ok += 1
                ok += 1
            elif has_source:
                # Source downloaded but API not extracted yet
                lib_ok += 1
                ok += 1
            else:
                # No markers at all — download likely failed or not attempted
                artifacts = [f for f in os.listdir(ver_path)
                            if f.endswith(('.tar.gz', '.zip', '.whl'))]
                if artifacts:
                    lib_failed += 1
                    failed += 1
                    lib_issues.append({"library": lib, "version": ver,
                                       "status": "extraction_failed",
                                       "reason": "archive downloaded but source not extracted",
                                       "fix": f"rm -rf {ver_path} && re-run KA"})
                elif _dir_is_recent(ver_path):
                    lib_failed += 1
                    failed += 1
                    lib_issues.append({"library": lib, "version": ver,
                                       "status": "download_failed",
                                       "reason": "download failed — no archive retrieved from PyPI",
                                       "fix": f"rm -rf {ver_path} && re-run KA"})
                else:
                    lib_failed += 1
                    failed += 1
                    lib_issues.append({"library": lib, "version": ver,
                                       "status": "empty_legacy",
                                       "reason": "empty directory from old KB",
                                       "fix": f"find {lib_dir} -empty -type d -delete && re-run KA"})

        if lib_issues:
            by_library[lib] = {
                "total": lib_ok + lib_skipped + lib_failed,
                "ok": lib_ok, "skipped": lib_skipped, "failed": lib_failed,
                "issues": lib_issues,
            }
            issues.extend(lib_issues)

    return {
        "total": total, "ok": ok, "skipped": skipped, "failed": failed,
        "by_library": by_library, "issues": issues,
    }


def _has_source(ver_path):
    """Check if any Python source exists in the version directory.

    Recursively checks for ``__init__.py`` (PEP 420 namespace packages) and falls
    back to ``.dist-info``/``.egg-info`` detection (metadata-only packages, P38).
    """
    for d in sorted(os.listdir(ver_path)):
        if d.startswith(".") or d.endswith((".dist-info", ".egg-info", ".data")):
            continue
        full = os.path.join(ver_path, d)
        if os.path.isdir(full):
            if _has_init_py(full):
                return True
        if os.path.isfile(full) and d.endswith(".py") and d != "setup.py":
            return True
    return _has_metadata(ver_path)


def _has_init_py(dir_path):
    """Recursively check for ``__init__.py`` (handles PEP 420 namespace packages)."""
    if os.path.isfile(os.path.join(dir_path, "__init__.py")):
        return True
    for d in sorted(os.listdir(dir_path)):
        full = os.path.join(dir_path, d)
        if os.path.isdir(full) and not d.startswith(".") \
                and not d.endswith((".dist-info", ".egg-info", ".data")):
            if _has_init_py(full):
                return True
    return False


def _has_metadata(ver_path):
    """Check for .dist-info or .egg-info directories (metadata-only packages)."""
    for d in os.listdir(ver_path):
        if d.endswith((".dist-info", ".egg-info")):
            return True
    return False


def _dir_is_recent(ver_path):
    """Check if directory was created/modified in 2026 or later (current era)."""
    try:
        st = os.stat(ver_path)
        from datetime import datetime
        return datetime.fromtimestamp(st.st_mtime).year >= 2026
    except OSError:
        return False


def _format_markdown(r, ts=""):
    """Format report as markdown."""
    total = r["total"]
    if total == 0:
        return "_No KB data found._"

    ok_pct = r["ok"] / total * 100 if total else 0
    skip_pct = r["skipped"] / total * 100 if total else 0
    fail_pct = r["failed"] / total * 100 if total else 0

    lines = []
    lines.append("# KB Build Report")
    if ts:
        lines.append(f"_{ts}_")
    lines.append("")
    lines.append("| Category | Count | Percentage |")
    lines.append("|----------|-------|------------|")
    lines.append(f"| OK       | {r['ok']} | {ok_pct:.1f}% |")
    lines.append(f"| Skipped  | {r['skipped']} | {skip_pct:.1f}% |")
    lines.append(f"| Failed   | {r['failed']} | {fail_pct:.1f}% |")
    lines.append(f"| **Total** | **{total}** | |")

    issues = r["issues"]
    if not issues:
        lines.append("")
        lines.append("No issues found.")
        return "\n".join(lines)

    by_lib = {}
    for iss in issues:
        lib = iss["library"]
        if lib not in by_lib:
            by_lib[lib] = {}
        status = iss.get("status", "unknown")
        if status not in by_lib[lib]:
            by_lib[lib][status] = []
        by_lib[lib][status].append(iss)

    lines.append("")
    lines.append("## Issues by Library")
    lines.append("")
    for lib in sorted(by_lib.keys()):
        for status in sorted(by_lib[lib].keys()):
            lib_issues = by_lib[lib][status]
            n = len(lib_issues)
            reason = lib_issues[0]["reason"]
            fix = lib_issues[0]["fix"]
            lines.append(f"- **{lib}** ({n} version(s), {status}): {reason}")
            lines.append(f"  - Action: {fix}")

    return "\n".join(lines)
