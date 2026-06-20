"""
Execution module for managing Incus instances.

Provides instance lifecycle (create, delete, start, stop, config, devices) as
thin wrappers over the client seam, plus agentless in-instance state via
:py:func:`call`, :py:func:`sls` and :py:func:`sls_build`, which ship the Salt
thin into an instance over ``incus exec`` and run ``salt-call --local`` there.
No resident minion is required: the thin is shipped per run and staging is
removed on every exit.

:py:func:`sls` has two render strategies via ``precompiled``: the default ships
SLS source and renders in-instance; ``precompiled=True`` compiles the low state
on the control node and ships a self-contained tarball. Pillar is always
resolved on the control node and shipped as a root-only file, never on the
command line.

:depends: incus binary
"""

import json
import logging
import os
import shlex
import shutil
import tarfile
import tempfile
import time
import uuid

import salt.client.ssh.state
import salt.fileclient
import salt.utils.hashutils
import salt.utils.json
import salt.utils.path
import salt.utils.state
import salt.utils.thin
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError
from salt.loader.dunder import __file_client__
from salt.state import HighState

log = logging.getLogger(__name__)

__virtualname__ = "incus"

# Don't shadow the built-in ``list``.
__func_alias__ = {"list_": "list"}


class _UtilsProxy:
    """Expose seam functions in ``__utils__`` as attributes (``proxy.exec_in``)."""

    def __init__(self, utils):
        self._utils = utils

    def __getattr__(self, name):
        try:
            return self._utils["incus." + name]
        except KeyError as err:
            raise AttributeError(name) from err


def _seam():
    """
    Return the Incus client seam (:py:mod:`incus.utils.incus`).

    Imports the seam directly when the extension is installed as a package;
    falls back to the copy in ``__utils__`` when the files are synced as custom
    modules, where cross-package imports are not possible.
    """
    try:
        # pylint: disable-next=import-outside-toplevel
        from incus.utils import incus as _incus
    except ImportError:
        return _UtilsProxy(globals().get("__utils__") or {})
    return _incus


# In-instance staging lives on /run (tmpfs, root-only).
_INSTANCE_STAGE_BASE = "/run"


def __virtual__():
    """Only load when the ``incus`` binary is present."""
    if salt.utils.path.which("incus"):
        return __virtualname__
    return (False, "the incus binary was not found")


def _config(key, default):
    """Read a layered config value under the ``incus:`` namespace."""
    return __salt__["config.get"]("incus:" + key, default)


def _validate_transport(transport):
    """Reject transports other than ``thin`` or ``baked``."""
    if transport not in ("thin", "baked"):
        raise SaltInvocationError(f"transport must be 'thin' or 'baked', got {transport!r}")


def _normalize_mods(mods):
    """Normalize ``mods`` (list or comma-separated string) to a clean list."""
    if isinstance(mods, str):
        return [item.strip() for item in mods.split(",") if item.strip()]
    return [str(item).strip() for item in (mods or []) if str(item).strip()]


# ----------------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------------


def create(
    name,
    image,
    profiles=None,
    config=None,
    devices=None,
    start=False,
    ephemeral=False,
    project=None,
):
    """
    Create an instance from ``image`` and return its info dict.

    name
        Instance name.

    image
        Source image, for example ``images:debian/12``.

    profiles
        Optional list of profile names to attach. When omitted the Incus
        default profile applies. Profile membership is not reconciled after
        creation in this version.

    config
        Optional mapping of instance config keys to set at creation.

    devices
        Optional mapping of device name to device definition. Each definition
        must include a ``type`` key.

    start : False
        Start the instance after creating it.

    ephemeral : False
        Create an ephemeral instance (deleted when stopped).

    CLI Example:

    .. code-block:: bash

        salt '*' incus.create web01 images:debian/12 start=True
        salt '*' incus.create web01 images:debian/12 config='{boot.autostart: true}'
    """
    _seam().create_instance(
        name,
        image,
        profiles=profiles,
        config=config,
        devices=devices,
        ephemeral=ephemeral,
        project=project,
    )
    if start:
        _seam().start_instance(name, project=project)
    return _seam().query_instance(name, project=project)


def delete(name, force=True, project=None):
    """
    Delete an instance.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.delete web01
    """
    _seam().delete_instance(name, force=force, project=project)
    return True


def start(name, project=None):
    """
    Start an instance.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.start web01
    """
    _seam().start_instance(name, project=project)
    return True


