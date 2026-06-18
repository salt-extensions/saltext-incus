"""
Unit tests for the Incus state module.

The execution module is mocked via ``__salt__``; ``__opts__["test"]`` is
toggled to exercise test mode. These verify the additive reconcile in
``present``, drift detection in the lifecycle states, and that ``sls_applied``
maps the in-instance highstate (success, failure, test) onto the outer result.
"""

from unittest.mock import MagicMock

import pytest
from salt.exceptions import CommandExecutionError

import incus.states.incus as incus_states


@pytest.fixture
def salt():
    return {
        "incus.info": MagicMock(return_value=None),
        "incus.create": MagicMock(return_value={"name": "web01", "status": "Stopped"}),
        "incus.config_set": MagicMock(return_value=True),
        "incus.config_unset": MagicMock(return_value=True),
        "incus.device_add": MagicMock(return_value=True),
        "incus.device_remove": MagicMock(return_value=True),
        "incus.start": MagicMock(return_value=True),
        "incus.stop": MagicMock(return_value=True),
        "incus.delete": MagicMock(return_value=True),
        "incus.sls": MagicMock(return_value={}),
    }


@pytest.fixture
def opts():
    return {"test": False}


@pytest.fixture(autouse=True)
def _inject(monkeypatch, salt, opts):
    monkeypatch.setattr(incus_states, "__salt__", salt, raising=False)
    monkeypatch.setattr(incus_states, "__opts__", opts, raising=False)


# --- present: create path --------------------------------------------------


def test_present_creates_when_absent(salt):
    salt["incus.info"].return_value = None
    ret = incus_states.present("web01", image="images:debian/12")
    assert ret["result"] is True
    salt["incus.create"].assert_called_once()
    assert ret["changes"]["web01"]["old"] is None


def test_present_absent_without_image_fails(salt):
    salt["incus.info"].return_value = None
    ret = incus_states.present("web01")
    assert ret["result"] is False
    salt["incus.create"].assert_not_called()


def test_present_create_test_mode_no_mutation(salt, opts):
    opts["test"] = True
    salt["incus.info"].return_value = None
    ret = incus_states.present("web01", image="img")
    assert ret["result"] is None
    salt["incus.create"].assert_not_called()
    assert ret["changes"]


# --- present: reconcile path -----------------------------------------------


def test_present_noop_when_matching(salt):
    salt["incus.info"].return_value = {
        "name": "web01",
        "status": "Running",
        "config": {"limits.cpu": "2"},
        "devices": {},
    }
    ret = incus_states.present("web01", config={"limits.cpu": 2}, running=True)
    assert ret["result"] is True
    assert not ret["changes"]
    salt["incus.config_set"].assert_not_called()


def test_present_config_delta_applied(salt):
    salt["incus.info"].return_value = {
        "name": "web01",
        "status": "Running",
        "config": {"limits.cpu": "1"},
        "devices": {},
    }
    ret = incus_states.present("web01", config={"limits.cpu": 2})
    assert ret["result"] is True
    salt["incus.config_set"].assert_called_once_with("web01", "limits.cpu", "2", project=None)
    assert ret["changes"]["config"]["limits.cpu"] == {"old": "1", "new": "2"}


def test_present_device_delta_replaces(salt):
    salt["incus.info"].return_value = {
        "name": "web01",
        "status": "Running",
        "config": {},
        "devices": {"data": {"type": "disk", "source": "/old", "path": "/data"}},
    }
    ret = incus_states.present(
        "web01", devices={"data": {"type": "disk", "source": "/new", "path": "/data"}}
    )
    assert ret["result"] is True
    salt["incus.device_remove"].assert_called_once_with("web01", "data", project=None)
    args, kwargs = salt["incus.device_add"].call_args
    assert args[:3] == ("web01", "data", "disk")
    assert kwargs["source"] == "/new" and kwargs["path"] == "/data"
    assert "type" not in kwargs


def test_present_starts_when_stopped(salt):
    salt["incus.info"].return_value = {
        "name": "web01",
        "status": "Stopped",
        "config": {},
        "devices": {},
    }
    ret = incus_states.present("web01", running=True)
    assert ret["result"] is True
    salt["incus.start"].assert_called_once_with("web01", project=None)
    assert ret["changes"]["status"] == {"old": "Stopped", "new": "Running"}


