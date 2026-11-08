"""Application integration tests; only the opt-in marked, disposable PG18 instance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock
import threading
import pytest
from fastapi import HTTPException
from sqlalchemy import select,text,func,event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from backend.alembic.application_safety import apply_application,verify_application,HEAD
from backend.app.models.user import User
from backend.app.models.equipment import Equipment,EquipmentAsset,EquipmentStock
from backend.app.models.operations import StockLedger,QuantityLoan,LoanDestination,OverdueReminder
from backend.app.schemas.stock import StockAdjustment
from backend.app.schemas.operations import QuantityLoanCreate,QuantityReturnCreate,LoanPolicyInput
from backend.app.schemas.asset import AssetCreate
from backend.app.services.stock import adjust_stock
from backend.app.services import quantity_loans as loans
from backend.app.services.asset import create_asset_bulk
from backend.app.services.equipment import create_equipment_bulk
from backend.app.schemas.equipment import EquipmentCreate
from backend.app.services.checkout import create_checkout
from backend.app.services.maintenance import report_maintenance_issue,add_maintenance_note
from backend.app.services.reminders import process_reminders
from infra.nfc_postgres.harness import ADMIN


@pytest.fixture
def application(database):
    database.deploy()
    with database.engine(ADMIN).connect() as c:
        database.verify_engine_identity(c);c.rollback();apply_application(c,database.settings)
    admin=database.engine(ADMIN)
    with admin.begin() as c:
        database.verify_engine_identity(c)
        c.exec_driver_sql("GRANT SELECT ON users,equipment,equipment_assets,equipment_checkouts,maintenance_requests,maintenance_events TO inventory_app")
        c.exec_driver_sql("GRANT INSERT,UPDATE ON equipment,equipment_assets,equipment_checkouts,maintenance_requests TO inventory_app")
        c.exec_driver_sql("GRANT INSERT ON maintenance_events TO inventory_app")
    staff=User(id=uuid4(),college_id="STAFF",full_name="Staff",email="staff@example.invalid",role="FACULTY",is_active=True)
    student=User(id=uuid4(),college_id="STUDENT",full_name="Student",email="student@example.invalid",role="STUDENT",is_active=True)
    other=User(id=uuid4(),college_id="OTHER",full_name="Other",email="other@example.invalid",role="STUDENT",is_active=True)
    quantity=Equipment(id=uuid4(),name="Cables",category="Network",tracking_type="QUANTITY",default_location="Lab")
    individual=Equipment(id=uuid4(),name="Scope",category="Network",tracking_type="INDIVIDUAL",default_location="Lab")
    asset=EquipmentAsset(id=uuid4(),equipment_id=individual.id,asset_code="SCOPE-1",location="Lab")
    with Session(admin,expire_on_commit=False) as db:
        db.add_all([staff,student,other,quantity,individual]);db.commit()
        db.add(asset);db.commit()
    app_engine=database.engine("inventory_app")
    with Session(app_engine) as db:
        adjust_stock(db,quantity.id,StockAdjustment(expected_quantity=0,change=20),staff)
    return SimpleNamespace(h=database,engine=app_engine,admin=admin,staff=staff,student=student,other=other,
                           quantity=quantity,individual=individual,asset=asset)


def issue_data(c,quantity=3,**values):
    return QuantityLoanCreate(idempotency_key=uuid4(),equipment_id=c.quantity.id,student_id=c.student.id,
                              quantity=quantity,due_at=datetime.now(timezone.utc)+timedelta(days=1),**values)


def test_application_upgrade_and_verify_only_rerun(application):
    c=application
    with c.admin.connect() as connection:
        apply_application(connection,c.h.settings)
    with c.admin.connect() as connection:
        verify_application(connection,c.h.settings)
        assert connection.scalar(text("SELECT version_num FROM alembic_version"))==HEAD


def test_stock_ledger_is_atomic_immutable_and_context_required(application):
    c=application;key=uuid4()
    data=StockAdjustment(expected_quantity=20,change=-3,idempotency_key=key,reason="Damaged cable removed")
    for _ in range(2):
        with Session(c.engine) as db:
            assert adjust_stock(db,c.quantity.id,data,c.staff).quantity==17
    with Session(c.engine) as db:
        rows=db.scalars(select(StockLedger).order_by(StockLedger.occurred_at)).all()
        assert len(rows)==2 and rows[-1].previous_quantity==20 and rows[-1].quantity==17
    for statement in ("UPDATE stock_ledger SET reason='changed'","DELETE FROM stock_ledger","TRUNCATE stock_ledger",
                      "ALTER TABLE stock_ledger DISABLE TRIGGER ALL","UPDATE equipment_stock SET quantity=0"):
        with pytest.raises(DBAPIError):
            with c.engine.begin() as connection:
                connection.exec_driver_sql(statement)


def test_quantity_issue_return_retry_history_and_student_scope(application):
    c=application;data=issue_data(c)
    for _ in range(2):
        with Session(c.engine) as db:
            issued=loans.issue_quantity(db,data,c.staff)
            assert issued.quantity==3
    with Session(c.engine) as db:
        assert len(loans.list_quantity(db,c.student)["items"])==1
    with Session(c.engine) as db:
        assert not loans.list_quantity(db,c.other)["items"]
        with pytest.raises(HTTPException) as error:
            loans.list_quantity(db,c.other,loan_id=data.idempotency_key)
        assert error.value.status_code==404
    returned=QuantityReturnCreate(idempotency_key=uuid4(),condition_status="WORKING")
    for _ in range(2):
        with Session(c.engine) as db:
            assert loans.return_quantity(db,issued.id,returned,c.staff).returned_to==c.staff.id
    with Session(c.engine) as db:
        assert db.get(EquipmentStock,c.quantity.id).quantity==20
        assert db.scalar(select(func.count()).select_from(StockLedger))==3
    with Session(c.engine) as db:
        with pytest.raises(HTTPException) as error:
            loans.return_quantity(db,issued.id,QuantityReturnCreate(idempotency_key=uuid4(),condition_status="WORKING"),c.staff)
        assert error.value.status_code==409


def test_insufficient_quantity_rolls_back_custody_and_history(application):
    c=application;data=issue_data(c,21)
    with Session(c.engine) as db:
        with pytest.raises(HTTPException) as error:
            loans.issue_quantity(db,data,c.staff)
        assert error.value.status_code==409
    with Session(c.engine) as db:
        assert db.get(QuantityLoan,data.idempotency_key) is None
        assert db.get(EquipmentStock,c.quantity.id).quantity==20
        assert db.scalar(select(func.count()).select_from(StockLedger))==1


def test_stock_failure_rolls_back_loan_and_ledger(application):
    c=application;data=issue_data(c)
    def fail(connection,cursor,statement,parameters,context,many):
        if statement.startswith("UPDATE equipment_stock"):
            raise RuntimeError("Injected application write failure")
    event.listen(c.engine,"before_cursor_execute",fail)
    try:
        with Session(c.engine) as db:
            with pytest.raises(RuntimeError):
                loans.issue_quantity(db,data,c.staff)
    finally:
        event.remove(c.engine,"before_cursor_execute",fail)
    with Session(c.engine) as db:
        assert db.get(QuantityLoan,data.idempotency_key) is None
        assert db.get(EquipmentStock,c.quantity.id).quantity==20


@pytest.mark.parametrize("quantity_issue",[False,True])
def test_competing_changes_cannot_overdraw_stock(application,quantity_issue):
    c=application;barrier=threading.Barrier(2)
    def execute(_):
        barrier.wait(timeout=10)
        try:
            with Session(c.engine) as db:
                if quantity_issue:
                    loans.issue_quantity(db,issue_data(c,15),c.staff)
                else:
                    adjust_stock(db,c.quantity.id,StockAdjustment(expected_quantity=20,change=-15),c.staff)
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(execute,range(2)))==[200,409]
    with Session(c.engine) as db:
        assert db.get(EquipmentStock,c.quantity.id).quantity==5
        assert db.scalar(select(func.count()).select_from(StockLedger))==2


def test_bulk_registration_is_all_or_nothing(application):
    c=application
    items=[AssetCreate(equipment_id=c.individual.id,asset_code="BULK-1",location="Lab")]*2
    with Session(c.engine) as db:
        with pytest.raises(HTTPException):
            create_asset_bulk(db,items,c.staff)
    with Session(c.engine) as db:
        assert db.scalar(select(EquipmentAsset.id).where(EquipmentAsset.asset_code=="BULK-1")) is None
    items=[AssetCreate(equipment_id=c.individual.id,asset_code="BULK-"+str(i),location="Lab") for i in range(2)]
    with Session(c.engine) as db:
        assert len(create_asset_bulk(db,items,c.staff))==2


def test_offsite_policy_and_recorded_destination(application):
    c=application
    values=dict(asset_id=c.asset.id,student_id=c.student.id,issued_by=c.staff,due_at=datetime.now(timezone.utc)+timedelta(days=1),destination="Classroom")
    with Session(c.engine) as db:
        with pytest.raises(HTTPException) as error:
            create_checkout(db,**values)
        assert error.value.status_code==409
    with Session(c.engine) as db:
        loans.set_policy(db,c.individual.id,LoanPolicyInput(allow_offsite=True),c.staff)
    with Session(c.engine) as db:
        loan=create_checkout(db,**values)
    with Session(c.engine) as db:
        assert db.get(LoanDestination,loan.id).destination=="Classroom"


def test_maintenance_note_commits_immutable_event_and_hold(application):
    c=application
    with Session(c.engine) as db:
        request=report_maintenance_issue(db,c.asset.id,c.staff,"DAMAGE","Damaged connector")
    with Session(c.engine) as db:
        noted=add_maintenance_note(db,request.id,c.staff,request.version,"Replacement part ordered")
        assert noted.version==2 and noted.maintenance_hold and noted.status=="OPEN"
    with c.engine.connect() as connection:
        rows=connection.execute(text("SELECT event_type FROM maintenance_events ORDER BY sequence")).scalars().all()
        assert rows==["REPORTED","NOTE_ADDED"]


@pytest.mark.parametrize("ambiguous",[False,True])
def test_reminders_never_automatically_repeat_smtp(application,monkeypatch,ambiguous):
    c=application
    past=datetime.now(timezone.utc)-timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr(loans,"_now",lambda db:past)
        data=QuantityLoanCreate(idempotency_key=uuid4(),equipment_id=c.quantity.id,student_id=c.student.id,quantity=1,due_at=past+timedelta(days=1))
        with Session(c.engine) as db:
            loans.issue_quantity(db,data,c.staff)
    def send(config,delivery):
        with Session(c.engine) as db:
            assert db.get(OverdueReminder,delivery.id).state=="UNCERTAIN"
        if ambiguous:
            raise RuntimeError("Synthetic ambiguous SMTP acknowledgement")
    sender=Mock(side_effect=send)
    factory=lambda:Session(c.engine)
    counts=process_reminders(factory,object(),sender=sender)
    assert counts["uncertain" if ambiguous else "sent"]==1
    assert process_reminders(factory,object(),sender=sender)==dict(enqueued=0,sent=0,uncertain=0)
    assert sender.call_count==1


def test_altered_application_trigger_predicate_is_rejected(application):
    c=application
    with c.admin.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER stock_change_capture ON equipment_stock")
        connection.exec_driver_sql("CREATE TRIGGER stock_change_capture AFTER INSERT OR UPDATE ON equipment_stock FOR EACH ROW WHEN(false) EXECUTE FUNCTION stock_change_capture()")
    with c.admin.connect() as connection:
        with pytest.raises(RuntimeError):
            verify_application(connection,c.h.settings)


def test_terminated_application_connection_cannot_commit_custody(application):
    c=application;data=issue_data(c)
    def terminate(connection,cursor,statement,parameters,context,many):
        if statement.startswith("INSERT INTO quantity_loans"):
            c.h.terminate(connection.connection.driver_connection.info.backend_pid)
    event.listen(c.engine,"after_cursor_execute",terminate)
    try:
        with Session(c.engine) as db:
            with pytest.raises(HTTPException) as error:
                loans.issue_quantity(db,data,c.staff)
            assert error.value.status_code==503
    finally:
        event.remove(c.engine,"after_cursor_execute",terminate)
    with Session(c.engine) as db:
        assert db.get(QuantityLoan,data.idempotency_key) is None
        assert db.get(EquipmentStock,c.quantity.id).quantity==20


def test_equipment_bulk_registration_reuses_registration_service(application):
    c=application
    data=[EquipmentCreate(name="Bulk-"+str(i),category="Lab",tracking_type="INDIVIDUAL",default_location="Lab") for i in range(2)]
    with Session(c.engine) as db:
        records=create_equipment_bulk(db,data,c.staff)
        assert len(records)==2 and len({row.id for row in records})==2


def test_quantity_retry_survives_default_location_change(application):
    c=application;data=issue_data(c)
    with Session(c.engine) as db:
        first=loans.issue_quantity(db,data,c.staff)
    from backend.app.services.equipment import update_equipment
    from backend.app.schemas.equipment import EquipmentUpdate
    with Session(c.engine) as db:
        update_equipment(db,c.quantity.id,EquipmentUpdate(default_location="Other lab"),c.staff)
    with Session(c.engine) as db:
        replay=loans.issue_quantity(db,data,c.staff)
        assert replay.id==first.id and replay.destination=="Lab"
    with Session(c.engine) as db:
        assert db.get(EquipmentStock,c.quantity.id).quantity==17


def test_existing_stock_gets_a_locked_opening_ledger_balance(database):
    database.deploy()
    with database.engine(ADMIN).begin() as connection:
        database.verify_engine_identity(connection)
        identifier=uuid4()
        connection.execute(text("INSERT INTO equipment(id,name,category,tracking_type,default_location) VALUES(:id,'Legacy stock','Lab','QUANTITY','Lab')"),dict(id=identifier))
        connection.execute(text("INSERT INTO equipment_stock(equipment_id,quantity) VALUES(:id,12)"),dict(id=identifier))
    with database.engine(ADMIN).connect() as connection:
        apply_application(connection,database.settings)
    with database.engine("inventory_app").connect() as connection:
        row=connection.execute(text("SELECT kind,previous_quantity,change,quantity,revision FROM stock_ledger WHERE equipment_id=:id"),dict(id=identifier)).one()
        assert tuple(row)==('OPENING',0,12,12,1)


def test_concurrent_reminder_workers_claim_only_one_attempt(application,monkeypatch):
    c=application;past=datetime.now(timezone.utc)-timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr(loans,"_now",lambda db:past)
        data=QuantityLoanCreate(idempotency_key=uuid4(),equipment_id=c.quantity.id,student_id=c.student.id,quantity=1,due_at=past+timedelta(days=1))
        with Session(c.engine) as db:
            loans.issue_quantity(db,data,c.staff)
    barrier=threading.Barrier(2);sender=Mock()
    def execute(_):
        barrier.wait(timeout=10)
        return process_reminders(lambda:Session(c.engine),object(),sender=sender)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(execute,range(2)))
    assert sum(result["sent"] for result in results)==1 and sender.call_count==1
