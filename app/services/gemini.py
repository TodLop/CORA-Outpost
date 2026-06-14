# app/services/gemini.py

import os
import time
import logging
from dotenv import load_dotenv
from google import genai
from google.api_core import exceptions as google_exceptions

# [설정 파일 임포트]
from app.core.config import ENV_FILE

# 1. 환경변수 로드
load_dotenv(dotenv_path=ENV_FILE)

api_key_value = os.getenv("GEMINI_API_KEY")
client = None

# API Key 확인 및 클라이언트 초기화
if not api_key_value:
    logging.warning("[Warning] GEMINI_API_KEY not found in environment variables.")
else:
    try:
        client = genai.Client(api_key=api_key_value)
    except Exception as e:
        logging.error(f"Failed to initialize Gemini client: {e}")

def _call_gemini(prompt: str, max_retries=3) -> str:
    """
    Gemini API를 호출하는 내부 헬퍼 함수 (자동 재시도 로직 포함).
    """
    if not client:
        return "Error: Gemini client not initialized."

    # 재시도 루프
    for attempt in range(1, max_retries + 1):
        try:
            # [수정] 사용자가 원래 사용하던 모델(gemini-2.5-flash)로 원복
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return response.text

        except Exception as e:
            error_msg = str(e)
            logging.warning(f"[Gemini] Attempt {attempt}/{max_retries} failed: {error_msg}")
            
            # 마지막 시도였다면 에러 반환
            if attempt == max_retries:
                logging.error(f"[Gemini] All {max_retries} attempts failed.")
                return "Unable to generate content due to repeated API errors."
            
            # 지수 백오프 (Exponential Backoff): 2초, 4초, 8초...
            wait_time = 2 ** attempt 
            print(f"   -> ⚠️  API Error (Overloaded?). Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

def batch_summarize_emails(email_list):
    """
    이메일 목록(보낸사람, 제목, 스니펫)을 받아 요약합니다.
    """
    if not email_list:
        return []

    total_emails = len(email_list)
    print(f"\n[Gemini] Preparing data for {total_emails} emails...")

    # 프롬프트 구성
    prompt = "The following is a list of recently received emails. Please summarize each into one very concise sentence in the format 'Sender: Key Point'. Do not use numbering, separate by line breaks.\n\n"
    
    for idx, email in enumerate(email_list):
        # 제목 표시용 자르기
        display_subject = (email['subject'][:40] + '..') if len(email['subject']) > 40 else email['subject']
        print(f"  -> [Processing {idx+1}/{total_emails}] {display_subject}")

        prompt += f"[{idx+1}] From: {email['sender']} | Subject: {email['subject']} | Content: {email['snippet']}\n"

    print(f"[Gemini] Sending batch request to AI model... (Please wait)")

    # Gemini 호출
    result_text = _call_gemini(prompt)

    # 결과 파싱
    summaries = [s.strip() for s in result_text.strip().split('\n') if s.strip()]
    
    # 개수 불일치 시 Fallback
    if len(summaries) != len(email_list):
        logging.warning(f"[Gemini] Count mismatch (Input: {len(email_list)}, Output: {len(summaries)}). Using originals.")
        return [f"{e['sender']}: {e['subject']}" for e in email_list]
    
    print("[Gemini] Summarization complete.")
    return summaries

def generate_daily_todos(email_context, calendar_context):
    """
    이메일과 캘린더 일정을 바탕으로 오늘의 추천 할 일 3가지를 제안합니다.
    """
    print("[Gemini] Analyzing your schedule and emails for To-Do recommendations...")
    
    prompt = f"""
    You are my smart personal assistant. Based on the email and schedule info below, summarize '3 Key Tasks I must do today'.
    
    [Criteria]
    1. Prioritize preparation for scheduled events or deadlines.
    2. Include urgent email replies or actions.
    3. If no urgent tasks, suggest general self-improvement or organization.
    4. Tone: Polite, Clear, Korean Language.

    [Recent Emails]
    {email_context}

    [Calendar Events]
    {calendar_context}

    [Output Format]
    오늘의 추천 할 일 3가지:
    1. (Task) - (Reason)
    2. (Task) - (Reason)
    3. (Task) - (Reason)
    """

    return _call_gemini(prompt)

def generate_strategic_advice(email_context, calendar_context, user_profile):
    """
    사용자의 프로필(목표, 스타일)과 현재 상황을 종합하여 전략적 조언을 생성합니다.
    """
    print("[Gemini] Synthesizing strategic advice based on your profile...")

    prompt = f"""
    You are a wise and strategic personal mentor.
    
    [User Profile]
    {user_profile}

    [Current Situation]
    - Calendar Context:
    {calendar_context}
    
    - Recent Email Context:
    {email_context}

    [Task]
    Based on the user's profile (goals, constraints) and today's actual data (schedule, emails),
    provide a brief, high-level strategic advice paragraph for today.
    
    - If the schedule is busy, advise on efficiency or prioritization.
    - If there are urgent emails, remind them to handle those first to clear mental friction.
    - If the day is clear, encourage progress on long-term goals mentioned in the profile.
    - Tone: Encouraging, analytical, and concise (Korean).
    
    [Output]
    Write 2-3 sentences of actionable advice.
    """
    
    return _call_gemini(prompt)


def generate_economy_summary(report_data: dict) -> str:
    """
    경제 보고서 데이터를 바탕으로 AI 요약을 생성합니다.
    Gemini 3 Flash Preview 모델을 사용합니다.
    """
    print("[Gemini] Generating AI summary for economy report...")
    
    if not client:
        return "Error: Gemini client not initialized."
    
    # 보고서 데이터에서 주요 정보 추출
    period_label = report_data.get("period_label", "Unknown Period")
    net_change = report_data.get("net_change", 0)
    net_change_percent = report_data.get("net_change_percent", 0)
    starting_balance = report_data.get("starting_balance", 0)
    ending_balance = report_data.get("ending_balance", 0)
    gini_change = report_data.get("gini_change", 0)
    positive_days = report_data.get("positive_days", 0)
    negative_days = report_data.get("negative_days", 0)
    top_gainers = report_data.get("top_gainers", [])
    top_losers = report_data.get("top_losers", [])
    
    # 상위 변동 플레이어 정보 구성
    gainers_str = ", ".join([f"{p['name']}(+{p['change']:,.0f})" for p in top_gainers[:3]]) if top_gainers else "없음"
    losers_str = ", ".join([f"{p['name']}({p['change']:,.0f})" for p in top_losers[:3]]) if top_losers else "없음"
    
    prompt = f"""
    당신은 마인크래프트 서버 경제 분석가입니다. 아래 데이터를 바탕으로 간결하고 통찰력 있는 한국어 경제 보고서 요약을 작성해주세요.
    
    [기간]: {period_label}
    [시작 총 자산]: {starting_balance:,.0f}원
    [종료 총 자산]: {ending_balance:,.0f}원
    [순 변화]: {net_change:+,.0f}원 ({net_change_percent:+.1f}%)
    [지니 계수 변화]: {gini_change:+.4f} (0에 가까울수록 평등)
    [상승일/하락일]: {positive_days}일 / {negative_days}일
    [자산 증가 Top 3]: {gainers_str}
    [자산 감소 Top 3]: {losers_str}
    
    [요청사항]
    1. 2-3문장으로 전체 경제 동향을 요약해주세요.
    2. 주목할 만한 변화나 패턴이 있다면 언급해주세요.
    3. 서버 경제 건강도에 대한 짧은 평가를 포함해주세요.
    4. 친근하고 읽기 쉬운 톤으로 작성해주세요.
    """
    
    # Gemini 3 Flash Preview 모델 사용
    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
            )
            print("[Gemini] Economy summary generated successfully.")
            return response.text
        except Exception as e:
            error_msg = str(e)
            logging.warning(f"[Gemini] Economy summary attempt {attempt}/3 failed: {error_msg}")
            if attempt == 3:
                logging.error("[Gemini] Failed to generate economy summary.")
                return None
            time.sleep(2 ** attempt)