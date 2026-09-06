import json
from pathlib import Path

import pytest

from greenwash import api

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeTransport:
    """A transport backed by a dict of path -> body.

    Lookup is by exact path first, then by the path with its query string
    stripped, so that most tests can stay readable while the one test that
    cares about query construction can pin it exactly.
    """

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        for key in (path, path.split("?", 1)[0]):
            if key in self.responses:
                value = self.responses[key]
                if isinstance(value, api.ApiError):
                    raise value
                return value
        raise AssertionError(
            f"unexpected request {path!r}; known paths: {sorted(self.responses)}"
        )


@pytest.fixture
def transport():
    return FakeTransport()


def make_run(
    run_id=1,
    *,
    attempt=1,
    conclusion="success",
    status="completed",
    started="2026-01-01T00:00:00Z",
    updated="2026-01-01T00:10:00Z",
    name="CI",
    event="push",
    branch="main",
):
    return {
        "id": run_id,
        "name": name,
        "event": event,
        "head_branch": branch,
        "display_title": "a commit",
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "created_at": started,
        "run_started_at": started,
        "updated_at": updated,
    }
