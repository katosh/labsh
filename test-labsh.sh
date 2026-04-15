#!/usr/bin/env bash
# test-labsh.sh — comprehensive tests for the labsh CLI.
#
# Two sections:
#   1. Unit tests — venv/kernel management (no server needed, fast)
#   2. Integration tests — kernel exec, notebook editing (real JupyterLab)
#
# Requirements: uv on PATH.  Run from the repo root:
#     ./test-labsh.sh
# Or with verbose output:
#     VERBOSE=1 ./test-labsh.sh

set -euo pipefail

: "${VERBOSE:=0}"

# ── Paths ──────────────────────────────────────────────────────────────────

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAB="$REPO_DIR/bin/labsh"
HELPER="$REPO_DIR/bin/_labsh_kernel.py"

UNIT_WORK_DIR="$(mktemp -d -t labsh-unit-XXXXXX)"
INTEG_WORK_DIR="$(mktemp -d -t labsh-integ-XXXXXX)"

pass=0
fail=0
total=0

# ── Helpers ────────────────────────────────────────────────────────────────

cleanup() {
    if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "${PORT:-}" ]; then
        pkill -f "jupyter-lab.*--port $PORT" 2>/dev/null || true
    fi
    if [ -n "${INTEG_RUNTIME_DIR:-}" ]; then
        pkill -f "ipykernel_launcher.*$INTEG_RUNTIME_DIR" 2>/dev/null || true
    fi
    rm -rf "$UNIT_WORK_DIR" "$INTEG_WORK_DIR"
}
trap cleanup EXIT

log() { echo "  $*"; }
vlog() { if [ "$VERBOSE" = "1" ]; then echo "  [v] $*"; fi; }

run_test() {
    local name="$1"; shift
    total=$((total + 1))
    if [ "$VERBOSE" = "1" ]; then echo "── $name"; fi
    if "$@"; then
        pass=$((pass + 1))
        echo "  ✓ $name"
    else
        fail=$((fail + 1))
        echo "  ✗ $name"
    fi
}

# Run labsh in the unit test working directory.
lab_unit() {
    (cd "$UNIT_WORK_DIR" && "$LAB" "$@")
}

command -v uv >/dev/null 2>&1 || { echo "SKIP: uv not found"; exit 0; }

# ══════════════════════════════════════════════════════════════════════════
# UNIT TESTS — venv/kernel management (no server needed)
# ══════════════════════════════════════════════════════════════════════════

echo "test-labsh: unit tests in $UNIT_WORK_DIR"
echo

# --- kernel add ---

test_kernel_add_creates_venv() {
    lab_unit kernel add testkernel >/dev/null 2>&1
    [ -x "$UNIT_WORK_DIR/.venv/bin/python" ]
}
run_test "kernel add creates .venv" test_kernel_add_creates_venv

test_kernel_add_registers_kernelspec() {
    local ks="$UNIT_WORK_DIR/.jupyter/share/jupyter/kernels/testkernel/kernel.json"
    [ -f "$ks" ] && python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
assert 'testkernel' in d.get('display_name', ''), f'unexpected display_name: {d}'
" "$ks"
}
run_test "kernel add registers kernelspec" test_kernel_add_registers_kernelspec

test_kernel_add_with_packages() {
    # Use a fresh subdir to get a clean state
    local d="$UNIT_WORK_DIR/pkg-test"
    mkdir -p "$d"
    (cd "$d" && "$LAB" kernel add mypkg six >/dev/null 2>&1)
    "$d/.venv/bin/python" -c "import six" 2>/dev/null
}
run_test "kernel add with packages installs them" test_kernel_add_with_packages

test_kernel_add_default_name() {
    local d="$UNIT_WORK_DIR/My-Project"
    mkdir -p "$d"
    (cd "$d" && "$LAB" kernel add >/dev/null 2>&1)
    [ -d "$d/.jupyter/share/jupyter/kernels/my-project" ]
}
run_test "kernel add defaults name to sanitized dirname" test_kernel_add_default_name

