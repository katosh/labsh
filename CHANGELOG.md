# Changelog

## [0.5.1] - 2026-10-02

### Fixed — stale server records after a restart

- **A recycled pid no longer blocks `labsh start`.** The runtime dir
  (`.jupyter/share/jupyter/runtime/`) survives a host reboot or a
  container / PID-namespace restart; the server does not. Its
  `jpserver-<pid>.json` stayed behind, and labsh trusted it whenever
  `kill -0 <pid>` succeeded — which, after a restart, is often an
  unrelated process that inherited the pid. `labsh start` then reported
  "server is already running" and exited 1 indefinitely. A record now
  counts as live only when its pid (1) is running, (2) has a jupyter
  server argv (`jupyter-lab`, `jupyter-server`, `-m jupyterlab`, …), and
  (3) started no later than the record was written (60 s slack for
  network-filesystem clock skew). `start`/`serve` move a stale record
  aside to `jpserver-<pid>.json.stale` with a warning; `url` and
  `ext` ignore it.
- **`labsh stop` can no longer SIGTERM an unrelated process.** The
  helper's check was a substring test for `jupyter` anywhere in argv,
  which a kernel (`…/share/jupyter/runtime/kernel-*.json`) or any process
  under a `.jupyter/` path satisfies. `status` and `stop` now apply the
  same identity check as the shell side.
- **Helper venv rebuild survives a damaged lockfile.** After a `/tmp`
  wipe the helper venv is rebuilt from `.jupyter/.labshvenv.lock`. An
  empty lockfile was "restored" (installing nothing) and a truncated one
  restored a partial set, leaving every helper command broken. The
  rebuild now re-checks the required packages and installs what is still
  missing, and the lockfile is written via temp file + rename so an
  interrupted write cannot truncate it.

### Tests

- Planted `jpserver-*.json` records whose pid is a live unrelated
  process, or a jupyter-named process younger than the record: `start`
  proceeds (record moved aside), `url` ignores it, `stop` leaves the
  process alive. Positive control: a jupyter process older than its
  record still blocks a second start.
- Helper venv rebuild from an empty and from a truncated lockfile.

## [0.5.0] - 2026-07-06

### Added — kernel resilience: stop guardrail + hot extension installs

Motivated by a production incident where automated service restarts
destroyed an operator's warm kernel state (hours of interactive work).
Kernels cannot outlive the Jupyter server — jupyter-server shuts all
kernels down on exit (`cleanup_kernels` → `shutdown_all`), and
ipykernel's parent poller exits kernels within ~1s of the server dying
even on SIGKILL — so the only safe restart is a deliberate one.

- **`labsh stop` refuses while live kernels exist** (exit 3), listing
  each kernel's execution state, connected websocket clients, last
  activity, and owning notebook. `labsh stop --force` overrides. A
  server that no longer answers its REST API has nothing left to
  protect and is stopped without the check (automated recovery of a
  wedged server keeps working). Supervisors should *not* pass
  `--force` on automated bounce paths — the refusal is the signal
  that a healthy server holds state worth keeping.
- **`labsh ext install PKG...`** installs prebuilt JupyterLab
  extensions into the **running** server's environment. JupyterLab
  enumerates federated extensions at page-load time, so a browser
  refresh activates them — no server restart, kernels keep running.
  Installed specs persist in `.jupyter/labsh-extensions` and are
  re-included (via `--with`) on every future `labsh start`. Packages
  that also ship a jupyter *server* extension are detected and
  flagged as needing one deliberate restart for their backend.
- **`labsh ext list`** shows persisted specs and the labextensions
  present in the running server env.

### Fixed

- `labsh stop` now actually removes the stale `labsh.bg.pid` (the
  shell wrapper exec'd into the helper before reaching its cleanup
  line, so the pidfile was never deleted).

### Tests

- Regression: a barrage of client operations (`ps`, `find`, `status`,
  `inspect`, `exec` over native/ZMQ, notebook reads) leaves the server
  pid unchanged and kernel state intact — client ops can never
  signal/stop the server.
- Stop guardrail: refusal with live kernels (exit 3, server survives,
  state intact) and `--force` override.
- Hot extension install: package lands in the running server env, the
  live `/lab` page serves it without a restart, spec persists.

### Added — pip in the server env + `LABSH_WITH` seam

- The server env now ships `pip`, so JupyterLab's in-UI **Extension
  Manager** (which shells out to pip) works out of the box instead of
  being disabled in the uvx-built env.
- New `LABSH_WITH` environment variable: a whitespace-separated list
  of extra packages appended as `--with` to the server build — bake in
  labextensions or their backends (e.g.
  `LABSH_WITH="jupyterlab-code-formatter black isort" labsh start`)
  without forking the baseline. Mirrors the `LABSH_AI` toggle.

### Added — native Jupyter Server interface (REST + kernel websocket)

Runtime kernel commands (`ps`, `find`, `exec`, `inspect`, `notebook
attach/append/replace`) now interface with a running Jupyter server
*natively* instead of reaching around it via connection files:

