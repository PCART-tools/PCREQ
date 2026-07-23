import os, re, ast, platform, json, logging, shutil, time
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from packaging import version
from queue import Queue
from extraction.import_to_path import get_paths_of_import, paths_of_import_file

if (platform.system() == 'Windows'):
    slash = "\\"
else:
    slash = r"/"


def norm_pkg(name):
    """PEP 503 package name normalization: lowercase, underscores to hyphens."""
    return name.lower().replace('_', '-')


def resolve_pkg_dir(pkg, *prefixes):
    """Return the directory name for *pkg*, trying PEP 503 norm first, then
    underscore fallback when an old-KB directory exists under any of *prefixes*.
    """
    norm = norm_pkg(pkg)
    alt = pkg.lower().replace('-', '_')
    if alt == norm:
        return norm
    for prefix in prefixes:
        if os.path.isdir(f"{prefix}{alt}/") or os.path.isdir(f"{prefix}{alt}"):
            return alt
    return norm


def norm_ver(ver):
    """Normalize a version string via PEP 440.

    Strips leading zeros in release segments (e.g. '0.2.2.09' -> '0.2.2.9').
    Preserves pre-release labels, local version labels, and legacy versions.
    Falls back to the original string if parsing fails.
    """
    try:
        return str(Version(ver))
    except Exception:
        try:
            return str(version.parse(ver))
        except Exception:
            return ver


def get_path_by_extension(root_dir, flag='.py'):
    paths = []
    for root, dirs, files in os.walk(root_dir):
        files = [f for f in files if not f[0] == '.']  # skip hidden files such as git files
        dirs[:] = [d for d in dirs if not d[0] == '.']
        for f in files:
            if f.endswith(flag):
                paths.append(os.path.join(root, f))
    return paths

def find_setup_path(dir_path):
    """
    在指定目录及其子目录中查找setup.py文件。
    如果找到，返回setup.py文件的路径；否则返回None。
    """
    for root, dirs, files in os.walk(dir_path):
        if 'setup.py' in files:
            return os.path.join(root, 'setup.py')
    return None

def find_requirements_path(dir_path):
    """
    find requirements.txt path
    :param dir_path:
    :return: requirements.txt path or None
    """
    for root, dirs, files in os.walk(dir_path):
        if 'requirements.txt' in files:
            return os.path.join(root, 'requirements.txt')
    return None

def is_version_compat(proj_cons, lib_cons):
    """Check if proj_cons satisfies lib_cons (a PEP 440 specifier or similar).

    Handles PEP 440 features (local labels, pre-release, post-release)
    and non-standard constraints (quoted specifiers, missing commas,
    legacy versions like '2011k').
    """
    # Strip +local labels first (PEP 440: ignored for version comparison,
    # and '+' is not a valid specifier character)
    proj_cons_clean = re.sub(r'\+[^,<>=~!\s]*', '', proj_cons)
    lib_cons_clean = re.sub(r'\+[^,<>=~!\s]*', '', lib_cons)

    # Tier 1: PEP 440 passthrough (primary — handles pre/post/rc/dev release)
    try:
        return proj_cons_clean in SpecifierSet(lib_cons_clean)
    except Exception:
        pass

    # Tier 2: Strip letters, quotes, fix commas (legacy fallback for non-standard versions)
    new_proj_cons = re.sub(r'[a-zA-Z]', '', proj_cons_clean)
    new_lib_cons = re.sub(r'[a-zA-Z]', '', lib_cons_clean)
    new_lib_cons = new_lib_cons.replace('*', '0')
    new_lib_cons = new_lib_cons.replace('\'', '')
    new_lib_cons = new_lib_cons.replace('"', '')
    if new_lib_cons.endswith('.'):
        new_lib_cons = new_lib_cons[:-1]
    new_lib_cons = re.sub(r'(\d[\d\.]*)(?=(<|>|=|~|!))', r'\1,', new_lib_cons)

    try:
        return new_proj_cons in SpecifierSet(new_lib_cons)
    except Exception:
        return False

def compare_version(version1, version2):
    v1 = version.parse(version1)

    v2 = version.parse(version2)

    if v1 > v2:
        return True
    else:
        return False

