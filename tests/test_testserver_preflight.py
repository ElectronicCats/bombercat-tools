#!/usr/bin/env python3

# Electronic Cats
# test_testserver_preflight.py — the checks `bombercat testserver run` makes
# before shelling out to Docker (modules/testserver/preflight.py). Their whole
# point is turning a cryptic native failure into an explanation with a fix, so
# these tests assert on the branch taken and on the advice printed. Nothing here
# needs Docker installed: the environment is scripted per test.

import socket
import subprocess
import sys
import types

import pytest

from conftest import flat
from modules.testserver import preflight


@pytest.fixture
def docker_probe(monkeypatch):
    """Script `shutil.which("docker")` and what `docker info` answers."""

    def _set(installed=True, returncode=0, stderr=""):
        monkeypatch.setattr(
            preflight.shutil,
            "which",
            lambda name: "/usr/bin/docker" if installed else None,
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], returncode, stderr=stderr
            ),
        )

    return _set


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


# ── short_path ───────────────────────────────────────────────────────────────


def test_short_path_prefers_the_relative_spelling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert preflight.short_path(tmp_path / "testserver" / "run.sh") == (
        "testserver/run.sh"
    )


def test_short_path_keeps_the_absolute_one_when_it_is_shorter(tmp_path, monkeypatch):
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert preflight.short_path(preflight.Path("/x")) == "/x"


# ── check_server_sources ─────────────────────────────────────────────────────


