# run.py
import uvicorn
import os
import sys

# 프로젝트 루트 경로를 시스템 경로에 추가 (모듈 인식 문제 방지)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.core.config import PORT, HOST, APP_VERSION

if __name__ == "__main__":
    reload_enabled = os.getenv("UVICORN_RELOAD", "false").strip().lower() in {"1", "true", "yes", "on"}

    print(f"===========================================================")
    print(f" CORA Minecraft Admin v{APP_VERSION} starting")
    print(f" Dashboard URL: http://{HOST}:{PORT}")
    print(f"===========================================================")

    uvicorn.run(
        "app:create_app", 
        host=HOST, 
        port=PORT, 
        reload=reload_enabled, 
        factory=True
    )
