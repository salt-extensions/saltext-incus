"""
Salt state module
"""

import logging

log = logging.getLogger(__name__)

__virtualname__ = "incus"


def __virtual__():
    # To force a module not to load return something like:
    #   return (False, "The incus state module is not implemented yet")

    # Replace this with your own logic
    if "incus.example_function" not in __salt__:
        return False, "The 'incus' execution module is not available"
    return __virtualname__


def exampled(name):
    """
    This example function should be replaced
    """
    ret = {"name": name, "changes": {}, "result": False, "comment": ""}
    value = __salt__["incus.example_function"](name)
    if value == name:
        ret["result"] = True
        ret["comment"] = f"The 'incus.example_function' returned: '{value}'"
    return ret
