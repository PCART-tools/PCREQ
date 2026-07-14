from utils.util import *
from utils.kb_report import generate as kb_report_generate
from extraction.getCall import get_all_used_api
from extraction.lib_module_and_package_extraction import *
from extraction.library_api_and_module import *
from call_graph.get_FDG import *
import platform, argparse, os, json, time, requests, logging, tempfile, uuid
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import parse as parse_version
from packaging.utils import parse_wheel_filename
from packaging.tags import cpython_tags, compatible_tags
import tarfile
import zipfile
import shutil
import sys
from multiprocessing import Pool, cpu_count


if (platform.system() == 'Windows'):
    slash = "\\"
else:
    slash = r"/"

library_path_prefix = ""
constraint_path_prefix = ""
version_path_prefix = ""
api_path_prefix = ""

_stats = {"downloaded": 0, "failed": 0, "skipped": 0, "crashed": 0}

def setup_path(library_path_prefix_pass, constraint_path_prefix_pass, version_path_prefix_pass, api_path_prefix_pass):
    global library_path_prefix, constraint_path_prefix, version_path_prefix, api_path_prefix
    library_path_prefix = library_path_prefix_pass
    constraint_path_prefix = constraint_path_prefix_pass
    version_path_prefix = version_path_prefix_pass
    api_path_prefix = api_path_prefix_pass
    setup_path_1(library_path_prefix, constraint_path_prefix, version_path_prefix, api_path_prefix)

def load_config(config_path):
    with open(f"./configure/{config_path}", 'r') as file:
        config = json.load(file)
    return config

def get_proj_dependency_from_requirements(file_path):
    requirements_dict = {}
    with open(file_path, 'r') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#') and '@' not in line:
                package, version = line.split('==')
                requirements_dict[package.lower()] = version
    return requirements_dict

def get_available_version(FDG, sub_graph, python_version, target_proj_dependency, target_library, target_version):
    target_library_constraint = get_library_constraint_from_metadata(target_library, target_version, python_version)
    available_versions1 = {}
    available_versions2 = {}
    available_versions = {}
    target_library_dependency = FDG[target_library]
    available_versions[target_library] = []
    available_versions[target_library].append(target_version)
    with open(f"{version_path_prefix}library_version.json", 'r') as file:
        version_ls = json.load(file)
    for proj_dependency in sub_graph:
        flag = False
        if proj_dependency not in target_proj_dependency:
            continue
        condidate_version = []
        if proj_dependency not in target_library_dependency:
            try:
                condidate_version = version_ls[proj_dependency.lower()][python_version]
            except:
                logging.warning("Package %s not found in version list for py%s",
                                proj_dependency.lower(), python_version)
            if len(condidate_version) >= 150:
                condidate_version = condidate_version[-150:]
            elif len(condidate_version) >= 30:
                condidate_version = condidate_version[-30:]

            target_ver = target_proj_dependency[proj_dependency]
            target_ver_norm = str(parse_version(target_ver))
            match_idx = None
            for idx, v in enumerate(condidate_version):
                if str(parse_version(v)) == target_ver_norm:
                    match_idx = idx
                    break
            if match_idx is not None:
                condidate_version.pop(match_idx)
                condidate_version.append(target_ver)
                flag = True
            else:
                condidate_version.append(target_ver)
                flag = True
        else:
            try:
                for version in version_ls[proj_dependency][python_version]:
                    try:
                        if is_version_compat(version, target_library_constraint[proj_dependency]):
                            condidate_version.append(version)
                    except:
                        condidate_version = version_ls[proj_dependency][python_version]
                        break
            except:
                logging.warning("Package %s not found in version list (constrained path)",
                                proj_dependency)
            target_ver = target_proj_dependency[proj_dependency]
            target_ver_norm = str(parse_version(target_ver))
            match_idx = None
            for idx, v in enumerate(condidate_version):
                if str(parse_version(v)) == target_ver_norm:
                    match_idx = idx
                    break
            if match_idx is not None:
                condidate_version.pop(match_idx)
                condidate_version.append(target_ver)
                flag = True
        if flag:
            available_versions1[proj_dependency] = condidate_version
        else:
            available_versions2[proj_dependency] = condidate_version
    sorted_available_versions1 = dict(sorted(available_versions1.items(), key=lambda item: len(item[1])))
    sorted_available_versions2 = dict(sorted(available_versions2.items(), key=lambda item: len(item[1])))
    for i in sorted_available_versions1:
        available_versions[i] = sorted_available_versions1[i]
    for i in sorted_available_versions2:
        if i not in available_versions:
            available_versions[i] = sorted_available_versions2[i]
    return available_versions

def filter_versions(version_list):
    """Remove versions that fail parse_version (e.g. date-like strings)."""
    result = []
    for v in version_list:
        try:
            parse_version(v)
            result.append(v)
        except Exception:
            pass
    return result

# Point 29: packaging.tags-based wheel compatibility (runtime platform)
def _is_runtime_compat_wheel(filename, compat_tags):
    """Return True if *filename* wheel is compatible with the current platform."""
    if not filename.endswith(".whl"):
        return False
    try:
        _, _, _, tags = parse_wheel_filename(filename)
    except Exception:
        return False
    return any(t in compat_tags for t in tags)


def get_compatible_versions(package_name, python_version):
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        response = requests.get(url).json()
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return []
    compatible_versions = []
    new_python_version = python_version.replace(".", "")
    if "releases" not in response:
        return []
    # Pre-compute runtime platform compatibility tags (Point 29)
    _ver_major, _ver_minor = (int(python_version.split('.')[0]),
                               int(python_version.split('.')[1]))
    _compat_tags = set(cpython_tags((_ver_major, _ver_minor)))
    # Point 32: supplement with pure-python any-platform tags from pip
    _interp = f'cp{_ver_major}{_ver_minor}'
    for t in compatible_tags(python_version=(_ver_major, _ver_minor),
                              interpreter=_interp):
        if '-none-any' in str(t):
            _compat_tags.add(t)
    for version, files in response["releases"].items():
        # Point 29: skip versions with no runtime-compatible artifact
        if not any(f.get("packagetype") == "sdist"
                   or _is_runtime_compat_wheel(f.get("filename", ""),
                                                _compat_tags)
                   for f in files if f):
            continue
        for file_info in files:
            if file_info.get("python_version"):
                try:
                    if file_info["python_version"] == f"cp{new_python_version}":
                        rp = file_info.get("requires_python")
                        if rp is None or SpecifierSet(rp).contains(python_version):
                            compatible_versions.append(version)
                            break
                    elif file_info["python_version"] != None and f"py{python_version.split('.')[0]}" in file_info["python_version"]:
                        requires_python = file_info.get("requires_python")
                        if requires_python is not None and ("=" in requires_python or ">" in requires_python or "<" in requires_python):
                            if SpecifierSet(requires_python).contains(python_version):
                                compatible_versions.append(version)
                                break
                        else:
                            compatible_versions.append(version)
                            break
                    elif file_info.get("requires_python") == None:
                        compatible_versions.append(version)
                        break
                    elif SpecifierSet(file_info["requires_python"]).contains(python_version):
                        compatible_versions.append(version)
                        break
                except (KeyError, TypeError, InvalidSpecifier):
                    pass
    compatible_versions = filter_versions(compatible_versions)
    compatible_versions.sort(key=parse_version)
    if package_name == "torchvision" and "0.11.0" in compatible_versions:
        compatible_versions.remove("0.11.0")
    return compatible_versions


# ---------------------------------------------------------------------------
# Download infrastructure
# ---------------------------------------------------------------------------

