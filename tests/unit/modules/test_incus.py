"""
Unit tests for the Incus execution module.

The client seam (``incus.utils.incus``) and Salt internals (``gen_thin``,
the salt-ssh trans-tar helpers) are mocked, so no real ``incus`` or instance is
required. These focus on the in-instance apply behaviour: the ``salt-call``
invocation, that pillar never reaches the command line, that test mode
propagates, and that ``sls_build`` always cleans up.
"""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest
import salt.config
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

from saltext.incus.modules import incus
from saltext.incus.utils import incus as incus_seam

PASS_HS = {"file_|-x_|-/x_|-managed": {"result": True, "changes": {}, "comment": "ok"}}
FAIL_HS = {"file_|-x_|-/x_|-managed": {"result": False, "changes": {}, "comment": "no"}}


def _ok(stdout=""):
    return {"retcode": 0, "stdout": stdout, "stderr": ""}


def _salt_call_json(return_value, retcode=0):
    return json.dumps({"local": {"return": return_value, "retcode": retcode}})


@pytest.fixture
def utils():
    return {
        "incus.exec_in": MagicMock(return_value=_ok()),
        "incus.push_file": MagicMock(return_value=None),
        "incus.delete_path": MagicMock(return_value=_ok()),
        "incus.create_instance": MagicMock(return_value=None),
        "incus.start_instance": MagicMock(return_value=None),
        "incus.stop_instance": MagicMock(return_value=None),
        "incus.delete_instance": MagicMock(return_value=None),
        "incus.publish": MagicMock(return_value={"fingerprint": "abc", "alias": "img"}),
        "incus.query_instance": MagicMock(return_value={"name": "web01", "status": "Running"}),
        "incus.list_instances": MagicMock(return_value=[{"name": "web01"}]),
        "incus.set_config": MagicMock(return_value=None),
        "incus.unset_config": MagicMock(return_value=None),
        "incus.add_device": MagicMock(return_value=None),
        "incus.remove_device": MagicMock(return_value=None),
    }


@pytest.fixture
def opts(tmp_path):
    o = salt.config.DEFAULT_MINION_OPTS.copy()
    o["cachedir"] = str(tmp_path)
    o["id"] = "control"
    o["grains"] = {}
    o["pillar"] = {}
    return o


@pytest.fixture(autouse=True)
def _inject(monkeypatch, utils, opts):
    # The seam runs the incus CLI via subprocess and is reached through
    # _seam(): a direct import when installed as a package, or __utils__ when
    # synced as custom modules. Patch the seam module that _seam() imports.
    for key, mock in utils.items():
        monkeypatch.setattr(incus_seam, key.split(".", 1)[1], mock, raising=False)
    monkeypatch.setattr(incus, "__opts__", opts, raising=False)
    monkeypatch.setattr(incus, "__context__", {}, raising=False)
    monkeypatch.setattr(
        incus, "__salt__", {"config.get": lambda key, default=None: default}, raising=False
    )


# --- small helpers ---------------------------------------------------------


def test_normalize_mods():
    assert incus._normalize_mods("a, b ,c") == ["a", "b", "c"]
    assert incus._normalize_mods(["a", " b "]) == ["a", "b"]
    assert incus._normalize_mods(None) == []


def test_validate_transport():
    incus._validate_transport("thin")
    incus._validate_transport("baked")
    with pytest.raises(SaltInvocationError):
        incus._validate_transport("nope")


def test_parse_salt_call():
    ret, rc = incus._parse_salt_call(_salt_call_json({"a": 1}, 0))
    assert ret == {"a": 1} and rc == 0
    assert incus._parse_salt_call("not json") == (None, None)


# --- lifecycle wrappers ----------------------------------------------------


def test_create_starts_when_requested(utils):
    incus.create("web01", "images:debian/12", start=True, project="p")
    utils["incus.create_instance"].assert_called_once()
    utils["incus.start_instance"].assert_called_once_with("web01", project="p")


def test_lifecycle_passthroughs(utils):
    incus.config_set("web01", "k", "v")
    utils["incus.set_config"].assert_called_once_with("web01", "k", "v", project=None)
    incus.device_add("web01", "d", "disk", source="/srv", __pub_x="ignored")
    _, kwargs = utils["incus.add_device"].call_args
    assert kwargs["options"] == {"source": "/srv"}
    assert incus.list_() == ["web01"]
    assert incus.exists("web01") is True


# --- call ------------------------------------------------------------------


