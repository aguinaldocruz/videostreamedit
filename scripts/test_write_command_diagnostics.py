"""Diagnostics and warning exit codes must not weaken default write safety."""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'app/job_safety.py'
nodes = [node for node in ast.parse(source.read_text()).body
         if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in {'WriteCommandError', 'run_write_command'}]
scope = dict(os=os, shutil=shutil, subprocess=subprocess, tempfile=tempfile, time=time)
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), scope)
with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, WORKFLOW_MIN_FREE_GB='0', WORKFLOW_RESERVED_GB='0'):
    warning = [sys.executable, '-c', "print('Warning: source encoding'); raise SystemExit(1)"]
    try:
        scope['run_write_command'](warning, folder)
    except subprocess.CalledProcessError as exc:
        assert 'Warning: source encoding' in str(exc) and exc.returncode == 1
    else:
        raise AssertionError('Default writes accepted nonzero exit')
    result = scope['run_write_command'](warning, folder, accepted_returncodes=(0, 1))
    assert result['returncode'] == 1 and 'source encoding' in result['output'] and not result['truncated']
    try:
        scope['run_write_command']([sys.executable, '-c', "import sys; print('Fatal source error', file=sys.stderr); sys.exit(2)"],
                                   folder, accepted_returncodes=(0, 1))
    except subprocess.CalledProcessError as exc:
        assert 'Fatal source error' in str(exc)
    else:
        raise AssertionError('Fatal write error accepted')
    result = scope['run_write_command']([sys.executable, '-c', "print('x'*9000)"], folder)
    assert result['truncated'] and len(result['output']) <= 8000
    try:
        scope['run_write_command']([sys.executable, '-c', 'import time; time.sleep(30)'], folder, timeout=0.1)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError('Write timeout ignored')
print('PASS: stdout/stderr diagnostics retained, default nonzero refusal, explicit warning handling, timeout')
