import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import auth, security
from portal.db import Base, init_engine, session_scope
from portal.models import LoginToken, Setting, Session as SessionModel, User, utcnow


@pytest.fixture
def db():
    engine = init_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def user(db):
    with session_scope() as s:
        u = User(display_name="Chris", person_slug="Chris", access_code_hash="x")
        s.add(u)
        s.flush()
        return u.id


class TestGate:
    def test_no_code_configured_rejects_everything(self, db):
        with session_scope() as s:
            assert auth.verify_gate_code(s, "anything") is False

    def test_correct_code_accepted(self, db):
        with session_scope() as s:
            s.add(Setting(key="invite_code_hash", value=security.hash_secret("open-sesame")))
        with session_scope() as s:
            assert auth.verify_gate_code(s, "open-sesame") is True

    def test_wrong_code_rejected(self, db):
        with session_scope() as s:
            s.add(Setting(key="invite_code_hash", value=security.hash_secret("open-sesame")))
        with session_scope() as s:
            assert auth.verify_gate_code(s, "wrong-code") is False

    def test_gate_cookie_values_are_high_entropy_and_unique(self, db):
        values = {auth.gate_cookie_value() for _ in range(50)}
        assert len(values) == 50


class TestLoginTokenLifecycle:
    def test_issue_and_redeem(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            raw = auth.issue_login_token(s, u)

        with session_scope() as s:
            result = auth.redeem_login_token(s, raw)
            assert result is not None
            assert result.user.id == user
            assert result.raw_session_token

    def test_token_cannot_be_redeemed_twice(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            raw = auth.issue_login_token(s, u)

        with session_scope() as s:
            first = auth.redeem_login_token(s, raw)
            assert first is not None

        with session_scope() as s:
            second = auth.redeem_login_token(s, raw)
            assert second is None

    def test_expired_token_rejected(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            raw = security.generate_token()
            s.add(LoginToken(
                user_id=u.id, token_hash=security.hash_token(raw),
                purpose="INVITE", expires_at=utcnow() - timedelta(days=1),
            ))

        with session_scope() as s:
            assert auth.redeem_login_token(s, raw) is None

    def test_garbage_token_rejected(self, db, user):
        with session_scope() as s:
            assert auth.redeem_login_token(s, "not-a-real-token") is None

    def test_blocked_user_cannot_redeem(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            u.is_blocked = True
            raw = auth.issue_login_token(s, u)

        with session_scope() as s:
            assert auth.redeem_login_token(s, raw) is None

    def test_redeeming_creates_a_session_row(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            raw = auth.issue_login_token(s, u)
        with session_scope() as s:
            result = auth.redeem_login_token(s, raw)
        with session_scope() as s:
            count = s.query(SessionModel).filter(SessionModel.user_id == user).count()
            assert count == 1


class TestCurrentUserLookup:
    def test_valid_session_resolves_to_user(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            raw = auth.issue_login_token(s, u)
        with session_scope() as s:
            result = auth.redeem_login_token(s, raw)
            session_token = result.raw_session_token

        with session_scope() as s:
            token_hash = security.hash_token(session_token)
            row = s.query(SessionModel).filter(SessionModel.token_hash == token_hash).one()
            assert row.user_id == user
            assert row.revoked_at is None
            assert row.expires_at > utcnow()

    def test_revoked_session_no_longer_resolves(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            raw = auth.issue_login_token(s, u)
        with session_scope() as s:
            result = auth.redeem_login_token(s, raw)
            session_token = result.raw_session_token

        with session_scope() as s:
            auth.revoke_session(s, session_token)

        with session_scope() as s:
            token_hash = security.hash_token(session_token)
            row = s.query(SessionModel).filter(SessionModel.token_hash == token_hash).one()
            assert row.revoked_at is not None

    def test_revoke_all_sessions_revokes_every_active_one(self, db, user):
        with session_scope() as s:
            u = s.get(User, user)
            raw1 = auth.issue_login_token(s, u)
        with session_scope() as s:
            auth.redeem_login_token(s, raw1)

        with session_scope() as s:
            u = s.get(User, user)
            raw2 = auth.issue_login_token(s, u)
        with session_scope() as s:
            auth.redeem_login_token(s, raw2)

        with session_scope() as s:
            u = s.get(User, user)
            auth.revoke_all_sessions(s, u)

        with session_scope() as s:
            active = (
                s.query(SessionModel)
                .filter(SessionModel.user_id == user, SessionModel.revoked_at.is_(None))
                .count()
            )
            assert active == 0


class TestAccessCodeLogin:
    def test_correct_code_logs_in(self, db):
        with session_scope() as s:
            u = User(display_name="Admin", person_slug="Admin",
                      access_code_hash=security.hash_secret("hunter2"), is_admin=True)
            s.add(u)

        with session_scope() as s:
            result = auth.login_with_access_code(s, "Admin", "hunter2")
            assert result is not None
            assert result.user.is_admin is True

    def test_wrong_code_rejected(self, db):
        with session_scope() as s:
            s.add(User(display_name="Admin", person_slug="Admin",
                        access_code_hash=security.hash_secret("hunter2"), is_admin=True))

        with session_scope() as s:
            assert auth.login_with_access_code(s, "Admin", "wrong") is None

    def test_unknown_slug_rejected(self, db):
        with session_scope() as s:
            assert auth.login_with_access_code(s, "NoSuchUser", "anything") is None

    def test_blocked_user_rejected(self, db):
        with session_scope() as s:
            s.add(User(display_name="Chris", person_slug="Chris",
                        access_code_hash=security.hash_secret("code123"), is_blocked=True))

        with session_scope() as s:
            assert auth.login_with_access_code(s, "Chris", "code123") is None


class TestSlidingExpiry:
    def test_active_session_expiry_extends_on_use(self, db, user):
        from portal.auth import get_current_user

        with session_scope() as s:
            u = s.get(User, user)
            raw = auth.issue_login_token(s, u)
        with session_scope() as s:
            result = auth.redeem_login_token(s, raw)
            session_token = result.raw_session_token

        with session_scope() as s:
            token_hash = security.hash_token(session_token)
            row = s.query(SessionModel).filter(SessionModel.token_hash == token_hash).one()
            original_expiry = row.expires_at
            row.expires_at = utcnow() + timedelta(days=1)  # simulate near-expiry

        class FakeRequest:
            cookies = {auth.SESSION_COOKIE: session_token}

        with session_scope() as s:
            resolved = get_current_user(FakeRequest(), s)
            assert resolved is not None

        with session_scope() as s:
            token_hash = security.hash_token(session_token)
            row = s.query(SessionModel).filter(SessionModel.token_hash == token_hash).one()
            assert row.expires_at > utcnow() + timedelta(days=50)


class TestWorkerToken:
    def test_matching_tokens_verify(self):
        assert auth.verify_worker_token("secret123", "secret123") is True

    def test_mismatched_tokens_rejected(self):
        assert auth.verify_worker_token("wrong", "secret123") is False
