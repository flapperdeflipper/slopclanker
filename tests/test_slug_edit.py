"""Slug edits: projects + stacks — rename, uniqueness, validation."""

import pytest
from helpers_ids import PW

from app import bootstrap, db, objects, setup


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "t.db"
    bootstrap.ensure(path)
    c = db.connect(path)
    boss = setup.create_superadmin(c, "root", PW)
    sid = objects.create_stack(c, boss, name="Stack", slug="stack")
    objects.create_project(c, boss, name="Proj", stack_id=sid, slug="proj")
    yield c, boss, sid
    c.close()


def test_project_slug_rename(conn):
    c, boss, sid = conn
    objects.edit_project(c, boss, 1, slug="renamed")
    assert objects.get_project(c, 1)["slug"] == "renamed"


def test_project_slug_conflict_rejected(conn):
    c, boss, sid = conn
    objects.create_project(c, boss, name="Other", stack_id=sid, slug="other")
    with pytest.raises(objects.SlugInvalid):
        objects.edit_project(c, boss, 1, slug="other")


def test_project_slug_format_rejected(conn):
    c, boss, _ = conn
    with pytest.raises(objects.SlugInvalid):
        objects.edit_project(c, boss, 1, slug="Bad Slug!")


def test_stack_slug_rename(conn):
    c, boss, _ = conn
    objects.edit_stack(c, boss, 1, slug="renamed-stack")
    st = c.execute("SELECT slug FROM stacks WHERE id = 1").fetchone()
    assert st["slug"] == "renamed-stack"


def test_same_slug_kept_is_allowed(conn):
    c, boss, _ = conn
    objects.edit_project(c, boss, 1, slug="proj")  # own slug: no conflict
    assert objects.get_project(c, 1)["slug"] == "proj"
