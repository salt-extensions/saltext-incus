"""
Functional tests for the Incus execution module.

These run against a real ``incus`` daemon and are skipped when the binary is
absent (so they no-op in environments without Incus). They use the
salt-factories ``modules`` and ``state_tree`` fixtures supplied by the
functional conftest.

The in-instance apply tests (``call`` and ``sls``) need a Python 3 interpreter
inside the instance; ``_ensure_python3`` bootstraps one with the instance's
package manager and the test self-skips if that is not possible (for example a
minimal image with no outbound network). Override the base image with the
``INCUS_TEST_IMAGE`` environment variable.
"""

import logging
import os
import subprocess

import pytest
from saltfactories.utils import random_string

log = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.slow_test,
    pytest.mark.skip_if_binaries_missing("incus"),
]

DEFAULT_IMAGE = os.environ.get("INCUS_TEST_IMAGE", "images:debian/12")
MARKER = "/root/incus_saltext_marker"


def _incus(*args):
    return subprocess.run(["incus", *args], capture_output=True, text=True, check=False)


def _ensure_python3(name):
    """
    Make a python3 interpreter available inside the instance, returning whether
    it is present. Used to self-skip in-instance apply tests on minimal images.
    """
    if _incus("exec", name, "--", "python3", "--version").returncode == 0:
        return True
    for installer in (
        "command -v apt-get >/dev/null && apt-get update && apt-get install -y python3",
        "command -v apk >/dev/null && apk add --no-cache python3",
        "command -v dnf >/dev/null && dnf install -y python3",
        "command -v yum >/dev/null && yum install -y python3",
    ):
        _incus("exec", name, "--", "sh", "-c", installer)
        if _incus("exec", name, "--", "python3", "--version").returncode == 0:
            return True
    return False


@pytest.fixture(scope="module")
def base_image():
    return DEFAULT_IMAGE


@pytest.fixture
def instance(modules, base_image):
    name = random_string("saltext-incus-", uppercase=False)
    modules.incus.create(name, base_image, start=True)
    try:
        yield name
    finally:
        if modules.incus.exists(name):
            modules.incus.delete(name, force=True)


@pytest.fixture(scope="module")
def demo_state_tree(state_tree):
    sls = """
{}:
  file.managed:
    - contents: configured by saltext-incus
    - mode: "0644"
""".format(MARKER)
    with pytest.helpers.temp_file("demo.sls", sls, state_tree):
        yield state_tree


# --- lifecycle -------------------------------------------------------------


def test_create_and_info(modules, instance):
    info = modules.incus.info(instance)
    assert info is not None
    assert info["status"] == "Running"
    assert modules.incus.exists(instance) is True


def test_stop_and_start(modules, instance):
    modules.incus.stop(instance)
    assert modules.incus.info(instance)["status"] == "Stopped"
    modules.incus.start(instance)
    assert modules.incus.info(instance)["status"] == "Running"


def test_config_roundtrip(modules, instance):
    modules.incus.config_set(instance, "user.saltext-test", "yes")
    assert modules.incus.info(instance)["config"].get("user.saltext-test") == "yes"
    modules.incus.config_unset(instance, "user.saltext-test")
    assert "user.saltext-test" not in modules.incus.info(instance)["config"]


# --- in-instance apply -----------------------------------------------------


def test_call_ping(modules, instance):
    if not _ensure_python3(instance):
        pytest.skip("no python3 available in the test image")
    assert modules.incus.call(instance, "test.ping") is True


def test_sls_applies_and_is_idempotent(modules, instance, demo_state_tree):
    if not _ensure_python3(instance):
        pytest.skip("no python3 available in the test image")
    _incus("exec", instance, "--", "rm", "-f", MARKER)

    first = modules.incus.sls(instance, ["demo"])
    assert isinstance(first, dict) and first
    assert all(state["result"] for state in first.values())
    assert any(state["changes"] for state in first.values())

    second = modules.incus.sls(instance, ["demo"])
    assert all(state["result"] for state in second.values())
    assert not any(state["changes"] for state in second.values())


def test_sls_test_mode_changes_nothing(modules, instance, demo_state_tree):
    if not _ensure_python3(instance):
        pytest.skip("no python3 available in the test image")
    _incus("exec", instance, "--", "rm", "-f", MARKER)

    res = modules.incus.sls(instance, ["demo"], test=True)
    assert isinstance(res, dict) and res
    assert all(state["result"] is None for state in res.values())
    # the file must not have been created in test mode
    assert _incus("exec", instance, "--", "test", "-e", MARKER).returncode != 0


def test_sls_precompiled(modules, instance, demo_state_tree):
    if not _ensure_python3(instance):
        pytest.skip("no python3 available in the test image")
    _incus("exec", instance, "--", "rm", "-f", MARKER)

    res = modules.incus.sls(instance, ["demo"], precompiled=True)
    assert isinstance(res, dict) and res
    assert all(state["result"] for state in res.values())
    assert _incus("exec", instance, "--", "test", "-e", MARKER).returncode == 0


def test_staging_is_cleaned_up(modules, instance):
    if not _ensure_python3(instance):
        pytest.skip("no python3 available in the test image")
    modules.incus.call(instance, "test.ping")
    left = _incus("exec", instance, "--", "sh", "-c", "ls -d /run/salt.incus.* 2>/dev/null | wc -l")
    assert left.stdout.strip() == "0"
