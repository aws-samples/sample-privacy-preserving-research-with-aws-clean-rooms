# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import copy

import pytest

from airlock_lib.config import load_config


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture()
def raw(cfg):
    return copy.deepcopy(cfg.raw)


@pytest.fixture(scope="session")
def generated_root_data(cfg):
    """Generate into ROOT once (CDK Source.asset needs the real files_dir)."""
    from datagen.generate import generate

    return generate(cfg)