test_kernel_add_name_sanitization() {
    local d="$UNIT_WORK_DIR/sanitize-test"
    mkdir -p "$d"
    (cd "$d" && "$LAB" kernel add "My Kernel!!" >/dev/null 2>&1)
    [ -d "$d/.jupyter/share/jupyter/kernels/my-kernel" ]
}
run_test "kernel add sanitizes name" test_kernel_add_name_sanitization

# --- kernel list ---

test_kernel_list() {
    local out
    out="$(lab_unit kernel list 2>&1)"
    echo "$out" | grep -q "testkernel"
}
run_test "kernel list shows registered kernel" test_kernel_list

# --- kernel remove ---

test_kernel_remove() {
    # Add a throwaway kernel, then remove it
    local d="$UNIT_WORK_DIR/rm-test"
    mkdir -p "$d"
    (cd "$d" && "$LAB" kernel add removeme >/dev/null 2>&1)
    (cd "$d" && "$LAB" kernel remove removeme >/dev/null 2>&1)
    [ ! -d "$d/.jupyter/share/jupyter/kernels/removeme" ]
}
run_test "kernel remove unregisters kernelspec" test_kernel_remove

# --- kernel install ---

test_kernel_install() {
    lab_unit kernel install six >/dev/null 2>&1
    "$UNIT_WORK_DIR/.venv/bin/python" -c "import six" 2>/dev/null
}
run_test "kernel install adds packages to .venv" test_kernel_install

test_kernel_install_updates_lockfile() {
    local lock="$UNIT_WORK_DIR/.jupyter/.kernel-deps.lock"
    [ -f "$lock" ] && grep -qi "six" "$lock"
}
run_test "kernel install updates lockfile" test_kernel_install_updates_lockfile

test_kernel_install_no_venv() {
    local d="$UNIT_WORK_DIR/no-venv"
    mkdir -p "$d"
    local err
    err="$( (cd "$d" && "$LAB" kernel install requests) 2>&1 )" && return 1
    echo "$err" | grep -q "no .venv found"
}
run_test "kernel install fails without .venv" test_kernel_install_no_venv

test_kernel_install_no_packages() {
    local err
    err="$(lab_unit kernel install 2>&1)" && return 1
    echo "$err" | grep -q "no packages specified"
}
run_test "kernel install fails with no packages" test_kernel_install_no_packages

# --- kernel run ---

test_kernel_run() {
    local out
    out="$(lab_unit kernel run -- python -c "print('from_venv')" 2>&1)"
    echo "$out" | grep -q "from_venv"
}
run_test "kernel run executes command in .venv" test_kernel_run

test_kernel_run_uses_venv_python() {
    local out
    out="$(lab_unit kernel run -- python -c "import sys; print(sys.prefix)" 2>&1)"
    echo "$out" | grep -q ".venv"
}
run_test "kernel run uses .venv python" test_kernel_run_uses_venv_python

test_kernel_run_no_separator() {
    local out
    out="$(lab_unit kernel run python -c "print('nosep')" 2>&1)"
    echo "$out" | grep -q "nosep"
}
run_test "kernel run works without -- separator" test_kernel_run_no_separator

test_kernel_run_no_venv() {
    local d="$UNIT_WORK_DIR/no-venv-run"
    mkdir -p "$d"
    local err
    err="$( (cd "$d" && "$LAB" kernel run -- echo hi) 2>&1 )" && return 1
    echo "$err" | grep -q "no .venv found"
}
run_test "kernel run fails without .venv" test_kernel_run_no_venv

test_kernel_run_no_command() {
    local err
    err="$(lab_unit kernel run 2>&1)" && return 1
    echo "$err" | grep -q "no command specified"
}
run_test "kernel run fails with no command" test_kernel_run_no_command

# --- kernel shell ---

