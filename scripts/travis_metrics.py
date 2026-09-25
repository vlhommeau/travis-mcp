#!/usr/bin/env python3
"""Travis CI usage metrics for a whole account, from the Travis API v3.

Extracts, over a period (default: the last 3 months, up to yesterday):
  - build minutes per month and per day, builds per day, build duration mean/median;
  - peak concurrent jobs per day (sweep over every job's [started_at, finished_at]),
    and the days at or above the plan's concurrency limit, with an estimate of the jobs
    left waiting while the limit was reached;
  - build state split per day (passed / failed / errored / canceled) and errored rate;
  - active repositories (at least one build in the period).

Read-only: GET requests only. Standard library only (Python 3.11+).

    export TRAVIS_API_TOKEN=...   # Travis > Settings > API authentication
    ./travis_metrics.py --owner my-org --out ./travis-metrics
    ./travis_metrics.py --from-cache ./travis-metrics/raw.json --out ./travis-metrics   # re-analyze only
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = "https://api.travis-ci.com"
PAGE_SIZE = 100
BUILD_STATES = ("passed", "failed", "errored", "canceled")
# A job starting later than this after being queued counts as "waited" (VM boot alone is ~30s).
WAIT_THRESHOLD = timedelta(seconds=60)
# Longer "waits" are data artifacts (stuck/orphaned jobs), not queueing: excluded, and counted.
MAX_PLAUSIBLE_WAIT = timedelta(hours=6)
# Queue wait histogram buckets (upper bounds), to see how many jobs sit just under the threshold.
WAIT_BUCKETS = (
    ("under_40s", timedelta(seconds=40)),
    ("40s_to_1min", timedelta(minutes=1)),
    ("1_to_2min", timedelta(minutes=2)),
    ("2_to_5min", timedelta(minutes=5)),
    ("5_to_15min", timedelta(minutes=15)),
    ("over_15min", None),
)


def wait_bucket(wait: timedelta) -> str:
    for name, upper in WAIT_BUCKETS:
        if upper is None or wait < upper:
            return name
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- API client


class TravisClient:
    def __init__(self, token: str, min_interval: float, api_url: str = API_URL, verbose: bool = True):
        self.token = token
        self.min_interval = min_interval
        self.api_url = api_url.rstrip("/")
        self.verbose = verbose
        self.requests = 0
        self._last_request = 0.0

    def get(self, path: str, params: dict | None = None) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.api_url}{path}{'?' + query if query else ''}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Travis-API-Version": "3",
                "Authorization": f"token {self.token}",
                "User-Agent": "travis-metrics",
            },
        )

        for attempt in range(6):
            # Travis publishes no rate-limit headers: throttle every request, back off on 429/5xx.
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 5:
                    body = error.read().decode(errors="replace")[:300]
                    raise RuntimeError(f"Travis API {error.code} on GET {path}: {body}") from error
                delay = float(error.headers.get("Retry-After") or 2 ** (attempt + 1))
                reason = f"HTTP {error.code}"
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == 5:
                    raise
                delay = 2 ** (attempt + 1)
                reason = str(error)
            self.log(f"  retrying GET {path} in {delay:.0f}s ({reason})")
            time.sleep(delay)
        raise AssertionError("unreachable")

    def paginate(self, path: str, key: str, params: dict | None = None):
        """Yield pages (lists) of `key`, following @pagination offsets."""
        offset = 0
        while True:
            page = self.get(path, {**(params or {}), "limit": PAGE_SIZE, "offset": offset})
            yield page[key]
            pagination = page.get("@pagination") or {}
            if pagination.get("is_last", True) or not page[key]:
                return
            offset = pagination["next"]["offset"]

    def log(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)


# --------------------------------------------------------------------------- collection


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def build_trigger_time(build: dict) -> datetime | None:
    """When the build entered the system: earliest job creation, else its start/update time."""
    created = [parse_time(job.get("created_at")) for job in build.get("jobs", [])]
    created = [value for value in created if value]
    if created:
        return min(created)
    return parse_time(build.get("started_at")) or parse_time(build.get("updated_at"))


def collect(client: TravisClient, owner: str, since: datetime, until: datetime, only_repos: set[str] | None) -> dict:
    repos = []
    for page in client.paginate(f"/owner/{urllib.parse.quote(owner, safe='')}/repos", "repositories"):
        repos.extend(page)
    client.log(f"{len(repos)} repositories under {owner}")

    candidates = [
        repo
        for repo in repos
        if (repo.get("build_count") or 0) > 0 and (only_repos is None or repo["slug"] in only_repos)
    ]
    client.log(f"{len(candidates)} with at least one build ever, scanning their recent builds")

    builds = []
    for index, repo in enumerate(candidates, 1):
        kept = 0
        path = f"/repo/{repo['id']}/builds"
        # Newest first by id (= creation order); stop once a whole page predates the period.
        for page in client.paginate(path, "builds", {"sort_by": "id:desc", "include": "build.jobs"}):
            triggers = []
            for build in page:
                trigger = build_trigger_time(build)
                triggers.append(trigger)
                if trigger and since <= trigger < until:
                    complete_jobs(client, build)
                    build["_repo"] = repo["slug"]
                    builds.append(build)
                    kept += 1
            known = [trigger for trigger in triggers if trigger]
            if known and max(known) < since - timedelta(days=1):
                break
        if kept:
            client.log(f"  [{index}/{len(candidates)}] {repo['slug']}: {kept} builds")

    return {
        "owner": owner,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "api_requests": client.requests,
        "builds": builds,
    }


def complete_jobs(client: TravisClient, build: dict) -> None:
    """`include=build.jobs` embeds standard job representations; fall back to GET /job/{id}."""
    for position, job in enumerate(build.get("jobs", [])):
        if "started_at" not in job:
            build["jobs"][position] = client.get(f"/job/{job['id']}")


# --------------------------------------------------------------------------- analysis


@dataclass
class Job:
    build_id: int
    repo: str
    start: datetime | None
    end: datetime | None
    wait_start: datetime | None
    wait_end: datetime | None


def extract_jobs(builds: list[dict], fetched_at: datetime) -> tuple[list[Job], int]:
    jobs: list[Job] = []
    implausible_waits = 0
    for build in builds:
        raw_jobs = build.get("jobs", [])
        # Jobs of stage N are created with the build but only become runnable when stage N-1
        # finishes: their queue wait starts then, not at creation.
        stage_end: dict[int, datetime] = defaultdict(lambda: datetime.min.replace(tzinfo=timezone.utc))
        for job in raw_jobs:
            number = (job.get("stage") or {}).get("number") or 1
            finished = parse_time(job.get("finished_at"))
            if finished:
                stage_end[number] = max(stage_end[number], finished)

        for job in raw_jobs:
            start = parse_time(job.get("started_at"))
            finished = parse_time(job.get("finished_at"))
            end = finished or (fetched_at if start else None)
            number = (job.get("stage") or {}).get("number") or 1
            queued = parse_time(job.get("restarted_at")) or parse_time(job.get("created_at"))
            previous_stages = [stage_end[n] for n in stage_end if n < number]
            if queued and previous_stages:
                queued = max([queued, *previous_stages])

            # A job canceled while still queued waited until it was canceled.
            wait_end = start or (finished if job.get("state") == "canceled" else None)
            if queued and wait_end and wait_end - queued > MAX_PLAUSIBLE_WAIT:
                implausible_waits += 1
                queued = None
            jobs.append(Job(build["id"], build["_repo"], start, end, queued, wait_end))
    return jobs, implausible_waits


def day_bounds(since: datetime, until: datetime, tz: ZoneInfo) -> list[tuple[date, datetime, datetime]]:
    days = []
    current = since.astimezone(tz).date()
    last = (until - timedelta(microseconds=1)).astimezone(tz).date()
    while current <= last:
        start = datetime.combine(current, datetime.min.time(), tz)
        days.append((current, start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)))
        current += timedelta(days=1)
    return days


def sweep(jobs: list[Job], days: list[tuple[date, datetime, datetime]], limit: int) -> dict[date, dict]:
    """Single sweep over running and waiting intervals, split at day boundaries.

    Per day: peak running jobs, peak demand (running + waiting: running alone can never exceed
    the plan's cap, so demand is what shows by how much the cap was exceeded), minutes spent
    at/above `limit`, peak waiting jobs while at the limit, and waiting job-minutes
    accumulated while at the limit.
    """
    events: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])  # [running delta, waiting delta]
    window_start, window_end = days[0][1], days[-1][2]

    def add(start: datetime | None, end: datetime | None, slot: int) -> None:
        if not start or not end or end <= start:
            return
        start, end = max(start, window_start), min(end, window_end)
        if end <= start:
            return
        events[start][slot] += 1
        events[end][slot] -= 1

    for job in jobs:
        add(job.start, job.end, 0)
        add(job.wait_start, job.wait_end, 1)
    for _, day_start, _ in days:
        events[day_start]  # boundary marker, no delta

    boundaries = days
    stats ={day: {"peak": 0, "peak_demand": 0, "at_limit_minutes": 0.0, "peak_waiting_at_limit": 0, "waiting_minutes_at_limit": 0.0} for day, _, _ in days}

    running = waiting = 0
    times = sorted(t for t in events if window_start <= t <= window_end)
    day_index = 0
    for position, moment in enumerate(times):
        delta_running, delta_waiting = events[moment]
        running += delta_running
        waiting += delta_waiting
        while day_index + 1 < len(boundaries) and moment >= boundaries[day_index + 1][1]:
            day_index += 1
        if moment >= window_end:
            break
        day = stats[boundaries[day_index][0]]
        day["peak"] = max(day["peak"], running)
        day["peak_demand"] = max(day["peak_demand"], running + waiting)
        segment = ((times[position + 1] if position + 1 < len(times) else window_end) - moment).total_seconds() / 60
        if running >= limit:
            day["at_limit_minutes"] += segment
            day["peak_waiting_at_limit"] = max(day["peak_waiting_at_limit"], waiting)
            day["waiting_minutes_at_limit"] += waiting * segment
    return stats


def minutes(start: datetime | None, end: datetime | None) -> float:
    return (end - start).total_seconds() / 60 if start and end and end > start else 0.0


def describe(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "p90": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 2),
        "max": round(ordered[-1], 2),
    }


def percentiles(values: list[float], points: tuple[int, ...]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {f"p{p}": round(ordered[int(p / 100 * (len(ordered) - 1))], 1) for p in points}


def analyze(raw: dict, tz: ZoneInfo, limit: int) -> tuple[list[dict], dict]:
    since, until = parse_time(raw["since"]), parse_time(raw["until"])
    fetched_at = parse_time(raw["fetched_at"])
    builds = raw["builds"]
    days = day_bounds(since, until, tz)
    jobs, implausible_waits = extract_jobs(builds, fetched_at)
    concurrency = sweep(jobs, days, limit)

    local_day = lambda moment: moment.astimezone(tz).date()  # noqa: E731
    daily = {day: {"builds": 0, "jobs": 0, "build_minutes": 0.0, "jobs_waited_over_1min": 0, **{s: 0 for s in BUILD_STATES}, "other": 0} for day, _, _ in days}
    monthly_minutes: dict[str, float] = defaultdict(float)
    repo_builds: Counter[str] = Counter()
    state_totals: Counter[str] = Counter()
    wall_clock, billed = [], []

    for build in builds:
        day = local_day(build_trigger_time(build))
        if day not in daily:
            continue
        row = daily[day]
        row["builds"] += 1
        state = build.get("state")
        row[state if state in BUILD_STATES else "other"] += 1
        state_totals[state if state in BUILD_STATES else "other"] += 1
        repo_builds[build["_repo"]] += 1
        if state in ("passed", "failed", "errored"):
            duration = minutes(parse_time(build.get("started_at")), parse_time(build.get("finished_at")))
            if duration:
                wall_clock.append(duration)
            if build.get("duration"):
                billed.append(build["duration"] / 60)

    wait_histogram: Counter[str] = Counter({name: 0 for name, _ in WAIT_BUCKETS})
    wait_seconds: list[float] = []
    for job in jobs:
        if job.start and local_day(job.start) in daily:
            row = daily[local_day(job.start)]
            row["jobs"] += 1
            row["build_minutes"] += minutes(job.start, job.end)
            monthly_minutes[job.start.astimezone(tz).strftime("%Y-%m")] += minutes(job.start, job.end)
            if job.wait_start:
                wait = max(job.start - job.wait_start, timedelta(0))
                wait_histogram[wait_bucket(wait)] += 1
                wait_seconds.append(wait.total_seconds())
                if job.start - job.wait_start > WAIT_THRESHOLD:
                    row["jobs_waited_over_1min"] += 1

    rows = []
    for day, _, _ in days:
        row, peak = daily[day], concurrency[day]
        finished = sum(row[s] for s in BUILD_STATES)
        rows.append(
            {
                "date": day.isoformat(),
                "weekday": day.strftime("%a"),
                "builds": row["builds"],
                "jobs": row["jobs"],
                "build_minutes": round(row["build_minutes"], 1),
                "peak_concurrent_jobs": peak["peak"],
                "peak_demand_jobs": peak["peak_demand"],
                "at_or_above_limit": peak["peak"] >= limit,
                "minutes_at_or_above_limit": round(peak["at_limit_minutes"], 1),
                "peak_waiting_jobs_at_limit": peak["peak_waiting_at_limit"],
                "waiting_job_minutes_at_limit": round(peak["waiting_minutes_at_limit"], 1),
                "jobs_waited_over_1min": row["jobs_waited_over_1min"],
                **{s: row[s] for s in BUILD_STATES},
                "other": row["other"],
                "errored_rate": round(row["errored"] / finished, 4) if finished else None,
                "failed_rate": round(row["failed"] / finished, 4) if finished else None,
            }
        )

    finished_total = sum(state_totals[s] for s in BUILD_STATES)
    saturated = [row for row in rows if row["at_or_above_limit"]]
    summary = {
        "owner": raw["owner"],
        "period": {"since": since.astimezone(tz).isoformat(), "until": until.astimezone(tz).isoformat(), "days": len(days), "timezone": str(tz)},
        "fetched_at": raw["fetched_at"],
        "api_requests": raw.get("api_requests"),
        "concurrency_limit": limit,
        "volume": {
            "builds": len([b for b in builds if local_day(build_trigger_time(b)) in daily]),
            "jobs_started": sum(r["jobs"] for r in rows),
            "build_minutes_total": round(sum(r["build_minutes"] for r in rows), 1),
            "build_minutes_by_month": {month: round(value, 1) for month, value in sorted(monthly_minutes.items())},
            "builds_per_day": describe([r["builds"] for r in rows]),
            "builds_per_weekday_day": describe([r["builds"] for r in rows if r["weekday"] not in ("Sat", "Sun")]),
            "build_duration_minutes_wall_clock": describe(wall_clock),
            "build_duration_minutes_billed": describe(billed),
        },
        "concurrency": {
            "max_peak": max((r["peak_concurrent_jobs"] for r in rows), default=0),
            "daily_peak": describe([r["peak_concurrent_jobs"] for r in rows]),
            "max_peak_demand": max((r["peak_demand_jobs"] for r in rows), default=0),
            "daily_peak_demand": describe([r["peak_demand_jobs"] for r in rows]),
            "days_at_or_above_limit": len(saturated),
            "minutes_at_or_above_limit_total": round(sum(r["minutes_at_or_above_limit"] for r in rows), 1),
            "wait_threshold_seconds": int(WAIT_THRESHOLD.total_seconds()),
            "queue_wait_distribution": dict(wait_histogram),
            "queue_wait_seconds_percentiles": percentiles(wait_seconds, (50, 90, 95, 99)),
            "waiting_job_minutes_at_limit_total": round(sum(r["waiting_job_minutes_at_limit"] for r in rows), 1),
            "saturated_days": [
                {k: r[k] for k in ("date", "weekday", "peak_concurrent_jobs", "peak_demand_jobs", "minutes_at_or_above_limit", "peak_waiting_jobs_at_limit", "waiting_job_minutes_at_limit", "jobs_waited_over_1min")}
                for r in saturated
            ],
        },
        "reliability": {
            "states": dict(state_totals),
            "errored_rate": round(state_totals["errored"] / finished_total, 4) if finished_total else None,
            "failed_rate": round(state_totals["failed"] / finished_total, 4) if finished_total else None,
            "canceled_rate": round(state_totals["canceled"] / finished_total, 4) if finished_total else None,
        },
        "scope": {
            "active_repositories": len(repo_builds),
            "builds_by_repository": dict(repo_builds.most_common()),
        },
        "caveats": [
            "Build minutes are job run times (started_at to finished_at). A restarted job only keeps its latest run's timestamps, so earlier runs of restarted jobs are not counted: minutes and peaks are lower bounds.",
            "Concurrency counts all running jobs of the account at once, across every repository and queue. Running jobs can never exceed the plan's cap; 'peak demand' (running + waiting) is what shows how far above the cap the need went. Its waiting part includes VM boot time, so it is a slight upper bound.",
            f"Queue wait = started_at minus the time the job became runnable (restarted_at or created_at, or the end of the previous stage). It includes VM boot time, hence the {int(WAIT_THRESHOLD.total_seconds())}s threshold for 'waited'. {implausible_waits} waits longer than {MAX_PLAUSIBLE_WAIT} were excluded as data artifacts.",
            "Waiting jobs 'at limit' are only counted while running jobs >= the concurrency limit, i.e. the queueing attributable to the plan's cap.",
            "Days are calendar days in the given timezone; a job spanning midnight counts towards both days' concurrency, and towards its start day's minutes.",
        ],
    }
    return rows, summary


# --------------------------------------------------------------------------- output


def write_outputs(out: Path, rows: list[dict], summary: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "daily.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    saturated = summary["concurrency"]["saturated_days"]
    with open(out / "saturated_days.csv", "w", newline="") as handle:
        fields = ["date", "weekday", "peak_concurrent_jobs", "peak_demand_jobs", "minutes_at_or_above_limit", "peak_waiting_jobs_at_limit", "waiting_job_minutes_at_limit", "jobs_waited_over_1min"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(saturated)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")


def print_summary(summary: dict) -> None:
    volume, concurrency, reliability = summary["volume"], summary["concurrency"], summary["reliability"]
    period = summary["period"]
    print(f"\n{summary['owner']}: {period['since'][:10]} -> {period['until'][:10]} ({period['days']} days, {period['timezone']})")
    print(f"  builds: {volume['builds']}  jobs: {volume['jobs_started']}  minutes: {volume['build_minutes_total']}")
    for month, value in volume["build_minutes_by_month"].items():
        print(f"    {month}: {value} min")
    per_day, duration = volume["builds_per_day"], volume["build_duration_minutes_wall_clock"]
    if per_day["count"]:
        print(f"  builds/day: mean {per_day['mean']}, median {per_day['median']}, max {per_day['max']}")
    if duration["count"]:
        print(f"  build duration (wall clock): mean {duration['mean']} min, median {duration['median']} min")
    print(f"  concurrency: max peak {concurrency['max_peak']}, max demand {concurrency['max_peak_demand']} (limit {summary['concurrency_limit']}), "
          f"{concurrency['days_at_or_above_limit']} day(s) at/above limit, "
          f"{concurrency['minutes_at_or_above_limit_total']} min total at limit")
    for day in concurrency["saturated_days"]:
        print(f"    {day['date']} {day['weekday']}: peak {day['peak_concurrent_jobs']}, demand {day['peak_demand_jobs']}, {day['minutes_at_or_above_limit']} min at limit, "
              f"up to {day['peak_waiting_jobs_at_limit']} waiting, {day['waiting_job_minutes_at_limit']} job-min waited")
    print(f"  queue wait (threshold {concurrency['wait_threshold_seconds']}s): {concurrency['queue_wait_seconds_percentiles']} {concurrency['queue_wait_distribution']}")
    print(f"  states: {reliability['states']}  errored rate: {reliability['errored_rate']}  failed rate: {reliability['failed_rate']}")
    print(f"  active repositories: {summary['scope']['active_repositories']}")


# --------------------------------------------------------------------------- CLI


def months_ago(moment: datetime, months: int) -> datetime:
    year, month = divmod(moment.year * 12 + moment.month - 1 - months, 12)
    return moment.replace(year=year, month=month + 1, day=min(moment.day, calendar.monthrange(year, month + 1)[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner", help="Travis owner login (organization or user), e.g. my-org")
    parser.add_argument("--months", type=int, default=3, help="period length ending at --until (default: 3)")
    parser.add_argument("--since", type=date.fromisoformat, help="first day, YYYY-MM-DD (overrides --months)")
    parser.add_argument("--until", type=date.fromisoformat, help="day after the last day, YYYY-MM-DD (default: today, i.e. up to yesterday)")
    parser.add_argument("--tz", default="Europe/Paris", help="timezone for calendar days (default: Europe/Paris)")
    parser.add_argument("--limit", type=int, default=10, help="plan concurrency limit (default: 10)")
    parser.add_argument("--repo", action="append", dest="repos", help="restrict to this repo slug (repeatable)")
    parser.add_argument("--min-interval", type=float, default=0.25, help="seconds between API requests (default: 0.25)")
    parser.add_argument("--out", type=Path, default=Path("travis-metrics"), help="output directory (default: ./travis-metrics)")
    parser.add_argument("--from-cache", type=Path, help="re-analyze a previous raw.json instead of calling the API")
    args = parser.parse_args(argv)
    tz = ZoneInfo(args.tz)

    if args.from_cache:
        raw = json.loads(args.from_cache.read_text())
    else:
        if not args.owner:
            parser.error("--owner is required unless --from-cache is given")
        token = os.environ.get("TRAVIS_API_TOKEN")
        if not token:
            parser.error("TRAVIS_API_TOKEN is not set")
        until_day = args.until or datetime.now(tz).date()
        until = datetime.combine(until_day, datetime.min.time(), tz)
        since = datetime.combine(args.since, datetime.min.time(), tz) if args.since else months_ago(until, args.months)
        client = TravisClient(token, args.min_interval)
        raw = collect(client, args.owner, since.astimezone(timezone.utc), until.astimezone(timezone.utc), set(args.repos) if args.repos else None)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "raw.json").write_text(json.dumps(raw) + "\n")
        client.log(f"{client.requests} API requests, raw data saved to {args.out / 'raw.json'}")

    rows, summary = analyze(raw, tz, args.limit)
    write_outputs(args.out, rows, summary)
    print_summary(summary)
    print(f"\nWritten: {args.out / 'daily.csv'}, {args.out / 'saturated_days.csv'}, {args.out / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
