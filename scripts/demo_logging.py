# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo step 5b (FR10, AC5): the audit trail.

(a) Institution A's Clean Rooms analysis log (CloudWatch Logs, the data owner's audit
    record): which query ran, who ran it, who received the results.
(c) Institution B's CloudTrail Event history: StartProtectedQuery calls (recorded in the
    caller's account).

Usage: .venv/bin/python -m scripts.demo_logging [--run-id R | --since-minutes N]
       [--wait-minutes M] [--config PATH]

AC5 PASS: an A-side SUBMISSION entry for the target query (latest SUCCESS approved/control
query) whose resultReceivers include the analyst account. Exit 0 PASS, 1 failure,
3 PENDING (not yet delivered). CloudTrail is printed but never changes the exit code.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time

from airlock_lib import aws, cleanrooms, deployment, state
from airlock_lib.config import Config, load_config
from airlock_lib.output import banner, info, warn

PENDING = 3


def _ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


def a_side_events(cfg: Config, sessions: dict, collab: str, start: dt.datetime) -> tuple[list[str], list[dict]]:
    logs = sessions["data_owner"].client("logs")
    groups = []
    for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePattern=collab):
        groups.extend(g["logGroupName"] for g in page.get("logGroups", []))
    events = []
    for g in groups:
        for page in logs.get_paginator("filter_log_events").paginate(logGroupName=g, startTime=_ms(start)):
            for e in page.get("events", []):
                ev = cleanrooms.decode_log_event(e.get("message", ""))
                ev["_logGroup"] = g
                events.append(ev)
    return groups, events


def b_cloudtrail(sessions: dict, start: dt.datetime) -> list[dict]:
    ct = sessions["analyst"].client("cloudtrail")
    out = []
    for page in ct.get_paginator("lookup_events").paginate(
            LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "StartProtectedQuery"}],
            StartTime=start):
        for e in page.get("Events", []):
            try:
                out.append(json.loads(e.get("CloudTrailEvent", "{}")))
            except json.JSONDecodeError:
                continue
    return out


