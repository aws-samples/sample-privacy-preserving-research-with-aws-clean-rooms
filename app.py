#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK entry point. Config path: $AIRLOCK_CONFIG or config/airlock.yaml.

Optional context: -c queryRunnerMembershipId=<uuid> (deploy phase 3 pins the data-role trust).
"""
import re
import sys

from aws_cdk import App

from airlock_lib import datafiles
from airlock_lib.config import ConfigError, load_config
from stacks import build_app

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def main() -> None:
    app = App()
    try:
        cfg = load_config()
        datafiles.validate(cfg)
    except ConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    runner = app.node.try_get_context("queryRunnerMembershipId")
    if runner is not None and (not isinstance(runner, str) or not UUID_RE.match(runner)):
        print(f"[ERROR] context queryRunnerMembershipId={runner!r} is not a membership UUID", file=sys.stderr)
        sys.exit(1)
    build_app(cfg, app, query_runner_membership_id=runner)
    app.synth()


if __name__ == "__main__":
    main()
