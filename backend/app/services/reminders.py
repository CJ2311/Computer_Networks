"""One overdue reminder per loan; at most one automatic SMTP attempt.

Claim is committed as UNCERTAIN before SMTP. Crash/ambiguous outcomes require
operator reconciliation instead of an automatic duplicate message.
"""
from dataclasses import dataclass
from email.message import EmailMessage
import os
import re
import smtplib
import ssl
from uuid import uuid4
from sqlalchemy import select, exists, func
from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.operations import QuantityLoan, OverdueReminder
from backend.app.models.user import User
from backend.app.services.inventory_transactions import transaction, flush_stage
from backend.app.services.maintenance import _now


def valid_address(value):
    return isinstance(value,str) and len(value)<=254 and re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?",value) is not None


@dataclass(frozen=True,repr=False)
class SMTPConfig:
    host: str
    port: int
    sender: str
    mode: str
    username: str | None
    password: str | None

    @classmethod
    def load(cls):
        if os.getenv("OVERDUE_EMAIL_ENABLED","false")!="true":
            return None
        try:
            config=cls(os.environ["SMTP_HOST"],int(os.getenv("SMTP_PORT","587")),os.environ["SMTP_FROM"],
                       os.getenv("SMTP_TLS_MODE","starttls"),os.getenv("SMTP_USERNAME"),os.getenv("SMTP_PASSWORD"))
            if (not config.host or any(c.isspace() for c in config.host) or not 1<=config.port<=65535
                    or not valid_address(config.sender) or config.mode not in {"starttls","implicit"}
                    or bool(config.username)!=bool(config.password)):
                raise ValueError
            return config
        except (KeyError,ValueError):
            raise ValueError("SMTP configuration is invalid") from None


@transaction
def enqueue_overdue(db,limit=100):
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Reminder batch must be between one and 100")
    if not db.scalar(select(func.pg_try_advisory_xact_lock(714821903,2))):
        return 0
    now=_now(db)
    count=0
    for model,field in ((EquipmentCheckout,"checkout_id"),(QuantityLoan,"quantity_loan_id")):
        if count==limit:
            break
        relation=getattr(OverdueReminder,field)
        statement=select(model.id).where(model.returned_at.is_(None),model.due_at<now,
            ~exists(select(OverdueReminder.id).where(relation==model.id))).order_by(model.due_at,model.id).limit(limit-count)
        for loan_id in db.scalars(statement).all():
            record=OverdueReminder(id=uuid4(),**{field:loan_id},state="PENDING",attempts=0)
            db.add(record);flush_stage(db,record);count+=1
    return count


@dataclass(frozen=True)
class Delivery:
    id: object
    email: str
    loan_id: object
    due_at: object


@transaction
def claim_reminder(db):
    job=db.scalar(select(OverdueReminder).where(OverdueReminder.state=="PENDING").order_by(OverdueReminder.id).limit(1).with_for_update(skip_locked=True))
    if job is None:
        return None
    model=EquipmentCheckout if job.checkout_id else QuantityLoan
    loan=db.get(model,job.checkout_id or job.quantity_loan_id,populate_existing=True)
    user=db.get(User,loan.student_id,populate_existing=True) if loan else None
    now=_now(db)
    if loan is None or loan.returned_at or loan.due_at>=now or user is None or not user.is_active or not valid_address(user.email):
        job.state="CANCELLED";flush_stage(db,job)
        return False
    job.state="UNCERTAIN";job.attempts+=1;job.last_attempt_at=now
    flush_stage(db,job)
    return Delivery(job.id,user.email,loan.id,loan.due_at)


@transaction
def mark_sent(db,job_id):
    job=db.scalar(select(OverdueReminder).where(OverdueReminder.id==job_id).with_for_update())
    if job is None or job.state!="UNCERTAIN":
        raise ValueError("Unexpected reminder state")
    job.state="SENT";job.sent_at=_now(db);flush_stage(db,job)


def deliver(config,delivery):
    message=EmailMessage()
    message["From"]=config.sender;message["To"]=delivery.email
    message["Subject"]="Laboratory equipment return reminder"
    message["Message-ID"]="<inventory-reminder-"+str(delivery.id)+"@"+config.sender.split("@",1)[1]+">"
    message.set_content("Please return your overdue laboratory equipment.\nLoan: "+str(delivery.loan_id)+"\nDue: "+delivery.due_at.isoformat()+"\nContact the laboratory staff if already returned.")
    context=ssl.create_default_context()
    factory=smtplib.SMTP_SSL if config.mode=="implicit" else smtplib.SMTP
    options=dict(timeout=10)
    if config.mode=="implicit":
        options["context"]=context
    with factory(config.host,config.port,**options) as smtp:
        if config.mode=="starttls":
            smtp.ehlo();smtp.starttls(context=context);smtp.ehlo()
        if config.username:
            smtp.login(config.username,config.password)
        refused=smtp.send_message(message)
        if refused:
            raise RuntimeError("Reminder delivery was not accepted")


def process_reminders(session_factory,config,limit=100,sender=deliver):
    if config is None:
        return dict(enqueued=0,sent=0,uncertain=0)
    with session_factory() as db:
        enqueued=enqueue_overdue(db,limit)
    counts=dict(enqueued=enqueued,sent=0,uncertain=0)
    for _ in range(limit):
        with session_factory() as db:
            delivery=claim_reminder(db)
        if delivery is None:
            break
        if delivery is False:
            continue
        try:
            sender(config,delivery)  # outside every database transaction
        except Exception:
            counts["uncertain"]+=1
            continue
        with session_factory() as db:
            mark_sent(db,delivery.id)
        counts["sent"]+=1
    return counts
