#!/usr/bin/env python3
"""
_labsh_kernel.py — backend for `labsh` subcommands that discover, attach to, and
drive running Jupyter kernels.

Two transports:

  * NATIVE (preferred): when a running Jupyter Server owns the kernel — i.e.
    the kernel appears in the server's /api/sessions or /api/kernels — labsh
    talks to the server directly: REST for discovery, session management,
    interrupt/restart and notebook contents; the kernel websocket
    (/api/kernels/<id>/channels, Jupyter message protocol) for execution and
    inspection. Auth is the server's token; http and https both work.

  * LOCAL (fallback): kernels with no serving session (bare `jupyter
    console`, a kernel whose server died, ...) are driven the classic way —
    psutil process scan for `-m ipykernel_launcher -f <connection-file>`,
    then ZMQ via jupyter_client. `--local` forces this path.

This file is invoked by bin/labsh. It is intentionally a single file with no
non-stdlib dependencies beyond psutil, jupyter_client, nbformat, and
websocket-client — all of which are ensured in the labsh helper venv by
bin/labsh before dispatching here.

Server discovery order (first hit wins for --server; otherwise merged):
  1. Explicit --server URL (token via --token, $LABSH_TOKEN, or ?token= in
     the URL). Verified live via GET /api/status.
  2. jpserver-<pid>.json files in the project runtime dir
     (./.jupyter/share/jupyter/runtime), pid-verified.
  3. The same walk applied to parent directories' .jupyter trees — so a
     project nested under a workspace-wide server root finds that server.
  4. jupyter_core's default runtime dir (what `jupyter server list` reads).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


# ---------------------------------------------------------------------------
# Paths / environment
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(os.environ.get("PWD") or os.getcwd()).resolve()
JUPYTER_CONFIG_DIR = Path(
    os.environ.get("JUPYTER_CONFIG_DIR") or (PROJECT_DIR / ".jupyter")
)
JUPYTER_DATA_DIR = Path(
    os.environ.get("JUPYTER_DATA_DIR") or (JUPYTER_CONFIG_DIR / "share" / "jupyter")
)
JUPYTER_RUNTIME_DIR = JUPYTER_DATA_DIR / "runtime"

ENV_SERVER = "LABSH_SERVER"
ENV_TOKEN = "LABSH_TOKEN"

PROTOCOL_VERSION = "5.3"


def eprint(*args: Any, **kwargs: Any) -> None:
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Kernel model
# ---------------------------------------------------------------------------


@dataclass
class Kernel:
    pid: int | None
    username: str
    connection_file: Path | None
    short_id: str
    kernelspec: str | None
    notebook_path: Path | None  # absolute when the owning root is known
    connection: dict[str, Any] = field(default_factory=dict)
    server: "LabServer | None" = None  # owning Jupyter server, if any
    kernel_id: str | None = None  # server-side kernel uuid

    @property
    def native(self) -> bool:
        return self.server is not None and bool(self.kernel_id)

    def as_row(self) -> dict[str, str]:
        return {
            "PID": str(self.pid) if self.pid else "-",
            "ID": self.short_id,
            "KERNEL": self.kernelspec or "-",
            "NOTEBOOK": (
                str(self.notebook_path) if self.notebook_path else "<unknown>"
            ),
        }


def _kernel_key(k: Kernel) -> tuple:
    """Stable identity for dedup across match passes."""
    return (k.pid, k.kernel_id, str(k.connection_file or ""))


def _is_ipykernel(proc_info: dict[str, Any]) -> bool:
    cmdline = proc_info.get("cmdline") or []
    return (
        len(cmdline) > 2
        and "-m" in cmdline
        and "ipykernel_launcher" in cmdline
        and "-f" in cmdline
    )


def discover_kernels(current_user_only: bool = True) -> list[Kernel]:
    """Scan processes for running ipykernel_launcher instances (LOCAL path).

    We identify the connection file from the `-f` flag and read it directly.
    `jupyter_session` (added by jupyter_server) maps the kernel to its
    notebook; when absent (classic notebook, bare `jupyter console`, etc.)
    the kernel is still returned with notebook_path=None.
    """
    current_user = getpass.getuser() if current_user_only else None
    seen: set[tuple[str, ...]] = set()
    out: list[Kernel] = []

    for proc in psutil.process_iter(attrs=["pid", "cmdline", "username"]):
        info = proc.info
        if not _is_ipykernel(info):
            continue
        cmdline = tuple(info.get("cmdline") or ())
        if cmdline in seen:
            continue
        seen.add(cmdline)

        username = info.get("username") or ""
        if current_user and username != current_user:
            continue

        try:
            idx = cmdline.index("-f")
        except ValueError:
            continue
        if idx + 1 >= len(cmdline):
            continue

        cf = Path(cmdline[idx + 1])
        try:
            conn = json.loads(cf.read_text())
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            conn = {}

        # short id: the uuid fragment of `kernel-<uuid>.json`
        stem_parts = cf.stem.split("-")
        short_id = stem_parts[1] if len(stem_parts) > 1 else cf.stem
        short_id = short_id[:8]

        nb_raw = conn.get("jupyter_session")
        nb_path = Path(nb_raw).resolve() if nb_raw else None

        out.append(
            Kernel(
                pid=info["pid"],
                username=username,
                connection_file=cf,
                short_id=short_id,
                kernelspec=conn.get("kernel_name"),
                notebook_path=nb_path,
                connection=conn,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Server discovery
# ---------------------------------------------------------------------------


@dataclass
class LabServer:
    pid: int | None
    url: str
    token: str
    root_dir: Path | None  # None when unknown (explicit --server URL)
    secure: bool
    port: int
    runtime_file: Path | None
    source: str = "project"  # project | parent | runtime | explicit

    @property
    def api_base(self) -> str:
        return self.url.rstrip("/")


def _server_from_runtime_file(jf: Path, source: str) -> LabServer | None:
    """Parse one jpserver-<pid>.json, returning a live LabServer or None.

    Only files whose pid is alive in this PID namespace are considered
    (sandbox-safe: orphaned files from a previous run are ignored).
    """
    try:
        data = json.loads(jf.read_text())
    except (json.JSONDecodeError, PermissionError, FileNotFoundError, OSError):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int):
        return None
    if not psutil.pid_exists(pid):
        return None
    try:
        proc = psutil.Process(pid)
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    # Sanity: ensure this really is a jupyter process. This guards against
    # PID reuse after the server died without cleaning up its json.
    if not any("jupyter" in part for part in cmdline):
        return None
    root_raw = data.get("root_dir")
    return LabServer(
        pid=pid,
        url=data.get("url", ""),
        token=data.get("token") or "",
        root_dir=Path(root_raw) if root_raw else PROJECT_DIR,
        secure=bool(data.get("secure")),
        port=int(data.get("port") or 0),
        runtime_file=jf,
        source=source,
    )


def _scan_runtime_dir(runtime_dir: Path, source: str) -> list[LabServer]:
    out: list[LabServer] = []
    try:
        if not runtime_dir.is_dir():
            return out
        files = sorted(runtime_dir.glob("jpserver-*.json"))
    except OSError:
        return out
    for jf in files:
        s = _server_from_runtime_file(jf, source)
        if s is not None:
            out.append(s)
    return out


def discover_servers() -> list[LabServer]:
    """Project-scoped discovery: jpserver-*.json in THIS project's runtime
    dir only. Used by `labsh stop`, which must never reach outside the
    project (a parent workspace server is not ours to kill)."""
    return _scan_runtime_dir(JUPYTER_RUNTIME_DIR, "project")


def _explicit_server(url: str, token: str | None) -> LabServer:
    """Build a LabServer from an explicit URL; verify it responds."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        _die(f"labsh: --server URL must be http(s)://..., got '{url}'")
    if token is None:
        qs = urllib.parse.parse_qs(parsed.query)
        token = (qs.get("token") or [None])[0]
    clean = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", "", "")
    )
    server = LabServer(
        pid=None,
        url=clean,
        token=token or "",
        root_dir=None,
        secure=parsed.scheme == "https",
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        runtime_file=None,
        source="explicit",
    )
    # Fail fast with a clear message if unreachable or unauthorized.
    ServerClient(server).request("GET", "api/status")
    return server


