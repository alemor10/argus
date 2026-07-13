import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite golden files with current output instead of comparing.",
    )


@pytest.fixture
def update_golden(request) -> bool:
    return request.config.getoption("--update-golden")