def test_present_reconcile_test_mode_no_mutation(salt, opts):
    opts["test"] = True
    salt["incus.info"].return_value = {
        "name": "web01",
        "status": "Stopped",
        "config": {"limits.cpu": "1"},
        "devices": {},
    }
    ret = incus_states.present("web01", config={"limits.cpu": 2}, running=True)
    assert ret["result"] is None
    salt["incus.config_set"].assert_not_called()
    salt["incus.start"].assert_not_called()
    assert "config" in ret["changes"] and "status" in ret["changes"]


# --- running / stopped / absent --------------------------------------------


def test_running_fails_when_absent(salt):
    salt["incus.info"].return_value = None
    ret = incus_states.running("web01")
    assert ret["result"] is False
    salt["incus.start"].assert_not_called()


def test_running_starts(salt):
    salt["incus.info"].return_value = {"status": "Stopped"}
    ret = incus_states.running("web01")
    assert ret["result"] is True
    salt["incus.start"].assert_called_once()


def test_running_noop(salt):
    salt["incus.info"].return_value = {"status": "Running"}
    ret = incus_states.running("web01")
    assert ret["result"] is True
    assert not ret["changes"]
    salt["incus.start"].assert_not_called()


def test_running_test_mode(salt, opts):
    opts["test"] = True
    salt["incus.info"].return_value = {"status": "Stopped"}
    ret = incus_states.running("web01")
    assert ret["result"] is None
    salt["incus.start"].assert_not_called()


def test_stopped_stops(salt):
    salt["incus.info"].return_value = {"status": "Running"}
    ret = incus_states.stopped("web01")
    assert ret["result"] is True
    salt["incus.stop"].assert_called_once()


def test_stopped_noop(salt):
    salt["incus.info"].return_value = {"status": "Stopped"}
    ret = incus_states.stopped("web01")
    assert ret["result"] is True
    assert not ret["changes"]
    salt["incus.stop"].assert_not_called()


def test_absent_deletes(salt):
    salt["incus.info"].return_value = {"status": "Running"}
    ret = incus_states.absent("web01")
    assert ret["result"] is True
    salt["incus.delete"].assert_called_once()


def test_absent_already_absent(salt):
    salt["incus.info"].return_value = None
    ret = incus_states.absent("web01")
    assert ret["result"] is True
    assert not ret["changes"]
    salt["incus.delete"].assert_not_called()


def test_absent_test_mode(salt, opts):
    opts["test"] = True
    salt["incus.info"].return_value = {"status": "Running"}
    ret = incus_states.absent("web01")
    assert ret["result"] is None
    salt["incus.delete"].assert_not_called()


# --- sls_applied -----------------------------------------------------------


def test_sls_applied_maps_success(salt):
    salt["incus.info"].return_value = {"status": "Running"}
    salt["incus.sls"].return_value = {"s1": {"result": True, "changes": {"a": 1}, "comment": "c"}}
    ret = incus_states.sls_applied("web01", "mod")
    assert ret["result"] is True
    assert ret["changes"] == {"s1": {"a": 1}}


def test_sls_applied_maps_failure(salt):
    salt["incus.info"].return_value = {"status": "Running"}
    salt["incus.sls"].return_value = {"s1": {"result": False, "changes": {}, "comment": "boom"}}
    ret = incus_states.sls_applied("web01", "mod")
    assert ret["result"] is False
    assert "failed" in ret["comment"]


def test_sls_applied_test_mode_maps_none(salt, opts):
    opts["test"] = True
    salt["incus.info"].return_value = {"status": "Running"}
    salt["incus.sls"].return_value = {
        "s1": {"result": None, "changes": {"a": 1}, "comment": "would"}
    }
    ret = incus_states.sls_applied("web01", "mod")
    assert ret["result"] is None
    _, kwargs = salt["incus.sls"].call_args
    assert kwargs["test"] is True


def test_sls_applied_requires_running(salt):
    salt["incus.info"].return_value = {"status": "Stopped"}
    ret = incus_states.sls_applied("web01", "mod")
    assert ret["result"] is False
    salt["incus.sls"].assert_not_called()


def test_sls_applied_absent_fails(salt):
    salt["incus.info"].return_value = None
    ret = incus_states.sls_applied("web01", "mod")
    assert ret["result"] is False
    salt["incus.sls"].assert_not_called()


def test_sls_applied_inner_exception_fails(salt):
    salt["incus.info"].return_value = {"status": "Running"}
    salt["incus.sls"].side_effect = CommandExecutionError("nope")
    ret = incus_states.sls_applied("web01", "mod")
    assert ret["result"] is False
    assert "failed" in ret["comment"]
