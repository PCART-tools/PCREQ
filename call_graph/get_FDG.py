import requests
import json
import os
import ast, re
import platform
import time
import uuid
import logging
from packaging.markers import Marker
from utils.util import norm_pkg, resolve_pkg_dir, norm_ver

if (platform.system() == 'Windows'):
    slash = "\\"
else:
    slash = r"/"

library_path_prefix = ""
constraint_path_prefix = ""
version_path_prefix = ""
api_path_prefix = ""

def setup_path_1(library_path_prefix_pass, constraint_path_prefix_pass, version_path_prefix_pass, api_path_prefix_pass):
    global library_path_prefix, constraint_path_prefix, version_path_prefix, api_path_prefix
    library_path_prefix = library_path_prefix_pass
    constraint_path_prefix = constraint_path_prefix_pass
    version_path_prefix = version_path_prefix_pass
    api_path_prefix = api_path_prefix_pass

def split_and_take_first_part(elements):
    # 使用列表推导式处理每个元素
    #return [element.split(';')[0].strip() for element in elements]
    res =[]
    for element in elements:
        if ';' in element:
            res.append(element.split(';')[0])
        else:
            res.append(element)
    return res

def split_packname_and_cons(line):
    version_ops = [r'<', r'<=', r'!=', r'==', r'>=', r'>', r'~=', r'===']
    min_op_idx = None
    for op in version_ops:
        if line.find(op) != -1:
            if min_op_idx == None:
                min_op_idx = line.find(op)
            else:
                min_op_idx = min(min_op_idx, line.find(op))
    res = []
    if min_op_idx != None:
        res.append(line[:min_op_idx])
        res.append(line[min_op_idx:])
    else:
        res.append(line)
    for i in range(len(res)):
        res[i]=res[i].replace(" ","")
    return res

def remove_parentheses_from_end(elements):
    # 使用列表推导式处理每个元素
    return [element.rstrip('()') for element in elements]

def download_json(url, filename):
    for retry in range(3):
        try:
            response = requests.get(url, timeout=60)
            if response.status_code == 200:
                data = response.json()
                tmp_filename = f"{filename}.{uuid.uuid4().hex[:8]}.tmp"
                with open(tmp_filename, 'w') as file:
                    json.dump(data, file, indent=4)
                os.replace(tmp_filename, filename)
                logging.debug("Constraint saved to %s", filename)
                return
            elif response.status_code == 404:
                tmp_filename = f"{filename}.{uuid.uuid4().hex[:8]}.tmp"
                with open(tmp_filename, 'w') as file:
                    json.dump({"message": "Not Found"}, file)
                os.replace(tmp_filename, filename)
                return
            else:
                logging.warning("Failed to retrieve constraint: HTTP %s", response.status_code)
                return
        except requests.RequestException:
            if retry < 2:
                time.sleep(2 ** retry)
    logging.error("Failed to download constraint after 3 retries: %s", url)

def download_from_data(package, package_version):
    norm_name = norm_pkg(package)
    norm_ver_name = norm_ver(package_version)
    logging.debug("Downloading constraint data for %s", package)
    url = 'https://pypi.tuna.tsinghua.edu.cn/pypi/' + norm_name + '/json'
    # PyPI API URL requires RAW version string
    url2 = 'https://pypi.org/pypi/' + norm_name + '/' + package_version + '/json'
    # Filesystem path uses NORMALIZED version
    path = constraint_path_prefix + norm_name + '/' + norm_name + norm_ver_name
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return
    download_json(url2, path + '/' + norm_name + '.json')

def remove_elements_with_extra(lst):
    # 使用列表推导式过滤掉包含'docs'或'tests'的元素
    #return [item for item in lst if 'extra' not in item]
    new_requires_dist = []
    for item in lst:
        if 'extra' in item and 'alldeps' in item:
            new_requires_dist.append(item)
        elif 'extra' not in item:
            new_requires_dist.append(item)
    return new_requires_dist

