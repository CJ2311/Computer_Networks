#pragma once
// Copy to scanner_config.local.h (Git-ignored). Never commit real credentials.
static const char WIFI_SSID[] = "REPLACE_WITH_WIFI_SSID";
static const char WIFI_PASSWORD[] = "REPLACE_WITH_WIFI_PASSWORD";
// Laptop RFC1918 LAN IPv4; no trailing slash or path. Never localhost.
static const char FASTAPI_BASE_URL[] = "http://192.168.1.100:8000";
static const char SCANNER_UUID[] = "REPLACE_WITH_PROVISIONED_SCANNER_UUID";
static const char SCANNER_DEVICE_SECRET[] = "REPLACE_WITH_PROVISIONED_DEVICE_SECRET";
static const char CAPTURE_SESSION_UUID[] = "REPLACE_WITH_ACTIVE_CAPTURE_SESSION_UUID";
// HTTP exposes device credentials to network observers. Explicit demo opt-in.
static const bool ALLOW_TRUSTED_LAN_HTTP = false;