def _wheel_priority(filename, python_version):
    """Return pip-native priority (position in compat tag list, 0 = best).

    Returns -1 for incompatible wheels, 999 for sdist.
    Uses packaging.tags.cpython_tags for pip's native ordering (Point 29).
    """
    if not filename.endswith(".whl"):
        return -1  # sdist handled separately by caller
    major, minor = int(python_version.split('.')[0]), int(python_version.split('.')[1])
    compat = list(cpython_tags((major, minor)))
    compat_set = set(compat)
    # Point 32: supplement with pure-python any-platform tags from pip
    _interp = f'cp{major}{minor}'
    for t in compatible_tags(python_version=(major, minor), interpreter=_interp):
        if '-none-any' in str(t) and t not in compat_set:
            compat.append(t)
            compat_set.add(t)
    try:
        _, _, _, tags = parse_wheel_filename(filename)
    except Exception:
        return -1
    best = 99999
    for t in tags:
        if t in compat_set:
            pos = compat.index(t)
            if pos < best:
                best = pos
    return best if best < 99999 else -1


def _select_download_urls(package_name, version, python_version):
    """Return priority-sorted download URLs from version constraint JSON.

    Priority (platform-independent):
      1: py3-none-any        5: cp{ver}-*-{platform}
      2: cp{ver}-*-any       6: other py3/cp3 + {platform}
      3: other py3/cp3 + any 7: sdist
      4: py3-none-{platform}
    Python 2-only wheels are skipped.
    """
    norm_name = norm_pkg(package_name)
    _resolved = resolve_pkg_dir(package_name, constraint_path_prefix)
    nv = norm_ver(version)
    json_path = None
    for name in (norm_name, _resolved):
        _candidate = f"{constraint_path_prefix}{name}/{name}{nv}/{name}.json"
        if os.path.exists(_candidate):
            json_path = _candidate
            break
    if json_path is None:
        return []
    try:
        with open(json_path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        try:
            os.remove(json_path)
        except FileNotFoundError:
            pass
        return []
    except OSError:
        return []
    urls = data.get("urls", [])
    if not urls:
        return []
    scored = []
    for u in urls:
        pkg_type = u.get("packagetype")
        filename = u.get("filename", "")
        url = u.get("url", "")
        if not url:
            continue
        if filename.endswith((".exe", ".msi", ".dmg", ".rpm", ".deb")):
            continue
        if pkg_type == "bdist_wheel":
            priority = _wheel_priority(filename, python_version)
            if priority < 0:
                continue
        elif pkg_type == "sdist":
            priority = 999
        else:
            continue
        scored.append((priority, url))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [url for _, url in scored]


def _build_fallback_candidates(package_name, version, python_version):
    """Return [(url, is_wheel), ...] from PyPI JSON API, sorted by priority.

    Same platform-independent priority as _select_download_urls.
    """
    candidates = []
    pypi_url = f"https://pypi.org/pypi/{package_name}/{version}/json"
    try:
        r = requests.get(pypi_url, timeout=7200)
        if r.status_code != 200:
            return candidates
        data = r.json()
        for u in data.get("urls", []):
            fname = u.get("filename", "")
            url = u.get("url", "")
            if not url or fname.endswith((".exe", ".msi", ".dmg", ".rpm", ".deb")):
                continue
            pkg_type = u.get("packagetype", "")
            if pkg_type == "bdist_wheel":
                priority = _wheel_priority(fname, python_version)
                if priority < 0:
                    continue
                candidates.append((priority, url, True))
            elif pkg_type == "sdist":
                candidates.append((999, url, False))
        candidates.sort(key=lambda x: (x[0], x[1]))
    except requests.RequestException:
        pass
    return [(url, is_wheel) for _, url, is_wheel in candidates]


def _safe_target(base_dir, member_name):
    """Return safe absolute target path, or None if traversal detected.

    Guarantees the resolved path stays within *base_dir*, blocking:
      - ``../`` and ``..\\`` parent traversal
      - absolute paths (``/etc/passwd``)
    """
    name = member_name.replace("\\", "/")
    if os.path.isabs(name):
        return None
    target = os.path.abspath(os.path.join(base_dir, name))
    base = os.path.abspath(base_dir)
    if os.path.commonpath([base, target]) != base:
        return None
    return target


def _should_extract(name):
    """Return True if *name* is Python source or packaging metadata.

    Keeps:
      - ``.py``, ``.pyi``, ``.pyw``, ``.pxi`` source files
      - ``.dist-info/``, ``.egg-info/`` metadata directories (whole tree)
      - ``.data/purelib/`` and ``.data/platlib/``: ``.py`` source only
      - ``setup.py``, ``setup.cfg``, ``pyproject.toml`` (build configs)
    Skips:
      - ``.so``, ``.dll``, ``.pyc`` binaries
      - ``.data/scripts/``, ``.data/headers/``, ``.data/data/`` (non-source)
    """
    # .dist-info and .egg-info: keep entire tree
    if ".dist-info/" in name or name.endswith(".dist-info/"):
        return True
    if ".egg-info/" in name or name.endswith(".egg-info/"):
        return True

    # .data/purelib and .data/platlib: keep .py source only
    if ".data/purelib/" in name or ".data/platlib/" in name:
        return name.endswith((".py", ".pyi", ".pyw", ".pxi"))

    # .data/ other (scripts, headers, data): skip entirely
    if ".data/" in name:
        return False

    # Top-level source files
    if name.endswith((".py", ".pyi", ".pyw", ".pxi")):
        return True
    if os.path.basename(name) in ("setup.py", "setup.cfg", "pyproject.toml"):
        return True
    return False


def _extract_archive(archive_path, target_dir):
    """Extract archive to *target_dir*, flattening single-top-level-dir wrappers.

    Per-member safety validation (Point 6):
      - rejects ``../`` and absolute-path traversal
      - rejects symlink, hardlink, device and fifo tar members

    Selective extraction (Point 14):
      - only writes ``.py/.pyi/.pyw/.pxi`` + metadata + build configs
      - skips ``.so``, ``.dll``, ``.pyc`` and other non-source files
    """
    extract_tmp = tempfile.mkdtemp(dir=os.path.dirname(target_dir),
                                    prefix=".extract-")
    # Try tarfile first (auto-detects compression from magic bytes:
    # .tar.gz, .tar.bz2, .tar.xz, .tar, .tgz, .tbz2, .txz)
    try:
        with tarfile.open(archive_path) as tf:
            # --- Point 6: per-member safety validation ---------------
            extract_ok = []
            for member in tf.getmembers():
                if member.issym() or member.islnk():
                    continue  # skip symlinks/hardlinks (rare, not .py source)
                if member.isdev():
                    raise ValueError(
                        f"Unsafe tar member (device): {member.name}")
                if member.isfifo():
                    raise ValueError(
                        f"Unsafe tar member (fifo): {member.name}")
                target = _safe_target(extract_tmp, member.name)
                if target is None:
                    raise ValueError(
                        f"Path traversal in tar: {member.name}")
                extract_ok.append(member)
            # --- Point 6+14: selective per-member extract ------------
            for member in extract_ok:
                if not _should_extract(member.name):
                    continue
                tf.extract(member, extract_tmp)
    except (tarfile.ReadError, tarfile.CompressionError):
        # Fall back to zipfile (handles .whl, .zip)
        with zipfile.ZipFile(archive_path) as zf:
            # --- Point 6: per-entry safety validation ----------------
            extract_ok = []
            for info in zf.infolist():
                if _safe_target(extract_tmp, info.filename) is None:
                    raise ValueError(
                        f"Path traversal in zip: {info.filename}")
                extract_ok.append(info)
            # --- Point 6+14: selective per-entry extract -------------
            for info in extract_ok:
                if not _should_extract(info.filename):
                    continue
                zf.extract(info, extract_tmp)
    for root, dirs, files in os.walk(extract_tmp):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o755)
            except OSError:
                pass
        for f in files:
            try:
                os.chmod(os.path.join(root, f), 0o644)
            except OSError:
                pass
    items = os.listdir(extract_tmp)
    os.makedirs(target_dir, exist_ok=True)
    if len(items) == 1 and os.path.isdir(os.path.join(extract_tmp, items[0])):
        src_dir = os.path.join(extract_tmp, items[0])
        for item in os.listdir(src_dir):
            try:
                shutil.move(os.path.join(src_dir, item),
                            os.path.join(target_dir, item))
            except OSError:
                pass
    else:
        for item in items:
            try:
                shutil.move(os.path.join(extract_tmp, item),
                            os.path.join(target_dir, item))
            except OSError:
                pass
    shutil.rmtree(extract_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def _promote_purelib(target_dir):
    """Lift .data/purelib/ and .data/platlib/ contents to target_dir root."""
    for d in os.listdir(target_dir):
        if not d.endswith(".data"):
            continue
        data_dir = os.path.join(target_dir, d)
        for sub in ("purelib", "platlib"):
            sub_path = os.path.join(data_dir, sub)
            if os.path.isdir(sub_path):
                for item in os.listdir(sub_path):
                    src = os.path.join(sub_path, item)
                    dst = os.path.join(target_dir, item)
                    if not os.path.exists(dst):
                        shutil.move(src, dst)
                shutil.rmtree(data_dir)
                break


def _read_top_level(extract_dir):
    """Read top_level.txt from .dist-info/ in extract_dir.  Returns list or None."""
    for d in os.listdir(extract_dir):
        if d.endswith(".dist-info"):
            tl = os.path.join(extract_dir, d, "top_level.txt")
            if not os.path.isfile(tl):
                continue
            try:
                with open(tl) as f:
                    entries = [l.strip() for l in f if l.strip()]
            except OSError:
                continue
            return entries if entries else None
    return None


def _install_source(target_dir, extract_dir, call_module, is_wheel=True):
    """Move Python packages from extract_dir to target_dir.

    *is_wheel* controls how aggressive the fallback strategy is:
      - wheel: allow recursive os.walk and whitelist relaxation
      - sdist: strict — refuse recursive walk and whitelist relaxation
        to avoid pulling in tests/docs/examples

    Returns True if at least one .py file ends up in target_dir.
    """
    os.makedirs(target_dir, exist_ok=True)

    whitelist = _read_top_level(extract_dir)

    src_dir = os.path.join(extract_dir, "src")
    has_src_layout = os.path.isdir(src_dir)
    lib_dir = os.path.join(extract_dir, "lib")
    has_lib_layout = os.path.isdir(lib_dir)

    def _should_move(item):
        if item.startswith("."):
            return False
        if whitelist:
            mod = item[:-3] if item.endswith(".py") else item
            # Normalize hyphen/underscore (PyPI name vs import name)
            mod_norm = mod.lower().replace("-", "_")
            wl_norm = {w.lower().replace("-", "_") for w in whitelist}
            return mod_norm in wl_norm
        return True

    def _move_items(source_dir, allow_recursive_fallback=True):
        any_moved = False
        for item in sorted(os.listdir(source_dir)):
            if not _should_move(item):
                continue
            src = os.path.join(source_dir, item)
            dst = os.path.join(target_dir, item)
            if os.path.exists(dst):
                try:
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    else:
                        os.remove(dst)
                except OSError:
                    pass
            if os.path.exists(dst):
                continue
            if os.path.isdir(src) and os.path.isfile(os.path.join(src, "__init__.py")):
                try:
                    shutil.move(src, dst)
                    any_moved = True
                except OSError:
                    pass
            elif os.path.isdir(src) and whitelist and item in whitelist:
                # PEP 420 namespace package: no __init__.py but in whitelist
                os.makedirs(dst, exist_ok=True)
                for sub_item in os.listdir(src):
                    s_src = os.path.join(src, sub_item)
                    s_dst = os.path.join(dst, sub_item)
                    try:
                        shutil.move(s_src, s_dst)
                    except OSError:
                        pass
                any_moved = True
            elif os.path.isdir(src) and not allow_recursive_fallback:
                # P39: detect nested packages in sdist non-standard layouts
                # (wheel uses recursive fallback below instead).
                # Mode A: nested __init__.py (e.g. python/curl/__init__.py)
                # Mode B: single-file modules (e.g. regex_3/regex.py, no __init__.py)
                _skip_dirs = {'test', 'tests', 'docs', 'examples'}
                try:
                    _contents = os.listdir(src)
                except OSError:
                    _contents = []
                # Separate subdirectories from files
                _sub_dirs = sorted(
                    [c for c in _contents if os.path.isdir(os.path.join(src, c))],
                    reverse=True)
                _files = [c for c in _contents
                          if os.path.isfile(os.path.join(src, c))]
                _nested_found = False
                # Mode A: nested __init__.py in subdirectories
                for _sub in _sub_dirs:
                    if _sub.lower() in _skip_dirs:
                        continue
                    _sub_path = os.path.join(src, _sub)
                    if os.path.isfile(os.path.join(_sub_path, "__init__.py")):
                        _dst_sub = os.path.join(target_dir, _sub)
                        if not os.path.exists(_dst_sub):
                            try:
                                shutil.move(_sub_path, _dst_sub)
                                _nested_found = True
                                any_moved = True
                            except OSError:
                                pass
                # Mode B: single-file module (no __init__.py, .py files directly
                # in this directory, e.g. regex_3/regex.py)
                if not _nested_found and not os.path.isfile(
                        os.path.join(src, "__init__.py")):
                    _has_module_py = any(
                        f.endswith('.py') and not f.startswith('_')
                        for f in _files)
                    if _has_module_py:
                        _dst_sub = os.path.join(target_dir, call_module)
                        os.makedirs(_dst_sub, exist_ok=True)
                        for _f in _files:
                            if _f.endswith(('.py', '.so', '.pyd')):
                                _s_src = os.path.join(src, _f)
                                _s_dst = os.path.join(_dst_sub, _f)
                                if not os.path.exists(_s_dst):
                                    try:
                                        shutil.move(_s_src, _s_dst)
                                        any_moved = True
                                    except OSError:
                                        pass
                        _nested_found = True
            elif os.path.isfile(src) and item.endswith(".py") and item != "setup.py":
                try:
                    shutil.move(src, dst)
                    any_moved = True
                except OSError:
                    pass
        # Fallback: recursively find .py files in non-package subdirs.
        # Allowed for wheel (structured), refused for sdist (tests/docs mixed in).
        if not any_moved and allow_recursive_fallback:
            for root, dirs, files in os.walk(source_dir):
                for f in files:
                    if f.endswith(".py") and f != "setup.py":
                        src_f = os.path.join(root, f)
                        dst_f = os.path.join(target_dir, os.path.relpath(src_f, source_dir))
                        os.makedirs(os.path.dirname(dst_f), exist_ok=True)
                        try:
                            shutil.copy2(src_f, dst_f)
                            any_moved = True
                        except OSError:
                            pass
        return any_moved

    moved = False
    if has_lib_layout:
        moved = _move_items(lib_dir, allow_recursive_fallback=is_wheel) or moved
    if has_src_layout:
        moved = _move_items(src_dir, allow_recursive_fallback=is_wheel) or moved
    if not moved:
        moved = _move_items(extract_dir, allow_recursive_fallback=is_wheel)

    # top_level.txt whitelist may fail when import name differs from
    # directory name (e.g. "cv2" vs "opencv_python").  Fall back.
    # Only wheel can relax the whitelist — sdist would pull in tests/docs.
    if whitelist and not moved and is_wheel:
        whitelist_orig = whitelist
        whitelist = None
        if has_lib_layout:
            moved = _move_items(lib_dir, allow_recursive_fallback=True) or moved
        if has_src_layout:
            moved = _move_items(src_dir, allow_recursive_fallback=True) or moved
        if not moved:
            moved = _move_items(extract_dir, allow_recursive_fallback=True)
        whitelist = whitelist_orig

    # Move .dist-info, .egg-info, .data metadata
    for item in sorted(os.listdir(extract_dir)):
        if item.startswith("."):
            continue
        src = os.path.join(extract_dir, item)
        dst = os.path.join(target_dir, item)
        if os.path.exists(dst):
            continue
        if os.path.isdir(src) and item.endswith((".dist-info", ".egg-info", ".data")):
            try:
                shutil.move(src, dst)
            except (shutil.Error, OSError):
                pass

    return any(f.endswith('.py') for _, _, files in os.walk(target_dir) for f in files)


def _keep_dist_info(target_dir, package_name, version):
    """Verify .dist-info/ is preserved for METADATA access."""
    dist_dir = os.path.join(target_dir, f"{norm_pkg(package_name)}-{version}.dist-info")
    return os.path.isdir(dist_dir)


# ---------------------------------------------------------------------------
# Call module detection
# ---------------------------------------------------------------------------


# Known library → module name mappings where the PyPI name differs from
# the import name and cannot be detected via top_level.txt.  Used to
# decide whether the call_module source is trusted for .complete marker.
_CALL_MODULE_MAP = {
    'scikit-learn': 'sklearn',
    'pillow': 'PIL',
    'grpcio': 'grpc',
    'absl-py': 'absl',
    'pytorch-lightning': 'pytorch_lightning',
    'opencv-python': 'cv2',
    'scikit-image': 'skimage',
    'tensorboardx': 'tensorboardX',
    'python-dateutil': 'dateutil',
    'python-dotenv': 'dotenv',
    'pysocks': 'socks',
    'python-gflags': 'gflags',
    'websocket-client': 'websocket',
    'nvidia-ml-py3': 'pynvml',
    'greenlet': 'greenlet',
}


def _read_top_level_txt(target_dir, package_name):
    """Read top_level.txt from .dist-info/ (PEP 427) to get module name.

    Multi-entry: prefers package name match, then most .py files.
    Returns module name or None.
    """
    if not os.path.isdir(target_dir):
        return None
    for d in os.listdir(target_dir):
        if d.endswith(".dist-info"):
            tl_path = os.path.join(target_dir, d, "top_level.txt")
            if not os.path.isfile(tl_path):
                continue
            try:
                with open(tl_path) as f:
                    entries = [l.strip() for l in f if l.strip()]
            except OSError:
                continue
            if not entries:
                continue
            if len(entries) == 1:
                return entries[0]
            norm_pkg = package_name.replace("-", "_")
            matching = [e for e in entries if e.replace("-", "_") == norm_pkg]
            if matching:
                return matching[0]
            def _py_count(e):
                epath = os.path.join(target_dir, e)
                if os.path.isdir(epath):
                    return sum(1 for _, _, fs in os.walk(epath) for f in fs if f.endswith(".py"))
                return 0
            return max(entries, key=_py_count)
    return None


def _detect_call_module(target_dir, fallback):
    """Auto-detect module from extracted source: __init__.py dir, then matching .py file."""
    if not os.path.isdir(target_dir):
        return fallback
    _ignore = {"tests", "test", "docs", "examples", "example",
               "benchmarks", "benchmark", "ez_setup", "scripts", "tools"}
    # Prefer directory whose name matches the expected package
    _fallback_norm = fallback.replace('-', '_')
    for d in sorted(os.listdir(target_dir)):
        if d.startswith(".") or d.endswith((".dist-info", ".egg-info", ".data")):
            continue
        full = os.path.join(target_dir, d)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
            if d.replace('-', '_') == _fallback_norm:
                return d
    # Any other __init__.py directory (excluding known non-module dirs)
    for d in sorted(os.listdir(target_dir)):
        if d.startswith(".") or d.endswith((".dist-info", ".egg-info", ".data")):
            continue
        if d.lower() in _ignore:
            continue
        full = os.path.join(target_dir, d)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
            return d
    for d in sorted(os.listdir(target_dir)):
        if d.startswith("."):
            continue
        full = os.path.join(target_dir, d)
        if os.path.isfile(full) and d.endswith(".py") and d != "setup.py":
            name = d[:-3]
            if name.replace('-', '_') == fallback.replace('-', '_'):
                return name
    # Namespace package: dir without __init__.py but containing subdirs with __init__.py
    # (e.g., google/ has google/protobuf/__init__.py but no google/__init__.py)
    for d in sorted(os.listdir(target_dir)):
        if d.startswith(".") or d.endswith((".dist-info", ".egg-info", ".data")):
            continue
        if d.lower() in _ignore:
            continue
        full = os.path.join(target_dir, d)
        if os.path.isdir(full):
            for sub in os.listdir(full):
                if os.path.isfile(os.path.join(full, sub, "__init__.py")):
                    return d
    return fallback


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Multiprocessing worker logging
# ---------------------------------------------------------------------------

_worker_knowledge_path = None


def _set_worker_knowledge_path(path):
    global _worker_knowledge_path
    _worker_knowledge_path = path


def _init_worker():
    """Configure logging for Pool worker processes."""
    import faulthandler, signal
    faulthandler.enable()
    # Dump stacks on SIGUSR1 for manual inspection
    signal.signal(signal.SIGUSR1,
                  lambda *_: faulthandler.dump_traceback())
    if _worker_knowledge_path:
        log_file = os.path.join(_worker_knowledge_path, "knowledge_acquisition.log")
        h = logging.FileHandler(log_file, mode='a')
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.INFO)


