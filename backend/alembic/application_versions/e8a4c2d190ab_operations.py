"""Immutable stock ledger, quantity custody, identity mappings and reminders.

Requires an already trusted deployment administrator; never modifies role memberships.
"""
import re
from alembic import op
from sqlalchemy import text

revision = "e8a4c2d190ab"
down_revision = "d6f2b8a931c0"
branch_labels = None
depends_on = None


def upgrade():
    c = op.get_bind()
    if c.dialect.name != "postgresql" or not c.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")):
        raise RuntimeError("Application ownership migration requires a trusted deployment administrator")
    name = c.scalar(text("SELECT current_schema()"))
    if not name or not re.fullmatch(r"[a-z_][a-z_0-9]*", name) or name.startswith("pg_"):
        raise RuntimeError("Trusted application schema required")
    q = c.dialect.identifier_preparer.quote_schema(name)
    owner = "inventory_metadata_audit_owner"
    for role in (owner, "inventory_metadata_audit_writer"):
        if c.scalar(text("""SELECT rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
            OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=r.oid)
            FROM pg_roles r WHERE rolname=:role"""), {"role":role}) is not False:
            raise RuntimeError("Protected roles must remain isolated NOLOGIN roles")
    if c.scalar(text("SELECT has_schema_privilege('inventory_app', :schema, 'CREATE')"), {"schema": name}):
        raise RuntimeError("Runtime schema CREATE is forbidden")
    # Freeze the opening balance before installing capture; concurrent old clients
    # must not change stock between the snapshot and ownership separation.
    op.execute(f"LOCK TABLE {q}.equipment_stock IN ACCESS EXCLUSIVE MODE")
    if c.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {q}.equipment_stock s JOIN {q}.equipment e ON e.id=s.equipment_id WHERE e.tracking_type<>'QUANTITY')")):
        raise RuntimeError("Existing quantity stock has an inconsistent tracking type")
    statements = [
        """CREATE TABLE {q}.external_identities (
            id uuid PRIMARY KEY, issuer varchar(500) NOT NULL, subject varchar(255) NOT NULL,
            user_id uuid NOT NULL REFERENCES {q}.users(id), CONSTRAINT uq_external_identity_subject UNIQUE(issuer,subject))""",
        """CREATE TABLE {q}.equipment_loan_policies (equipment_id uuid PRIMARY KEY REFERENCES {q}.equipment(id), allow_offsite boolean NOT NULL)""",
        """CREATE TABLE {q}.loan_destinations (id uuid PRIMARY KEY REFERENCES {q}.equipment_checkouts(id), destination varchar(150) NOT NULL)""",
        """CREATE TABLE {q}.quantity_loans (
            id uuid PRIMARY KEY, equipment_id uuid NOT NULL REFERENCES {q}.equipment(id), student_id uuid NOT NULL REFERENCES {q}.users(id),
            issued_by uuid NOT NULL REFERENCES {q}.users(id), quantity integer NOT NULL, request_fingerprint varchar(64) NOT NULL, equipment_name varchar(150) NOT NULL,
            destination varchar(150) NOT NULL, due_at timestamptz NOT NULL, checked_out_at timestamptz NOT NULL,
            returned_at timestamptz, returned_to uuid REFERENCES {q}.users(id), return_key uuid UNIQUE,
            CONSTRAINT ck_quantity_loan_positive CHECK(quantity>0), CONSTRAINT ck_quantity_loan_due CHECK(due_at>checked_out_at),
            CONSTRAINT ck_quantity_loan_return CHECK((returned_at IS NULL AND returned_to IS NULL AND return_key IS NULL) OR
                (returned_at IS NOT NULL AND returned_at >= checked_out_at AND returned_to IS NOT NULL AND return_key IS NOT NULL)))""",
        """CREATE TABLE {q}.stock_ledger (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(), operation_key uuid NOT NULL UNIQUE, revision bigint NOT NULL,
            equipment_id uuid NOT NULL REFERENCES {q}.equipment(id), actor_id uuid REFERENCES {q}.users(id),
            loan_id uuid REFERENCES {q}.quantity_loans(id), kind varchar(20) NOT NULL, reason text NOT NULL,
            previous_quantity integer NOT NULL, change integer NOT NULL, quantity integer NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_stock_ledger_revision UNIQUE(equipment_id,revision), CONSTRAINT ck_stock_ledger_revision CHECK(revision>0),
            CONSTRAINT ck_stock_ledger_balance CHECK(quantity>=0 AND previous_quantity>=0 AND quantity::bigint=previous_quantity::bigint+change::bigint),
            CONSTRAINT ck_stock_ledger_kind CHECK(kind IN ('OPENING','ADJUSTMENT','ISSUE','RETURN')))""",
        """CREATE TABLE {q}.overdue_reminders (
            id uuid PRIMARY KEY, checkout_id uuid UNIQUE REFERENCES {q}.equipment_checkouts(id),
            quantity_loan_id uuid UNIQUE REFERENCES {q}.quantity_loans(id), state varchar(20) NOT NULL DEFAULT 'PENDING',
            attempts integer NOT NULL DEFAULT 0, last_attempt_at timestamptz, sent_at timestamptz,
            CONSTRAINT ck_reminder_one_loan CHECK((checkout_id IS NULL)<>(quantity_loan_id IS NULL)),
            CONSTRAINT ck_reminder_state CHECK(state IN ('PENDING','SENT','UNCERTAIN','CANCELLED')))""",
        """INSERT INTO {q}.stock_ledger(operation_key,revision,equipment_id,kind,reason,previous_quantity,change,quantity)
            SELECT gen_random_uuid(),1,equipment_id,'OPENING','Migration opening balance',0,quantity,quantity FROM {q}.equipment_stock""",
    ]
    for statement in statements:
        op.execute(statement.format(q=q))
    op.execute(f"""
CREATE FUNCTION {q}.stock_history_protect() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,pg_temp AS $$ BEGIN
    IF TG_OP='INSERT' AND current_user='{owner}' AND pg_trigger_depth()>=2 THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'Stock history is immutable' USING ERRCODE='42501';
END $$;
CREATE TRIGGER stock_history_immutable BEFORE INSERT OR UPDATE OR DELETE ON {q}.stock_ledger
FOR EACH ROW EXECUTE FUNCTION {q}.stock_history_protect();
CREATE TRIGGER stock_history_no_truncate BEFORE TRUNCATE ON {q}.stock_ledger
FOR EACH STATEMENT EXECUTE FUNCTION {q}.stock_history_protect();
CREATE FUNCTION {q}.stock_change_capture() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $$
DECLARE ctx jsonb; actor uuid; key uuid; loan uuid; old_quantity integer; delta integer;
BEGIN
    IF TG_RELID <> '{q}.equipment_stock'::regclass OR TG_WHEN<>'AFTER' OR TG_LEVEL<>'ROW'
       OR TG_OP NOT IN ('INSERT','UPDATE') THEN RAISE EXCEPTION 'Invalid stock source'; END IF;
    IF TG_OP='UPDATE' AND OLD.equipment_id<>NEW.equipment_id THEN RAISE EXCEPTION 'Stock identity is immutable'; END IF;
    old_quantity := CASE WHEN TG_OP='INSERT' THEN 0 ELSE OLD.quantity END;
    delta := NEW.quantity - old_quantity;
    IF TG_OP='UPDATE' AND delta=0 THEN RETURN NULL; END IF;
    ctx := nullif(current_setting('inventory.stock_context',true),'')::jsonb;
    actor := (ctx->>'actor_id')::uuid; key := (ctx->>'operation_key')::uuid; loan := (ctx->>'loan_id')::uuid;
    IF key IS NULL OR ctx->>'kind' NOT IN ('ADJUSTMENT','ISSUE','RETURN') OR ctx->>'kind' IS NULL
       OR ctx->>'reason' IS NULL OR length(btrim(ctx->>'reason'))=0 OR length(ctx->>'reason')>4000
       OR NOT EXISTS(SELECT 1 FROM {q}.users WHERE id=actor AND is_active AND role IN ('ADMIN','FACULTY','LAB_ASSISTANT'))
    THEN RAISE EXCEPTION 'Invalid stock operation context' USING ERRCODE='42501'; END IF;
    IF (ctx->>'kind'='ADJUSTMENT' AND loan IS NOT NULL)
       OR (ctx->>'kind'='ISSUE' AND NOT EXISTS(SELECT 1 FROM {q}.quantity_loans WHERE id=loan AND equipment_id=NEW.equipment_id AND quantity=-delta AND issued_by=actor AND id=key))
       OR (ctx->>'kind'='RETURN' AND NOT EXISTS(SELECT 1 FROM {q}.quantity_loans WHERE id=loan AND equipment_id=NEW.equipment_id AND quantity=delta AND returned_to=actor AND return_key=key))
    THEN RAISE EXCEPTION 'Stock operation does not match custody' USING ERRCODE='23514'; END IF;
    INSERT INTO {q}.stock_ledger(operation_key,revision,equipment_id,actor_id,loan_id,kind,reason,previous_quantity,change,quantity)
    VALUES(key,(SELECT coalesce(max(revision),0)+1 FROM {q}.stock_ledger WHERE equipment_id=NEW.equipment_id),NEW.equipment_id,actor,loan,ctx->>'kind',ctx->>'reason',old_quantity,delta,NEW.quantity);
    RETURN NULL;
END $$;
CREATE TRIGGER stock_change_capture AFTER INSERT OR UPDATE ON {q}.equipment_stock
FOR EACH ROW EXECUTE FUNCTION {q}.stock_change_capture();
CREATE FUNCTION {q}.operations_no_delete() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,pg_temp AS $$ BEGIN RAISE EXCEPTION 'Custody records cannot be removed' USING ERRCODE='42501'; END $$;
CREATE TRIGGER stock_no_delete BEFORE DELETE ON {q}.equipment_stock FOR EACH ROW EXECUTE FUNCTION {q}.operations_no_delete();
CREATE TRIGGER stock_no_truncate BEFORE TRUNCATE ON {q}.equipment_stock FOR EACH STATEMENT EXECUTE FUNCTION {q}.operations_no_delete();
CREATE FUNCTION {q}.quantity_loan_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,pg_temp AS $$ BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.returned_at IS NOT NULL OR NEW.returned_to IS NOT NULL OR NEW.return_key IS NOT NULL
           OR NOT EXISTS(SELECT 1 FROM {q}.users WHERE id=NEW.student_id AND is_active AND role='STUDENT')
           OR NOT EXISTS(SELECT 1 FROM {q}.users WHERE id=NEW.issued_by AND is_active AND role IN ('ADMIN','FACULTY','LAB_ASSISTANT'))
        THEN RAISE EXCEPTION 'Invalid quantity borrower or issuer'; END IF;
    ELSE
        IF (to_jsonb(NEW)-ARRAY['returned_at','returned_to','return_key']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['returned_at','returned_to','return_key']) OR OLD.returned_at IS NOT NULL
           OR NEW.returned_at IS NULL OR NEW.returned_to IS NULL OR NEW.return_key IS NULL
           OR NOT EXISTS(SELECT 1 FROM {q}.users WHERE id=NEW.returned_to AND is_active AND role IN ('FACULTY','LAB_ASSISTANT'))
        THEN RAISE EXCEPTION 'Quantity custody is immutable except for a single authorized return'; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER quantity_loan_validate BEFORE INSERT OR UPDATE ON {q}.quantity_loans FOR EACH ROW EXECUTE FUNCTION {q}.quantity_loan_guard();
CREATE TRIGGER quantity_loan_no_delete BEFORE DELETE ON {q}.quantity_loans FOR EACH ROW EXECUTE FUNCTION {q}.operations_no_delete();
CREATE TRIGGER quantity_loan_no_truncate BEFORE TRUNCATE ON {q}.quantity_loans FOR EACH STATEMENT EXECUTE FUNCTION {q}.operations_no_delete();
CREATE FUNCTION {q}.quantity_ledger_consistency() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,pg_temp AS $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM {q}.stock_ledger WHERE loan_id=NEW.id AND kind='ISSUE' AND change=-NEW.quantity AND operation_key=NEW.id)
       OR (NEW.returned_at IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {q}.stock_ledger WHERE loan_id=NEW.id AND kind='RETURN' AND change=NEW.quantity AND operation_key=NEW.return_key))
    THEN RAISE EXCEPTION 'Quantity custody and stock ledger must commit together' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER quantity_ledger_consistent AFTER INSERT OR UPDATE ON {q}.quantity_loans
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {q}.quantity_ledger_consistency();
""")
    tables = ('external_identities','equipment_loan_policies','loan_destinations','quantity_loans','stock_ledger','overdue_reminders','equipment_stock')
    for table in tables:
        op.execute(f"REVOKE ALL ON {q}.{table} FROM PUBLIC, inventory_app, inventory_migrator, inventory_metadata_audit_writer")
        op.execute(f"ALTER TABLE {q}.{table} OWNER TO {owner}")
        op.execute(f"GRANT SELECT ON {q}.{table} TO inventory_app")
    for table in ('equipment_loan_policies','overdue_reminders','equipment_stock','quantity_loans'):
        op.execute(f"GRANT INSERT, UPDATE ON {q}.{table} TO inventory_app")
    op.execute(f"GRANT INSERT ON {q}.loan_destinations TO inventory_app")
    op.execute(f"GRANT SELECT(id,role,is_active) ON {q}.users TO {owner}")
    for function in ('stock_history_protect','stock_change_capture','operations_no_delete','quantity_loan_guard','quantity_ledger_consistency'):
        op.execute(f"REVOKE ALL ON FUNCTION {q}.{function}() FROM PUBLIC, inventory_app, inventory_migrator, inventory_metadata_audit_writer")
        op.execute(f"ALTER FUNCTION {q}.{function}() OWNER TO {owner}")
    op.execute(f"GRANT SELECT ON {q}.users, {q}.equipment, {q}.equipment_assets, {q}.equipment_checkouts, {q}.maintenance_requests, {q}.maintenance_events TO inventory_app")
    op.execute(f"GRANT INSERT, UPDATE ON {q}.equipment, {q}.equipment_assets, {q}.equipment_checkouts, {q}.maintenance_requests TO inventory_app")
    op.execute(f"GRANT INSERT ON {q}.maintenance_events TO inventory_app")
    # Unexpected default ACLs must never silently authorize history rewriting.
    for table in ('stock_ledger','external_identities'):
        if c.scalar(text(f"""SELECT EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
            WHERE c.oid='{q}.{table}'::regclass AND a.grantee<>c.relowner
            AND NOT(a.grantee='inventory_app'::regrole AND a.privilege_type='SELECT' AND NOT a.is_grantable))""")):
            raise RuntimeError("Unexpected protected application table privileges")


def downgrade():
    raise RuntimeError("Immutable inventory history requires a separately reviewed retention migration")
