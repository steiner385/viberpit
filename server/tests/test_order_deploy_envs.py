from server.pipeline.track import order_deploy_envs


def test_conventional_order_dev_qa_staging_prod():
    assert order_deploy_envs(["production", "staging", "qa", "dev"]) == ["dev", "qa", "staging", "production"]


def test_unknown_envs_cluster_middle_alphabetical():
    # unknown rank == staging rank (2); alphabetical tie-break among rank-2 items
    out = order_deploy_envs(["production", "zebra", "alpha", "dev"])
    assert out[0] == "dev"
    assert out[-1] == "production"
    assert out.index("alpha") < out.index("zebra")


def test_prod_family_always_last():
    assert order_deploy_envs(["live", "qa"])[-1] == "live"
    assert order_deploy_envs(["release-prod", "test"])[-1] == "release-prod"


def test_empty():
    assert order_deploy_envs([]) == []


def test_dedup_preserves_single():
    assert order_deploy_envs(["qa", "qa"]) == ["qa"]