def _mark_no_source(target_dir, reason=""):
    """Write ``.no_source`` marker **inside** *target_dir* with optional *reason*.

    Creates the directory if it does not exist (Point 8).
    """
    os.makedirs(target_dir, exist_ok=True)
    marker = os.path.join(target_dir, ".no_source")
    with open(marker, "w") as f:
        if reason:
            f.write(reason + "\n")


def _check_source(td, cm):
    """Check if a Python module is already extracted in *td* under *cm* or its variants.

    Marker protocol (Point 13):
      - ``.complete`` exists → trust completed build, return True
      - ``.building`` exists without ``.complete`` → incomplete, return False
      - no markers → fall back to legacy source scan
    """
    if not os.path.isdir(td):
        return False
    # Point 13: .complete marker is authoritative for finished builds
    if os.path.isfile(os.path.join(td, ".complete")):
        return True
    # Point 13: .building without .complete → interrupted build
    if os.path.isfile(os.path.join(td, ".building")):
        return False
    # 1. Flat package: __init__.py directly in target_dir
    if os.path.isfile(os.path.join(td, "__init__.py")):
        return True
    # 2. Try naming variants including namespace paths (zope.event → zope/event)
    names = {cm, cm.replace('-', '_'), cm.replace('_', '-')}
    for n in list(names):
        names.add(n.replace('.', '/'))
    for name in names:
        p = os.path.join(td, name)
        if os.path.isdir(p) or os.path.isfile(p + ".py"):
            return True
    # 3. Check top_level.txt for packages with different module names (e.g. cv2 ← opencv-contrib-python)
    for d in os.listdir(td):
        if d.endswith(".dist-info"):
            tl = os.path.join(td, d, "top_level.txt")
            if os.path.isfile(tl):
                try:
                    with open(tl) as f:
                        for entry in f:
                            entry = entry.strip()
                            if entry:
                                ep = os.path.join(td, entry)
                                if os.path.isdir(ep) or os.path.isfile(ep + ".py"):
                                    return True
                except OSError:
                    pass
            break
    return False


