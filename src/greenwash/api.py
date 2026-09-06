"""Talking to the GitHub API.

The transport is the ``gh`` CLI rather than urllib, because ``gh`` already
solves the boring half of this problem: token discovery, GitHub Enterprise
hosts via ``GH_HOST``, and the retry behaviour. Anyone who would run this
tool has ``gh`` installed and authenticated already.

The interesting consequence is that ``gh api`` writes the error body to
*stdout* as JSON with a ``status`` field, even when it exits non-zero. That
is how :class:`GhTransport` recovers the HTTP status without parsing the
human-readable line on stderr — which matters, because a 404 on one attempt
is data that has expired and a 401 on the same request is a broken token,
and the two must not be reported the same way.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote


class ApiError(Exception):
    """A request that did not come back with a usable body."""

    def __init__(self, message: str, *, status: int | None = None, path: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.path = path

    def __str__(self) -> str:
        bits = [self.message]
        if self.status is not None:
            bits.append(f"(HTTP {self.status})")
        if self.path is not None:
            bits.append(f"[{self.path}]")
        return " ".join(bits)


class Transport(Protocol):
    """Anything that can turn an API path into a decoded JSON body."""

    def get(self, path: str) -> Any:  # pragma: no cover - protocol
        ...


@dataclass
class GhTransport:
    """Fetch API paths by shelling out to ``gh api``."""

    gh: str = "gh"

    def get(self, path: str) -> Any:
        try:
            proc = subprocess.run(
                [self.gh, "api", "-H", "Accept: application/vnd.github+json", path],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            raise ApiError(
                f"{self.gh!r} is not on PATH. greenwash uses the GitHub CLI as its "
                f"transport; install it from https://cli.github.com or pass --gh."
            ) from None

        body = _loads(proc.stdout)

        if proc.returncode != 0:
            raise ApiError(_error_message(body, proc.stderr), status=_status(body), path=path)
        if body is _UNDECODABLE:
            raise ApiError(
                "gh exited 0 but its output was not JSON. Run the same request with "
                f"`{self.gh} api {path}` to see what came back.",
                path=path,
            )
        return body


_UNDECODABLE = object()


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return _UNDECODABLE


def _status(body: Any) -> int | None:
    if isinstance(body, dict):
        try:
            return int(body["status"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _error_message(body: Any, stderr: str) -> str:
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    line = stderr.strip().splitlines()
    return line[-1] if line else "gh failed with no output"


# --- endpoints -------------------------------------------------------------


def resolve_workflow(transport: Transport, repo: str, wanted: str) -> str:
    """Turn a workflow *name* into the filename the runs endpoint wants.

    A filename is passed straight through. Anything else is looked up against
    the repository's workflows and matched on display name, because that is
    the string people actually read — in this org the file is
    ``night-shift.yaml`` and every human calls it ``Richmond``.
    """
    if wanted.endswith((".yml", ".yaml")):
        return wanted

    body = transport.get(f"repos/{repo}/actions/workflows?per_page=100")
    workflows = body.get("workflows", []) if isinstance(body, dict) else []
    lowered = wanted.lower()
    for wf in workflows:
        if str(wf.get("name", "")).lower() == lowered:
            return str(wf.get("path", "")).rsplit("/", 1)[-1]

    known = ", ".join(sorted(str(w.get("name")) for w in workflows)) or "none"
    raise ApiError(f"no workflow named {wanted!r} in {repo}. Known workflows: {known}")


def list_runs(
    transport: Transport,
    repo: str,
    *,
    workflow: str | None = None,
    branch: str | None = None,
    event: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List workflow runs, newest first, at most *limit* of them.

    Every run here is its *latest* attempt — that is the whole reason this
    tool exists — but the payload carries ``run_attempt``, so one page tells
    us which runs have a history worth going and fetching.
    """
    if workflow:
        base = f"repos/{repo}/actions/workflows/{quote(workflow, safe='')}/runs"
    else:
        base = f"repos/{repo}/actions/runs"

    filters = []
    if branch:
        filters.append(("branch", branch))
    if event:
        filters.append(("event", event))
    if since:
        filters.append(("created", f">={since}"))

    runs: list[dict] = []
    page = 1
    while len(runs) < limit:
        want = min(100, limit - len(runs))
        query = [("per_page", str(want)), ("page", str(page))] + filters
        encoded = "&".join(f"{k}={quote(v, safe='')}" for k, v in query)
        body = transport.get(f"{base}?{encoded}")
        batch = body.get("workflow_runs", []) if isinstance(body, dict) else []
        runs.extend(batch)
        if len(batch) < want:
            break
        page += 1
    return runs[:limit]


def fetch_earlier_attempts(transport: Transport, repo: str, run: dict) -> dict[int, dict | None]:
    """Fetch every attempt of *run* except the last one.

    The last attempt needs no request: the run object returned by the list
    endpoint *is* the latest attempt, with that attempt's ``conclusion``,
    ``run_started_at`` and ``updated_at`` already on it. On a healthy repo
    where nothing has ever been re-run this function makes no requests at
    all, which is the difference between one API call and several hundred.

    A missing attempt maps to ``None`` rather than raising: GitHub can and
    does return 404 for old attempt data, and a run whose early history has
    aged out is still worth reporting with the part that survives.
    """
    out: dict[int, dict | None] = {}
    latest = int(run.get("run_attempt") or 1)
    run_id = run["id"]
    for number in range(1, latest):
        path = f"repos/{repo}/actions/runs/{run_id}/attempts/{number}"
        try:
            out[number] = transport.get(path)
        except ApiError as exc:
            if exc.status == 404:
                out[number] = None
            else:
                raise
    return out
