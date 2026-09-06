import io
import json
import subprocess

import pytest
from conftest import FakeTransport, fixture, make_run

from greenwash import cli

RUNS = "repos/o/r/actions/runs"


def invoke(argv, responses):
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(argv, out=out, err=err, transport=FakeTransport(responses))
    return code, out.getvalue(), err.getvalue()


def listing(*runs):
    return {RUNS: {"workflow_runs": list(runs)}}


# --- working out the repository -------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/committed-nightly/ops.git", "committed-nightly/ops"),
        ("https://github.com/committed-nightly/ops", "committed-nightly/ops"),
        ("git@github.com:committed-nightly/ops.git", "committed-nightly/ops"),
        ("ssh://git@github.com/committed-nightly/ops.git", "committed-nightly/ops"),
        ("https://github.com/committed-nightly/ops/", "committed-nightly/ops"),
        ("git@ghe.internal:team/thing.git\n", "team/thing"),
    ],
)
def test_remote_urls_of_every_shape(url, expected):
    assert cli.parse_remote(url) == expected


def test_a_credentialed_remote_does_not_leak_the_token_as_the_owner():
    url = "https://x-access-token:ghs_SECRETSECRET@github.com/committed-nightly/ops.git"
    assert cli.parse_remote(url) == "committed-nightly/ops"


def test_a_url_with_nothing_in_it_is_none():
    assert cli.parse_remote("") is None
    assert cli.parse_remote("origin") is None


def test_gh_repo_wins_over_the_git_remote(monkeypatch):
    monkeypatch.setenv("GH_REPO", "someone/else")
    assert cli.infer_repo() == "someone/else"


def test_a_directory_with_no_origin_says_to_pass_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    with pytest.raises(cli.Problem) as caught:
        cli.infer_repo(cwd=str(tmp_path))
    assert "--repo" in str(caught.value)


def test_a_real_origin_is_read_back(tmp_path, monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:committed-nightly/greenwash.git"],
        cwd=tmp_path,
        check=True,
    )
    assert cli.infer_repo(cwd=str(tmp_path)) == "committed-nightly/greenwash"


# --- exit codes ------------------------------------------------------------


def test_a_clean_window_exits_zero():
    code, out, _ = invoke(["--repo", "o/r"], listing(make_run(1), make_run(2)))
    assert code == cli.EXIT_CLEAN
    assert "2 completed runs checked" in out


def test_one_greenwashed_run_exits_one():
    responses = listing(make_run(9, attempt=2, conclusion="success"))
    responses["repos/o/r/actions/runs/9/attempts/1"] = make_run(9, attempt=1, conclusion="failure")
    code, out, _ = invoke(["--repo", "o/r"], responses)
    assert code == cli.EXIT_FINDINGS
    assert "green on retry" in out
    assert "1 hidden failure" in out


def test_a_plainly_failing_run_is_not_a_finding_here():
    """`gh run list --status failure` already shows you this one."""
    code, out, _ = invoke(["--repo", "o/r"], listing(make_run(1, conclusion="failure")))
    assert code == cli.EXIT_CLEAN


def test_a_repository_that_has_never_run_a_workflow_is_an_error_not_a_pass():
    code, _, err = invoke(["--repo", "o/r"], listing())
    assert code == cli.EXIT_ERROR
    assert "nothing was checked" in err


def test_but_a_filter_that_matches_nothing_is_a_legitimate_empty_window():
    code, out, _ = invoke(["--repo", "o/r", "--since", "2099-01-01"], listing())
    assert code == cli.EXIT_CLEAN
    assert "0 runs matched" in out


def test_a_bad_repo_argument_is_rejected_before_any_request():
    code, _, err = invoke(["--repo", "not-a-repo"], {})
    assert code == cli.EXIT_ERROR
    assert "OWNER/REPO" in err


def test_a_zero_limit_is_rejected():
    code, _, err = invoke(["--repo", "o/r", "--limit", "0"], {})
    assert code == cli.EXIT_ERROR
    assert "--limit" in err


def test_an_api_failure_exits_two_with_the_reason():
    from greenwash import api

    code, _, err = invoke(["--repo", "o/r"], {RUNS: api.ApiError("Bad credentials", status=401)})
    assert code == cli.EXIT_ERROR
    assert "Bad credentials" in err and "401" in err


# --- what gets printed -----------------------------------------------------


def test_only_re_run_runs_are_listed_by_default():
    responses = listing(make_run(9, attempt=2, conclusion="success"), make_run(4))
    responses["repos/o/r/actions/runs/9/attempts/1"] = make_run(9, attempt=1, conclusion="failure")
    _, out, _ = invoke(["--repo", "o/r"], responses)
    assert "9  CI" in out
    assert "4  CI" not in out