def remove_incompat_python_version(requires_dist, python_version):
    """Drop dependencies whose ``python_version`` marker is incompatible.

    Entries without markers are always kept.  Only markers that contain
    ``python_version`` are evaluated; markers without it (``sys_platform``,
    ``extra``, ``os_name``, etc.) are kept as-is — keeping a pure-platform
    dependency is harmless (extra dep on other platforms), but dropping a
    version constraint with a platform marker would make it universal and
    force unnecessary upgrades (e.g. Darwin-only ``numpy>=1.21.0`` forcing
    upgrade on Linux).
    """
    new_requires_dist = []
    env = {"python_version": python_version}
    for item in requires_dist:
        if ";" not in item:
            new_requires_dist.append(item)
            continue
        _req_part, marker_str = item.split(";", 1)
        marker_str = marker_str.strip()
        # Markers without python_version are not our concern —
        # evaluating them with a partial env would incorrectly drop
        # entries like ``extra == 'alldeps'``.
        if "python_version" not in marker_str:
            new_requires_dist.append(item)
        else:
            try:
                m = Marker(marker_str)
                if m.evaluate(env):
                    new_requires_dist.append(item)
            except Exception:
                # Malformed marker — keep the dependency (safe default)
                new_requires_dist.append(item)
    return new_requires_dist

def get_tree(filename):
    def get_tree_with_feature_version(filename, feature_version=None):
        """
        Get the entire AST for this file

        :param filename str:
        :rtype: ast
        """
        try:
            with open(filename) as f:
                raw = f.read()
        except ValueError:
            with open(filename, encoding='UTF-8', errors='ignore') as f:
                raw = f.read()
        if feature_version == None:
            tree = ast.parse(raw)
        else:
            tree = ast.parse(raw, feature_version=feature_version)
        return tree

    try:
        tree = get_tree_with_feature_version(filename, feature_version=(3, 8))
        return tree
    except:
        try:
            tree = get_tree_with_feature_version(filename)
            return tree
        except:
            try:
                tree = get_tree_with_feature_version(filename, feature_version=(3, 4))
                return tree
            except:
                return None

def find_setup_path(dir_path):
        """
        find setup.py path
        :param dir_path:
        :return: setpy.py path or None
        """
        setup_path = None
        for f in os.listdir(dir_path):
            full_f = os.path.join(dir_path, f)
            if full_f.endswith(r"\setup.py") or full_f.endswith(r"/setup.py"):
                setup_path = full_f
                break

        if setup_path == None:
            parent_dir_path = slash.join(dir_path.split(slash)[:-1])
            for f in os.listdir(parent_dir_path):
                full_f = os.path.join(parent_dir_path, f)
                if full_f.endswith(r"\setup.py") or full_f.endswith(r"/setup.py"):
                    setup_path = full_f
                    break

        return setup_path

def get_packname_and_cons_from_setup(librarypath):
    setupfilepath = find_setup_path(librarypath)

    try:
        r_node = get_tree(setupfilepath)
    except SyntaxError:
        return []
    
    if r_node == None:
        return []

    install_requires = None
    res = []
    for element in ast.walk(r_node):
        if type(element) == ast.Call and ((type(element.func) == ast.Name and element.func.id == "setup") or
                                          ((type(element.func) == ast.Attribute and type(element.func.value)==ast.Name and element.func.value.id == "setuptools" and element.func.attr=="setup"))):
            for keyword in element.keywords:
                if keyword.arg in ["install_requires","setup_requires"]:
                    install_requires = keyword.value
                    break

            if install_requires == None:
                pass

            break

    if install_requires == None:
        return []

    if type(install_requires) not in [ast.Name, ast.List]:
        return []

    assert type(install_requires) in [ast.Name, ast.List]
    if type(install_requires) == ast.Name:
        to_search_str = install_requires.id
        install_requires = None
        for element in ast.walk(r_node):
            if type(element) == ast.Assign:
                for target in element.targets:
                    if hasattr(target, "id") and target.id == to_search_str and hasattr(element, "value") and type(element.value) == ast.List:
                        install_requires = element.value

    if install_requires == None:
        return []

    assert type(install_requires) == ast.List
    for single_req in install_requires.elts:
        if type(single_req) == ast.BinOp:
            continue

        assert type(single_req) in [ast.Str, ast.Constant]
        if type(single_req)==ast.Str:
            res.append(split_packname_and_cons(single_req.s))
        else:
            res.append(split_packname_and_cons(single_req.value))



    return res


