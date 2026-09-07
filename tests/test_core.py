import pytest
from conftest import make_run

from greenwash import core


def build(latest, earlier=None):
    return core.build_run(latest, earlier or {})


def test_a_single_attempt_run_hides_nothing():
    run = build(make_run(attempt=1))
    assert run.verdict == core.SINGLE
    assert run.hidden_failures == ()
    assert len(run.attempts) == 1


def test_failure_then_success_is_greenwashed():
    run = build(
        make_run(attempt=2, conclusion="success"),
        {1: make_run(attempt=1, conclusion="failure")},
    )
    assert run.verdict == core.GREENWASHED
    assert [a.number for a in run.hidden_failures] == [1]


def test_two_failures_then_success_counts_both():
    run = build(
        make_run(attempt=3, conclusion="success"),
        {
            1: make_run(attempt=1, conclusion="failure"),
            2: make_run(attempt=2, conclusion="timed_out"),
        },
    )
    assert run.verdict == core.GREENWASHED
    assert len(run.hidden_failures) == 2


def test_re_run_that_still_fails_is_not_greenwashed():
    run = build(
        make_run(attempt=2, conclusion="failure"),
        {1: make_run(attempt=1, conclusion="failure")},
    )
    assert run.verdict == core.STILL_FAILING
    # It is already visible to `gh run list --status failure`, so it is not
    # a finding — but its first attempt still counts against the rate.
    assert core.summarise([run]).greenwashed == 0
    assert core.summarise([run]).first_pass == 0


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "neutral", "stale", "action_required"])
def test_a_cancelled_earlier_attempt_is_reported_but_not_a_failure(conclusion):
    """Somebody stopping a run on purpose is not CI hiding a failure."""
    run = build(
        make_run(attempt=2, conclusion="success"),
        {1: make_run(attempt=1, conclusion=conclusion)},
    )
    assert run.verdict == core.RETRIED
    assert run.hidden_failures == ()
    assert core.summarise([run]).greenwashed == 0


def test_startup_failure_counts_as_a_hidden_failure():
    run = build(
        make_run(attempt=2, conclusion="success"),
        {1: make_run(attempt=1, conclusion="startup_failure")},
    )
    assert run.verdict == core.GREENWASHED


def test_an_unfinished_run_has_no_verdict_and_no_vote():
    run = build(make_run(attempt=1, status="in_progress", conclusion=None))
    assert run.verdict == core.RUNNING

    summary = core.summarise([run, build(make_run(2))])
    assert summary.running == 1
    assert summary.checked == 1
    assert summary.eventual_rate == 1.0


def test_an_unavailable_attempt_does_not_crash_or_guess():
    run = build(make_run(attempt=2, conclusion="success"), {1: None})
    assert run.first.available is False
    assert run.first.duration is None
    # Unknown, so not counted as a hidden failure and not counted in the
    # first-attempt rate either.
    assert run.verdict == core.RETRIED
    summary = core.summarise([run])
    assert summary.checked == 1
    assert summary.first_known == 0
    assert summary.first_rate is None


def test_earlier_attempts_get_their_own_url_the_latest_does_not():
    run = build(
        make_run(7, attempt=2, conclusion="success"),
        {1: make_run(7, attempt=1, conclusion="failure")},
    )
    base = "https://github.com/o/r/actions/runs/7"
    assert run.first.url == f"{base}/attempts/1"
    assert run.latest.url == base


def test_duration_uses_run_started_at():
    run = build(
        make_run(started="2026-01-01T00:00:00Z", updated="2026-01-01T00:02:30Z"),
    )
    assert run.latest.duration == 150.0


def test_duration_is_none_when_the_run_has_not_finished():
    run = build(make_run(status="in_progress", conclusion=None, updated=None))
    assert run.latest.duration is None


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_a_queued_run_has_no_duration_even_though_github_sets_updated_at(status):
    """GitHub sets updated_at == run_started_at on a run that has not begun.

    Subtracting them gives a confident 0s for a run that has done nothing.
    Real payload shape, from this repository's first pull request.
    """
    run = build(
        make_run(
            status=status,
            conclusion=None,
            started="2026-09-06T23:57:33Z",
            updated="2026-09-06T23:57:33Z",
        )
    )
    assert run.latest.duration is None
    assert run.verdict == core.RUNNING


