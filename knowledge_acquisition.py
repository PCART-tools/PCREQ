from utils.util import *
from utils.kb_report import generate as kb_report_generate
from extraction.getCall import get_all_used_api
from extraction.lib_module_and_package_extraction import *
from extraction.library_api_and_module import *
from call_graph.get_FDG import *
import platform, argparse, os, json, time, requests, logging, tempfile, uuid
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import parse as parse_version
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
                print(proj_dependency.lower())
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
                print(proj_dependency)
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
    for version, files in response["releases"].items():
        for file_info in files:
            if file_info.get("python_version"):
                try:
                    if file_info["python_version"] == f"cp{new_python_version}":
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
    if package_name == "python-dateutil" and compatible_versions[-1] == "2.9.0":
        compatible_versions.append("2.9.0.post0")
    return compatible_versions


# ---------------------------------------------------------------------------
# Download infrastructure
# ---------------------------------------------------------------------------

def _parse_wheel_tag(filename):
    """Return (priority, is_pure, py_major) from a wheel filename.

    Priority: 1=py3-none-any, 2=cpXX-none-any, 3=abi=none+platform, <0=compiled/not-wheel.
    py_major: 3 if python tag is py3/cp3X, 2 for py2/cp2X, 0 otherwise.
    """
    if not filename.endswith(".whl"):
        return -1, False, 0
    parts = filename[:-4].split("-")
    if len(parts) < 4:
        return -1, False, 0
    platform_tag = parts[-1]
    abi_tag = parts[-2]
    python_tags = parts[2:-2]
    all_tags = set()
    for t in python_tags:
        all_tags.update(t.split("."))
    py_major = 0
    for t in all_tags:
        if t == "py3" or t.startswith("cp3"):
            py_major = 3
            break
        elif t == "py2" or t.startswith("cp2"):
            py_major = 2
    is_pure = abi_tag in ("none", "abi3")
    if not is_pure:
        return -1, False, py_major
    if platform_tag == "any":
        if any(t == "py3" or t.startswith("py3") for t in all_tags):
            return 1, True, py_major
        elif any(t.startswith("cp") for t in all_tags):
            return 2, True, py_major
    return 3, True, py_major


def _select_download_urls(package_name, version, python_version):
    """Return priority-sorted download URLs from version constraint JSON."""
    json_path = f"{constraint_path_prefix}{package_name}/{package_name}{version}/{package_name}.json"
    if not os.path.exists(json_path):
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
    if sys.platform.startswith("linux"):
        plat_tag = "manylinux"
    elif sys.platform == "darwin":
        plat_tag = "macosx"
    elif sys.platform == "win32":
        plat_tag = "win"
    else:
        plat_tag = None
    scored = []
    for u in urls:
        pkg_type = u.get("packagetype")
        filename = u.get("filename", "")
        url = u.get("url", "")
        if not url:
            continue
        if filename.endswith((".exe", ".msi", ".dmg", ".rpm", ".deb")):
            continue
        # Skip Python 2 wheels (cp2*/py2*), but not universal py2.py3
        if pkg_type == "bdist_wheel" and ('cp2' in filename or 'py2' in filename) and 'py2.py3' not in filename:
            continue
        if pkg_type == "bdist_wheel":
            priority, is_pure, py_major = _parse_wheel_tag(filename)
            platform_ok = ("any" in filename) or (plat_tag and plat_tag in filename)
            if is_pure:
                if platform_ok:
                    pass
                else:
                    priority = 6
            else:
                if platform_ok:
                    if py_major == 3:
                        priority = 4
                    else:
                        priority = 5
                else:
                    priority = 7
        elif pkg_type == "sdist":
            priority = 8
        else:
            continue
        scored.append((priority, url))
    scored.sort(key=lambda x: x[0])
    return [url for _, url in scored]