def download_pypi_source(package_name, version=None, python_version="3.7", output_dir="."):
    norm_name = norm_pkg(package_name)
    norm_ver_name = norm_ver(version)
    target_dir = f"{library_path_prefix}{norm_name}/{norm_name}{norm_ver_name}"
    call_module = get_library_call_module(package_name)

    if _check_source(target_dir, call_module):
        _stats["skipped"] += 1
        return

    # backward compat: check old KB underscore path
    _resolved_lib = resolve_pkg_dir(package_name, library_path_prefix)
    if _resolved_lib != norm_name:
        _old_dir = f"{library_path_prefix}{_resolved_lib}/{_resolved_lib}{norm_ver_name}"
        if _check_source(_old_dir, call_module):
            _stats["skipped"] += 1
            return

    if os.path.exists(os.path.join(target_dir, ".no_source")) or (
            _resolved_lib != norm_name and
            os.path.exists(os.path.join(f"{library_path_prefix}{_resolved_lib}/"
                                        f"{_resolved_lib}{norm_ver_name}",
                                        ".no_source"))):
        _stats["skipped"] += 1
        return

    os.makedirs(target_dir, exist_ok=True)

    # Point 13: mark build as in-progress
    building_marker = os.path.join(target_dir, ".building")
    complete_marker = os.path.join(target_dir, ".complete")
    no_source_marker = os.path.join(target_dir, ".no_source")
    for m in (complete_marker, no_source_marker):
        if os.path.exists(m):
            os.remove(m)
    with open(building_marker, "w") as f:
        f.write(f"started {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")

    url_sources = []
    urls = _select_download_urls(package_name, version, python_version)
    if urls:
        for u in urls:
            fname = os.path.basename(u.split("#")[0].split("?")[0])
            url_sources.append((u, fname.endswith(".whl")))
    else:
        url_sources = _build_fallback_candidates(package_name, version, python_version)

    if not url_sources:
        if os.path.exists(building_marker):
            os.remove(building_marker)
        _mark_no_source(target_dir, "no URL candidates")
        _stats["failed"] += 1
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_path = None
        artifact_is_wheel = False
        extract_dir = os.path.join(tmpdir, "extract")

        for url, _is_wheel in url_sources:
            fname = os.path.basename(url.split("#")[0].split("?")[0])
            existing = os.path.join(target_dir, fname)
            extract_tmp = os.path.join(tmpdir, "e")

            if os.path.exists(existing):
                if os.path.exists(extract_tmp):
                    shutil.rmtree(extract_tmp)
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                tmp_archive = os.path.join(tmpdir, fname)
                shutil.copy2(existing, tmp_archive)
                _extract_archive(tmp_archive, extract_dir)
                if any(f.endswith('.py') for _, _, files in os.walk(extract_dir) for f in files):
                    artifact_path = existing
                    artifact_is_wheel = _is_wheel
                    _stats["skipped"] += 1
                    break
                else:
                    os.remove(existing)
                    continue

            dl_path = os.path.join(tmpdir, fname)
            downloaded = False
            for attempt in range(3):
                try:
                    r = requests.get(url, timeout=7200, stream=True)
                    if r.status_code == 200:
                        expected_size = int(r.headers.get('Content-Length', 0))
                        actual_size = 0
                        with open(dl_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                                actual_size += len(chunk)
                        if expected_size > 0 and actual_size != expected_size:
                            logging.warning("Content-Length mismatch for %s: expected %d, got %d",
                                            url[:80], expected_size, actual_size)
                        downloaded = True
                        break
                except requests.ConnectionError:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                except OSError:
                    break
            if not downloaded:
                continue

            saved = os.path.join(target_dir, fname)
            shutil.copy2(dl_path, saved)

            if os.path.exists(extract_tmp):
                shutil.rmtree(extract_tmp)
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir)
            tmp_archive = os.path.join(tmpdir, fname)
            shutil.copy2(saved, tmp_archive)
            _extract_archive(tmp_archive, extract_dir)
            if any(f.endswith('.py') for _, _, files in os.walk(extract_dir) for f in files):
                artifact_path = saved
                artifact_is_wheel = _is_wheel
                break
            else:
                os.remove(saved)

        if artifact_path is None:
            if os.path.exists(building_marker):
                os.remove(building_marker)
            _mark_no_source(target_dir, "no artifact with .py files from any URL")
            _stats["failed"] += 1
            return

        _promote_purelib(extract_dir)
        if _install_source(target_dir, extract_dir, call_module, artifact_is_wheel):
            detected = _read_top_level_txt(target_dir, package_name)
            if detected:
                if os.path.isdir(os.path.join(target_dir, detected)) or \
                   os.path.isfile(os.path.join(target_dir, detected + ".py")):
                    call_module = detected
            if not os.path.isdir(os.path.join(target_dir, call_module)) and \
               not os.path.isfile(os.path.join(target_dir, call_module + ".py")):
                call_module = _detect_call_module(target_dir, call_module)
            _has_valid_module = (
                os.path.isdir(os.path.join(target_dir, call_module)) or
                os.path.isfile(os.path.join(target_dir, call_module + ".py")) or
                os.path.isfile(os.path.join(target_dir, "__init__.py"))  # flat package
            )
            if not _has_valid_module:
                # No valid Python module extracted (C extension or similar)
                if os.path.exists(building_marker):
                    os.remove(building_marker)
                _mark_no_source(target_dir, "no valid Python module — C extension or similar")
                _stats["failed"] += 1
            else:
                _keep_dist_info(target_dir, package_name, version)
                # Point 13: mark build as complete
                if os.path.exists(building_marker):
                    os.remove(building_marker)
                with open(complete_marker, "w") as _:
                    pass
                _stats["downloaded"] += 1
                # Point 25: check for empty source (all .py files are 0 bytes)
                # Common in CUDA metapackages (nvidia-cublas-cu11 etc.)
                _py_total = 0
                for _root, _dirs, _files in os.walk(target_dir):
                    for _f in _files:
                        if _f.endswith('.py'):
                            try:
                                _py_total += os.path.getsize(
                                    os.path.join(_root, _f))
                            except OSError:
                                pass
                if _py_total == 0:
                    # Remove large .whl/.tar.gz archives
                    for _f in os.listdir(target_dir):
                        if _f.endswith(('.whl', '.tar.gz', '.zip',
                                        '.tgz', '.tar.bz2')):
                            try:
                                os.remove(os.path.join(target_dir, _f))
                            except OSError:
                                pass
                    # Remove empty .py files
                    for _root, _dirs, _files in os.walk(target_dir):
                        for _f in _files:
                            if _f.endswith('.py'):
                                try:
                                    os.remove(os.path.join(_root, _f))
                                except OSError:
                                    pass
                    if os.path.exists(complete_marker):
                        os.remove(complete_marker)
                    _mark_no_source(target_dir,
                                    "all .py files are empty — binary-only package")
                    _stats["failed"] += 1
                    _stats["downloaded"] -= 1
                    return
        else:
            if os.path.exists(building_marker):
                os.remove(building_marker)
            _mark_no_source(target_dir, "_install_source failed")
            _stats["failed"] += 1


