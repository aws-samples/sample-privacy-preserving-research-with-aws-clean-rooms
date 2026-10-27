# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Live stack state for both accounts. Every script reads IDs from here (describe-stacks
outputs fetched live), so the scripts stay correct after a redeploy."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import boto3
import botocore.exceptions as bce

from airlock_lib.aws import AirlockError, reraise_if_sso
from airlock_lib.config import Config


class NotDeployed(AirlockError):
    pass


@dataclass(frozen=True)
class StackInfo:
    name: str
    status: str
    outputs: dict
    parameters: dict
    stack_id: str
    tags: dict = field(default_factory=dict)


def describe_stack(sess: boto3.Session, name: str) -> StackInfo | None:
    """Return the stack, or None when it does not exist."""
    cfn = sess.client("cloudformation")
    try:
        stacks = cfn.describe_stacks(StackName=name)["Stacks"]
    except bce.ClientError as exc:
        reraise_if_sso(exc, getattr(sess, "airlock_profile", None))
        if "does not exist" in exc.response.get("Error", {}).get("Message", ""):
            return None
        raise
    if not stacks:
        return None
    s = stacks[0]
    return StackInfo(
        name=s["StackName"],
        status=s["StackStatus"],
        outputs={o["OutputKey"]: o.get("OutputValue", "") for o in s.get("Outputs", [])},
        parameters={p["ParameterKey"]: p.get("ParameterValue", "") for p in s.get("Parameters", [])},
        stack_id=s["StackId"],
        tags={t["Key"]: t["Value"] for t in s.get("Tags", [])},
    )


def failed_events(sess: boto3.Session, stack: str, n: int = 20) -> list[dict]:
    """Newest ``*_FAILED`` stack events (``stack`` may be a name or a stack ID)."""
    cfn = sess.client("cloudformation")
    out = []
    try:
        for page in cfn.get_paginator("describe_stack_events").paginate(StackName=stack):
            for e in page["StackEvents"]:
                if e.get("ResourceStatus", "").endswith("_FAILED"):
                    out.append({k: e.get(k) for k in ("Timestamp", "LogicalResourceId", "ResourceType",
                                                      "ResourceStatus", "ResourceStatusReason")})
                    if len(out) >= n:
                        return out
    except bce.ClientError as exc:
        reraise_if_sso(exc, getattr(sess, "airlock_profile", None))
        return out
    return out


def created_memberships(sess: boto3.Session, stack: str) -> list[str]:
    """Physical IDs of AWS::CleanRooms::Membership resources that reached CREATE_COMPLETE."""
    cfn = sess.client("cloudformation")
    ids = []
    try:
        for page in cfn.get_paginator("describe_stack_events").paginate(StackName=stack):
            for e in page["StackEvents"]:
                if (e.get("ResourceType") == "AWS::CleanRooms::Membership"
                        and e.get("ResourceStatus") == "CREATE_COMPLETE" and e.get("PhysicalResourceId")):
                    ids.append(e["PhysicalResourceId"])
    except bce.ClientError as exc:
        reraise_if_sso(exc, getattr(sess, "airlock_profile", None))
    return sorted(set(ids))


def print_failed_events(events: list[dict], stack: str) -> None:
    if not events:
        print(f"  (no *_FAILED events for {stack})")
        return
    print(f"  Newest *_FAILED events for {stack}:")
    for e in events:
        print(f"    {e.get('Timestamp')}  {e.get('LogicalResourceId')} ({e.get('ResourceType')}) "
              f"{e.get('ResourceStatus')}: {e.get('ResourceStatusReason')}")


@dataclass(frozen=True)
class Deployed:
    cfg: Config
    a: StackInfo | None
    b: StackInfo | None

    def _a(self, key: str) -> str:
        if self.a is None:
            raise NotDeployed(f"stack {self.cfg.accounts.data_owner.stack_name} is not deployed in Account A; "
                              "run .venv/bin/python -m scripts.deploy")
        if key not in self.a.outputs:
            raise NotDeployed(f"stack {self.a.name} ({self.a.status}) has no output {key}")
        return self.a.outputs[key]

    def _b(self, key: str, params: bool = False) -> str:
        if self.b is None:
            raise NotDeployed(f"stack {self.cfg.accounts.analyst.stack_name} is not deployed in Account B; "
                              "run .venv/bin/python -m scripts.deploy")
        src = self.b.parameters if params else self.b.outputs
        if key not in src:
            raise NotDeployed(f"stack {self.b.name} ({self.b.status}) has no {'parameter' if params else 'output'} {key}")
        return src[key]

    @property
    def collaboration_id(self) -> str:
        return self._a("CollaborationId")

    @property
    def collaboration_arn(self) -> str:
        return self._a("CollaborationArn")

    @property
    def a_membership_id(self) -> str:
        return self._a("DataOwnerMembershipId")

    @property
    def a_membership_arn(self) -> str:
        return self._a("DataOwnerMembershipArn")

    @property
    def b_membership_id(self) -> str:
        return self._b("AnalystMembershipId")

    @property
    def b_membership_arn(self) -> str:
        return self._b("AnalystMembershipArn")

    @property
    def b_joined_collaboration_id(self) -> str:
        return self._b("CollaborationId", params=True)

    @property
    def configured_table_ids(self) -> dict:
        return json.loads(self._a("ConfiguredTableIds"))

    @property
    def data_bucket(self) -> str:
        if self.a is not None and "DataBucketName" in self.a.outputs:
            return self.a.outputs["DataBucketName"]
        return self.cfg.data_bucket_name

    @property
    def glue_database(self) -> str:
        if self.a is not None and "GlueDatabaseName" in self.a.outputs:
            return self.a.outputs["GlueDatabaseName"]
        return self.cfg.glue.database_name

    @property
    def results_bucket(self) -> str:
        if self.b is not None and "ResultsBucketName" in self.b.outputs:
            return self.b.outputs["ResultsBucketName"]
        return self.cfg.results_bucket_name

    @property
    def data_role_arn(self) -> str:
        return self._a("DataAccessRoleArn")

    @property
    def trust_phase(self) -> str:
        return self._a("TrustPhase")


def live(cfg: Config, sessions: dict) -> Deployed:
    return Deployed(
        cfg=cfg,
        a=describe_stack(sessions["data_owner"], cfg.accounts.data_owner.stack_name),
        b=describe_stack(sessions["analyst"], cfg.accounts.analyst.stack_name),
    )
