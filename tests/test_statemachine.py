"""The nine-state machine: DESIGN §3 proven, negatives first."""

import time

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import chain, objects
from app.statemachine import (
    BlockedByQuestions,
    HumanRequired,
    IllegalTransition,
    ProofRequired,
    TodosOutstanding,
    VersionConflict,
    is_human_only,
    transition,
)


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "theuser", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    return conn, agent, user, pid


def _task(conn, pid, **kw):
    tid = objects.create_task(conn, *(_ids(conn, pid)), **kw)
    return tid


def _ids(conn, pid):
    actor = conn.execute("SELECT * FROM identities WHERE kind='clanker'").fetchone()
    return conn, actor, pid


def test_transition_table_shape():
    assert is_human_only("proposed", "approved")
    assert is_human_only("review", "done")
    assert is_human_only("idea", "trashed")
    assert is_human_only("done", "previous")
    assert not is_human_only("idea", "plan")
    assert not is_human_only("building", "review")


def test_happy_walk(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "walk")
    for to in ("plan", "proposed"):
        transition(conn, tid, to, agent)
    transition(conn, tid, "approved", user)
    transition(conn, tid, "building", agent)
    conn.execute(
        "INSERT INTO proofs(task_id, provider, kind, url, added_by, added_at)"
        " VALUES (?, 'github', 'pr', 'https://x', ?, ?)",
        (tid, agent["id"], time.time()),
    )
    conn.commit()
    transition(conn, tid, "review", agent)
    t = transition(conn, tid, "done", user)
    assert t["state"] == "done" and t["previous_state"] == "review"
    t = transition(conn, tid, "previous", user, note="needs work")
    assert t["state"] == "review" and t["previous_state"] is None
    conn.close()


def test_skip_states_rejected(env):
    conn, agent, _, pid = env
    tid = objects.create_task(conn, agent, pid, "skip")
    with pytest.raises(IllegalTransition):
        transition(conn, tid, "done", agent)
    with pytest.raises(IllegalTransition):
        transition(conn, tid, "approved", agent)
    with pytest.raises(IllegalTransition):
        transition(conn, tid, "bogus", agent)
    conn.close()


def test_clanker_denied_human_only_and_logged(env):
    conn, agent, _, pid = env
    tid = objects.create_task(conn, agent, pid, "denied")
    transition(conn, tid, "plan", agent)
    transition(conn, tid, "proposed", agent)
    with pytest.raises(HumanRequired):
        transition(conn, tid, "approved", agent)
    with pytest.raises(HumanRequired):
        transition(conn, tid, "trashed", agent)
    denials = conn.execute(
        "SELECT COUNT(*) FROM events WHERE verb = 'task.transition_denied'"
    ).fetchone()[0]
    assert denials == 2
    conn.close()


def test_proof_gate(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "proof")
    for to in ("plan", "proposed"):
        transition(conn, tid, to, agent)
    transition(conn, tid, "approved", user)
    transition(conn, tid, "building", agent)
    with pytest.raises(ProofRequired):
        transition(conn, tid, "review", agent)
    objects.edit_task(conn, user, tid, proof_waived=True)
    transition(conn, tid, "review", agent)
    assert (
        conn.execute(
            "SELECT proof_waived_by FROM tasks WHERE id = ?", (tid,)
        ).fetchone()[0]
        == user["id"]
    )
    with pytest.raises(objects.ObjectError):
        objects.edit_task(conn, agent, tid, proof_waived=False)
    conn.close()


def test_waiver_is_human_only(env):
    conn, agent, _, pid = env
    tid = objects.create_task(conn, agent, pid, "w")
    with pytest.raises(objects.ObjectError):
        objects.edit_task(conn, agent, tid, proof_waived=True)
    conn.close()


def test_done_gating(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "gate")
    for to in ("plan", "proposed"):
        transition(conn, tid, to, agent)
    transition(conn, tid, "approved", user)
    transition(conn, tid, "building", agent)
    conn.execute(
        "INSERT INTO proofs(task_id, provider, kind, url, added_by, added_at)"
        " VALUES (?, 'github', 'pr', 'https://x', ?, ?)",
        (tid, agent["id"], time.time()),
    )
    conn.commit()
    transition(conn, tid, "review", agent)
    todo = objects.add_todo(conn, agent, tid, "left")
    with pytest.raises(TodosOutstanding):
        transition(conn, tid, "done", user)
    objects.tick_todo(conn, agent, todo, True)
    transition(conn, tid, "done", user)
    conn.close()