def list_to_dict(package_and_cons):
    package_and_cons_dict = {}
    for i in package_and_cons:
        package_and_cons_dict[i[0]] = i[1]
    return package_and_cons_dict

class FunctionDefExtractor(ast.NodeVisitor):
    def __init__(self):
        self.function_names = []

    def visit_FunctionDef(self, node):
        # 提取函数名
        self.function_names.append(node.name)
        self.generic_visit(node)

def extract_function_defs_from_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        file_content = file.read()
        
    # 解析 AST
    tree = ast.parse(file_content)
    extractor = FunctionDefExtractor()
    extractor.visit(tree)
    
    return extractor.function_names

def extract_classes_from_file(file_path):
    """从给定的 Python 文件中提取所有的类名"""
    try:
        with open(file_path, 'r') as file:
            # 读取文件内容并解析为 AST
            node = ast.parse(file.read(), filename=file_path)
    except Exception as e:
        print(f"Error parsing file: {e}")
        return []

    # 存储提取的类名
    classes = []

    # 遍历 AST 节点
    for n in ast.walk(node):
        if isinstance(n, ast.ClassDef):
            # 提取类名
            classes.append(n.name)

    return classes

def get_library_call_module(library):
        module_map = {
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
        return module_map.get(library, library)


def _dedup_roots(entries):
    """Extract first path component from *entries* and return unique roots."""
    result = []
    seen = set()
    for e in entries:
        root = e.split('/')[0]
        if root not in seen:
            seen.add(root)
            result.append(root)
    return result


def _merge_roots(all_entries):
    """Merge root entries from multiple .dist-info/.egg-info directories."""
    result = []
    seen = set()
    for _d, entries in all_entries:
        for e in entries:
            root = e.split('/')[0]
            if root not in seen:
                seen.add(root)
                result.append(root)
    return result


def _match_package_name(all_entries, suffix, package_name):
    """Select the .dist-info/.egg-info entry matching *package_name*.

    Strategy A: .dist-info directory name (minus version) matches package name.
    Strategy B: top_level.txt contains an entry matching package name.
    Returns the deduped root list on match, or None.
    """
    norm_pkg = package_name.replace("-", "_")
    # Strategy A: match by .dist-info directory name
    for d, entries in all_entries:
        d_name = d[:-(len(suffix) + 1)]        # strip ".dist-info" / ".egg-info"
        d_pkg = d_name.rsplit("-", 1)[0]        # strip version suffix
        if d_pkg.replace("-", "_") == norm_pkg:
            return _dedup_roots(entries)
    # Strategy B: match by top_level.txt entry
    for d, entries in all_entries:
        for e in entries:
            if e.split('/')[0].replace("-", "_") == norm_pkg:
                return _dedup_roots(entries)
    return None


def detect_call_modules(target_dir, package_name=None):
    """Return all importable module names found in *target_dir*.

    Fallback order:
      1. .dist-info/top_level.txt
      2. .egg-info/top_level.txt
      3. directories with __init__.py
      4. root .py files (single-file modules)

    When *package_name* is provided and multiple .dist-info/.egg-info
    directories are present (old KB ``pip install --target`` pollution),
    the entry matching the package name is preferred.
    Returns an empty list when *target_dir* does not exist or contains no modules.
    """
    if not os.path.isdir(target_dir):
        return []

    # Tier 1 & 2: top_level.txt from .dist-info or .egg-info
    for suffix in (".dist-info", ".egg-info"):
        all_entries = []
        for d in sorted(os.listdir(target_dir)):
            if d.endswith(suffix):
                tl_path = os.path.join(target_dir, d, "top_level.txt")
                if not os.path.isfile(tl_path):
                    continue
                try:
                    with open(tl_path) as f:
                        entries = [l.strip() for l in f if l.strip()]
                except OSError:
                    continue
                if entries:
                    all_entries.append((d, entries))
        if not all_entries:
            continue

        # Prefer the .dist-info matching package_name when multiple present
        if package_name and len(all_entries) > 1:
            matched = _match_package_name(all_entries, suffix, package_name)
            if matched is not None:
                return matched

        # Fallback: merge entries from all matching directories
        if len(all_entries) == 1:
            return _dedup_roots(all_entries[0][1])
        return _merge_roots(all_entries)

    # Tier 3: directories with __init__.py, and namespace packages
    # (dirs without __init__.py that contain subdirs with __init__.py,
    # e.g. google/ containing google/protobuf/__init__.py).
    modules = []
    seen = set()
    for d in sorted(os.listdir(target_dir)):
        if d.startswith(".") or d.endswith((".dist-info", ".egg-info", ".data")):
            continue
        dp = os.path.join(target_dir, d)
        if not os.path.isdir(dp):
            continue
        if os.path.isfile(os.path.join(dp, "__init__.py")):
            if d not in seen:
                modules.append(d)
                seen.add(d)
        else:
            # Check for namespace package: subdirs with __init__.py
            for sub in os.listdir(dp):
                subp = os.path.join(dp, sub)
                if os.path.isdir(subp) and os.path.isfile(os.path.join(subp, "__init__.py")):
                    if d not in seen:
                        modules.append(d)
                        seen.add(d)
                    break

    # Tier 4: root .py files (single-file modules)
    if not modules:
        for f in sorted(os.listdir(target_dir)):
            if f.startswith("."):
                continue
            fp = os.path.join(target_dir, f)
            if os.path.isfile(fp) and f.endswith(".py") and f != "setup.py":
                modules.append(f[:-3])
    return modules

def transform_and_remove_last_segment(input_str):
    # 将点号替换为斜杠
    transformed_str = input_str.replace('.', '/')
    
    # 移除最后一个斜杠及其后面的所有内容
    last_slash_index = transformed_str.rfind('/')
    if last_slash_index != -1:
        transformed_str = transformed_str[:last_slash_index]
    
    return transformed_str

def getAst(filePath,strFlag=0): #若strFlag=1,则表明传进来的是一个api，而不是一个路径
    if strFlag==0:
        with open(filePath,'r') as f:
            s=f.read()
        root=ast.parse(s,filename='<unknown>',mode='exec')
        return root
    root=ast.parse(filePath,filename='<unknown>',mode='exec')
    return root

def find_init_files(directory):
    init_files = []
    for root, dirs, files in os.walk(directory):
        # 如果目录中有 __init__.py 文件，则加入到结果列表
        if '__init__.py' in files:
            init_files.append(os.path.join(root, '__init__.py'))
    init_files.reverse()
    return init_files

class FromImport(ast.NodeVisitor):
    def __init__(self, currentLevel):
        self._importDict={}
        self._currentLevel=currentLevel

    @property
    def importDict(self):
        return self._importDict

    def visit_ImportFrom(self, node):
        if node.module is not None:
            module=node.module
            if node.level==0:#若是绝对导入，需考虑层级
                tempLst=module.split('.')
                if len(tempLst)==1:
                    module=''
                elif self._currentLevel in tempLst:
                    index=tempLst.index(self._currentLevel)
                    module='.'.join(tempLst[index+1:])
            
            lst=[{'name':name.name,'alias':name.asname} for name in node.names] #可能会import个多个,from A import a,b,c 
            for dic in lst: #lst中每个元素都是字典
                key=module+'.'+dic['name'] #dic['name']可能是*
                key=key.lstrip('.')
                if dic['alias']:
                    self._importDict[key]=dic['alias']
                else:
                    self._importDict[key]=dic['name']



#通过解析__init__.py,把源码中的部分API路径缩短
#缩短API路径可能会将不同文件中的API还原成相同的形式，比如A.b.f,A.c.f都还原成A.f
def shortenPath(api_dict, library, version, library_path_prefix, source_module=None): #lst是传入传出参数，保存修正之后的API路径
    library_call_module = source_module or get_library_call_module(library)
    norm_lib = resolve_pkg_dir(library, library_path_prefix)
    library_path = f"{library_path_prefix}{norm_lib}/{norm_lib}{version}/{library_call_module}"
    prefix = f"{library_path_prefix}{norm_lib}/{norm_lib}{version}/"
    new_dict = api_dict.copy()
    init_files = find_init_files(library_path)
    _t0 = time.time()
    logging.debug("shortenPath start: %s==%s | %d keys, %d init_files",
                  library, version, len(api_dict), len(init_files))

    # Point 35: cache (init_file, currentLevel) -> importDict to avoid
    # repeated FromImport visits while preserving the original semantics.
    _import_cache = {}

    for init_file in init_files:
        try:
            root = getAst(init_file)
        except Exception:
            continue

        api_prefix = init_file.replace(prefix, '')
        if api_prefix.endswith('/__init__.py'):
            api_prefix = api_prefix.replace('/__init__.py', '').replace('/', '.')
        else:
            api_prefix = api_prefix.replace('.py', '').replace('/', '.')

        for api in list(new_dict.keys()):
            try:
                currentLevel = f"{api.split('.')[-2]}.py"
            except Exception:
                continue

            cache_key = (init_file, currentLevel)
            if cache_key not in _import_cache:
                obj = FromImport(currentLevel)
                obj.visit(root)
                _import_cache[cache_key] = obj.importDict.copy()

            importDict = _import_cache[cache_key]

            replaceKey1 = ''
            replaceVal1 = ''
            replaceKey2 = ''
            replaceVal2 = ''
            for key, value in importDict.items():
                if key[-1] == '*':
                    key = key.rstrip('*')
                    if key in api:
                        replaceKey1 = key
                        replaceVal1 = ''
                elif key in api:
                    replaceKey2 = key
                    replaceVal2 = value

            new_api = None
            if replaceKey2:  # 优先使用第二种替换方式
                new_api = api.replace(replaceKey2, replaceVal2)
                if not new_api.startswith(library_call_module):
                    append_str = new_api.replace(replaceKey2, '')
                    if append_str:
                        new_api = f"{api_prefix}.{append_str}"
                        new_api = new_api.replace('..', '.')
                    else:
                        new_api = None  # P56: skip empty-suffix alias
            elif replaceKey1:
                new_api = api.replace(replaceKey1, replaceVal1)
                if not new_api.startswith(library_call_module):
                    append_str = new_api.replace(replaceKey1, '')
                    if append_str:
                        new_api = f"{api_prefix}.{append_str}"
                        new_api = new_api.replace('..', '.')
                    else:
                        new_api = None  # P56: skip empty-suffix alias

            # P56: normalize path separators in keys
            if new_api is not None:
                new_api = new_api.replace('/', '.')

            if not isinstance(new_dict[api], str) and new_api is not None:
                if new_api not in new_dict:
                    new_dict[new_api] = api
            elif new_api is not None:
                if new_api not in new_dict:
                    new_dict[new_api] = new_dict[api]

    # P56: normalize path separators in original keys
    _clean_keys = {k: v for k, v in new_dict.items() if '/' not in k}
    for k, v in new_dict.items():
        if '/' in k:
            _norm = k.replace('/', '.')
            if _norm not in _clean_keys:
                _clean_keys[_norm] = v

    _dt = time.time() - _t0
    if _dt > 1:
        logging.debug("shortenPath done: %s==%s | %d keys, %d init_files → %.0fs",
                      library, version, len(api_dict), len(init_files), _dt)
    return _clean_keys


def load_config(config_path):
    # config文件是一个JSON格式文件
    with open(f"./configure/{config_path}", 'r') as file:
        config = json.load(file)
    return config

# 配置日志，输出到文件和控制台
def setup_logging(log_filename):
    # 创建一个日志记录器
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)  # 设置日志级别为INFO

    # 创建一个日志处理器，用于输出到文件
    file_handler = logging.FileHandler(log_filename, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.INFO)  # 设置该处理器输出INFO及以上级别的日志

    # 创建一个日志处理器，用于输出到控制台
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)  # 控制台输出INFO级别的日志

    # 创建日志格式
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 将处理器添加到日志记录器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