def _build_fallback_candidates(package_name, version):
    """Return [(url, is_wheel), ...] from PyPI JSON API, sorted by priority."""
    candidates = []
    pypi_url = f"https://pypi.org/pypi/{package_name}/{version}/json"
    try:
        r = requests.get(pypi_url, timeout=7200)
        if r.status_code != 200:
            return candidates
        data = r.json()
        if sys.platform.startswith("linux"):
            _plat = "manylinux"
        elif sys.platform == "darwin":
            _plat = "macosx"
        elif sys.platform == "win32":
            _plat = "win"
        else:
            _plat = None
        for u in data.get("urls", []):
            fname = u.get("filename", "")
            url = u.get("url", "")
            if not url or fname.endswith((".exe", ".msi", ".dmg", ".rpm", ".deb")):
                continue
            pkg_type = u.get("packagetype", "")
            if pkg_type == "bdist_wheel":
                prio, pure, py_major = _parse_wheel_tag(fname)
                platform_ok = ("any" in fname) or (_plat and _plat in fname)
                if pure:
                    candidates.append((prio if platform_ok else 6, url, True))
                elif platform_ok:
                    if py_major == 3:
                        candidates.append((4, url, True))
                    else:
                        candidates.append((5, url, True))
                else:
                    candidates.append((7, url, True))
            elif pkg_type == "sdist":
                candidates.append((8, url, False))
        candidates.sort(key=lambda x: x[0])
    except requests.RequestException:
        pass
    return [(url, is_wheel) for _, url, is_wheel in candidates]


def _extract_archive(archive_path, target_dir):
    """Extract archive to target_dir, flattening single-top-level-dir wrappers."""
    extract_tmp = tempfile.mkdtemp(dir=os.path.dirname(target_dir),
                                    prefix=".extract-")
    if archive_path.endswith(('.tar.gz', '.tgz', '.tar.bz2')):
        with tarfile.open(archive_path) as tf:
            tf.extractall(extract_tmp)
    else:
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_tmp)
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


def _install_source(target_dir, extract_dir, call_module):
    """Move Python packages from extract_dir to target_dir.

    Uses top_level.txt as a whitelist when available (wheel/sdist with
    dist-info); otherwise falls back to __init__.py-based auto-detection.

    Returns True if at least one .py file ends up in target_dir.
    """
    os.makedirs(target_dir, exist_ok=True)

    whitelist = _read_top_level(extract_dir)

    src_dir = os.path.join(extract_dir, "src")
    has_src_layout = os.path.isdir(src_dir)

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

    def _move_items(source_dir):
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
            elif os.path.isfile(src) and item.endswith(".py") and item != "setup.py":
                try:
                    shutil.move(src, dst)
                    any_moved = True
                except OSError:
                    pass
        # Fallback: recursively find .py files in non-package subdirs
        if not any_moved:
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

    if has_src_layout:
        _move_items(src_dir)
    moved = _move_items(extract_dir)

    # top_level.txt whitelist may fail when import name differs from
    # directory name (e.g. "cv2" vs "opencv_python").  Fall back.
    if whitelist and not moved:
        if has_src_layout:
            whitelist = None
            _move_items(src_dir)
        whitelist = None
        _move_items(extract_dir)

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


def _merge_core_namespace(target_dir, call_module):
    """Merge {call_module}_core/ subpackages into {call_module}/."""
    init_path = os.path.join(target_dir, call_module, "__init__.py")
    if not os.path.isfile(init_path):
        return
    try:
        with open(init_path) as f:
            tree = ast.parse(f.read())
    except SyntaxError:
        return
    core_mod = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith(call_module + "_core"):
                core_mod = node.module.split(".")[0]
                break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(call_module + "_core"):
                    core_mod = alias.name.split(".")[0]
                    break
    if core_mod is None:
        return
    core_path = os.path.join(target_dir, core_mod)
    if not os.path.isdir(core_path):
        return
    for item in os.listdir(core_path):
        src = os.path.join(core_path, item)
        dst = os.path.join(target_dir, call_module, item)
        if os.path.exists(dst):
            continue
        shutil.move(src, dst)
    shutil.rmtree(core_path)


def _keep_dist_info(target_dir, package_name, version):
    """Verify .dist-info/ is preserved for METADATA access."""
    dist_dir = os.path.join(target_dir, f"{package_name}-{version}.dist-info")
    return os.path.isdir(dist_dir)


