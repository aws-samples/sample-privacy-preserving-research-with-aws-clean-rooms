# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Clean Rooms query, result and log helpers shared by the demo scripts (design §5.1).

Queries always run strictly sequentially: none starts until the previous one reaches a
terminal state (the non-us-east-1 quota is 2 concurrent SQL queries per account).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import botocore.exceptions as bce
import pyarrow.parquet as pq

from airlock_lib import state
from airlock_lib.aws import reraise_if_sso
from airlock_lib.config import Config
from airlock_lib.output import info, warn

TERMINAL = {"SUCCESS", "FAILED", "CANCELLED", "TIMED_OUT"}
# L7: an asynchronous FAILED whose text matches this is a rule rejection.
REJECT_RE = re.compile(r"(?i)analysis.?rule|validat|not allowed|not supported|disallow|invalid|column")
MARKER_SUFFIXES = ("/", "_SUCCESS", ".crc", "$folder$")
FORMAT_EXT = {"CSV": ".csv", "PARQUET": ".parquet"}
RETRY_CODES = {"ServiceQuotaExceededException", "ThrottlingException"}
NESTED_LOG_FIELDS = ("resultReceivers", "queryRunners", "memberDisplayNames", "memberSchemaMapping")
QUERY_LOG = "query_log.jsonl"


