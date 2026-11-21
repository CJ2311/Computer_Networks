"""Isolated, bounded RFID scan staging; no inventory or borrower writes."""
import re

from alembic import op
from sqlalchemy import text

revision = "c3a7e9b120df"
down_revision = "b2d4e6f810ac"
branch_labels = None
depends_on = None
OWNER = "inventory_metadata_audit_owner"
SIGNATURES = {"scanner_ingest": "(uuid,text,uuid,uuid,bigint,text)",
              "scanner_read_events": "(uuid,uuid,text,bigint,integer)"}
ARGUMENTS = {"scanner_ingest": "p_scanner uuid,p_digest text,p_session uuid,p_scan uuid,p_sequence bigint,p_uid text",
             "scanner_read_events": "p_staff uuid,p_session uuid,p_digest text,p_after bigint,p_limit integer"}


def routine_bodies(q):
    return {
        "scanner_ingest": f"""
DECLARE s record; hit record; recent record; at_time timestamptz; assets uuid[]; paired boolean;
BEGIN
    SELECT * INTO s FROM {q}.equipment_scanners WHERE id=p_scanner FOR UPDATE;
    IF NOT FOUND OR NOT s.enabled OR s.secret_sha256 IS DISTINCT FROM p_digest THEN
        RETURN jsonb_build_object('status',401,'error','invalid_scanner');
    END IF;
    at_time := clock_timestamp();
    p_uid := upper(p_uid COLLATE "C");
    IF p_session IS NULL OR p_scan IS NULL OR p_sequence IS NULL OR p_sequence<1 OR p_uid IS NULL
       OR char_length(p_uid) NOT IN (8,14,20)
       OR p_uid !~ '^([0-9A-F]{{8}}|[0-9A-F]{{14}}|[0-9A-F]{{20}})$' THEN
        RETURN jsonb_build_object('status',422,'error','invalid_input');
    END IF;
    DELETE FROM {q}.scanner_scan_events WHERE expires_at<=at_time;
    IF at_time>=s.window_started_at+interval '60 seconds' OR at_time<s.window_started_at THEN
        s.window_started_at := at_time; s.request_count := 0;
    END IF;
    s.request_count := least(s.request_count+1,61);
    UPDATE {q}.equipment_scanners SET window_started_at=s.window_started_at,request_count=s.request_count WHERE id=p_scanner;
    IF s.request_count>60 THEN
        RETURN jsonb_build_object('status',429,'error','rate_limited');
    END IF;
    paired := coalesce(s.capture_session_id=p_session AND s.capture_started_at<=at_time
        AND at_time<s.capture_expires_at AND EXISTS(SELECT 1 FROM {q}.users u
            WHERE u.id=s.capture_user_id AND u.is_active AND u.role IN ('FACULTY','LAB_ASSISTANT')),false);
    SELECT * INTO hit FROM {q}.scanner_scan_events WHERE scanner_id=p_scanner AND scan_id=p_scan;
    IF FOUND THEN
        IF NOT paired OR hit.session_id<>s.capture_session_id OR hit.staff_user_id<>s.capture_user_id
           OR hit.sequence<>p_sequence OR hit.uid<>p_uid THEN
            RETURN jsonb_build_object('status',409,'error','scan_conflict');
        END IF;
        RETURN jsonb_build_object('status',200,'data',jsonb_build_object('scan_id',hit.scan_id,
            'sequence',hit.sequence,'uid',hit.uid,'asset_id',hit.asset_id,'received_at',hit.received_at,
            'expires_at',hit.expires_at,'duplicate',true));
    END IF;
    IF p_sequence<=s.last_sequence THEN
        RETURN jsonb_build_object('status',409,'error','scan_conflict');
    END IF;
    -- Consume new sequences even for unknown/unpaired/conflicting scans. Neither
    -- expiration nor a later pairing/registration can make old payloads fresh.
    UPDATE {q}.equipment_scanners SET last_sequence=p_sequence WHERE id=p_scanner;
    IF NOT paired THEN
        RETURN jsonb_build_object('status',409,'error','capture_unavailable');
    END IF;
    SELECT * INTO recent FROM {q}.scanner_scan_events WHERE scanner_id=p_scanner
        AND session_id=s.capture_session_id AND staff_user_id=s.capture_user_id ORDER BY sequence DESC LIMIT 1;
    IF FOUND AND recent.uid=p_uid AND recent.received_at>at_time-interval '2 seconds' THEN
        RETURN jsonb_build_object('status',409,'error','duplicate_uid');
    END IF;
    assets := ARRAY(SELECT a.id FROM {q}.equipment_assets a
        WHERE upper(a.nfc_tag_id COLLATE "C")=p_uid LIMIT 2);
    IF cardinality(assets)=0 THEN
        RETURN jsonb_build_object('status',404,'error','unknown_uid');
    ELSIF cardinality(assets)>1 THEN
        RETURN jsonb_build_object('status',409,'error','ambiguous_uid');
    END IF;
    INSERT INTO {q}.scanner_scan_events(scanner_id,scan_id,sequence,session_id,staff_user_id,asset_id,uid,received_at,expires_at)
        VALUES(p_scanner,p_scan,p_sequence,s.capture_session_id,s.capture_user_id,assets[1],p_uid,at_time,at_time+interval '120 seconds');
    DELETE FROM {q}.scanner_scan_events WHERE scanner_id=p_scanner AND sequence NOT IN
        (SELECT sequence FROM {q}.scanner_scan_events WHERE scanner_id=p_scanner ORDER BY sequence DESC LIMIT 100);
    RETURN jsonb_build_object('status',200,'data',jsonb_build_object('scan_id',p_scan,'sequence',p_sequence,
        'uid',p_uid,'asset_id',assets[1],'received_at',at_time,'expires_at',at_time+interval '120 seconds','duplicate',false));
END;
""",
        "scanner_read_events": f"""
DECLARE s record; at_time timestamptz; result jsonb;
BEGIN
    SELECT * INTO s FROM {q}.equipment_scanners WHERE capture_session_id=p_session FOR UPDATE;
    at_time := clock_timestamp();
    IF NOT FOUND OR NOT s.enabled OR s.capture_user_id IS DISTINCT FROM p_staff
       OR s.capture_secret_sha256 IS DISTINCT FROM p_digest
       OR NOT (s.capture_started_at<=at_time AND at_time<s.capture_expires_at)
       OR NOT EXISTS(SELECT 1 FROM {q}.users u WHERE u.id=p_staff AND u.is_active
                     AND u.role IN ('FACULTY','LAB_ASSISTANT')) THEN
        RETURN jsonb_build_object('status',404,'error','capture_not_found');
    END IF;
    IF p_after IS NULL OR p_after<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 50 THEN
        RETURN jsonb_build_object('status',422,'error','invalid_input');
    END IF;
    DELETE FROM {q}.scanner_scan_events WHERE expires_at<=at_time;
    SELECT jsonb_build_object('items',coalesce(jsonb_agg(jsonb_build_object('scan_id',x.scan_id,
        'sequence',x.sequence,'uid',x.uid,'asset_id',x.asset_id,'received_at',x.received_at,'expires_at',x.expires_at)
        ORDER BY x.sequence) FILTER(WHERE x.n<=p_limit),'[]'::jsonb),
        'has_more',count(*)>p_limit,'after_sequence',p_after,'limit',p_limit) INTO result
    FROM (SELECT e.*,row_number() OVER(ORDER BY e.sequence) AS n FROM {q}.scanner_scan_events e
        WHERE e.scanner_id=s.id AND e.session_id=p_session AND e.staff_user_id=p_staff
        AND e.expires_at>at_time AND e.sequence>p_after ORDER BY e.sequence LIMIT p_limit+1) x;
    RETURN jsonb_build_object('status',200,'data',result);
END;
""",
    }


