"""Assertions about GitHub's payloads, not about greenwash.

Every claim the README makes rests on how the Actions API reports a re-run.
These tests read real captured payloads from `committed-nightly/ops` and
check the claims directly. If GitHub ever starts surfacing earlier attempts
in the run object, this file goes red and the pitch needs rewriting — which
is the point of testing the premise rather than only the tool.

Provenance of the fixtures is in tests/fixtures/README.md.
"""

from conftest import fixture

from greenwash import core

# The night shift of 2026-09-03: attempt 1 died seven minutes in, leaving a
# public repository with no pull request and no ledger line; attempt 2 ran
# seven hours later and succeeded.
DEADOWNERS = 33819369404

# The same shift dispatched on 2026-09-03 at 14:56: re-run in place, failed
# a second time. Two attempts, no success anywhere.
TWICE_FAILED = 33769793117


def test_the_run_object_reports_only_the_latest_attempt():
    run = fixture(f"run-{DEADOWNERS}")
    assert run["run_attempt"] == 2
    assert run["conclusion"] == "success"
    # Nothing on the run object says an attempt failed.
    assert "failure" not in {run["conclusion"], run["status"]}


def test_the_earlier_attempt_failed():
    first = fixture(f"run-{DEADOWNERS}-attempt-1")
    assert first["run_attempt"] == 1
    assert first["conclusion"] == "failure"


def test_a_failure_filter_over_the_list_endpoint_misses_it():
    """This is the whole pitch in one assertion.

    `gh run list --status failure` filters on the field below. The run whose
    first attempt failed is not in the result.
    """
    runs = fixture("runs-list")["workflow_runs"]
    failing = [r["id"] for r in runs if r["conclusion"] == "failure"]

    assert DEADOWNERS in {r["id"] for r in runs}, "the run is in the list at all"
    assert DEADOWNERS not in failing, "but a failure filter does not return it"
    assert TWICE_FAILED in failing, "while a run that never passed is returned"


def test_the_run_object_is_its_own_latest_attempt():
    """Why greenwash makes no request for the last attempt.

    If this stops holding, fetch_earlier_attempts() has to fetch the latest
    one too and the request count goes up by one per run.
    """
    for run_id in (DEADOWNERS, TWICE_FAILED):
        run = fixture(f"run-{run_id}")
        latest = fixture(f"run-{run_id}-attempt-{run['run_attempt']}")
        for field in ("status", "conclusion", "run_started_at", "updated_at"):
            assert run[field] == latest[field], f"{run_id}: {field}"


def test_created_at_is_the_trap_run_started_at_is_the_answer():
    """On a re-run these two differ, and only one of them gives a duration.

    The run object keeps the *original* creation time while `run_started_at`
    moves forward to the latest attempt, so `updated_at - created_at` on a
    re-run measures the gap between somebody's first try and their last one.
    Here that is seven hours for an eighteen minute run.
    """
    run = fixture(f"run-{DEADOWNERS}")
    assert run["created_at"] != run["run_started_at"]

    honest = core.parse_time(run["updated_at"]) - core.parse_time(run["run_started_at"])
    naive = core.parse_time(run["updated_at"]) - core.parse_time(run["created_at"])

    assert honest.total_seconds() == 18 * 60 + 28
    assert naive.total_seconds() > 7 * 3600


def test_attempt_durations_of_the_real_incident():
    run = core.build_run(
        fixture(f"run-{DEADOWNERS}"),
        {1: fixture(f"run-{DEADOWNERS}-attempt-1")},
    )
    assert run.verdict == core.GREENWASHED
    assert [a.duration for a in run.attempts] == [7 * 60 + 45, 18 * 60 + 28]