# ---------------------------------------------------------------------------
# Call module detection
# ---------------------------------------------------------------------------


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
                break
            try:
                with open(tl_path) as f:
                    entries = [l.strip() for l in f if l.strip()]
            except OSError:
                break
            if not entries:
                break
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
    """Auto-detect module from extracted source: first __init__.py dir, then .py file."""
    if not os.path.isdir(target_dir):
        return fallback
    for d in sorted(os.listdir(target_dir)):
        if d.startswith(".") or d.endswith((".dist-info", ".egg-info", ".data")):
            continue
        full = os.path.join(target_dir, d)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
            return d
    for d in sorted(os.listdir(target_dir)):
        if d.startswith("."):
            continue
        full = os.path.join(target_dir, d)
        if os.path.isfile(full) and d.endswith(".py") and d != "setup.py":
            return d[:-3]
    return fallback


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_pypi_source(package_name, version=None, python_version="3.7", output_dir="."):
    target_dir = f"{library_path_prefix}{package_name}/{package_name}{version}"
    call_module = get_library_call_module(package_name)

    call_path = os.path.join(target_dir, call_module)
    if os.path.isdir(call_path) or os.path.isfile(call_path + ".py"):
        _stats["skipped"] += 1
        return

    if os.path.exists(target_dir + ".no_source"):
        _stats["skipped"] += 1
        return

    os.makedirs(target_dir, exist_ok=True)

    url_sources = []
    urls = _select_download_urls(package_name, version, python_version)
    if urls:
        for u in urls:
            fname = os.path.basename(u.split("#")[0].split("?")[0])
            url_sources.append((u, fname.endswith(".whl")))
    else:
        url_sources = _build_fallback_candidates(package_name, version)

    if not url_sources:
        with open(target_dir + ".no_source", "w") as _:
            pass
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        _stats["failed"] += 1
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_path = None
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
                break
            else:
                os.remove(saved)

        if artifact_path is None:
            with open(target_dir + ".no_source", "w") as _:
                pass
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            _stats["failed"] += 1
            return

        _promote_purelib(extract_dir)
        if _install_source(target_dir, extract_dir, call_module):
            detected = _read_top_level_txt(target_dir, package_name)
            if detected:
                if os.path.isdir(os.path.join(target_dir, detected)) or \
                   os.path.isfile(os.path.join(target_dir, detected + ".py")):
                    call_module = detected
            if not os.path.isdir(os.path.join(target_dir, call_module)) and \
               not os.path.isfile(os.path.join(target_dir, call_module + ".py")):
                call_module = _detect_call_module(target_dir, call_module)
            _merge_core_namespace(target_dir, call_module)
            _keep_dist_info(target_dir, package_name, version)
            _stats["downloaded"] += 1
        else:
            with open(target_dir + ".no_source", "w") as _:
                pass
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            _stats["failed"] += 1


# ---------------------------------------------------------------------------
# library_version persistence
# ---------------------------------------------------------------------------

def _write_library_version(pkg, python_version, compatible_versions):
    """Atomically update library_version.json with merge semantics.

    Uses a lock file to protect the read-modify-write sequence and a
    UUID-based tmp file for atomic os.replace.
    """
    lv_path = f"{version_path_prefix}library_version.json"
    lock_path = lv_path + ".lock"

    # Acquire cross-process lock
    while True:
        try:
            lfd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            time.sleep(0.01)

    try:
        if os.path.exists(lv_path):
            with open(lv_path, "r") as f:
                data = json.load(f)
        else:
            data = {}
        if pkg not in data:
            data[pkg] = {}
        data[pkg][python_version] = compatible_versions
        tmp_path = f"{lv_path}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, lv_path)
        except (OSError, TypeError):
            _stats["crashed"] += 1
            logging.exception("library_version.json write failed for %s", pkg)
    finally:
        os.close(lfd)
        try:
            os.remove(lock_path)
        except OSError:
            pass


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
        for e in entries:
            if os.path.isdir(os.path.join(target_dir, e)) or \
               os.path.isfile(os.path.join(target_dir, e + ".py")):
                modules.append(e)
    if not modules:
        modules = [get_library_call_module(lib)]
    return modules


