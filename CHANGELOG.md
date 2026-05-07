# Changelog

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