def test_call_ships_thin_runs_salt_call_and_cleans(monkeypatch, utils):
    monkeypatch.setattr("salt.utils.thin.gen_thin", MagicMock(return_value="/tmp/thin.tgz"))

    def exec_side(name, argv, project=None, **kw):
        argv = list(argv)
        if argv and argv[0] == "sh":
            return _ok(_salt_call_json(True, 0))
        return _ok("Python 3.12")

    utils["incus.exec_in"].side_effect = exec_side

    result = incus.call("web01", "test.ping")
    assert result is True
    assert incus.__context__["retcode"] == 0

    # thin was generated and pushed
    assert utils["incus.push_file"].called
    # the salt-call script ran the requested function locally
    sh_calls = [c for c in utils["incus.exec_in"].call_args_list if list(c.args[1])[:1] == ["sh"]]
    script = sh_calls[-1].args[1][2]
    assert "salt-call" in script and "test.ping" in script
    assert "--local" in script
    # cleanup happened
    assert utils["incus.delete_path"].called


def test_call_baked_skips_thin(monkeypatch, utils):
    gen = MagicMock(return_value="/tmp/thin.tgz")
    monkeypatch.setattr("salt.utils.thin.gen_thin", gen)

    def exec_side(name, argv, project=None, **kw):
        argv = list(argv)
        if argv and argv[0] == "sh":
            return _ok(_salt_call_json(True, 0))
        return _ok("Python 3.12")

    utils["incus.exec_in"].side_effect = exec_side

    incus.call("web01", "test.ping", transport="baked")
    gen.assert_not_called()
    assert not utils["incus.push_file"].called
    sh_calls = [c for c in utils["incus.exec_in"].call_args_list if list(c.args[1])[:1] == ["sh"]]
    script = sh_calls[-1].args[1][2]
    # baked uses the instance's own salt-call, not a python+thin invocation
    assert script.strip().startswith("salt-call") or "; salt-call" in script


# --- sls: source strategy --------------------------------------------------


def test_stage_pillar_writes_json_renderer(tmp_path):
    incus._stage_pillar(str(tmp_path), {"access": {"token": "SECRET"}})
    top = (tmp_path / "top.sls").read_text()
    data = (tmp_path / "incus_pillar.sls").read_text()
    assert top.startswith("#!json")
    assert data.startswith("#!json")
    assert "SECRET" in data
    assert json.loads(data.split("\n", 1)[1]) == {"access": {"token": "SECRET"}}


def test_stage_sls_source_selects_component(tmp_path, monkeypatch):
    f_users = tmp_path / "users.sls"
    f_users.write_text("u")
    f_init = tmp_path / "init.sls"
    f_init.write_text("i")
    cache = {
        "access/users.sls": str(f_users),
        "access/init.sls": str(f_init),
    }
    monkeypatch.setitem(
        incus.__salt__,
        "cp.list_master",
        lambda saltenv: ["access/users.sls", "access/init.sls", "other/x.sls"],
    )
    monkeypatch.setitem(
        incus.__salt__, "cp.cache_file", lambda url, saltenv: cache[url[len("salt://") :]]
    )
    states_dir = tmp_path / "states"
    states_dir.mkdir()
    incus._stage_sls_source(str(states_dir), ["access.users"], "base")
    # the whole access component shipped, the unrelated component did not
    assert (states_dir / "access" / "users.sls").exists()
    assert (states_dir / "access" / "init.sls").exists()
    assert not (states_dir / "other").exists()


def test_stage_sls_source_raises_when_missing(monkeypatch):
    monkeypatch.setitem(incus.__salt__, "cp.list_master", lambda saltenv: ["other/x.sls"])
    monkeypatch.setitem(incus.__salt__, "cp.cache_file", lambda url, saltenv: "")
    with pytest.raises(CommandExecutionError):
        incus._stage_sls_source("/tmp/nope", ["access.users"], "base")


