"""
Unit tests for the Incus client seam (``incus.utils.incus``).

These assert the exact ``incus`` argument vectors the seam builds, which is the
contract every module and state relies on, and that reads are parsed correctly.
The seam runs the CLI through :py:mod:`subprocess`, which is mocked here, so no
real ``incus`` is required.
"""

import json
import subprocess
from unittest.mock import MagicMock

import pytest
from salt.exceptions import CommandExecutionError

import saltext.incus.utils.incus as incus_utils


def _completed(retcode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=retcode, stdout=stdout, stderr=stderr)


@pytest.fixture
def run(monkeypatch):
    mock = MagicMock(return_value=_completed())
    monkeypatch.setattr(incus_utils.subprocess, "run", mock)
    return mock


def _last_cmd(run):
    return run.call_args_list[-1].args[0]


def _all_cmds(run):
    return [c.args[0] for c in run.call_args_list]


def test_stringify_lowercases_bool():
    assert incus_utils._stringify(True) == "true"
    assert incus_utils._stringify(False) == "false"
    assert incus_utils._stringify(2) == "2"
    assert incus_utils._stringify("x") == "x"


def test_run_prepends_project_and_runs_without_shell(run):
    incus_utils._run(["start", "web01"], project="proj")
    cmd = _last_cmd(run)
    assert cmd == ["incus", "--project", "proj", "start", "web01"]
    # argv list and no shell: values can never be word-split by a shell
    assert isinstance(cmd, list)
    assert run.call_args_list[-1].kwargs.get("shell") in (None, False)


def test_run_raises_on_nonzero(run):
    run.return_value = _completed(retcode=1, stderr="boom")
    with pytest.raises(CommandExecutionError):
        incus_utils._run(["start", "web01"])


def test_run_ignore_retcode_does_not_raise(run):
    run.return_value = _completed(retcode=1, stderr="boom")
    rc, _, err = incus_utils._run(["start", "web01"], ignore_retcode=True)
    assert rc == 1 and err == "boom"


def test_query_instance_returns_dict(run):
    run.return_value = _completed(stdout=json.dumps({"name": "web01", "status": "Running"}))
    out = incus_utils.query_instance("web01")
    assert out["status"] == "Running"
    assert _last_cmd(run) == ["incus", "query", "/1.0/instances/web01?recursion=1"]


def test_query_instance_missing_returns_none(run):
    run.return_value = _completed(retcode=1, stderr="not found")
    assert incus_utils.query_instance("nope") is None


def test_create_instance_builds_create_then_devices(run):
    incus_utils.create_instance(
        "web01",
        "images:debian/12",
        profiles=["default", "web"],
        config={"limits.cpu": 2, "boot.autostart": True},
        devices={"data": {"type": "disk", "source": "/srv", "path": "/data"}},
        ephemeral=True,
        project="proj",
    )
    cmds = _all_cmds(run)
    create = cmds[0]
    assert create[:3] == ["incus", "--project", "proj"]
    assert create[3:6] == ["create", "images:debian/12", "web01"]
    assert "--ephemeral" in create
    assert create.count("--profile") == 2
    assert "default" in create and "web" in create
    # config rendered as -c key=value with bool lowercased
    assert "-c" in create
    assert "limits.cpu=2" in create
    assert "boot.autostart=true" in create
    # device added in a second command
    add = cmds[1]
    assert add[3:7] == ["config", "device", "add", "web01"]
    assert add[7:9] == ["data", "disk"]
    assert "source=/srv" in add and "path=/data" in add


def test_create_instance_device_missing_type_raises(run):
    with pytest.raises(CommandExecutionError):
        incus_utils.create_instance("web01", "img", devices={"bad": {"source": "/srv"}})


def test_delete_force(run):
    incus_utils.delete_instance("web01", force=True)
    assert _last_cmd(run) == ["incus", "delete", "web01", "--force"]


def test_stop_timeout_vs_force(run):
    incus_utils.stop_instance("web01", timeout=15)
    assert _last_cmd(run) == ["incus", "stop", "web01", "--timeout", "15"]
    incus_utils.stop_instance("web01", force=True)
    assert _last_cmd(run) == ["incus", "stop", "web01", "--force"]


def test_set_unset_config(run):
    incus_utils.set_config("web01", "boot.autostart", True)
    assert _last_cmd(run) == ["incus", "config", "set", "web01", "boot.autostart", "true"]
    incus_utils.unset_config("web01", "boot.autostart")
    assert _last_cmd(run) == ["incus", "config", "unset", "web01", "boot.autostart"]


def test_add_remove_device(run):
    incus_utils.add_device("web01", "eth1", "nic", options={"network": "incusbr0"})
    assert _last_cmd(run) == [
        "incus",
        "config",
        "device",
        "add",
        "web01",
        "eth1",
        "nic",
        "network=incusbr0",
    ]
    incus_utils.remove_device("web01", "eth1")
    assert _last_cmd(run) == ["incus", "config", "device", "remove", "web01", "eth1"]


def test_exec_in_builds_argv_and_does_not_raise(run):
    run.return_value = _completed(retcode=7, stdout="out", stderr="err")
    out = incus_utils.exec_in(
        "web01",
        ["sh", "-c", "echo hi"],
        project="proj",
        environment={"FOO": "bar"},
        cwd="/root",
    )
    # nonzero return is surfaced, not raised
    assert out["retcode"] == 7
    cmd = _last_cmd(run)
    assert cmd[:5] == ["incus", "--project", "proj", "exec", "web01"]
    assert "--env" in cmd and "FOO=bar" in cmd
    assert "--cwd" in cmd and "/root" in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1 :] == ["sh", "-c", "echo hi"]


def test_push_file_locks_down_and_joins_target(run):
    incus_utils.push_file("web01", "/local/x", "/run/stage/x", recursive=True)
    cmd = _last_cmd(run)
    assert cmd[:4] == ["incus", "file", "push", "/local/x"]
    assert "web01/run/stage/x" in cmd
    assert cmd[cmd.index("--mode") + 1] == "0600"
    assert cmd[cmd.index("--uid") + 1] == "0"
    assert cmd[cmd.index("--gid") + 1] == "0"
    assert "--create-dirs" in cmd
    assert "--recursive" in cmd


def test_delete_path_uses_rm_rf(run):
    incus_utils.delete_path("web01", "/run/stage")
    cmd = _last_cmd(run)
    sep = cmd.index("--")
    assert cmd[sep + 1 :] == ["rm", "-rf", "/run/stage"]


def test_publish_parses_fingerprint(run):
    run.return_value = _completed(stdout="Instance published with fingerprint: abcdef0123456789")
    out = incus_utils.publish("web01", alias="myimg")
    assert out == {"fingerprint": "abcdef0123456789", "alias": "myimg"}
    cmd = _last_cmd(run)
    assert cmd[:3] == ["incus", "publish", "web01"]
    assert cmd[cmd.index("--alias") + 1] == "myimg"
