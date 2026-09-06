"""Proof links — DESIGN §12: parsing, append-only, gate, enrichment."""

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import objects, proofs, questions
from app.proofs import ProofError, parse_url
from app.statemachine import ProofRequired, transition


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "theuser", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    return conn, agent, user, pid


def _at_review(conn, agent, user, pid, title="p"):
    tid = objects.create_task(conn, agent, pid, title)
    for to in ("plan", "proposed"):
        transition(conn, tid, to, agent)
    transition(conn, tid, "approved", user)
    transition(conn, tid, "building", agent)
    return tid


def test_parse_url_structured_refs():
    assert parse_url("https://github.com/own/repo/pull/42") == {
        "provider": "github",
        "repo": "own/repo",
        "number": "42",
        "kind": "pr",
    }
    assert parse_url("https://github.com/own/repo/issues/7")["kind"] == "issue"
    assert (
        parse_url("https://github.com/own/repo/commit/deadbeef99")["kind"] == "commit"
    )
    assert parse_url("https://gitlab.com/grp/proj/-/merge_requests/5") == {
        "provider": "gitlab",
        "repo": "grp/proj",
        "number": "5",
        "kind": "mr",
    }
    assert parse_url("https://gitlab.com/grp/proj/-/issues/3")["kind"] == "issue"
    other = parse_url("https://example.com/some/page")
    assert other == {"provider": "other", "repo": "", "number": "", "kind": "other"}


def test_add_proof_records_actor_and_event(env):
    conn, agent, _user, pid = env
    tid = objects.create_task(conn, agent, pid, "x")
    row = proofs.add_proof(conn, agent, tid, "https://github.com/o/r/pull/1")
    assert row["provider"] == "github" and row["kind"] == "pr"
    assert row["added_by"] == agent["id"]
    ev = conn.execute(
        "SELECT verb, obj_type FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev["verb"] == "proof.added" and ev["obj_type"] == "proof"


def test_structured_override_and_validation(env):
    conn, agent, _user, pid = env
    tid = objects.create_task(conn, agent, pid, "x")
    row = proofs.add_proof(
        conn,
        agent,
        tid,
        "https://gitea.internal/o/r/pulls/9",
        provider="gitea",
        repo="o/r",
        number="9",
        kind="pr",
    )
    assert row["provider"] == "gitea" and row["number"] == "9"
    with pytest.raises(ProofError):
        proofs.add_proof(conn, agent, tid, "https://example.com/x", kind="pr")
    with pytest.raises(ProofError):
        proofs.add_proof(conn, agent, tid, "")


def test_free_url_does_not_satisfy_gate_but_pr_does(env):
    conn, agent, user, pid = env
    tid = _at_review(conn, agent, user, pid)
    proofs.add_proof(conn, agent, tid, "https://example.com/look-ma")
    with pytest.raises(ProofRequired):
        transition(conn, tid, "review", agent)
    proofs.add_proof(conn, agent, tid, "https://github.com/o/r/pull/2")
    transition(conn, tid, "review", agent)


def test_trashed_proof_stops_counting(env):
    conn, agent, user, pid = env
    tid = _at_review(conn, agent, user, pid)
    row = proofs.add_proof(conn, agent, tid, "https://github.com/o/r/pull/3")
    with pytest.raises(ProofError):
        proofs.trash_proof(conn, agent, row["id"])  # clankers never remove
    proofs.trash_proof(conn, user, row["id"])
    with pytest.raises(ProofRequired):
        transition(conn, tid, "review", agent)


def test_frozen_task_refuses_proof_add(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "frozen")
    questions.ask(
        conn,
        user,
        pid,
        "why?",
        attach_type="task",
        attach_id=tid,
        to_identity_id=agent["id"],
    )
    with pytest.raises(ProofError):
        proofs.add_proof(conn, user, tid, "https://github.com/o/r/pull/4")
    qs = questions.list_questions(conn, attach_type="task", attach_id=tid)
    questions.answer(conn, agent, qs[0]["id"], "because")
    proofs.add_proof(conn, user, tid, "https://github.com/o/r/pull/4")


def test_check_inert_without_token_and_fixed_hosts_only(env, monkeypatch):
    conn, agent, user, pid = env
    monkeypatch.delenv("SLOPCLANKER_GITHUB_TOKEN", raising=False)
    tid = _at_review(conn, agent, user, pid)
    row = proofs.add_proof(conn, agent, tid, "https://github.com/o/r/pull/5")
    with pytest.raises(ProofError):
        proofs.check_proof(conn, row["id"])
    assert row["state"] is None

    called = []

    def fake_fetch(url, token):
        called.append(url)
        return {"state": "closed", "merged": True}

    out = proofs.check_proof(conn, row["id"], fetch=fake_fetch)
    assert out["state"] == "merged" and out["state_checked_at"]
    assert called == ["https://api.github.com/repos/o/r/pulls/5"]

    other = proofs.add_proof(conn, agent, tid, "https://example.com/nope")
    with pytest.raises(ProofError):
        proofs.check_proof(conn, other["id"])  # unverified: never fetched


def test_check_maps_gitlab_and_skips_commits(env, monkeypatch):
    conn, agent, user, pid = env
    monkeypatch.setenv("SLOPCLANKER_GITLAB_TOKEN", "t")
    tid = _at_review(conn, agent, user, pid)
    mr = proofs.add_proof(conn, agent, tid, "https://gitlab.com/g/p/-/merge_requests/8")
    out = proofs.check_proof(conn, mr["id"], fetch=lambda url, tok: {"state": "opened"})
    assert out["state"] == "open"
    cm = proofs.add_proof(conn, agent, tid, "https://gitlab.com/g/p/-/commit/abc12345")
    with pytest.raises(ProofError):
        proofs.check_proof(conn, cm["id"])


def test_check_task_checks_all_mr_pr_and_reports(env, monkeypatch):
    conn, agent, user, pid = env
    monkeypatch.setenv("SLOPCLANKER_GITHUB_TOKEN", "t")
    tid = _at_review(conn, agent, user, pid)
    proofs.add_proof(conn, agent, tid, "https://github.com/o/r/pull/10")
    proofs.add_proof(conn, agent, tid, "https://example.com/wiki")
    rows = proofs.check_task(
        conn, tid, fetch=lambda url, tok: {"state": "open", "merged": False}
    )
    assert len(rows) == 1 and rows[0]["state"] == "open"
