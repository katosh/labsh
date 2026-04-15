---
description: Project-local JupyterLab management — stateful kernel execution, notebook editing, server lifecycle. Use when working with Jupyter notebooks or needing persistent Python state across turns.
---

# labsh — JupyterLab CLI for agents

`labsh` runs project-local JupyterLab with all config under `./.jupyter`.
Variables, dataframes, and models loaded in one `kernel exec` call persist
for the next — no need to reload expensive state each turn.

## Startup

```bash
labsh kernel add            # one-time: create .venv, register kernel
labsh start                 # daemonize JupyterLab (log: .jupyter/labsh.bg.log)
```

## Core workflow

```bash
# Execute code in a live kernel (state persists between calls)
labsh kernel exec -n analysis.ipynb "df = pd.read_csv('data.csv')"
labsh kernel exec -n analysis.ipynb "df.shape"

# Inspect live variables
labsh kernel inspect -n analysis.ipynb

# Write results into the notebook (with outputs)
labsh notebook append -n analysis.ipynb --execute "df.describe()"
```

## Selectors

`-n NOTEBOOK` matches by: absolute path, relative path, basename, or
substring. Ambiguous matches print candidates and exit non-zero.

`-k SELECTOR` accepts PID or short kernel id (first 8 hex chars).

With neither flag and exactly one running kernel, it auto-selects.

## Command reference

### Server
| Command | What it does |
|---------|-------------|
| `labsh` | Run JupyterLab in foreground |
| `labsh start [--https] [--port N]` | Daemonize |
| `labsh stop` | Stop the server |
| `labsh status` | Show servers and kernels |
| `labsh url` | Print access URL with token |

### Kernels
| Command | What it does |
|---------|-------------|
| `labsh kernel add [NAME]` | Create `.venv`, register kernel |
| `labsh kernel list` | List registered kernels |
| `labsh kernel remove NAME` | Unregister |
| `labsh kernel ps` | List running kernels |
| `labsh kernel find QUERY` | Find kernel by notebook path |
| `labsh kernel exec [-n NB\|-k K] CODE` | Execute code (state persists) |
| `labsh kernel exec [-n NB\|-k K] -f FILE` | Execute from file (`-` for stdin) |
| `labsh kernel inspect [-n NB\|-k K]` | Show live variables |

### Notebooks
| Command | What it does |
|---------|-------------|
| `labsh notebook attach PATH` | Ensure kernel exists for notebook |
| `labsh notebook cells [-n PATH]` | List cells |
| `labsh notebook show [-n PATH] IDX` | Print cell source + outputs |
| `labsh notebook append [-n PATH] [--execute] CODE` | Append cell |
| `labsh notebook replace [-n PATH] IDX [--execute] CODE` | Replace cell |

## Tips

- **Port conflicts:** `labsh start --port 9012` (auto-increments if taken)
- **HTTPS:** `labsh start --https` (auto-generates self-signed cert, binds 0.0.0.0)
- **Code from stdin:** `echo "print('hi')" | labsh kernel exec -n nb.ipynb -f -`
- **No browser needed:** agent drives kernels over ZMQ, fully headless
- Requires `uv` on PATH