def new_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- SQL
def prepare_sql(path: str | Path) -> tuple[str, str, str]:
    """Return (sent_text, file_sha256, sent_sha256).

    Full-line ``--`` comments are dropped, leading/trailing blank lines are stripped, and a
    single trailing ``;`` is removed. Inline ``--`` after code is left alone.
    """
    raw = Path(path).read_bytes()
    lines = [ln.rstrip() for ln in raw.decode("utf-8").splitlines() if not ln.strip().startswith("--")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    sent = "\n".join(lines).rstrip()
    if sent.endswith(";"):
        sent = sent[:-1].rstrip()
    return sent, _sha(raw), _sha(sent.encode("utf-8"))


def classify_failure(code: str | None, message: str | None) -> str:
    return "REJECTED" if REJECT_RE.search(f"{code or ''} {message or ''}") else "FAILED"


# --------------------------------------------------------------------------- results
def filter_result_keys(objects: Iterable[dict], result_format: str) -> list[str]:
    """Drop markers and zero-byte objects; prefer keys with the format's extension (L8)."""
    kept = []
    for o in objects:
        key = o.get("Key", "")
        if not key or key.endswith(MARKER_SUFFIXES) or int(o.get("Size", 0) or 0) == 0:
            continue
        kept.append(key)
    ext = FORMAT_EXT.get(result_format.upper(), "")
    preferred = [k for k in kept if ext and k.lower().endswith(ext)]
    return preferred if preferred else kept


def parse_result_body(body: bytes, result_format: str) -> tuple[list[str], list[dict]]:
    if result_format.upper() == "PARQUET":
        table = pq.read_table(io.BytesIO(body))
        return list(table.column_names), table.to_pylist()
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    rows = [dict(r) for r in reader]
    return list(reader.fieldnames or []), rows


def parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def _list_objects(s3, bucket: str, prefix: str) -> list[dict]:
    objs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objs.extend(page.get("Contents", []))
    return objs


def fetch_results(s3, protected_query: dict, results_bucket: str, key_prefix: str, protected_query_id: str,
                  result_format: str) -> tuple[str, list[str], list[str], list[dict]]:
    """Locate, filter and parse result objects. Returns (location, keys, header, rows)."""
    output = ((protected_query or {}).get("result") or {}).get("output") or {}
    s3_out = output.get("s3") or {}
    if s3_out.get("location"):
        location = s3_out["location"]
        bucket, prefix = parse_s3_uri(location)
        objects = _list_objects(s3, bucket, prefix)
    else:
        warn(f"result output union keys: {list(output.keys())}")
        bucket, prefix = results_bucket, key_prefix
        location = f"s3://{bucket}/{prefix}"
        objects = [o for o in _list_objects(s3, bucket, prefix) if protected_query_id in o.get("Key", "")]
    keys = filter_result_keys(objects, result_format)
    header: list[str] = []
    rows: list[dict] = []
    for key in keys:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        try:
            h, r = parse_result_body(body, result_format)
        except Exception as exc:  # noqa: BLE001 - report and skip unparsable objects
            warn(f"could not parse {key}: {exc}")
            continue
        if not header:
            header = h
        rows.extend(r)
    return location, keys, header, rows


# --------------------------------------------------------------------------- queries
@dataclass
class QueryOutcome:
    label: str
    status: str  # SUCCESS | REJECTED | FAILED | ERROR | TIMEOUT | CANCELLED
    protected_query_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    result_location: str | None = None
    result_keys: list = field(default_factory=list)
    header: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    duration_s: float = 0.0
    file_sha256: str = ""
    sent_sha256: str = ""
    sent_text: str = ""
    run_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _log_outcome(o: QueryOutcome, collaboration_id: str | None, membership_id: str) -> None:
    state.append_jsonl(QUERY_LOG, {
        "ts_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": o.run_id,
        "label": o.label,
        "collaboration_id": collaboration_id,
        "membership_id": membership_id,
        "file_sha256": o.file_sha256,
        "sent_sha256": o.sent_sha256,
        "protected_query_id": o.protected_query_id,
        "status": o.status,
        "error_code": o.error_code,
        "error_message": o.error_message,
        "result_location": o.result_location,
        "result_keys": o.result_keys,
        "row_count": len(o.rows),
    })


def run_protected_query(session, membership_id: str, sql_path: str | Path, label: str, cfg: Config, run_id: str,
                        results_bucket: str, collaboration_id: str | None = None,
                        sleep=time.sleep) -> QueryOutcome:
    """Start one protected SQL query, poll to a terminal state, fetch and parse results."""
    sent, file_sha, sent_sha = prepare_sql(sql_path)
    out = QueryOutcome(label=label, status="ERROR", file_sha256=file_sha, sent_sha256=sent_sha, sent_text=sent,
                       run_id=run_id)
    profile = getattr(session, "airlock_profile", None)
    cr = session.client("cleanrooms")
    kwargs: dict[str, Any] = {"type": "SQL", "membershipIdentifier": membership_id,
                              "sqlParameters": {"queryString": sent}}
    if cfg.query.compute is not None:
        kwargs["computeConfiguration"] = {"worker": {"type": cfg.query.compute.worker_type,
                                                     "number": cfg.query.compute.worker_count}}
    t0 = time.monotonic()
    attempts = 0
    while True:
        try:
            resp = cr.start_protected_query(**kwargs)
            break
        except bce.ClientError as exc:
            reraise_if_sso(exc, profile)
            code = exc.response.get("Error", {}).get("Code", "")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            if code in RETRY_CODES and attempts < 3:
                attempts += 1
                warn(f"{code}: {msg} - retrying in 20 s ({attempts}/3)")
                sleep(20)
                continue
            out.status = "REJECTED" if code == "ValidationException" else "ERROR"
            out.error_code, out.error_message = code, msg
            out.duration_s = round(time.monotonic() - t0, 1)
            _log_outcome(out, collaboration_id, membership_id)
            return out

    qid = resp["protectedQuery"]["id"]
    out.protected_query_id = qid
    info(f"protected query {qid} started ({label})")
    deadline = t0 + cfg.query.timeout_seconds
    last = None
    pq_desc: dict = resp["protectedQuery"]
    while True:
        try:
            pq_desc = cr.get_protected_query(membershipIdentifier=membership_id,
                                             protectedQueryIdentifier=qid)["protectedQuery"]
        except bce.ClientError as exc:
            reraise_if_sso(exc, profile)
            raise
        status = pq_desc["status"]
        if status != last:
            info(f"  status {status} after {time.monotonic() - t0:.0f} s")
            last = status
        if status in TERMINAL:
            break
        if time.monotonic() > deadline:
            try:
                cr.update_protected_query(membershipIdentifier=membership_id, protectedQueryIdentifier=qid,
                                          targetStatus="CANCELLED")
            except bce.ClientError as exc:
                reraise_if_sso(exc, profile)
                warn(f"cancel failed: {exc}")
            status = "TIMEOUT"
            break
        sleep(cfg.query.poll_interval_seconds)

    out.duration_s = round(time.monotonic() - t0, 1)
    if status == "SUCCESS":
        out.status = "SUCCESS"
        s3 = session.client("s3")
        out.result_location, out.result_keys, out.header, out.rows = fetch_results(
            s3, pq_desc, results_bucket, cfg.storage.results_key_prefix, qid, cfg.storage.result_format)
    elif status == "FAILED":
        err = pq_desc.get("error") or {}
        out.error_code, out.error_message = err.get("code"), err.get("message")
        out.status = classify_failure(out.error_code, out.error_message)
    elif status == "TIMED_OUT" or status == "TIMEOUT":
        out.status = "TIMEOUT"
        err = pq_desc.get("error") or {}
        out.error_code, out.error_message = err.get("code") or "TIMEOUT", err.get("message") or "query timed out"
    else:  # CANCELLED
        out.status = "CANCELLED"
        err = pq_desc.get("error") or {}
        out.error_code, out.error_message = err.get("code"), err.get("message")
    _log_outcome(out, collaboration_id, membership_id)
    return out


# --------------------------------------------------------------------------- log warm-up
def ensure_log_warmup(cfg: Config, sessions: dict, deployed, skip: bool = False, sleep=time.sleep,
                      now=utc_now) -> None:
    """Wait until analysis-log delivery is likely set up (AWS: queries run in the first
    minutes after logging is enabled may not send logs). Never fatal."""
    if skip:
        warn("warm-up skipped; logs for these queries may not be delivered")
        return
    a_cr = sessions["data_owner"].client("cleanrooms")
    b_cr = sessions["analyst"].client("cleanrooms")
    ta = a_cr.get_membership(membershipIdentifier=deployed.a_membership_id)["membership"]["createTime"]
    tb = b_cr.get_membership(membershipIdentifier=deployed.b_membership_id)["membership"]["createTime"]
    t_ready = max(ta, tb) + dt.timedelta(seconds=cfg.logging.warmup_seconds)
    while True:
        left = (t_ready - now()).total_seconds()
        if left <= 0:
            break
        print(f"Waiting for analysis-log setup: {int(left)} s left (AWS: logs may not be sent for queries "
              "run just after membership creation)", flush=True)
        sleep(min(15, left))

    logs = sessions["data_owner"].client("logs")
    collab = deployed.collaboration_id
    waited = 0
    while True:
        groups = logs.describe_log_groups(logGroupNamePattern=collab).get("logGroups", [])
        if groups:
            info(f"analysis log group(s) present in Account A: {', '.join(g['logGroupName'] for g in groups)}")
            return
        if waited >= cfg.logging.log_group_wait_seconds:
            warn(f"no analysis log group containing {collab} in Account A yet; logs for these queries may "
                 "not be delivered")
            return
        print(f"Waiting for Account A's analysis log group ({cfg.logging.log_group_wait_seconds - waited} s max)",
              flush=True)
        sleep(15)
        waited += 15


# --------------------------------------------------------------------------- AC5 helpers (L6)
def decode_log_event(message: str) -> dict:
    """json.loads the event, then the nested JSON-string fields wherever they are strings."""
    try:
        ev = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return {"raw": message}
    if not isinstance(ev, dict):
        return {"raw": message}
    for f in NESTED_LOG_FIELDS:
        v = ev.get(f)
        if isinstance(v, str):
            try:
                ev[f] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return ev


def _contains(value: Any, needle: str) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains(k, needle) or _contains(v, needle) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(v, needle) for v in value)
    return needle in str(value)


