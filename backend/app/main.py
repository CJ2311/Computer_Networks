from fastapi import FastAPI

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