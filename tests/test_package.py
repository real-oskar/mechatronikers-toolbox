import mechatronikers
from mechatronikers.spec import SPEC_VERSION


def test_version() -> None:
    assert mechatronikers.__version__ == "0.1.0"
    assert SPEC_VERSION
