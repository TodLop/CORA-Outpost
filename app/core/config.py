# app/core/config.py
import os
from pathlib import Path

import yaml

from app.core.deployment_identity import get_access_bootstrap_identity

# ==========================================
# 📂 PATH CONFIGURATION (경로 설정)
# ==========================================

# 1. 기준점 설정
# __file__  : 이 파일 (app/core/config.py)
# .parent   : app/core
# .parent.parent : app
# .parent.parent.parent : project_root (여기가 프로젝트의 뿌리입니다)
CORE_DIR = Path(__file__).resolve().parent
APP_DIR = CORE_DIR.parent
ROOT_DIR = APP_DIR.parent

# 2. 보안 파일 경로 (config_files 폴더)
# json 파일들은 이제 프로젝트 루트의 config_files 폴더에 모아둡니다.
CONFIG_FILES_DIR = ROOT_DIR / 'config_files'

TOKEN_FILE = CONFIG_FILES_DIR / 'token.json'
CREDS_FILE = CONFIG_FILES_DIR / 'credentials.json'
CLIENT_SECRETS_FILE = CONFIG_FILES_DIR / 'client_secret_web.json'

# 환경 변수 파일 (.env)은 루트에 둡니다.
ENV_FILE = ROOT_DIR / '.env'

# 3. 앱 리소스 경로
STATIC_DIR = APP_DIR / 'static'
TEMPLATES_DIR = APP_DIR / 'templates'

# 4. 데이터 저장 경로 (캐시, 히스토리)
# data 폴더가 없으면 자동으로 생성되지 않으므로, 사용하는 쪽에서 mkdir을 해야 합니다.
DATA_DIR = ROOT_DIR / 'data'
CACHE_DIR = DATA_DIR / 'cache'
HISTORY_DIR = DATA_DIR / 'history'
TASKBOARD_IMAGES_DIR = DATA_DIR / 'dashboard_images'
BACKUP_TEMP_DIR = DATA_DIR / 'backup_temp'
METRICS_DB_PATH = DATA_DIR / 'server_metrics.db'

# ==========================================
# 🎮 MINECRAFT SERVER CONFIGURATION
# ==========================================
# Change folder name here when switching servers (e.g., "minecraft_server_production")
MINECRAFT_SERVER_PATH = DATA_DIR / "minecraft_server_paper"

_ACCESS_BOOTSTRAP_IDENTITY = get_access_bootstrap_identity()

# Staff emails (limited permissions: start, restart, tempban)
STAFF_EMAILS = _ACCESS_BOOTSTRAP_IDENTITY.staff_emails

# Hardcoded staff Minecraft account mapping for security-sensitive actions.
# Keys must be normalized email addresses (lowercase). Values should normally
# be valid in-game usernames (3-16 chars, letters/numbers/underscore).
# Example:
# "staff@example.com": "StaffInGameName",
STAFF_MINECRAFT_IDS = _ACCESS_BOOTSTRAP_IDENTITY.staff_minecraft_ids

# Load protected players from YAML config (editable)
def load_protected_players():
    """Load protected players list from YAML config file."""
    config_file = DATA_DIR / "protected_players.yml"
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                data = yaml.safe_load(f)
                players = data.get("protected_players", []) if data else []
                return frozenset(players) if players else frozenset(["ExampleAdmin"])
        except Exception:
            pass
    return frozenset(["ExampleAdmin"])  # Fallback default

# Players that cannot be banned by staff (admin protection)
PROTECTED_PLAYERS = load_protected_players()


# ==========================================
# 📌 APP VERSION
# ==========================================
APP_VERSION = "4.1.0"

# ==========================================
# ⚙️ SERVER CONFIGURATION (서버 설정)
# ==========================================

# 포트 설정: 환경 변수가 없으면 8001 사용
PORT = int(os.getenv("PORT", 8000))

# 호스트 설정: 로컬 개발 환경
HOST = "127.0.0.1"

# API 기본 주소
API_BASE_URL = f"http://{HOST}:{PORT}"


# ==========================================
# 🔐 GOOGLE API SCOPES (권한 설정)
# ==========================================
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',  # 이메일 읽기
    'https://www.googleapis.com/auth/calendar.readonly', # 캘린더 읽기
    'https://www.googleapis.com/auth/spreadsheets',    # 스프레드시트 쓰기
    'https://www.googleapis.com/auth/userinfo.profile', # 사용자 프로필
    'https://www.googleapis.com/auth/userinfo.email',   # 사용자 이메일 (추가됨: 확실한 식별을 위해)
    'openid' # (추가됨: 구글 로그인 표준)
]