def stop(name, timeout=30, force=False, project=None):
    """
    Stop an instance.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.stop web01
    """
    _seam().stop_instance(name, timeout=timeout, force=force, project=project)
    return True


def restart(name, timeout=30, force=False, project=None):
    """
    Restart an instance.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.restart web01
    """
    _seam().restart_instance(name, timeout=timeout, force=force, project=project)
    return True


def info(name, project=None):
    """
    Return the instance info dict (config, devices, profiles, status) or
    ``None`` if it does not exist.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.info web01
    """
    return _seam().query_instance(name, project=project)


def exists(name, project=None):
    """
    Return ``True`` if the instance exists.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.exists web01
    """
    return _seam().query_instance(name, project=project) is not None


def list_(project=None):
    """
    Return a list of instance names.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.list
    """
    return [item["name"] for item in _seam().list_instances(project=project)]


def config_set(name, key, value, project=None):
    """
    Set a single instance config key.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.config_set web01 boot.autostart true
    """
    _seam().set_config(name, key, value, project=project)
    return True


def config_unset(name, key, project=None):
    """
    Unset a single instance config key.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.config_unset web01 boot.autostart
    """
    _seam().unset_config(name, key, project=project)
    return True


def device_add(name, device_name, device_type, project=None, **options):
    """
    Add a device to an instance.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.device_add web01 data disk source=/srv/data path=/data
    """
    opts = {key: value for key, value in options.items() if not key.startswith("__")}
    _seam().add_device(name, device_name, device_type, options=opts, project=project)
    return True


def device_remove(name, device_name, project=None):
    """
    Remove a device from an instance.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.device_remove web01 data
    """
    _seam().remove_device(name, device_name, project=project)
    return True


# ----------------------------------------------------------------------------
# In-instance apply: shared helpers
# ----------------------------------------------------------------------------


def _generate_tmp_path():
    """
    A unique, root-only staging path inside the instance, on tmpfs.
    """
    return os.path.join(_INSTANCE_STAGE_BASE, f"salt.incus.{uuid.uuid4().hex[:8]}")


def _local_stage():
    """
    A 0700 staging directory on the control node, on tmpfs when available, with
    a restrictive umask so anything written inside is private.
    """
    base = "/dev/shm" if os.path.isdir("/dev/shm") else None
    old_umask = os.umask(0o077)
    try:
        return tempfile.mkdtemp(prefix="incus-sls.", dir=base)
    finally:
        os.umask(old_umask)


def _file_client():
    """
    Return a file client, reusing the loader's if one is set.
    """
    if __file_client__:
        return __file_client__.value()
    return salt.fileclient.get_file_client(__opts__)


def _detect_python(name, project=None):
    """
    Find a usable Python interpreter inside the instance.
    """
    for candidate in ("python3", "/usr/libexec/platform-python", "python"):
        ret = _seam().exec_in(name, [candidate, "--version"], project=project)
        if ret["retcode"] == 0:
            return candidate
    raise CommandExecutionError(f"no Python 3 interpreter found inside instance '{name}'")


def _parse_salt_call(stdout):
    """
    Parse ``salt-call --metadata --out json`` output into ``(return, retcode)``.
    """
    try:
        data = salt.utils.json.find_json(stdout)
    except ValueError:
        return None, None
    local = data.get("local", data) if isinstance(data, dict) else data
    if isinstance(local, dict):
        return local.get("return", local), local.get("retcode")
    return local, None


def _run_in_instance(name, inst_dir, salt_argv, project=None, cleanup=True):
    """
    Run a ``salt-call`` invocation inside the instance, returning
    ``(return, retcode)``.

    With ``cleanup`` set, the command is wrapped in a shell ``trap`` so staging
    is removed on exit even if the process is killed.
    """
    joined = " ".join(shlex.quote(str(arg)) for arg in salt_argv)
    if cleanup:
        script = f"trap 'rm -rf {shlex.quote(inst_dir)}' EXIT; {joined}"
    else:
        script = joined
    ret = _seam().exec_in(name, ["sh", "-c", script], project=project)
    return_value, retcode = _parse_salt_call(ret["stdout"])
    if return_value is None and ret["retcode"] != 0:
        raise CommandExecutionError(
            "salt-call inside instance '{}' failed (rc={}): {}".format(
                name, ret["retcode"], ret["stderr"] or ret["stdout"]
            )
        )
    if retcode is None:
        retcode = ret["retcode"]
    return return_value, retcode


