"""S0 — Development Environment Isolation.

These tests pin the isolation guard that prevents a dev/ci runtime from ever
mounting live data or placing a vector store on a synced/FUSE path (where
Qdrant's own filesystem check documents silent data loss). The guard is pure
and dependency-injected so it can be unit-tested without touching any real
service, port, or directory.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S0 -> G0)
"""

import pytest

from pathlib import Path

from ragtools.devenv import (
    DevEnvironment,
    IsolationError,
    PROFILE_DEV,
    PROFILE_INSTALLED,
    RESERVED_LIVE_PORTS,
    assert_dev_data_dir_isolated,
    assert_dev_port_allowed,
    bootstrap_dev_environment,
    default_synced_detector,
    dev_data_dir,
    profile_may_autostart,
    resolve_and_verify_dev_data_dir,
    resolve_profile,
)


# --- data-dir isolation guard --------------------------------------------


def test_guard_passes_for_clean_isolated_dir(tmp_path):
    """A fresh dir that touches no live dir and is not synced is allowed."""
    candidate = tmp_path / "devenv" / "wt-abc"
    live = tmp_path / "live" / "RAGTools"
    # Should not raise.
    assert_dev_data_dir_isolated(
        candidate, live_dirs=[live], is_synced_path=lambda p: False
    )


def test_guard_refuses_candidate_equal_to_live_dir(tmp_path):
    live = tmp_path / "live" / "RAGTools" / "data"
    with pytest.raises(IsolationError):
        assert_dev_data_dir_isolated(
            live, live_dirs=[live], is_synced_path=lambda p: False
        )


def test_guard_refuses_candidate_inside_live_dir(tmp_path):
    live = tmp_path / "live" / "RAGTools"
    candidate = live / "data" / "qdrant"
    with pytest.raises(IsolationError):
        assert_dev_data_dir_isolated(
            candidate, live_dirs=[live], is_synced_path=lambda p: False
        )


def test_guard_refuses_live_dir_inside_candidate(tmp_path):
    """A candidate that would *contain* a live dir is equally unsafe."""
    candidate = tmp_path / "shared"
    live = candidate / "RAGTools" / "data"
    with pytest.raises(IsolationError):
        assert_dev_data_dir_isolated(
            candidate, live_dirs=[live], is_synced_path=lambda p: False
        )


def test_guard_refuses_synced_path(tmp_path):
    """Qdrant corrupts on FUSE/synced storage — the guard must refuse it."""
    candidate = tmp_path / "SyncedWorkspace" / "devenv"
    with pytest.raises(IsolationError):
        assert_dev_data_dir_isolated(
            candidate, live_dirs=[], is_synced_path=lambda p: True
        )


def test_guard_error_names_the_offending_reason(tmp_path):
    """The refusal must state *why* (live-collision vs synced), not just fail."""
    live = tmp_path / "RAGTools"
    with pytest.raises(IsolationError, match="(?i)live"):
        assert_dev_data_dir_isolated(
            live / "data", live_dirs=[live], is_synced_path=lambda p: False
        )
    with pytest.raises(IsolationError, match="(?i)sync"):
        assert_dev_data_dir_isolated(
            tmp_path / "clean", live_dirs=[], is_synced_path=lambda p: True
        )


def test_guard_normalizes_before_comparing(tmp_path):
    """Nesting is detected even through '..' and non-normalized input."""
    live = tmp_path / "live" / "RAGTools"
    candidate = tmp_path / "live" / "RAGTools" / "x" / ".." / "data"
    with pytest.raises(IsolationError):
        assert_dev_data_dir_isolated(
            candidate, live_dirs=[live], is_synced_path=lambda p: False
        )


# --- port guard ----------------------------------------------------------


def test_reserved_ports_include_owner_and_roy():
    assert 21420 in RESERVED_LIVE_PORTS  # owner service
    assert 21422 in RESERVED_LIVE_PORTS  # Roy / restricted bot