def save_dict_to_file(d, filename='output.txt'):
    # 格式化字典
    formatted_output = '\n'.join([f"{key}=={value}" for key, value in sorted(d.items())])
    
    # 将格式化后的内容写入文件
    with open(filename, 'w') as file:
        file.write(formatted_output)

def get_proj_dependency_from_requirements(file_path):
    requirements_dict = {}
    with open(file_path, 'r') as file:
        for line in file:
            # 移除行首行尾的空格和换行符
            line = line.strip()
            # 如果行不是空行并且不是注释
            if line and not line.startswith('#') and '@' not in line:
                # 按照 '==' 分割包名和版本号
                package, version = line.split('==')
                # 将包名和版本号添加到字典中
                requirements_dict[package.lower()] = version
    return requirements_dict

def remove_invalid_versions(dependency):
    return {pkg: ver for pkg, ver in dependency.items() if ver not in ["0.0.0", "0.0", "0"]}

def update_project_dependencies(target_proj_dependency, res):
    '''if res is None:
        return target_proj_dependency'''
    for pkg, ver in res.items():
        if target_proj_dependency.get(pkg) != ver:
            target_proj_dependency[pkg] = ver
    return target_proj_dependency

def get_library_paths(library_path_prefix, target_library, version, call_module):
    return resolve_library_source_path(
        library_path_prefix, target_library, version, call_module)


