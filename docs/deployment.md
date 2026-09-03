# Run the server on a dedicated VPS

`bombercat testserver run` is meant for a **local, ephemeral** server in Docker
(see [`testserver/README.md`](../testserver/README.md)). To keep the relay up
**permanently** you run `nfcgate-server` on a machine of its own — a VPS, or a
box on your LAN — and point both BomberCats at it. The server is just a TCP
relay: clients join a 1-byte **session** and it forwards every length-prefixed
frame to the other client in that session. No crypto, no app logic.

```
[ card ] --RF--> [ BomberCat READER ] --WiFi/TCP--\
                                                   >-- nfcgate-server (:5566)
[ terminal ] <--RF-- [ BomberCat CARD ] --WiFi/TCP-/
```

The code is the **`ElectronicCats/nfcgate-server` fork** (branch `v2`) pinned to
commit `fc9103d` — upstream `nfcgate/server@4d32cc1` plus our latency patch. A
fuller Spanish walkthrough lives in `docs/SERVIDOR_DEDICADO_NFCGATE.md`;
this is the condensed English version.

**The whole guide in one screen:**

```bash
# on the VPS
sudo ufw allow 5566/tcp
curl -fsSL https://get.docker.com | sudo sh
sudo git clone https://github.com/ElectronicCats/nfcgate-server.git /opt/nfcgate-server
cd /opt/nfcgate-server && sudo git checkout fc9103d
# from your machine:  scp tools/testserver/Dockerfile USER@VPS_IP:/opt/nfcgate-server/
sudo docker build -f Dockerfile -t nfcgate-server .
sudo docker run -d --restart unless-stopped --name nfcgate-server \
  -p 5566:5566 --entrypoint python nfcgate-server server.py

# from your machine
bombercat testserver verify VPS_IP 5566     # -> RESULT: PATCH ACTIVE
bombercat testserver smoke  VPS_IP 5566     # -> RELAY SMOKE TEST PASSED
```