# ---------------------------------------------------------------------------
# library_version persistence
# ---------------------------------------------------------------------------

def _write_library_version(pkg, python_version, compatible_versions):
    """Atomically update library_version.json with merge semantics.

    Uses a UUID-based tmp file + os.replace for atomic write.
    No lock — single-process KB build, lock residue risk outweighs benefit.
    """
    lv_path = f"{version_path_prefix}library_version.json"

    if os.path.exists(lv_path):
        with open(lv_path, "r") as f:
            data = json.load(f)
    else:
        data = {}
    norm_pkg_name = norm_pkg(pkg)
    if norm_pkg_name not in data:
        data[norm_pkg_name] = {}
    if not compatible_versions and data[norm_pkg_name].get(python_version):
        # guard: don't overwrite existing version data with empty list
        # (e.g. when PyPI API is throttled)
        logging.warning("Keeping existing %d versions for %s, got empty from PyPI",
                        len(data[norm_pkg_name][python_version]), pkg)
    else:
        data[norm_pkg_name][python_version] = [norm_ver(v) for v in compatible_versions]
    tmp_path = f"{lv_path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, lv_path)
    except (OSError, TypeError):
        _stats["crashed"] += 1
        logging.exception("library_version.json write failed for %s", pkg)


# ---------------------------------------------------------------------------
# Knowledge extraction
# ---------------------------------------------------------------------------