def resolve_library_source_path(library_path_prefix, lib, version, call_module):
    """Return actual source root for API extraction.

    If {call_module}_core/ exists under the version root, use it
    (e.g. tensorflow_core/ for TF 1.15/2.0/2.1). Otherwise fall back
    to {call_module}/ (old KB, already merged; or non-TF packages).
    """
    norm_lib = resolve_pkg_dir(lib, library_path_prefix)
    norm_ver_name = norm_ver(version)
    version_root = f"{library_path_prefix}{norm_lib}/{norm_lib}{norm_ver_name}"
    core_candidate = f"{version_root}/{call_module}_core"
    if os.path.isdir(core_candidate):
        return core_candidate
    return f"{version_root}/{call_module}"


def add_public_api_aliases(api_dict, source_module, public_module):
    """Create public_module.* → source_module.* aliases.

    For every source_module.* key in api_dict, add a corresponding
    public_module.* alias pointing to the same value. Existing
    public_module.* keys are not overwritten.

    Example: add_public_api_aliases(funcs, "tensorflow_core", "tensorflow")
      tensorflow.python.ops.xxx → tensorflow_core.python.ops.xxx
    """
    if not source_module or not public_module:
        return api_dict
    if source_module == public_module:
        return api_dict
    new_dict = api_dict.copy()
    source_prefix = source_module + "."
    public_prefix = public_module + "."
    for api, value in list(api_dict.items()):
        if api.startswith(source_prefix):
            alias = public_prefix + api[len(source_prefix):]
            if alias not in new_dict:
                new_dict[alias] = value
    return new_dict


