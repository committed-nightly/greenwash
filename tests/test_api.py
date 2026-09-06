"""The transport and the endpoint wrappers.

The transport tests drive a real subprocess. That is deliberate: the part
worth testing is that an error body arriving on *stdout* with a non-zero
exit is read as an HTTP status, and a fake object with the right shape would
not prove anything about that.
"""

import os
import stat

import pytest
from conftest import FakeTransport, make_run

from greenwash import api


def fake_gh(tmp_path, *, stdout="", stderr="", code=0):
    """Write a script that behaves like `gh api` for one canned response."""
    (tmp_path / "out").write_text(stdout)
    (tmp_path / "err").write_text(stderr)
    script = tmp_path / "fake-gh"
    script.write_text(
        "#!/bin/sh\n"
        f'cat "{tmp_path}/out"\n'
        f'cat "{tmp_path}/err" >&2\n'
        f"exit {code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


# --- GhTransport -----------------------------------------------------------


def test_a_successful_call_returns_the_decoded_body(tmp_path):
    gh = fake_gh(tmp_path, stdout='{"total_count": 3}')
    assert api.GhTransport(gh).get("repos/o/r/actions/runs") == {"total_count": 3}


def test_a_404_body_on_stdout_becomes_a_404_status(tmp_path):
    # Verbatim from `gh api` 2.98 against a missing attempt.
    gh = fake_gh(
        tmp_path,
        stdout='{"message":"Not Found","documentation_url":"https://docs.github.com/","status":"404"}',
        stderr="gh: Not Found (HTTP 404)\n",
        code=1,
    )
    with pytest.raises(api.ApiError) as caught:
        api.GhTransport(gh).get("repos/o/r/actions/runs/1/attempts/9")
    assert caught.value.status == 404
    assert caught.value.message == "Not Found"


def test_an_auth_failure_is_not_mistaken_for_missing_data(tmp_path):
    gh = fake_gh(
        tmp_path,
        stdout='{"message":"Bad credentials","status":"401"}',
        stderr="gh: Bad credentials (HTTP 401)\n",
        code=1,
    )
    with pytest.raises(api.ApiError) as caught:
        api.GhTransport(gh).get("repos/o/r/actions/runs")
    assert caught.value.status == 401
    assert "401" in str(caught.value)


def test_a_failure_with_no_json_falls_back_to_stderr(tmp_path):
    gh = fake_gh(tmp_path, stdout="", stderr="gh: could not resolve host\n", code=1)
    with pytest.raises(api.ApiError) as caught:
        api.GhTransport(gh).get("repos/o/r/actions/runs")
    assert caught.value.status is None
    assert "could not resolve host" in str(caught.value)


def test_success_with_output_that_is_not_json_is_an_error_not_a_crash(tmp_path):
    gh = fake_gh(tmp_path, stdout="<html>a proxy said no</html>", code=0)
    with pytest.raises(api.ApiError) as caught:
        api.GhTransport(gh).get("repos/o/r/actions/runs")
    assert "not JSON" in str(caught.value)


def test_a_missing_gh_says_so_instead_of_raising_filenotfound(tmp_path):
    with pytest.raises(api.ApiError) as caught:
        api.GhTransport(str(tmp_path / "nope")).get("x")
    assert "not on PATH" in str(caught.value)


# --- resolve_workflow ------------------------------------------------------

WORKFLOWS = {
    "repos/o/r/actions/workflows?per_page=100": {
        "workflows": [
            {"name": "Richmond", "path": ".github/workflows/night-shift.yaml"},
            {"name": "CI", "path": ".github/workflows/ci.yml"},
        ]
    }
}


@pytest.mark.parametrize("given", ["ci.yml", "night-shift.yaml"])
def test_a_filename_is_passed_straight_through_without_a_request(given):
    transport = FakeTransport()
    assert api.resolve_workflow(transport, "o/r", given) == given
    assert transport.calls == []


def test_a_display_name_is_resolved_to_its_filename():
    transport = FakeTransport(WORKFLOWS)
    assert api.resolve_workflow(transport, "o/r", "Richmond") == "night-shift.yaml"


def test_name_matching_ignores_case():
    assert api.resolve_workflow(FakeTransport(WORKFLOWS), "o/r", "richmond") == "night-shift.yaml"


def test_an_unknown_workflow_lists_the_real_ones():
    with pytest.raises(api.ApiError) as caught:
        api.resolve_workflow(FakeTransport(WORKFLOWS), "o/r", "Richard")
    assert "CI, Richmond" in str(caught.value)


# --- list_runs -------------------------------------------------------------


def test_the_query_string_is_built_exactly():
    path = "repos/o/r/actions/runs?per_page=5&page=1&branch=main&event=schedule&created=%3E%3D2026-09-01"
    transport = FakeTransport({path: {"workflow_runs": []}})
    api.list_runs(transport, "o/r", branch="main", event="schedule", since="2026-09-01", limit=5)
    assert transport.calls == [path]


def test_a_workflow_uses_the_workflow_endpoint():
    transport = FakeTransport({"repos/o/r/actions/workflows/ci.yml/runs": {"workflow_runs": []}})
    api.list_runs(transport, "o/r", workflow="ci.yml", limit=1)
    assert transport.calls[0].startswith("repos/o/r/actions/workflows/ci.yml/runs?")


def test_pagination_continues_until_a_short_page():
    full = {"workflow_runs": [make_run(i) for i in range(100)]}
    short = {"workflow_runs": [make_run(999)]}
    transport = FakeTransport(
        {
            "repos/o/r/actions/runs?per_page=100&page=1": full,
            "repos/o/r/actions/runs?per_page=100&page=2": full,
            "repos/o/r/actions/runs?per_page=50&page=3": short,
        }
    )
    runs = api.list_runs(transport, "o/r", limit=250)
    assert len(runs) == 201
    assert len(transport.calls) == 3


def test_a_limit_below_a_page_asks_for_only_that_many():
    transport = FakeTransport({"repos/o/r/actions/runs": {"workflow_runs": [make_run(i) for i in range(7)]}})
    assert len(api.list_runs(transport, "o/r", limit=3)) == 3
    assert "per_page=3" in transport.calls[0]


def test_an_empty_first_page_stops_immediately():
    transport = FakeTransport({"repos/o/r/actions/runs": {"workflow_runs": []}})
    assert api.list_runs(transport, "o/r", limit=100) == []
    assert len(transport.calls) == 1


# --- fetch_earlier_attempts ------------------------------------------------


def test_a_run_that_was_never_re_run_costs_no_requests():
    transport = FakeTransport()
    assert api.fetch_earlier_attempts(transport, "o/r", make_run(5, attempt=1)) == {}
    assert transport.calls == []


def test_only_the_attempts_before_the_last_are_fetched():
    transport = FakeTransport(
        {
            "repos/o/r/actions/runs/5/attempts/1": make_run(5, attempt=1),
            "repos/o/r/actions/runs/5/attempts/2": make_run(5, attempt=2),
        }
    )
    got = api.fetch_earlier_attempts(transport, "o/r", make_run(5, attempt=3))
    assert sorted(got) == [1, 2]
    assert "repos/o/r/actions/runs/5/attempts/3" not in transport.calls


def test_an_expired_attempt_becomes_none_rather_than_an_error():
    transport = FakeTransport(
        {"repos/o/r/actions/runs/5/attempts/1": api.ApiError("Not Found", status=404)}
    )
    assert api.fetch_earlier_attempts(transport, "o/r", make_run(5, attempt=2)) == {1: None}


def test_an_auth_error_on_an_attempt_is_not_swallowed():
    transport = FakeTransport(
        {"repos/o/r/actions/runs/5/attempts/1": api.ApiError("Bad credentials", status=401)}
    )
    with pytest.raises(api.ApiError):
        api.fetch_earlier_attempts(transport, "o/r", make_run(5, attempt=2))


def test_api_error_str_carries_status_and_path():
    text = str(api.ApiError("Not Found", status=404, path="repos/o/r"))
    assert "Not Found" in text and "404" in text and "repos/o/r" in text
