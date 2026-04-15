# lab

Project-local JupyterLab management from the command line. Designed for
both human users and AI coding agents on shared HPC infrastructure.

`lab` runs JupyterLab from the current directory with all configuration
and kernels under `./.jupyter`, so each project gets its own reproducible
Jupyter setup with no writes to `~/.local`. It also provides CLI access to
running kernels so code can be executed against a live notebook's state
without clicking through the web UI.

## Install

### Homebrew (Linux)

```bash
brew tap katosh/tools
brew install lab
```

### From source

```bash
git clone https://github.com/katosh/lab.git
cd lab
make install                  # installs to ~/.local/bin/lab
```

Requires `uv` on PATH. Install it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Quick Start

```bash
cd /path/to/project
lab kernel add                # create .venv, register kernel
lab start                     # start JupyterLab in background
lab kernel exec "print('hello')"
```

### Two Run Modes

**Background mode** (for agents or headless work):

```bash
lab start                           # daemonize
lab notebook attach analysis.ipynb  # spawn a kernel
lab kernel exec -n analysis.ipynb "df = pd.read_csv('data.csv')"
lab kernel exec -n analysis.ipynb "df.shape"
lab stop                            # shut down
```

**Foreground mode** (tmux pane, SSH session):

```bash
lab                                 # runs in foreground, prints URL
# In another terminal:
lab kernel exec -n analysis.ipynb "df.describe()"
```

## Commands

### Server lifecycle

| Command | Description |
|---------|-------------|
| `lab` | Run JupyterLab in foreground |
| `lab start [--https] [--port N] [--ip ADDR]` | Start in background |
| `lab stop` | Stop the server |
| `lab status` | Show running servers and kernels |
| `lab url` | Print the server access URL (with token) |
| `lab password` | Set a persistent password |

### Kernel management

| Command | Description |
|---------|-------------|
| `lab kernel add [NAME]` | Create `.venv` and register kernel |
| `lab kernel list` | List registered kernels |
| `lab kernel remove NAME` | Unregister a kernel |

### Runtime kernel operations

| Command | Description |
|---------|-------------|
| `lab kernel ps` | List running kernels |
| `lab kernel find QUERY` | Find kernel by notebook path |
| `lab kernel exec [-n NB\|-k K] CODE` | Execute code in a live kernel |
| `lab kernel inspect [-n NB\|-k K]` | Show live variables |

### Notebook editing

| Command | Description |
|---------|-------------|
| `lab notebook attach PATH` | Ensure a kernel exists for a notebook |
| `lab notebook cells [-n PATH]` | List cells |
| `lab notebook show [-n PATH] IDX` | Print cell source and outputs |
| `lab notebook append [-n PATH] [--execute] CODE` | Append a cell |
| `lab notebook replace [-n PATH] IDX [--execute] CODE` | Replace a cell |

## HTTPS and Port Selection

```bash
lab start --https                   # auto-generates self-signed cert, binds 0.0.0.0
lab start --https --port 9012       # custom port
lab start --port 9012               # HTTP, custom port
```

When a port is in use, `lab` auto-increments up to 10 times. Set a
persistent password with `lab password` or rely on the auto-generated
token. Use `lab url` to retrieve the URL later.

## NFS Performance

On shared HPC with NFS storage (common at research institutions), Python
venv startup is ~46x slower than on local storage. `lab` mitigates this
automatically:

- `UV_CACHE_DIR` defaults to `/tmp/uv-cache-$UID`
- `UV_LINK_MODE` defaults to `copy` (avoids cross-fs hardlink failures)
- Lab helper venv lives on `/tmp` (symlinked from `.jupyter/.labvenv`)
- Package installs are followed by `compileall` to pre-generate `.pyc`
- Lockfiles pin exact versions for reproducible rebuilds after `/tmp` wipe

## Architecture

`lab` is a bash script (`bin/lab`) that manages JupyterLab server
lifecycle and delegates kernel/notebook operations to a Python helper
(`bin/_lab_kernel.py`).

Two separate venvs are used:
- **Project venv** (`.venv/`): contains `ipykernel` and user packages.
  Created by `lab kernel add`. Stays on the project filesystem.
- **Lab helper venv** (`.jupyter/.labvenv` -> `/tmp/...`): contains
  `psutil`, `jupyter_client`, `nbformat` for CLI operations. Created
  automatically. Lives on `/tmp` for speed.

The JupyterLab server itself runs via `uvx` (no venv needed).

## For AI Agents

`lab` is designed to be used by AI coding agents that need stateful
computation. The full agent-oriented reference (discovery internals,
selector semantics, workflow recipes) is in [doc/lab.md](doc/lab.md).

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
VERBOSE=1 ./test-lab.sh       # verbose output
```

## License

MIT. See [LICENSE](LICENSE).
