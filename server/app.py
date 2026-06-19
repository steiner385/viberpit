from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="ccswitch", version="0.1.0")

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
