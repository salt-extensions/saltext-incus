import pytest

pytestmark = [
    pytest.mark.requires_salt_modules("incus.example_function"),
]


@pytest.fixture
def incus(modules):
    return modules.incus


def test_replace_this_this_with_something_meaningful(incus):
    echo_str = "Echoed!"
    res = incus.example_function(echo_str)
    assert res == echo_str
