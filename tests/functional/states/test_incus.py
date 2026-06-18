import pytest

pytestmark = [
    pytest.mark.requires_salt_states("incus.exampled"),
]


@pytest.fixture
def incus(states):
    return states.incus


def test_replace_this_this_with_something_meaningful(incus):
    echo_str = "Echoed!"
    ret = incus.exampled(echo_str)
    assert ret.result
    assert not ret.changes
    assert echo_str in ret.comment
