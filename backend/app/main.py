from fastapi import FastAPI

from backend.app.routers.equipment import router as equipment_router
from backend.app.routers.asset import router as asset_router

app = FastAPI(
    title="IoT Laboratory Inventory Management System",
    description="Backend API for managing laboratory equipment, borrowing, and inventory tracking.",
    version="0.1.0",
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