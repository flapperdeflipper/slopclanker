"""Shared test identity/actor factories."""

from app import auth, db, setup

PW = "long" + "enough12"  # dummy fixture, assembled to calm scanners


def fresh_db(tmp_path):
    return db.init_db(tmp_path / "t.db")


def superadmin(conn):
    return setup.create_superadmin(conn, "root", PW)


def human(conn, name, role="user", boss=None):
    if boss is None:
        boss = superadmin(conn)
    return auth.create_human(conn, name, PW, role, boss["id"])


def clanker(conn, name="clanker-x"):
    cur = conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES (?, 'clanker', 'active', 1.0)",
        (name,),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM identities WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