- [A. The latency patch](#a-the-latency-patch--already-in-the-pinned-commit)
- [B. Deploy with Docker (recommended, from scratch)](#b-deploy-with-docker-recommended-from-scratch)
- [C. Deploy with systemd (no Docker)](#c-deploy-with-systemd-no-docker)
- [D. Check the port is reachable](#d-check-the-port-is-reachable)
- [E. Verify the patch is actually live](#e-verify-the-patch-is-actually-live)
- [F. Point the boards at it, then run](#f-point-the-boards-at-it-then-run)
- [G. Day 2: update, restart, inspect](#g-day-2-update-restart-inspect)
- [H. Troubleshooting](#h-troubleshooting)

---

<a id="a-the-latency-patch--already-in-the-pinned-commit"></a>
## A. The latency patch — already in the pinned commit

Upstream `4d32cc1` as-is costs **~13.5 s per transaction**; with the patch it
drops to **~4.2 s**. It is the single biggest latency cut in the project and it
is **pure server code** — no reflashing the boards. **You no longer apply it by
hand:** the fork's pinned `fc9103d` already contains it, so it arrives with the
clone in step B.3. The same changes stay versioned in this repo:

```
tools/testserver/latency-fixes.patch
```

It touches only `server.py`, in two places:

| Phase | What it does | Gain |
|---|---|---|
| **E** | `TCP_NODELAY` + coalesced write (header+payload in **one** TCP segment) so Nagle/delayed-ACK doesn't stall every server→board relay | ~13.5 s → ~5 s |
| **H** | per-frame logging moved off the hot path (adds `-v/--verbose`, quiet by default) | ~50–150 ms |

Locally `bombercat testserver run` asserts it for you; on the VPS it comes with
the clone. Either way, **verify** it is live — that is section E. You only apply
the patch by hand on a pristine `nfcgate/server@4d32cc1` checkout, or to bring an
older clone up to date.

<a id="b-deploy-with-docker-recommended-from-scratch"></a>
## B. Deploy with Docker (recommended, from scratch)

1. **Provision the VPS.** Ubuntu 22.04/24.04 LTS, 1 vCPU / 1 GB RAM is plenty.
   Note its **public IP**, admin **user**, and open **TCP 5566** both in the OS
   firewall and in the provider's security group:

   ```bash
   sudo ufw allow 5566/tcp        # or: firewall-cmd --add-port=5566/tcp --permanent && firewall-cmd --reload
   ```

2. **Install Docker** on the VPS (over SSH):

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo docker run --rm hello-world      # sanity check
   ```

3. **Clone and pin the server** on the VPS:

   ```bash
   sudo git clone https://github.com/ElectronicCats/nfcgate-server.git /opt/nfcgate-server
   cd /opt/nfcgate-server && sudo git checkout fc9103d
   ```

   > `fc9103d` (branch `v2`) is upstream `nfcgate/server@4d32cc1` **plus** the
   > latency patch, so there is nothing to apply afterwards.

4. **Copy this repo's Dockerfile to the VPS.** The `Dockerfile` lives here, not
   in the fork. From **your machine** (a second local terminal):

   ```bash
   scp tools/testserver/Dockerfile USER@VPS_IP:/opt/nfcgate-server/Dockerfile
   ```

   > Bringing an **older** clone up to date instead of re-cloning? Either
   > `sudo git fetch origin && sudo git checkout fc9103d`, or copy the patch over
   > (`scp tools/testserver/latency-fixes.patch USER@VPS_IP:/tmp/`) and run
   > `sudo git apply /tmp/latency-fixes.patch` from `/opt/nfcgate-server`. If
   > `git apply` fails it's almost always because the checkout isn't at `4d32cc1`
   > (`git -C /opt/nfcgate-server rev-parse --short HEAD`) or the patch is already
   > applied — `git apply --check` tells you without touching anything.

5. **Build and run** (the Dockerfile already pins `protobuf==3.20.3`):

   ```bash
   cd /opt/nfcgate-server
   sudo docker build -f Dockerfile -t nfcgate-server .
   sudo docker run -d --restart unless-stopped --name nfcgate-server \
     -p 5566:5566 --entrypoint python nfcgate-server server.py
   ```

   `--restart unless-stopped` survives reboots and SSH logout.

   > **Why `--entrypoint python … server.py`?** The image ends in
   > `ENTRYPOINT ["python", "server.py"]` **plus `CMD ["log"]`**, so a bare
   > `docker run nfcgate-server` does *not* start "with no arguments" — it starts
   > `server.py log`, loading the `log` plugin. That plugin protobuf-decodes and
   > hex-prints **every relayed frame** inside `PluginHandler.filter()`, on the
   > same lock-step hot path Phase H just cleaned up. It is deliberate for the
   > local fixture (it is how `testserver run` shows you APDUs) and wrong for a
   > production relay. Overriding the entrypoint is what actually gets you
   > **no plugins = fast mode**.

   For debugging, relaunch *with* the plugin and verbose logging, then put it
   back when you're done — both cost latency:

   ```bash
   sudo docker rm -f nfcgate-server
   sudo docker run -d --restart unless-stopped --name nfcgate-server \
     -p 5566:5566 nfcgate-server log -v
   sudo docker logs -f nfcgate-server          # decoded APDUs, live
   ```

   > **Re-patching or updating later?** The Dockerfile does `COPY server.py` at
   > *build* time, so `docker restart` does **not** pick up code changes. You must
   > `docker build` again and recreate the container — see section G.

<a id="c-deploy-with-systemd-no-docker"></a>
## C. Deploy with systemd (no Docker)

Same code, no container. Isolate the pinned protobuf in a venv:

```bash
cd /opt/nfcgate-server
sudo python3 -m venv .venv
sudo .venv/bin/pip install "protobuf==3.20.3"
```

Create `/etc/systemd/system/nfcgate-server.service`:

```ini
[Unit]
Description=NFCGate relay server (BomberCat)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/nfcgate-server
# No plugins and no -v = fast mode. To debug: "server.py log -v", then
# systemctl daemon-reload && systemctl restart nfcgate-server
ExecStart=/opt/nfcgate-server/.venv/bin/python server.py
Restart=always
RestartSec=2
# Optional hardening:
DynamicUser=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nfcgate-server
sudo journalctl -u nfcgate-server -f    # connections/sessions (APDUs only with 'log -v')
```

Unlike Docker, this path runs `server.py` **straight from the working tree**, so
a `git checkout` followed by `systemctl restart` is enough — nothing is baked in.

> Here `ExecStart` really does take no arguments, so this genuinely is the
> no-plugin fast mode. `server.py` exits cleanly on SIGTERM (what systemd and
> `docker stop` send); on SIGINT (Ctrl-C) upstream throws a traceback, which is
> why running it under systemd or Docker beats running it by hand.

<a id="d-check-the-port-is-reachable"></a>
## D. Check the port is reachable

Both boards must reach **TCP 5566** on the server. Three places can block it —
the OS firewall, the provider's security group, and the network path itself:

```bash
# on the VPS: is the server actually listening?
ss -ltn | grep 5566

# from your machine: does the port answer from outside?
nc -vz VPS_IP 5566
```

- On a VPS, opening the OS firewall is **not enough** — open it in the provider's
  security group too.
- The boards are on WiFi. If the server is on another network you need a public
  IP (or a port-forward) reachable from that WiFi, not just from your laptop.
- **No TLS, no auth.** Anyone who reaches the port and guesses the session byte
  is in the relay. Outside a closed lab, put it behind a VPN (WireGuard /
  Tailscale) or restrict the source IPs by firewall.

<a id="e-verify-the-patch-is-actually-live"></a>
## E. Verify the patch is actually live

Three independent checks — do at least the first and the third:

```bash
# 1. On disk (VPS): all three must print something
grep -n TCP_NODELAY server.py                  # Phase E
grep -n "int.to_bytes(len(msg), 4" server.py   # Phase E, coalesced write (one line)
python3 server.py --help | grep verbose        # Phase H

# 2. Inside the *running* server, not the file on disk:
sudo docker exec nfcgate-server grep -c TCP_NODELAY /srv/server.py   # Docker: must print 2
systemctl show nfcgate-server -p ExecStart                           # systemd: which file runs?

# 3. On the wire, from your machine, no hardware needed (conclusive):
bombercat testserver verify VPS_IP 5566        # -> RESULT: PATCH ACTIVE
```

Check 2 is the one people skip and the one that bites: with Docker a stale image
keeps serving the old `server.py` even after you patch the file, because the
`COPY` happened at build time. Check 3 measures what the server *does* (frames
arriving in one segment vs two) and also reports the relay round-trip.

<a id="f-point-the-boards-at-it-then-run"></a>
## F. Point the boards at it, then run

```bash
# per board, over USB:
bombercat relay config wifi    --ssid "MyNet" --pass "s3cret"
bombercat relay config nfcgate --server VPS_IP:5566 --session 42 --role reader   # 'card' on the other
bombercat relay config show
```

Both boards share the **same `--server` and `--session`** (1–255); only `--role`
differs (`reader` + `card`). Then `bombercat relay run` / `relay status` / `relay monitor` on each,
exactly as in the [end-to-end guide](usage.md). Smoke-test the server with
`bombercat testserver smoke VPS_IP 5566` (relays correctly?) alongside the
`verify` above (fast?).

> **Distance is latency you can't optimize away.** EMV is strict lock-step —
> ~72 one-way board↔server hops per transaction. A VPS ~20 ms away adds ~1.4 s on
> top of the ~4.2 s floor; ~80 ms away adds ~5.8 s. Put the VPS **near** the
> boards, and measure the real RTT from the boards' WiFi with `ping VPS_IP`.

<a id="g-day-2-update-restart-inspect"></a>
## G. Day 2: update, restart, inspect

To move the server to a newer fork commit:

```bash
cd /opt/nfcgate-server
sudo git fetch origin
sudo git checkout <new-commit>          # or: sudo git checkout v2 && sudo git pull
```

Then make the running process actually pick it up:

```bash
# Docker — rebuild, the restart alone is NOT enough
sudo docker build -f Dockerfile -t nfcgate-server .
sudo docker rm -f nfcgate-server
sudo docker run -d --restart unless-stopped --name nfcgate-server \
  -p 5566:5566 --entrypoint python nfcgate-server server.py

# systemd — runs from the working tree, so a restart is enough
sudo systemctl restart nfcgate-server
```

Re-run section E check 3 (`bombercat testserver verify`) afterwards: it is the
one check that cannot be fooled by a stale image. Everyday operations:

```bash
sudo docker logs -f nfcgate-server   /   sudo journalctl -u nfcgate-server -f
sudo docker stop|start nfcgate-server /  sudo systemctl stop|start nfcgate-server
```

Whenever you change the pinned commit here, update
[`firmware/core/proto/UPSTREAM.md`](../../firmware/core/proto/UPSTREAM.md) and
`SERVER_COMMIT` in [`tools/testserver/fetch_server.sh`](../testserver/fetch_server.sh)
so local and VPS stay on the same code.

<a id="h-troubleshooting"></a>
## H. Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| `bombercat relay run` never reaches `relaying` | Port not reachable (`nc -vz VPS_IP 5566`, section D); board's WiFi can't route to the server; `--session` differs between boards. Confirm what was persisted with `bombercat relay config show`. |
| Server is up but nothing relays | The two boards must share one session and take **opposite** roles (`reader` + `card`). |
| protobuf traceback on the server | protobuf 4+ is installed; pin `3.20.3` (*"Descriptors cannot be created directly"*). |
| **Works, but ~13 s per transaction** | The classic missing-Phase-E signature. Run `bombercat testserver verify VPS_IP 5566`; if it says `PATCH MISSING`, the running code is unpatched — with Docker, **rebuild** (section G), a restart won't do it. |
| `grep TCP_NODELAY` finds it but it's still slow | You're looking at the file, not the process. Ask inside the container — section E, check 2. |
| Noticeably slower than ~4.5 s **with** the patch live | Distance (`ping VPS_IP` from the boards' network, section F), or you left the server running with `log` / `-v` from a debugging session (section B.5). |

See also: [`testserver/README.md`](../testserver/README.md) for the local,
ephemeral fixture this guide's permanent deployment is based on, and
[`docs/commands/testserver.md`](commands/testserver.md) for the CLI commands
(`run`/`verify`/`smoke`) used throughout.