def _wait_for_exec(name, project=None, timeout=60, interval=1):
    """
    Block until the instance can run a trivial command, or raise on timeout.

    Used after starting a freshly created instance (``sls_build``) so the first
    ``exec`` does not race instance start-up.
    """
    deadline = time.time() + timeout
    while True:
        ret = _seam().exec_in(name, ["true"], project=project)
        if ret["retcode"] == 0:
            return True
        if time.time() >= deadline:
            raise CommandExecutionError(
                f"instance '{name}' did not become ready for exec within {timeout}s"
            )
        time.sleep(interval)


def _gen_thin():
    """Generate the Salt thin tarball."""
    return salt.utils.thin.gen_thin(
        __opts__["cachedir"],
        extra_mods=_config("thin_extra_mods", ""),
        so_mods=_config("thin_so_mods", ""),
    )


def _ship_thin(name, inst_dir, python_bin, project=None):
    """
    Push the thin tarball into ``inst_dir`` and extract it there. Returns the
    path to the extracted ``salt-call`` entrypoint.
    """
    thin_path = _gen_thin()
    remote = os.path.join(inst_dir, os.path.basename(thin_path))
    _seam().push_file(name, thin_path, remote, mode="0600", project=project)
    untar = [
        python_bin,
        "-c",
        f'import tarfile; tarfile.open("{remote}").extractall(path="{inst_dir}")',
    ]
    ret = _seam().exec_in(name, untar, project=project)
    if ret["retcode"] != 0:
        raise CommandExecutionError(
            "could not unpack thin in instance '{}': {}".format(name, ret["stderr"])
        )
    return os.path.join(inst_dir, "salt-call")


# ----------------------------------------------------------------------------
# In-instance apply: call
# ----------------------------------------------------------------------------


def call(name, function, *args, project=None, transport="thin", **kwargs):
    """
    Run a single Salt execution function inside an instance.

    The instance does not need Salt installed (with ``transport='thin'``, the
    default); it only needs a Python 3 interpreter. With ``transport='baked'``
    the instance's own ``salt-call`` is used and no thin is shipped.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.call web01 test.ping
        salt '*' incus.call web01 cmd.run 'id -un'
    """
    if function is None:
        raise CommandExecutionError("Missing function parameter")
    _validate_transport(transport)

    inst_dir = _generate_tmp_path()
    ret = _seam().exec_in(name, ["mkdir", "-p", "-m", "0700", inst_dir], project=project)
    if ret["retcode"] != 0:
        raise CommandExecutionError(
            "could not create staging dir in instance '{}': {}".format(name, ret["stderr"])
        )
    try:
        python_bin = _detect_python(name, project=project)
        if transport == "baked":
            salt_argv = ["salt-call"]
        else:
            salt_call = _ship_thin(name, inst_dir, python_bin, project=project)
            salt_argv = [python_bin, salt_call]
        salt_argv += [
            "--metadata",
            "--local",
            "--log-file",
            os.path.join(inst_dir, "log"),
            "--cachedir",
            os.path.join(inst_dir, "cache"),
            "--out",
            "json",
            "-l",
            "quiet",
            "--retcode-passthrough",
            "--",
            function,
        ]
        salt_argv += [str(arg) for arg in args]
        salt_argv += [f"{key}={value}" for key, value in kwargs.items() if not key.startswith("__")]
        return_value, retcode = _run_in_instance(
            name, inst_dir, salt_argv, project=project, cleanup=True
        )
        if retcode is not None:
            __context__["retcode"] = retcode
        return return_value
    finally:
        _seam().delete_path(name, inst_dir, project=project)


# ----------------------------------------------------------------------------
# In-instance apply: sls (source strategy)
# ----------------------------------------------------------------------------


