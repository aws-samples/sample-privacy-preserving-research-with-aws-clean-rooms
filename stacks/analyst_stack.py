# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Account B stack (Institution B, analyst).

Creating B's membership *is* the invitation acceptance. B also owns the results bucket
and pays for query compute. Deleting the stack deletes the membership (B leaves).
"""
from __future__ import annotations

from aws_cdk import CfnOutput, CfnParameter, RemovalPolicy, Stack
from aws_cdk import aws_cleanrooms as cleanrooms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from airlock_lib.config import Config

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class AnalystStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: Config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        st = cfg.storage

        self.collaboration_id_param = CfnParameter(
            self, "CollaborationId",
            type="String",
            allowed_pattern=UUID_PATTERN,
            description=f"Collaboration ID created by {cfg.accounts.data_owner.display_name} (Account A output)",
        )

        self.results_bucket = s3.Bucket(
            self, "ResultsBucket",
            bucket_name=cfg.results_bucket_name,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        cm = cleanrooms.CfnMembership
        self.membership = cm(
            self, "AnalystMembership",
            collaboration_identifier=self.collaboration_id_param.value_as_string,
            query_log_status=cfg.collaboration.query_log_status,
            payment_configuration=cm.MembershipPaymentConfigurationProperty(
                query_compute=cm.MembershipQueryComputePaymentConfigProperty(
                    is_responsible=cfg.payer_flags()["analyst"])),
            # No role_arn: a member that both queries and receives results may skip the result role.
            default_result_configuration=cm.MembershipProtectedQueryResultConfigurationProperty(
                output_configuration=cm.MembershipProtectedQueryOutputConfigurationProperty(
                    s3=cm.ProtectedQueryS3OutputConfigurationProperty(
                        bucket=self.results_bucket.bucket_name,
                        key_prefix=st.results_key_prefix or None,
                        result_format=st.result_format,
                        single_file_output=st.single_file_output,
                    ))),
        )
        # L2: create the membership last, after the bucket, its policy and auto-delete resource,
        # so a bucket-side failure cannot roll back (and permanently "leave") the membership.
        self.membership.node.add_dependency(self.results_bucket)

        CfnOutput(self, "AnalystMembershipId", value=self.membership.attr_membership_identifier)
        CfnOutput(self, "AnalystMembershipArn", value=self.membership.attr_arn)
        CfnOutput(self, "ResultsBucketName", value=self.results_bucket.bucket_name)
        # Named JoinedCollaborationId: "CollaborationId" is already the parameter's construct id.
        CfnOutput(self, "JoinedCollaborationId", value=self.collaboration_id_param.value_as_string)
