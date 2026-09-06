"""Turning API payloads into attempts, verdicts and a pass rate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

PASS = "success"

# An earlier attempt with one of these conclusions is a failure the latest
# attempt is now hiding. `cancelled`, `skipped`, `neutral`, `stale` and
# `action_required` are deliberately not in here: they are usually somebody
# stopping a run on purpose, and calling that a hidden failure would make the
# headline number meaningless within a week. They are still reported — see
# the `retried` verdict — just not counted as failures.
FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

# Only these conclusions are a verdict on the code. `cancelled`, `skipped`,
# `neutral`, `stale` and `action_required` are all "this run never got to
# say", and putting them in a pass rate makes the number describe the
# repository's queueing rather than its tests. On grafana/grafana, 189 of
# 300 recent runs are `action_required` — the approval gate on first-time
# contributors' pull requests — and counting those as failures reports a
# 29% pass rate for a repository whose real one is 99%.
DECISIVE_CONCLUSIONS = frozenset({PASS}) | FAILURE_CONCLUSIONS

# Verdicts.
SINGLE = "single"  # ran once; nothing is hidden
GREENWASHED = "green-on-retry"  # passed, but an earlier attempt failed
RETRIED = "retried"  # passed, earlier attempts ended some other way
STILL_FAILING = "still-failing"  # re-run and still failing
INCONCLUSIVE = "inconclusive"  # re-run, and it still reached no verdict
RUNNING = "running"  # not finished; no verdict yet


@dataclass(frozen=True)
class Attempt:
    number: int
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    ended_at: datetime | None
    url: str
    available: bool = True

    @property
    def passed(self) -> bool:
        return self.conclusion == PASS

    @property
    def failed(self) -> bool:
        return self.conclusion in FAILURE_CONCLUSIONS

    @property
    def decisive(self) -> bool:
        """Did this attempt actually say pass or fail?"""
        return self.conclusion in DECISIVE_CONCLUSIONS

    @property
    def duration(self) -> float | None:
        """Seconds this attempt took, or None if that can't be known.

        ``ended_at`` is the run's ``updated_at``, which is when GitHub last
        touched the record. For a completed attempt that is its completion,
        give or take whatever else may have written to the run since. Exact
        per-job timings would need a request per attempt to the jobs
        endpoint; this is close enough to read, and it is the same number
        the Actions UI shows.
        """
        if self.started_at is None or self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


@dataclass(frozen=True)
class Run:
    id: int
    workflow: str
    event: str
    branch: str
    title: str
    url: str
    attempts: tuple[Attempt, ...]

    @property
    def latest(self) -> Attempt:
        return self.attempts[-1]

    @property
    def first(self) -> Attempt:
        return self.attempts[0]

    @property
    def complete(self) -> bool:
        return self.latest.status == "completed"

    @property
    def earlier(self) -> tuple[Attempt, ...]:
        return self.attempts[:-1]

    @property
    def hidden_failures(self) -> tuple[Attempt, ...]:
        """Failed attempts you cannot see from the run's own page."""
        return tuple(a for a in self.earlier if a.failed)

    @property
    def verdict(self) -> str:
        if not self.complete:
            return RUNNING
        if len(self.attempts) == 1:
            return SINGLE
        if self.latest.passed:
            return GREENWASHED if self.hidden_failures else RETRIED
        if self.latest.failed:
            return STILL_FAILING
        return INCONCLUSIVE


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _attempt(number: int, payload: dict | None, run_url: str, is_latest: bool) -> Attempt:
    # The attempt-specific page is the only way to reach an earlier attempt in
    # the UI; the API's own `html_url` on an attempt payload points at the run.
    url = run_url if is_latest else f"{run_url}/attempts/{number}"
    if payload is None:
        return Attempt(number, None, None, None, None, url, available=False)
    return Attempt(
        number=number,
        status=payload.get("status"),
        conclusion=payload.get("conclusion"),
        # `run_started_at`, never `created_at`. On a re-run the run object
        # keeps the *original* creation time while `run_started_at` moves to
        # the latest attempt, so `updated_at - created_at` reports the wall
        # clock between the first try and the last one. For run 33819369404
        # that is 7 hours instead of 18 minutes.
        started_at=parse_time(payload.get("run_started_at")),
        ended_at=parse_time(payload.get("updated_at")),
        url=url,
    )