def discover_all_servers(
    explicit: str | None = None, token: str | None = None
) -> list[LabServer]:
    """Discovery for the native path.

    Explicit URL (flag or $LABSH_SERVER) short-circuits everything else.
    Otherwise: project runtime dir, then each parent directory's
    .jupyter/share/jupyter/runtime (a workspace-wide server whose root
    contains this project), then jupyter_core's default runtime dir (the
    same files `jupyter server list` reads). Deduped by URL.
    """
    explicit = explicit or os.environ.get(ENV_SERVER)
    token = token or os.environ.get(ENV_TOKEN)
    if explicit:
        return [_explicit_server(explicit, token)]

    out: list[LabServer] = []
    seen_urls: set[str] = set()

    def add(servers: list[LabServer]) -> None:
        for s in servers:
            key = s.api_base
            if key and key not in seen_urls:
                seen_urls.add(key)
                out.append(s)

    add(_scan_runtime_dir(JUPYTER_RUNTIME_DIR, "project"))
    for parent in PROJECT_DIR.parents:
        add(
            _scan_runtime_dir(
                parent / ".jupyter" / "share" / "jupyter" / "runtime", "parent"
            )
        )
    try:
        from jupyter_core.paths import jupyter_runtime_dir  # type: ignore

        add(_scan_runtime_dir(Path(jupyter_runtime_dir()), "runtime"))
    except Exception:
        pass
    return out


def server_for_path(servers: list[LabServer], path: Path) -> LabServer | None:
    """Return the server whose root_dir contains `path`. Prefers the longest
    matching root (most-specific server) so nested projects work."""
    path = path.resolve()
    best: LabServer | None = None
    best_len = -1
    for s in servers:
        if s.root_dir is None:
            continue
        try:
            root = s.root_dir.resolve()
        except OSError:
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if len(str(root)) > best_len:
            best = s
            best_len = len(str(root))
    return best


# ---------------------------------------------------------------------------
# Native server client (REST + kernel websocket)
# ---------------------------------------------------------------------------