@pytest.mark.parametrize("port", sorted(RESERVED_LIVE_PORTS))
def test_dev_port_refuses_reserved_live_ports(port):
    with pytest.raises(IsolationError):
        assert_dev_port_allowed(port)


@pytest.mark.parametrize("port", [0, 21419, 21421, 21423, 34567, 51000])
def test_dev_port_allows_non_reserved(port):
    # Should not raise (0 = OS-assigned ephemeral).
    assert_dev_port_allowed(port)


# --- real synced-path detector (Syncthing .stfolder ancestor probe) ------


def test_synced_detector_flags_ancestor_with_stfolder(tmp_path):
    root = tmp_path / "SyncedRoot"
    (root / ".stfolder").mkdir(parents=True)
    deep = root / "rag" / "devenv" / "qdrant"
    deep.mkdir(parents=True)
    assert default_synced_detector(deep) is True


def test_synced_detector_flags_dir_itself_containing_stfolder(tmp_path):
    root = tmp_path / "SyncedRoot"
    (root / ".stfolder").mkdir(parents=True)
    assert default_synced_detector(root) is True


def test_synced_detector_passes_clean_tree(tmp_path):
    clean = tmp_path / "local" / "devenv"
    clean.mkdir(parents=True)
    assert default_synced_detector(clean) is False


def test_synced_detector_terminates_and_passes_at_root(tmp_path):
    # No marker anywhere: must return False without walking forever.
    assert default_synced_detector(tmp_path) is False


def test_guard_uses_default_detector_end_to_end(tmp_path):
    """The guard wired to the real detector refuses a synced candidate."""
    synced_root = tmp_path / "Synced"
    (synced_root / ".stfolder").mkdir(parents=True)
    candidate = synced_root / "rag-v3-dev" / "data"
    with pytest.raises(IsolationError, match="(?i)sync"):
        assert_dev_data_dir_isolated(
            candidate, live_dirs=[], is_synced_path=default_synced_detector
        )


# --- runtime profile resolution ------------------------------------------


def test_resolve_profile_explicit_valid_wins():
    assert resolve_profile({"RAG_PROFILE": "ci"}) == "ci"


def test_resolve_profile_defaults_to_dev_from_source():
    assert resolve_profile({}, packaged=False) == PROFILE_DEV


def test_resolve_profile_defaults_to_installed_when_packaged():
    assert resolve_profile({}, packaged=True) == PROFILE_INSTALLED


def test_resolve_profile_is_case_insensitive_and_trims():
    assert resolve_profile({"RAG_PROFILE": "  Dev "}) == PROFILE_DEV


def test_resolve_profile_rejects_unknown():
    with pytest.raises(ValueError, match="(?i)profile"):
        resolve_profile({"RAG_PROFILE": "prod"})


# --- worktree-keyed dev data dir -----------------------------------------


def test_dev_data_dir_is_keyed_by_worktree(tmp_path):
    base = tmp_path / "base"
    a = dev_data_dir(worktree_root=tmp_path / "wtA", base=base, env={})
    b = dev_data_dir(worktree_root=tmp_path / "wtB", base=base, env={})
    assert Path(a) != Path(b)  # parallel worktrees never collide


def test_dev_data_dir_is_deterministic(tmp_path):
    base = tmp_path / "base"
    a1 = dev_data_dir(worktree_root=tmp_path / "wtA", base=base, env={})
    a2 = dev_data_dir(worktree_root=tmp_path / "wtA", base=base, env={})
    assert Path(a1) == Path(a2)


def test_dev_data_dir_lives_under_base(tmp_path):
    base = tmp_path / "base"
    d = dev_data_dir(worktree_root=tmp_path / "wt", base=base, env={})
    assert Path(d).is_relative_to(base.resolve())


def test_dev_data_dir_explicit_override_wins(tmp_path):
    override = tmp_path / "explicit-data"
    d = dev_data_dir(
        worktree_root=tmp_path / "wt",
        base=tmp_path / "base",
        env={"RAG_DATA_DIR": str(override)},
    )
    assert Path(d) == override.resolve()