def _get_all_modules(target_dir, lib):
    """Return all valid module names from top_level.txt, or auto-detect.

    Returns a list of module names that actually exist in target_dir.
    """
    modules = []
    if os.path.isdir(target_dir):
        tl_path = None
        for d in os.listdir(target_dir):
            if d.endswith(".dist-info"):
                tl_path = os.path.join(target_dir, d, "top_level.txt")
                break
        entries = []
        if tl_path and os.path.isfile(tl_path):
            try:
                with open(tl_path) as f:
                    entries = [l.strip() for l in f if l.strip()]
            except OSError:
                pass
        if not entries:
            entries = [get_library_call_module(lib)]
        seen = set()
        for e in entries:
            root = e.split('/')[0]
            if root in seen:
                continue
            if os.path.isdir(os.path.join(target_dir, root)) or \
               os.path.isfile(os.path.join(target_dir, root + ".py")):
                modules.append(root)
                seen.add(root)
    if not modules:
        modules = [get_library_call_module(lib)]
    return modules


def extract_fine_grained_knowledge(lib, version):
    norm_lib = resolve_pkg_dir(lib, library_path_prefix)
    norm_ver_name = norm_ver(version)
    target_dir = f"{library_path_prefix}{norm_lib}/{norm_lib}{norm_ver_name}"
    all_modules = _get_all_modules(target_dir, lib)

    merged = {"functions": {}, "classes": {}, "methods": {},
              "modules": [], "api_usage": [], "global_vars": []}

    for call_module in all_modules:
        library_path = f"{target_dir}/{call_module}"

        if not os.path.isdir(library_path):
            py_file = library_path + ".py"
            if os.path.isfile(py_file):
                try:
                    with open(py_file) as f:
                        tree = ast.parse(f.read())
                except Exception:
                    continue
                merged["modules"].append(call_module)
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef):
                        merged["functions"][f"{call_module}.{node.name}"] = {}
                    elif isinstance(node, ast.ClassDef):
                        merged["classes"][f"{call_module}.{node.name}"] = {}
                continue
            # Flat package: files directly in target_dir (e.g. imageio 0.2.3)
            if os.path.isfile(f"{target_dir}/__init__.py"):
                library_path = target_dir
            else:
                continue

        res = extract_from_directory(library_path)
        dir_mods = get_python_modules_and_packages_from_dir(library_path, call_module)
        init_mods = get_python_modules_and_packages_from_init(library_path, call_module)
        dir_mods.update(init_mods)
        merged["modules"].extend(dir_mods)
        try:
            api_usage_in_target_library, _, __, ___ = get_all_used_api(library_path, call_module)
        except Exception:
            api_usage_in_target_library = []
        merged["api_usage"].extend(api_usage_in_target_library)
        funcs = res["functions"]
        new_funcs = shortenPath(funcs, lib, version, library_path_prefix)
        classes = res["classes"]
        new_classes = shortenPath(classes, lib, version, library_path_prefix)
        # P45: add public API aliases for _core source modules
        source_path = resolve_library_source_path(
            library_path_prefix, lib, version, call_module)
        source_module_name = os.path.basename(source_path)
        if source_module_name != call_module:
            new_funcs = add_public_api_aliases(
                new_funcs, source_module_name, call_module)
            new_classes = add_public_api_aliases(
                new_classes, source_module_name, call_module)
        merged["functions"].update(new_funcs)
        merged["classes"].update(new_classes)
        merged["methods"].update(res.get("methods", {}))
        merged["global_vars"].extend(res.get("global_vars", []))

    if not merged["modules"] and not merged["functions"] and not merged["classes"]:
        return
    norm_lib = resolve_pkg_dir(lib, api_path_prefix)
    os.makedirs(f"{api_path_prefix}{norm_lib}/", exist_ok=True)
    out_path = f"{api_path_prefix}{norm_lib}/{norm_ver_name}.json"
    tmp_path = f"{out_path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(merged, f)
    os.replace(tmp_path, out_path)

    # Write .complete sidecar when call module source is trusted
    # (from top_level.txt or a known hand-coded mapping).
    if _read_top_level_txt(target_dir, lib) is not None or \
       lib in _CALL_MODULE_MAP:
        with open(out_path + ".complete", "w") as _:
            pass