class ServerClient:
    """Client for a running Jupyter server's REST API and kernel websockets."""

    def __init__(self, server: LabServer) -> None:
        self.server = server
        self._ssl_ctx = None
        if server.url.startswith("https://"):
            # Lab certs are often self-signed on HPC — trust them since we
            # only ever talk to servers we discovered locally (or that the
            # user pointed us at explicitly).
            self._ssl_ctx = ssl._create_unverified_context()

    # -- REST ---------------------------------------------------------------

    def request(self, method: str, api_path: str, body: dict | None = None) -> Any:
        url = f"{self.server.api_base}/{api_path}"
        headers = {"Content-Type": "application/json"}
        if self.server.token:
            headers["Authorization"] = f"token {self.server.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"labsh: server {method} /{api_path} failed: "
                f"HTTP {e.code} {e.read().decode(errors='replace')}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"labsh: cannot reach jupyter server at {self.server.url}: {e}"
            ) from e
        return json.loads(raw) if raw else {}

    def sessions(self) -> list[dict]:
        result = self.request("GET", "api/sessions")
        return result if isinstance(result, list) else []

    def kernels(self) -> list[dict]:
        result = self.request("GET", "api/kernels")
        return result if isinstance(result, list) else []

    def create_session(self, path: str, name: str, kernel_name: str) -> dict:
        return self.request(
            "POST",
            "api/sessions",
            {
                "kernel": {"name": kernel_name},
                "name": name,
                "path": path,
                "type": "notebook",
            },
        )

    def delete_session(self, session_id: str) -> None:
        self.request("DELETE", f"api/sessions/{urllib.parse.quote(session_id)}")

    def interrupt_kernel(self, kernel_id: str) -> None:
        self.request(
            "POST", f"api/kernels/{urllib.parse.quote(kernel_id)}/interrupt"
        )

    def restart_kernel(self, kernel_id: str) -> None:
        self.request("POST", f"api/kernels/{urllib.parse.quote(kernel_id)}/restart")

    # -- kernel websocket -----------------------------------------------------

    def _ws_connect(self, kernel_id: str, session: str):
        import websocket  # type: ignore

        base = self.server.api_base
        ws_base = "ws" + base[len("http"):]  # http->ws, https->wss
        url = (
            f"{ws_base}/api/kernels/{urllib.parse.quote(kernel_id)}/channels"
            f"?session_id={session}"
        )
        headers = []
        if self.server.token:
            headers.append(f"Authorization: token {self.server.token}")
        sslopt = {"cert_reqs": ssl.CERT_NONE} if ws_base.startswith("wss") else None
        try:
            return websocket.create_connection(
                url, header=headers, sslopt=sslopt, timeout=10
            )
        except Exception as e:
            raise RuntimeError(
                f"labsh: cannot open kernel websocket at {self.server.url}: {e}"
            ) from e

    @staticmethod
    def _jupyter_msg(msg_type: str, content: dict, session: str, channel: str) -> dict:
        return {
            "header": {
                "msg_id": uuid.uuid4().hex,
                "username": "labsh",
                "session": session,
                "msg_type": msg_type,
                "version": PROTOCOL_VERSION,
                "date": datetime.now(timezone.utc).isoformat(),
            },
            "parent_header": {},
            "metadata": {},
            "content": content,
            "channel": channel,
            "buffers": [],
        }

    @staticmethod
    def _recv_msg(ws) -> dict | None:
        """One websocket frame -> parsed Jupyter message, or None to skip."""
        import websocket  # type: ignore

        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except (websocket.WebSocketConnectionClosedException, OSError) as e:
            raise RuntimeError(
                f"labsh: kernel websocket closed unexpectedly: {e}"
            ) from e
        if isinstance(raw, bytes):  # binary subprotocol frame — not requested
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _await_reply(self, ws, msg_id: str, reply_type: str, timeout: float) -> dict:
        """Wait for a shell reply to msg_id, ignoring everything else."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._recv_msg(ws)
            if msg is None:
                continue
            if (msg.get("parent_header") or {}).get("msg_id") != msg_id:
                continue
            if msg.get("channel") == "shell" and msg.get("msg_type") == reply_type:
                return msg
        raise RuntimeError(
            f"labsh: kernel did not answer {reply_type.replace('_reply', '')} "
            f"within {timeout:.0f}s — it may be dead or busy"
        )

    def execute(
        self,
        kernel_id: str,
        code: str,
        *,
        timeout: float | None,
        capture: bool,
        short_id: str = "",
    ) -> tuple[int, str, str, list[dict], int | None]:
        """Execute `code` through the server's kernel websocket.

        Same contract as execute_in_kernel(): returns
        (exit_code, captured_stdout, captured_stderr, outputs, execution_count).
        """
        session = uuid.uuid4().hex
        ws = self._ws_connect(kernel_id, session)
        try:
            # Preflight: prove the kernel answers at all (parity with the
            # ZMQ path's wait_for_ready) so timeout=None can't hang forever
            # on a dead kernel.
            info = self._jupyter_msg("kernel_info_request", {}, session, "shell")
            ws.settimeout(2)
            ws.send(json.dumps(info))
            try:
                self._await_reply(ws, info["header"]["msg_id"], "kernel_info_reply", 10)
            except RuntimeError as e:
                raise RuntimeError(
                    f"labsh: kernel {short_id or kernel_id[:8]} is not responding: {e}"
                ) from e

            req = self._jupyter_msg(
                "execute_request",
                {
                    "code": code,
                    "silent": False,
                    "store_history": False,
                    "user_expressions": {},
                    "allow_stdin": False,
                    "stop_on_error": True,
                },
                session,
                "shell",
            )
            msg_id = req["header"]["msg_id"]
            per_msg_timeout = 5.0 if timeout is None else min(5.0, timeout)
            deadline = None if timeout is None else time.monotonic() + timeout
            ws.settimeout(per_msg_timeout)
            ws.send(json.dumps(req))

            collector = OutputCollector(capture)
            reply_count: int | None = None
            reply_seen = False
            idle_grace: float | None = None  # reply straggler allowance

            while not (collector.idle and reply_seen):
                now = time.monotonic()
                if deadline is not None and now > deadline:
                    return (
                        124,
                        "".join(collector.stdout_parts),
                        "".join(collector.stderr_parts),
                        collector.outputs,
                        collector.execution_count,
                    )
                if collector.idle:
                    if idle_grace is None:
                        idle_grace = now + 5.0
                    elif now > idle_grace:
                        break
                msg = self._recv_msg(ws)
                if msg is None:
                    continue
                if (msg.get("parent_header") or {}).get("msg_id") != msg_id:
                    continue
                channel = msg.get("channel")
                if channel == "iopub":
                    collector.handle_iopub(
                        msg.get("msg_type", ""), msg.get("content") or {}
                    )
                elif channel == "shell" and msg.get("msg_type") == "execute_reply":
                    reply_seen = True
                    ec = (msg.get("content") or {}).get("execution_count")
                    if ec is not None:
                        reply_count = ec

            execution_count = (
                collector.execution_count
                if collector.execution_count is not None
                else reply_count
            )
            return (
                1 if collector.had_error else 0,
                "".join(collector.stdout_parts),
                "".join(collector.stderr_parts),
                collector.outputs,
                execution_count,
            )
        finally:
            try:
                ws.close()
            except Exception:
                pass


def discover_server_kernels(server: LabServer) -> list[Kernel]:
    """Enumerate a server's kernels the server-native way: /api/sessions for
    the notebook<->kernel mapping, /api/kernels for sessionless kernels."""
    client = ServerClient(server)
    username = getpass.getuser()
    out: list[Kernel] = []
    seen_ids: set[str] = set()

    def abs_nb(rel: str | None) -> Path | None:
        if not rel:
            return None
        if server.root_dir is not None:
            return (server.root_dir / rel).resolve()
        return Path(rel)  # root unknown (explicit URL): keep server-relative

    for sess in client.sessions():
        kinfo = sess.get("kernel") or {}
        kid = kinfo.get("id")
        if not kid:
            continue
        seen_ids.add(kid)
        out.append(
            Kernel(
                pid=None,
                username=username,
                connection_file=None,
                short_id=kid[:8],
                kernelspec=kinfo.get("name"),
                notebook_path=abs_nb(sess.get("path")),
                server=server,
                kernel_id=kid,
            )
        )
    for kinfo in client.kernels():
        kid = kinfo.get("id")
        if not kid or kid in seen_ids:
            continue
        out.append(
            Kernel(
                pid=None,
                username=username,
                connection_file=None,
                short_id=kid[:8],
                kernelspec=kinfo.get("name"),
                notebook_path=None,
                server=server,
                kernel_id=kid,
            )
        )
    return out


def merge_kernels(local: list[Kernel], served: list[Kernel]) -> list[Kernel]:
    """Merge the process scan with server enumerations.

    A served kernel and a local process are the same kernel when the
    connection file is named kernel-<kernel_id>.json (jupyter_server's
    convention), or — as a fallback — when both map to the same notebook.
    Merged entries carry both identities: pid + connection file from the
    process, server + kernel id (authoritative notebook mapping) from the
    server, so execution prefers the native path.
    """
    by_uuid: dict[str, Kernel] = {}
    for k in local:
        if k.connection_file is None:
            continue
        stem = k.connection_file.stem
        u = stem[len("kernel-"):] if stem.startswith("kernel-") else stem
        by_uuid[u] = k

    out: list[Kernel] = []
    matched: set[int] = set()
    for s in served:
        lk = by_uuid.get(s.kernel_id or "")
        if lk is None and s.notebook_path is not None:
            for k in local:
                if (
                    id(k) not in matched
                    and k.notebook_path is not None
                    and k.notebook_path == s.notebook_path
                ):
                    lk = k
                    break
        if lk is not None:
            matched.add(id(lk))
            out.append(
                Kernel(
                    pid=lk.pid,
                    username=lk.username or s.username,
                    connection_file=lk.connection_file,
                    short_id=s.short_id,
                    kernelspec=s.kernelspec or lk.kernelspec,
                    notebook_path=s.notebook_path or lk.notebook_path,
                    connection=lk.connection,
                    server=s.server,
                    kernel_id=s.kernel_id,
                )
            )
        else:
            out.append(s)
    for k in local:
        if id(k) not in matched:
            out.append(k)
    return out


def gather_kernels(args: argparse.Namespace | None = None) -> list[Kernel]:
    """The kernel view every runtime subcommand works from.

    --local        -> process scan only (classic path).
    --server URL   -> that server's kernels only, all native.
    default        -> all discoverable servers' kernels merged with the
                      process scan; server-owned kernels prefer the native
                      transport, the rest stay on ZMQ.
    """
    local_only = bool(getattr(args, "local", False)) if args is not None else False
    server_url = getattr(args, "server", None) if args is not None else None
    token = getattr(args, "token", None) if args is not None else None
    if local_only and server_url:
        _die("labsh: pass either --server or --local, not both")
    if local_only:
        return discover_kernels()

    explicit = server_url or os.environ.get(ENV_SERVER)
    try:
        servers = discover_all_servers(server_url, token)
    except RuntimeError as e:
        _die(str(e))
    served: list[Kernel] = []
    for s in servers:
        try:
            served.extend(discover_server_kernels(s))
        except RuntimeError as e:
            if explicit:
                _die(str(e))
            eprint(f"labsh: warning: skipping server {s.url}: {e}")
    if explicit:
        return served
    return merge_kernels(discover_kernels(), served)


# ---------------------------------------------------------------------------
# Contents API client
# ---------------------------------------------------------------------------


class ContentsClient:
    """Minimal client for a running server's /api/contents endpoint.

    We use this for notebook reads/writes so the server broadcasts file-change
    events to any open frontend (avoiding the "file has been modified on
    disk" dialog that direct file writes trigger).
    """

    def __init__(self, server: LabServer) -> None:
        self._client = ServerClient(server)

    def _request(self, method: str, rel_path: str, body: dict | None = None) -> dict:
        return self._client.request(
            method, f"api/contents/{urllib.parse.quote(rel_path)}", body
        )

    def get_notebook(self, rel_path: str) -> dict:
        doc = self._request("GET", rel_path)
        content = doc.get("content")
        if not isinstance(content, dict):
            raise RuntimeError(
                f"labsh: {rel_path}: not a notebook (type={doc.get('type')})"
            )
        return content

    def put_notebook(self, rel_path: str, notebook: dict) -> dict:
        body = {"type": "notebook", "format": "json", "content": notebook}
        return self._request("PUT", rel_path, body)


def notebook_rel_path(nb_abs: Path, server: LabServer) -> str:
    """Return the notebook path relative to the server's root_dir (as the
    Contents API expects)."""
    if server.root_dir is None:
        raise RuntimeError(
            f"labsh: server {server.url} has an unknown root dir — pass the "
            f"notebook path relative to the server root instead"
        )
    nb_abs = nb_abs.resolve()
    root = server.root_dir.resolve()
    try:
        return str(nb_abs.relative_to(root))
    except ValueError as e:
        raise RuntimeError(
            f"labsh: notebook {nb_abs} is not under server root {root}"
        ) from e


# ---------------------------------------------------------------------------
# Selector resolution
# ---------------------------------------------------------------------------


def _match_notebook(kernels: list[Kernel], query: str) -> list[Kernel]:
    """Resolve a notebook-like query to kernels.

    Accepted forms:
      * absolute path                         (must match an existing kernel exactly)
      * path relative to $PWD                 (absolute-matched)
      * basename (globbed under $PWD)
      * substring match against the kernel's notebook path
    """
    if not kernels:
        return []

    query_path = Path(query)
    candidates: list[Path] = []
    if query_path.is_absolute():
        candidates.append(query_path.resolve())
    else:
        rel = (PROJECT_DIR / query_path).resolve()
        if rel.exists():
            candidates.append(rel)
        # glob on basename under project dir
        for hit in PROJECT_DIR.rglob(query_path.name):
            candidates.append(hit.resolve())

    def by_abs(abs_path: Path) -> list[Kernel]:
        return [
            k
            for k in kernels
            if k.notebook_path is not None and k.notebook_path == abs_path
        ]

    matches: list[Kernel] = []
    seen: set[tuple] = set()
    for c in candidates:
        for k in by_abs(c):
            if _kernel_key(k) not in seen:
                matches.append(k)
                seen.add(_kernel_key(k))
    if matches:
        return matches

    # Substring fallback against the notebook path
    q_lower = query.lower()
    q_abs_lower = str(
        (query_path if query_path.is_absolute() else PROJECT_DIR / query_path).resolve()
    ).lower()
    for k in kernels:
        if k.notebook_path is None or _kernel_key(k) in seen:
            continue
        nb_lower = str(k.notebook_path).lower()
        hit = q_lower in nb_lower
        if not hit and not k.notebook_path.is_absolute():
            # Served path relative to an unknown server root (explicit
            # --server URL): match when the query ends in that path.
            hit = (
                q_lower == nb_lower
                or q_lower.endswith("/" + nb_lower)
                or q_abs_lower.endswith("/" + nb_lower)
            )
        if hit:
            matches.append(k)
            seen.add(_kernel_key(k))
    return matches


def _match_kernel(kernels: list[Kernel], query: str) -> list[Kernel]:
    """Resolve a kernel selector: PID, (short) kernel id, or connection file
    path."""
    if query.isdigit():
        pid = int(query)
        return [k for k in kernels if k.pid == pid]
    query_path = Path(query)
    if query_path.is_absolute():
        qp = query_path.resolve()
        return [
            k
            for k in kernels
            if k.connection_file is not None and k.connection_file.resolve() == qp
        ]
    # short id (prefix match on hex) or server kernel uuid prefix
    q_lower = query.lower()
    return [
        k
        for k in kernels
        if k.short_id.lower().startswith(q_lower)
        or (k.kernel_id or "").lower().startswith(q_lower)
        or (
            k.connection_file is not None
            and k.connection_file.stem.lower().endswith(q_lower)
        )
    ]


def resolve_one(
    kernels: list[Kernel],
    *,
    notebook: str | None,
    kernel_sel: str | None,
    required_action: str,
) -> Kernel:
    """Resolve exactly one kernel from the selectors. Refuses to guess on
    ambiguity — that is the contract with the agent."""
    if notebook and kernel_sel:
        _die(f"{required_action}: pass either -n NOTEBOOK or -k KERNEL, not both")
    matches: list[Kernel]
    if notebook is not None:
        matches = _match_notebook(kernels, notebook)
        if not matches:
            _die(
                f"{required_action}: no running kernel matches notebook "
                f"'{notebook}'. `labsh kernel ps` to list live kernels, or "
                f"`labsh notebook attach <path>` to spawn one."
            )
    elif kernel_sel is not None:
        matches = _match_kernel(kernels, kernel_sel)
        if not matches:
            _die(f"{required_action}: no running kernel matches '{kernel_sel}'")
    else:
        if len(kernels) == 1:
            return kernels[0]
        if not kernels:
            _die(
                f"{required_action}: no running kernels. Start labsh "
                f"(`labsh` or `labsh start`) and attach a notebook."
            )
        _die_with_candidates(
            f"{required_action}: multiple kernels running; pass -n NOTEBOOK or -k PID/ID",
            kernels,
        )
    if len(matches) > 1:
        _die_with_candidates(
            f"{required_action}: ambiguous selector matched {len(matches)} kernels",
            matches,
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Kernel execution
# ---------------------------------------------------------------------------


class OutputCollector:
    """Accumulate iopub messages into labsh's output shape.

    Shared by the ZMQ and websocket transports so both produce identical
    stdout/stderr streams and nbformat-shaped output dicts.
    """

    def __init__(self, capture: bool) -> None:
        self.capture = capture
        self.had_error = False
        self.idle = False
        self.execution_count: int | None = None
        self.stdout_parts: list[str] = []
        self.stderr_parts: list[str] = []
        self.outputs: list[dict] = []

    def _emit_out(self, text: str) -> None:
        if self.capture:
            self.stdout_parts.append(text)
        else:
            sys.stdout.write(text)
            sys.stdout.flush()

    def _emit_err(self, text: str) -> None:
        if self.capture:
            self.stderr_parts.append(text)
        else:
            sys.stderr.write(text)
            sys.stderr.flush()

    def handle_iopub(self, msg_type: str, content: dict) -> None:
        if msg_type == "stream":
            text = content.get("text", "")
            if content.get("name") == "stdout":
                self._emit_out(text)
                self.outputs.append(
                    {"output_type": "stream", "name": "stdout", "text": text}
                )
            else:
                self._emit_err(text)
                self.outputs.append(
                    {"output_type": "stream", "name": "stderr", "text": text}
                )
        elif msg_type in ("execute_result", "display_data"):
            data = content.get("data") or {}
            text = data.get("text/plain", "")
            if text:
                self._emit_out(text + "\n")
            metadata = content.get("metadata") or {}
            if msg_type == "execute_result":
                self.execution_count = content.get("execution_count")
                self.outputs.append(
                    {
                        "output_type": "execute_result",
                        "execution_count": self.execution_count,
                        "data": data,
                        "metadata": metadata,
                    }
                )
            else:
                self.outputs.append(
                    {
                        "output_type": "display_data",
                        "data": data,
                        "metadata": metadata,
                    }
                )
        elif msg_type == "error":
            self.had_error = True
            tb = "\n".join(content.get("traceback", []))
            self._emit_err(tb + "\n")
            self.outputs.append(
                {
                    "output_type": "error",
                    "ename": content.get("ename", ""),
                    "evalue": content.get("evalue", ""),
                    "traceback": content.get("traceback", []),
                }
            )
        elif msg_type == "status" and content.get("execution_state") == "idle":
            self.idle = True


def execute_in_kernel(
    kernel: Kernel,
    code: str,
    *,
    timeout: float | None,
    capture: bool,
) -> tuple[int, str, str, list[dict], int | None]:
    """Execute `code` in the given kernel over ZMQ (connection-file path).

    If `capture` is False, stdout/stderr/result/display all stream directly to
    the parent process's stdout/stderr. If True, everything is accumulated
    and returned along with a list of nbformat-shaped output dicts (used by
    `notebook append --execute`).

    Returns (exit_code, captured_stdout, captured_stderr, outputs, execution_count).
    """
    from jupyter_client import BlockingKernelClient  # type: ignore

    kc = BlockingKernelClient(connection_file=str(kernel.connection_file))
    kc.load_connection_file()
    kc.start_channels()
    try:
        try:
            kc.wait_for_ready(timeout=10)
        except RuntimeError as e:
            raise RuntimeError(
                f"labsh: kernel {kernel.short_id} is not responding: {e}"
            ) from e

        msg_id = kc.execute(code, store_history=False, allow_stdin=False)
        collector = OutputCollector(capture)
        per_msg_timeout = 5.0 if timeout is None else min(5.0, timeout)
        deadline = None if timeout is None else time.monotonic() + timeout

        while not collector.idle:
            if deadline is not None and time.monotonic() > deadline:
                return (
                    124,
                    "".join(collector.stdout_parts),
                    "".join(collector.stderr_parts),
                    collector.outputs,
                    collector.execution_count,
                )
            try:
                msg = kc.get_iopub_msg(timeout=per_msg_timeout)
            except queue.Empty:
                continue
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            collector.handle_iopub(msg["msg_type"], msg["content"])

        execution_count = collector.execution_count
        # Drain the shell reply for the execution_count in the no-result case.
        try:
            reply = kc.get_shell_msg(timeout=5)
            if execution_count is None:
                execution_count = reply["content"].get("execution_count")
        except queue.Empty:
            pass

        return (
            1 if collector.had_error else 0,
            "".join(collector.stdout_parts),
            "".join(collector.stderr_parts),
            collector.outputs,
            execution_count,
        )
    finally:
        kc.stop_channels()


def execute(
    kernel: Kernel,
    code: str,
    *,
    timeout: float | None,
    capture: bool,
) -> tuple[int, str, str, list[dict], int | None]:
    """Transport dispatch: native websocket when a server owns the kernel,
    ZMQ via the connection file otherwise."""
    if kernel.native:
        try:
            return ServerClient(kernel.server).execute(
                kernel.kernel_id,  # type: ignore[arg-type]
                code,
                timeout=timeout,
                capture=capture,
                short_id=kernel.short_id,
            )
        except RuntimeError as e:
            # A merged kernel still has its connection file — fall back to
            # ZMQ rather than failing outright when the server-side path
            # breaks mid-flight (e.g. websocket handshake refused).
            if kernel.connection_file is None:
                raise
            eprint(f"{e}")
            eprint("labsh: falling back to direct connection-file (ZMQ) path")
            return execute_in_kernel(kernel, code, timeout=timeout, capture=capture)
    if kernel.connection_file is None:
        _die(
            f"labsh: kernel {kernel.short_id} has neither a reachable server "
            f"nor a connection file"
        )
    return execute_in_kernel(kernel, code, timeout=timeout, capture=capture)


# ---------------------------------------------------------------------------
# CLI: helpers
# ---------------------------------------------------------------------------


def _die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    eprint(msg)
    sys.exit(code)


def _die_with_candidates(msg: str, kernels: list[Kernel]) -> "NoReturn":  # type: ignore[name-defined]
    eprint(msg)
    eprint()
    _print_table([k.as_row() for k in kernels], stream=sys.stderr)
    sys.exit(2)


def _print_table(rows: list[dict[str, str]], *, stream=sys.stdout) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in cols}
    stream.write("  ".join(c.ljust(widths[c]) for c in cols) + "\n")
    for r in rows:
        stream.write("  ".join(r[c].ljust(widths[c]) for c in cols) + "\n")


def _read_code(args: argparse.Namespace) -> str:
    if args.file:
        if args.code:
            _die("labsh: pass either CODE or -f FILE, not both")
        if args.file == "-":
            return sys.stdin.read()
        return Path(args.file).read_text()
    if args.code:
        if len(args.code) == 1 and args.code[0] == "-":
            return sys.stdin.read()
        return "\n".join(args.code)
    _die("labsh: no code provided (pass CODE, -f FILE, or '-' for stdin)")


def _notebook_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "notebook", None)


def _kernel_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "kernel", None)


def _servers_for(args: argparse.Namespace | None) -> list[LabServer]:
    """Discover servers honoring --server/--token/env for a command."""
    server_url = getattr(args, "server", None) if args is not None else None
    token = getattr(args, "token", None) if args is not None else None
    try:
        return discover_all_servers(server_url, token)
    except RuntimeError as e:
        _die(str(e))


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_kernel_ps(args: argparse.Namespace) -> int:
    if getattr(args, "all_users", False):
        kernels = discover_kernels(current_user_only=False)
    else:
        kernels = gather_kernels(args)
    if not kernels:
        eprint("labsh: no running kernels")
        return 0
    _print_table([k.as_row() for k in kernels])
    return 0


def cmd_kernel_find(args: argparse.Namespace) -> int:
    kernels = gather_kernels(args)
    matches = _match_notebook(kernels, args.query)
    if not matches:
        eprint(f"labsh: no running kernel matches '{args.query}'")
        return 1
    _print_table([k.as_row() for k in matches])
    return 0


def cmd_kernel_exec(args: argparse.Namespace) -> int:
    kernels = gather_kernels(args)
    kernel = resolve_one(
        kernels,
        notebook=_notebook_arg(args),
        kernel_sel=_kernel_arg(args),
        required_action="kernel exec",
    )
    code = _read_code(args)
    exit_code, *_ = execute(kernel, code, timeout=args.timeout, capture=False)
    return exit_code


def cmd_kernel_inspect(args: argparse.Namespace) -> int:
    kernels = gather_kernels(args)
    kernel = resolve_one(
        kernels,
        notebook=_notebook_arg(args),
        kernel_sel=_kernel_arg(args),
        required_action="kernel inspect",
    )
    pattern = args.pattern or ""
    # Produce a compact "whos" style listing of user globals.
    code = (
        "def __lab_inspect(pat=''):\n"
        "    import sys\n"
        "    g = dict(globals())\n"
        "    hide = {'In','Out','exit','quit','get_ipython'}\n"
        "    rows = []\n"
        "    for k,v in g.items():\n"
        "        if k.startswith('_') or k in hide: continue\n"
        "        if pat and pat not in k: continue\n"
        "        t = type(v).__name__\n"
        "        try: size = len(v)\n"
        "        except Exception: size = ''\n"
        "        try:\n"
        "            s = repr(v)\n"
        "            if len(s) > 60: s = s[:57] + '...'\n"
        "        except Exception: s = '<unreprable>'\n"
        "        rows.append((k,t,str(size),s))\n"
        "    rows.sort()\n"
        "    w = [max(len(r[i]) for r in rows) if rows else 0 for i in range(4)]\n"
        "    hdr = ('NAME','TYPE','LEN','VALUE')\n"
        "    w = [max(w[i], len(hdr[i])) for i in range(4)]\n"
        "    sys.stdout.write('  '.join(hdr[i].ljust(w[i]) for i in range(4)) + '\\n')\n"
        "    for r in rows:\n"
        "        sys.stdout.write('  '.join(r[i].ljust(w[i]) for i in range(4)) + '\\n')\n"
        f"__lab_inspect({pattern!r})\n"
    )
    exit_code, *_ = execute(kernel, code, timeout=args.timeout, capture=False)
    return exit_code


def cmd_kernel_interrupt(args: argparse.Namespace) -> int:
    kernels = gather_kernels(args)
    kernel = resolve_one(
        kernels,
        notebook=_notebook_arg(args),
        kernel_sel=_kernel_arg(args),
        required_action="kernel interrupt",
    )
    if kernel.native:
        try:
            ServerClient(kernel.server).interrupt_kernel(kernel.kernel_id)  # type: ignore[arg-type]
        except RuntimeError as e:
            _die(str(e))
        eprint(f"labsh: interrupted kernel {kernel.short_id} (via server)")
        return 0
    if kernel.pid:
        try:
            os.kill(kernel.pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError) as e:
            _die(f"kernel interrupt: cannot signal pid {kernel.pid}: {e}")
        eprint(f"labsh: interrupted kernel {kernel.short_id} (SIGINT to {kernel.pid})")
        return 0
    _die(f"kernel interrupt: no way to reach kernel {kernel.short_id}")


def cmd_kernel_restart(args: argparse.Namespace) -> int:
    kernels = gather_kernels(args)
    kernel = resolve_one(
        kernels,
        notebook=_notebook_arg(args),
        kernel_sel=_kernel_arg(args),
        required_action="kernel restart",
    )
    if not kernel.native:
        _die(
            "kernel restart: only server-managed kernels can be restarted "
            "(no running jupyter server owns this kernel)"
        )
    try:
        ServerClient(kernel.server).restart_kernel(kernel.kernel_id)  # type: ignore[arg-type]
    except RuntimeError as e:
        _die(str(e))
    eprint(f"labsh: restarted kernel {kernel.short_id} — state is cleared")
    return 0


def _resolve_notebook_path(arg: str | None, args: argparse.Namespace | None = None) -> Path:
    """Resolve the argument to an absolute notebook path.

    Resolution order when arg is given:
      1. Running kernels' notebook paths (exact, basename, substring)
      2. Server root-relative path
      3. CWD-relative path
      4. Absolute path (if given as such)

    If no arg is given, fall back to the single running-kernel notebook.
    """
    if arg:
        # 1. Check running kernels first — they know where their notebooks are
        kernels = gather_kernels(args)
        matches = _match_notebook(kernels, arg)
        if len(matches) == 1 and matches[0].notebook_path is not None:
            return matches[0].notebook_path
        if len(matches) > 1:
            # Multiple matches — don't guess, but let caller proceed with
            # filesystem resolution (the kernel exec path will catch ambiguity
            # separately if needed)
            pass

        # 2. Server root-relative
        servers = _servers_for(args)
        if servers:
            for s in servers:
                if s.root_dir is None:
                    continue
                candidate = (s.root_dir / arg).resolve()
                if candidate.exists():
                    return candidate

        # 3. CWD-relative
        p = Path(arg)
        if p.is_absolute():
            return p.resolve()
        cwd_rel = (PROJECT_DIR / p).resolve()
        if cwd_rel.exists():
            return cwd_rel

        # 4. Glob basename under project-local server roots or CWD. Only
        # roots at/under PROJECT_DIR are globbed — a recursive walk of a
        # workspace-wide parent root would be pathologically slow on NFS.
        def under_project(d: Path) -> bool:
            try:
                d.resolve().relative_to(PROJECT_DIR)
            except (ValueError, OSError):
                return False
            return True

        search_dirs = [
            s.root_dir
            for s in servers
            if s.root_dir is not None and under_project(s.root_dir)
        ] or [PROJECT_DIR]
        for d in search_dirs:
            hits = list(d.rglob(p.name))
            if len(hits) == 1:
                return hits[0].resolve()

        # Fall through — return CWD-relative even if it doesn't exist yet
        # (caller will get a clear "file not found" from nbformat/server)
        return cwd_rel

    kernels = gather_kernels(args)
    nb_kernels = [k for k in kernels if k.notebook_path is not None]
    if len(nb_kernels) == 1:
        return nb_kernels[0].notebook_path  # type: ignore[return-value]
    if not nb_kernels:
        _die(
            "notebook: no -n NOTEBOOK given and no running kernel has an "
            "associated notebook. Pass -n explicitly."
        )
    _die_with_candidates(
        "notebook: -n NOTEBOOK is required (multiple live notebooks)",
        nb_kernels,
    )


def _new_notebook() -> dict:
    """Return a minimal empty notebook (nbformat v4)."""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3"},
        },
        "cells": [],
    }


def _load_notebook(
    nb_path: Path,
    *,
    allow_create: bool = False,
    args: argparse.Namespace | None = None,
) -> tuple[dict, LabServer | None, str]:
    """Load the notebook via a running jupyter server if possible, else from
    disk. Returns (notebook dict, server or None, path-key for saving).

    If allow_create is True and the notebook doesn't exist, returns a fresh
    empty notebook instead of raising an error."""
    nb_path = nb_path.resolve()
    server = server_for_path(_servers_for(args), nb_path)
    if server is not None:
        rel = notebook_rel_path(nb_path, server)
        client = ContentsClient(server)
        try:
            return client.get_notebook(rel), server, rel
        except RuntimeError:
            if allow_create:
                eprint(f"labsh: creating new notebook {nb_path.name}")
                return _new_notebook(), server, rel
            raise
    # Fallback: read directly.
    if allow_create and not nb_path.exists():
        eprint(f"labsh: creating new notebook {nb_path.name}")
        return _new_notebook(), None, str(nb_path)
    import nbformat  # type: ignore

    nb = nbformat.read(str(nb_path), as_version=4)
    return nb, None, str(nb_path)


def _save_notebook(
    nb: dict, server: LabServer | None, key: str, nb_path: Path
) -> None:
    if server is not None:
        ContentsClient(server).put_notebook(key, nb)
        return
    import nbformat  # type: ignore

    nbformat.write(nbformat.from_dict(nb), str(nb_path))


def _cell_snippet(cell: dict) -> str:
    src = cell.get("source", "")
    if isinstance(src, list):
        src = "".join(src)
    first = src.split("\n", 1)[0]
    if len(first) > 72:
        first = first[:69] + "..."
    return first


def cmd_notebook_cells(args: argparse.Namespace) -> int:
    nb_path = _resolve_notebook_path(args.notebook, args)
    nb, _server, _key = _load_notebook(nb_path, args=args)
    cells = nb.get("cells", [])
    rows = [
        {
            "IDX": str(i),
            "TYPE": cell.get("cell_type", "?"),
            "EC": str(cell.get("execution_count") or ""),
            "SOURCE": _cell_snippet(cell),
        }
        for i, cell in enumerate(cells)
    ]
    if not rows:
        eprint(f"labsh: {nb_path} has no cells")
        return 0
    _print_table(rows)
    return 0


def cmd_notebook_show(args: argparse.Namespace) -> int:
    nb_path = _resolve_notebook_path(args.notebook, args)
    nb, _server, _key = _load_notebook(nb_path, args=args)
    cells = nb.get("cells", [])
    if args.index < 0 or args.index >= len(cells):
        _die(f"notebook show: index {args.index} out of range (0..{len(cells) - 1})")
    cell = cells[args.index]
    src = cell.get("source", "")
    if isinstance(src, list):
        src = "".join(src)
    print(f"# cell {args.index} ({cell.get('cell_type', '?')})")
    print(src)
    outputs = cell.get("outputs") or []
    if outputs:
        print()
        print("# outputs")
        for out in outputs:
            ot = out.get("output_type")
            if ot == "stream":
                sys.stdout.write(out.get("text", ""))
            elif ot in ("execute_result", "display_data"):
                data = out.get("data") or {}
                text = data.get("text/plain", "")
                if text:
                    print(text)
            elif ot == "error":
                for line in out.get("traceback", []):
                    print(line)
    return 0


def _make_cell_id() -> str:
    import secrets

    return secrets.token_hex(4)


def _make_code_cell(
    source: str, outputs: list[dict] | None = None, execution_count: int | None = None
) -> dict:
    return {
        "cell_type": "code",
        "id": _make_cell_id(),
        "metadata": {},
        "execution_count": execution_count,
        "outputs": outputs or [],
        "source": source,
    }


def _make_md_cell(source: str) -> dict:
    return {
        "cell_type": "markdown",
        "id": _make_cell_id(),
        "metadata": {},
        "source": source,
    }


def cmd_notebook_append(args: argparse.Namespace) -> int:
    nb_path = _resolve_notebook_path(args.notebook, args)
    nb, server, key = _load_notebook(nb_path, allow_create=True, args=args)
    cells = nb.setdefault("cells", [])
    content = _read_code(args)

    if args.markdown:
        if args.execute:
            _die("notebook append: --execute is not meaningful for markdown cells")
        cells.append(_make_md_cell(content))
        _save_notebook(nb, server, key, nb_path)
        eprint(f"labsh: appended markdown cell at index {len(cells) - 1}")
        return 0

    exit_code = 0
    outputs: list[dict] = []
    execution_count: int | None = None
    if args.execute:
        kernels = gather_kernels(args)
        kernel = resolve_one(
            kernels,
            notebook=str(nb_path),
            kernel_sel=None,
            required_action="notebook append --execute",
        )
        exit_code, _stdout, _stderr, outputs, execution_count = execute(
            kernel, content, timeout=args.timeout, capture=True
        )
    cells.append(
        _make_code_cell(content, outputs=outputs, execution_count=execution_count)
    )
    _save_notebook(nb, server, key, nb_path)
    msg = f"labsh: appended code cell at index {len(cells) - 1}"
    if args.execute:
        msg += f" (executed, exit={exit_code})"
    eprint(msg)
    return exit_code


def cmd_notebook_replace(args: argparse.Namespace) -> int:
    nb_path = _resolve_notebook_path(args.notebook, args)
    nb, server, key = _load_notebook(nb_path, args=args)
    cells = nb.setdefault("cells", [])
    if args.index < 0 or args.index >= len(cells):
        _die(f"notebook replace: index {args.index} out of range (0..{len(cells) - 1})")
    content = _read_code(args)
    old = cells[args.index]
    ct = old.get("cell_type", "code")
    exit_code = 0
    outputs: list[dict] = []
    execution_count: int | None = None
    if args.execute:
        if ct != "code":
            _die("notebook replace: --execute only applies to code cells")
        kernels = gather_kernels(args)
        kernel = resolve_one(
            kernels,
            notebook=str(nb_path),
            kernel_sel=None,
            required_action="notebook replace --execute",
        )
        exit_code, _stdout, _stderr, outputs, execution_count = execute(
            kernel, content, timeout=args.timeout, capture=True
        )
    if ct == "code":
        cells[args.index] = _make_code_cell(
            content,
            outputs=outputs or old.get("outputs") or [],
            execution_count=execution_count
            if execution_count is not None
            else old.get("execution_count"),
        )
        cells[args.index]["id"] = old.get("id") or _make_cell_id()
    else:
        cells[args.index] = _make_md_cell(content)
        cells[args.index]["id"] = old.get("id") or _make_cell_id()
    _save_notebook(nb, server, key, nb_path)
    return exit_code


def cmd_notebook_attach(args: argparse.Namespace) -> int:
    """Ensure a kernel exists for the given notebook path by asking a running
    jupyter server to create a Session for it. Prints the resulting kernel
    row."""
    servers = _servers_for(args)
    explicit = getattr(args, "server", None) or os.environ.get(ENV_SERVER)

    if explicit and servers:
        server = servers[0]
        # Root dir unknown for explicit URLs: the notebook path must be
        # given relative to the server root.
        if args.notebook is None:
            _die("notebook attach: --server requires an explicit notebook PATH")
        rel = args.notebook
        if Path(rel).is_absolute():
            if server.root_dir is None:
                _die(
                    "notebook attach: pass the notebook path relative to the "
                    "server root when using --server"
                )
            rel = notebook_rel_path(Path(rel), server)
        nb_display = rel
    else:
        nb_path = _resolve_notebook_path(args.notebook, args)
        server = server_for_path(servers, nb_path)
        if server is None:
            _die(
                "notebook attach: no running jupyter server owns this notebook. "
                "Start one with `labsh` (foreground) or `labsh start` (background)."
            )
        rel = notebook_rel_path(nb_path, server)
        nb_display = str(nb_path)

    client = ServerClient(server)

    def served_rows() -> list[Kernel]:
        try:
            served = discover_server_kernels(server)
        except RuntimeError:
            return []
        hits = [
            k
            for k in served
            if k.notebook_path is not None
            and str(k.notebook_path).endswith(rel)
        ]
        if not hits:
            return []
        # Enrich with local pids where visible; keep only the hits (the merge
        # also returns unmatched local kernels, which are not ours to show).
        hit_ids = {k.kernel_id for k in hits}
        merged = merge_kernels(discover_kernels(), hits)
        return [k for k in merged if k.kernel_id in hit_ids]

    existing = [k for k in served_rows() if k.native]
    if existing:
        _print_table([k.as_row() for k in existing])
        return 0

    try:
        data = client.create_session(
            rel, Path(rel).name, args.kernel_name or "python3"
        )
    except RuntimeError as e:
        _die(f"notebook attach: {e}")
    kernel_id = (data.get("kernel") or {}).get("id", "?")
    eprint(f"labsh: created session for {nb_display} (kernel id {kernel_id})")
    for _ in range(20):
        time.sleep(0.25)
        hits = [k for k in served_rows() if k.kernel_id == kernel_id]
        if hits:
            _print_table([k.as_row() for k in hits])
            return 0
    eprint("labsh: session created but kernel did not appear yet")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    servers = _servers_for(args)
    if servers:
        rows = [
            {
                "PID": str(s.pid) if s.pid else "-",
                "URL": s.url,
                "ROOT": str(s.root_dir) if s.root_dir else "-",
                "TOKEN": "(set)" if s.token else "(none)",
                "SRC": s.source,
            }
            for s in servers
        ]
        print("servers:")
        _print_table(rows)
    else:
        print("servers: (none)")
    kernels = gather_kernels(args)
    if kernels:
        print()
        print("kernels:")
        _print_table([k.as_row() for k in kernels])
    else:
        print()
        print("kernels: (none)")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    # Deliberately project-scoped discovery: `stop` must never signal a
    # workspace-wide parent server found by walk-up discovery.
    servers = discover_servers()
    if not servers:
        eprint("labsh: no running labsh server to stop")
        return 1
    for s in servers:
        root = s.root_dir.resolve() if s.root_dir else None
        if args.all or root == PROJECT_DIR.resolve():
            try:
                os.kill(s.pid, signal.SIGTERM)  # type: ignore[arg-type]
                eprint(f"labsh: sent SIGTERM to labsh server pid {s.pid}")
            except ProcessLookupError:
                eprint(f"labsh: pid {s.pid} already gone")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _add_selector(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-n",
        "--notebook",
        help="Select by notebook path/glob/substring (resolved against running kernels' notebooks)",
    )
    p.add_argument(
        "-k",
        "--kernel",
        help="Select by kernel PID, short id, or connection file path",
    )


def _add_server_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--server",
        default=None,
        help=f"Target a specific jupyter server by URL (env: {ENV_SERVER}); "
        "token from --token, env, or ?token= in the URL",
    )
    p.add_argument(
        "--token",
        default=None,
        help=f"Auth token for --server (env: {ENV_TOKEN})",
    )
    p.add_argument(
        "--local",
        action="store_true",
        help="Force the connection-file/ZMQ path (skip server REST/websocket)",
    )


def _add_code_inputs(p: argparse.ArgumentParser) -> None:
    p.add_argument("code", nargs="*", help="Code to execute (or '-' for stdin)")
    p.add_argument("-f", "--file", help="Read code from FILE (or '-' for stdin)")
    p.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=None,
        help="Overall execution timeout in seconds (default: none)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labsh", add_help=True)
    sub = parser.add_subparsers(dest="group", required=True)

    # --- kernel ----------------------------------------------------------------
    p_k = sub.add_parser("kernel", help="Runtime kernel discovery and execution")
    sk = p_k.add_subparsers(dest="cmd", required=True)

    p = sk.add_parser("ps", help="List running kernels")
    p.add_argument("--all-users", action="store_true")
    _add_server_opts(p)
    p.set_defaults(func=cmd_kernel_ps)

    p = sk.add_parser("find", help="Resolve a notebook pattern to kernel(s)")
    p.add_argument("query")
    _add_server_opts(p)
    p.set_defaults(func=cmd_kernel_find)

    p = sk.add_parser("exec", help="Execute code in a live kernel")
    _add_selector(p)
    _add_server_opts(p)
    _add_code_inputs(p)
    p.set_defaults(func=cmd_kernel_exec)

    p = sk.add_parser("inspect", help="Print a 'whos'-style summary of user globals")
    _add_selector(p)
    _add_server_opts(p)
    p.add_argument("pattern", nargs="?", default="")
    p.add_argument("-t", "--timeout", type=float, default=30.0)
    p.set_defaults(func=cmd_kernel_inspect)

    p = sk.add_parser("interrupt", help="Interrupt a running kernel")
    _add_selector(p)
    _add_server_opts(p)
    p.set_defaults(func=cmd_kernel_interrupt)

    p = sk.add_parser("restart", help="Restart a kernel (server-managed only; clears state)")
    _add_selector(p)
    _add_server_opts(p)
    p.set_defaults(func=cmd_kernel_restart)

    # --- notebook --------------------------------------------------------------
    p_n = sub.add_parser("notebook", help="Read and edit notebook files via the running jupyter server")
    sn = p_n.add_subparsers(dest="cmd", required=True)

    p = sn.add_parser("cells", help="List cells (idx, type, snippet)")
    p.add_argument("-n", "--notebook")
    _add_server_opts(p)
    p.set_defaults(func=cmd_notebook_cells)

    p = sn.add_parser("show", help="Show a single cell's source and outputs")
    p.add_argument("-n", "--notebook")
    p.add_argument("index", type=int)
    _add_server_opts(p)
    p.set_defaults(func=cmd_notebook_show)

    p = sn.add_parser("append", help="Append a cell, optionally execute it in the live kernel")
    p.add_argument("-n", "--notebook")
    p.add_argument("--markdown", action="store_true")
    p.add_argument("--execute", action="store_true")
    _add_server_opts(p)
    _add_code_inputs(p)
    p.set_defaults(func=cmd_notebook_append)

    p = sn.add_parser("replace", help="Replace cell at IDX")
    p.add_argument("-n", "--notebook")
    p.add_argument("index", type=int)
    p.add_argument("--execute", action="store_true")
    _add_server_opts(p)
    _add_code_inputs(p)
    p.set_defaults(func=cmd_notebook_replace)

    p = sn.add_parser("attach", help="Ensure a kernel is running for the given notebook (via jupyter server)")
    p.add_argument("notebook", nargs="?")
    p.add_argument("--kernel-name", default=None, help="kernelspec name (default: python3)")
    _add_server_opts(p)
    p.set_defaults(func=cmd_notebook_attach)

    # --- status / stop ---------------------------------------------------------
    p = sub.add_parser("status", help="Show running jupyter servers and kernels")
    _add_server_opts(p)
    p.set_defaults(func=cmd_status, group="status", cmd="status")

    p = sub.add_parser("stop", help="Stop background labsh server(s) owning this project")
    p.add_argument("--all", action="store_true", help="Stop every project-local labsh server, not just this project's")
    p.set_defaults(func=cmd_stop, group="stop", cmd="stop")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