def rename_core_keys(obj, call_module):
    """Rename {call_module}.* keys to their public names.

    TensorFlow 1.15/2.0/2.1 ship a stub tensorflow/ and real source in
    tensorflow_core/.  Extracted API keys get the _core prefix, but the
    __init__.py imports inside tensorflow_core/ reference the public name
    (tensorflow.*).  Renaming keys before shortenPath lets those imports
    match; renaming afterwards fixes alias keys that shortenPath derives
    from directory paths.

    Accepts a dict (renames keys) or a single string key.
    """
    if not call_module.endswith('_core'):
        return obj
    public = call_module[:-5]
    core_prefix = call_module + '.'
    if isinstance(obj, str):
        if obj == call_module:
            return public
        if obj.startswith(core_prefix):
            return public + '.' + obj[len(core_prefix):]
        return obj
    new_dict = {}
    for k, v in obj.items():
        if k == call_module:
            new_dict[public] = v
        elif k.startswith(core_prefix):
            new_dict[public + '.' + k[len(core_prefix):]] = v
        else:
            new_dict[k] = v
    return new_dict


def cleanup_temp_files():
    tmp_json = "./extraction/tmp.json"
    if os.path.exists(tmp_json):
        try:
            os.remove(tmp_json)
        except OSError:
            pass



