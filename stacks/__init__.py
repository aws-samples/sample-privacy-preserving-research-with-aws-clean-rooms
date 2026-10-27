# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK stacks for Airlock. ``build_app`` is the single code path for synth and tests."""
from __future__ import annotations

from aws_cdk import App, Environment, Tags

from airlock_lib.config import Config


def build_app(cfg: Config, app: App, query_runner_membership_id: str | None = None):
    """Add both stacks to ``app`` and tag everything. Returns (DataOwnerStack, AnalystStack)."""
    from stacks.analyst_stack import AnalystStack
    from stacks.data_owner_stack import DataOwnerStack

    a, b = cfg.accounts.data_owner, cfg.accounts.analyst
    owner = DataOwnerStack(
        app, a.stack_name,
        stack_name=a.stack_name,
        cfg=cfg,
        query_runner_membership_id=query_runner_membership_id,
        env=Environment(account=a.account_id, region=cfg.region),
        description=f"{cfg.project.name} Account A ({a.display_name}): data, configured tables, collaboration",
    )
    analyst = AnalystStack(
        app, b.stack_name,
        stack_name=b.stack_name,
        cfg=cfg,
        env=Environment(account=b.account_id, region=cfg.region),
        description=f"{cfg.project.name} Account B ({b.display_name}): membership and results bucket",
    )
    for key, value in cfg.project.tags.items():
        Tags.of(app).add(key, value)
    return owner, analyst
