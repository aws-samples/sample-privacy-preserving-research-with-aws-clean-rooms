# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AWS access for every script: per-profile boto3 sessions, SSO-expiry detection (exit 2),
CLI echo (``[cli]``) and mutating-API echo (``[api]``) so the output is its own
CLI-equivalent record (RR5). Credentials only ever come from ``aws sso login``.
"""
from __future__ import annotations

import collections
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import boto3
import botocore.exceptions as bce
from botocore import xform_name

from airlock_lib import ROOT
from airlock_lib.config import Config, ConfigError
from airlock_lib.output import error, info

SSO_EXCEPTIONS = tuple(getattr(bce, n) for n in (
    "SSOTokenLoadError", "UnauthorizedSSOTokenError", "TokenRetrievalError", "NoCredentialsError", "SSOError",
) if hasattr(bce, n))
SSO_CODES = {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "UnrecognizedClientException"}
SSO_STRINGS = ("Token has expired", "SSO session", "Error loading SSO Token", "ExpiredToken")
MUTATING_PREFIXES = ("Create", "Delete", "Start", "Update", "Put")
# Only the services Airlock itself calls are echoed. This excludes botocore's internal
# sso / sso-oidc / sts credential calls, whose parameters are secrets.
ECHO_SERVICES = {"cleanrooms", "s3", "cloudformation", "glue", "iam", "logs", "cloudtrail"}
SECRET_PARAM = ("secret", "token", "password", "credential")


class AirlockError(Exception):
    """A handled failure: print the message and exit with ``exit_code``."""
    exit_code = 1


class SsoExpired(Exception):
    def __init__(self, profile: str | None = None):
        super().__init__(profile or "")
        self.profile = profile


class CliError(AirlockError):
    def __init__(self, cmd: list[str], returncode: int, tail: list[str]):
        super().__init__(f"command failed (exit {returncode}): {shlex.join(cmd)}")
        self.cmd, self.returncode, self.tail = cmd, returncode, tail


def is_sso_error(exc: BaseException) -> bool:
    if SSO_EXCEPTIONS and isinstance(exc, SSO_EXCEPTIONS):
        return True
    if isinstance(exc, bce.ClientError):
        return exc.response.get("Error", {}).get("Code") in SSO_CODES
    return False


def error_code(exc: BaseException) -> str:
    if isinstance(exc, bce.ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return type(exc).__name__


def error_message(exc: BaseException) -> str:
    if isinstance(exc, bce.ClientError):
        return exc.response.get("Error", {}).get("Message", str(exc))
    return str(exc)


def reraise_if_sso(exc: BaseException, profile: str | None = None) -> None:
    if is_sso_error(exc):
        raise SsoExpired(profile) from exc


def _echo_api(params, model, **_kwargs):
    name = model.name
    service = model.service_model.service_name
    if service not in ECHO_SERVICES or not name.startswith(MUTATING_PREFIXES):
        return
    args = []
    for k, v in params.items():
        if any(s in k.lower() for s in SECRET_PARAM):
            s = "***"
        else:
            s = repr(v) if not isinstance(v, str) else v
        if len(s) > 80:
            s = s[:77] + "..."
        args.append(f"{k}={s}")
    print(f"[api] {service}.{xform_name(name)}({', '.join(args)})", file=sys.stderr, flush=True)


def session(profile: str, region: str) -> boto3.Session:
    s = boto3.Session(profile_name=profile, region_name=region)
    s.events.register("provide-client-params", _echo_api)
    s.airlock_profile = profile  # used for SSO messages
    return s


def sso_message(profile: str | None, cfg: Config | None = None) -> str:
    if profile:
        return f"SSO session expired for profile {profile}. Run: aws sso login --profile {profile}"
    if cfg is not None:
        pa, pb = cfg.accounts.data_owner.profile, cfg.accounts.analyst.profile
        return (f"SSO session expired. Run: aws sso login --profile {pa}  and  "
                f"aws sso login --profile {pb}")
    return "SSO session expired. Run: aws sso login --profile <profile>"


def run_main(fn: Callable[[], int | None], cfg_loader: Callable[[], Config] | None = None) -> None:
    """Run ``fn`` and map failures to exit codes: 1 config/handled error, 2 SSO expired."""
    try:
        rc = fn()
    except ConfigError as exc:
        error(str(exc))
        sys.exit(1)
    except SsoExpired as exc:
        error(sso_message(exc.profile, _try(cfg_loader)))
        sys.exit(2)
    except AirlockError as exc:
        error(str(exc))
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        error("interrupted")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - map SSO errors raised anywhere to exit 2
        if is_sso_error(exc):
            error(sso_message(None, _try(cfg_loader)))
            sys.exit(2)
        raise
    sys.exit(rc or 0)


def _try(loader):
    if loader is None:
        return None
    try:
        return loader()
    except Exception:  # noqa: BLE001
        return None


def run_cli(cmd: list[str], profile: str | None, env_extra: dict | None = None, check: bool = True,
            cwd: Path = ROOT, quiet: bool = False) -> tuple[int, list[str]]:
    """Echo ``[cli] <cmd>``, stream merged output, keep the last 300 lines.

    Raises SsoExpired when a failing command reports an SSO/token problem, and CliError
    when ``check`` is set and the command fails.
    """
    print(f"[cli] {shlex.join(cmd)}", file=sys.stderr, flush=True)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    tail: collections.deque[str] = collections.deque(maxlen=300)
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        tail.append(line.rstrip("\n"))
        if not quiet:
            print(line, end="", flush=True)
    rc = proc.wait()
    lines = list(tail)
    if rc != 0:
        text = "\n".join(lines)
        if any(s in text for s in SSO_STRINGS):
            raise SsoExpired(profile)
        if check:
            raise CliError(cmd, rc, lines)
    return rc, lines


def cdk_bin() -> str:
    env = os.environ.get("AIRLOCK_CDK_BIN")
    if env and os.access(env, os.X_OK):
        return env
    found = shutil.which("cdk")
    if found:
        return found
    raise AirlockError("CDK CLI not found; put cdk on PATH or set AIRLOCK_CDK_BIN=/path/to/cdk")


def preflight(cfg: Config) -> dict[str, boto3.Session]:
    """One session per member; verify each profile resolves to the configured account."""
    sessions = {}
    for member in ("data_owner", "analyst"):
        acct = cfg.member(member)
        try:
            s = session(acct.profile, cfg.region)
            ident = s.client("sts").get_caller_identity()
        except bce.ProfileNotFound as exc:
            raise AirlockError(f"AWS profile {acct.profile!r} not found in your AWS config "
                               f"(accounts.{member}.profile)") from exc
        except Exception as exc:  # noqa: BLE001
            if is_sso_error(exc):
                raise SsoExpired(acct.profile) from exc
            raise
        if ident["Account"] != acct.account_id:
            raise AirlockError(f"profile {acct.profile} resolves to account {ident['Account']}, but "
                               f"accounts.{member}.account_id is {acct.account_id} (wrong profile?)")
        info(f"{acct.profile} -> {ident['Account']} ({acct.display_name}) as {ident['Arn']}")
        sessions[member] = s
    return sessions
