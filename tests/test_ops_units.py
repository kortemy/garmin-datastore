"""M6 operations helpers: migrations ledger, worker login, doctor checks (no database)."""

import base64
import hashlib
import hmac

import pytest

from gds import ops


def test_scram_verifier_matches_rfc7677_exchange():
    """The verifier must let Postgres complete RFC 7677's example exchange (user/pencil)."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    v = ops.scram_verifier("pencil", salt=salt)
    head, keys = v.split("$", 1)[1].split("$")
    stored_key, server_key = (base64.b64decode(k) for k in keys.split(":"))
    assert head == "4096:W22ZaJ0SNY7soEsUEjb6gQ=="

    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth_message = (f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
                    f"c=biws,r={nonce}").encode()
    server_signature = hmac.new(server_key, auth_message, "sha256").digest()
    assert base64.b64encode(server_signature).decode() == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="

    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_signature = hmac.new(stored_key, auth_message, "sha256").digest()
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature, strict=True))
    assert hashlib.sha256(client_key).digest() == stored_key


def test_scram_verifier_salts_each_call():
    assert ops.scram_verifier("x") != ops.scram_verifier("x")


@pytest.mark.parametrize("admin, expected", [
    ("postgresql://postgres.abcref:p%40ss@aws-0-eu-west-1.pooler.supabase.com:6543/postgres",
     "postgresql://gds_worker.abcref:s3cr%2Ft@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"),
    ("postgresql://postgres:pw@db.abcref.supabase.co:5432/postgres?sslmode=require",
     "postgresql://gds_worker:s3cr%2Ft@db.abcref.supabase.co:5432/postgres?sslmode=require"),
])
def test_worker_url_swaps_user_and_password(admin, expected):
    url = ops.worker_url(admin, "s3cr/t")
    assert url == expected
    assert ops.url_password(url) == "s3cr/t"


def test_migration_files_are_ordered_and_named():
    files = ops.migration_files()
    assert files and [m.version for m in files] == sorted(m.version for m in files)
    assert files[0].name == "m0_spike"
    assert any(m.name == "m6_worker_role" for m in files)


def test_migration_files_reject_odd_names(tmp_path):
    (tmp_path / "001_init.sql").write_text("select 1")
    with pytest.raises(ValueError):
        ops.migration_files(tmp_path)


class _Conn:
    """Fake connection answering the ledger queries."""

    def __init__(self, ledger: list[tuple[str, str]] | None):
        self.ledger = ledger

    def execute(self, query, params=None):
        class R:
            def __init__(self, rows):
                self.rows = rows

            def fetchone(self):
                return self.rows[0]

            def fetchall(self):
                return self.rows

        if "to_regclass" in query:
            return R([(None if self.ledger is None else "supabase_migrations.schema_migrations",)])
        return R(self.ledger)


def test_pending_matches_ledger_by_version_or_name(tmp_path):
    for f in ("20260101000000_a.sql", "20260102000000_b.sql", "20260103000000_c.sql"):
        (tmp_path / f).write_text("select 1")
    files = ops.migration_files(tmp_path)
    # a: recorded under its file version; b: recorded by the Supabase MCP under another timestamp.
    conn = _Conn([("20260101000000", "a"), ("20261231235959", "b")])
    assert [m.name for m in ops.pending_migrations(conn, files)] == ["c"]
    assert [m.name for m in ops.pending_migrations(_Conn(None), files)] == ["a", "b", "c"]


def test_env_check_flags_placeholders_and_missing_optionals():
    env = {"DATABASE_URL": "postgresql://postgres.<project-ref>:<password>@<pooler-host>:6543/postgres",
           "TOKEN_ENC_KEY": "k", "TICK_SECRET": "t", "MCP_PATH_SECRET": "m", "GDS_BASE_URL": "https://x.example"}
    checks = list(ops.check_env(env))
    assert checks[0].level == ops.FAIL and "DATABASE_URL" in checks[0].detail
    assert {c.name for c in checks if c.level == ops.WARN} == {"worker login", "connector password", "alerts"}
    assert all("t" != c.detail for c in checks)  # never echoes values