def test_sls_source_keeps_pillar_off_cmdline_and_propagates_test(monkeypatch, utils):
    # real thin file so the source path can copy it
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as fh:
        fh.write(b"thin")
        thin_name = fh.name
    monkeypatch.setattr("salt.utils.thin.gen_thin", MagicMock(return_value=thin_name))

    with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as fh:
        fh.write(b"user state")
        users_name = fh.name
    monkeypatch.setitem(incus.__salt__, "cp.list_master", lambda saltenv: ["access/users.sls"])
    monkeypatch.setitem(incus.__salt__, "cp.cache_file", lambda url, saltenv: users_name)

    def exec_side(name, argv, project=None, **kw):
        argv = list(argv)
        s = " ".join(str(a) for a in argv)
        if argv and argv[0] == "sh":
            return _ok(_salt_call_json(PASS_HS, 0))
        return _ok("Python 3.12") if "--version" in s else _ok()

    utils["incus.exec_in"].side_effect = exec_side

    secret = "SUPERSECRETTOKEN"
    result = incus.sls("web01", "access.users", pillar={"access": {"token": secret}}, test=True)
    assert result == PASS_HS

    # the salt-call invocation applied the mod in test mode
    sh_calls = [c for c in utils["incus.exec_in"].call_args_list if list(c.args[1])[:1] == ["sh"]]
    script = sh_calls[-1].args[1][2]
    assert "state.apply" in script and "access.users" in script
    assert "test=True" in script
    assert "--config-dir" in script

    # the pillar secret never appears in any exec argv or push target
    for c in utils["incus.exec_in"].call_args_list:
        assert secret not in " ".join(str(a) for a in c.args[1])
    for c in utils["incus.push_file"].call_args_list:
        assert secret not in " ".join(str(a) for a in c.args)

    os.unlink(thin_name)
    os.unlink(users_name)


def test_sls_rejects_cmdline_pillar_mode(utils):
    with pytest.raises(SaltInvocationError):
        incus.sls("web01", "access.users", pillar_mode="cmdline")


# --- sls: precompiled strategy --------------------------------------------


def test_sls_precompiled_uses_state_pkg_with_test(monkeypatch, utils):
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as fh:
        fh.write(b"trans")
        trans_name = fh.name
    monkeypatch.setattr(incus, "_prepare_trans_tar", MagicMock(return_value=trans_name))

    calls = []

    def fake_call(name, function, *args, **kwargs):
        calls.append((function, args, kwargs))
        if function == "grains.items":
            return {"os": "Debian"}
        return PASS_HS

    monkeypatch.setattr(incus, "call", fake_call)

    result = incus.sls("web01", "hardening", precompiled=True, test=True)
    assert result == PASS_HS

    funcs = [c[0] for c in calls]
    assert "grains.items" in funcs
    pkg = [c for c in calls if c[0] == "state.pkg"][0]
    assert pkg[1][0].endswith("salt_state.tgz")  # remote tar path
    assert pkg[1][2] == "sha256"
    assert pkg[2].get("test") is True
    # staging dir and local trans tar cleaned up
    assert utils["incus.delete_path"].called
    assert not os.path.exists(trans_name)


def test_sls_sets_retcode_context_on_failure(monkeypatch, utils):
    monkeypatch.setattr(incus, "_prepare_trans_tar", MagicMock(return_value=tempfile.mkstemp()[1]))
    monkeypatch.setattr(
        incus,
        "call",
        lambda name, function, *a, **k: {"os": "x"} if function == "grains.items" else FAIL_HS,
    )
    incus.sls("web01", "hardening", precompiled=True)
    assert incus.__context__["retcode"] == 2


# --- sls_build -------------------------------------------------------------


def test_sls_build_publishes_then_always_deletes(monkeypatch, utils):
    monkeypatch.setattr(incus, "sls", lambda *a, **k: PASS_HS)
    out = incus.sls_build("mycorp/web", "images:debian/12", "web")
    assert out["published"]["alias"] == "img"
    utils["incus.create_instance"].assert_called_once()
    utils["incus.start_instance"].assert_called_once()
    utils["incus.publish"].assert_called_once()
    utils["incus.delete_instance"].assert_called_once()  # cleanup


def test_sls_build_test_mode_does_not_publish(monkeypatch, utils):
    monkeypatch.setattr(incus, "sls", lambda *a, **k: PASS_HS)
    out = incus.sls_build("mycorp/web", "images:debian/12", "web", test=True)
    assert out["published"] is False
    utils["incus.publish"].assert_not_called()
    utils["incus.delete_instance"].assert_called_once()  # still cleaned up


def test_sls_build_failure_still_deletes(monkeypatch, utils):
    monkeypatch.setattr(incus, "sls", lambda *a, **k: FAIL_HS)
    with pytest.raises(CommandExecutionError):
        incus.sls_build("mycorp/web", "images:debian/12", "web")
    utils["incus.delete_instance"].assert_called_once()  # cleanup on failure
    utils["incus.publish"].assert_not_called()
