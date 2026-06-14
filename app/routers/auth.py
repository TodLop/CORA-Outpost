# app/routers/auth.py
import os
import logging
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# 설정 파일에서 필요한 정보 가져오기
from app.core.config import CLIENT_SECRETS_FILE, SCOPES

# 라우터 생성
router = APIRouter()

# 로컬 개발 환경에서만 HTTPS를 강제하지 않도록 설정
if os.environ.get('ENVIRONMENT', 'development') != 'production':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'


def _normalize_redirect_target(value: str | None) -> str | None:
    if not value:
        return None

    target = value.strip()
    if not target.startswith("/") or target.startswith("//"):
        return None

    return target

def get_flow(request: Request):
    """
    OAuth2 Flow 객체를 생성하는 헬퍼 함수입니다.
    현재 요청의 URL을 기반으로 Redirect URI를 동적으로 설정합니다.
    """
    # 인증 후 돌아올 콜백 주소 생성 (예: http://localhost:8001/auth/callback)
    auth_url = str(request.url_for("auth_callback"))
    
    # HTTPS 강제 변환 (프로덕션 환경 등에서 필요한 경우 사용, 로컬은 http 유지)
    # if auth_url.startswith("http://") and "localhost" not in auth_url and "127.0.0.1" not in auth_url:
    #     auth_url = auth_url.replace("http://", "https://", 1)

    # Path 객체를 문자열로 변환하여 라이브러리에 전달
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRETS_FILE),
        scopes=SCOPES,
        redirect_uri=auth_url
    )
    return flow

@router.get("/login")
async def login(request: Request):
    """
    구글 로그인 페이지로 리다이렉트합니다.
    """
    next_path = _normalize_redirect_target(request.query_params.get("next"))
    if next_path:
        request.session["post_login_redirect"] = next_path
    else:
        request.session.pop("post_login_redirect", None)

    flow = get_flow(request)
    
    # 인증 URL 생성
    authorization_url, state = flow.authorization_url(
        access_type='offline',          # 리프레시 토큰을 받기 위해 offline 설정
        include_granted_scopes='true',  # 점진적 권한 동의
        prompt='consent'                # 매번 동의 화면 표시 (테스트용, 배포 시 제거 가능)
    )
    
    # 상태값(State)을 세션에 저장 (CSRF 방지)
    request.session["state"] = state
    
    return RedirectResponse(authorization_url)

@router.get("/auth/callback")
async def auth_callback(request: Request):
    """
    구글 인증 후 돌아오는 콜백 URL입니다.
    토큰을 교환하고 사용자 정보를 세션에 저장합니다.
    """
    # OAuth state (CSRF) 검증
    callback_state = request.query_params.get("state")
    session_state = request.session.get("state")
    if not callback_state or callback_state != session_state:
        logger.warning("OAuth state mismatch: callback=%s session=%s", callback_state, session_state)
        return RedirectResponse("/login", status_code=303)

    flow = get_flow(request)

    try:
        # URL에 포함된 코드를 토큰으로 교환
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        logger.error("OAuth token exchange failed", exc_info=True)
        return RedirectResponse("/login", status_code=303)

    # State는 일회용 — 사용 후 제거
    request.session.pop("state", None)

    creds = flow.credentials
    # Cookie-backed session에서 OAuth credential payload를 제거한다.
    request.session.pop("creds", None)

    # 사용자 정보(프로필) 가져오기
    try:
        service = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()
        request.session["user_info"] = user_info
    except Exception as e:
        logger.error("Failed to fetch user info", exc_info=True)
        request.session["user_info"] = {"name": "User"}

    redirect_target = _normalize_redirect_target(request.session.pop("post_login_redirect", None)) or "/"

    # 로그인 성공 후 원래 요청한 페이지 또는 메인 페이지로 이동
    return RedirectResponse(redirect_target, status_code=303)

@router.get("/logout")
async def logout(request: Request):
    """
    세션을 비우고 로그아웃 처리합니다.
    """
    request.session.clear()
    return RedirectResponse("/")
