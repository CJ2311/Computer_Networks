# ESP32 RC522 scanner → FastAPI (Step 12C.2B)

This firmware only submits equipment scan events. It does not authenticate staff,
resolve borrowers, initiate checkout/returns, change inventory or contact PostgreSQL.
Staff still retrieve the staged scan, enter the borrower's name/registration number,
and explicitly confirm checkout through the existing APIs.

The implementation has not been compiled, uploaded or tested on hardware as part
of this change. The previously reported backend results are baseline results,
not verification of this firmware.

## Hardware and Arduino dependencies

Keep the existing wiring unchanged:

| Component | ESP32 GPIO |
|---|---|
| RC522 SS / RST | 5 / 4 |
| RC522 SCK / MISO / MOSI | 18 / 19 / 23 |
| OLED SDA / SCL (address 0x3C) | 21 / 22 |
| Green / red LED | 25 / 26 |
| Buzzer | 27 |

Install the Espressif **esp32** Arduino board package (target Arduino-ESP32 3.x)
and select the board matching the actual ESP32 module. Library Manager dependencies:

- **MFRC522** (existing RC522 library).
- **Adafruit GFX Library**, **Adafruit SSD1306**, and their Adafruit BusIO dependency.
- **ArduinoJson 7.x** (the sketch uses `JsonDocument` and 64-bit integer JSON values).

SPI, Wire, WiFi, HTTPClient, Preferences and ESP random functions come from the
ESP32 board package. No PN532 library is used. Set **Core Debug Level: None** to
avoid library debug output; application Serial diagnostics contain only fixed
state messages and the RC522 version register, never credentials, UIDs or bodies.
Exact board/library compatibility still requires a manual compile and upload.

## Private local configuration

Copy `arduino/sketch_nov19a/scanner_config.example.h` beside the sketch as
`scanner_config.local.h`. The local header is Git-ignored; the example contains
placeholders only. Missing local configuration deliberately fails compilation.
Do not place real credentials in the `.ino`, example, documentation or Git.

Fill these local constants:

| Constant | Value |
|---|---|
| `WIFI_SSID` / `WIFI_PASSWORD` | Trusted Wi-Fi network shared by laptop and ESP32 |
| `FASTAPI_BASE_URL` | Laptop LAN IPv4, e.g. `http://192.168.1.100:8000` |
| `SCANNER_UUID` | UUID produced by provisioning `register` |
| `SCANNER_DEVICE_SECRET` | Device secret printed after successful registration |
| `CAPTURE_SESSION_UUID` | Current UUID printed by `start-session` or `renew-session` |
| `ALLOW_TRUSTED_LAN_HTTP` | Explicitly set to `true` only for the trusted demo LAN |

Use the existing [provisioning guide](../backend/SCANNER_PROVISIONING.md). Provisioning
is a trusted operator action, not a firmware operation. Do not give the ESP32 the
database password, staff bearer token, or `X-Scan-Session-Token`. The capture UUID
is sufficient alongside the device credential. Store the capture secret only with
the selected authenticated staff dashboard session.

The local file and compiled firmware contain plaintext device/Wi-Fi credentials.
Keep the workstation, firmware binaries, backups and physical device private.
Git-ignore does not protect backups or compiled artifacts. Revoke a lost device.
Do not enable terminal transcription or publish screenshots containing credentials.

## Laptop network and API

On Windows, run `ipconfig` manually and locate **IPv4 Address** under the active
Wi-Fi adapter. Use that address, not a Docker/virtual adapter or default gateway.
This demonstration firmware accepts only explicit RFC1918 addresses (10/8,
172.16/12 or 192.168/16), with an optional port. No hostname, loopback address,
URL path, query, credentials or trailing slash is accepted. Port defaults to 80
when omitted. Set the API's actual port explicitly when using 8000.

