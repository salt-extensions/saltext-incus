"""
States for managing Incus instances.

:py:func:`present`, :py:func:`running`, :py:func:`stopped` and :py:func:`absent`
manage instance lifecycle; :py:func:`sls_applied` applies SLS inside an instance
through the agentless thin path and surfaces the in-instance highstate result.

:py:func:`present` reconciles declared config and devices additively: it sets
what you declare and leaves undeclared keys, devices and profile membership
alone.

:depends: incus execution module
"""

from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

__virtualname__ = "incus"


def __virtual__():
    """Only load when the incus execution module is available."""
    if "incus.info" in __salt__:
        return __virtualname__
    return (False, "the incus execution module is not available")


def _stringify(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _normalize_device(device):
    return {key: _stringify(value) for key, value in (device or {}).items()}


def _config_deltas(current_config, desired_config):
    """
    Return ``{key: {"old": ..., "new": ...}}`` for declared keys that differ.
    """
    deltas = {}
    for key, value in (desired_config or {}).items():
        desired = _stringify(value)
        current = current_config.get(key)
        if current != desired:
            deltas[key] = {"old": current, "new": desired}
    return deltas


def _device_deltas(current_devices, desired_devices):
    """
    Return ``{device: {"old": ..., "new": ...}}`` for declared devices that are
    missing or differ.
    """
    deltas = {}
    for device_name, device in (desired_devices or {}).items():
        desired = _normalize_device(device)
        current = current_devices.get(device_name)
        current_norm = _normalize_device(current) if current is not None else None
        if current_norm != desired:
            deltas[device_name] = {"old": current, "new": device}
    return deltas


def present(
    name,
    image=None,
    profiles=None,
    config=None,
    devices=None,
    running=None,
    project=None,
):
    """
    Ensure an instance exists with the declared config and devices.

    name
        Instance name.

    image
        Source image. Required only when the instance must be created.

    profiles
        Profiles to attach at creation. Not reconciled after creation.

    config
        Instance config keys to ensure are set.

    devices
        Devices to ensure are present, mapping device name to a definition that
        includes a ``type``.

    running
        ``True`` ensures the instance is running, ``False`` ensures it is
        stopped, ``None`` leaves the run state untouched.

    Example:

    .. code-block:: yaml

        web01:
          incus.present:
            - image: images:debian/12
            - running: true
            - config:
                boot.autostart: true
    """
    ret = {"name": name, "result": True, "changes": {}, "comment": ""}
    test = __opts__["test"]

    try:
        current = __salt__["incus.info"](name, project=project)
    except CommandExecutionError as exc:
        ret["result"] = False
        ret["comment"] = f"could not query instance '{name}': {exc}"
        return ret

    # --- create path -------------------------------------------------------
    if current is None:
        if not image:
            ret["result"] = False
            ret["comment"] = "instance '{}' is absent and no image was given to create it".format(
                name
            )
            return ret
        if test:
            ret["result"] = None
            ret["changes"] = {name: {"old": None, "new": f"created from {image}"}}
            ret["comment"] = f"instance '{name}' would be created from {image}"
            return ret
        try:
            __salt__["incus.create"](
                name,
                image,
                profiles=profiles,
                config=config,
                devices=devices,
                start=(running is True),
                project=project,
            )
            if running is False:
                # creation leaves it stopped already; nothing to do
                pass
        except CommandExecutionError as exc:
            ret["result"] = False
            ret["comment"] = f"could not create instance '{name}': {exc}"
            return ret
        ret["changes"] = {name: {"old": None, "new": f"created from {image}"}}
        ret["comment"] = f"instance '{name}' created"
        return ret

    # --- reconcile path ----------------------------------------------------
    config_deltas = _config_deltas(current.get("config", {}), config)
    device_deltas = _device_deltas(current.get("devices", {}), devices)

    status = current.get("status")
    run_delta = None
    if running is True and status != "Running":
        run_delta = {"old": status, "new": "Running"}
    elif running is False and status == "Running":
        run_delta = {"old": status, "new": "Stopped"}

    if not config_deltas and not device_deltas and not run_delta:
        ret["comment"] = f"instance '{name}' is already in the declared state"
        return ret

    changes = {}
    if config_deltas:
        changes["config"] = config_deltas
    if device_deltas:
        changes["devices"] = device_deltas
    if run_delta:
        changes["status"] = run_delta

    if test:
        ret["result"] = None
        ret["changes"] = changes
        ret["comment"] = f"instance '{name}' would be updated"
        return ret

    try:
        for key, delta in config_deltas.items():
            __salt__["incus.config_set"](name, key, delta["new"], project=project)
        for device_name, delta in device_deltas.items():
            if delta["old"] is not None:
                __salt__["incus.device_remove"](name, device_name, project=project)
            device = dict(delta["new"])
            device_type = device.pop("type", None)
            if device_type is None:
                raise CommandExecutionError(f"device '{device_name}' is missing a 'type'")
            __salt__["incus.device_add"](name, device_name, device_type, project=project, **device)
        if run_delta and run_delta["new"] == "Running":
            __salt__["incus.start"](name, project=project)
        elif run_delta and run_delta["new"] == "Stopped":
            __salt__["incus.stop"](name, project=project)
    except CommandExecutionError as exc:
        ret["result"] = False
        ret["comment"] = f"could not update instance '{name}': {exc}"
        ret["changes"] = changes
        return ret

    ret["changes"] = changes
    ret["comment"] = f"instance '{name}' updated"
    return ret


def running(name, project=None):
    """
    Ensure an existing instance is running.

    Example:

    .. code-block:: yaml

        web01 running:
          incus.running:
            - name: web01
    """
    ret = {"name": name, "result": True, "changes": {}, "comment": ""}
    test = __opts__["test"]

    current = __salt__["incus.info"](name, project=project)
    if current is None:
        ret["result"] = False
        ret["comment"] = f"instance '{name}' does not exist"
        return ret
    if current.get("status") == "Running":
        ret["comment"] = f"instance '{name}' is already running"
        return ret
    if test:
        ret["result"] = None
        ret["changes"] = {"status": {"old": current.get("status"), "new": "Running"}}
        ret["comment"] = f"instance '{name}' would be started"
        return ret
    try:
        __salt__["incus.start"](name, project=project)
    except CommandExecutionError as exc:
        ret["result"] = False
        ret["comment"] = f"could not start instance '{name}': {exc}"
        return ret
    ret["changes"] = {"status": {"old": current.get("status"), "new": "Running"}}
    ret["comment"] = f"instance '{name}' started"
    return ret


def stopped(name, timeout=30, force=False, project=None):
    """
    Ensure an existing instance is stopped.

    Example:

    .. code-block:: yaml

        web01 stopped:
          incus.stopped:
            - name: web01
    """
    ret = {"name": name, "result": True, "changes": {}, "comment": ""}
    test = __opts__["test"]

    current = __salt__["incus.info"](name, project=project)
    if current is None:
        ret["result"] = False
        ret["comment"] = f"instance '{name}' does not exist"
        return ret
    if current.get("status") != "Running":
        ret["comment"] = f"instance '{name}' is already stopped"
        return ret
    if test:
        ret["result"] = None
        ret["changes"] = {"status": {"old": current.get("status"), "new": "Stopped"}}
        ret["comment"] = f"instance '{name}' would be stopped"
        return ret
    try:
        __salt__["incus.stop"](name, timeout=timeout, force=force, project=project)
    except CommandExecutionError as exc:
        ret["result"] = False
        ret["comment"] = f"could not stop instance '{name}': {exc}"
        return ret
    ret["changes"] = {"status": {"old": current.get("status"), "new": "Stopped"}}
    ret["comment"] = f"instance '{name}' stopped"
    return ret


def absent(name, force=True, project=None):
    """
    Ensure an instance does not exist.

    Example:

    .. code-block:: yaml

        web01 absent:
          incus.absent:
            - name: web01
    """
    ret = {"name": name, "result": True, "changes": {}, "comment": ""}
    test = __opts__["test"]

    current = __salt__["incus.info"](name, project=project)
    if current is None:
        ret["comment"] = f"instance '{name}' is already absent"
        return ret
    if test:
        ret["result"] = None
        ret["changes"] = {name: {"old": "present", "new": "absent"}}
        ret["comment"] = f"instance '{name}' would be deleted"
        return ret
    try:
        __salt__["incus.delete"](name, force=force, project=project)
    except CommandExecutionError as exc:
        ret["result"] = False
        ret["comment"] = f"could not delete instance '{name}': {exc}"
        return ret
    ret["changes"] = {name: {"old": "present", "new": "absent"}}
    ret["comment"] = f"instance '{name}' deleted"
    return ret


def _process_inner_highstate(ret, inner, test):
    """
    Map the in-instance highstate result onto the outer state return.

    A failed inner state makes the outer state fail. In test mode the outer
    result is ``None``. Inner changes surface through the outer ``changes``.
    """
    if not isinstance(inner, dict) or not inner:
        ret["result"] = False
        ret["comment"] = "in-instance apply returned no state data"
        if inner:
            ret["comment"] += f": {inner}"
        return ret

    failed = []
    changed = {}
    total = 0
    for state_id, sdata in inner.items():
        if not isinstance(sdata, dict):
            continue
        total += 1
        if sdata.get("result") is False:
            failed.append((state_id, sdata.get("comment", "")))
        if sdata.get("changes"):
            changed[state_id] = sdata["changes"]

    if failed:
        ret["result"] = False
    elif test:
        ret["result"] = None
    else:
        ret["result"] = True

    if changed:
        ret["changes"] = changed

    if failed:
        ret["comment"] = "{} of {} in-instance states failed: {}".format(
            len(failed), total, "; ".join(f"{sid} ({msg})" for sid, msg in failed)
        )
    elif test:
        ret["comment"] = "{} in-instance states would be applied ({} with changes)".format(
            total, len(changed)
        )
    else:
        ret["comment"] = f"applied {total} in-instance states ({len(changed)} changed)"
    return ret


def sls_applied(
    name,
    mods,
    pillar=None,
    saltenv="base",
    transport="thin",
    pillar_mode="file",
    precompiled=False,
    cleanup=True,
    project=None,
):
    """
    Apply SLS ``mods`` inside a running instance via the agentless thin path.

    The instance must already exist and be running (compose this after
    :py:func:`present` or :py:func:`running`). The in-instance highstate result
    is mapped onto this state's result: any failed inner state fails this
    state, ``test=True`` yields a ``None`` result, and the inner changes are
    reported through ``changes``.

    name
        Instance name.

    mods
        SLS modules to apply, as a list or comma-separated string.

    pillar
        Pillar to use, already resolved on the control node.

    precompiled : False
        Compile on the control node and ship low state instead of SLS source.
        See :py:func:`incus.sls <salt.modules.incus.sls>`.

    Example:

    .. code-block:: yaml

        web01 configured:
          incus.sls_applied:
            - name: web01
            - mods:
              - access.users
              - access.sshd
            - pillar:
                access: {{ salt['pillar.get']('access') | json }}
    """
    ret = {"name": name, "result": None, "changes": {}, "comment": ""}
    test = __opts__["test"]

    current = __salt__["incus.info"](name, project=project)
    if current is None:
        ret["result"] = False
        ret["comment"] = f"instance '{name}' does not exist"
        return ret
    if current.get("status") != "Running":
        ret["result"] = False
        ret["comment"] = f"instance '{name}' is not running"
        return ret

    try:
        inner = __salt__["incus.sls"](
            name,
            mods,
            saltenv=saltenv,
            pillar=pillar,
            test=test,
            transport=transport,
            pillar_mode=pillar_mode,
            precompiled=precompiled,
            cleanup=cleanup,
            project=project,
        )
    except (CommandExecutionError, SaltInvocationError) as exc:
        ret["result"] = False
        ret["comment"] = f"in-instance apply failed: {exc}"
        return ret

    return _process_inner_highstate(ret, inner, test)