- **Discovery**: `GET /api/sessions` supplies the server's own
  notebook↔kernel mapping; `GET /api/kernels` covers sessionless
  kernels. Servers are found from the project's
  `.jupyter/share/jupyter/runtime/jpserver-*.json`, from **parent
  directories'** `.jupyter` trees (a project nested under a
  workspace-wide server root now finds that server), and from the
  standard jupyter runtime dir — or targeted explicitly with
  `--server URL` / `LABSH_SERVER` (token via `--token`, `LABSH_TOKEN`,
  or `?token=` in the URL; verified via `GET /api/status`).
- **Execution**: `kernel exec` / `inspect` / `notebook append
  --execute` run through the kernel websocket
  (`/api/kernels/<id>/channels`, Jupyter message protocol
  `execute_request`), authenticated with the server token, over http
  or https. Output shapes and exit codes are unchanged.
- **Fallback**: kernels with no serving session keep working over the
  classic connection-file/ZMQ path; `--local` forces it. `labsh stop`
  deliberately stays project-scoped and never touches a parent
  workspace server.

### Added — `labsh kernel interrupt` / `labsh kernel restart`

`POST /api/kernels/<id>/interrupt|restart` via the owning server;
`interrupt` falls back to SIGINT for server-less local kernels.

### Fixed — `kernel exec -n <name>` hung with empty output on a busy tree

Notebook→kernel resolution (`_match_notebook`, behind `kernel
exec`/`find`/`inspect` and the `notebook` subcommands) turned a
basename query into candidate paths by walking the *entire* directory
tree under `$PWD` (`PROJECT_DIR.rglob(<name>)`). Invoked from a high-up
working directory over a large NFS tree — exactly how the root-server
wrappers call labsh from inside a project — that walk took minutes, so
`kernel exec -n <name>` appeared to **hang and return nothing**: the
process was stuck globbing the filesystem during kernel *resolution*,
before a single byte of code ever reached the kernel. `kernel ps` and
`-k <id>` selectors were unaffected because they never glob, which is
why discovery looked healthy while exec stalled.

