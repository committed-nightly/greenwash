# greenwash

Your CI is green. This tells you how many times it had to try.

`greenwash` expands GitHub Actions workflow runs into their individual
attempts and reports two things the Actions UI will not show you: runs that
only passed because somebody re-ran them, and your true first-attempt pass
rate.

It is for anyone who has ever hit **Re-run failed jobs**, watched it go
green, and moved on.

## The problem

The Actions list, `gh run list`, and `GET /actions/runs` all collapse a run
to its **latest attempt**. Nothing in the output admits an earlier one
existed. So a run that failed, sat broken for seven hours, and was re-run
successfully looks like this:

```console
$ gh run list --repo committed-nightly/ops --limit 100 \
    --json databaseId,conclusion --jq '.[] | select(.databaseId==33819369404)'
{"conclusion":"success","databaseId":33819369404}

$ gh run list --repo committed-nightly/ops --limit 100 --status failure \
    --json databaseId --jq '.[].databaseId'
33769793117
```

A clean success, and a failure filter over the same window that returns a
different run and not this one — because the field that filter reads says
`success`. The failure is at
`/attempts/1`, reachable only through `previous_attempt_url`, which you only
follow if you already suspect there is something to find.

`gh run list --json attempt` will tell you the *number* of attempts, so the
count is not hidden — but no conclusion, no duration and no link for any
attempt but the last, and no way to filter on "something failed here".

## Install

```bash
pip install git+https://github.com/committed-nightly/greenwash
```

Python 3.10+. No dependencies. It uses the [GitHub CLI](https://cli.github.com)
as its transport, so `gh` must be installed and authenticated — which also
means `GH_HOST` and `GH_TOKEN` work exactly as they do everywhere else.

## Usage

```console
$ greenwash --repo committed-nightly/ops --limit 60
33819369404  Richmond · schedule · main
  attempt 1  failure            7m 45s  https://github.com/committed-nightly/ops/actions/runs/33819369404/attempts/1
  attempt 2  success           18m 28s
  -> green on retry, 1 hidden failure, started 2026-09-03 23:51 UTC

33769793117  Richmond · workflow_dispatch · main
  attempt 1  failure            3m 47s  https://github.com/committed-nightly/ops/actions/runs/33769793117/attempts/1
  attempt 2  failure            3m 46s
  -> still failing, started 2026-09-03 14:56 UTC

59 completed runs checked · 1 still running, not counted
  1 green on retry (1 hidden failure)
  1 still failing after a re-run

first-attempt pass rate   96.6%  (57/59)
eventual pass rate        98.3%  (58/59)
```

That is a real run against a real repository. The first entry is the
incident this tool was written for.

Only re-run runs are listed by default, because everything else has no
hidden history worth printing. `--all` lists every run.

With no `--repo`, it reads `GH_REPO`, then the `origin` remote of the
current directory.

```
--repo OWNER/REPO   default: GH_REPO, then the origin remote
--workflow NAME     display name ("Richmond") or filename ("night-shift.yaml")
--branch BRANCH
--event EVENT       push, pull_request, schedule, ...
--since DATE        only runs created on or after this date
--limit N           runs to examine (default 100)
--all               list every run, not only the re-run ones
--json
--gh PATH           the gh executable to use
```

Exit codes: **0** nothing was hidden, **1** at least one run went green on a
retry, **2** the check could not run. So it works as a gate:

```yaml
- run: greenwash --since $(date -d '7 days ago' +%F) --workflow CI
```

## What counts as hidden

An earlier attempt is a **hidden failure** if it concluded `failure`,
`timed_out` or `startup_failure`. Earlier attempts that were `cancelled`,
`skipped`, `neutral`, `stale` or `action_required` are printed but are not
findings, because they are usually someone stopping a run on purpose.

That distinction is not theoretical. The only re-run in the last 300 runs of
`cli/cli` is a pull request whose first attempt was `action_required` — the
approval gate on first-time contributors — and then passed. Treating every
non-success attempt as a failure would make that a finding, and the approval
gate is the single most common reason an open-source run gets a second
attempt.

The same care applies to the rates. Only conclusions that are a verdict on
the code (`success`, `failure`, `timed_out`, `startup_failure`) go in the
denominator. Runs still in progress, and runs that ended `cancelled` or
`action_required`, are counted and named separately:

```console
$ greenwash --repo grafana/grafana --limit 300
300 completed runs checked
  0 green on retry (0 hidden failures)
  211 reached no pass/fail conclusion (action_required 189, skipped 22)

first-attempt pass rate   98.9%  (88/89)
eventual pass rate        98.9%  (88/89)
```

Counting those 211 as not-passes reports a 29% pass rate for a repository
whose real one is 99%.

## How rare is this, honestly

Rare, in healthy repositories. Across roughly 1,000 recent runs of
`grafana/grafana`, `home-assistant/core`, `pallets/flask` and `cli/cli`
there were **no** greenwashed runs at all, and exactly one re-run of any
kind. Maintainers who could re-run mostly do not.

So do not install this expecting a stream of findings. The two things it is
actually good for:

1. **The rate lines, every time you run it.** A first-attempt pass rate is
   not obtainable from the Actions UI at any window size, and the gap
   between it and the eventual rate is the honest measure of how much your
   CI is being re-run into submission.
2. **The one run that matters.** In this org it fired once in sixty runs,
   and that one had left a public repository half-built for seven hours
   while every dashboard read green.

## Cost

One API request per page of 100 runs, plus one per *earlier* attempt. The
run object returned by the list endpoint is already the latest attempt, so a
repository where nothing has ever been re-run costs exactly one request per
page and nothing else.

## Known limits

- **Attempt durations are `updated_at - run_started_at`**, which is when
  GitHub last touched the record rather than the exact moment the last job
  finished. It matches what the Actions UI shows. Exact per-job timings
  would need another request per attempt.
- **Only workflow runs, not jobs.** greenwash will not tell you *which* job
  failed on attempt 1 — follow the printed attempt URL for that.
- **GitHub expires old attempt data.** An attempt that 404s is reported as
  `unavailable` and left out of the first-attempt rate rather than guessed
  at.
- **A repository with no workflow runs at all exits 2**, not 0. A check that
  never ran must not look like a pass. A filter that legitimately matches
  nothing (`--since` in the future) is a clean 0.

## Development

```bash
git clone https://github.com/committed-nightly/greenwash && cd greenwash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

`tests/test_premise.py` asserts against GitHub's real captured payloads
rather than against greenwash, so if the API ever starts surfacing earlier
attempts in the run object, the premise fails rather than the tool quietly
finding nothing.

## Licence

MIT.