test_kernel_shell_env() {
    local out
    # Run /bin/sh as SHELL, feed it a command via stdin, verify env
    # shellcheck disable=SC2016  # inner shell expands $VIRTUAL_ENV
    out="$(echo 'echo "VENV=$VIRTUAL_ENV"; exit' | \
        timeout 5 env SHELL=/bin/sh \
        sh -c "cd '$UNIT_WORK_DIR' && '$LAB' kernel shell" 2>/dev/null)"
    echo "$out" | grep -q "VENV=$UNIT_WORK_DIR/.venv"
}
run_test "kernel shell sets VIRTUAL_ENV" test_kernel_shell_env

test_kernel_shell_path() {
    local out
    # shellcheck disable=SC2016  # inner shell expands $PATH
    out="$(echo 'echo "$PATH"; exit' | \
        timeout 5 env SHELL=/bin/sh \
        sh -c "cd '$UNIT_WORK_DIR' && '$LAB' kernel shell" 2>/dev/null)"
    # PATH should start with .venv/bin
    echo "$out" | grep -q "$UNIT_WORK_DIR/.venv/bin"
}
run_test "kernel shell prepends .venv/bin to PATH" test_kernel_shell_path

test_kernel_shell_no_venv() {
    local d="$UNIT_WORK_DIR/no-venv-shell"
    mkdir -p "$d"
    local err
    err="$( (cd "$d" && "$LAB" kernel shell) 2>&1 )" && return 1
    echo "$err" | grep -q "no .venv found"
}
run_test "kernel shell fails without .venv" test_kernel_shell_no_venv

# --- kernel help ---

test_kernel_help() {
    local out
    out="$(lab_unit kernel help 2>&1)"
    echo "$out" | grep -q "kernel add" && \
    echo "$out" | grep -q "kernel install" && \
    echo "$out" | grep -q "kernel shell" && \
    echo "$out" | grep -q "kernel run"
}
run_test "kernel help lists all subcommands" test_kernel_help

# --- labsh help ---

test_labsh_help() {
    local out
    out="$("$LAB" help 2>&1)"
    echo "$out" | grep -q "Project-local JupyterLab"
}
run_test "labsh help prints usage" test_labsh_help

echo
echo "test-labsh: unit tests done — $pass/$total passed"
echo

# ══════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — kernel exec, notebooks (real JupyterLab server)
# ══════════════════════════════════════════════════════════════════════════

INTEG_VENV_DIR="$INTEG_WORK_DIR/.venv"
INTEG_JUPYTER_CONFIG_DIR="$INTEG_WORK_DIR/.jupyter"
INTEG_JUPYTER_DATA_DIR="$INTEG_WORK_DIR/.jupyter/share/jupyter"
INTEG_RUNTIME_DIR="$INTEG_JUPYTER_DATA_DIR/runtime"

export JUPYTER_CONFIG_DIR="$INTEG_JUPYTER_CONFIG_DIR"
export JUPYTER_DATA_DIR="$INTEG_JUPYTER_DATA_DIR"

PORT=$((20000 + RANDOM % 40000))
TOKEN="testtoken_$(openssl rand -hex 4 2>/dev/null || echo labsh)"

lab_py() {
    cd "$INTEG_WORK_DIR" && "$INTEG_VENV_DIR/bin/python" "$HELPER" "$@"
}