def task(args):
    lib, version = args
    logging.info("Task start: %s==%s", lib, version)
    try:
        extract_fine_grained_knowledge(lib, version)
        logging.info("Task done: %s==%s", lib, version)
        return (lib, version, True)
    except Exception:
        logging.exception("Task failed for %s==%s", lib, version)
        return (lib, version, False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    start = time.time()

    parser = argparse.ArgumentParser(description="命令行工具示例")
    parser.add_argument('--config', type=str, required=True, help="配置文件路径")
    args = parser.parse_args()
    config_path = args.config

    config = load_config(config_path)

    proj_path = config["projPath"]
    target_project = proj_path.split("/")[-1]
    target_library = config["targetLibrary"]
    start_version = config["startVersion"]
    target_version = config["targetVersion"]
    python_version = config["pythonVersion"]
    start_requirements_path = config["requirementsPath"]
    knowledge_path = config["knowledgePath"].rstrip("/") + "/"
    library_path_prefix = f"{knowledge_path}libraries/"
    constraint_path_prefix = f"{knowledge_path}version_constraint/"
    version_path_prefix = f"{knowledge_path}"
    api_path_prefix = f"{knowledge_path}library_api/"
    setup_path(library_path_prefix, constraint_path_prefix, version_path_prefix, api_path_prefix)

    # auto-create knowledge directories
    for p in [knowledge_path, library_path_prefix, constraint_path_prefix, api_path_prefix]:
        os.makedirs(p, exist_ok=True)

    # logging: INFO+ to file, WARNING+ to console
    log_file = os.path.join(knowledge_path, "knowledge_acquisition.log")
    fh = logging.FileHandler(log_file, mode='a')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(ch)
    _run_id = uuid.uuid4().hex[:8]
    _error_breakdown = {}  # lib → set of failed versions
    _phase_start_time = time.time()
    logging.info("=" * 70)
    logging.info("Build: %s | %s %s->%s | py%s | run=%s",
                 target_project, target_library, start_version, target_version,
                 python_version, _run_id)
    logging.info("=" * 70)

    fake_start_proj_dependency = get_proj_dependency_from_requirements(start_requirements_path)
    start_proj_dependency = {}
    for i in fake_start_proj_dependency:
        if fake_start_proj_dependency[i] == "0.0.0" or fake_start_proj_dependency[i] == "0.0" or fake_start_proj_dependency[i] == "0":
            pass
        else:
            start_proj_dependency[i] = fake_start_proj_dependency[i]

    target_proj_dependency = start_proj_dependency.copy()
    target_proj_dependency[target_library] = target_version
    FDG = get_FDG_from_requirements(target_proj_dependency, python_version)
    sub_graph = get_sub_graph(FDG, target_library)

    all_packages = set(sub_graph) | set(target_proj_dependency.keys())

    # Phase 1: download source and constraint data for each compatible version
    try:
        # Phase 1: download source and constraint data for each compatible version
        logging.info("Phase 1: downloading %d packages", len(all_packages))
        pkg_total = len(all_packages)
        # Read library_version.json for per-package cache lookup
        try:
            with open(f"{version_path_prefix}library_version.json", 'r') as _lv_f:
                _version_ls = json.load(_lv_f)
        except (json.JSONDecodeError, OSError, FileNotFoundError):
            _version_ls = {}
        # Read .phase1_done for per-package PyPI skip (same pattern as .phase2_done)
        _phase1_done_path = f"{version_path_prefix}.phase1_done"
        try:
            with open(_phase1_done_path) as _p1_f:
                _phase1_done = json.load(_p1_f)
        except (FileNotFoundError, json.JSONDecodeError):
            _phase1_done = {}
        for pkg_idx, pkg in enumerate(sorted(all_packages)):
            # Check .phase1_done: if all versions previously downloaded, skip PyPI
            _pkg_key = norm_pkg(pkg)
            _cached_versions = _version_ls.get(_pkg_key, {}).get(python_version, [])
            if _cached_versions and _phase1_done.get(_pkg_key, {}).get(python_version):
                compatible_versions = _cached_versions
                n_vers = len(compatible_versions)
                _write_library_version(pkg, python_version, compatible_versions)
                logging.info("Phase 1 [%d/%d]: %s (%d versions) — phase1_done cached",
                             pkg_idx + 1, pkg_total, pkg, n_vers)
                continue
            compatible_versions = get_compatible_versions(pkg, python_version)
            n_vers = len(compatible_versions)
            _dl_before = _stats["downloaded"]
            for ver_idx, ver in enumerate(compatible_versions):
                # Check if constraint JSON already exists (delegates to
                # try_read_constraint_json which handles name+version fallbacks)
                _data, __ = try_read_constraint_json(pkg, ver)
                if _data is None:
                    download_from_data(pkg, ver)
                try:
                    download_pypi_source(pkg, ver, python_version)
                except Exception:
                    _stats["crashed"] += 1
                    _error_breakdown.setdefault(pkg, set()).add(ver)
                    logging.exception("download_pypi_source crashed for %s==%s", pkg, ver)
                _dl_during = _stats["downloaded"] - _dl_before
                _vers_total = ver_idx + 1
                if n_vers >= 40 and _dl_during > 0 and (
                        (_vers_total % 40 == 0 or ver_idx == n_vers - 1)):
                    logging.info("  %s: %d/%d versions | dl:%d skip:%d",
                                 pkg, _vers_total, n_vers, _dl_during,
                                 _vers_total - _dl_during)
            # Point 25: filter out .no_source versions from library_version.json
            _filtered = [v for v in compatible_versions
                         if not os.path.exists(
                             f"{library_path_prefix}{resolve_pkg_dir(pkg, library_path_prefix)}/"
                             f"{resolve_pkg_dir(pkg, library_path_prefix)}{norm_ver(v)}"
                             "/.no_source")]
            _write_library_version(pkg, python_version, _filtered)
            # Mark Phase 1 done for this package+pyVer if all filtered
            # versions have source (guard: only mark complete downloads)
            _pkg_done = bool(_filtered)
            if _pkg_done:
                _norm_pkg_dir = resolve_pkg_dir(pkg, library_path_prefix)
                for _v in _filtered:
                    _ver_dir = f"{library_path_prefix}{_norm_pkg_dir}/{_norm_pkg_dir}{norm_ver(_v)}"
                    if not (os.path.exists(os.path.join(_ver_dir, ".complete"))
                            or os.path.exists(os.path.join(_ver_dir, ".no_source"))):
                        _pkg_done = False
                        break
            if _pkg_done:
                _phase1_done.setdefault(_pkg_key, {})[python_version] = True
            _dl_delta = _stats["downloaded"] - _dl_before
            if _dl_delta == 0:
                logging.info("Phase 1 [%d/%d]: %s (%d versions) — cached",
                             pkg_idx + 1, pkg_total, pkg, n_vers)
            elif n_vers < 40:
                logging.info("Phase 1 [%d/%d]: %s (%d versions) — %d dl",
                             pkg_idx + 1, pkg_total, pkg, n_vers, _dl_delta)

        _phase1_elapsed = time.time() - _phase_start_time
        logging.info("Phase 1 complete: %d packages | dl:%d fail:%d skip:%d crash:%d | %.0fs",
                     pkg_total, _stats["downloaded"], _stats["failed"],
                     _stats["skipped"], _stats["crashed"], _phase1_elapsed)
        # Persist .phase1_done (same atomic pattern as .phase2_done)
        with open(_phase1_done_path + ".tmp", "w") as _p1_tmp:
            json.dump(_phase1_done, _p1_tmp, sort_keys=True)
        os.replace(_phase1_done_path + ".tmp", _phase1_done_path)
        _phase_start_time = time.time()

        # Phase 2: one-hop dependency discovery from known libraries
        _phase_start_time = time.time()
        logging.info("Phase 2: discovering one-hop dependencies from %d known libs",
                     len(all_packages))
        try:
            with open(f"{version_path_prefix}library_version.json", 'r') as file:
                version_ls = json.load(file)
        except (json.JSONDecodeError, OSError):
            _stats["crashed"] += 1
            logging.exception("Corrupted library_version.json, resetting")
            version_ls = {}
        known_libs = set(all_packages)
        discovered = set()
        # Point 33: incremental phase-2 discovery — skip libs/versions
        # already processed in previous runs, tracked via .phase2_done
        _phase2_done_path = f"{version_path_prefix}.phase2_done"
        try:
            with open(_phase2_done_path) as f:
                phase2_done = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            phase2_done = {}
        for lib in list(all_packages):
            known_versions = set(version_ls.get(lib, {}).get(python_version, []))
            processed_versions = set(
                phase2_done.get(lib, {}).get(python_version, []))
            new_versions = known_versions - processed_versions
            if not new_versions:
                continue
            for ver in sorted(new_versions):
                constraint = get_library_constraint_from_metadata(lib, ver, python_version)
                for dep in constraint:
                    base_dep = dep.split('[')[0].lower().replace('_', '-')
                    if base_dep not in known_libs and base_dep not in discovered:
                        discovered.add(base_dep)
            phase2_done.setdefault(lib, {})[python_version] = sorted(known_versions)
        # Persist updated tracking file
        with open(_phase2_done_path + ".tmp", "w") as f:
            json.dump(phase2_done, f, sort_keys=True)
        os.replace(_phase2_done_path + ".tmp", _phase2_done_path)
        # Download and register newly discovered libraries
        _disc_total = len(discovered)
        for _disc_idx, dep in enumerate(sorted(discovered)):
            compatible_versions = get_compatible_versions(dep, python_version)
            n_dep_vers = len(compatible_versions)
            if not compatible_versions:
                logging.info("Phase 2 [%d/%d]: %s — no compatible versions",
                             _disc_idx + 1, _disc_total, dep)
                continue
            # Point 26: skip binary-only transitive deps (no sdist on PyPI)
            _has_sdist = False
            try:
                _r = requests.get(f"https://pypi.org/pypi/{dep}/json",
                                  timeout=30)
                if _r.status_code == 200:
                    _urls = _r.json().get("urls", [])
                    _has_sdist = any(
                        u.get("packagetype") == "sdist" for u in _urls)
            except requests.RequestException:
                pass
            if not _has_sdist:
                logging.info("Phase 2 [%d/%d]: %s — binary-only, skipping",
                             _disc_idx + 1, _disc_total, dep)
                continue
            _dl_before = _stats["downloaded"]
            for ver_idx, ver in enumerate(compatible_versions):
                _dep_json = f"{constraint_path_prefix}{dep}/{dep}{ver}/{dep}.json"
                if not os.path.exists(_dep_json):
                    _resolved_dep = resolve_pkg_dir(dep, constraint_path_prefix)
                    if _resolved_dep != dep:
                        _old_dep = f"{constraint_path_prefix}{_resolved_dep}/{_resolved_dep}{ver}/{_resolved_dep}.json"
                        if not os.path.exists(_old_dep):
                            download_from_data(dep, ver)
                    else:
                        download_from_data(dep, ver)
                try:
                    download_pypi_source(dep, ver, python_version)
                except Exception:
                    _stats["crashed"] += 1
                    logging.error("download_pypi_source crashed for %s==%s", dep, ver)
                _dl_during = _stats["downloaded"] - _dl_before
                _vers_total = ver_idx + 1
                if n_dep_vers >= 40 and _dl_during > 0 and (
                        _vers_total % 40 == 0 or ver_idx == n_dep_vers - 1):
                    logging.info("  %s: %d/%d versions | dl:%d skip:%d",
                                 dep, _vers_total, n_dep_vers,
                                 _dl_during, _vers_total - _dl_during)
            # Point 25: filter out .no_source versions from library_version.json
            _filtered = [v for v in compatible_versions
                         if not os.path.exists(
                             f"{library_path_prefix}{resolve_pkg_dir(dep, library_path_prefix)}/"
                             f"{resolve_pkg_dir(dep, library_path_prefix)}{norm_ver(v)}"
                             "/.no_source")]
            _write_library_version(dep, python_version, _filtered)
            # Mark Phase 1 done for discovered packages too (cross-project cache)
            _dep_done = bool(_filtered)
            if _dep_done:
                _dep_pkg_dir = resolve_pkg_dir(dep, library_path_prefix)
                for _v in _filtered:
                    _ver_dir = f"{library_path_prefix}{_dep_pkg_dir}/{_dep_pkg_dir}{norm_ver(_v)}"
                    if not (os.path.exists(os.path.join(_ver_dir, ".complete"))
                            or os.path.exists(os.path.join(_ver_dir, ".no_source"))):
                        _dep_done = False
                        break
            if _dep_done:
                _phase1_done.setdefault(norm_pkg(dep), {})[python_version] = True
            all_packages.add(dep)
            _dl_delta = _stats["downloaded"] - _dl_before
            if _dl_delta == 0:
                logging.info("Phase 2 [%d/%d]: %s (%d versions) — cached",
                             _disc_idx + 1, _disc_total, dep, n_dep_vers)
            else:
                logging.info("Phase 2 [%d/%d]: %s (%d versions) — %d dl",
                             _disc_idx + 1, _disc_total, dep, n_dep_vers, _dl_delta)

        logging.info("Phase 2 complete: discovered %d one-hop dependencies | %.0fs",
                     len(discovered), time.time() - _phase_start_time)
        # Persist .phase1_done again (Phase 2 may have added discovered packages)
        with open(_phase1_done_path + ".tmp", "w") as _p1_tmp:
            json.dump(_phase1_done, _p1_tmp, sort_keys=True)
        os.replace(_phase1_done_path + ".tmp", _phase1_done_path)

        # Phase 3: build available_versions and extract APIs
        _phase_start_time = time.time()
        available_version = get_available_version(FDG, sub_graph, python_version, target_proj_dependency, target_library, target_version)
        available_version[target_library].append(start_version)

        try:
            with open(f"{version_path_prefix}library_version.json", 'r') as file:
                version_ls = json.load(file)
        except (json.JSONDecodeError, OSError):
            version_ls = {}
        for dep in discovered:
            if dep not in available_version:
                try:
                    available_version[dep] = version_ls[dep][python_version]
                except KeyError:
                    pass

        all_library = []
        for i in all_packages:
            if i not in all_library and i in target_proj_dependency.keys():
                all_library.append(i)
        for dep in discovered:
            if dep not in all_library:
                all_library.append(dep)

        for lib in all_library:
            norm_lib = resolve_pkg_dir(lib, api_path_prefix)
            os.makedirs(f"{api_path_prefix}{norm_lib}/", exist_ok=True)
        tasks = []
        for lib in all_library:
            norm_lib = resolve_pkg_dir(lib, api_path_prefix)
            for ver in available_version.get(lib, []):
                nv = norm_ver(ver)
                json_path = f"{api_path_prefix}{norm_lib}/{nv}.json"
                # Point 25: skip .no_source versions — no Python source available
                _nl_src = resolve_pkg_dir(lib, library_path_prefix)
                _src_dir = f"{library_path_prefix}{_nl_src}/{_nl_src}{nv}"
                if os.path.exists(os.path.join(_src_dir, ".no_source")):
                    continue
                # Step 1: .complete marker — trusted call module source → skip
                if os.path.exists(json_path + ".complete"):
                    continue
                # Step 1.5: weak consistency check for legacy JSON
                # Rebuild when call_module or top_level.txt contradicts JSON modules.
                _force_rebuild = False
                try:
                    if os.path.getsize(json_path) >= 10:
                        with open(json_path, 'r') as f:
                            _legacy_data = json.load(f)
                        _legacy_modules = set(
                            m.lower().replace("-", "_")
                            for m in (_legacy_data.get("modules") or [])
                        )
                        if _legacy_modules:
                            _norm_lib_src = resolve_pkg_dir(lib, library_path_prefix)
                            _src_dir = f"{library_path_prefix}{_norm_lib_src}/{_norm_lib_src}{nv}"
                            _public_roots = set()
                            _tl = _read_top_level_txt(_src_dir, lib)
                            if _tl:
                                _public_roots.add(_tl.lower().replace("-", "_"))
                            if lib in _CALL_MODULE_MAP:
                                _public_roots.add(
                                    _CALL_MODULE_MAP[lib].lower().replace("-", "_"))
                            if _public_roots and not (_legacy_modules & _public_roots):
                                _force_rebuild = True
                except (json.JSONDecodeError, OSError, KeyError, FileNotFoundError):
                    pass
                if _force_rebuild:
                    tasks.append((lib, ver))
                    continue
                # Step 2: legacy check — existing JSON with content → skip
                try:
                    if os.path.getsize(json_path) >= 10:
                        with open(json_path, 'r') as f:
                            data = json.load(f)
                        if any(data.get(k) for k in ('functions', 'classes', 'modules')):
                            continue
                except (json.JSONDecodeError, OSError, KeyError, FileNotFoundError):
                    pass
                tasks.append((lib, ver))
        pool_size = min(20, cpu_count())
        logging.info("Phase 3: extracting APIs | %d libs, %d tasks, pool=%d workers",
                     len(all_library), len(tasks), pool_size)
        _task_total = len(tasks)
        _task_done = 0
        _task_fail = 0
        sys.setrecursionlimit(5000)
        cleanup_temp_files()
        _set_worker_knowledge_path(knowledge_path)
        with Pool(processes=pool_size, initializer=_init_worker) as pool:
            for _lib, _ver, ok in pool.imap_unordered(task, tasks):
                _task_done += 1
                if not ok:
                    _task_fail += 1
                    _stats["crashed"] += 1
                    _error_breakdown.setdefault(_lib, set()).add(_ver)
                if _task_done % 50 == 0 or _task_done == _task_total:
                    logging.info("Phase 3 progress: %d/%d tasks | fail:%d | %.0fs",
                                 _task_done, _task_total, _task_fail,
                                 time.time() - _phase_start_time)

        logging.info("Phase 3 complete: %d tasks | ok:%d fail:%d | %.0fs",
                     _task_total, _task_total - _task_fail, _task_fail,
                     time.time() - _phase_start_time)
        if _error_breakdown:
            logging.warning("=== Error breakdown by library ===")
            for lib in sorted(_error_breakdown):
                vers = sorted(_error_breakdown[lib], key=parse_version)
                logging.warning("  %s: %d versions failed: %s",
                                lib, len(vers), ', '.join(vers[:10]))
                if len(vers) > 10:
                    logging.warning("    ... and %d more", len(vers) - 10)
        logging.info("Build complete: %d downloaded, %d failed, %d skipped, %d crashed | run=%s",
                     _stats["downloaded"], _stats["failed"], _stats["skipped"],
                     _stats["crashed"], _run_id)
        logging.info("=" * 70)
    except BaseException:
        try:
            kb_report_generate(knowledge_path)
        except Exception:
            logging.exception("Failed to generate KB report")
        raise
    else:
        kb_report_generate(knowledge_path)