def extract_fine_grained_knowledge(lib, version):
    target_dir = f"{library_path_prefix}{lib}/{lib}{version}"
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
        merged["functions"].update(new_funcs)
        classes = res["classes"]
        new_classes = shortenPath(classes, lib, version, library_path_prefix)
        merged["classes"].update(new_classes)
        merged["methods"].update(res.get("methods", {}))
        merged["global_vars"].extend(res.get("global_vars", []))

    if not merged["modules"] and not merged["functions"] and not merged["classes"]:
        return
    os.makedirs(f"{api_path_prefix}{lib}/", exist_ok=True)
    out_path = f"{api_path_prefix}{lib}/{version}.json"
    tmp_path = f"{out_path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(merged, f)
    os.replace(tmp_path, out_path)


def task(args):
    lib, version = args
    try:
        extract_fine_grained_knowledge(lib, version)
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

    # add file logging (append across runs, INFO+ to file, WARNING+ to console)
    log_file = os.path.join(knowledge_path, "knowledge_acquisition.log")
    fh = logging.FileHandler(log_file, mode='a')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(logging.INFO)
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.WARNING)
    _run_id = uuid.uuid4().hex[:8]
    _error_breakdown = {}  # lib → set of failed versions
    _phase_start_time = time.time()
    logging.info("=== Build: %s | %s %s->%s | py%s | run=%s ===",
                 target_project, target_library, start_version, target_version,
                 python_version, _run_id)

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
        for pkg_idx, pkg in enumerate(sorted(all_packages)):
            compatible_versions = get_compatible_versions(pkg, python_version)
            for ver in compatible_versions:
                if not os.path.exists(f"{constraint_path_prefix}{pkg}/{pkg}{ver}/{pkg}.json"):
                    download_from_data(pkg, ver)
                try:
                    download_pypi_source(pkg, ver, python_version)
                except Exception:
                    _stats["crashed"] += 1
                    logging.exception("download_pypi_source crashed for %s==%s", pkg, ver)
            _write_library_version(pkg, python_version, compatible_versions)
            if (pkg_idx + 1) % 10 == 0 or pkg_idx == pkg_total - 1:
                logging.info("Phase 1 progress: %d/%d packages | dl:%d fail:%d skip:%d",
                             pkg_idx + 1, pkg_total, _stats["downloaded"],
                             _stats["failed"], _stats["skipped"])

        _phase_start_time = time.time()
        logging.info("Phase 1 complete: %d packages | dl:%d fail:%d skip:%d crash:%d | %.0fs",
                     pkg_total, _stats["downloaded"], _stats["failed"],
                     _stats["skipped"], _stats["crashed"],
                     time.time() - _phase_start_time)

        # Phase 2: transitive dependency discovery (cached per library_version.json state)
        _phase_start_time = time.time()
        logging.info("Phase 2: discovering transitive dependencies from %d known libs",
                     len(all_packages))
        try:
            with open(f"{version_path_prefix}library_version.json", 'r') as file:
                version_ls = json.load(file)
        except (json.JSONDecodeError, OSError):
            _stats["crashed"] += 1
            logging.exception("Corrupted library_version.json, resetting")
            version_ls = {}
        known_libs = set(all_packages)
        discovery_cache = f"{version_path_prefix}discovery_cache.json"
        lib_names_key = sorted(version_ls.keys())
        discovered = set()
        cache_hit = False
        if os.path.exists(discovery_cache):
            try:
                with open(discovery_cache, 'r') as f:
                    cache = json.load(f)
                if (cache.get('lib_names') == lib_names_key and
                        cache.get('python_version') == python_version):
                    discovered = set(cache.get('discovered', []))
                    cache_hit = True
            except (json.JSONDecodeError, KeyError):
                pass
        if not cache_hit:
            for lib in list(all_packages):
                for ver in version_ls.get(lib, {}).get(python_version, []):
                    constraint = get_library_constraint_from_metadata(lib, ver, python_version)
                    for dep in constraint:
                        base_dep = dep.split('[')[0].lower().replace('_', '-')
                        if base_dep not in known_libs and base_dep not in discovered:
                            discovered.add(base_dep)
            cache = {'lib_names': lib_names_key, 'python_version': python_version,
                     'discovered': list(discovered)}
            tmp_cache = discovery_cache + ".tmp"
            with open(tmp_cache, "w") as f:
                json.dump(cache, f)
            os.replace(tmp_cache, discovery_cache)
        # Download and register newly discovered libraries
        for dep in discovered:
            logging.info("Discovered transitive dependency: %s", dep)
            compatible_versions = get_compatible_versions(dep, python_version)
            if not compatible_versions:
                continue
            # Check if this is a source-only or binary-only package
            pypi_url = f'https://pypi.org/pypi/{dep}/json'
            has_sdist = False
            try:
                r = requests.get(pypi_url, timeout=7200)
                if r.status_code == 200:
                    urls = r.json().get('urls', [])
                    has_sdist = any(u.get('packagetype') == 'sdist' for u in urls)
            except requests.RequestException:
                pass
            if has_sdist:
                for ver in compatible_versions:
                    if not os.path.exists(f"{constraint_path_prefix}{dep}/{dep}{ver}/{dep}.json"):
                        download_from_data(dep, ver)
                    try:
                        download_pypi_source(dep, ver, python_version)
                    except Exception:
                        _stats["crashed"] += 1
                        logging.error("download_pypi_source crashed for %s==%s", dep, ver)
                try:
                    with open(f"{version_path_prefix}library_version.json", "r") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    _stats["crashed"] += 1
                    logging.exception("Corrupted library_version.json, resetting")
                    data = {}
                if dep not in data:
                    data[dep] = {}
                data[dep][python_version] = compatible_versions
                lv_path = f"{version_path_prefix}library_version.json"
                tmp_path = lv_path + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(data, f)
                os.replace(tmp_path, lv_path)
                all_packages.add(dep)
            else:
                logging.info("Skipping %s (binary-only, no source distribution)", dep)

        logging.info("Phase 2 complete: discovered %d transitive deps | %.0fs",
                     len(discovered), time.time() - _phase_start_time)

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
            os.makedirs(f"{api_path_prefix}{lib}/", exist_ok=True)
        tasks = []
        for lib in all_library:
            for ver in available_version.get(lib, []):
                if not os.path.exists(f"{api_path_prefix}{lib}/{ver}.json"):
                    logging.info("Queued extraction task: %s==%s", lib, ver)
                    tasks.append((lib, ver))
        pool_size = min(20, cpu_count())
        logging.info("Phase 3: extracting APIs | %d libs, %d tasks, pool=%d workers",
                     len(all_library), len(tasks), pool_size)
        _task_total = len(tasks)
        _task_done = 0
        _task_fail = 0
        sys.setrecursionlimit(5000)
        cleanup_temp_files()
        with Pool(processes=pool_size) as pool:
            for _lib, _ver, ok in pool.map(task, tasks):
                _task_done += 1
                if not ok:
                    _task_fail += 1
                    _stats["crashed"] += 1
                if _task_done % 50 == 0 or _task_done == _task_total:
                    logging.info("Phase 3 progress: %d/%d tasks | fail:%d | %.0fs",
                                 _task_done, _task_total, _task_fail,
                                 time.time() - _phase_start_time)

        logging.info("Phase 3 complete: %d tasks | ok:%d fail:%d | %.0fs",
                     _task_total, _task_total - _task_fail, _task_fail,
                     time.time() - _phase_start_time)
        logging.info("Build complete: %d downloaded, %d failed, %d skipped, %d crashed | run=%s",
                     _stats["downloaded"], _stats["failed"], _stats["skipped"],
                     _stats["crashed"], _run_id)
    except BaseException:
        try:
            kb_report_generate(knowledge_path)
        except Exception:
            logging.exception("Failed to generate KB report")
        raise
    else:
        kb_report_generate(knowledge_path)
