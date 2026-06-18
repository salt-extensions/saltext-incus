"""
Client seam for Incus: the single place the extension runs the ``incus`` CLI.

Every execution and state function reaches Incus through these helpers rather
than shelling out directly, so a REST backend can later replace the CLI calls
here without touching callers. Reads use ``incus query`` (JSON over the REST
path); writes use ``incus`` subcommands. Commands are argument lists run without
a shell.

:depends: incus binary
"""

import json
import logging
import re
import subprocess

import salt.utils.path
from salt.exceptions import CommandExecutionError

log = logging.getLogger(__name__)

__virtualname__ = "incus"


def __virtual__():
    """Only load when the ``incus`` binary is present."""
    if salt.utils.path.which("incus"):
        return __virtualname__
    return (False, "the incus binary was not found")


def _stringify(value):
    """
    Render a config/device value the way the Incus CLI expects it.

    Incus config and device values are strings. Booleans are lower-cased so
    ``True`` becomes ``true`` rather than Python's ``True``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cmd_run_all(cmd, stdin=None):
    """
    Run an argument list and return a ``cmd.run_all``-style dict (``retcode``,
    ``stdout``, ``stderr``).

    Uses :py:mod:`subprocess` rather than ``__salt__["cmd.run_all"]`` so the
    seam needs no injected dunders and works whether the extension is installed
    as a package or synced as custom modules.
    """
    proc = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    return {
        "retcode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def _run(args, project=None, ignore_retcode=False):
    """
    Run an ``incus`` CLI command and return ``(retcode, stdout, stderr)``.

    Raises :py:class:`CommandExecutionError <salt.exceptions.CommandExecutionError>`
    on a non-zero return code unless ``ignore_retcode`` is set, in which case
    the caller is responsible for inspecting the return code.
    """
    cmd = ["incus"]
    if project:
        cmd += ["--project", project]
    cmd += [str(arg) for arg in args]
    out = _cmd_run_all(cmd)
    if out["retcode"] != 0 and not ignore_retcode:
        raise CommandExecutionError(
            "incus {} failed: {}".format(" ".join(str(a) for a in args), out["stderr"])
        )
    return out["retcode"], out["stdout"], out["stderr"]


def query(path, project=None, ignore_retcode=False):
    """
    Issue ``incus query`` against a REST ``path`` and return the parsed JSON.

    Returns ``None`` when the query fails and ``ignore_retcode`` is set.
    """
    rc, stdout, _ = _run(["query", path], project=project, ignore_retcode=ignore_retcode)
    if ignore_retcode and rc != 0:
        return None
    if not stdout.strip():
        return None
    return json.loads(stdout)


def query_instance(name, project=None):
    """
    Return the instance dict for ``name`` or ``None`` if it does not exist.

    The dict carries ``config``, ``devices``, ``profiles``, ``status`` and the
    expanded variants, exactly as the REST API reports them.
    """
    rc, stdout, _ = _run(
        ["query", f"/1.0/instances/{name}?recursion=1"],
        project=project,
        ignore_retcode=True,
    )
    if rc != 0 or not stdout.strip():
        return None
    return json.loads(stdout)


def list_instances(project=None):
    """
    Return a list of instance dicts for the project.
    """
    rc, stdout, _ = _run(
        ["query", "/1.0/instances?recursion=1"], project=project, ignore_retcode=True
    )
    if rc != 0 or not stdout.strip():
        return []
    return json.loads(stdout)


def create_instance(
    name, image, profiles=None, config=None, devices=None, ephemeral=False, project=None
):
    """
    Create (but do not start) an instance from ``image``.

    ``config`` is applied at creation; ``devices`` are added afterwards, one per
    device. With ``profiles=None`` the Incus default profile applies.
    """
    args = ["create", image, name]
    if ephemeral:
        args.append("--ephemeral")
    for prof in profiles or []:
        args += ["--profile", prof]
    for key, value in (config or {}).items():
        args += ["-c", f"{key}={_stringify(value)}"]
    _run(args, project=project)

    for device_name, device in (devices or {}).items():
        device = dict(device)
        device_type = device.pop("type", None)
        if device_type is None:
            raise CommandExecutionError(
                f"device '{device_name}' on instance '{name}' is missing a 'type'"
            )
        add_device(name, device_name, device_type, options=device, project=project)


def delete_instance(name, force=True, project=None):
    """
    Delete an instance. ``force`` is required to delete a running instance.
    """
    args = ["delete", name]
    if force:
        args.append("--force")
    _run(args, project=project)


def start_instance(name, project=None):
    """
    Start an instance.
    """
    _run(["start", name], project=project)


def stop_instance(name, timeout=30, force=False, project=None):
    """
    Stop an instance. ``force`` kills it immediately; otherwise ``timeout``
    seconds are allowed for a clean shutdown.
    """
    args = ["stop", name]
    if force:
        args.append("--force")
    else:
        args += ["--timeout", str(timeout)]
    _run(args, project=project)


def restart_instance(name, timeout=30, force=False, project=None):
    """
    Restart an instance.
    """
    args = ["restart", name]
    if force:
        args.append("--force")
    else:
        args += ["--timeout", str(timeout)]
    _run(args, project=project)


def set_config(name, key, value, project=None):
    """
    Set a single instance config key.
    """
    _run(["config", "set", name, key, _stringify(value)], project=project)


def unset_config(name, key, project=None):
    """
    Unset a single instance config key.
    """
    _run(["config", "unset", name, key], project=project)


def add_device(name, device_name, device_type, options=None, project=None):
    """
    Add a device to an instance.

    ``options`` is a mapping of the device's properties (everything other than
    ``type``), for example ``{"source": "/srv/data", "path": "/data"}`` for a
    disk device.
    """
    args = ["config", "device", "add", name, device_name, device_type]
    for key, value in (options or {}).items():
        args.append(f"{key}={_stringify(value)}")
    _run(args, project=project)


def remove_device(name, device_name, project=None):
    """
    Remove a device from an instance.
    """
    _run(["config", "device", "remove", name, device_name], project=project)


def exec_in(name, argv, project=None, environment=None, cwd=None, stdin=None):
    """
    Run ``argv`` inside the instance and return a ``cmd.run_all``-style dict.

    The return code is surfaced, not raised, so callers can inspect it. Runs as
    the instance's root user.
    """
    cmd = ["incus"]
    if project:
        cmd += ["--project", project]
    cmd += ["exec", name]
    for key, value in (environment or {}).items():
        cmd += ["--env", f"{key}={value}"]
    if cwd:
        cmd += ["--cwd", cwd]
    cmd += ["--"] + [str(arg) for arg in argv]
    return _cmd_run_all(cmd, stdin=stdin)


def push_file(
    name,
    local_path,
    remote_path,
    mode="0600",
    uid=0,
    gid=0,
    recursive=False,
    create_dirs=True,
    project=None,
):
    """
    Push a file or directory into the instance.

    Defaults are deliberately locked down: ``0600`` and root ownership. The
    target is addressed as ``<instance><absolute-remote-path>`` which is how the
    Incus CLI maps a push onto an instance path.
    """
    args = [
        "file",
        "push",
        local_path,
        f"{name}{remote_path}",
        "--mode",
        mode,
        "--uid",
        str(uid),
        "--gid",
        str(gid),
    ]
    if create_dirs:
        args.append("--create-dirs")
    if recursive:
        args.append("--recursive")
    _run(args, project=project)


def delete_path(name, remote_path, project=None):
    """
    Remove a path inside the instance with ``rm -rf`` (never fails if absent).
    """
    return exec_in(name, ["rm", "-rf", remote_path], project=project)


def publish(name, alias=None, public=False, force=False, project=None):
    """
    Publish a (normally stopped) instance to a local image.

    Returns a dict with the image ``fingerprint`` (parsed from the CLI output)
    and the ``alias`` if one was requested.
    """
    args = ["publish", name]
    if force:
        args.append("--force")
    if public:
        args.append("--public")
    if alias:
        args += ["--alias", alias]
    _, stdout, _ = _run(args, project=project)
    fingerprint = None
    match = re.search(r"fingerprint:\s*([0-9a-fA-F]+)", stdout)
    if match:
        fingerprint = match.group(1)
    return {"fingerprint": fingerprint, "alias": alias}
