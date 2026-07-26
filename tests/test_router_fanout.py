"""S10 §17 — cross-collection fan-out with failure isolation + RRF fusion.

§17.1's isolation rule is the safety core: a PROJECT collection failure is
fail-closed (the query must not look complete when the project's own corpus
wasn't searched), while a FRAMEWORK failure or timeout returns explicit
``degraded`` partial results — never the ``except: return []`` anti-pattern that
makes a failure indistinguishable from "not found". Survivors fuse by RRF.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S10 §17 -> G10)
"""

import time

import pytest

from ragtools.retrieval.router import ResolvedScope, fan_out_and_fuse


def _id(x):
    return x


def _scope(primary, linked=()):
    return ResolvedScope(client_profile_id="p", resolution="explicit",
                         primary=list(primary), linked=list(linked))


def test_fuses_hits_across_collections_by_rrf():
    scope = _scope(["proj"], ["fw"])
    data = {"proj": ["x", "y", "z"], "fw": ["x", "w"]}  # x ranks high in both
    out = fan_out_and_fuse(scope, lambda ref: data[ref], key=_id)
    assert out.hits[0] == "x"
    assert set(out.hits) == {"x", "y", "z", "w"}
    assert out.degraded == []
    assert out.per_collection == {"proj": 3, "fw": 2}


def test_framework_failure_is_degraded_not_fatal():
    scope = _scope(["proj"], ["fw"])

    def search_one(ref):
        if ref == "fw":
            raise RuntimeError("qdrant scroll failed")
        return ["a", "b"]

    out = fan_out_and_fuse(scope, search_one, key=_id)
    assert set(out.hits) == {"a", "b"}                 # project results survive
    assert [r for r, _ in out.degraded] == ["fw"]      # framework explicitly degraded
    assert out.per_collection["fw"] == 0


def test_project_failure_is_fail_closed():
    scope = _scope(["proj"], ["fw"])

    def search_one(ref):
        if ref == "proj":
            raise RuntimeError("project collection unavailable")
        return ["a"]

    with pytest.raises(RuntimeError):
        fan_out_and_fuse(scope, search_one, key=_id)


def test_framework_timeout_is_degraded():
    scope = _scope(["proj"], ["slow_fw"])

    def search_one(ref):
        if ref == "slow_fw":
            time.sleep(2.0)
            return ["late"]
        return ["a", "b"]

    out = fan_out_and_fuse(scope, search_one, key=_id, timeout=0.2)
    assert set(out.hits) == {"a", "b"}
    assert [r for r, reason in out.degraded] == ["slow_fw"]
    assert out.degraded[0][1] == "timeout"


def test_empty_result_is_not_a_failure():
    # A collection returning [] is distinct from one that errored.
    scope = _scope(["proj"], ["fw"])
    out = fan_out_and_fuse(scope, lambda ref: (["a"] if ref == "proj" else []), key=_id)
    assert out.degraded == []                 # empty != degraded
    assert out.per_collection["fw"] == 0
    assert out.hits == ["a"]


def test_no_collections_returns_empty():
    scope = ResolvedScope(client_profile_id="p", resolution="explicit")
    out = fan_out_and_fuse(scope, lambda ref: ["never"], key=_id)
    assert out.hits == []
    assert out.degraded == []