def _resolve_pkg_dir(pkg):
    return resolve_pkg_dir(pkg, constraint_path_prefix, library_path_prefix)


def try_read_constraint_json(pkg, version):
    """Read constraint JSON, trying PEP 503 name then underscore fallback."""
    norm_name = norm_pkg(pkg)
    alt_name = pkg.lower().replace('-', '_')
    nv = norm_ver(version)
    for name in (norm_name, alt_name):
        json_path = f"{constraint_path_prefix}{name}/{name}{nv}/{name}.json"
        if os.path.isfile(json_path):
            try:
                with open(json_path, 'r') as file:
                    return json.load(file), None
            except (json.JSONDecodeError, OSError):
                continue
    return None, None


def get_library_constraint_from_metadata(pkg, version, python_version):
    res = {}
    norm_pkg_name = _resolve_pkg_dir(pkg)
    norm_ver_name = norm_ver(version)
    target_dir = f"{library_path_prefix}{norm_pkg_name}/{norm_pkg_name}{norm_ver_name}/"

    # Priority chain: setup.py → METADATA → egg-info/requires.txt → PyPI JSON
    # setup.py is the authoritative source (developer-written); METADATA
    # supplements with entries not in setup.py.
    metadata_found = False
    metadata_is_old = False  # Metadata-Version ≤ 2.0 may lack Requires-Dist
    requires_dist = None

    # Priority 0: setup.py as authoritative baseline
    library_path = f"{library_path_prefix}{norm_pkg_name}/{norm_pkg_name}{norm_ver_name}/{norm_pkg_name}"
    if os.path.exists(library_path):
        s = get_packname_and_cons_from_setup(library_path)
        for i in s:
            key = re.sub(r'\[.*\]', '', i[0]).lower().replace('_', '-')
            if len(i) == 2:
                res[key] = i[1].replace("-", ".")
            else:
                res[key] = None

    # Priority 1: scan for *.dist-info/METADATA (not fixed path —
    #   wheel/sdist may use underscore, different case, etc.)
    # Prefer the dist-info matching the package name; old KB may have
    # leftover dist-info dirs from pip-installed dependencies.
    if os.path.isdir(target_dir):
        candidates = []
        for item in os.listdir(target_dir):
            if item.endswith(".dist-info"):
                candidate = os.path.join(target_dir, item, "METADATA")
                if os.path.isfile(candidate):
                    # score: 0 = name match, 1 = no match
                    pkg_lower = pkg.lower().replace("-", "_")
                    item_lower = item.lower().replace("-", "_")
                    score = 0 if item_lower.startswith(pkg_lower) else 1
                    candidates.append((score, candidate))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            metadata_path = candidates[0][1]
            metadata_found = True
            try:
                with open(metadata_path, 'r') as file:
                    metadata = file.read()
            except OSError:
                metadata = None
            if metadata is not None:
                requires_dist_pattern = r"Requires-Dist: (.+?)(?=\n|$)"
                requires_dist = re.findall(requires_dist_pattern, metadata)
                # Detect old-format METADATA that predates Requires-Dist
                mv_match = re.search(
                    r"^Metadata-Version:\s*([\d.]+)", metadata,
                    re.MULTILINE | re.IGNORECASE)
                if mv_match:
                    try:
                        metadata_is_old = (
                            tuple(map(int, mv_match.group(1).split(".")))
                            <= (2, 0)
                        )
                    except (ValueError, TypeError):
                        pass

    if not metadata_found:
        # Priority 2: .egg-info/requires.txt
        egg_requires = None
        if os.path.isdir(target_dir):
            for item in os.listdir(target_dir):
                if item.endswith('.egg-info'):
                    req_path = os.path.join(target_dir, item, 'requires.txt')
                    if os.path.isfile(req_path):
                        try:
                            with open(req_path, 'r') as f:
                                egg_requires = [l.strip() for l in f.read().split('\n')
                                                if l.strip() and not l.strip().startswith('[')]
                        except OSError:
                            pass
                    break
        if egg_requires is not None:
            requires_dist = egg_requires
        else:
            # Priority 3: PyPI JSON fallback
            logging.debug("Falling back to PyPI JSON for %s==%s constraints",
                          pkg, version)
            data, _ = try_read_constraint_json(pkg, version)
            if data is not None:
                try:
                    requires_dist = data['info']['requires_dist']
                except (KeyError, TypeError):
                    requires_dist = None
            else:
                logging.debug("No constraint JSON for %s==%s", pkg, version)
                requires_dist = None

    # Fall back to PyPI JSON when:
    # 1. No artifact metadata was found at all (not metadata_found), OR
    # 2. Metadata-Version ≤ 2.0, which predates Requires-Dist —
    #    an empty result means "format too old", not zero-dependency.
    # Modern METADATA (≥2.1) with empty Requires-Dist IS authoritative.
    if not metadata_found or metadata_is_old:
        if requires_dist is None or len(requires_dist) == 0:
            logging.debug("No requires_dist in artifact metadata for %s==%s, "
                           "trying PyPI JSON", pkg, version)
            data, _ = try_read_constraint_json(pkg, version)
            if data is not None:
                try:
                    requires_dist = data['info']['requires_dist']
                except (KeyError, TypeError):
                    requires_dist = None
            else:
                logging.debug("No constraint data for %s==%s", pkg, version)
                requires_dist = None

    if requires_dist is not None:
        requires_dist = remove_elements_with_extra(requires_dist)
        requires_dist = remove_incompat_python_version(requires_dist, python_version)

        new_requires_dist = split_and_take_first_part(requires_dist)
        for i in range(len(requires_dist)):
            tmp = requires_dist[i]
            tmp = tmp.split(';')[0]
            new_requires_dist[i] = split_packname_and_cons(tmp)
            tmp1 = new_requires_dist[i]
            new_requires_dist[i] = remove_parentheses_from_end(tmp1)
    else:
        new_requires_dist = None

    # METADATA / egg-info / JSON supplements setup.py (does not overwrite)
    if new_requires_dist is not None:
        for i in new_requires_dist:
            try:
                key = re.sub(r'\[.*\]', '', i[0]).lower().replace('_', '-')
                if key not in res:  # setup.py is authoritative
                    res[key] = i[1].replace("-", ".")
            except Exception:
                key = i[0].lower().replace('_', '-')
                if key not in res:
                    res[key] = None

    return res 