def test_done_override_with_reason(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "over")
    for to in ("plan", "proposed"):
        transition(conn, tid, to, agent)
    transition(conn, tid, "approved", user)
    transition(conn, tid, "building", agent)
    conn.execute(
        "INSERT INTO proofs(task_id, provider, kind, url, added_by, added_at)"
        " VALUES (?, 'github', 'pr', 'https://x', ?, ?)",
        (tid, agent["id"], time.time()),
    )
    conn.commit()
    transition(conn, tid, "review", agent)
    objects.add_todo(conn, agent, tid, "left")
    with pytest.raises(TodosOutstanding):
        transition(conn, tid, "done", user, note="  ")
    transition(conn, tid, "done", user, note="good enough for now")
    note = conn.execute(
        "SELECT note FROM transitions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()[0]
    assert "good enough" in note
    conn.close()


def test_open_question_freezes_all(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "q")
    conn.execute(
        "INSERT INTO questions(project_id, body, asked_by, asked_to_group,"
        " attach_type, attach_id, status, created_at)"
        " VALUES (?, 'why?', ?, 'everyone', 'task', ?, 'open', ?)",
        (pid, user["id"], tid, time.time()),
    )
    conn.commit()
    with pytest.raises(BlockedByQuestions) as exc:
        transition(conn, tid, "plan", agent)
    assert exc.value.questions[0]["body"] == "why?"
    with pytest.raises(objects.Frozen):
        objects.add_todo(conn, agent, tid, "nope")
    with pytest.raises(objects.Frozen):
        objects.edit_task(conn, user, tid, body="nope")
    conn.execute("UPDATE questions SET status = 'answered' WHERE attach_id = ?", (tid,))
    conn.commit()
    transition(conn, tid, "plan", agent)
    conn.close()


def test_not_done_returns_to_review_and_addresses_assignee(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "nd", assignee_id=agent["id"])
    for to in ("plan", "proposed"):
        transition(conn, tid, to, agent)
    transition(conn, tid, "approved", user)
    transition(conn, tid, "building", agent)
    conn.execute(
        "INSERT INTO proofs(task_id, provider, kind, url, added_by, added_at)"
        " VALUES (?, 'github', 'pr', 'https://x', ?, ?)",
        (tid, agent["id"], time.time()),
    )
    conn.commit()
    transition(conn, tid, "review", agent)
    transition(conn, tid, "done", user)
    transition(conn, tid, "previous", user, note="redo")
    row = conn.execute(
        "SELECT to_identity_id, verb FROM events WHERE verb = 'task.transitioned'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["to_identity_id"] == agent["id"]
    conn.close()


def test_trash_and_restore(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "t")
    transition(conn, tid, "plan", agent)
    t = transition(conn, tid, "trashed", user)
    assert t["state"] == "trashed" and t["previous_state"] == "plan"
    t = transition(conn, tid, "previous", user)
    assert t["state"] == "plan"
    with pytest.raises(HumanRequired):
        transition(conn, tid, "trashed", agent)
    conn.close()


def test_paused_roundtrip(env):
    conn, agent, _user, pid = env
    tid = objects.create_task(conn, agent, pid, "p")
    transition(conn, tid, "plan", agent)
    transition(conn, tid, "paused", agent)
    transition(conn, tid, "review", agent)
    transition(conn, tid, "paused", agent)
    t = transition(conn, tid, "plan", agent)
    assert t["state"] == "plan"
    conn.close()


def test_version_conflict(env):
    conn, agent, _user, pid = env
    tid = objects.create_task(conn, agent, pid, "v")
    with pytest.raises(VersionConflict):
        transition(conn, tid, "plan", agent, version=999)
    transition(conn, tid, "plan", agent, version=1)
    conn.close()


def test_transition_log_is_chained(env):
    conn, agent, user, pid = env
    tid = objects.create_task(conn, agent, pid, "c")
    transition(conn, tid, "plan", agent)
    transition(conn, tid, "proposed", agent)
    transition(conn, tid, "approved", user)
    assert chain.verify_chain(conn, "transitions")[0]
    conn.close()


def test_arrival_notifications(env):
    conn, agent, _user, pid = env
    tid = objects.create_task(conn, agent, pid, "n")
    transition(conn, tid, "plan", agent)
    transition(conn, tid, "proposed", agent)
    kinds = [
        r["kind"]
        for r in conn.execute("SELECT kind FROM notifications ORDER BY id DESC LIMIT 1")
    ]
    assert kinds == ["attention"]
    conn.close()