Resolution now matches the query against the notebook paths the kernels
already carry (from `jupyter_session` / the server's `/api/sessions`),
touching the filesystem at most for a single `stat`, never a recursive
walk — the glob was redundant anyway, since a globbed path only ever
matched a kernel whose absolute notebook path is already known here. The
`$PWD`-scoping the walk used to provide is preserved as a tie-breaker
when a basename matches kernels in several locations. Independent of the
transport, so both the native websocket path and the ZMQ fallback are
fixed.

### Changed

- The labsh helper venv now also carries `websocket-client` (pure
  Python, zero transitive deps — the one new dependency backing the
  websocket transport; `jupyter_client` offers no websocket client).
- Every REST call to the server is now bounded by a timeout (default
  30 s; 5 s for the stop guardrail's kernel probes; 120 s for
  kernel-launching calls) — a half-dead server that accepts the TCP
  connection but never answers can no longer hang labsh commands
  forever.
- The websocket→ZMQ execution fallback only triggers for failures
  *before* the execute request is sent (connect, handshake, dead-kernel
  preflight). A mid-flight failure after the code may have reached the
  kernel now surfaces as an error instead of silently re-running the
  cell over ZMQ — side-effecting code can no longer execute twice.

## [0.4.1] - 2026-05-07

### Fixed — kernels built against Lmod-managed Python die on startup

`labsh kernel add` (and bare `python -m ipykernel install`) wrote a
`kernel.json` with no `env` block. When the venv's Python comes from
an Lmod module — realpath under `/app/software/...`, e.g.
`Python/3.12.3-GCCcore-13.3.0` — the dynamic loader couldn't find
`libpython*.so.*` (or the matching OpenSSL / libffi / SQLite runtime
libs) and the kernel died with **"Kernel died before replying to
kernel_info"**. This commonly surfaced as "labsh notebook attach
doesn't accept `--kernel`" — the flag was fine; the kernel just
couldn't start.

`kernel add` now detects an Lmod-managed Python and bakes the
registering shell's `$LD_LIBRARY_PATH` into `kernel.json`'s `env`
block. No-op on non-Lmod hosts.

### Added — `labsh kernel register` for external venvs

```
labsh kernel register --project DIR [--name NAME] [--display-name DISP]
                      [--ld-library-path PATHS] [--notebook PATH]
                      [--no-attach]
```

Registers an existing venv at `DIR/.venv` as a kernelspec under the
current labsh project's `.jupyter/share/jupyter/kernels/<NAME>/`,
with `LD_LIBRARY_PATH` baked into `kernel.json` for Lmod pythons.
Auto-installs `ipykernel` into the venv if missing.

With `--notebook PATH`, also pins `metadata.kernelspec.name=<NAME>`
into the notebook (creating a fresh `nbformat`-v4 file if missing)
and calls `labsh notebook attach PATH --kernel-name <NAME>` unless
`--no-attach`.

Closes [#2](https://github.com/katosh/labsh/issues/2).

## [0.4.0] - 2026-04-30

### Changed — startup stability

- **`notebook-intelligence` is now opt-in.** The previous default pulled
  `tiktoken`, which only ships `manylinux_2_28` wheels — on hosts with
  glibc < 2.28 (Ubuntu 18.04, RHEL 7, the agent-sandbox) the source build
  needs Rust 1.85+ and routinely fails, so `labsh start` failed out of the
  box. With this release `labsh start` works on any host that can install
  the base JupyterLab stack.

### Added

- **`--with-ai` flag and `LABSH_AI=1` env var** to enable the
  `notebook-intelligence` extension. `--no-ai` overrides `LABSH_AI=1` on
  the CLI.

## [0.3.0] - 2026-04-17

### Changed — security posture

- **Default bind is now `0.0.0.0`** (was `127.0.0.1`). Matches the typical
  HPC use case of accessing the notebook from another machine on the
  institute network. Override with `--ip 127.0.0.1` or `IP=127.0.0.1`.
- **Stable auth token** at `.jupyter/token` (mode `0600`). Generated once
  per project, persists across restarts, passed to Jupyter via the
  `JUPYTER_TOKEN` env var so it does **not** appear in `ps` output on
  multi-user machines. Any on-machine process with read access to the
  file can authenticate without coordination.
- **Loud warning** when binding publicly over plain HTTP, recommending
  `--https`. The warning fires regardless of password state because a
  plain-HTTP token still travels in cleartext.
- Auto-writes `.jupyter/.gitignore` covering `token`, runtime state, and
  `jupyter_server_config.json` so secrets don't accidentally get committed.

### Added

- **`labsh token`:** Print / rotate / locate the stable auth token.
  - `labsh token` — print (create on first call)
  - `labsh token --rotate` — regenerate; restart server to apply
  - `labsh token --path` — print absolute path of the token file

## [0.2.2] - 2026-04-17

### Fixed

- **False "binding to 0.0.0.0 without a password" warning:** The password
  check was only looking at `ServerApp.password` / `NotebookApp.password`,
  missing the `IdentityProvider.hashed_password` key written by
  `jupyter server password` under Jupyter Server 2.x (what `labsh password`
  invokes). It now also recognises `PasswordIdentityProvider.hashed_password`
  and passwords set via `jupyter_server_config.py`.

## [0.2.1] - 2026-04-15

### Added

- **`labsh version`:** New command (also `--version`, `-V`) prints the installed version.
- **`notebook append` auto-creates notebooks:** Appending to a non-existent notebook
  now creates it automatically instead of crashing with HTTP 404.

### Fixed

- **`labshsh` typo** in `_labsh_kernel.py` stop subparser help text.
- **Integration tests on Python 3.13:** Test suite now uses the same Python version
  as the labsh runtime (`LAB_PYTHON` variable) instead of the system default,
  fixing failures on systems where Python 3.13 lacks sqlite3.
- **`LAB_PYTHON` variable:** Extracted hard-coded Python version into a single
  variable in `bin/labsh`, referenced by both the script and the test suite.

## [0.2.0] - 2026-04-15

### Added

- **`kernel add` accepts packages:** `labsh kernel add mykernel pandas numpy` installs
  extra packages alongside ipykernel in a single command.
- **`kernel install`:** Install packages into the kernel `.venv` after creation
  (`labsh kernel install scikit-learn seaborn`). Alias: `kernel pip`.
- **`kernel shell`:** Drop into a subshell with `.venv` activated. Alias: `kernel sh`.
- **`kernel run`:** Run a one-off command inside the `.venv`
  (`labsh kernel run -- python script.py`).
- **Claude Code skill:** `/labsh` slash command installed to `~/.claude/commands/`
  via `make install`. Provides agent-oriented quick reference.
- **GitHub Actions CI:** Test suite (41 tests) and shellcheck run on every push/PR.
- **Comprehensive test suite:** 21 unit tests (venv/kernel management) + 20
  integration tests (live JupyterLab server, kernel exec, notebook editing).

## [0.1.0] - 2026-04-15

### Added

- Initial release as `labsh` (renamed from `lab`).
- Server lifecycle: `labsh start`, `labsh stop`, `labsh status`, `labsh url`,
  `labsh password`.
- Kernel management: `labsh kernel add`, `labsh kernel list`, `labsh kernel remove`.
- Live kernel execution: `labsh kernel exec`, `labsh kernel inspect`.
- Kernel discovery: `labsh kernel ps`, `labsh kernel find`.
- Notebook editing: `labsh notebook attach`, `labsh notebook cells`,
  `labsh notebook show`, `labsh notebook append`, `labsh notebook replace`.
- HTTPS with auto-generated self-signed certs (`--https`).
- NFS performance: helper venv on `/tmp`, uv cache redirect, `.pyc` pre-compilation.
- Homebrew formula (`brew tap katosh/tools && brew install labsh`).