def get_library_dependency_from_metadata(pkg, version, python_version):
    res = []

    library_constraint = get_library_constraint_from_metadata(pkg, version, python_version)
    for library in library_constraint:
        res.append(library)

    return res 


def get_FDG_from_requirements(proj_dependency, python_version):
    res = {}

    #proj_dependency = get_proj_dependency_from_requirements(file_path)
    for library in proj_dependency:
        res[library] = get_library_dependency_from_metadata(library, proj_dependency[library], python_version)
        #print(res[library])
        #pass
    res = {node.lower(): [neighbor.lower() for neighbor in neighbors] for node, neighbors in res.items()}
    return res

def reachable_nodes(graph, start_node):
    visited = set()
    #start_node = start_node.lower

    def dfs(node):
        if node not in visited:
            #print(node)
            visited.add(node)
            for neighbor in graph.get(node, []):
                dfs(neighbor)

    dfs(start_node)
    return visited

def build_undirected_graph(graph):
    undirected_graph = {}
    for node, neighbors in graph.items():
        if node not in undirected_graph:
            undirected_graph[node] = []
        for neighbor in neighbors:
            if neighbor not in undirected_graph:
                undirected_graph[neighbor] = []
            undirected_graph[node].append(neighbor)
            undirected_graph[neighbor].append(node)  # 添加反向边
    return undirected_graph

def get_sub_graph(graph, node):
    sub_graph = {}

    undirected_graph = build_undirected_graph(graph)
    #print(graph)
    visited = reachable_nodes(undirected_graph, node)
    for i in visited:
        try:
            sub_graph[i] = graph[i]
        except:
            continue
    return sub_graph
'''
if __name__ == '__main__':
    res = get_library_dependency_from_metadata('tensorflow', '2.6.2')
    print(res)
'''