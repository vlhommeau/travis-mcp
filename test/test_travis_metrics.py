import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import travis_metrics as tm  # noqa: E402

UTC = timezone.utc


def job(created, started=None, finished=None, state="passed", stage=None, restarted=None):
    return {
        "id": hash((created, started, finished)) & 0xFFFF,
        "created_at": created,
        "started_at": started,
        "finished_at": finished,
        "restarted_at": restarted,
        "state": state,
        "stage": {"number": stage} if stage else None,
    }


def build(build_id, jobs, state="passed", repo="org/a"):
    starts = [j["started_at"] for j in jobs if j["started_at"]]
    ends = [j["finished_at"] for j in jobs if j["finished_at"]]
    return {
        "id": build_id,
        "state": state,
        "started_at": min(starts) if starts else None,
        "finished_at": max(ends) if ends else None,
        "duration": None,
        "jobs": jobs,
        "_repo": repo,
    }


def raw(builds, since="2026-09-01T00:00:00+00:00", until="2026-09-03T00:00:00+00:00"):
    return {"owner": "org", "since": since, "until": until, "fetched_at": "2026-09-10T00:00:00+00:00", "builds": builds}


class SweepTest(unittest.TestCase):
    def test_peak_counts_overlaps_and_touching_intervals_do_not_overlap(self):
        builds = [
            build(1, [job("2026-09-01T10:00:00Z", "2026-09-01T10:00:00Z", "2026-09-01T11:00:00Z")]),
            build(2, [job("2026-09-01T10:30:00Z", "2026-09-01T10:30:00Z", "2026-09-01T12:00:00Z")]),
            # starts exactly when build 1 ends: peak stays 2
            build(3, [job("2026-09-01T11:00:00Z", "2026-09-01T11:00:00Z", "2026-09-01T11:30:00Z")]),
        ]
        rows, summary = tm.analyze(raw(builds), ZoneInfo("UTC"), limit=2)
        day1 = rows[0]
        self.assertEqual(day1["peak_concurrent_jobs"], 2)
        # at >= 2 running from 10:30 to 11:30
        self.assertEqual(day1["minutes_at_or_above_limit"], 60.0)
        self.assertEqual(day1["build_minutes"], 60 + 90 + 30)
        self.assertEqual(summary["concurrency"]["days_at_or_above_limit"], 1)
        self.assertEqual(rows[1]["peak_concurrent_jobs"], 0)

    def test_job_spanning_midnight_counts_on_both_days(self):
        builds = [build(1, [job("2026-09-01T23:00:00Z", "2026-09-01T23:00:00Z", "2026-09-02T01:00:00Z")])]
        rows, _ = tm.analyze(raw(builds), ZoneInfo("UTC"), limit=10)
        self.assertEqual([r["peak_concurrent_jobs"] for r in rows], [1, 1])
        self.assertEqual([r["build_minutes"] for r in rows], [120.0, 0.0])

    def test_waiting_is_only_counted_while_at_limit(self):
        builds = [
            build(1, [job("2026-09-01T10:00:00Z", "2026-09-01T10:00:00Z", "2026-09-01T11:00:00Z")]),
            # queued at 10:10 while 1 job runs (limit 1), starts when the slot frees at 11:00
            build(2, [job("2026-09-01T10:10:00Z", "2026-09-01T11:00:00Z", "2026-09-01T11:20:00Z")]),
        ]
        rows, _ = tm.analyze(raw(builds), ZoneInfo("UTC"), limit=1)
        self.assertEqual(rows[0]["peak_waiting_jobs_at_limit"], 1)
        self.assertEqual(rows[0]["waiting_job_minutes_at_limit"], 50.0)
        self.assertEqual(rows[0]["jobs_waited_over_1min"], 1)
        # running is capped at 1 by the plan, but demand shows 2 jobs wanted to run at once
        self.assertEqual(rows[0]["peak_concurrent_jobs"], 1)
        self.assertEqual(rows[0]["peak_demand_jobs"], 2)

    def test_later_stage_waits_from_previous_stage_end_not_creation(self):
        jobs = [
            job("2026-09-01T10:00:00Z", "2026-09-01T10:00:00Z", "2026-09-01T10:30:00Z", stage=1),
            job("2026-09-01T10:00:00Z", "2026-09-01T10:30:20Z", "2026-09-01T10:40:00Z", stage=2),
        ]
        extracted, _ = tm.extract_jobs([build(1, jobs)], datetime(2026, 9, 10, tzinfo=UTC))
        second = extracted[1]
        self.assertEqual(second.wait_start, datetime(2026, 9, 1, 10, 30, tzinfo=UTC))
        rows, _ = tm.analyze(raw([build(1, jobs)]), ZoneInfo("UTC"), limit=10)
        self.assertEqual(rows[0]["jobs_waited_over_1min"], 0)

    def test_canceled_while_queued_waited_until_cancel_and_restart_resets_queue_time(self):
        jobs = [
            job("2026-09-01T10:00:00Z", None, "2026-09-01T10:20:00Z", state="canceled"),
            job("2026-08-01T10:00:00Z", "2026-09-01T12:00:30Z", "2026-09-01T12:10:00Z", restarted="2026-09-01T12:00:00Z"),
        ]
        extracted, implausible = tm.extract_jobs([build(1, jobs)], datetime(2026, 9, 10, tzinfo=UTC))
        self.assertEqual(extracted[0].wait_end, datetime(2026, 9, 1, 10, 20, tzinfo=UTC))
        self.assertIsNone(extracted[0].start)
        self.assertEqual(extracted[1].wait_start, datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
        self.assertEqual(implausible, 0)

    def test_states_rates_and_active_repos(self):
        builds = [
            build(1, [job("2026-09-01T10:00:00Z", "2026-09-01T10:00:00Z", "2026-09-01T10:10:00Z")], "passed", "org/a"),
            build(2, [job("2026-09-01T11:00:00Z", "2026-09-01T11:00:00Z", "2026-09-01T11:10:00Z")], "errored", "org/b"),
            build(3, [job("2026-09-02T11:00:00Z", "2026-09-02T11:00:00Z", "2026-09-02T11:10:00Z")], "failed", "org/b"),
            build(4, [job("2026-09-02T12:00:00Z", None, "2026-09-02T12:01:00Z", state="canceled")], "canceled", "org/b"),
        ]
        rows, summary = tm.analyze(raw(builds), ZoneInfo("UTC"), limit=10)
        self.assertEqual((rows[0]["passed"], rows[0]["errored"], rows[0]["errored_rate"]), (1, 1, 0.5))
        self.assertEqual(summary["reliability"]["errored_rate"], 0.25)
        self.assertEqual(summary["scope"]["active_repositories"], 2)
        self.assertEqual(summary["volume"]["builds"], 4)

    def test_local_days_follow_timezone(self):
        # 23:30 UTC on Sep 1 is Sep 2 in Paris (UTC+2)
        builds = [build(1, [job("2026-09-01T23:30:00Z", "2026-09-01T23:30:00Z", "2026-09-01T23:40:00Z")])]
        rows, _ = tm.analyze(raw(builds, since="2026-09-01T00:00:00+02:00", until="2026-09-03T00:00:00+02:00"), ZoneInfo("Europe/Paris"), 10)
        self.assertEqual([(r["date"], r["builds"]) for r in rows], [("2026-09-01", 0), ("2026-09-02", 1)])

    def test_queue_wait_distribution_and_threshold_are_explicit(self):
        builds = [
            build(1, [job("2026-09-01T10:00:00Z", "2026-09-01T10:00:20Z", "2026-09-01T10:10:00Z")]),  # 20s
            build(2, [job("2026-09-01T11:00:00Z", "2026-09-01T11:00:45Z", "2026-09-01T11:10:00Z")]),  # 45s
            build(3, [job("2026-09-01T12:00:00Z", "2026-09-01T12:03:00Z", "2026-09-01T12:10:00Z")]),  # 3min
            build(4, [job("2026-09-01T13:00:00Z", "2026-09-01T13:20:00Z", "2026-09-01T13:30:00Z")]),  # 20min
        ]
        rows, summary = tm.analyze(raw(builds), ZoneInfo("UTC"), limit=10)
        self.assertEqual(summary["concurrency"]["wait_threshold_seconds"], 60)
        self.assertEqual(
            summary["concurrency"]["queue_wait_distribution"],
            {"under_40s": 1, "40s_to_1min": 1, "1_to_2min": 0, "2_to_5min": 1, "5_to_15min": 0, "over_15min": 1},
        )
        self.assertEqual(rows[0]["jobs_waited_over_1min"], 2)

    def test_months_ago_clamps_day(self):
        self.assertEqual(tm.months_ago(datetime(2026, 5, 31, tzinfo=UTC), 3).date().isoformat(), "2026-02-28")


if __name__ == "__main__":
    unittest.main()