def test_rates_over_a_mixed_window():
    runs = [
        build(make_run(1)),  # clean pass
        build(make_run(2)),  # clean pass
        build(  # failed first, passed second
            make_run(3, attempt=2, conclusion="success"),
            {1: make_run(3, attempt=1, conclusion="failure")},
        ),
        build(make_run(4, conclusion="failure")),  # plain failure
    ]
    s = core.summarise(runs)
    assert (s.checked, s.multi_attempt, s.greenwashed, s.hidden_failures) == (4, 1, 1, 1)
    assert (s.first_pass, s.first_known) == (2, 4)
    assert s.first_rate == 0.5
    assert s.eventual_pass == 3
    assert s.eventual_rate == 0.75


def test_a_run_awaiting_approval_is_not_a_failed_run():
    """The grafana case: 189 of 300 runs were `action_required`.

    Counting those as not-passes reported a 29% pass rate for a repository
    whose real one is 99%. They are not a verdict on anything, so they stay
    out of the denominator and get counted separately instead.
    """
    runs = [build(make_run(i, conclusion="action_required")) for i in range(9)]
    runs.append(build(make_run(99, conclusion="success")))

    s = core.summarise(runs)
    assert s.checked == 10
    assert s.undecided == 9
    assert s.decided == 1
    assert s.eventual_rate == 1.0
    assert s.first_rate == 1.0


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "neutral", "stale", "action_required"])
def test_no_undecided_conclusion_drags_the_rate_down(conclusion):
    s = core.summarise([build(make_run(1, conclusion=conclusion)), build(make_run(2))])
    assert s.eventual_rate == 1.0
    assert s.undecided == 1


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
def test_every_kind_of_failure_does_count_against_the_rate(conclusion):
    s = core.summarise([build(make_run(1, conclusion=conclusion)), build(make_run(2))])
    assert s.eventual_rate == 0.5
    assert s.undecided == 0


def test_a_re_run_that_ends_cancelled_is_inconclusive_not_failing():
    run = build(
        make_run(attempt=2, conclusion="cancelled"),
        {1: make_run(attempt=1, conclusion="failure")},
    )
    assert run.verdict == core.INCONCLUSIVE
    s = core.summarise([run])
    assert (s.still_failing, s.greenwashed, s.inconclusive) == (0, 0, 1)
    # Nothing looks green, so it is not a finding — but the failure is still
    # recorded for anyone reading the JSON.
    assert len(run.hidden_failures) == 1


def test_an_undecided_first_attempt_leaves_the_two_denominators_different():
    """cli/cli 33951649093: attempt 1 `action_required`, attempt 2 success."""
    run = build(
        make_run(attempt=2, conclusion="success"),
        {1: make_run(attempt=1, conclusion="action_required")},
    )
    s = core.summarise([run])
    assert run.verdict == core.RETRIED
    assert (s.first_known, s.decided) == (0, 1)
    assert s.first_rate is None
    assert s.eventual_rate == 1.0


def test_the_conclusion_breakdown_is_counted_and_ordered_by_frequency():
    runs = [build(make_run(i, conclusion="action_required")) for i in range(3)]
    runs += [build(make_run(10 + i, conclusion="success")) for i in range(5)]
    runs.append(build(make_run(99, conclusion="failure")))
    assert core.summarise(runs).conclusions == (
        ("success", 5),
        ("action_required", 3),
        ("failure", 1),
    )


def test_summarise_of_nothing_reports_no_rate_rather_than_zero():
    s = core.summarise([])
    assert s.checked == 0
    assert s.first_rate is None
    assert s.eventual_rate is None


def test_parse_time_handles_z_and_offsets_and_junk():
    assert core.parse_time("2026-09-03T23:51:19Z").hour == 23
    assert core.parse_time("2026-09-03T23:51:19+00:00").hour == 23
    assert core.parse_time("2026-09-03T23:51:19").tzinfo is not None
    assert core.parse_time(None) is None
    assert core.parse_time("") is None
    assert core.parse_time("not a date") is None


def test_missing_run_attempt_is_treated_as_one():
    payload = make_run()
    del payload["run_attempt"]
    assert len(build(payload).attempts) == 1