def _short(v, n: int = 120) -> str:
    s = v if isinstance(v, str) else json.dumps(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def print_a_events(events: list[dict], run_qids: set[str]) -> None:
    if not events:
        print("  (no analysis-log entries in Account A for this window yet)")
    for ev in sorted(events, key=lambda e: str(e.get("eventTimestamp", ""))):
        mark = "*" if ev.get("eventId") in run_qids else " "
        table = f"  table={ev.get('schemaName')}" if ev.get("schemaName") else ""
        print(f" {mark} {ev.get('eventTimestamp')}  {cleanrooms.log_stage(ev)}  eventId={ev.get('eventId')}  "
              f"status={ev.get('status')}{table}")
        if ev.get("errorMessage") or ev.get("queryValidationErrors"):
            print(f"      errorMessage={ev.get('errorMessage')}  queryValidationErrors={ev.get('queryValidationErrors')}")
        print(f"      queryRunners={_short(ev.get('queryRunners'))}  resultReceivers={_short(ev.get('resultReceivers'))}")
        print(f"      memberDisplayNames={_short(ev.get('memberDisplayNames'))}")
        print(f"      queryText: {_short(ev.get('queryText', ''))}")
    print("  (* = query from this run)")


def print_ct_events(events: list[dict], run_qids: set[str]) -> None:
    if not events:
        print("  (no StartProtectedQuery events in Account B's Event history for this window yet)")
    for ev in sorted(events, key=lambda e: e.get("eventTime", "")):
        pq_ = ((ev.get("responseElements") or {}).get("protectedQuery") or {})
        mark = "*" if pq_.get("id") in run_qids else " "
        err = f"  errorCode={ev.get('errorCode')}  errorMessage={_short(ev.get('errorMessage') or '', 160)}" \
            if ev.get("errorCode") else ""
        print(f" {mark} {ev.get('eventTime')}  {(ev.get('userIdentity') or {}).get('arn')}")
        print(f"      protectedQuery.id={pq_.get('id')}  status={pq_.get('status')}{err}  "
              f"sqlParameters={_short((ev.get('requestParameters') or {}).get('sqlParameters'), 20)}")
    print("  StartProtectedQuery is recorded in the caller's account (B), not in A's; A's audit record is (a).")


def run(cfg: Config, sessions: dict, d: deployment.Deployed, run_id: str | None = None, since_minutes: int = 60,
        wait_minutes: float = 0.0, sleep=time.sleep) -> int:
    A, B = cfg.accounts.data_owner, cfg.accounts.analyst
    qlog = state.read_jsonl(cleanrooms.QUERY_LOG)
    now = cleanrooms.utc_now()
    since = None if run_id else now - dt.timedelta(minutes=since_minutes)
    target = cleanrooms.select_ac5_target(qlog, since, run_id)
    if target is None:
        raise aws.AirlockError("no SUCCESS approved/control query in window; run demo_allowed first")
    t_run, t_qid = target
    lines = cleanrooms.run_lines(qlog, t_run)
    run_qids = {ln["protected_query_id"] for ln in lines if ln.get("protected_query_id")}
    starts = [cleanrooms._parse_ts(ln.get("ts_utc", "")) for ln in lines]
    start = min([s for s in starts if s] or [now]) - dt.timedelta(minutes=30)
    collab = d.collaboration_id

    banner(f"Logging (FR10/AC5): run {t_run}, target query {t_qid}")
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        groups, a_events = a_side_events(cfg, sessions, collab, start)
        entry = cleanrooms.find_ac5_entry(a_events, t_qid, B.account_id)
        ct_events = b_cloudtrail(sessions, start)
        ct_seen = any(((e.get("responseElements") or {}).get("protectedQuery") or {}).get("id") == t_qid
                      for e in ct_events)
        if entry is not None or time.monotonic() >= deadline:
            break
        info(f"waiting for analysis-log delivery ({int(deadline - time.monotonic())} s left); retry in 30 s")
        sleep(30)

    state.write_json("log_groups.json", {"collaboration_id": collab, "data_owner_log_groups": groups})
    print(f"\n(a) {A.display_name} ({A.account_id}) analysis log groups: {groups or 'none yet'}")
    print_a_events(a_events, run_qids)
    print(f"\n(c) {B.display_name} ({B.account_id}) CloudTrail Event history: StartProtectedQuery")
    print_ct_events(ct_events, run_qids)
    if not ct_seen:
        warn("CloudTrail event not yet in Event history (delivery can take ~15 min)")

    print()
    if entry is not None:
        print(f"  PASS  AC5: {A.display_name}'s analysis log has a SUBMISSION entry for {t_qid}; "
              f"resultReceivers={json.dumps(entry.get('resultReceivers'))} includes {B.account_id} "
              f"({B.display_name})")
        return 0
    print(f"  PENDING (not yet delivered): no SUBMISSION entry for {t_qid} in Account A's analysis log yet. "
          "Re-run: .venv/bin/python -m scripts.demo_logging --wait-minutes 10")
    return PENDING


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.demo_logging", description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--run-id", help="target this demo run ID (from .state/query_log.jsonl)")
    g.add_argument("--since-minutes", type=int, default=60, help="window for the latest run (default 60)")
    ap.add_argument("--wait-minutes", type=float, default=0.0, help="poll every 30 s until AC5 passes (default 0)")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)

    def body() -> int:
        cfg = load_config(args.config)
        sessions = aws.preflight(cfg)
        d = deployment.live(cfg, sessions)
        return run(cfg, sessions, d, run_id=args.run_id, since_minutes=args.since_minutes,
                   wait_minutes=args.wait_minutes)

    aws.run_main(body, lambda: load_config(args.config))


if __name__ == "__main__":
    main()
