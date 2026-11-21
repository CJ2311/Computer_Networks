"""Private student NFC registration; additive, no source record changes."""
import re
from alembic import op
from sqlalchemy import text

revision = "e4b7c9d210af"
down_revision = "d6f2b8a931c0"
branch_labels = None
depends_on = None
OWNER = "inventory_metadata_audit_owner"
SIGNATURE = "(text,uuid,uuid,jsonb)"


def routine_body(q):
    return f"""
DECLARE r record; at_time timestamptz := clock_timestamp(); v_student uuid;
        v_uid text; v_scanner uuid; v_request uuid; v_nonce uuid; v_digest text; v_profile text;
BEGIN
    IF p_action NOT IN ('init','status','lookup','verify') OR p_action IS NULL THEN
        RETURN jsonb_build_object('status',422);
    END IF;
    IF p_action <> 'verify' AND NOT EXISTS(SELECT 1 FROM {q}.users
        WHERE id=p_actor AND is_active AND role IN ('FACULTY','LAB_ASSISTANT')) THEN
        RETURN jsonb_build_object('status',403);
    END IF;
    IF p_action='init' THEN
        IF NOT pg_try_advisory_xact_lock(471161236) THEN
            RETURN jsonb_build_object('status',409);
        END IF;
        v_student := (p_data->>'student_id')::uuid;
        v_uid := p_data->>'card_uid'; v_scanner := (p_data->>'scanner_id')::uuid;
        v_request := (p_data->>'request_id')::uuid; v_nonce := (p_data->>'nonce')::uuid;
        v_digest := p_data->>'digest'; v_profile := p_data->>'profile';
        IF v_student IS NULL OR v_scanner IS NULL OR v_request IS NULL OR v_nonce IS NULL OR p_id IS NULL
           OR v_uid IS NULL OR v_uid !~ '^([0-9A-F]{{8}}|[0-9A-F]{{14}}|[0-9A-F]{{20}})$'
           OR v_digest IS NULL OR v_digest !~ '^[0-9a-f]{{64}}$'
           OR v_profile IS NULL OR v_profile !~ '^[A-Za-z0-9._-]{{1,64}}$'
           OR NOT EXISTS(SELECT 1 FROM {q}.users WHERE id=v_student AND role='STUDENT' AND is_active) THEN
            RETURN jsonb_build_object('status',422);
        END IF;
        IF EXISTS(SELECT 1 FROM {q}.student_nfc_cards
                  WHERE (card_uid=v_uid AND student_id<>v_student)
                     OR (student_id=v_student AND card_uid<>v_uid)
                     OR (request_id=v_request AND (student_id<>v_student OR card_uid<>v_uid))) THEN
            RETURN jsonb_build_object('status',409);
        END IF;
        SELECT * INTO r FROM {q}.student_nfc_cards WHERE student_id=v_student FOR UPDATE;
        IF FOUND THEN
            IF r.status='ACTIVE' THEN
                RETURN jsonb_build_object('status',200,'data',to_jsonb(r)-'payload_sha256');
            END IF;
            IF r.status='PENDING_WRITE' AND r.expires_at>at_time THEN
                IF r.request_id<>v_request OR r.scanner_id<>v_scanner OR r.initiated_by<>p_actor
                   OR r.write_profile<>v_profile THEN
                    RETURN jsonb_build_object('status',409);
                END IF;
                RETURN jsonb_build_object('status',200,'data',to_jsonb(r)-'payload_sha256');
            END IF;
            UPDATE {q}.student_nfc_cards SET status='PENDING_WRITE',verified_at=NULL,
                request_id=v_request,attempt_nonce=v_nonce,scanner_id=v_scanner,initiated_by=p_actor,
                payload_sha256=v_digest,write_profile=v_profile,attempt_started_at=at_time,
                expires_at=at_time+interval '5 minutes' WHERE id=r.id RETURNING * INTO r;
        ELSE
            INSERT INTO {q}.student_nfc_cards(id,student_id,card_uid,status,created_at,verified_at,
                request_id,attempt_nonce,scanner_id,initiated_by,payload_sha256,write_profile,attempt_started_at,expires_at)
                VALUES(p_id,v_student,v_uid,'PENDING_WRITE',at_time,NULL,v_request,v_nonce,v_scanner,p_actor,
                       v_digest,v_profile,at_time,at_time+interval '5 minutes') RETURNING * INTO r;
        END IF;
        RETURN jsonb_build_object('status',200,'data',to_jsonb(r)-'payload_sha256');
    END IF;
    IF p_action='lookup' THEN
        SELECT * INTO r FROM {q}.student_nfc_cards
            WHERE card_uid=p_data->>'card_uid' AND status='ACTIVE' FOR UPDATE;
    ELSE
        SELECT * INTO r FROM {q}.student_nfc_cards WHERE id=p_id FOR UPDATE;
    END IF;
    IF NOT FOUND THEN RETURN jsonb_build_object('status',404); END IF;
    IF p_action='verify' AND (r.scanner_id IS DISTINCT FROM (p_data->>'scanner_id')::uuid) THEN
        RETURN jsonb_build_object('status',404);
    END IF;
    IF r.status='PENDING_WRITE' AND r.expires_at<=at_time THEN
        UPDATE {q}.student_nfc_cards SET status='FAILED' WHERE id=r.id RETURNING * INTO r;
    END IF;
    IF p_action='verify' THEN
        IF r.status<>'PENDING_WRITE' THEN RETURN jsonb_build_object('status',409); END IF;
        IF r.card_uid IS DISTINCT FROM p_data->>'card_uid'
           OR r.payload_sha256 IS DISTINCT FROM p_data->>'digest'
           OR r.write_profile IS DISTINCT FROM p_data->>'profile' THEN
            RETURN jsonb_build_object('status',409);
        END IF;
        UPDATE {q}.student_nfc_cards SET status='ACTIVE',verified_at=at_time WHERE id=r.id RETURNING * INTO r;
    END IF;
    RETURN jsonb_build_object('status',200,'data',to_jsonb(r)-'payload_sha256');
END;
"""


