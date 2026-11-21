"""Database-free source/protocol contracts, not ESP32 compilation or hardware tests."""
import base64
import json
from pathlib import Path
import re
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.auth.scanners import token_digest
from backend.app.schemas.scanners import ScanSubmit, ScanReceipt

ROOT = Path(__file__).resolve().parents[2]
SKETCH = ROOT / "arduino/sketch_nov19a/sketch_nov19a.ino"
SOURCE = SKETCH.read_text(encoding="utf-8-sig")
ID = "4b587bc0-978c-4c87-b9b2-d0ffda13e32d"
SESSION = "dc57d770-d39c-416a-a471-0c62b8958e44"


def function(name, next_name):
    return SOURCE.split(name, 1)[1].split(next_name, 1)[0]


def test_hardware_pins_preserved():
    expected = {"SS_PIN": 5, "RST_PIN": 4, "SCK_PIN": 18, "MISO_PIN": 19, "MOSI_PIN": 23,
                "GREEN_LED": 25, "RED_LED": 26, "BUZZER": 27}
    actual = {name: int(value) for name, value in re.findall(r"#define (\w+) (\d+)", SOURCE)}
    assert expected.items() <= actual.items()
    assert "Wire.begin(21, 22)" in SOURCE


@pytest.mark.parametrize("uid", ["04A1B2C3", "04A1B2C3D4E5F6", "04A1B2C3D4E5F6070809"])
@pytest.mark.parametrize("sequence", [1, 2**53 + 1, 2**63 - 1])
def test_firmware_json_fields_and_integer_range_match_backend(uid, sequence):
    fields = set(re.findall(r'request\["(\w+)"\] =', SOURCE))
    payload = {"scan_id": ID, "capture_session_id": SESSION, "sequence": sequence, "uid": uid}
    assert fields == set(payload) == set(ScanSubmit.model_fields)
    submitted = ScanSubmit.model_validate_json(json.dumps(payload))
    assert submitted.sequence == sequence and submitted.uid == uid
    assert '0x7fffffffffffffffULL' in SOURCE
    assert 'request["sequence"] = pending.sequence' in SOURCE


@pytest.mark.parametrize("sequence", [0, 2**63, "1", 1.5])
def test_backend_rejects_invalid_sequence_contract(sequence):
    with pytest.raises(ValidationError):
        ScanSubmit(scan_id=ID, capture_session_id=SESSION, sequence=sequence, uid="04A1B2C3")


def test_canonical_device_secret_tail_matches_backend():
    permitted = set("AEIMQUYcgkosw048")
    tails = set()
    for byte in range(256):
        secret = base64.urlsafe_b64encode(bytes(31) + bytes([byte])).rstrip(b"=").decode()
        tails.add(secret[-1])
        assert len(token_digest(secret)) == 64
    assert tails == permitted and 'strchr("AEIMQUYcgkosw048", s[42])' in SOURCE


@pytest.mark.parametrize("duplicate", [False, True])
def test_backend_receipt_contract_supports_initial_and_idempotent_success(duplicate):
    receipt = ScanReceipt(scan_id=UUID(ID), sequence=1, uid="04A1B2C3", asset_id=UUID(SESSION),
                          received_at="2026-11-20T12:00:00Z", expires_at="2026-11-20T12:02:00Z", duplicate=duplicate)
    validated_fields = set(re.findall(r'receipt\["(\w+)"\]', SOURCE))
    assert validated_fields <= set(ScanReceipt.model_fields)
    assert receipt.duplicate is duplicate
    assert {"scan_id", "sequence", "uid", "asset_id", "duplicate"} <= validated_fields


def test_sequence_is_reserved_durably_before_pending_event_and_no_runtime_reset():
    reserve = function("bool reserveSequence(", "class BoundedResponse")
    assert reserve.index("putULong64") < reserve.index("reserved = next")
    loop = SOURCE.split("void loop()", 1)[1]
    assert loop.index("reserveSequence(pending.sequence)") < loop.index("pending.active = true")
    assert "sequenceStore.clear(" not in SOURCE and "sequenceStore.remove(" not in SOURCE
    assert "highWater >= MAX_SEQUENCE" in reserve


def test_retry_sends_frozen_body_and_never_generates_new_event_identity():
    submit = function("void submitPending()", "void maintainWifi()")
    assert "http.POST(pending.body)" in submit
    assert "newScanUuid()" not in submit and "reserveSequence(" not in submit
    assert "MAX_ATTEMPTS = 3" in SOURCE and "MAX_PENDING_MS = 90000" in SOURCE
    assert 'http.collectHeaders(headers, 1)' in submit and '"Retry-After"' in submit
    assert "HTTPC_DISABLE_FOLLOW_REDIRECTS" in submit


def test_only_validated_200_response_calls_success_signal():
    submit = function("void submitPending()", "void maintainWifi()")
    success = submit.split("if (status == 200)", 1)[1].split("bool expiredSession", 1)[0]
    assert "acceptedReceipt(" in success and "if (!valid)" in success
    assert SOURCE.count("successSignal();") == 1
    assert "successSignal();" in success


def test_local_config_is_ignored_and_example_is_opt_in_only():
    ignore = (ROOT / ".gitignore").read_text()
    example = (SKETCH.parent / "scanner_config.example.h").read_text()
    assert "/arduino/sketch_nov19a/scanner_config.local.h" in ignore
    for name in ("WIFI_SSID", "WIFI_PASSWORD", "SCANNER_UUID", "SCANNER_DEVICE_SECRET", "CAPTURE_SESSION_UUID"):
        assert re.search(rf'{name}\[\] = "REPLACE_[A-Z_]+"', example)
    assert "ALLOW_TRUSTED_LAN_HTTP = false" in example
    assert "https://" not in example


def test_serial_calls_do_not_emit_runtime_credentials_or_payloads():
    statements = re.findall(r"Serial\.(?:print|println)\((.*?)\);", SOURCE)
    assert set(statements) == {"line1", '"RC522 version: 0x"', "version, HEX"}
    assert not any(value in " ".join(statements) for value in ("SECRET", "PASSWORD", "pending", "response"))
