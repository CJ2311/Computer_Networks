"""SMTP is replaced in every test; no actual email is sent."""
from datetime import datetime,timezone
from unittest.mock import Mock
import pytest
from backend.app.services import reminders


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for key in ("OVERDUE_EMAIL_ENABLED","SMTP_HOST","SMTP_PORT","SMTP_FROM","SMTP_TLS_MODE","SMTP_USERNAME","SMTP_PASSWORD"):
        monkeypatch.delenv(key,raising=False)


def test_disabled_reminders_do_not_open_session_or_smtp():
    factory=Mock(side_effect=AssertionError("No database"))
    assert reminders.process_reminders(factory,None)==dict(enqueued=0,sent=0,uncertain=0)
    factory.assert_not_called()


def test_enabled_requires_explicit_configuration(monkeypatch):
    monkeypatch.setenv("OVERDUE_EMAIL_ENABLED","true")
    with pytest.raises(ValueError,match="SMTP configuration is invalid"):
        reminders.SMTPConfig.load()


@pytest.mark.parametrize("mode",["starttls","implicit"])
def test_tls_is_required_before_auth_and_send(monkeypatch,mode):
    smtp=Mock();manager=Mock();manager.__enter__=Mock(return_value=smtp);manager.__exit__=Mock(return_value=False)
    factory=Mock(return_value=manager)
    monkeypatch.setattr(reminders.smtplib,"SMTP" if mode=="starttls" else "SMTP_SSL",factory)
    smtp.send_message.return_value={}
    config=reminders.SMTPConfig("smtp.example.invalid",587,"lab@example.invalid",mode,"username","synthetic-secret")
    delivery=reminders.Delivery("job","student@example.invalid","loan",datetime.now(timezone.utc))
    reminders.deliver(config,delivery)
    smtp.login.assert_called_once_with("username","synthetic-secret")
    smtp.send_message.assert_called_once()
    assert factory.call_args.kwargs["timeout"]==10
    if mode=="starttls":
        calls=[call[0] for call in smtp.method_calls]
        assert calls.index("starttls")<calls.index("login")<calls.index("send_message")
    else:
        assert "context" in factory.call_args.kwargs
    assert "synthetic-secret" not in repr(config)


@pytest.mark.parametrize("address",["student@example.invalid\r\nBcc: other@example.invalid","student@example.invalid\n","bad address",""])
def test_header_injection_rejected(address):
    assert not reminders.valid_address(address)


def test_no_plaintext_smtp_mode(monkeypatch):
    for key,value in dict(OVERDUE_EMAIL_ENABLED="true",SMTP_HOST="smtp.example.invalid",SMTP_FROM="lab@example.invalid",SMTP_TLS_MODE="none").items():
        monkeypatch.setenv(key,value)
    with pytest.raises(ValueError):
        reminders.SMTPConfig.load()



def test_acknowledged_smtp_followed_by_database_failure_is_not_replayed(monkeypatch):
    from contextlib import nullcontext
    delivery=reminders.Delivery("job","student@example.invalid","loan",datetime.now(timezone.utc))
    monkeypatch.setattr(reminders,"enqueue_overdue",Mock(side_effect=[1,0]))
    monkeypatch.setattr(reminders,"claim_reminder",Mock(side_effect=[delivery,None]))
    monkeypatch.setattr(reminders,"mark_sent",Mock(side_effect=RuntimeError("Injected acknowledgement persistence failure")))
    sender=Mock()
    factory=lambda:nullcontext(Mock())
    with pytest.raises(RuntimeError):
        reminders.process_reminders(factory,object(),sender=sender)
    assert reminders.process_reminders(factory,object(),sender=sender)==dict(enqueued=0,sent=0,uncertain=0)
    sender.assert_called_once()
