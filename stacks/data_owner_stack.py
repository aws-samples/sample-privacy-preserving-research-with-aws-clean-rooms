# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Account A stack (Institution A, data owner, collaboration creator).

Resources: S3 data bucket + seeded Parquet upload, Glue database/tables, the Clean Rooms
collaboration (invites B), A's own membership, the data-access role, two configured
tables with aggregation analysis rules, and their associations.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_cleanrooms as cleanrooms
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from airlock_lib.config import Config
from stacks.analysis_rule import build_aggregation_rule

PARQUET_INPUT = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
PARQUET_OUTPUT = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
PARQUET_SERDE = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
CLEANROOMS_PRINCIPAL = "cleanrooms.amazonaws.com"


class DataOwnerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: Config,
                 query_runner_membership_id: str | None = None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        a = cfg.accounts.data_owner
        b = cfg.accounts.analyst
        region = cfg.region
        db_name = cfg.glue.database_name

        # ---- 3.1.1 S3 data bucket (A only; no grants to B) + seeded data upload
        self.data_bucket = s3.Bucket(
            self, "DataBucket",
            bucket_name=cfg.data_bucket_name,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        deployment_logs = logs.LogGroup(self, "DataDeploymentLogs", retention=logs.RetentionDays.ONE_WEEK,
                                        removal_policy=RemovalPolicy.DESTROY)
        self.deployment = s3deploy.BucketDeployment(
            self, "DataDeployment",
            sources=[s3deploy.Source.asset(str(cfg.files_dir_path), exclude=[".*", "**/.*"])],
            destination_bucket=self.data_bucket,
            prune=True,
            retain_on_delete=False,
            memory_limit=256,
            log_group=deployment_logs,
        )

        # ---- 3.1.2 Glue catalog (IAM-only Lake Formation mode; no LF grants)
        database = glue.CfnDatabase(
            self, "GlueDatabase",
            catalog_id=a.account_id,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=db_name, description=f"{cfg.project.name} synthetic research data ({a.display_name})"),
        )
        self.glue_tables: dict[str, glue.CfnTable] = {}
        for t in cfg.tables:
            gt = glue.CfnTable(
                self, f"GlueTable{t.construct_suffix}",
                catalog_id=a.account_id,
                database_name=db_name,
                table_input=glue.CfnTable.TableInputProperty(
                    name=t.glue_table_name,
                    description=t.description or None,
                    table_type="EXTERNAL_TABLE",
                    parameters={"classification": "parquet", "EXTERNAL": "TRUE"},
                    storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                        columns=[glue.CfnTable.ColumnProperty(name=c.name, type=c.type) for c in t.columns],
                        location=f"s3://{self.data_bucket.bucket_name}/{t.s3_prefix}",
                        input_format=PARQUET_INPUT,
                        output_format=PARQUET_OUTPUT,
                        serde_info=glue.CfnTable.SerdeInfoProperty(
                            serialization_library=PARQUET_SERDE, parameters={"serialization.format": "1"}),
                    ),
                ),
            )
            gt.node.add_dependency(database)
            self.glue_tables[t.key] = gt

        # ---- 3.1.3 Collaboration (A creates, invites B).
        # Create-only (changing any of these REPLACES the collaboration; run teardown first):
        # CreatorDisplayName, CreatorMemberAbilities, CreatorMLMemberAbilities, DataEncryptionMetadata,
        # JobLogStatus, QueryLogStatus, Members, CreatorPaymentConfiguration, AutoApprovedChangeTypes,
        # AllowedResultRegions.
        payer = cfg.payer_flags()
        cc = cleanrooms.CfnCollaboration
        self.collaboration = cc(
            self, "Collaboration",
            name=cfg.collaboration.name,
            description=cfg.collaboration.description,
            creator_display_name=a.display_name,
            creator_member_abilities=list(cfg.collaboration.creator_member_abilities),
            creator_payment_configuration=cc.PaymentConfigurationProperty(
                query_compute=cc.QueryComputePaymentConfigProperty(is_responsible=payer["data_owner"])),
            members=[cc.MemberSpecificationProperty(
                account_id=b.account_id,
                display_name=b.display_name,
                member_abilities=list(cfg.collaboration.analyst_member_abilities),
                payment_configuration=cc.PaymentConfigurationProperty(
                    query_compute=cc.QueryComputePaymentConfigProperty(is_responsible=payer["analyst"])),
            )],
            query_log_status=cfg.collaboration.query_log_status,
            analytics_engine=cfg.collaboration.analytics_engine,
        )

        # ---- 3.1.4 A's own membership. RETAIN: the creator can only leave by deleting the
        # collaboration, so teardown removes the leftover record after the stack is gone.
        self.membership = cleanrooms.CfnMembership(
            self, "DataOwnerMembership",
            collaboration_identifier=self.collaboration.attr_collaboration_identifier,
            query_log_status=cfg.collaboration.query_log_status,
        )
        self.membership.apply_removal_policy(RemovalPolicy.RETAIN)

        # ---- 3.1.5 Data-access role (confused-deputy trust; phase 1 or phase 2)
        bucket_arn = self.data_bucket.bucket_arn
        s3_condition = {"StringEquals": {"s3:ResourceAccount": a.account_id}}
        glue_resources = [f"arn:aws:glue:{region}:{a.account_id}:catalog",
                          f"arn:aws:glue:{region}:{a.account_id}:database/{db_name}"]
        glue_resources += [f"arn:aws:glue:{region}:{a.account_id}:table/{db_name}/{t.glue_table_name}"
                           for t in cfg.tables]
        policy = iam.PolicyDocument(statements=[
            iam.PolicyStatement(
                sid="GlueMetadataRead",
                actions=["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables",
                         "glue:GetPartition", "glue:GetPartitions", "glue:BatchGetPartition"],
                resources=glue_resources),
            iam.PolicyStatement(sid="GlueSchemaRead", actions=["glue:GetSchema", "glue:GetSchemaVersion"],
                                resources=["*"]),
            iam.PolicyStatement(sid="S3BucketRead", actions=["s3:GetBucketLocation", "s3:ListBucket"],
                                resources=[bucket_arn], conditions=s3_condition),
            iam.PolicyStatement(sid="S3ObjectRead", actions=["s3:GetObject"],
                                resources=[f"{bucket_arn}/{t.s3_prefix}*" for t in cfg.tables],
                                conditions=s3_condition),
        ])
        self.data_role = iam.Role(
            self, "DataAccessRole",
            assumed_by=iam.ServicePrincipal(CLEANROOMS_PRINCIPAL),
            description=f"{cfg.project.name}: lets Clean Rooms read {a.display_name}'s configured tables",
            inline_policies={"AirlockDataRead": policy},
        )
        pinned = bool(query_runner_membership_id) and cfg.security.pin_data_role_trust_to_query_runner
        self.trust_phase = "2" if pinned else "1"
        self.data_role.node.default_child.add_property_override(
            "AssumeRolePolicyDocument",
            self._trust_document(region, b.account_id, self.membership.attr_arn,
                                 query_runner_membership_id if pinned else None))

        # ---- 3.1.6 Configured tables with the aggregation analysis rule
        ct = cleanrooms.CfnConfiguredTable
        self.configured_tables: dict[str, cleanrooms.CfnConfiguredTable] = {}
        self.associations: dict[str, cleanrooms.CfnConfiguredTableAssociation] = {}
        for t in cfg.tables:
            table = ct(
                self, f"ConfiguredTable{t.construct_suffix}",
                name=t.configured_table_name,
                description=t.description or None,
                analysis_method="DIRECT_QUERY",
                allowed_columns=list(t.allowed_columns),
                table_reference=ct.TableReferenceProperty(
                    glue=ct.GlueTableReferenceProperty(database_name=db_name, table_name=t.glue_table_name)),
                analysis_rules=[ct.AnalysisRuleProperty(
                    type="AGGREGATION",
                    policy=ct.ConfiguredTableAnalysisRulePolicyProperty(
                        v1=ct.ConfiguredTableAnalysisRulePolicyV1Property(
                            aggregation=build_aggregation_rule(t, cfg.privacy))),
                )],
            )
            # Glue names are plain strings: make the dependency explicit.
            table.node.add_dependency(self.glue_tables[t.key])
            self.configured_tables[t.key] = table

            # ---- 3.1.7 Association (the association name is the table name used in SQL)
            self.associations[t.key] = cleanrooms.CfnConfiguredTableAssociation(
                self, f"Association{t.construct_suffix}",
                name=t.association_name,
                configured_table_identifier=table.attr_configured_table_identifier,
                membership_identifier=self.membership.attr_membership_identifier,
                role_arn=self.data_role.role_arn,
            )

        # ---- 3.1.8 Outputs (no cross-stack exports)
        CfnOutput(self, "CollaborationId", value=self.collaboration.attr_collaboration_identifier)
        CfnOutput(self, "CollaborationArn", value=self.collaboration.attr_arn)
        CfnOutput(self, "DataOwnerMembershipId", value=self.membership.attr_membership_identifier)
        CfnOutput(self, "DataOwnerMembershipArn", value=self.membership.attr_arn)
        CfnOutput(self, "DataBucketName", value=self.data_bucket.bucket_name)
        CfnOutput(self, "GlueDatabaseName", value=db_name)
        CfnOutput(self, "ConfiguredTableIds", value=self.to_json_string(
            {k: v.attr_configured_table_identifier for k, v in self.configured_tables.items()}))
        CfnOutput(self, "DataAccessRoleArn", value=self.data_role.role_arn)
        CfnOutput(self, "TrustPhase", value=self.trust_phase)

    @staticmethod
    def _trust_document(region: str, analyst_account: str, owner_membership_arn: str,
                        runner_membership_id: str | None) -> dict:
        """AWS confused-deputy form (two statements). Phase 1 before B joins, phase 2 pinned to B."""
        principal = {"Service": CLEANROOMS_PRINCIPAL}
        if runner_membership_id:
            source = {"ForAnyValue:ArnEquals": {"aws:SourceArn": [
                owner_membership_arn,
                f"arn:aws:cleanrooms:{region}:{analyst_account}:membership/{runner_membership_id}"]}}
            external = f"arn:aws:*:{region}:*:dbuser:*/{runner_membership_id}*"
        else:
            source = {"ForAnyValue:ArnLike": {"aws:SourceArn": [
                owner_membership_arn,
                f"arn:aws:cleanrooms:{region}:{analyst_account}:membership/*"]}}
            external = f"arn:aws:*:{region}:*:dbuser:*/*"
        return {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "AllowIfSourceArnMatches", "Effect": "Allow", "Principal": principal,
                 "Action": "sts:AssumeRole", "Condition": source},
                {"Sid": "AllowIfExternalIdMatches", "Effect": "Allow", "Principal": principal,
                 "Action": "sts:AssumeRole", "Condition": {"StringLike": {"sts:ExternalId": external}}},
            ],
        }