Start the already configured backend in the project's Python environment using
the normal deployment procedure. For a manual LAN demo, its listener must bind
the laptop's LAN interface or `0.0.0.0`, for example:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
```

This command is for the operator and was not executed during implementation. It
does not provision scanners or prepare a database. Keep existing authentication,
deployment verification and database settings. Allow the API port through Windows
Firewall only on the intended private network. Do not expose PostgreSQL: it stays
localhost-only and the ESP32 never uses its port. CORS is a browser control and
does not authorize or prevent ESP32 HTTP requests.

Both devices must be able to communicate directly. Guest-network isolation,
college access-point client isolation, captive portals, VPN routing or changed
DHCP addresses can prevent this. Prefer a reserved laptop LAN address for the demo.
The classic ESP32 normally requires a compatible 2.4 GHz Wi-Fi network; confirm
the actual module and college network requirements. This sketch uses normal
SSID/password Wi-Fi, not enterprise EAP or captive-portal authentication.

**HTTP is not encrypted. Network observers can capture the device secret and scan
traffic.** Private addressing is not encryption or proof of network trust. This
firmware requires explicit HTTP opt-in and is for the trusted demo only. Production
requires an HTTPS implementation with certificate verification, trusted time/CA
management and protected credential provisioning. It intentionally has no insecure
TLS fallback, certificate-verification bypass, redirects or production-TLS claim.

## Upload and session renewal

1. Open `arduino/sketch_nov19a/sketch_nov19a.ino` in Arduino IDE.
2. Install/select the libraries and actual board/USB port; configure the private
   header. Choose normal upload with **Erase All Flash Before Sketch Upload: Disabled**.
3. Have an operator register the scanner and start a session for an existing active
   FACULTY or LAB_ASSISTANT. Set the resulting device credential and session UUID.
4. Compile/upload manually and open Serial Monitor at **115200**. Do not paste
   credentials into Serial Monitor; there is no Serial credential command.
5. Confirm the OLED reaches Wi-Fi Connected / Ready to Scan. Present a registered
   equipment tag. Green LED and a short beep mean a matching backend receipt was
   validated; RFID detection alone never produces success.
6. Check the equipment appears through the existing staff scan-retrieval endpoint.
   Leave a tag held against the reader to verify it is not repeatedly submitted,
   then remove and re-present it to create another scan.

Capture sessions last at most 15 minutes. After operator renewal, update only
`CAPTURE_SESSION_UUID` in the private header and upload again without erasing NVS.
Supply the new capture secret to the selected staff session separately. The old
session UUID and secret immediately stop working. There is no automatic pairing,
OTA session update, browser integration or credential-setup web server in this step.
Re-uploading/restarting can lose an in-flight scan's acknowledgement; check staff
results before deliberately scanning it again.

## Protocol, persistence and bounds

The device sends only `POST /device-scans` with:

```text
Authorization: Scanner <scanner_uuid>.<device_secret>
Content-Type: application/json
```

```json
{
  "scan_id": "<random-version-4-uuid>",
  "capture_session_id": "<active-session-uuid>",
  "sequence": 1,
  "uid": "04A1B2C3"
}
```

Only 4-, 7- or 10-byte UIDs are submitted, as uppercase hex with leading zeroes
preserved. The fixed body is below the backend's existing 2 KiB ingestion limit.
One scan is pending at a time; new RFID reads wait until it completes. No unbounded
queue or offline accumulation exists. Remove and re-present a halted card for a
new scan; the existing two-second same-UID cooldown is retained.

Before network submission, Preferences/NVS namespace `equip-scan` reserves the
next unsigned 64-bit value in `sequence`, bounded to PostgreSQL's signed bigint
maximum (`9223372036854775807`). It commits and reads back that reservation before
sending. Scanner UUID is also stored to prevent silently reusing another identity's
state. Wi-Fi reconnection, session renewal, HTTP failure and reboot never decrement
or reset the counter. A failed request may consume its number; gaps are valid.
JSON uses an integer, not a floating-point counter, including above 2^53.

Every retry uses the identical serialized body (same UUID, session, sequence and
UID). The RAM-only pending payload is lost on reboot, but its sequence remains
reserved: it is not reissued as a different request. Outcome can remain uncertain
after a power cut or lost response. There is no guarantee of exactly-once physical
scanning or durable replay across reboot.

NVS errors, partially initialized state, a different configured scanner UUID, and
counter exhaustion stop ingestion and require operator review. Do not clear NVS
or clone its contents to another device. An erased/replaced board cannot recover
the old high-water mark: revoke that scanner and provision a fresh identity, rather
than restarting an existing identity at zero. Rebinding a different scanner identity
on a board also requires a deliberate operator recovery procedure; there is no
automatic NVS reset command. Each new scan writes NVS; normal wear leveling applies,
but high-volume deployments should evaluate flash endurance separately.

## States, failures and retry policy

| OLED / outcome | Meaning and action |
|---|---|
| Starting / Connecting Wi-Fi | Normal startup; connection attempts are nonblocking |
| Wi-Fi Connected / Ready to Scan | Ready for a new equipment tag |
| Sending Scan | Request in progress; RFID read is not yet accepted |
| Scan Accepted / Equipment staged | Validated HTTP 200 receipt matches UUID, sequence, UID and asset |
| Scan Accepted / Already received | Validated HTTP 200 idempotent duplicate; also success |
| Unknown Equipment | HTTP 404; register/inspect the equipment through normal staff workflows |
| Session Expired | Known HTTP 409 session-unavailable response; obtain/update the active session UUID |
| Session / Conflict | Other HTTP 409: session mismatch, sequence conflict, UID cooldown or ambiguous tag; no retry |
| Scanner Denied | HTTP 401/403; scanner stops until configuration/restart after operator review |
| Invalid Scan | HTTP 400/413/422; no retry; inspect configuration/protocol |
| Scan Rejected | Unexpected status, including redirects; no automatic retry |
| Network Error / Retry scheduled | Transient failure; same payload will be retried within bounds |
| Server Backoff / Retry scheduled | Server-directed delay (including rate limiting); new scans also wait |
| Network Error / Outcome uncertain | Retry budget expired; backend acceptance is unknown; check staff results |
| Server Backoff / Operator required | Unsupported/long Retry-After; automatic scanning stops instead of retrying early |
| Sequence Error / Configuration Error | Fail closed; investigate local setup/state |
| RFID ERROR | RC522 version 0x00/0xFF; inspect wiring/power |

Timeouts: each Wi-Fi connection attempt has a 15-second budget and reconnect
attempts are spaced at least 10 seconds apart. HTTP connect/read inactivity timeouts
are 3/4 seconds. Response bodies are limited to 1 KiB with an additional 8-second
sink deadline; oversized/malformed success receipts never produce a green signal.
HTTPClient remains synchronous during each bounded request; these are not hard
real-time guarantees against a hostile HTTP peer or board/network-driver stalls.

Transport failures, HTTP 408/429/5xx, and invalid success receipts get at most
**three attempts total**, using 1/2-second exponential retry delays. Pending age is
limited to 90 seconds, below the normal event-retention period of 120 seconds.
There is no automatic retry for authentication, validation, unknown tags or conflicts.

Numeric `Retry-After` is honored when it fits the 60-second scheduling bound and
remaining pending budget. A 429 without that header defaults to 60 seconds. The
backoff also blocks new scans if the pending event is abandoned, so a new card
cannot evade it. HTTP-date values or delays above 60 seconds stop the scanner for
operator review; it never silently retries earlier. After retry exhaustion, do not
assume rejection: inspect the authenticated staff result before resubmitting a
physical scan. Green LED/short beep occurs only after validated backend acceptance;
rejections and failures use red LED/error beep.

## Troubleshooting and verification

- **Configuration Error:** check placeholder replacement, canonical 43-character
  device secret, UUIDs, private IPv4-only URL and explicit HTTP opt-in.
- **No Wi-Fi:** check SSID/password/band, signal and client-isolation policies.
- **Timeout:** check laptop IPv4, listener, firewall, port and backend availability.
- **401:** check the scanner UUID/device secret pair and revocation state; never
  substitute DEV-FACULTY, DEV-LAB_ASSISTANT or staff OAuth credentials.
- **409 after renewal:** update the session UUID; do not reset the sequence counter.
- **Unknown equipment:** use the backend's stored asset tag representation; leading
  zeroes matter. An unknown scan intentionally creates no equipment or borrower.
- **Repeated rate limiting:** stop repeated manual scans; keep server backoff intact.
- **Sequence Error after re-upload:** verify flash was not erased or scanner UUID
  changed. Escalate to the provisioning operator, not a database/counter reset.

Added database-free checks live in `backend/tests/test_esp32_firmware_contract.py`.
They check configuration/source guardrails and compatibility with Pydantic request,
credential and receipt contracts. They do **not** execute the firmware or prove C++
compilation, NVS power-loss behavior, HTTP timing, Wi-Fi recovery or electrical
operation. Run them manually using the project's Python environment with an
external timeout, then compile and verify the actual board. Manually exercise
unknown tags, expired sessions, backend/Wi-Fi interruptions, reboot persistence,
idempotent acknowledgements, held-card behavior and rate limiting. No tests,
compilation, uploads, provisioning, Docker or database operations were executed
during this change.
