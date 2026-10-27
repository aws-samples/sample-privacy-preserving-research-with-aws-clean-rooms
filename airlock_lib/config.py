# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Load and validate ``config/airlock.yaml`` into frozen dataclasses.

Path rule (L9): every relative path in the config (``data.files_dir``,
``research.approved_query_file``, ``research.blocked_queries[*].file``) resolves
against the project root ``ROOT = Path(airlock_lib.__file__).resolve().parent.parent``
(the repository root directory), never against the current working directory or the
config file's own location. Absolute paths and paths that escape ROOT are rejected.

Config file precedence: ``--config PATH`` > ``$AIRLOCK_CONFIG`` > ``ROOT/config/airlock.yaml``.

Every validation failure raises ``ConfigError("<dotted.path>: <reason>")``. Unknown keys
are rejected so typos (and removed options) fail loudly.
"""
from __future__ import annotations

import copy
import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from airlock_lib import ROOT

DEFAULT_CONFIG = ROOT / "config" / "airlock.yaml"
GENERATED_DIR = "data/generated"

GLUE_TYPES = {"string", "int", "bigint", "double", "date", "timestamp", "boolean"}
AGG_FUNCTIONS = {"COUNT", "COUNT_DISTINCT", "AVG", "SUM", "SUM_DISTINCT"}
SCALAR_FUNCTIONS = {
    "ABS", "CAST", "CEILING", "COALESCE", "CONVERT", "CURRENT_DATE", "DATEADD", "EXTRACT",
    "FLOOR", "GETDATE", "LN", "LOG", "LOWER", "ROUND", "RTRIM", "SQRT", "SUBSTRING",
    "TO_CHAR", "TO_DATE", "TO_NUMBER", "TO_TIMESTAMP", "TRIM", "TRUNC", "UPPER",
}
ANALYST_ABILITIES = {"CAN_QUERY", "CAN_RECEIVE_RESULTS"}
# CR.8X exists in the API but is deliberately excluded for the MVP (L12).
WORKER_LIMITS = {"CR.1X": (2, 128), "CR.4X": (2, 32)}
MEMBERS = ("data_owner", "analyst")

RE_ACCOUNT = re.compile(r"^\d{12}$")
RE_REGION = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
RE_BUCKET_PREFIX = re.compile(r"^[a-z0-9-]+$")
RE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
RE_KEY_PREFIX = re.compile(r"^[\w!.=*/-]{0,512}$")
RE_GLUE_DB = re.compile(r"^[a-z0-9_]{1,128}$")
RE_GLUE_TABLE = re.compile(r"^[a-z0-9_]{1,255}$")
RE_S3_PREFIX = re.compile(r"^[a-z0-9_-]+/$")
RE_COLUMN = re.compile(r"^[a-z0-9_]+$")
RE_IDENT = re.compile(r"^[a-zA-Z0-9_]+$")
RE_TABLE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
RE_STACK = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
RE_QUERY_ID = re.compile(r"^[a-z0-9_]+$")
RE_ID_PREFIX = re.compile(r"^[A-Za-z0-9-]{1,16}$")


class ConfigError(Exception):
    """Invalid configuration. The message is ``<dotted.path>: <reason>``."""


# --------------------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class Project:
    name: str
    tags: dict


@dataclass(frozen=True)
class Account:
    account_id: str
    profile: str
    display_name: str
    stack_name: str


@dataclass(frozen=True)
class Accounts:
    data_owner: Account
    analyst: Account


@dataclass(frozen=True)
class Collaboration:
    name: str
    description: str
    analytics_engine: str
    query_log_status: str
    creator_member_abilities: tuple
    analyst_member_abilities: tuple
    query_compute_payer: str


@dataclass(frozen=True)
class Storage:
    data_bucket_prefix: str
    results_bucket_prefix: str
    results_key_prefix: str
    result_format: str
    single_file_output: bool


@dataclass(frozen=True)
class Glue:
    database_name: str


@dataclass(frozen=True)
class Privacy:
    identity_column: str
    min_distinct_patients: int


@dataclass(frozen=True)
class Security:
    pin_data_role_trust_to_query_runner: bool


@dataclass(frozen=True)
class Logging:
    warmup_seconds: int
    log_group_wait_seconds: int


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class AggregateColumn:
    columns: tuple
    function: str


@dataclass(frozen=True)
class AnalysisRuleCfg:
    join_columns: tuple
    dimension_columns: tuple
    aggregate_columns: tuple  # of AggregateColumn
    scalar_functions: tuple


@dataclass(frozen=True)
class Table:
    key: str
    glue_table_name: str
    s3_prefix: str
    configured_table_name: str
    association_name: str
    description: str
    columns: tuple  # of Column
    allowed_columns: tuple
    analysis_rule: AnalysisRuleCfg

    @property
    def column_names(self) -> tuple:
        return tuple(c.name for c in self.columns)

    @property
    def construct_suffix(self) -> str:
        return self.key.title().replace("_", "")


@dataclass(frozen=True)
class BlockedQuery:
    id: str
    title: str
    file: str
    expect: str


@dataclass(frozen=True)
class DerivedDifference:
    name: str
    minuend: str
    subtrahend: str


@dataclass(frozen=True)
class Research:
    question: str
    approved_query_file: str
    group_by_column: str
    count_column: str
    derived_difference: DerivedDifference | None
    blocked_queries: tuple  # of BlockedQuery


@dataclass(frozen=True)
class Compute:
    worker_type: str
    worker_count: int


@dataclass(frozen=True)
class Query:
    compute: Compute | None
    poll_interval_seconds: int
    timeout_seconds: int


@dataclass(frozen=True)
class Dist:
    mean: float
    sd: float
    min: float
    max: float


@dataclass(frozen=True)
class MedicationClass:
    name: str
    count: int
    mean_change: float
    sd_change: float


@dataclass(frozen=True)
class GeneratorCfg:
    seed: int
    patient_count: int
    id_prefix: str
    age: Dist
    sex_weights: dict
    region_weights: dict
    diagnosis_weights: dict
    procedure_weights: dict
    enrollment_start: dt.date
    enrollment_days: int
    followup_days_min: int
    followup_days_max: int
    baseline_hba1c: Dist
    followup_clip_min: float
    followup_clip_max: float
    improved_max_delta: float
    worsened_min_delta: float
    medication_classes: tuple  # of MedicationClass


@dataclass(frozen=True)
class Data:
    source: str
    files_dir: str
    generator: GeneratorCfg | None


@dataclass(frozen=True)
class Config:
    project: Project
    region: str
    accounts: Accounts
    collaboration: Collaboration
    storage: Storage
    glue: Glue
    privacy: Privacy
    security: Security
    logging: Logging
    tables: tuple  # of Table
    research: Research
    query: Query
    data: Data
    raw: dict = field(repr=False, compare=False)
    path: Path | None = field(default=None, compare=False)

    # ---- derived values
    @property
    def data_bucket_name(self) -> str:
        return f"{self.storage.data_bucket_prefix}-{self.accounts.data_owner.account_id}-{self.region}"

    @property
    def results_bucket_name(self) -> str:
        return f"{self.storage.results_bucket_prefix}-{self.accounts.analyst.account_id}-{self.region}"

    def member(self, name: str) -> Account:
        if name not in MEMBERS:
            raise KeyError(name)
        return getattr(self.accounts, name)

    def payer_flags(self) -> dict:
        return {m: (m == self.collaboration.query_compute_payer) for m in MEMBERS}

    def table(self, key: str) -> Table:
        for t in self.tables:
            if t.key == key:
                return t
        raise KeyError(key)

    @property
    def files_dir_path(self) -> Path:
        return resolve(self.data.files_dir, "data.files_dir")

    @property
    def approved_query_path(self) -> Path:
        return resolve(self.research.approved_query_file, "research.approved_query_file")

    def blocked_query_path(self, query_id: str) -> Path:
        for q in self.research.blocked_queries:
            if q.id == query_id:
                return resolve(q.file, f"research.blocked_queries.{q.id}.file")
        raise KeyError(query_id)

    @property
    def generated_dir(self) -> Path:
        return ROOT / GENERATED_DIR


# --------------------------------------------------------------------------- path helpers
def resolve(rel: Any, path: str = "path") -> Path:
    """Resolve a config-relative path against ROOT (L9). Rejects absolute or escaping paths."""
    if not isinstance(rel, str) or not rel.strip():
        raise ConfigError(f"{path}: must be a non-empty relative path")
    if os.path.isabs(rel):
        raise ConfigError(f"{path}: must be relative to the project root, got absolute {rel!r}")
    p = (ROOT / rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ConfigError(f"{path}: resolves outside the project root ({p})")
    return p


def config_path(cli: str | None = None) -> Path:
    """Config file precedence: CLI flag > $AIRLOCK_CONFIG > ROOT/config/airlock.yaml."""
    if cli:
        return Path(cli).expanduser().resolve()
    env = os.environ.get("AIRLOCK_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_CONFIG


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path).expanduser().resolve() if path else config_path(None)
    if not p.is_file():
        raise ConfigError(f"config: file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: invalid YAML in {p}: {exc}") from exc
    return load_config_dict(raw, path=p)


# --------------------------------------------------------------------------- primitive checks
def _map(d: Any, path: str, required: set, optional: set = frozenset()) -> dict:
    if not isinstance(d, dict):
        raise ConfigError(f"{path or 'config'}: must be a mapping")
    for k in d:
        if k not in required and k not in optional:
            raise ConfigError(f"{_join(path, k)}: unknown key")
    for k in sorted(required):
        if k not in d:
            raise ConfigError(f"{_join(path, k)}: required key missing")
    return d


def _join(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _str(v: Any, path: str, *, min_len: int = 1, max_len: int | None = None,
         pattern: re.Pattern | None = None, not_blank: bool = False) -> str:
    if not isinstance(v, str):
        raise ConfigError(f"{path}: must be a string")
    if len(v) < min_len:
        raise ConfigError(f"{path}: must be at least {min_len} characters")
    if max_len is not None and len(v) > max_len:
        raise ConfigError(f"{path}: must be at most {max_len} characters (got {len(v)})")
    if not_blank and not v.strip():
        raise ConfigError(f"{path}: must not be blank or whitespace-only")
    if pattern is not None and not pattern.match(v):
        raise ConfigError(f"{path}: {v!r} does not match {pattern.pattern}")
    return v


def _int(v: Any, path: str, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{path}: must be an integer")
    if lo is not None and v < lo or hi is not None and v > hi:
        raise ConfigError(f"{path}: must be in {lo}..{hi} (got {v})")
    return v


def _num(v: Any, path: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{path}: must be a number")
    return float(v)


def _bool(v: Any, path: str) -> bool:
    if not isinstance(v, bool):
        raise ConfigError(f"{path}: must be true or false")
    return v


def _list(v: Any, path: str, *, min_items: int = 0, max_items: int | None = None) -> list:
    if not isinstance(v, list):
        raise ConfigError(f"{path}: must be a list")
    if len(v) < min_items:
        raise ConfigError(f"{path}: must have at least {min_items} item(s)")
    if max_items is not None and len(v) > max_items:
        raise ConfigError(f"{path}: must have at most {max_items} items")
    return v


def _str_list(v: Any, path: str, *, min_items: int = 0, max_items: int | None = None,
              pattern: re.Pattern | None = None, choices: set | None = None) -> tuple:
    items = _list(v, path, min_items=min_items, max_items=max_items)
    out = []
    for i, item in enumerate(items):
        s = _str(item, f"{path}[{i}]", pattern=pattern)
        if choices is not None and s not in choices:
            raise ConfigError(f"{path}[{i}]: {s!r} not in {sorted(choices)}")
        out.append(s)
    if len(set(out)) != len(out):
        raise ConfigError(f"{path}: duplicate entries")
    return tuple(out)


def _choice(v: Any, path: str, choices: set, reason: str = "") -> str:
    if not isinstance(v, str) or v not in choices:
        extra = f" ({reason})" if reason else ""
        raise ConfigError(f"{path}: must be one of {sorted(choices)}{extra}, got {v!r}")
    return v


def _weights(v: Any, path: str) -> dict:
    if not isinstance(v, dict) or not v:
        raise ConfigError(f"{path}: must be a non-empty mapping of value -> weight")
    out = {}
    for k, w in v.items():
        if not isinstance(k, str) or not k:
            raise ConfigError(f"{path}: keys must be non-empty strings")
        if _num(w, f"{path}.{k}") <= 0:
            raise ConfigError(f"{path}.{k}: weight must be positive")
        out[k] = float(w)
    return out


def _dist(v: Any, path: str) -> Dist:
    d = _map(v, path, {"mean", "sd", "min", "max"})
    dist = Dist(*(_num(d[k], f"{path}.{k}") for k in ("mean", "sd", "min", "max")))
    if dist.sd < 0:
        raise ConfigError(f"{path}.sd: must be >= 0")
    if dist.min > dist.max:
        raise ConfigError(f"{path}: min must be <= max")
    return dist


def _under(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


# --------------------------------------------------------------------------- section parsers
def _accounts(raw: dict) -> Accounts:
    d = _map(raw, "accounts", set(MEMBERS))
    accts = {}
    for m in MEMBERS:
        p = f"accounts.{m}"
        a = _map(d[m], p, {"account_id", "profile", "display_name", "stack_name"})
        accts[m] = Account(
            account_id=_str(a["account_id"], f"{p}.account_id", pattern=RE_ACCOUNT),
            profile=_str(a["profile"], f"{p}.profile", not_blank=True),
            display_name=_str(a["display_name"], f"{p}.display_name", max_len=100, not_blank=True),
            stack_name=_str(a["stack_name"], f"{p}.stack_name", pattern=RE_STACK),
        )
    if accts["data_owner"].account_id == accts["analyst"].account_id:
        raise ConfigError("accounts.analyst.account_id: must differ from accounts.data_owner.account_id")
    return Accounts(**accts)


def _collaboration(raw: dict) -> Collaboration:
    p = "collaboration"
    d = _map(raw, p, {"name", "description", "analytics_engine", "query_log_status",
                      "creator_member_abilities", "analyst_member_abilities", "query_compute_payer"})
    creator = _str_list(d["creator_member_abilities"], f"{p}.creator_member_abilities")
    if creator:
        raise ConfigError(f"{p}.creator_member_abilities: must be [] in the MVP "
                          "(Institution A contributes data only)")
    analyst = _str_list(d["analyst_member_abilities"], f"{p}.analyst_member_abilities",
                        min_items=1, choices=ANALYST_ABILITIES)
    if "CAN_QUERY" not in analyst:
        raise ConfigError(f"{p}.analyst_member_abilities: must include CAN_QUERY")
    return Collaboration(
        name=_str(d["name"], f"{p}.name", max_len=100, not_blank=True),
        # L4: CloudFormation requires a non-blank description (1..255).
        description=_str(d["description"], f"{p}.description", max_len=255, not_blank=True),
        analytics_engine=_choice(d["analytics_engine"], f"{p}.analytics_engine", {"SPARK"},
                                 "Clean Rooms SQL support ended Dec 17, 2025"),
        query_log_status=_choice(d["query_log_status"], f"{p}.query_log_status", {"ENABLED", "DISABLED"}),
        creator_member_abilities=creator,
        analyst_member_abilities=analyst,
        query_compute_payer=_choice(d["query_compute_payer"], f"{p}.query_compute_payer", {"analyst"},
                                    "MVP: the analyst pays for query compute"),
    )


def _storage(raw: dict, accounts: Accounts, region: str) -> Storage:
    p = "storage"
    d = _map(raw, p, {"data_bucket_prefix", "results_bucket_prefix", "results_key_prefix",
                      "result_format", "single_file_output"})
    s = Storage(
        data_bucket_prefix=_str(d["data_bucket_prefix"], f"{p}.data_bucket_prefix", pattern=RE_BUCKET_PREFIX),
        results_bucket_prefix=_str(d["results_bucket_prefix"], f"{p}.results_bucket_prefix",
                                   pattern=RE_BUCKET_PREFIX),
        results_key_prefix=_str(d["results_key_prefix"], f"{p}.results_key_prefix", min_len=0,
                                pattern=RE_KEY_PREFIX),
        result_format=_choice(d["result_format"], f"{p}.result_format", {"CSV", "PARQUET"}),
        single_file_output=_bool(d["single_file_output"], f"{p}.single_file_output"),
    )
    for key, prefix, acct in (("data_bucket_prefix", s.data_bucket_prefix, accounts.data_owner.account_id),
                              ("results_bucket_prefix", s.results_bucket_prefix, accounts.analyst.account_id)):
        bucket = f"{prefix}-{acct}-{region}"
        if not RE_BUCKET.match(bucket):
            raise ConfigError(f"{p}.{key}: derived bucket name {bucket!r} is not a valid S3 bucket name")
    return s


def _analysis_rule(raw: dict, path: str, allowed: tuple) -> AnalysisRuleCfg:
    d = _map(raw, path, {"join_columns", "dimension_columns", "aggregate_columns", "scalar_functions"})
    join = _str_list(d["join_columns"], f"{path}.join_columns", pattern=RE_COLUMN)
    dims = _str_list(d["dimension_columns"], f"{path}.dimension_columns", pattern=RE_COLUMN)
    aggs = []
    for i, a in enumerate(_list(d["aggregate_columns"], f"{path}.aggregate_columns", min_items=1)):
        ap = f"{path}.aggregate_columns[{i}]"
        a = _map(a, ap, {"columns", "function"})
        aggs.append(AggregateColumn(
            columns=_str_list(a["columns"], f"{ap}.columns", min_items=1, pattern=RE_COLUMN),
            function=_choice(a["function"], f"{ap}.function", AGG_FUNCTIONS),
        ))
    scalars = _str_list(d["scalar_functions"], f"{path}.scalar_functions", choices=SCALAR_FUNCTIONS)
    for name, cols in (("join_columns", join), ("dimension_columns", dims)):
        for c in cols:
            if c not in allowed:
                raise ConfigError(f"{path}.{name}: column {c!r} is not in allowed_columns")
    agg_cols = {c for a in aggs for c in a.columns}
    for c in sorted(agg_cols):
        if c not in allowed:
            raise ConfigError(f"{path}.aggregate_columns: column {c!r} is not in allowed_columns")
    if set(join) & set(dims):
        raise ConfigError(f"{path}: join_columns and dimension_columns must be disjoint "
                          f"(both contain {sorted(set(join) & set(dims))})")
    if agg_cols & set(dims):
        raise ConfigError(f"{path}: aggregate columns and dimension_columns must be disjoint "
                          f"(both contain {sorted(agg_cols & set(dims))})")
    return AnalysisRuleCfg(join_columns=join, dimension_columns=dims, aggregate_columns=tuple(aggs),
                           scalar_functions=scalars)


def _tables(raw: Any) -> tuple:
    items = _list(raw, "tables", min_items=1)
    tables, keys, prefixes, ct_names, assoc_names = [], set(), set(), set(), set()
    for i, t in enumerate(items):
        if isinstance(t, dict) and isinstance(t.get("key"), str) and RE_TABLE_KEY.match(t["key"]):
            p = f"tables.{t['key']}"
        else:
            p = f"tables[{i}]"
        d = _map(t, p, {"key", "glue_table_name", "s3_prefix", "configured_table_name", "association_name",
                        "description", "columns", "allowed_columns", "analysis_rule"})
        key = _str(d["key"], f"{p}.key", pattern=RE_TABLE_KEY)
        cols = []
        for j, c in enumerate(_list(d["columns"], f"{p}.columns", min_items=1)):
            cp = f"{p}.columns[{j}]"
            c = _map(c, cp, {"name", "type"})
            cols.append(Column(name=_str(c["name"], f"{cp}.name", pattern=RE_COLUMN),
                               type=_choice(c["type"], f"{cp}.type", GLUE_TYPES)))
        names = [c.name for c in cols]
        if len(set(names)) != len(names):
            raise ConfigError(f"{p}.columns: duplicate column names")
        allowed = _str_list(d["allowed_columns"], f"{p}.allowed_columns", min_items=1, max_items=100,
                            pattern=RE_COLUMN)
        for c in allowed:
            if c not in names:
                raise ConfigError(f"{p}.allowed_columns: {c!r} is not one of the table's columns")
        table = Table(
            key=key,
            glue_table_name=_str(d["glue_table_name"], f"{p}.glue_table_name", pattern=RE_GLUE_TABLE),
            s3_prefix=_str(d["s3_prefix"], f"{p}.s3_prefix", pattern=RE_S3_PREFIX),
            configured_table_name=_str(d["configured_table_name"], f"{p}.configured_table_name",
                                       max_len=100, not_blank=True),
            association_name=_str(d["association_name"], f"{p}.association_name", max_len=128,
                                  pattern=RE_IDENT),
            # L4: ConfiguredTable.Description is optional, 0..255.
            description=_str(d["description"], f"{p}.description", min_len=0, max_len=255),
            columns=tuple(cols),
            allowed_columns=allowed,
            analysis_rule=_analysis_rule(d["analysis_rule"], f"{p}.analysis_rule", allowed),
        )
        for seen, val, what in ((keys, table.key, "key"), (prefixes, table.s3_prefix, "s3_prefix"),
                                (ct_names, table.configured_table_name, "configured_table_name"),
                                (assoc_names, table.association_name, "association_name")):
            if val in seen:
                raise ConfigError(f"{p}.{what}: duplicate value {val!r}")
            seen.add(val)
        tables.append(table)
    return tuple(tables)


def _research(raw: dict) -> Research:
    p = "research"
    d = _map(raw, p, {"question", "approved_query_file", "group_by_column", "count_column",
                      "derived_difference", "blocked_queries"})
    approved = _str(d["approved_query_file"], f"{p}.approved_query_file")
    if not resolve(approved, f"{p}.approved_query_file").is_file():
        raise ConfigError(f"{p}.approved_query_file: file not found: {resolve(approved)}")
    dd = None
    if d["derived_difference"] is not None:
        ddp = f"{p}.derived_difference"
        x = _map(d["derived_difference"], ddp, {"name", "minuend", "subtrahend"})
        dd = DerivedDifference(**{k: _str(x[k], f"{ddp}.{k}", pattern=RE_IDENT)
                                  for k in ("name", "minuend", "subtrahend")})
    blocked, ids = [], set()
    for i, b in enumerate(_list(d["blocked_queries"], f"{p}.blocked_queries")):
        bp = f"{p}.blocked_queries[{i}]"
        b = _map(b, bp, {"id", "title", "file", "expect"})
        q = BlockedQuery(
            id=_str(b["id"], f"{bp}.id", pattern=RE_QUERY_ID),
            title=_str(b["title"], f"{bp}.title", not_blank=True),
            file=_str(b["file"], f"{bp}.file"),
            expect=_choice(b["expect"], f"{bp}.expect", {"REJECTED", "NO_ROWS"}),
        )
        if q.id in ids:
            raise ConfigError(f"{bp}.id: duplicate id {q.id!r}")
        ids.add(q.id)
        if not resolve(q.file, f"{bp}.file").is_file():
            raise ConfigError(f"{bp}.file: file not found: {resolve(q.file)}")
        blocked.append(q)
    return Research(
        question=_str(d["question"], f"{p}.question", not_blank=True),
        approved_query_file=approved,
        group_by_column=_str(d["group_by_column"], f"{p}.group_by_column", pattern=RE_IDENT),
        count_column=_str(d["count_column"], f"{p}.count_column", pattern=RE_IDENT),
        derived_difference=dd,
        blocked_queries=tuple(blocked),
    )


def _query(raw: dict) -> Query:
    p = "query"
    d = _map(raw, p, {"compute", "poll_interval_seconds", "timeout_seconds"})
    compute = None
    if d["compute"] is not None:
        c = _map(d["compute"], f"{p}.compute", {"worker_type", "worker_count"})
        wt = c["worker_type"]
        if wt not in WORKER_LIMITS:
            raise ConfigError(f"{p}.compute.worker_type: must be one of {sorted(WORKER_LIMITS)} "
                              f"(CR.8X is deliberately excluded for the MVP), got {wt!r}")
        lo, hi = WORKER_LIMITS[wt]
        compute = Compute(worker_type=wt, worker_count=_int(c["worker_count"], f"{p}.compute.worker_count", lo, hi))
    return Query(
        compute=compute,
        poll_interval_seconds=_int(d["poll_interval_seconds"], f"{p}.poll_interval_seconds", 1, 60),
        timeout_seconds=_int(d["timeout_seconds"], f"{p}.timeout_seconds", 60, 7200),
    )


def _generator(raw: dict) -> GeneratorCfg:
    p = "data.generator"
    d = _map(raw, p, {"seed", "patient_count", "id_prefix", "age", "sex_weights", "region_weights",
                      "diagnosis_weights", "procedure_weights", "enrollment_window", "followup_days",
                      "baseline_hba1c", "followup_clip", "outcome_thresholds", "medication_classes"})
    patient_count = _int(d["patient_count"], f"{p}.patient_count", 1, 999999)
    age = _dist(d["age"], f"{p}.age")
    if age.min < 18:
        raise ConfigError(f"{p}.age.min: adults only, must be >= 18")
    ew = _map(d["enrollment_window"], f"{p}.enrollment_window", {"start", "days"})
    start = ew["start"]
    if isinstance(start, str):
        try:
            start = dt.date.fromisoformat(start)
        except ValueError as exc:
            raise ConfigError(f"{p}.enrollment_window.start: must be an ISO date (YYYY-MM-DD)") from exc
    if not isinstance(start, dt.date) or isinstance(start, dt.datetime):
        raise ConfigError(f"{p}.enrollment_window.start: must be an ISO date (YYYY-MM-DD)")
    fd = _map(d["followup_days"], f"{p}.followup_days", {"min", "max"})
    fd_min = _int(fd["min"], f"{p}.followup_days.min", 0, 3650)
    fd_max = _int(fd["max"], f"{p}.followup_days.max", 0, 3650)
    if fd_min > fd_max:
        raise ConfigError(f"{p}.followup_days: min must be <= max")
    fc = _map(d["followup_clip"], f"{p}.followup_clip", {"min", "max"})
    fc_min, fc_max = _num(fc["min"], f"{p}.followup_clip.min"), _num(fc["max"], f"{p}.followup_clip.max")
    if fc_min >= fc_max:
        raise ConfigError(f"{p}.followup_clip: min must be < max")
    ot = _map(d["outcome_thresholds"], f"{p}.outcome_thresholds", {"improved_max_delta", "worsened_min_delta"})
    imp = _num(ot["improved_max_delta"], f"{p}.outcome_thresholds.improved_max_delta")
    wor = _num(ot["worsened_min_delta"], f"{p}.outcome_thresholds.worsened_min_delta")
    if imp >= wor:
        raise ConfigError(f"{p}.outcome_thresholds: improved_max_delta must be < worsened_min_delta")
    classes, names = [], set()
    for i, m in enumerate(_list(d["medication_classes"], f"{p}.medication_classes", min_items=1)):
        mp = f"{p}.medication_classes[{i}]"
        m = _map(m, mp, {"name", "count", "mean_change", "sd_change"})
        mc = MedicationClass(name=_str(m["name"], f"{mp}.name", not_blank=True),
                             count=_int(m["count"], f"{mp}.count", 1, 999999),
                             mean_change=_num(m["mean_change"], f"{mp}.mean_change"),
                             sd_change=_num(m["sd_change"], f"{mp}.sd_change"))
        if mc.sd_change < 0:
            raise ConfigError(f"{mp}.sd_change: must be >= 0")
        if mc.name in names:
            raise ConfigError(f"{mp}.name: duplicate class {mc.name!r}")
        names.add(mc.name)
        classes.append(mc)
    total = sum(c.count for c in classes)
    if total != patient_count:
        raise ConfigError(f"{p}.medication_classes: class counts sum to {total}, "
                          f"must equal patient_count {patient_count}")
    return GeneratorCfg(
        seed=_int(d["seed"], f"{p}.seed"),
        patient_count=patient_count,
        id_prefix=_str(d["id_prefix"], f"{p}.id_prefix", pattern=RE_ID_PREFIX),
        age=age,
        sex_weights=_weights(d["sex_weights"], f"{p}.sex_weights"),
        region_weights=_weights(d["region_weights"], f"{p}.region_weights"),
        diagnosis_weights=_weights(d["diagnosis_weights"], f"{p}.diagnosis_weights"),
        procedure_weights=_weights(d["procedure_weights"], f"{p}.procedure_weights"),
        enrollment_start=start,
        enrollment_days=_int(ew["days"], f"{p}.enrollment_window.days", 1, 36500),
        followup_days_min=fd_min,
        followup_days_max=fd_max,
        baseline_hba1c=_dist(d["baseline_hba1c"], f"{p}.baseline_hba1c"),
        followup_clip_min=fc_min,
        followup_clip_max=fc_max,
        improved_max_delta=imp,
        worsened_min_delta=wor,
        medication_classes=tuple(classes),
    )


def _data(raw: dict) -> Data:
    p = "data"
    d = _map(raw, p, {"source", "files_dir", "generator"})
    source = _choice(d["source"], f"{p}.source", {"generator", "files"})
    files_dir = _str(d["files_dir"], f"{p}.files_dir")
    resolved = resolve(files_dir, f"{p}.files_dir")
    generated = (ROOT / GENERATED_DIR).resolve()
    gen = None
    if source == "generator":
        if not _under(resolved, generated) or resolved == generated:
            raise ConfigError(f"{p}.files_dir: with source=generator it must be under {GENERATED_DIR}/")
        if d["generator"] is None:
            raise ConfigError(f"{p}.generator: required when source=generator")
        gen = _generator(d["generator"])
    else:
        if _under(resolved, generated):
            raise ConfigError(f"{p}.files_dir: with source=files it must not be under {GENERATED_DIR}/ "
                              "(the generator deletes that directory)")
        if d["generator"] is not None:
            gen = _generator(d["generator"])
    return Data(source=source, files_dir=files_dir, generator=gen)


def load_config_dict(raw: Any, path: Path | None = None) -> Config:
    """Validate a parsed config mapping and return a frozen Config."""
    if not isinstance(raw, dict):
        raise ConfigError("config: top level must be a mapping")
    raw = copy.deepcopy(raw)
    d = _map(raw, "", {"project", "region", "accounts", "collaboration", "storage", "glue", "privacy",
                       "security", "logging", "tables", "research", "query", "data"})

    pj = _map(d["project"], "project", {"name", "tags"})
    tags = pj["tags"]
    if not isinstance(tags, dict) or not tags:
        raise ConfigError("project.tags: must be a non-empty mapping")
    for k, v in tags.items():
        if not isinstance(k, str) or not k or not isinstance(v, str):
            raise ConfigError(f"project.tags.{k}: keys and values must be strings")
    project = Project(name=_str(pj["name"], "project.name", not_blank=True), tags=dict(tags))

    region = _str(d["region"], "region", pattern=RE_REGION)
    accounts = _accounts(d["accounts"])
    collaboration = _collaboration(d["collaboration"])
    storage = _storage(d["storage"], accounts, region)
    glue = Glue(database_name=_str(_map(d["glue"], "glue", {"database_name"})["database_name"],
                                   "glue.database_name", pattern=RE_GLUE_DB))
    pv = _map(d["privacy"], "privacy", {"identity_column", "min_distinct_patients"})
    privacy = Privacy(identity_column=_str(pv["identity_column"], "privacy.identity_column", pattern=RE_COLUMN),
                      min_distinct_patients=_int(pv["min_distinct_patients"], "privacy.min_distinct_patients",
                                                 2, 100000))
    sc = _map(d["security"], "security", {"pin_data_role_trust_to_query_runner"})
    security = Security(pin_data_role_trust_to_query_runner=_bool(
        sc["pin_data_role_trust_to_query_runner"], "security.pin_data_role_trust_to_query_runner"))
    lg = _map(d["logging"], "logging", {"warmup_seconds", "log_group_wait_seconds"})
    logging_cfg = Logging(warmup_seconds=_int(lg["warmup_seconds"], "logging.warmup_seconds", 0, 3600),
                          log_group_wait_seconds=_int(lg["log_group_wait_seconds"],
                                                      "logging.log_group_wait_seconds", 0, 3600))
    tables = _tables(d["tables"])
    for t in tables:
        if privacy.identity_column not in t.allowed_columns:
            raise ConfigError(f"privacy.identity_column: {privacy.identity_column!r} is not in "
                              f"tables.{t.key}.allowed_columns")
    research = _research(d["research"])
    query = _query(d["query"])
    data = _data(d["data"])

    return Config(project=project, region=region, accounts=accounts, collaboration=collaboration,
                  storage=storage, glue=glue, privacy=privacy, security=security, logging=logging_cfg,
                  tables=tables, research=research, query=query, data=data, raw=raw, path=path)