@pytest.fixture
def cloned_server(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    (server / "server.py").write_text("# nfcgate-server\n")
    return server


def test_server_sources_check_passes_when_the_clone_is_there(cloned_server, tmp_path):
    assert preflight.check_server_sources(cloned_server, tmp_path / "fetch.sh") is None


def test_server_sources_check_explains_a_missing_clone(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as e:
        preflight.check_server_sources(tmp_path / "server", tmp_path / "fetch.sh")
    out = flat(capsys.readouterr().out)

    assert e.value.code == 1
    assert "nfcgate-server sources are missing" in out
    assert "fetch" in out  # the panel names the script that clones it


def test_server_sources_check_can_fetch_the_clone(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(preflight.click, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *a, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    preflight.check_server_sources(tmp_path / "server", tmp_path / "fetch.sh")

    assert calls == [["bash", str(tmp_path / "fetch.sh")]]
    assert "nfcgate-server fetched" in flat(capsys.readouterr().out)


def test_server_sources_check_reports_a_failed_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(preflight.click, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 1)
    )
    with pytest.raises(SystemExit) as e:
        preflight.check_server_sources(tmp_path / "server", tmp_path / "fetch.sh")

    assert e.value.code == 1
    assert "Fetching the nfcgate-server failed" in flat(capsys.readouterr().out)


def test_server_sources_check_declined_by_the_user(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(preflight.click, "confirm", lambda *a, **k: False)
    with pytest.raises(SystemExit) as e:
        preflight.check_server_sources(tmp_path / "server", tmp_path / "fetch.sh")

    assert e.value.code == 1


# ── check_docker ─────────────────────────────────────────────────────────────


def test_docker_check_passes_when_the_daemon_answers(docker_probe):
    docker_probe(returncode=0)
    assert preflight.check_docker() is None


def test_docker_check_reports_a_missing_docker(docker_probe, capsys):
    docker_probe(installed=False)
    with pytest.raises(SystemExit) as e:
        preflight.check_docker()
    out = flat(capsys.readouterr().out)

    assert e.value.code == 1
    assert "Docker is not installed" in out
    assert "docs.docker.com" in out


def test_docker_check_explains_a_socket_permission_denial(docker_probe, capsys):
    docker_probe(returncode=1, stderr="permission denied while trying to connect")
    with pytest.raises(SystemExit):
        preflight.check_docker()
    out = flat(capsys.readouterr().out)

    assert "permission denied on its socket" in out
    assert "docker" in out and "group" in out


def test_docker_check_explains_a_stopped_daemon(docker_probe, capsys, monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    docker_probe(returncode=1, stderr="Cannot connect to the Docker daemon at unix://")
    with pytest.raises(SystemExit):
        preflight.check_docker()
    out = flat(capsys.readouterr().out)

    assert "Docker daemon is not running" in out
    assert "systemctl start docker" in out


def test_docker_check_points_at_docker_desktop_on_macos(
    docker_probe, capsys, monkeypatch
):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    docker_probe(returncode=1, stderr="Cannot connect to the Docker daemon")
    with pytest.raises(SystemExit):
        preflight.check_docker()

    assert "Docker Desktop" in flat(capsys.readouterr().out)


def test_docker_check_passes_an_unrecognised_failure_through(docker_probe, capsys):
    docker_probe(returncode=1, stderr="context deadline exceeded")
    with pytest.raises(SystemExit):
        preflight.check_docker()
    out = flat(capsys.readouterr().out)

    assert "installed but not usable" in out
    assert "context deadline exceeded" in out


# ── docker group state ───────────────────────────────────────────────────────


def _grp_module(gid=999, members=()):
    fake = types.ModuleType("grp")
    fake.getgrnam = lambda name: types.SimpleNamespace(gr_gid=gid, gr_mem=list(members))
    return fake


def test_group_state_active_when_the_process_already_has_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "grp", _grp_module())
    monkeypatch.setattr(preflight.os, "getgroups", lambda: [999])
    assert preflight._docker_group_state() == "active"


def test_group_state_pending_login_for_a_member_in_an_old_session(monkeypatch):
    monkeypatch.setitem(sys.modules, "grp", _grp_module(members=["darcko"]))
    monkeypatch.setattr(preflight.os, "getgroups", lambda: [100])
    monkeypatch.setitem(
        sys.modules, "getpass", types.SimpleNamespace(getuser=lambda: "darcko")
    )
    assert preflight._docker_group_state() == "pending-login"


def test_group_state_absent_for_a_non_member(monkeypatch):
    monkeypatch.setitem(sys.modules, "grp", _grp_module(members=["someone-else"]))
    monkeypatch.setattr(preflight.os, "getgroups", lambda: [100])
    monkeypatch.setitem(
        sys.modules, "getpass", types.SimpleNamespace(getuser=lambda: "darcko")
    )
    assert preflight._docker_group_state() == "absent"


def test_group_state_unknown_without_a_docker_group(monkeypatch):
    fake = types.ModuleType("grp")

    def _raise(name):
        raise KeyError(name)

    fake.getgrnam = _raise
    monkeypatch.setitem(sys.modules, "grp", fake)
    assert preflight._docker_group_state() == "unknown"


# ── activating the group ─────────────────────────────────────────────────────


def test_activation_step_prefers_newgrp(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda n: "/usr/bin/newgrp")
    step, notes = preflight._activate_group_step()

    assert "newgrp docker" in step and notes == []


def test_activation_step_falls_back_to_sg(monkeypatch):
    monkeypatch.setattr(
        preflight.shutil, "which", lambda n: "/usr/bin/sg" if n == "sg" else None
    )
    step, notes = preflight._activate_group_step()

    assert "sg docker" in step and notes == []


def test_activation_step_says_only_a_login_can_help(monkeypatch):
    """Trimmed installs have Docker but neither newgrp nor sg."""
    monkeypatch.setattr(preflight.shutil, "which", lambda n: None)
    step, notes = preflight._activate_group_step()

    assert "Log out and back in" in step
    assert any("only a new login can" in n for n in notes)
    assert any("sudo -E" in n for n in notes)  # the one-off escape hatch


@pytest.mark.parametrize(
    "os_release, expected",
    [
        ("ID=ubuntu\nID_LIKE=debian\n", "sudo apt install login"),
        ("ID=fedora\n", "sudo dnf install shadow-utils"),
        ("ID=arch\n", "sudo pacman -S shadow"),
        ("ID=alpine\n", "sudo apk add shadow"),
        ('ID="opensuse-leap"\n', "sudo zypper install shadow"),
        ("ID=plan9\n", None),
    ],
)
def test_newgrp_install_hint_per_distro(monkeypatch, tmp_path, os_release, expected):
    release = tmp_path / "os-release"
    release.write_text(os_release)
    monkeypatch.setattr(preflight, "Path", lambda *a, **k: release)

    assert preflight._newgrp_install_hint() == expected


def test_newgrp_install_hint_without_an_os_release(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "Path", lambda *a, **k: tmp_path / "nope")
    assert preflight._newgrp_install_hint() is None


# ── the permission panel ─────────────────────────────────────────────────────


@pytest.fixture
def group_state(monkeypatch):
    def _set(state):
        monkeypatch.setattr(preflight, "_docker_group_state", lambda: state)
        monkeypatch.setattr(
            preflight, "_activate_group_step", lambda: ("newgrp docker", [])
        )

    return _set


def test_permission_fix_offers_to_add_a_non_member_to_the_group(group_state):
    group_state("absent")
    why, fix, notes = preflight._permission_fix()

    assert "not one yet" in why
    assert any("usermod -aG docker" in step for step in fix)
    assert any("root-equivalent" in n for n in notes)  # the security caveat


def test_permission_fix_tells_a_member_to_start_a_new_session(group_state):
    group_state("pending-login")
    why, fix, notes = preflight._permission_fix()

    assert "only applied at login" in why
    assert not any("usermod" in step for step in fix)
    assert any("id -nG" in n for n in notes)


def test_permission_fix_falls_back_to_socket_ownership(group_state):
    group_state("active")
    why, fix, notes = preflight._permission_fix()

    assert "still refuses" in why
    assert any("docker.sock" in n for n in notes)


def test_permission_fix_mentions_a_custom_docker_host(group_state, monkeypatch):
    group_state("active")
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.9:2375")
    _, _, notes = preflight._permission_fix()

    assert "tcp://10.0.0.9:2375" in notes[0]


# ── check_port ───────────────────────────────────────────────────────────────


def test_port_check_passes_on_a_free_port():
    assert preflight.check_port(_free_port(), "bombercat-nfcgate-server-run") is None


def test_port_check_names_a_test_server_we_left_running(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="bombercat-nfcgate-server-run\n"
        ),
    )
    with socket.socket() as held:
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        with pytest.raises(SystemExit) as e:
            preflight.check_port(held.getsockname()[1], "bombercat-nfcgate-server-run")
    out = flat(capsys.readouterr().out)

    assert e.value.code == 1
    assert "test server you left running" in out
    assert "docker rm -f bombercat-nfcgate-server-run" in out


def test_port_check_suggests_another_port_when_something_else_holds_it(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=""),
    )
    with socket.socket() as held:
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        port = held.getsockname()[1]
        with pytest.raises(SystemExit):
            preflight.check_port(port, "bombercat-nfcgate-server-run")
    out = flat(capsys.readouterr().out)

    assert "already in use by another program" in out
    assert f"-p {port + 1}" in out