def upgrade():
    c = op.get_bind()
    if c.dialect.name != "postgresql" or c.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")) is not True:
        raise RuntimeError("Scanner migration requires a trusted deployment administrator")
    schema = c.scalar(text("SELECT current_schema()"))
    if not schema or not re.fullmatch(r"[a-z_][a-z_0-9]*", schema) or schema.startswith("pg_"):
        raise RuntimeError("Trusted application schema required")
    q = c.dialect.identifier_preparer.quote_schema(schema)
    if c.scalar(text("""SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
        OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid OR roleid=r.oid)
        FROM pg_roles r WHERE rolname=:owner"""), {"owner": OWNER}) is not False:
        raise RuntimeError("Scanner routine owner must remain an isolated NOLOGIN role")
    op.execute(f"""CREATE TABLE {q}.equipment_scanners (
        id uuid PRIMARY KEY, secret_sha256 varchar(64) NOT NULL UNIQUE,
        enabled boolean NOT NULL DEFAULT true, last_sequence bigint NOT NULL DEFAULT 0,
        window_started_at timestamptz NOT NULL DEFAULT now(), request_count integer NOT NULL DEFAULT 0,
        capture_session_id uuid UNIQUE, capture_user_id uuid REFERENCES {q}.users(id),
        capture_secret_sha256 varchar(64) UNIQUE, capture_started_at timestamptz, capture_expires_at timestamptz,
        CONSTRAINT ck_scanner_digest CHECK(secret_sha256 ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT ck_scanner_counters CHECK(last_sequence>=0 AND request_count BETWEEN 0 AND 61),
        CONSTRAINT ck_scanner_capture CHECK(
            (capture_session_id IS NULL AND capture_user_id IS NULL AND capture_secret_sha256 IS NULL
             AND capture_started_at IS NULL AND capture_expires_at IS NULL) OR
            (capture_session_id IS NOT NULL AND capture_user_id IS NOT NULL AND capture_secret_sha256 IS NOT NULL
             AND capture_secret_sha256 ~ '^[0-9a-f]{{64}}$' AND capture_started_at IS NOT NULL AND capture_expires_at IS NOT NULL
             AND capture_secret_sha256 <> secret_sha256
             AND capture_expires_at>capture_started_at AND capture_expires_at<=capture_started_at+interval '15 minutes')))""")
    op.execute(f"""CREATE TABLE {q}.scanner_scan_events (
        scanner_id uuid NOT NULL REFERENCES {q}.equipment_scanners(id),scan_id uuid NOT NULL,
        sequence bigint NOT NULL, session_id uuid NOT NULL,staff_user_id uuid NOT NULL REFERENCES {q}.users(id),
        asset_id uuid NOT NULL REFERENCES {q}.equipment_assets(id),uid varchar(20) NOT NULL,
        received_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,
        PRIMARY KEY(scanner_id,scan_id),CONSTRAINT uq_scanner_event_sequence UNIQUE(scanner_id,sequence),
        CONSTRAINT ck_scanner_event_sequence CHECK(sequence>0),
        CONSTRAINT ck_scanner_event_uid CHECK(uid ~ '^([0-9A-F]{{8}}|[0-9A-F]{{14}}|[0-9A-F]{{20}})$'),
        CONSTRAINT ck_scanner_event_expiry CHECK(expires_at=received_at+interval '120 seconds'))""")
    op.execute(f"CREATE INDEX ix_scanner_events_expiry ON {q}.scanner_scan_events(expires_at)")
    for table in ("equipment_scanners", "scanner_scan_events"):
        op.execute(f"REVOKE ALL ON {q}.{table} FROM PUBLIC,inventory_app,inventory_migrator,inventory_metadata_audit_writer")
        op.execute(f"ALTER TABLE {q}.{table} OWNER TO {OWNER}")
    op.execute(f"GRANT SELECT(id,nfc_tag_id) ON {q}.equipment_assets TO {OWNER}")
    for name, body in routine_bodies(q).items():
        qualified = f"{q}.{name}{SIGNATURES[name]}"
        op.execute(f"""CREATE FUNCTION {q}.{name}({ARGUMENTS[name]}) RETURNS jsonb LANGUAGE plpgsql
            SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $scanner${body}$scanner$""")
        op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC,inventory_app,inventory_migrator,inventory_metadata_audit_writer")
        op.execute(f"ALTER FUNCTION {qualified} OWNER TO {OWNER}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO inventory_app")
    from backend.alembic.scanner_safety import verify_scanner_staging
    verify_scanner_staging(c, q)


def downgrade():
    raise RuntimeError("Scanner staging requires a separately reviewed forward migration")
