# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import datetime as dt
import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from airlock_lib import ROOT
from airlock_lib.cleanrooms import (classify_failure, decode_log_event, fetch_results, filter_result_keys,
                                    find_ac5_entry, log_stage, parse_result_body, prepare_sql,
                                    select_ac5_target)


# --------------------------------------------------------------------------- prepare_sql
def test_prepare_sql_strips_comment_lines_and_semicolon(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text("-- header\n\n  -- indented comment\nSELECT a -- inline stays\nFROM t;\n\n")
    sent, fsha, ssha = prepare_sql(p)
    assert sent == "SELECT a -- inline stays\nFROM t"
    assert fsha != ssha


def test_prepare_sql_real_files():
    sent, fsha, ssha = prepare_sql(ROOT / "queries/approved/mean_hba1c_by_medication_class.sql")
    assert sent.splitlines()[0] == "SELECT"
    assert "--" not in sent and fsha != ssha
    sent2, _, _ = prepare_sql(ROOT / "queries/blocked/02_row_level_select_star.sql")
    assert sent2 == "SELECT * FROM patients"


# --------------------------------------------------------------------------- classify (L7)
@pytest.mark.parametrize("code,msg", [
    ("", "Column(s) [patient_id] is not allowed in select"),
    ("ValidationException", "bad"),
    ("", "Query violates the analysis rule"),
    ("", "Function not supported"),
    ("", "Disallowed output"),
    ("", "Invalid query"),
])
def test_classify_rejected(code, msg):
    assert classify_failure(code, msg) == "REJECTED"


def test_classify_failed():
    assert classify_failure("InternalError", "Internal error") == "FAILED"
    assert classify_failure(None, None) == "FAILED"


# --------------------------------------------------------------------------- result keys (L8)
def test_filter_markers_and_zero_byte():
    objs = [{"Key": "r/q1/", "Size": 0}, {"Key": "r/q1/_SUCCESS", "Size": 5}, {"Key": "r/q1/x.crc", "Size": 3},
            {"Key": "r/q1$folder$", "Size": 3}, {"Key": "r/q1/empty.csv", "Size": 0},
            {"Key": "r/q1/result", "Size": 10}]
    assert filter_result_keys(objs, "CSV") == ["r/q1/result"]


def test_filter_prefers_format_extension():
    objs = [{"Key": "r/q1/manifest.json", "Size": 10}, {"Key": "r/q1/part-0.CSV", "Size": 10}]
    assert filter_result_keys(objs, "CSV") == ["r/q1/part-0.CSV"]
    objs = [{"Key": "r/q1/a.parquet", "Size": 10}, {"Key": "r/q1/b.csv", "Size": 10}]
    assert filter_result_keys(objs, "PARQUET") == ["r/q1/a.parquet"]


def test_filter_falls_back_to_all_keys():
    objs = [{"Key": "r/q1/part-00000", "Size": 10}, {"Key": "r/q1/part-00001", "Size": 10}]
    assert filter_result_keys(objs, "CSV") == ["r/q1/part-00000", "r/q1/part-00001"]


# --------------------------------------------------------------------------- parsing
def test_parse_csv_with_bom():
    body = "\ufeffmedication_class,patient_count\nGLP-1,700\n".encode("utf-8")
    header, rows = parse_result_body(body, "CSV")
    assert header == ["medication_class", "patient_count"]
    assert rows == [{"medication_class": "GLP-1", "patient_count": "700"}]


def _parquet_bytes(rows):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf)
    return buf.getvalue()


def test_parse_parquet():
    header, rows = parse_result_body(_parquet_bytes([{"medication_class": "SGLT2", "patient_count": 650}]), "PARQUET")
    assert header == ["medication_class", "patient_count"]
    assert rows == [{"medication_class": "SGLT2", "patient_count": 650}]


class _Body:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d


class _Paginator:
    def __init__(self, s3):
        self.s3 = s3

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k, "Size": len(v)} for (b, k), v in self.s3.objects.items()
                            if b == Bucket and k.startswith(Prefix)]}


class StubS3:
    def __init__(self, objects):
        self.objects = objects  # {(bucket, key): bytes}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self)

    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.objects[(Bucket, Key)])}


CSV = b"medication_class,patient_count\nGLP-1,700\nSGLT2,650\n"


def test_fetch_results_with_location():
    s3 = StubS3({("res", "results/q1/result.csv"): CSV, ("res", "results/q1/_SUCCESS"): b"x",
                 ("res", "results/q2/result.csv"): CSV})
    pq_desc = {"result": {"output": {"s3": {"location": "s3://res/results/q1/"}}}}
    loc, keys, header, rows = fetch_results(s3, pq_desc, "res", "results/", "q1", "CSV")
    assert loc == "s3://res/results/q1/"
    assert keys == ["results/q1/result.csv"]
    assert header == ["medication_class", "patient_count"] and len(rows) == 2