wait_for_server() {
    local max_wait="${1:-15}"
    local _i
    for _i in $(seq 1 "$max_wait"); do
        compgen -G "$INTEG_RUNTIME_DIR/jpserver-*.json" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

wait_for_kernel() {
    local max_wait="${1:-15}"
    local _i
    for _i in $(seq 1 "$max_wait"); do
        lab_py kernel ps 2>/dev/null | grep -q 'hello.ipynb' && return 0
        sleep 1
    done
    return 1
}

echo "test-labsh: integration tests in $INTEG_WORK_DIR (port $PORT)"

mkdir -p "$INTEG_JUPYTER_CONFIG_DIR" "$INTEG_JUPYTER_DATA_DIR" "$INTEG_RUNTIME_DIR"

uv venv "$INTEG_VENV_DIR" >/dev/null 2>&1
uv pip install --python "$INTEG_VENV_DIR/bin/python" \
    jupyterlab ipykernel psutil nbformat 2>&1 | tail -1

cat > "$INTEG_WORK_DIR/hello.ipynb" <<'NB'
{
 "cells": [
  {"cell_type": "code", "id": "seed", "execution_count": null,
   "metadata": {}, "outputs": [],
   "source": "x = 42\nprint('seeded')"}
 ],
 "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
 "nbformat": 4, "nbformat_minor": 5
}
NB

cd "$INTEG_WORK_DIR"
"$INTEG_VENV_DIR/bin/jupyter" lab \
    --port "$PORT" --ip 127.0.0.1 --no-browser \
    --IdentityProvider.token="$TOKEN" \
    --ServerApp.password='' \
    > "$INTEG_WORK_DIR/jupyter.log" 2>&1 &
SERVER_PID=$!
vlog "server pid $SERVER_PID"

echo "test-labsh: waiting for server..."
if ! wait_for_server 15; then
    echo "FAIL: jupyter-lab did not start within 15 s"
    cat "$INTEG_WORK_DIR/jupyter.log"
    exit 1
fi
vlog "server ready"

curl -sf "http://127.0.0.1:$PORT/api/sessions" \
    -H "Authorization: token $TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"kernel":{"name":"python3"},"name":"hello.ipynb","path":"hello.ipynb","type":"notebook"}' \
    > /dev/null

echo "test-labsh: waiting for kernel..."
if ! wait_for_kernel 15; then
    echo "FAIL: kernel did not appear within 15 s"
    exit 1
fi
vlog "kernel ready"

echo
echo "test-labsh: running integration tests"

# --- kernel ps ---

test_kernel_ps() {
    lab_py kernel ps 2>&1 | grep -q "hello.ipynb"
}
run_test "kernel ps shows hello.ipynb" test_kernel_ps

test_kernel_ps_columns() {
    local out
    out="$(lab_py kernel ps 2>&1)"
    echo "$out" | head -1 | grep -q "PID" && echo "$out" | head -1 | grep -q "NOTEBOOK"
}
run_test "kernel ps has PID and NOTEBOOK columns" test_kernel_ps_columns

# --- kernel find ---

test_kernel_find_exact() {
    lab_py kernel find hello.ipynb 2>&1 | grep -q "hello.ipynb"
}
run_test "kernel find hello.ipynb" test_kernel_find_exact

test_kernel_find_substring() {
    lab_py kernel find hello 2>&1 | grep -q "hello.ipynb"
}
run_test "kernel find by substring" test_kernel_find_substring

test_kernel_find_miss() {
    ! lab_py kernel find nonexistent 2>&1
}
run_test "kernel find miss exits non-zero" test_kernel_find_miss

# --- kernel exec ---

test_exec_simple() {
    lab_py kernel exec -n hello.ipynb "print('hi')" 2>&1 | grep -q "hi"
}
run_test "kernel exec simple print" test_exec_simple

test_exec_state_persistence() {
    lab_py kernel exec -n hello.ipynb "lab_test_var = 1234" >/dev/null 2>&1
    lab_py kernel exec -n hello.ipynb "print(lab_test_var)" 2>&1 | grep -q "1234"
}
run_test "kernel exec state persists across calls" test_exec_state_persistence

test_exec_error_exit_code() {
    ! lab_py kernel exec -n hello.ipynb "1/0" 2>/dev/null
}
run_test "kernel exec error returns non-zero" test_exec_error_exit_code

test_exec_error_traceback() {
    local err
    err="$(lab_py kernel exec -n hello.ipynb "1/0" 2>&1 || true)"
    echo "$err" | grep -q "ZeroDivisionError"
}
run_test "kernel exec error includes traceback" test_exec_error_traceback

test_exec_stdin() {
    echo 'print("from_stdin")' | lab_py kernel exec -n hello.ipynb -f - 2>&1 | grep -q "from_stdin"
}
run_test "kernel exec from stdin via -f -" test_exec_stdin

test_exec_auto_select() {
    # With a single running kernel, -n/-k should not be required.
    # Skip if other kernels are running (shared systems).
    local kcount
    kcount="$(lab_py kernel ps 2>/dev/null | tail -n +2 | wc -l)"
    if [ "$kcount" -ne 1 ]; then
        echo "  [skip] $kcount kernels running (need exactly 1)" >&2
        return 0  # pass-through: can't test auto-select on multi-kernel systems
    fi
    local out _attempt
    for _attempt in 1 2 3; do
        out="$(lab_py kernel exec "print('auto')" 2>&1 || true)"
        if echo "$out" | grep -q "auto"; then
            return 0
        fi
        sleep 1
    done
    return 1
}
run_test "kernel exec auto-selects single kernel" test_exec_auto_select

# --- kernel inspect ---

test_inspect() {
    lab_py kernel exec -n hello.ipynb "inspect_var = [1,2,3]" >/dev/null 2>&1
    lab_py kernel inspect -n hello.ipynb 2>&1 | grep -q "inspect_var"
}
run_test "kernel inspect shows defined variable" test_inspect

test_inspect_filter() {
    local out
    out="$(lab_py kernel inspect -n hello.ipynb inspect_var 2>&1)"
    echo "$out" | grep -q "inspect_var" && ! echo "$out" | grep -q "lab_test_var"
}
run_test "kernel inspect pattern filter" test_inspect_filter

# --- status ---

test_status() {
    local out
    out="$(lab_py status 2>&1)"
    echo "$out" | grep -q "servers:" && echo "$out" | grep -q "kernels:"
}
run_test "status shows servers and kernels sections" test_status

# --- notebook cells ---

test_nb_cells() {
    lab_py notebook cells -n "$INTEG_WORK_DIR/hello.ipynb" 2>&1 | grep -q "x = 42"
}
run_test "notebook cells lists seed cell" test_nb_cells

# --- notebook show ---

test_nb_show() {
    lab_py notebook show -n "$INTEG_WORK_DIR/hello.ipynb" 0 2>&1 | grep -q "x = 42"
}
run_test "notebook show prints cell source" test_nb_show

# --- notebook append ---

test_nb_append() {
    lab_py notebook append -n "$INTEG_WORK_DIR/hello.ipynb" "y = x * 2" 2>&1
    lab_py notebook cells -n "$INTEG_WORK_DIR/hello.ipynb" 2>&1 | grep -q "y = x"
}
run_test "notebook append adds a code cell" test_nb_append

test_nb_append_execute() {
    lab_py notebook append --execute -n "$INTEG_WORK_DIR/hello.ipynb" "print('appended_exec')" 2>&1
    python3 -c "
import json, sys
nb = json.load(open('$INTEG_WORK_DIR/hello.ipynb'))
last = nb['cells'][-1]
print(json.dumps(last.get('outputs', [])))
" | grep -q "appended_exec"
}
run_test "notebook append --execute persists output" test_nb_append_execute

test_nb_append_markdown() {
    lab_py notebook append --markdown -n "$INTEG_WORK_DIR/hello.ipynb" "# A heading" 2>&1
    lab_py notebook cells -n "$INTEG_WORK_DIR/hello.ipynb" 2>&1 | grep -q "# A heading"
}
run_test "notebook append --markdown adds markdown cell" test_nb_append_markdown

# --- notebook replace ---

test_nb_replace() {
    lab_py notebook replace -n "$INTEG_WORK_DIR/hello.ipynb" 0 "x = 999; print('replaced')" 2>&1
    lab_py notebook show -n "$INTEG_WORK_DIR/hello.ipynb" 0 2>&1 | grep -q "x = 999"
}
run_test "notebook replace rewrites cell" test_nb_replace

# ── Summary ────────────────────────────────────────────────────────────────

echo
echo "test-labsh: $pass/$total passed, $fail failed"
if [ "$fail" -gt 0 ]; then
    exit 1
fi