def build_run(run: dict, earlier: dict[int, dict | None] | None = None) -> Run:
    """Assemble a :class:`Run` from a run payload and its earlier attempts.

    The run payload doubles as the final attempt, so *earlier* only needs to
    carry attempts 1..N-1.
    """
    earlier = earlier or {}
    latest_number = int(run.get("run_attempt") or 1)
    run_url = str(run.get("html_url") or "")

    attempts = [
        _attempt(n, earlier.get(n), run_url, is_latest=False)
        for n in range(1, latest_number)
    ]
    attempts.append(_attempt(latest_number, run, run_url, is_latest=True))

    return Run(
        id=int(run["id"]),
        workflow=str(run.get("name") or ""),
        event=str(run.get("event") or ""),
        branch=str(run.get("head_branch") or ""),
        title=str(run.get("display_title") or ""),
        url=run_url,
        attempts=tuple(attempts),
    )


@dataclass(frozen=True)
class Summary:
    checked: int = 0  # completed runs looked at
    running: int = 0  # not finished
    undecided: int = 0  # finished, but reached no pass/fail conclusion
    multi_attempt: int = 0
    greenwashed: int = 0
    hidden_failures: int = 0
    still_failing: int = 0
    retried: int = 0
    inconclusive: int = 0
    first_pass: int = 0
    first_known: int = 0  # runs whose attempt 1 said pass or fail
    eventual_pass: int = 0
    decided: int = 0  # runs whose last attempt said pass or fail
    conclusions: tuple[tuple[str, int], ...] = ()

    @property
    def first_rate(self) -> float | None:
        return None if not self.first_known else self.first_pass / self.first_known

    @property
    def eventual_rate(self) -> float | None:
        return None if not self.decided else self.eventual_pass / self.decided


def summarise(runs) -> Summary:
    """Count runs, and be careful about which ones are allowed to vote.

    Two exclusions, for the same reason: a rate should describe the tests,
    not the queue. A run still executing has no conclusion yet, and a run
    that ended `cancelled` or `action_required` never got as far as having
    an opinion. Neither is a failure, so neither goes in the denominator.
    Both are counted and printed, so the numbers say how much was left out.
    """
    fields = dict(
        checked=0,
        running=0,
        undecided=0,
        multi_attempt=0,
        greenwashed=0,
        hidden_failures=0,
        still_failing=0,
        retried=0,
        inconclusive=0,
        first_pass=0,
        first_known=0,
        eventual_pass=0,
        decided=0,
    )
    seen: dict[str, int] = {}

    for run in runs:
        if not run.complete:
            fields["running"] += 1
            continue

        fields["checked"] += 1
        label = run.latest.conclusion or "unknown"
        seen[label] = seen.get(label, 0) + 1

        if len(run.attempts) > 1:
            fields["multi_attempt"] += 1
        verdict = run.verdict
        for name, key in (
            (GREENWASHED, "greenwashed"),
            (STILL_FAILING, "still_failing"),
            (RETRIED, "retried"),
            (INCONCLUSIVE, "inconclusive"),
        ):
            if verdict == name:
                fields[key] += 1
        if verdict == GREENWASHED:
            fields["hidden_failures"] += len(run.hidden_failures)

        # An unavailable or undecided first attempt is left out of the
        # first-attempt rate rather than guessed at, so the denominator
        # always says how much we actually know.
        if run.first.available and run.first.decisive:
            fields["first_known"] += 1
            if run.first.passed:
                fields["first_pass"] += 1
        if run.latest.decisive:
            fields["decided"] += 1
            if run.latest.passed:
                fields["eventual_pass"] += 1
        else:
            fields["undecided"] += 1

    ordered = tuple(sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])))
    return Summary(**fields, conclusions=ordered)
