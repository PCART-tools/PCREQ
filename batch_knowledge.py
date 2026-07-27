#!/usr/bin/env python3
"""
Batch knowledge acquisition for all REQBench test cases.

Usage:
    python batch_knowledge.py --config-dir /path/to/REQBench/configure [--project PyTorch-ENet]
Results go to the knowledge base under knowledgePath (set in config.json).
"""

import os
import sys
import argparse
import subprocess
import signal
import time
import glob
from collections import defaultdict


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PCREQ_CONFIGURE = os.path.join(SCRIPT_DIR, 'configure')
LOG_BASE = os.path.join(SCRIPT_DIR, 'batch_logs', 'knowledge')
SUMMARY_LOG = None
_running_proc = None


def _kill_child():
    if _running_proc is not None:
        try:
            _running_proc.terminate()
        except Exception:
            pass
    sys.exit(1)


signal.signal(signal.SIGINT, lambda _sig, _frame: _kill_child())
signal.signal(signal.SIGTERM, lambda _sig, _frame: _kill_child())


def log_print(*args, **kwargs):
    print(*args, **kwargs)
    if SUMMARY_LOG is not None:
        kw = {k: v for k, v in kwargs.items() if k != 'flush'}
        print(*args, **kw, file=SUMMARY_LOG, flush=True)


def setup_symlinks(config_dir):
    os.makedirs(PCREQ_CONFIGURE, exist_ok=True)
    for name in os.listdir(config_dir):
        src = os.path.join(config_dir, name)
        dst = os.path.join(PCREQ_CONFIGURE, name)
        if name.endswith('.json') or name.endswith('.py'):
            continue
        if os.path.isdir(src) and not os.path.exists(dst):
            os.symlink(src, dst)


def collect_by_project(config_dir, target_project=None):
    by_project = defaultdict(list)
    for cf in glob.glob(os.path.join(config_dir, '**/config.json'), recursive=True):
        rel = os.path.relpath(cf, config_dir)
        parts = rel.split(os.sep)
        if len(parts) < 3:
            continue
        project, lib, version = parts[0], parts[1], parts[2]
        if target_project and project != target_project:
            continue
        by_project[project].append((lib, version, rel))
    return by_project


def run_one(config_rel, timeout, log_dir):
    global _running_proc
    cmd = [sys.executable, 'knowledge_acquisition.py', '--config', config_rel]
    os.makedirs(log_dir, exist_ok=True)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    stdout_file = os.path.join(log_dir, f"{timestamp}_stdout.log")
    stderr_file = os.path.join(log_dir, f"{timestamp}_stderr.log")

    start = time.time()
    try:
        with open(stdout_file, 'w') as out, open(stderr_file, 'w') as err:
            _running_proc = subprocess.Popen(cmd, stdout=out, stderr=err, text=True, cwd=SCRIPT_DIR)
            _running_proc.wait(timeout=timeout)
        elapsed = time.time() - start
        if _running_proc.returncode == 0:
            return True, '', elapsed
        else:
            return False, f"exit={_running_proc.returncode} (log: {stderr_file})", elapsed
    except subprocess.TimeoutExpired:
        if _running_proc is not None:
            _running_proc.terminate()
        elapsed = time.time() - start
        return False, f"TIMEOUT {timeout}s (log: {stderr_file})", elapsed
    except Exception as e:
        if _running_proc is not None:
            _running_proc.terminate()
        elapsed = time.time() - start
        return False, f"{type(e).__name__}: {e}", elapsed


def run_project(project, cases, args):
    n = len(cases)
    proj_ok = 0
    proj_fail = 0
    proj_start = time.time()
    failures = []

    for ci, (lib, version, config_rel) in enumerate(cases):
        log_dir = os.path.join(LOG_BASE, project, lib, version)
        log_print(f"  [{ci+1}/{n}] {lib} {version} ... ", end='', flush=True)

        ok, err, elapsed = run_one(config_rel, args.timeout, log_dir)
        if ok:
            log_print(f"OK ({elapsed:.0f}s)")
            proj_ok += 1
        else:
            log_print(f"FAIL ({elapsed:.0f}s)")
            proj_fail += 1
            failures.append(f"{lib}-{version}: {err}")
            log_print(f"    {err}")

    proj_elapsed = time.time() - proj_start
    return proj_ok, proj_fail, failures, proj_elapsed


def main():
    parser = argparse.ArgumentParser(description='Batch knowledge acquisition by project')
    parser.add_argument('--config-dir', required=True, help='REQBench configure directory')
    parser.add_argument('--project', default=None, help='Run only a specific project')
    parser.add_argument('--timeout', type=int, default=36000, help='Timeout per run (default: 36000s)')
    args = parser.parse_args()

    config_dir = os.path.abspath(args.config_dir)

    global SUMMARY_LOG
    os.makedirs(LOG_BASE, exist_ok=True)
    summary_path = os.path.join(LOG_BASE, f"summary_{time.strftime('%Y%m%d_%H%M%S')}.log")
    SUMMARY_LOG = open(summary_path, 'w')

    log_print(f"Batch knowledge acquisition started at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log_print(f"Config dir: {config_dir}")
    log_print(f"Timeout: {args.timeout}s\n")

    log_print("Setting up symlinks to REQBench configure...")
    setup_symlinks(config_dir)

    by_project = collect_by_project(config_dir, args.project)
    projects = sorted(by_project.keys())
    total_runs = sum(len(v) for v in by_project.values())
    log_print(f"Found {len(projects)} projects, {total_runs} runs\n")

    overall_start = time.time()
    total_ok = 0
    total_fail = 0
    project_failures = {}
    project_times = {}

    for pi, project in enumerate(projects):
        cases = sorted(by_project[project])
        n = len(cases)
        log_print(f"[P{pi+1}/{len(projects)}] {project} ({n} runs)")

        proj_ok, proj_fail, failures, proj_elapsed = run_project(project, cases, args)
        project_times[project] = proj_elapsed
        total_ok += proj_ok
        total_fail += proj_fail

        if failures:
            project_failures[project] = failures

        log_print(f"  -> {proj_ok} ok, {proj_fail} fail ({proj_elapsed:.0f}s)\n")

    elapsed = time.time() - overall_start
    log_print(f"Done. {total_ok} ok, {total_fail} fail, total {total_runs}, elapsed {elapsed:.0f}s")

    if project_failures:
        fail_log = os.path.join(SCRIPT_DIR, 'knowledge_failures.txt')
        with open(fail_log, 'w') as f:
            for proj, errs in sorted(project_failures.items()):
                f.write(f"[{proj}]\n")
                for e in errs:
                    f.write(f"  {e}\n")
                f.write('\n')
        log_print(f"Failures written to {fail_log}")
    log_print(f"Total logs: {LOG_BASE}")
    SUMMARY_LOG.close()


if __name__ == '__main__':
    main()
