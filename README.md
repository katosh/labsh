# labsh

Project-local JupyterLab management from the command line. Designed for
both human users and AI coding agents on shared HPC infrastructure.

`labsh` runs JupyterLab from the current directory with all configuration
and kernels under `./.jupyter`, so each project gets its own reproducible
Jupyter setup with no writes to `~/.local`. It also provides CLI access to
running kernels so code can be executed against a live notebook's state
without clicking through the web UI.

## Install

### Homebrew (Linux)

```bash
brew tap katosh/tools
brew install labsh
```

### From source

```bash
git clone https://github.com/katosh/labsh.git
cd labsh
make install                  # installs to ~/.local/bin/labsh
```

Requires `uv` on PATH. Install it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Quick Start

```bash
cd /path/to/project
labsh kernel add                # create .venv, register kernel
labsh start                     # start JupyterLab in background
labsh kernel exec "print('hello')"
```

### Two Run Modes

**Background mode** (for agents or headless work):

```bash
labsh start                           # daemonize
labsh notebook attach analysis.ipynb  # spawn a kernel
labsh kernel exec -n analysis.ipynb "df = pd.read_csv('data.csv')"
labsh kernel exec -n analysis.ipynb "df.shape"
labsh stop                            # shut down
```

**Foreground mode** (tmux pane, SSH session):

```bash
labsh                                 # runs in foreground, prints URL
# In another terminal:
labsh kernel exec -n analysis.ipynb "df.describe()"
```

## Commands

### Server lifecycle

| Command | Description |
|---------|-------------|
| `labsh` | Run JupyterLab in foreground |
| `labsh start [--https] [--port N] [--ip ADDR]` | Start in background |
| `labsh stop` | Stop the server |
| `labsh status` | Show running servers and kernels |
| `labsh url` | Print the server access URL (with token) |
| `labsh password` | Set a persistent password |

### Kernel management

| Command | Description |
|---------|-------------|
| `labsh kernel add [NAME] [PKGS...]` | Create `.venv`, register kernel, install packages |
| `labsh kernel install PKGS...` | Install packages into the kernel `.venv` |
| `labsh kernel shell` | Enter a subshell with `.venv` activated |
| `labsh kernel run [--] CMD...` | Run a command inside the `.venv` |
| `labsh kernel list` | List registered kernels |
| `labsh kernel remove NAME` | Unregister a kernel |

### Runtime kernel operations

| Command | Description |
|---------|-------------|
| `labsh kernel ps` | List running kernels |
| `labsh kernel find QUERY` | Find kernel by notebook path |
| `labsh kernel exec [-n NB\|-k K] CODE` | Execute code in a live kernel |
| `labsh kernel inspect [-n NB\|-k K]` | Show live variables |

### Notebook editing

| Command | Description |
|---------|-------------|
| `labsh notebook attach PATH` | Ensure a kernel exists for a notebook |
| `labsh notebook cells [-n PATH]` | List cells |
| `labsh notebook show [-n PATH] IDX` | Print cell source and outputs |
| `labsh notebook append [-n PATH] [--execute] CODE` | Append a cell |
| `labsh notebook replace [-n PATH] IDX [--execute] CODE` | Replace a cell |

## HTTPS and Port Selection

```bash
labsh start --https                   # auto-generates self-signed cert, binds 0.0.0.0
labsh start --https --port 9012       # custom port
labsh start --port 9012               # HTTP, custom port
```

When a port is in use, `labsh` auto-increments up to 10 times. Set a
persistent password with `labsh password` or rely on the auto-generated
token. Use `labsh url` to retrieve the URL later.

## NFS Performance

On shared HPC with NFS storage (common at research institutions), Python
venv startup is ~46x slower than on local storage. `labsh` mitigates this
automatically:

- `UV_CACHE_DIR` defaults to `/tmp/uv-cache-$UID`
- `UV_LINK_MODE` defaults to `copy` (avoids cross-fs hardlink failures)
- Labsh helper venv lives on `/tmp` (symlinked from `.jupyter/.labshvenv`)
- Package installs are followed by `compileall` to pre-generate `.pyc`
- Lockfiles pin exact versions for reproducible rebuilds after `/tmp` wipe

## Architecture

`labsh` is a bash script (`bin/labsh`) that manages JupyterLab server
lifecycle and delegates kernel/notebook operations to a Python helper
(`bin/_labsh_kernel.py`).

Two separate venvs are used:
- **Project venv** (`.venv/`): contains `ipykernel` and user packages.
  Created by `labsh kernel add`. Stays on the project filesystem.
- **Labsh helper venv** (`.jupyter/.labshvenv` -> `/tmp/...`): contains
  `psutil`, `jupyter_client`, `nbformat` for CLI operations. Created
  automatically. Lives on `/tmp` for speed.

The JupyterLab server itself runs via `uvx` (no venv needed).

## For AI Agents

`labsh` is designed to be used by AI coding agents that need stateful
computation. The full agent-oriented reference (discovery internals,
selector semantics, workflow recipes) is in [doc/labsh.md](doc/labsh.md).

Key patterns:
- Variables, dataframes, and models loaded in one `kernel exec` call
  persist for the next. No need to reload expensive state each turn.
- `kernel inspect` gives a compact `whos`-style listing of live variables.
- `notebook append --execute` runs code and writes the cell with outputs
  into the notebook file.
- Notebook path resolution (`-n` flag) matches against running kernels
  first, then filesystem paths.

## Testing

```bash
make check                    # requires uv
VERBOSE=1 ./test-labsh.sh       # verbose output
```

## License

MIT. See [LICENSE](LICENSE).
