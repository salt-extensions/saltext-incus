"""
Functional tests for the Incus state module.

These run against a real ``incus`` daemon and are skipped when the binary is
absent. They use the salt-factories ``states``, ``modules`` and ``state_tree``
fixtures from the functional conftest. The ``sls_applied`` tests need a Python 3
interpreter inside the instance and self-skip if one cannot be provided.
Override the base image with ``INCUS_TEST_IMAGE``.
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
def name(modules, base_image):
    instance_name = random_string("saltext-incus-", uppercase=False)
    try:
        yield instance_name
    finally:
        if modules.incus.exists(instance_name):
            modules.incus.delete(instance_name, force=True)


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


def test_present_creates_and_is_idempotent(states, name, base_image):
    ret = states.incus.present(name, image=base_image, running=True)
    assert ret["result"] is True
    assert ret["changes"]

    ret2 = states.incus.present(name, image=base_image, running=True)
    assert ret2["result"] is True
    assert ret2["changes"] == {}


def test_present_reconciles_config(states, modules, name, base_image):
    states.incus.present(name, image=base_image)
    ret = states.incus.present(name, config={"user.saltext-test": "yes"})
    assert ret["result"] is True
    assert ret["changes"].get("config")
    assert modules.incus.info(name)["config"].get("user.saltext-test") == "yes"


def test_running_then_stopped(states, name, base_image):
    states.incus.present(name, image=base_image, running=True)

    stopped = states.incus.stopped(name)
    assert stopped["result"] is True
    assert stopped["changes"]
    assert states.incus.stopped(name)["changes"] == {}

    started = states.incus.running(name)
    assert started["result"] is True
    assert started["changes"]


def test_absent(states, name, base_image):
    states.incus.present(name, image=base_image)

    gone = states.incus.absent(name)
    assert gone["result"] is True
    assert gone["changes"]
    assert states.incus.absent(name)["changes"] == {}


def test_sls_applied_converges_and_is_idempotent(states, name, base_image, demo_state_tree):
    states.incus.present(name, image=base_image, running=True)
    if not _ensure_python3(name):
        pytest.skip("no python3 available in the test image")
    _incus("exec", name, "--", "rm", "-f", MARKER)

    ret = states.incus.sls_applied(name, ["demo"])
    assert ret["result"] is True
    assert ret["changes"]

    ret2 = states.incus.sls_applied(name, ["demo"])
    assert ret2["result"] is True
    assert ret2["changes"] == {}


def test_sls_applied_requires_running(states, modules, name, base_image):
    states.incus.present(name, image=base_image, running=False)
    ret = states.incus.sls_applied(name, ["demo"])
    assert ret["result"] is False
