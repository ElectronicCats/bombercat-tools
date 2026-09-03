# `bombercat testserver`

> Local nfcgate-server for relay testing (no hardware/RF).

## Quick Start

```sh
bombercat testserver run          # start local nfcgate-server on host :5566
bombercat testserver smoke        # run relay smoke test against running server
bombercat testserver verify       # verify server has latency patch
```

For a bench setup with no RF, use `bombercat testserver smoke` to exercise the relay path against the local server.

---

## Subcommands

### `testserver run`

> Build (if needed) and run the local nfcgate-server in Docker. Ctrl-C to stop.

| Option | Description |
|---|---|
| `-p, --port INTEGER` | Host port to publish (default `5566`; the container always listens on 5566). |

```sh
bombercat testserver run          # publish on host :5566
bombercat testserver run -p 6000  # publish on host :6000
```

```
ℹ Starting nfcgate-server on host port 5566 (Ctrl-C to stop) …
```

**Requirements:**

| Requirement | Why | Check |
|---|---|---|
| `bash` on `PATH` | CLI launches `bash run.sh` | `bash --version` |
| Docker installed, daemon running, usable by your user | `run.sh` does `docker build` + `docker run` | `docker run --rm hello-world` |
| Your user in the `docker` group (Linux) | otherwise socket denies the build | `id -nG \| grep docker` |
| The server clone at `<repo>/server` | Docker build context | `ls server/server.py` |
| Network access on **first** run | pulls `python:3.11-slim`, installs `protobuf==3.20.3` | — |
| Host port free (default `5566`) | published as `-p <port>:5566` | `ss -ltn \| grep 5566` |

All requirements are pre-checked before the build starts. On failure, the CLI prints the fix to apply instead of a raw Docker error — see [Troubleshooting](../troubleshooting.md#testserver-errors).

The server clone is a dev-only fixture (not committed, not a submodule). Fetch it once (needs `git`):

```sh
tools/testserver/fetch_server.sh                            # clones ElectronicCats/nfcgate-server@fc9103d
SERVER_REPO=/path/to/clone tools/testserver/fetch_server.sh # offline / mirror
```

**Good to know:**

- The container **always** listens on 5566; `-p/--port` only changes the *host* port.
- The image (`bombercat-nfcgate-server`) is rebuilt on every invocation, but Docker's layer cache makes that a no-op after the first build.
- Ctrl-C stops and removes the container (`bombercat-nfcgate-server-run`); a leftover container from a crashed run is force-removed at the next start.
- Nothing here needs `protobuf` on the host: that is only for `testserver smoke`, which bootstraps its own venv.
- If relay peers live on other machines (phone running NFCGate app, BomberCat on WLAN), the host firewall must allow inbound TCP on that port, and they must target the host's LAN address — not `127.0.0.1`.

---

### `testserver verify`

> Check that a RUNNING server carries the relay latency patch.

| Argument | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Server host. |
| `PORT` | `5566` | Server port. |
| `-n, --rounds` | `8` | Relayed frames to measure. |

```sh
bombercat testserver verify                 # 127.0.0.1:5566
bombercat testserver verify 192.168.1.5 5566 -n 16
```

Grepping `server.py` only proves the file on disk is patched. This asks the server on the wire, so it also catches a Docker container still running an image built before the patch.

---

### `testserver smoke`

> Run the relay smoke test against a running server (needs `protobuf==3.20.3`, bootstrapped into a throwaway venv if the CLI's interpreter lacks it).

| Argument | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Server host. |
| `PORT` | `5566` | Server port. |

```sh
bombercat testserver smoke                 # 127.0.0.1:5566
bombercat testserver smoke 192.168.1.5 5566
```

The server must have been fetched once with `tools/testserver/fetch_server.sh` (the smoke test imports its committed `*_pb2.py`).

---

### Notes

- Wraps `tools/testserver/run.sh`, `tools/testserver/relay_smoketest.py`, `tools/testserver/verify_patch.py`.
- See [`testserver/README.md`](../../testserver/README.md) for the fixture itself.
- Need the relay up **permanently**, not just for local testing? See [Deployment](../deployment.md) for running `nfcgate-server` on a VPS.
