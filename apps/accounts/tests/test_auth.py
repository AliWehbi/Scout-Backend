import pytest
from django.core import mail

from conftest import PASSWORD

LOGIN = "/api/v1/auth/login/"


@pytest.mark.parametrize("identifier", ["karim.h", "KARIM.H", "karim@cedar.org", "Karim@Cedar.org"])
def test_login_with_username_or_email(world, anon, identifier):
    world.user("karim.h", email="karim@cedar.org")
    res = anon.post(LOGIN, {"identifier": identifier, "password": PASSWORD}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["user"]["username"] == "karim.h"
    assert res.data["access"] and res.data["refresh"]


def test_user_without_email_can_sign_in(world, anon):
    world.user("omar.t")
    res = anon.post(LOGIN, {"identifier": "omar.t", "password": PASSWORD}, format="json")
    assert res.status_code == 200
    assert res.data["user"]["email"] is None
    assert res.data["user"]["has_email"] is False


def test_wrong_password_is_rejected(world, anon):
    world.user("karim.h")
    res = anon.post(LOGIN, {"identifier": "karim.h", "password": "nope"}, format="json")
    assert res.status_code == 401
    assert res.data["code"] == "invalid_credentials"


def test_inactive_user_cannot_sign_in(world, anon):
    world.user("karim.h", is_active=False)
    res = anon.post(LOGIN, {"identifier": "karim.h", "password": PASSWORD}, format="json")
    assert res.status_code == 401


def test_username_and_email_are_case_insensitively_unique(world):
    from django.db import IntegrityError, transaction

    world.user("karim.h", email="k@x.org")
    with pytest.raises(IntegrityError), transaction.atomic():
        world.user("KARIM.H")
    with pytest.raises(IntegrityError), transaction.atomic():
        world.user("other", email="K@X.ORG")
    # several users may have no email
    world.user("a1")
    world.user("a2")


def test_password_reset_only_emails_users_with_an_email(world, anon):
    world.user("karim.h", email="karim@cedar.org")
    world.user("omar.t")
    for identifier in ("karim.h", "omar.t", "nobody"):
        res = anon.post("/api/v1/auth/password-reset/", {"identifier": identifier}, format="json")
        assert res.status_code == 200
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["karim@cedar.org"]


def test_password_reset_confirm(world, anon):
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    user = world.user("karim.h", email="karim@cedar.org")
    payload = {
        "uid": urlsafe_base64_encode(force_bytes(user.pk)),
        "token": default_token_generator.make_token(user),
        "new_password": "Camp-Fire-Night-77",
    }
    assert anon.post("/api/v1/auth/password-reset/confirm/", payload, format="json").status_code == 204
    res = anon.post(LOGIN, {"identifier": "karim.h", "password": "Camp-Fire-Night-77"}, format="json")
    assert res.status_code == 200
    # token is single-use
    assert anon.post("/api/v1/auth/password-reset/confirm/", payload, format="json").status_code == 400


def test_me_update_email_triggers_verification(world, client_for):
    user = world.user("karim.h")
    res = client_for(user).patch("/api/v1/me/", {"email": "New@Cedar.org", "preferred_language": "ar"}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["email"] == "new@cedar.org"
    assert res.data["email_verified"] is False
    assert res.data["preferred_language"] == "ar"
    assert len(mail.outbox) == 1


def test_me_update_rejects_taken_email(world, client_for):
    world.user("other", email="taken@cedar.org")
    user = world.user("karim.h")
    res = client_for(user).patch("/api/v1/me/", {"email": "TAKEN@cedar.org"}, format="json")
    assert res.status_code == 400


def test_verify_email(world, anon):
    from apps.accounts.emails import make_email_verification_token

    user = world.user("karim.h", email="karim@cedar.org")
    res = anon.post("/api/v1/auth/verify-email/", {"token": make_email_verification_token(user)}, format="json")
    assert res.status_code == 200
    user.refresh_from_db()
    assert user.email_verified
    assert anon.post("/api/v1/auth/verify-email/", {"token": "garbage"}, format="json").status_code == 400


def test_change_password(world, client_for):
    user = world.user("karim.h")
    client = client_for(user)
    assert (
        client.post(
            "/api/v1/me/password/", {"current_password": "bad", "new_password": "Another-Trail-99"}, format="json"
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/me/password/", {"current_password": PASSWORD, "new_password": "Another-Trail-99"}, format="json"
        ).status_code
        == 204
    )
    user.refresh_from_db()
    assert user.check_password("Another-Trail-99")
