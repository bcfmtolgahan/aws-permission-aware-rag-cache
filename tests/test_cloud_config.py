"""Offline tests for explicit sandbox selection; no live AWS requests."""
import importlib
from pathlib import Path
import sys

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
cloud = importlib.import_module("cloud")


@pytest.fixture
def local_config(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, "LOCAL", tmp_path)
    return tmp_path


def test_no_configuration_does_not_fall_back_to_ambient_profile(local_config, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "unintended")
    with pytest.raises(SystemExit, match="configure"):
        cloud.settings()


@pytest.mark.parametrize(("profile", "account"), [
    (None, "000000000000"), ("", "000000000000"), ("x\nx", "000000000000"),
    ("test-sandbox", None), ("test-sandbox", "wrong"),
])
def test_invalid_configuration_is_not_written(local_config, profile, account):
    with pytest.raises(SystemExit):
        cloud.configure(profile, account)
    assert not (local_config / "account.json").exists()


def test_configuration_is_private_and_cannot_be_repointed(local_config):
    cloud.configure("test-sandbox", "000000000000")
    assert (local_config / "account.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit, match="already pinned"):
        cloud.configure("another-sandbox", "000000000000")
    assert cloud.settings()["profile"] == "test-sandbox"


@pytest.mark.parametrize("matches", [True, False])
def test_sts_identity_is_verified_before_returning_session(local_config, monkeypatch, matches):
    cloud.configure("test-sandbox", "000000000000")
    calls = []

    class Session:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def client(self, name):
            assert name == "sts"
            return self

        def get_caller_identity(self):
            return {"Account": "000000000000" if matches else "111111111111"}

    monkeypatch.setattr(boto3, "Session", Session)
    if matches:
        _, account = cloud.guarded_session()
        assert account == "000000000000"
    else:
        with pytest.raises(SystemExit, match="does not match"):
            cloud.guarded_session()
    assert calls == [{"profile_name": "test-sandbox", "region_name": "eu-central-1"}]
