# travis-mcp

[MCP](https://modelcontextprotocol.io) server for Travis CI, over the
[Travis API v3](https://developer.travis-ci.com/). Lets AI coding agents check build
status, find the failing job and read its log — read-only by default, with opt-in build
restart/cancel. Also ships a standalone [usage metrics script](#usage-metrics-script)
(build minutes, concurrency peaks, reliability) for an entire Travis account.

## Why access control lives in the server

Travis API tokens have no scopes: the API docs state a token can be used "to do anything
that you can do using the web interface" (restart/cancel/trigger builds, edit settings and
env vars). Access therefore cannot be narrowed through the token and is enforced here:

- the HTTP client reads with `GET`, and its only write method refuses any path other than
  `/build/{id}/restart` and `/build/{id}/cancel` before sending anything (covered by tests,
  including path-traversal attempts);
- the write tools are not even registered unless `TRAVIS_ALLOW_WRITE` lists them;
- nothing ever triggers a build with custom config, reads or edits env vars, settings or
  caches.

## Tools

| Tool | Travis endpoint | Purpose |
|------|-----------------|---------|
| `get_build` | `GET /build/{id}` | State, branch/PR, commit, timing, job IDs, web URL |
| `get_build_jobs` | `GET /build/{id}/jobs` | Per-job state and stage, to spot the failing job |
| `list_builds` | `GET /repo/{slug}/builds` | Recent builds, filtered by branch, event type, state or PR number |
| `get_job_log` | `GET /job/{id}/log.txt` | Job log, ANSI and `travis_time`/`travis_fold` markers stripped, tail (default 200 lines) or `grep` with context |
| `restart_build` | `POST /build/{id}/restart` | **Opt-in** (`TRAVIS_ALLOW_WRITE=restart`). Restarts all jobs of a finished build — replaces the previous run's logs and result |
| `cancel_build` | `POST /build/{id}/cancel` | **Opt-in** (`TRAVIS_ALLOW_WRITE=cancel`). Cancels a created or running build |

The write tools are annotated `destructiveHint: true`, so MCP clients that honor
annotations ask for confirmation. Their descriptions tell the model to call them only on
an explicit human instruction naming the build; they return the build's state before the
action and whether Travis accepted it (`@type: pending`).

A job may have no stored log (Travis then returns a literal `null` body, e.g. a log not yet
archived or removed); `get_job_log` reports it as unavailable rather than returning `null`.

The API has no pull request filter, so `list_builds` with `pull_request` pages through the
last 500 PR builds and filters client side.

## Configuration

| Variable | Required | Default |
|----------|----------|---------|
| `TRAVIS_API_TOKEN` | yes | — |
| `TRAVIS_DEFAULT_REPO` | no | — (`owner/name` used when `list_builds` gets no `repo`) |
| `TRAVIS_ALLOW_WRITE` | no | empty = read-only. Comma list of `restart`, `cancel`; any other value fails at startup |
| `TRAVIS_API_URL` | no | `https://api.travis-ci.com` |
| `TRAVIS_WEB_URL` | no | `https://app.travis-ci.com` |

Get the token from <https://app.travis-ci.com/account/preferences> → **API authentication**
(works for accounts that sign in with GitHub), or with the Travis CLI:
`travis login --pro && travis token --pro`. The *Assets* and *RSS* tokens on the same page
do not give access to builds or logs.

Keep the token out of tracked files: store it in the OS keychain or a secrets manager and
export it into the environment the MCP client is launched from.

## Usage

Claude Code `.mcp.json`, pinned to a full commit SHA — not a tag or branch, which can be
moved to point at different code (the matching tag is listed in the release):

```json
{
  "mcpServers": {
    "travis": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "github:vlhommeau/travis-mcp#<commit-sha>"],
      "env": {
        "TRAVIS_API_TOKEN": "${TRAVIS_API_TOKEN}",
        "TRAVIS_DEFAULT_REPO": "owner/name",
        "TRAVIS_ALLOW_WRITE": "restart,cancel"
      }
    }
  }
}
```

No build step: the server is plain ESM JavaScript, runnable straight from a git checkout
(Node.js 20+). Dependencies are pinned to exact versions and the whole tree is locked by
`npm-shrinkwrap.json` — unlike `package-lock.json`, npm honors it when the package is
installed as a dependency (including through `npx github:...`), so every install resolves
the same transitive versions.

## Usage metrics script

[`scripts/travis_metrics.py`](scripts/travis_metrics.py) extracts account-wide usage over a
period — the numbers Travis Insights doesn't show (concurrency) or can't export. Python
3.11+, standard library only, **GET requests only**.

```sh
export TRAVIS_API_TOKEN=...
python3 scripts/travis_metrics.py --owner my-org --out ./travis-metrics            # last 3 months
python3 scripts/travis_metrics.py --owner my-org --since 2026-06-01 --until 2026-09-01 --limit 10
python3 scripts/travis_metrics.py --from-cache ./travis-metrics/raw.json --limit 5  # re-analyze, no API calls
```

| Option | Default | |
|---|---|---|
| `--owner` | — | Organization or user login; every repository under it is scanned |
| `--months` / `--since`, `--until` | 3 months up to yesterday | `--until` is exclusive |
| `--tz` | `Europe/Paris` | Timezone for calendar days |
| `--limit` | `10` | Plan concurrency limit, for saturation detection |
| `--repo` | all | Restrict to a slug (repeatable) |
| `--min-interval` | `0.25` s | Throttle between requests (Travis publishes no rate-limit headers; 429/5xx are retried with backoff, honoring `Retry-After`) |

Outputs, in `--out`:

- `daily.csv` — per day: builds, jobs, build minutes, peak concurrent jobs, **peak demand**
  (running + waiting — running jobs can never exceed the plan's cap, so demand is what shows
  how far above it the need went), minutes at or above the limit, peak waiting jobs and waiting job-minutes while at the limit, jobs that
  waited over 1 minute, passed/failed/errored/canceled counts, errored and failed rates;
- `saturated_days.csv` — the days whose peak reached the limit;
- `summary.json` — totals and mean/median/p90: minutes per month, builds per day, build
  duration (wall clock and billed), concurrency, state split, active repositories, and the
  caveats below;
- `raw.json` — the fetched builds and jobs, for `--from-cache` re-analysis.

How it works:

- Repositories come from `GET /owner/{login}/repos` (skipping those with no build ever);
  builds from `GET /repo/{id}/builds?include=build.jobs&sort_by=id:desc`, which embeds each
  job's `created_at`/`started_at`/`finished_at`/`restarted_at`, so one request covers 100
  builds; `GET /job/{id}` is only a fallback. Paging stops once a whole page predates the
  period.
- **Concurrency**: one sweep over every job's `[started_at, finished_at]` across all
  repositories (the plan's limit is account-wide), split at local midnight.
- **Queue wait**: from when a job became runnable — `restarted_at`, else `created_at`, or the
  end of the previous stage for multi-stage builds — to `started_at` (or to its
  cancellation if it never started). The "waiting at limit" figures only count waiting
  while running jobs are at or above `--limit`, i.e. queueing attributable to the cap.

Caveats (also written to `summary.json`): a restarted job only keeps its latest run's
timestamps, so minutes and peaks are lower bounds; queue wait includes VM boot time, hence
the 1-minute threshold; waits over 6 hours are treated as data artifacts and excluded.

## Development

```sh
npm install
npm test
python3 -m unittest discover -s test -p 'test_*.py'
TRAVIS_API_TOKEN=... npx @modelcontextprotocol/inspector node src/index.js
```