# --- resolve + verify (the S0 entry point) -------------------------------


def test_resolve_and_verify_returns_isolated_dir(tmp_path):
    d = resolve_and_verify_dev_data_dir(
        worktree_root=tmp_path / "wt",
        base=tmp_path / "base",
        live_dirs=[tmp_path / "live"],
        env={},
    )
    assert Path(d).is_relative_to((tmp_path / "base").resolve())


def test_resolve_and_verify_refuses_synced_base(tmp_path):
    base = tmp_path / "Synced"
    (base / ".stfolder").mkdir(parents=True)
    with pytest.raises(IsolationError, match="(?i)sync"):
        resolve_and_verify_dev_data_dir(
            worktree_root=tmp_path / "wt", base=base, live_dirs=[], env={}
        )


def test_resolve_and_verify_refuses_live_collision(tmp_path):
    base = tmp_path / "base"
    # The computed dir lives under `base`; declaring `base` itself live must
    # make the guard refuse (the dev dir would sit inside live data).
    with pytest.raises(IsolationError, match="(?i)live"):
        resolve_and_verify_dev_data_dir(
            worktree_root=tmp_path / "wt", base=base, live_dirs=[base], env={}
        )


# --- bootstrap + guaranteed teardown (S0.5) ------------------------------


def test_bootstrap_creates_isolated_dir_and_teardown_removes_it(tmp_path):
    base = tmp_path / "devbase"
    env = bootstrap_dev_environment(
        worktree_root=tmp_path / "wt",
        base=base,
        env={"RAG_PROFILE": "dev"},
        port=0,
        live_dirs=[tmp_path / "live"],
    )
    assert env.profile == PROFILE_DEV
    assert Path(env.data_dir).is_relative_to(base.resolve())
    assert Path(env.data_dir).is_dir()  # created on bootstrap
    env.teardown()
    assert not Path(env.data_dir).exists()  # guaranteed teardown


def test_bootstrap_refuses_reserved_port(tmp_path):
    with pytest.raises(IsolationError):
        bootstrap_dev_environment(
            worktree_root=tmp_path / "wt",
            base=tmp_path / "b",
            env={"RAG_PROFILE": "dev"},
            port=21420,
        )


def test_bootstrap_refuses_synced_base(tmp_path):
    base = tmp_path / "Synced"
    (base / ".stfolder").mkdir(parents=True)
    with pytest.raises(IsolationError, match="(?i)sync"):
        bootstrap_dev_environment(
            worktree_root=tmp_path / "wt", base=base, env={"RAG_PROFILE": "dev"}
        )


def test_bootstrap_refuses_persistent_profile(tmp_path):
    with pytest.raises(IsolationError, match="(?i)installed|persistent"):
        bootstrap_dev_environment(
            worktree_root=tmp_path / "wt",
            base=tmp_path / "b",
            env={"RAG_PROFILE": "installed"},
        )


def test_bootstrap_teardown_is_idempotent(tmp_path):
    env = bootstrap_dev_environment(
        worktree_root=tmp_path / "wt",
        base=tmp_path / "b",
        env={"RAG_PROFILE": "dev"},
    )
    env.teardown()
    env.teardown()  # must not raise


def test_teardown_refuses_to_delete_outside_base(tmp_path):
    """Even a hand-built env cannot teardown a path outside its dev base."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "keep.txt").write_text("important")
    env = DevEnvironment(
        profile=PROFILE_DEV,
        worktree_root=tmp_path / "wt",
        data_dir=live,
        port=0,
        base=tmp_path / "devbase",
    )
    with pytest.raises(IsolationError):
        env.teardown()
    assert (live / "keep.txt").exists()  # untouched


# --- no autostart for dev/ci (S0.6) --------------------------------------


def test_profile_may_autostart_only_persistent():
    assert profile_may_autostart(PROFILE_INSTALLED) is True
    assert profile_may_autostart("gateway") is True
    assert profile_may_autostart(PROFILE_DEV) is False
    assert profile_may_autostart("ci") is False
