"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from . import api, core

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

_VERDICT_TEXT = {
    core.GREENWASHED: "green on retry",
    core.RETRIED: "passed on a re-run",
    core.STILL_FAILING: "still failing",
    core.RUNNING: "still running",
    core.SINGLE: "ran once",
}


class Problem(Exception):
    """Something the user can fix. Printed without a traceback, exit 2."""


# --- working out which repository -----------------------------------------

def parse_remote(url: str) -> str | None:
    """Pull OWNER/REPO out of a remote URL, whatever shape it is in.

    Splitting on the separators and taking the last two segments, rather
    than matching from the left: a URL can carry credentials
    (``https://x-access-token:TOKEN@github.com/o/r.git``) and a leftmost
    match happily reads the token as the owner. Whatever comes back from
    here, the raw URL is never echoed anywhere — see infer_repo.
    """
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = [p for p in re.split(r"[/:]", url) if p]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def infer_repo(cwd: str | None = None) -> str:
    """Work out OWNER/REPO from the environment, then from git.

    ``GH_REPO`` comes first because that is the CLI's own convention and CI
    jobs already set it. Otherwise the ``origin`` remote is parsed locally
    rather than asked of the API — it is a question about this directory,
    and answering it should not need a network round trip or a token.
    """
    from_env = os.environ.get("GH_REPO", "").strip()
    if from_env:
        return from_env

    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except FileNotFoundError:
        raise Problem("git is not on PATH, so --repo cannot be guessed. Pass it.") from None

    if proc.returncode != 0:
        raise Problem(
            "no git remote called 'origin' here, so there is nothing to guess from. "
            "Pass --repo OWNER/REPO."
        )

    # The remote URL itself is never put in the message: it can contain a
    # token, and error output ends up in CI logs.
    repo = parse_remote(proc.stdout)
    if repo is None:
        raise Problem("could not read a repository out of the origin remote. Pass --repo OWNER/REPO.")
    return repo


# --- formatting ------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_time(when: datetime | None) -> str:
    if when is None:
        return "unknown"
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_rate(passed: int, total: int) -> str:
    if not total:
        return "n/a"
    return f"{100.0 * passed / total:5.1f}%  ({passed}/{total})"


def render_run(run: core.Run, out) -> None:
    head = " · ".join(x for x in (run.workflow, run.event, run.branch) if x)
    print(f"{run.id}  {head}", file=out)
    for attempt in run.attempts:
        if not attempt.available:
            label = "unavailable"
            detail = "GitHub no longer has this attempt"
        else:
            label = attempt.conclusion or attempt.status or "unknown"
            detail = format_duration(attempt.duration)
        line = f"  attempt {attempt.number}  {label:<16}{detail:>9}"
        # Only earlier attempts get a URL: the latest one is what the run's
        # own page already shows you, and the earlier ones are the pages you
        # cannot otherwise reach.
        if attempt is not run.latest:
            line += f"  {attempt.url}"
        print(line.rstrip(), file=out)

    note = _VERDICT_TEXT.get(run.verdict, run.verdict)
    if run.verdict == core.GREENWASHED:
        n = len(run.hidden_failures)
        note += f", {n} hidden failure{'s' if n != 1 else ''}"
    print(f"  -> {note}, started {format_time(run.first.started_at)}", file=out)
    print(file=out)


def render(runs: list[core.Run], summary: core.Summary, show_all: bool, out) -> None:
    shown = runs if show_all else [r for r in runs if len(r.attempts) > 1]
    for run in shown:
        render_run(run, out)

    if summary.checked == 0 and summary.running == 0:
        print("0 runs matched.", file=out)
        return

    bits = [f"{summary.checked} completed run{'s' if summary.checked != 1 else ''} checked"]
    if summary.running:
        bits.append(f"{summary.running} still running, not counted")
    print(" · ".join(bits), file=out)

    print(
        f"  {summary.greenwashed} green on retry"
        f" ({summary.hidden_failures} hidden failure"
        f"{'s' if summary.hidden_failures != 1 else ''})",
        file=out,
    )
    if summary.retried:
        print(f"  {summary.retried} passed on a re-run after a non-failure", file=out)
    if summary.still_failing:
        print(f"  {summary.still_failing} still failing after a re-run", file=out)
    if summary.inconclusive:
        print(f"  {summary.inconclusive} re-run, still no pass/fail conclusion", file=out)
    if summary.undecided:
        why = ", ".join(
            f"{name} {count}"
            for name, count in summary.conclusions
            if name not in core.DECISIVE_CONCLUSIONS
        )
        print(f"  {summary.undecided} reached no pass/fail conclusion ({why})", file=out)

    print(file=out)
    print(f"first-attempt pass rate  {format_rate(summary.first_pass, summary.first_known)}", file=out)
    print(f"eventual pass rate       {format_rate(summary.eventual_pass, summary.decided)}", file=out)


