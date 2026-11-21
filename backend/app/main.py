from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.secure_logging import SafeAccessLogMiddleware, install_secure_logging

install_secure_logging()

from backend.app.cors import get_allowed_origins
from backend.app.request_limits import RequestSizeLimitMiddleware, get_max_request_body_bytes
from backend.app.routers.equipment import router as equipment_router
from backend.app.routers.asset import router as asset_router
from backend.app.routers.checkout import router as checkout_router
from backend.app.routers.maintenance import router as maintenance_router
from backend.app.routers.inventory_audit import router as inventory_audit_router
from backend.app.routers.auth import router as auth_router
from backend.app.routers.operations import router as operations_router
from backend.app.routers.reports import router as reports_router
from backend.app.routers.scanners import router as scanners_router
from backend.app.routers.student_nfc import router as student_nfc_router


app = FastAPI(
    title="IoT Laboratory Inventory Management System",
    description="Backend API for managing laboratory equipment, borrowing, and inventory tracking.",
    version="0.1.0",
)

allowed_origins = get_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-Scan-Session-Token"],
)
app.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=get_max_request_body_bytes(),
                   allowed_origins=allowed_origins, path_limits={"/device-scans": 2048})
app.add_middleware(SafeAccessLogMiddleware)

@app.get("/")
def root():
    return {
        "message": "IoT Laboratory Inventory Management System",
        "status": "running",
        "version": "0.1.0",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


# Register equipment endpoints
app.include_router(equipment_router)
app.include_router(asset_router)
app.include_router(checkout_router)
app.include_router(maintenance_router)
app.include_router(inventory_audit_router)
app.include_router(auth_router)
app.include_router(operations_router)
app.include_router(reports_router)
app.include_router(scanners_router)
app.include_router(student_nfc_router)
