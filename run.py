"""Convenience launcher: `python run.py` starts the app on http://127.0.0.1:8000"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  System V8 Librarian -> http://{host}:{port}\n")
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)