def upgrade():
    c = op.get_bind()
    if c.dialect.name != 'postgresql' or c.scalar(text('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')) is not True:
        raise RuntimeError('Student NFC migration requires a trusted deployment administrator')
    schema = c.scalar(text('SELECT current_schema()'))
    if not schema or not re.fullmatch('[a-z_][a-z_0-9]*', schema) or schema.startswith('pg_'):
        raise RuntimeError('Trusted application schema required')
    q = c.dialect.identifier_preparer.quote_schema(schema)
    if c.scalar(text('''SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
        OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid OR roleid=r.oid)
        FROM pg_roles r WHERE rolname=:owner'''), {'owner': OWNER}) is not False:
        raise RuntimeError('Student NFC owner must remain an isolated NOLOGIN role')
    op.execute(f'''CREATE TABLE {q}.student_nfc_cards (
        id uuid PRIMARY KEY, student_id uuid NOT NULL REFERENCES {q}.users(id),
        card_uid varchar(20) NOT NULL, status varchar(20) NOT NULL DEFAULT 'PENDING_WRITE',
        created_at timestamptz NOT NULL DEFAULT now(), verified_at timestamptz,
        request_id uuid NOT NULL UNIQUE, attempt_nonce uuid NOT NULL UNIQUE,
        scanner_id uuid NOT NULL, initiated_by uuid NOT NULL REFERENCES {q}.users(id),
        payload_sha256 varchar(64) NOT NULL, write_profile varchar(64) NOT NULL,
        attempt_started_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
        CONSTRAINT uq_student_nfc_student UNIQUE(student_id),
        CONSTRAINT uq_student_nfc_uid UNIQUE(card_uid),
        CONSTRAINT ck_student_nfc_uid CHECK(card_uid ~ '^([0-9A-F]{{8}}|[0-9A-F]{{14}}|[0-9A-F]{{20}})$'),
        CONSTRAINT ck_student_nfc_digest CHECK(payload_sha256 ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT ck_student_nfc_expiry CHECK(attempt_started_at>=created_at AND expires_at>attempt_started_at
            AND expires_at<=attempt_started_at+interval '5 minutes'),
        CONSTRAINT ck_student_nfc_status CHECK(status IN ('PENDING_WRITE','ACTIVE','FAILED')),
        CONSTRAINT ck_student_nfc_verification CHECK((status='ACTIVE' AND verified_at IS NOT NULL)
            OR (status<>'ACTIVE' AND verified_at IS NULL)))''')
    op.execute(f'REVOKE ALL ON {q}.student_nfc_cards FROM PUBLIC,inventory_app,inventory_migrator,inventory_metadata_audit_writer')
    op.execute(f'ALTER TABLE {q}.student_nfc_cards OWNER TO {OWNER}')
    signature = f'{q}.student_nfc_manage{SIGNATURE}'
    op.execute(f'''CREATE FUNCTION {q}.student_nfc_manage(p_action text,p_actor uuid,p_id uuid,p_data jsonb)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $studentnfc${routine_body(q)}$studentnfc$''')
    op.execute(f'REVOKE ALL ON FUNCTION {signature} FROM PUBLIC,inventory_app,inventory_migrator,inventory_metadata_audit_writer')
    op.execute(f'ALTER FUNCTION {signature} OWNER TO {OWNER}')
    op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO inventory_app')


def downgrade():
    raise RuntimeError('Student NFC associations require a separately reviewed forward migration')
