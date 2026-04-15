# Changelog

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
