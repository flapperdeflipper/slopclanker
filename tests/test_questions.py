"""Questions: addressing, freeze scope, escape hatches — DESIGN §11."""

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import comms, decisions, links, objects, questions, ratelimit
from app.statemachine import BlockedByQuestions, transition


@pytest.fixture
def env(tmp_path):
    ratelimit.reset()
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    other = clanker(conn, "clanker-y")
    user = human(conn, "userx", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    return conn, boss, agent, other, user, pid


def test_ask_target_validation(env):
    conn, _boss, agent, _other, user, pid = env
    q = questions.ask(conn, agent, pid, "to one", to_identity_id=user["id"])
    assert q
    with pytest.raises(questions.QuestionError):
        questions.ask(
            conn, agent, pid, "both", to_identity_id=user["id"], to_group="everyone"
        )
    with pytest.raises(questions.QuestionError):
        questions.ask(conn, agent, pid, "neither")
    with pytest.raises(questions.InvalidTarget):
        questions.ask(conn, agent, pid, "bad group", to_group="bots")
    with pytest.raises(questions.InvalidTarget):
        questions.ask(conn, agent, pid, "dead", to_identity_id=4242)
    with pytest.raises(questions.QuestionError):
        questions.ask(
            conn,
            agent,
            pid,
            "bad attach",
            to_group="everyone",
            attach_type="task",
            attach_id=999,
        )
    conn.close()


def test_ask_rate_limit(env):
    conn, _boss, agent, other, _user, pid = env
    for _ in range(10):
        questions.ask(conn, agent, pid, "q", to_group="everyone")
    with pytest.raises(questions.RateLimited):
        questions.ask(conn, agent, pid, "one more", to_group="everyone")
    questions.ask(conn, other, pid, "other identity unaffected", to_group="everyone")
    conn.close()


def test_answer_permissions(env):
    conn, _boss, agent, other, user, pid = env
    qid = questions.ask(conn, agent, pid, "why?", to_identity_id=user["id"])
    with pytest.raises(questions.NotAddressee):
        questions.answer(conn, other, qid, "not me")
    with pytest.raises(questions.QuestionError):
        questions.answer(conn, user, qid, "   ")
    q = questions.answer(conn, user, qid, "because")
    assert q["status"] == "answered" and q["answered_by"] == user["id"]
    with pytest.raises(questions.AlreadyResolved):
        questions.answer(conn, user, qid, "again")
    conn.close()


def test_group_answer(env):
    conn, _boss, agent, other, user, pid = env
    qid = questions.ask(conn, agent, pid, "any human?", to_group="humans")
    with pytest.raises(questions.NotAddressee):
        questions.answer(conn, other, qid, "clanker cannot")
    questions.answer(conn, user, qid, "human can")
    qid2 = questions.ask(conn, user, pid, "any clanker?", to_group="clankers")
    questions.answer(conn, other, qid2, "clanker can")
    conn.close()


def test_withdraw_and_reassign(env):
    conn, boss, agent, other, user, pid = env
    qid = questions.ask(conn, agent, pid, "hm", to_identity_id=user["id"])
    with pytest.raises(questions.QuestionError):
        questions.withdraw(conn, other, qid)
    questions.withdraw(conn, agent, qid)
    qid2 = questions.ask(conn, agent, pid, "stuck", to_identity_id=other["id"])
    with pytest.raises(questions.QuestionError):
        questions.reassign(conn, user, qid2, user["id"])
    questions.reassign(conn, boss, qid2, user["id"])
    questions.answer(conn, user, qid2, "freed")
    qid3 = questions.ask(
        conn, agent, pid, "dead asker cleanup", to_identity_id=user["id"]
    )
    questions.withdraw(conn, boss, qid3)
    assert questions._get(conn, qid3)["status"] == "withdrawn"
    conn.close()


def test_freeze_is_object_scoped(env):
    conn, _boss, agent, other, user, pid = env
    tid = objects.create_task(conn, agent, pid, "frozen")
    todo = objects.add_todo(conn, agent, tid, "step")
    qid = questions.ask(
        conn,
        agent,
        pid,
        "why?",
        to_identity_id=user["id"],
        attach_type="task",
        attach_id=tid,
    )
    with pytest.raises(BlockedByQuestions):
        transition(conn, tid, "plan", agent)
    with pytest.raises(objects.Frozen):
        objects.tick_todo(conn, agent, todo, True)
    with pytest.raises(objects.Frozen):
        objects.edit_task(conn, user, tid, body="even human")
    qid2 = questions.ask(
        conn,
        other,
        pid,
        "still allowed?",
        to_group="everyone",
        attach_type="task",
        attach_id=tid,
    )
    comms.create_discussion(conn, agent, pid, "talking allowed")
    comms.post_chat(conn, agent, pid, "chat allowed")
    links.create(conn, agent, "task", tid, "project", pid)
    pid2 = objects.create_project(conn, agent, "unaffected")
    objects.create_task(conn, agent, pid2, "other work continues")
    questions.answer(conn, user, qid, "a1")
    with pytest.raises(BlockedByQuestions):
        transition(conn, tid, "plan", agent)
    questions.answer(conn, user, qid2, "a2")
    transition(conn, tid, "plan", agent)
    conn.close()


def test_project_freeze_blocks_children_but_not_talk(env):
    conn, _boss, agent, other, _user, pid = env
    questions.ask(
        conn,
        agent,
        pid,
        "project scope",
        to_group="humans",
        attach_type="project",
        attach_id=pid,
    )
    with pytest.raises(objects.Frozen):
        objects.create_task(conn, agent, pid, "blocked")
    with pytest.raises(objects.Frozen):
        objects.edit_project(conn, agent, pid, description="blocked")
    comms.create_discussion(conn, agent, pid, "discussion ok")
    comms.post_chat(conn, agent, pid, "chat ok")
    links.create(conn, agent, "project", pid, "project", pid) if False else None
    questions.ask(conn, other, pid, "more questions ok", to_group="everyone")
    conn.close()


def test_todo_freeze(env):
    conn, _boss, agent, _other, user, pid = env
    tid = objects.create_task(conn, agent, pid, "t")
    todo = objects.add_todo(conn, agent, tid, "step")
    questions.ask(
        conn,
        agent,
        pid,
        "todo q",
        to_identity_id=user["id"],
        attach_type="todo",
        attach_id=todo,
    )
    with pytest.raises(objects.Frozen):
        objects.tick_todo(conn, agent, todo, True)
    conn.close()


def test_decision_freeze_and_flow(env):
    conn, boss, agent, _other, user, pid = env
    did = decisions.create(conn, agent, pid, "go with sqlite")
    qid = questions.ask(
        conn,
        agent,
        pid,
        "sure?",
        to_identity_id=user["id"],
        attach_type="decision",
        attach_id=did,
    )
    with pytest.raises(decisions.DecisionError):
        decisions.set_status(conn, user, did, "accepted")
    questions.answer(conn, user, qid, "yes")
    with pytest.raises(decisions.DecisionError):
        decisions.set_status(conn, agent, did, "accepted")
    d = decisions.set_status(conn, user, did, "accepted")
    assert d["status"] == "accepted"
    with pytest.raises(decisions.DecisionError):
        decisions.set_status(conn, boss, did, "rejected")
    conn.close()


def test_unattached_blocks_nothing(env):
    conn, _boss, agent, _other, _user, pid = env
    questions.ask(conn, agent, pid, "free floating", to_group="everyone")
    tid = objects.create_task(conn, agent, pid, "works")
    transition(conn, tid, "plan", agent)
    conn.close()


def test_to_me_listing(env):
    conn, _boss, agent, other, user, pid = env
    questions.ask(conn, agent, pid, "direct", to_identity_id=user["id"])
    questions.ask(conn, other, pid, "to humans", to_group="humans")
    questions.ask(conn, other, pid, "to clankers", to_group="clankers")
    mine = questions.list_questions(conn, to_actor=user)
    assert len(mine) == 2
    theirs = questions.list_questions(conn, to_actor=agent)
    assert len(theirs) == 1
    open_q = questions.list_questions(conn, open_only=True, to_actor=user)
    assert len(open_q) == 2
    conn.close()