def as_json(repo: str, filters: dict, runs: list[core.Run], summary: core.Summary) -> dict:
    return {
        "repo": repo,
        "filters": {k: v for k, v in filters.items() if v is not None},
        "runs": [
            {
                "id": run.id,
                "workflow": run.workflow,
                "event": run.event,
                "branch": run.branch,
                "title": run.title,
                "url": run.url,
                "verdict": run.verdict,
                "hidden_failures": len(run.hidden_failures),
                "attempts": [
                    {
                        "number": a.number,
                        "available": a.available,
                        "status": a.status,
                        "conclusion": a.conclusion,
                        "started_at": a.started_at.isoformat() if a.started_at else None,
                        "ended_at": a.ended_at.isoformat() if a.ended_at else None,
                        "duration_seconds": a.duration,
                        "url": a.url,
                    }
                    for a in run.attempts
                ],
            }
            for run in runs
        ],
        "summary": {
            "checked": summary.checked,
            "running": summary.running,
            "undecided": summary.undecided,
            "multi_attempt": summary.multi_attempt,
            "greenwashed": summary.greenwashed,
            "hidden_failures": summary.hidden_failures,
            "retried": summary.retried,
            "still_failing": summary.still_failing,
            "inconclusive": summary.inconclusive,
            "first_attempt_passes": summary.first_pass,
            "first_attempt_known": summary.first_known,
            "first_attempt_pass_rate": summary.first_rate,
            "eventual_passes": summary.eventual_pass,
            "decided": summary.decided,
            "eventual_pass_rate": summary.eventual_rate,
            "conclusions": dict(summary.conclusions),
        },
    }


# --- the shift itself ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="greenwash",
        description=(
            "Find the CI failures GitHub hides: workflow runs that only went green "
            "because someone re-ran them."
        ),
        epilog=(
            "Exit codes: 0 nothing was hidden, 1 at least one run went green on a "
            "retry, 2 the check could not run."
        ),
    )
    parser.add_argument("--repo", metavar="OWNER/REPO", help="default: GH_REPO, then the origin remote")
    parser.add_argument("--workflow", metavar="NAME", help="workflow display name or filename")
    parser.add_argument("--branch", metavar="BRANCH")
    parser.add_argument("--event", metavar="EVENT", help="push, pull_request, schedule, ...")
    parser.add_argument("--since", metavar="DATE", help="only runs created on or after this date")
    parser.add_argument("--limit", type=int, default=100, metavar="N", help="runs to examine (default 100)")
    parser.add_argument("--all", action="store_true", help="list every run, not only the re-run ones")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--gh", default="gh", metavar="PATH", help="the gh executable to use")
    return parser


def run(argv: list[str] | None = None, out=None, err=None, transport=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)

    if args.limit < 1:
        print("greenwash: --limit must be at least 1", file=err)
        return EXIT_ERROR

    try:
        repo = args.repo or infer_repo()
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise Problem(f"--repo wants OWNER/REPO, got {repo!r}")

        transport = transport or api.GhTransport(gh=args.gh)
        workflow = api.resolve_workflow(transport, repo, args.workflow) if args.workflow else None

        payloads = api.list_runs(
            transport,
            repo,
            workflow=workflow,
            branch=args.branch,
            event=args.event,
            since=args.since,
            limit=args.limit,
        )
        runs = [
            core.build_run(p, api.fetch_earlier_attempts(transport, repo, p)) for p in payloads
        ]
    except (Problem, api.ApiError) as exc:
        print(f"greenwash: {exc}", file=err)
        return EXIT_ERROR

    filtered = any((args.workflow, args.branch, args.event, args.since))
    if not payloads and not filtered:
        # A check that never ran must not look like a pass. With no filters
        # in play, an empty list means this repository has never run a
        # workflow at all, which is not the same as having nothing hidden.
        print(
            f"greenwash: {repo} has no workflow runs at all, so nothing was checked.",
            file=err,
        )
        return EXIT_ERROR

    summary = core.summarise(runs)

    if args.as_json:
        filters = {
            "workflow": args.workflow,
            "branch": args.branch,
            "event": args.event,
            "since": args.since,
            "limit": args.limit,
        }
        json.dump(as_json(repo, filters, runs, summary), out, indent=2)
        print(file=out)
    else:
        render(runs, summary, args.all, out)

    return EXIT_FINDINGS if summary.greenwashed else EXIT_CLEAN


def main() -> int:  # pragma: no cover - thin wrapper
    return run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