def test_fetch_results_without_s3_output_uses_prefix_and_id(capsys):
    s3 = StubS3({("res", "results/q1-abc/result.csv"): CSV, ("res", "results/other/result.csv"): CSV})
    loc, keys, header, rows = fetch_results(s3, {"result": {"output": {"memberList": []}}}, "res", "results/",
                                            "q1-abc", "CSV")
    assert keys == ["results/q1-abc/result.csv"] and len(rows) == 2
    assert "result output union keys" in capsys.readouterr().err


def test_fetch_results_skips_unparsable(capsys):
    s3 = StubS3({("res", "results/q1/a.parquet"): b"not parquet", ("res", "results/q1/b.parquet"):
                 _parquet_bytes([{"medication_class": "DPP-4", "patient_count": 488}])})
    pq_desc = {"result": {"output": {"s3": {"location": "s3://res/results/q1"}}}}
    _, keys, header, rows = fetch_results(s3, pq_desc, "res", "results/", "q1", "PARQUET")
    assert len(keys) == 2 and rows == [{"medication_class": "DPP-4", "patient_count": 488}]
    assert "could not parse results/q1/a.parquet" in capsys.readouterr().err


# --------------------------------------------------------------------------- AC5 (L6)
def test_decode_nested_json_fields():
    msg = json.dumps({"eventId": "q1", "logStage": "SUBMISSION", "resultReceivers": json.dumps(["444455556666"]),
                      "queryRunners": json.dumps(["444455556666"]),
                      "memberDisplayNames": json.dumps({"444455556666": "State"}), "queryText": "SELECT"})
    ev = decode_log_event(msg)
    assert ev["resultReceivers"] == ["444455556666"]
    assert ev["memberDisplayNames"] == {"444455556666": "State"}
    assert decode_log_event("not json") == {"raw": "not json"}


def test_find_ac5_entry_live_format():
    """Live Spark entries have no logStage: submission = per-table entry without status."""
    sub = decode_log_event(json.dumps({"eventId": "q1", "schemaName": "outcomes", "configuredTableId": "ct",
                                       "resultReceivers": "444455556666", "queryRunners": "444455556666"}))
    term = {"eventId": "q1", "status": "SUCCESS", "resultReceivers": "444455556666"}
    assert log_stage(sub) == "SUBMISSION" and log_stage(term) == "TERMINATION"
    assert find_ac5_entry([term], "q1", "444455556666") is None
    assert find_ac5_entry([term, sub], "q1", "444455556666") is sub
    assert find_ac5_entry([sub], "q1", "777788889999") is None


EVENTS = [
    {"eventId": "q1", "logStage": "TERMINATION", "resultReceivers": ["444455556666"]},
    {"eventId": "q2", "logStage": "SUBMISSION", "resultReceivers": ["444455556666"]},
    {"eventId": "q1", "logStage": "SUBMISSION", "resultReceivers": ["777788889999"]},
]


def test_find_ac5_entry():
    assert find_ac5_entry(EVENTS + [{"eventId": "q1", "logStage": "SUBMISSION",
                                     "resultReceivers": ["444455556666"]}], "q1", "444455556666") is not None
    assert find_ac5_entry(EVENTS, "q1", "444455556666") is None  # wrong stage / receiver missing
    assert find_ac5_entry(EVENTS, "q3", "444455556666") is None  # wrong eventId


def _line(ts, run, label, status, qid):
    return {"ts_utc": ts, "run_id": run, "label": label, "status": status, "protected_query_id": qid}


QLOG = [
    _line("2026-10-21T10:00:00Z", "R1", "approved", "SUCCESS", "a1"),
    _line("2026-10-21T11:00:00Z", "R2", "control", "SUCCESS", "c2"),
    _line("2026-10-21T11:01:00Z", "R2", "blocked:select_patient_id", "REJECTED", None),
    _line("2026-10-21T12:00:00Z", "R3", "approved", "FAILED", "a3"),
    _line("2026-10-21T12:05:00Z", "R3", "blocked:row_level_select", "REJECTED", None),
]


def test_select_ac5_target_latest_success_in_window():
    since = dt.datetime(2026, 10, 21, 9, 0, tzinfo=dt.timezone.utc)
    assert select_ac5_target(QLOG, since) == ("R2", "c2")


def test_select_ac5_target_window_excludes_old():
    since = dt.datetime(2026, 10, 21, 11, 30, tzinfo=dt.timezone.utc)
    assert select_ac5_target(QLOG, since) is None


def test_select_ac5_target_explicit_run():
    assert select_ac5_target(QLOG, None, run_id="R1") == ("R1", "a1")
    assert select_ac5_target(QLOG, None, run_id="R3") is None