def test_all_lists_the_quiet_ones_too():
    responses = listing(make_run(9, attempt=2, conclusion="success"), make_run(4))
    responses["repos/o/r/actions/runs/9/attempts/1"] = make_run(9, attempt=1, conclusion="failure")
    _, out, _ = invoke(["--repo", "o/r", "--all"], responses)
    assert "4  CI" in out


def test_earlier_attempts_print_the_url_you_cannot_otherwise_reach():
    responses = listing(make_run(9, attempt=2, conclusion="success"))
    responses["repos/o/r/actions/runs/9/attempts/1"] = make_run(9, attempt=1, conclusion="failure")
    _, out, _ = invoke(["--repo", "o/r"], responses)
    assert "https://github.com/o/r/actions/runs/9/attempts/1" in out
    # The latest attempt's line carries no URL; the run's own page is that.
    latest_line = [ln for ln in out.splitlines() if ln.strip().startswith("attempt 2")][0]
    assert "http" not in latest_line


def test_an_expired_attempt_is_named_as_such():
    from greenwash import api

    responses = listing(make_run(9, attempt=2, conclusion="success"))
    responses["repos/o/r/actions/runs/9/attempts/1"] = api.ApiError("Not Found", status=404)
    code, out, _ = invoke(["--repo", "o/r"], responses)
    assert "unavailable" in out
    assert code == cli.EXIT_CLEAN


def test_an_unfinished_run_is_excluded_from_the_rates_and_said_so():
    responses = listing(make_run(1, status="in_progress", conclusion=None), make_run(2))
    _, out, _ = invoke(["--repo", "o/r"], responses)
    assert "1 completed run checked" in out
    assert "1 still running, not counted" in out


def test_undecided_runs_are_named_with_their_reasons():
    responses = listing(
        make_run(1, conclusion="action_required"),
        make_run(2, conclusion="action_required"),
        make_run(3, conclusion="skipped"),
        make_run(4, conclusion="success"),
    )
    code, out, _ = invoke(["--repo", "o/r"], responses)
    assert code == cli.EXIT_CLEAN
    assert "3 reached no pass/fail conclusion (action_required 2, skipped 1)" in out
    assert "eventual pass rate       100.0%  (1/1)" in out


def test_a_window_of_nothing_but_undecided_runs_reports_no_rate():
    _, out, _ = invoke(["--repo", "o/r"], listing(make_run(1, conclusion="cancelled")))
    assert "eventual pass rate       n/a" in out


@pytest.mark.parametrize(
    "seconds,text",
    [(0, "0s"), (45, "45s"), (465, "7m 45s"), (1108, "18m 28s"), (3600, "1h 00m"), (5400, "1h 30m")],
)
def test_durations_read_the_way_a_person_would_write_them(seconds, text):
    assert cli.format_duration(seconds) == text


def test_an_unknown_duration_is_a_dash():
    assert cli.format_duration(None) == "-"


def test_a_rate_with_no_denominator_is_not_zero_percent():
    assert cli.format_rate(0, 0) == "n/a"


# --- json ------------------------------------------------------------------


def test_json_carries_the_verdict_and_the_rates():
    responses = listing(make_run(9, attempt=2, conclusion="success"), make_run(4))
    responses["repos/o/r/actions/runs/9/attempts/1"] = make_run(9, attempt=1, conclusion="failure")
    code, out, _ = invoke(["--repo", "o/r", "--json"], responses)
    payload = json.loads(out)

    assert code == cli.EXIT_FINDINGS
    assert payload["repo"] == "o/r"
    assert payload["summary"]["greenwashed"] == 1
    assert payload["summary"]["first_attempt_pass_rate"] == 0.5
    assert payload["summary"]["eventual_pass_rate"] == 1.0

    nine = [r for r in payload["runs"] if r["id"] == 9][0]
    assert nine["verdict"] == "green-on-retry"
    assert nine["attempts"][0]["conclusion"] == "failure"
    assert nine["attempts"][0]["duration_seconds"] == 600.0


def test_json_lists_every_run_regardless_of_the_all_flag():
    """--all is a display choice; a machine-readable dump should be whole."""
    _, out, _ = invoke(["--repo", "o/r", "--json"], listing(make_run(1), make_run(2)))
    assert len(json.loads(out)["runs"]) == 2


# --- end to end on the real incident ---------------------------------------


def test_the_real_incident_from_end_to_end():
    run = fixture("run-33819369404")
    responses = {
        RUNS: {"workflow_runs": [run]},
        "repos/o/r/actions/runs/33819369404/attempts/1": fixture("run-33819369404-attempt-1"),
    }
    code, out, _ = invoke(["--repo", "o/r"], responses)
    assert code == cli.EXIT_FINDINGS
    assert "33819369404  Richmond · schedule · main" in out
    assert "7m 45s" in out
    assert "18m 28s" in out
    assert "green on retry, 1 hidden failure" in out