def log_stage(ev: dict) -> str:
    """SUBMISSION or TERMINATION. Live Spark-collaboration entries carry no ``logStage`` field:
    submission entries (one per configured table: ``schemaName``, ``configuredTableId``, the rule
    policy) have no ``status``; the termination entry carries ``status``."""
    if ev.get("logStage"):
        return str(ev["logStage"])
    return "TERMINATION" if "status" in ev else "SUBMISSION"


def find_ac5_entry(events: Iterable[dict], query_id: str, analyst_account_id: str) -> dict | None:
    """A-side SUBMISSION entry for ``query_id`` whose resultReceivers include the analyst account."""
    for ev in events:
        if (log_stage(ev) == "SUBMISSION" and ev.get("eventId") == query_id
                and _contains(ev.get("resultReceivers"), analyst_account_id)):
            return ev
    return None


def _parse_ts(s: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def select_ac5_target(query_log: list[dict], since_utc: dt.datetime | None,
                      run_id: str | None = None) -> tuple[str, str] | None:
    """(run_id, protected_query_id) of the latest SUCCESS approved/control query.

    With ``run_id`` only that run is considered; otherwise the most recent run inside the
    window (``ts_utc >= since_utc``) that has a SUCCESS approved/control line.
    """
    best = None
    for line in query_log:
        if line.get("label") not in ("approved", "control") or line.get("status") != "SUCCESS":
            continue
        if not line.get("protected_query_id"):
            continue
        ts = _parse_ts(line.get("ts_utc", ""))
        if ts is None:
            continue
        if run_id is not None:
            if line.get("run_id") != run_id:
                continue
        elif since_utc is not None and ts < since_utc:
            continue
        if best is None or ts >= best[0]:
            best = (ts, line["run_id"], line["protected_query_id"])
    return (best[1], best[2]) if best else None


def run_lines(query_log: list[dict], run_id: str) -> list[dict]:
    return [ln for ln in query_log if ln.get("run_id") == run_id]
