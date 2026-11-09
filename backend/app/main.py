from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.cors import get_allowed_origins
from backend.app.routers.equipment import router as equipment_router
from backend.app.routers.asset import router as asset_router
from backend.app.routers.checkout import router as checkout_router
from backend.app.routers.maintenance import router as maintenance_router
from backend.app.routers.inventory_audit import router as inventory_audit_router
from backend.app.routers.auth import router as auth_router
from backend.app.routers.operations import router as operations_router
from backend.app.routers.reports import router as reports_router


app = FastAPI(
    title="IoT Laboratory Inventory Management System",
    description="Backend API for managing laboratory equipment, borrowing, and inventory tracking.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

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
