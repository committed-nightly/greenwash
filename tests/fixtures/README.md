# Fixtures

Real payloads, captured from the public `committed-nightly/ops` repository
on 2026-09-06 with `gh api`. They are trimmed to the fields greenwash reads
and are otherwise unedited — no values were changed to make a test pass.

| file | what it is |
|---|---|
| `runs-list.json` | `GET /repos/committed-nightly/ops/actions/runs?per_page=30` |
| `run-33819369404.json` | the run object, i.e. its latest attempt |
| `run-33819369404-attempt-{1,2}.json` | `GET .../runs/33819369404/attempts/N` |
| `run-33769793117.json` | a run re-run in place that failed a second time |
| `run-33769793117-attempt-{1,2}.json` | both of its attempts |

Run `33819369404` is the night shift of 2026-09-03. Attempt 1 died seven
minutes in, having created a public repository and nothing else; attempt 2
ran seven hours later and succeeded. The Actions list shows one row, marked
success. That incident is why this tool exists, and `test_premise.py`
asserts against these payloads rather than against greenwash, so that a
change in how GitHub reports re-runs shows up as a failing premise rather
than as a tool that quietly stops finding anything.

To refresh them:

```bash
gh api repos/committed-nightly/ops/actions/runs/33819369404 \
  | jq '{id,name,event,head_branch,display_title,html_url,run_attempt,status,conclusion,created_at,run_started_at,updated_at}'
```
