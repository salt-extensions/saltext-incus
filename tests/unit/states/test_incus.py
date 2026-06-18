import pytest
import salt.modules.test as testmod

import incus.modules.incus_mod as incus_module
import incus.states.incus_mod as incus_state


@pytest.fixture
def configure_loader_modules():
    return {
        incus_module: {
            "__salt__": {
                "test.echo": testmod.echo,
            },
        },
        incus_state: {
            "__salt__": {
                "incus.example_function": incus_module.example_function,
            },
        },
    }


def test_replace_this_this_with_something_meaningful():
    echo_str = "Echoed!"
    expected = {
        "name": echo_str,
        "changes": {},
        "result": True,
        "comment": f"The 'incus.example_function' returned: '{echo_str}'",
    }
    assert incus_state.exampled(echo_str) == expected