def _stage_sls_source(states_dir, mods, saltenv):
    """
    Copy the SLS source for ``mods`` into ``states_dir``.

    Ships each mod's whole top-level component (so ``access.users`` pulls all of
    ``salt://access/``), covering ``include:`` and ``map.jinja``. File sources in
    other components are not shipped; use ``precompiled=True`` for those.
    """
    components = {mod.split(".")[0] for mod in mods}
    sls_files = {mod.replace(".", "/") + ".sls" for mod in mods}
    component_sls = {component + ".sls" for component in components}

    available = __salt__["cp.list_master"](saltenv) or []
    selected = set()
    for relpath in available:
        seg0 = relpath.split("/")[0]
        if seg0 in components or relpath in sls_files or relpath in component_sls:
            selected.add(relpath)

    if not selected:
        raise CommandExecutionError(f"no SLS source found for mods {mods} in saltenv '{saltenv}'")

    for relpath in sorted(selected):
        cached = __salt__["cp.cache_file"]("salt://" + relpath, saltenv)
        if not cached:
            continue
        dest = os.path.join(states_dir, *relpath.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(cached, dest)


def _stage_pillar(pillar_dir, pillar):
    """
    Write the resolved pillar as a root-only file tree. The ``#!json`` renderer
    sidesteps any Jinja/YAML ambiguity in pillar values.
    """
    if not pillar:
        return
    with open(os.path.join(pillar_dir, "top.sls"), "w", encoding="utf-8") as fh:
        fh.write("#!json\n")
        json.dump({"base": {"*": ["incus_pillar"]}}, fh)
    with open(os.path.join(pillar_dir, "incus_pillar.sls"), "w", encoding="utf-8") as fh:
        fh.write("#!json\n")
        json.dump(pillar, fh)


def _make_stage_tar(stage_dir):
    """
    Tar the contents of ``stage_dir`` (top-level entries become root entries)
    so a single push and a single in-instance extraction reproduce the layout.
    """
    tar_path = stage_dir + ".tgz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for entry in sorted(os.listdir(stage_dir)):
            tar.add(os.path.join(stage_dir, entry), arcname=entry)
    return tar_path


def _sls_source(name, mods, saltenv, pillar, test, transport, pillar_mode, cleanup, project):
    """Apply ``mods`` by shipping SLS source and running ``state.apply`` in-instance."""
    if pillar_mode != "file":
        raise SaltInvocationError(
            "incus supports pillar_mode='file' only; passing pillar on the "
            "command line is intentionally unsupported"
        )

    inst_dir = _generate_tmp_path()
    stage = _local_stage()
    stage_tar = None
    try:
        ret = _seam().exec_in(name, ["mkdir", "-p", "-m", "0700", inst_dir], project=project)
        if ret["retcode"] != 0:
            raise CommandExecutionError(
                "could not create staging dir in instance '{}': {}".format(name, ret["stderr"])
            )
        python_bin = _detect_python(name, project=project)

        states_dir = os.path.join(stage, "states")
        pillar_dir = os.path.join(stage, "pillar")
        conf_dir = os.path.join(stage, "conf")
        for directory in (states_dir, pillar_dir, conf_dir):
            os.makedirs(directory, mode=0o700, exist_ok=True)

        _stage_sls_source(states_dir, mods, saltenv)
        _stage_pillar(pillar_dir, pillar)

        # Masterless minion config points at the in-instance paths under inst_dir.
        minion_conf = {
            "file_client": "local",
            "fileserver_backend": ["roots"],
            "file_roots": {"base": [os.path.join(inst_dir, "states")]},
            "pillar_roots": {"base": [os.path.join(inst_dir, "pillar")]},
            "cachedir": os.path.join(inst_dir, "cache"),
            "log_file": os.path.join(inst_dir, "salt.log"),
            "log_level": "quiet",
            "log_level_logfile": "quiet",
        }
        with open(os.path.join(conf_dir, "minion"), "w", encoding="utf-8") as fh:
            json.dump(minion_conf, fh)

        thin_basename = None
        if transport != "baked":
            thin_path = _gen_thin()
            thin_basename = os.path.basename(thin_path)
            shutil.copyfile(thin_path, os.path.join(stage, thin_basename))

        stage_tar = _make_stage_tar(stage)
        remote_tar = os.path.join(inst_dir, "stage.tgz")
        _seam().push_file(name, stage_tar, remote_tar, mode="0600", project=project)
        extract = [
            python_bin,
            "-c",
            f'import tarfile; tarfile.open("{remote_tar}").extractall(path="{inst_dir}")',
        ]
        ret = _seam().exec_in(name, extract, project=project)
        if ret["retcode"] != 0:
            raise CommandExecutionError(
                "could not unpack staging tarball in instance '{}': {}".format(name, ret["stderr"])
            )
        _seam().exec_in(name, ["chmod", "-R", "go-rwx", inst_dir], project=project)

        if transport == "baked":
            salt_call = ["salt-call"]
        else:
            thin_dir = os.path.join(inst_dir, "thin")
            untar = [
                python_bin,
                "-c",
                'import tarfile; tarfile.open("{}").extractall(path="{}")'.format(
                    os.path.join(inst_dir, thin_basename), thin_dir
                ),
            ]
            ret = _seam().exec_in(name, untar, project=project)
            if ret["retcode"] != 0:
                raise CommandExecutionError(
                    "could not unpack thin in instance '{}': {}".format(name, ret["stderr"])
                )
            salt_call = [python_bin, os.path.join(thin_dir, "salt-call")]

        salt_argv = salt_call + [
            "--config-dir",
            os.path.join(inst_dir, "conf"),
            "--local",
            "--metadata",
            "--out",
            "json",
            "-l",
            "quiet",
            "--retcode-passthrough",
            "state.apply",
            ",".join(mods),
        ]
        if test:
            salt_argv.append("test=True")

        return_value, _ = _run_in_instance(
            name, inst_dir, salt_argv, project=project, cleanup=cleanup
        )
        return return_value
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if stage_tar:
            try:
                os.remove(stage_tar)
            except OSError:
                pass
        if cleanup:
            _seam().delete_path(name, inst_dir, project=project)


# ----------------------------------------------------------------------------
# In-instance apply: sls (precompiled strategy)
# ----------------------------------------------------------------------------


def _compile_state(sls_opts, mods):
    """
    Compile the requested ``mods`` to low-state chunks on the control node.

    Returns the list of chunks, or a list of error strings if compilation
    failed. Adapted from ``docker.sls``.
    """
    with HighState(sls_opts) as st_:
        if not mods:
            return st_.compile_low_chunks()

        high_data, errors = st_.render_highstate({sls_opts["saltenv"]: mods})
        high_data, ext_errors = st_.state.reconcile_extend(high_data)
        errors += ext_errors
        errors += st_.state.verify_high(high_data)
        if errors:
            return errors

        high_data, req_in_errors = st_.state.requisite_in(high_data)
        errors += req_in_errors
        high_data = st_.state.apply_exclude(high_data)
        if errors:
            return errors

        return st_.state.compile_high_data(high_data)


def _prepare_trans_tar(sls_opts, mods, pillar, extra_filerefs=""):
    """
    Build a self-contained state tarball (compiled chunks + referenced files +
    pillar) using the salt-ssh state machinery.
    """
    chunks = _compile_state(sls_opts, mods)
    if not chunks:
        raise CommandExecutionError(f"no state was compiled for mods {mods}")
    if isinstance(chunks, list) and chunks and isinstance(chunks[0], str):
        raise CommandExecutionError(
            "state compilation failed: {}".format("; ".join(str(item) for item in chunks))
        )
    refs = salt.client.ssh.state.lowstate_file_refs(chunks, extra_filerefs)
    with _file_client() as fileclient:
        return salt.client.ssh.state.prep_trans_tar(
            fileclient, chunks, refs, pillar or {}, __opts__["id"]
        )


def _sls_precompiled(
    name, mods, saltenv, pillar, test, transport, cleanup, project, extra_filerefs
):
    """Apply ``mods`` by compiling low state on the control node and shipping a tarball."""
    # Gather the instance's grains so host-side rendering matches the instance.
    grains = call(name, "grains.items", project=project, transport=transport)

    sls_opts = salt.utils.state.get_sls_opts(__opts__, saltenv=saltenv)
    if isinstance(grains, dict):
        sls_opts["grains"].update(grains)
    if pillar:
        sls_opts["pillar"].update(pillar)

    trans_tar = _prepare_trans_tar(sls_opts, mods, pillar, extra_filerefs)
    try:
        trans_sha = salt.utils.hashutils.get_hash(trans_tar, "sha256")
        tar_dir = _generate_tmp_path()
        ret = _seam().exec_in(name, ["mkdir", "-p", "-m", "0700", tar_dir], project=project)
        if ret["retcode"] != 0:
            raise CommandExecutionError(
                "could not create staging dir in instance '{}': {}".format(name, ret["stderr"])
            )
        remote_tar = os.path.join(tar_dir, "salt_state.tgz")
        _seam().push_file(name, trans_tar, remote_tar, mode="0600", project=project)
        try:
            return call(
                name,
                "state.pkg",
                remote_tar,
                trans_sha,
                "sha256",
                test=test,
                project=project,
                transport=transport,
            )
        finally:
            if cleanup:
                _seam().delete_path(name, tar_dir, project=project)
    finally:
        try:
            os.remove(trans_tar)
        except OSError:
            pass


def sls(
    name,
    mods,
    saltenv="base",
    pillar=None,
    test=False,
    transport="thin",
    pillar_mode="file",
    precompiled=False,
    cleanup=True,
    project=None,
    extra_filerefs="",
):
    """
    Apply the states in ``mods`` inside an instance and return the highstate
    result dict.

    name
        Instance name.

    mods
        SLS modules to apply, as a list or a comma-separated string.

    saltenv : base
        Environment to pull the SLS / compile against on the control node.

    pillar
        Pillar to use, already resolved on the control node (decrypt GPG/Vault
        values before passing them here). Shipped as a root-only file, never on
        the command line.

    test : False
        Run in test mode; nothing is changed inside the instance.

    transport : thin
        ``thin`` ships the Salt thin per run (the instance needs only Python 3).
        ``baked`` uses the instance's own ``salt-call``.

    pillar_mode : file
        How pillar is delivered. Only ``file`` is supported.

    precompiled : False
        ``False`` ships SLS source and runs ``state.apply`` in-instance.
        ``True`` compiles low state on the control node and applies it with
        ``state.pkg`` so no source reaches the instance.

    cleanup : True
        Remove the in-instance staging directory after the run. Set ``False``
        to leave it in place for debugging.

    CLI Example:

    .. code-block:: bash

        salt '*' incus.sls web01 mods=access.users,access.sshd
        salt '*' incus.sls web01 mods=hardening precompiled=True test=True
    """
    _validate_transport(transport)
    mods = _normalize_mods(mods)
    if not mods:
        raise SaltInvocationError("at least one SLS module is required")

    if precompiled:
        result = _sls_precompiled(
            name, mods, saltenv, pillar, test, transport, cleanup, project, extra_filerefs
        )
    else:
        result = _sls_source(
            name, mods, saltenv, pillar, test, transport, pillar_mode, cleanup, project
        )

    if not isinstance(result, dict):
        __context__["retcode"] = 1
    elif not salt.utils.state.check_result(result):
        __context__["retcode"] = 2
    else:
        __context__["retcode"] = 0
    return result


# ----------------------------------------------------------------------------
# In-instance apply: sls_build
# ----------------------------------------------------------------------------


def sls_build(
    name,
    base,
    mods,
    project=None,
    public=False,
    saltenv="base",
    pillar=None,
    test=False,
    transport="thin",
    precompiled=False,
    cleanup=True,
):
    """
    Build an image by applying states to a throwaway instance, then publishing.

    A temporary instance is launched from ``base``, ``mods`` are applied inside
    it with :py:func:`sls`, the instance is stopped and published to a local
    image aliased ``name``, and the temporary instance is always deleted.

    name
        Alias for the resulting image.

    base
        Source image to build from, for example ``images:debian/12``.

    mods
        SLS modules to apply, as a list or comma-separated string.

    test : False
        Compile and apply in test mode and skip publishing (a dry run).

    CLI Example:

    .. code-block:: bash

        salt '*' incus.sls_build mycorp/web base=images:debian/12 mods=web,hardening
    """
    _validate_transport(transport)
    build_name = f"salt-build-{uuid.uuid4().hex[:8]}"
    created = False
    running = False
    try:
        _seam().create_instance(build_name, base, project=project)
        created = True
        _seam().start_instance(build_name, project=project)
        running = True
        _wait_for_exec(build_name, project=project)

        result = sls(
            build_name,
            mods,
            saltenv=saltenv,
            pillar=pillar,
            test=test,
            transport=transport,
            precompiled=precompiled,
            cleanup=cleanup,
            project=project,
        )
        if not (isinstance(result, dict) and salt.utils.state.check_result(result)):
            raise CommandExecutionError(
                "state application failed during sls_build; image not published"
            )
        if test:
            return {"result": result, "published": False, "comment": "test mode: not published"}

        _seam().stop_instance(build_name, project=project)
        running = False
        published = _seam().publish(build_name, alias=name, public=public, project=project)
        return {"result": result, "published": published}
    finally:
        if running:
            try:
                _seam().stop_instance(build_name, force=True, project=project)
            except CommandExecutionError:
                log.error("sls_build: could not stop build instance '%s'", build_name)
        if created:
            try:
                _seam().delete_instance(build_name, force=True, project=project)
            except CommandExecutionError:
                log.error("sls_build: could not delete build instance '%s'", build_name)
