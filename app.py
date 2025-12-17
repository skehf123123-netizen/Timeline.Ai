import streamlit as st
import os
import time
import pandas as pd
import base64
import json
import io
import difflib
import tempfile
import shutil
import concurrent.futures
import math
import random
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, BadRequestError
from supabase import create_client, Client
from PIL import Image, ImageOps

# =====================================
# 0) 라이브러리 예외 처리 (Dependency Check)
# =====================================
# 필수 라이브러리가 없어도 앱이 터지지 않도록 플래그 설정 및 임포트 처리

# 1. HEIF 이미지 지원
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # 라이브러리 없으면 HEIC 지원 안함

# 2. DecompressionBomb 방어 (대용량 이미지 처리)
Image.MAX_IMAGE_PIXELS = 100_000_000

# 3. openpyxl (엑셀 저장)
try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# 4. pypdf (PDF 분석)
try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# 5. pydub (오디오 변환)
try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False

# 6. moviepy (비디오 분석)
try:
    import moviepy.editor as mp
    HAS_MOVIEPY = True
except (ImportError, RuntimeError, OSError):
    HAS_MOVIEPY = False

# Pillow Resampling 호환성 (구버전 대응)
try:
    RESAMPLING_METHOD = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLING_METHOD = Image.LANCZOS

# =====================================
# 환경 설정 및 초기화
# =====================================
st.set_page_config(
    page_title="Timeline.Ai", 
    page_icon="⚖️", 
    layout="wide", 
    initial_sidebar_state="expanded"
)

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
REDIRECT_URL = os.getenv("SUPABASE_REDIRECT_URL", "http://localhost:8501")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("❌ .env 파일에서 Supabase 설정을 찾을 수 없습니다.")
    st.stop()

if not OPENAI_KEY:
    st.error("❌ .env 파일에서 OPENAI_API_KEY를 찾을 수 없습니다.")
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = OpenAI(api_key=OPENAI_KEY)

if "auth_mode" not in st.session_state:
    st.session_state["auth_mode"] = "login"
if "user" not in st.session_state:
    st.session_state["user"] = None
if 'is_dark_mode' not in st.session_state:
    st.session_state['is_dark_mode'] = False
if 'result_data' not in st.session_state:
    st.session_state.result_data = []

# =====================================
# 1) 상수 설정
# =====================================
MAX_MEDIA_MS = 2 * 60 * 60 * 1000  # 2시간
MAX_PDF_CHUNK_SIZE = 15000         # PDF 청크 크기

MAX_IMAGES_PRO = 100
MAX_IMAGE_DIMENSION = 3072 # 이미지 분석 시 리사이즈 제한 (비용 절감 및 속도)
DEFAULT_BATCH_SIZE = 3     # 배치 사이즈
MAX_ZIP_SIZE_MB = 200      # ZIP 다운로드 용량 제한
DEFAULT_MAX_TOKENS = 2048  # [수정] 기본 토큰 수 제한 하향

# =========================================================
# [수정 1/3] 스키마: 날짜/시간/타임스탬프 "미확인" 허용
# =========================================================
# =========================================================
# [수정] 스키마: Pattern(정규식) 제거 -> AI가 자유롭게 추출 후 후처리
# =========================================================
TIMELINE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "timeline_response",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            # pattern 정규식 제약 제거 (AI가 자유롭게 텍스트 추출 가능하게 함)
                            "timestamp": {"type": "string"}, 
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "context": {"type": "string"},
                            "sender": {"type": "string"},
                            "content": {"type": "string"},
                            "importance": {"type": "string", "enum": ["상", "중", "하", "미상"]},
                            "is_estimated": {"type": "boolean"}
                        },
                        "required": ["timestamp", "date", "time", "context", "sender", "content", "importance", "is_estimated"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["messages"],
            "additionalProperties": False
        }
    }
}

# =========================================================
# [수정 1/3] 공통 인스트럭션: 1970 강제 금지, 미확인 사용
# =========================================================
COMMON_SCHEMA_INSTRUCTION = """
    [필수 포함 필드 (Strict Schema)]
    모든 메시지 객체는 아래 필드를 반드시 포함해야 합니다. 정보가 없으면 '미확인'으로 두십시오.
    - timestamp: "YYYY-MM-DD HH:MM:SS" 또는 "미확인"
    - date: "YYYY-MM-DD" 또는 "미확인"
    - time: "HH:MM:SS" 또는 "미확인"
    - context: 대화방 이름, 문서 제목, 상황 설명 등 (식별 불가 시 '미확인')
    - sender: 발화자 이름 (식별 불가 시 '불상')
    - content: 내용 원문 (Verbatim)
    - importance: "상/중/하/미상"
    - is_estimated: boolean (true/false)  ※ 날짜/시간이 미확인 또는 추정이면 true
"""

# =====================================
# 2) 유틸리티 및 AI 분석 함수
# =====================================
def safe_json_loads(s):
    try:
        return json.loads(s)
    except Exception:
        try:
            if not isinstance(s, str):
                s = str(s)
            start = s.find("{")
            end = s.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(s[start:end + 1])
        except Exception:
            pass
        return {}

def optimize_image_bytes(image_bytes: bytes):
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            w, h = img.size
            
            # [수정] 긴 스크린샷 대응 로직: 
            # 세로(h)가 아무리 길어도, 가로(w)가 1024px 이하라면 리사이즈 하지 않음 (화질 유지)
            # 가로가 너무 클 때만 줄여서 AI 토큰 비용 절약
            
            # 기준: 가로가 2048보다 크면 줄임, 아니면 원본 유지
            if w > 2048:
                scale = 2048 / w
                new_w = int(w * scale)
                new_h = int(h * scale)
                img = img.resize((new_w, new_h), RESAMPLING_METHOD)
            
            # (옵션) 하지만 높이가 OpenAI 제한(약 10,000~15,000px)을 넘어가면 오류가 날 수 있으므로
            # 극단적으로 긴 이미지는 반으로 자르는 등의 처리가 필요하지만,
            # 우선은 높이 제한을 넉넉하게 8000으로 둠
            elif h > 8000:
                # 가로폭이 충분하다면 높이만 줄이는 건 비율 깨짐 -> 비율 유지하며 줄임
                scale = 8000 / h
                # 단, 이렇게 줄였을 때 가로가 너무 작아지면(600px 미만) 안 줄임
                if (w * scale) > 600:
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    img = img.resize((new_w, new_h), RESAMPLING_METHOD)

            buffer = io.BytesIO()
            # 텍스트 선명도를 위해 품질 100 설정
            img.save(buffer, format="JPEG", quality=85)
            return base64.b64encode(buffer.getvalue()).decode("utf-8")

    except Image.DecompressionBombError:
        print("Image too large (DecompressionBomb)")
        return None
    except Exception as e:
        print(f"Optimize Error: {e}")
        return None

# =========================================================
# [수정 1/3] normalize_message_item: 1970 강제 세팅 제거, 미확인 유지
# =========================================================
def normalize_message_item(item: dict) -> dict:
    """
    - timestamp/date/time이 정상 포맷이면 date/time 보정
    - 인식 불가/누락이면 미확인 유지 + is_estimated=True 강제
    """
    ts_str = (item.get("timestamp") or "").strip()

    # 기본값 안전화
    if not item.get("timestamp"):
        item["timestamp"] = "미확인"
    if not item.get("date"):
        item["date"] = "미확인"
    if not item.get("time"):
        item["time"] = "미확인"
    if "is_estimated" not in item:
        item["is_estimated"] = True
    if not item.get("importance"):
        item["importance"] = "미상"

    # timestamp가 정상 포맷이면 date/time 동기화
    if ts_str and ts_str != "미확인":
        try:
            dt = pd.to_datetime(ts_str, errors="raise")
            item["timestamp"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            item["date"] = dt.strftime("%Y-%m-%d")
            item["time"] = dt.strftime("%H:%M:%S")
        except Exception:
            # 파싱 실패 시 미확인 처리
            item["timestamp"] = "미확인"
            item["date"] = "미확인"
            item["time"] = "미확인"
            item["is_estimated"] = True

    # date/time이 미확인이면 is_estimated는 true가 자연스러움
    if item.get("timestamp") == "미확인" or item.get("date") == "미확인" or item.get("time") == "미확인":
        item["is_estimated"] = True

    return item

def call_chat_json_robust(api_key, messages, max_tokens=DEFAULT_MAX_TOKENS):
    """
    GPT 호출 안전 래퍼: 4o(Schema) -> 4o-mini(Schema) -> 4o(JSON) 폴백 전략
    [수정] max_tokens 기본값을 DEFAULT_MAX_TOKENS(2048)로 변경
    """
    local_client = OpenAI(api_key=api_key)
    
    strategies = [
        ("gpt-4o-2024-08-06", TIMELINE_SCHEMA),
        ("gpt-4o-mini", TIMELINE_SCHEMA),
        ("gpt-4o", {"type": "json_object"})
    ]

    last_error = None
    
    for model, resp_format in strategies:
        retries = 0
        while retries <= 2:
            try:
                response = local_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    response_format=resp_format
                )
                content = response.choices[0].message.content
                data = safe_json_loads(content)
                if "messages" in data:
                    return data
                raise ValueError("JSON Key 'messages' not found")
            except (RateLimitError, APIConnectionError):
                retries += 1
                time.sleep(2 + random.random())
            except BadRequestError:
                # 스키마 미지원 등의 이유로 실패 시 다음 전략으로
                break
            except Exception as e:
                retries += 1
                last_error = e
                time.sleep(1)
    
    print(f"[API Failed] Last Error: {last_error}")
    return {"messages": []}

def transcribe_audio_chunk(file_path):
    last_error = None
    for attempt in range(3):
        try:
            with open(file_path, "rb") as f:
                return client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text",
                )
        except Exception as e:
            last_error = e
            time.sleep(1 * (attempt + 1))
    raise last_error

def calculate_similarity(s1, s2):
    if pd.isna(s1): s1 = ""
    if pd.isna(s2): s2 = ""
    return difflib.SequenceMatcher(None, str(s1), str(s2)).ratio() * 100

def normalize_date(d):
    if pd.isna(d): return ""
    return str(d).strip()[:10]

def evaluate_results(df_truth, df_ai):
    used_ai_indices = set()
    report_data = []
    total_score = 0.0
    matched_count = 0

    for i in range(len(df_truth)):
        truth_row = df_truth.iloc[i]
        best_idx = None
        best_sim = -1.0
        truth_content = truth_row.get("content", "")

        for idx, ai_row in df_ai.iterrows():
            if idx in used_ai_indices:
                continue
            sim = calculate_similarity(truth_content, ai_row.get("content", ""))
            if sim > best_sim:
                best_sim = sim
                best_idx = idx
       
        if best_idx is None or best_sim < 50.0:
            report_data.append({
                "ID": i+1, "상태": "❌ 미탐지", "정답내용": truth_content, "AI예측": "-", "점수": 0
            })
            continue

        used_ai_indices.add(best_idx)
        matched_count += 1
        ai_row = df_ai.loc[best_idx]
       
        content_score = best_sim
        date_match = normalize_date(truth_row.get("date")) == normalize_date(ai_row.get("date"))
        date_score = 100 if date_match else 0
        imp_match = str(truth_row.get("importance")) == str(ai_row.get("importance"))
        imp_score = 100 if imp_match else 0
        sender_score = calculate_similarity(truth_row.get("sender"), ai_row.get("sender"))

        final_score = (content_score * 0.5) + (date_score * 0.2) + (imp_score * 0.2) + (sender_score * 0.1)
        total_score += final_score

        report_data.append({
            "ID": i+1,
            "상태": "✅ 매칭됨",
            "정답내용": truth_content,
            "AI예측": ai_row.get("content"),
            "내용유사도": round(content_score, 1),
            "날짜일치": "O" if date_match else "X",
            "점수": round(final_score, 1),
        })

    avg_score = total_score / matched_count if matched_count > 0 else 0
    return avg_score, pd.DataFrame(report_data)

def encode_image(image_file):
    image_file.seek(0)
    return base64.b64encode(image_file.read()).decode('utf-8')

# =========================================================
# 안전한 임시 파일 처리 (Dependency Check 추가)
# =========================================================
def extract_audio_from_video(video_file):
    if not HAS_MOVIEPY:
        st.error("❌ 'moviepy' 라이브러리가 설치되지 않아 영상 분석을 수행할 수 없습니다.")
        return None

    tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    tfile.write(video_file.read())
    tfile.close()
    
    video_path = tfile.name
    audio_path = None
    
    try:
        video = mp.VideoFileClip(video_path)
        if video.audio is None:
            st.warning(f"🔇 '{video_file.name}' 영상에 오디오 트랙이 없습니다.")
            return None
            
        duration_ms = video.duration * 1000 if video.duration is not None else 0
        audio_clip = video.audio
        
        if duration_ms > MAX_MEDIA_MS:
            st.warning("🎬 영상이 너무 길어 앞부분 2시간만 분석합니다.")
            audio_clip = audio_clip.subclip(0, MAX_MEDIA_MS / 1000.0)
        
        afile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        afile.close()
        audio_path = afile.name
        
        audio_clip.write_audiofile(audio_path, logger=None)
        return audio_path

    except Exception as e:
        st.error(f"영상 변환 오류: {e}")
        return None
    finally:
        try:
            if 'video' in locals(): video.close()
            if 'audio_clip' in locals() and audio_clip: audio_clip.close()
        except: pass
        if os.path.exists(video_path):
            os.remove(video_path)

def process_audio_file(file_obj_or_path):
    """
    [수정] 하나의 긴 문자열이 아니라, 청크별 텍스트 리스트를 반환하도록 변경
    Returns: list[str]
    """
    if not HAS_PYDUB:
        st.error("❌ 'pydub' 라이브러리가 설치되지 않아 오디오 분석을 수행할 수 없습니다.")
        return []

    if isinstance(file_obj_or_path, str):
        file_path = file_obj_or_path
        should_cleanup_input = False
    else:
        tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tfile.write(file_obj_or_path.read())
        tfile.close()
        file_path = tfile.name
        should_cleanup_input = True

    transcript_chunks = []
    try:
        sound = AudioSegment.from_file(file_path)
        if len(sound) > MAX_MEDIA_MS:
            sound = sound[:MAX_MEDIA_MS]
        
        # 10분 단위로 자르기
        chunk_length_ms = 10 * 60 * 1000
        chunks = [sound[i:i + chunk_length_ms] for i in range(0, len(sound), chunk_length_ms)]
        
        for i, chunk in enumerate(chunks):
            cfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            cfile.close()
            chunk_path = cfile.name
            
            chunk.export(chunk_path, format="mp3")
            try:
                transcript = transcribe_audio_chunk(chunk_path)
                if transcript and transcript.strip():
                    transcript_chunks.append(transcript)
            finally:
                if os.path.exists(chunk_path): os.remove(chunk_path)
                
    except Exception as e:
        st.error(f"오디오 처리 오류: {e}")
    finally:
        if should_cleanup_input and os.path.exists(file_path):
            os.remove(file_path)
            
    return transcript_chunks

# =========================================================
# AI 분석 프롬프트 & 워커
# =========================================================
# =========================================================
# [수정 2/3] 병렬 순서 유지: batch_start_index를 워커에 전달
# =========================================================
def analyze_image_batch_worker(batch_data, api_key, batch_start_index: int):
    system_prompt = f"""
    당신은 법원에 제출할 증거자료를 분석하는 '디지털 포렌식 전문가'입니다.
    이 내용은 SNS 대화기록입니다.
    
    [핵심 원칙]
    1. 원문 유지: 대화 내용을 빠짐없이 전사하십시오. (날짜/시간 표시 포함) 오타, 비속어, 이모티콘 텍스트를 수정하지 말고 그대로 전사하십시오.
    2. 객관성: 추측성 내용은 배제하고 "[판독불가]"로 표기하십시오.
    3. 시간 정보: 이미지 내 시간 정보를 최우선으로 하되, 없거나 불명확하면 "미확인"으로 표기하고 is_estimated=true로 표시하십시오.
    4. 화면 상단이나 중간에 있는 "202x년 x월 x일" 같은 날짜 정보를 놓치지 마십시오.
    3. 시간(오전/오후) 정보가 보이면 24시간제로 변환하여 timestamp에 기록하십시오.
    4. 발신자(sender) 이름이 없으면 말풍선 위치(노란색: 나, 흰색: 상대방)를 보고 판단하여 '나' 또는 '상대방'으로 적으십시오.
    {COMMON_SCHEMA_INSTRUCTION}
    """
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [{"type": "text", "text": "이미지들을 분석하여 업로드된 순서를 해치지 않도록 JSON으로 반환하라. 날짜/시간이 없으면 미확인으로 둔다."}]}
    ]

    valid_files = []
    for fname, fbytes in batch_data:
        b64 = optimize_image_bytes(fbytes)
        if b64:
            messages[1]["content"].append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
            })
            valid_files.append(fname)

    if not valid_files:
        return [], [x[0] for x in batch_data]

    try:
        res = call_chat_json_robust(api_key, messages)
        items = []
        for j, item in enumerate(res.get("messages", [])):
            item = normalize_message_item(item)
            item['source'] = '스크린샷'
            item['filename'] = f"Batch_Start_{valid_files[0]}"
            if not item.get('context'): item['context'] = "메신저 대화"

            # 업로드 순서 기반 정렬을 위한 키(병렬 섞임 방지)
            # 배치 시작 인덱스 + 배치 내부 메시지 순번(소수점)으로 안정 정렬
            item['upload_index'] = float(batch_start_index) + (j / 1000.0)

            items.append(item)
        return items, []
    except Exception as e:
        print(f"Batch Worker Error: {e}")
        return [], [x[0] for x in batch_data]

def analyze_pdf_chunk(text_chunk, page_info):
    prompt = f"""
    법원 제출용 증거 문서를 분석하여 '입증 사실'을 JSON으로 추출하라.
    현재 분석 중인 부분: {page_info}
    
    [규칙]
    1. 문서에 명시된 날짜와 사건을 정확히 매칭하라.
    2. 핵심 문장을 요약 없이 발췌하라.
    3. 날짜/시간이 불명확하면 "미확인"으로 두고 is_estimated=true로 하라.
    
    {COMMON_SCHEMA_INSTRUCTION}
    
    [문서 텍스트 일부]
    {text_chunk}
    """
    return call_chat_json_robust(OPENAI_KEY, [{"role": "user", "content": prompt}])

def analyze_transcript_with_gpt(transcript_text, chunk_info=""):
    """
    [수정] chunk_info를 인자로 받아 프롬프트에 반영
    """
    prompt = f"""
    법원 제출용 녹취록을 작성하라.
    분석 구간: {chunk_info}
    
    [규칙]
    1. 발화 내용은 요약하지 말고 비속어, 추임새를 포함하여 그대로 전사하라.
    2. 화자가 불분명할 경우 '화자미상'으로 표기하라.
    3. 날짜/시간이 불명확하면 "미확인"으로 두고 is_estimated=true로 하라.
    
    {COMMON_SCHEMA_INSTRUCTION}
    
    [녹취록 텍스트]
    {transcript_text}
    """
    return call_chat_json_robust(OPENAI_KEY, [{"role": "user", "content": prompt}])

# =====================================
# 통합 분석 실행 함수 (Dependency Check & 병렬 처리)
# =====================================
def run_analysis(imgs, audio, video, pdf, plan_type="pro"):
    final_data = []

    # 요금제에 따른 이미지 제한 (지금은 Pro만 쓰는 구조라면 pro 고정)
    max_images = MAX_IMAGES_PRO if str(plan_type).lower().startswith("pro") else 20

    # 0) 이미지 제한
    if imgs and len(imgs) > max_images:
        st.warning(f"⚠️ 이미지가 많아 상위 {max_images}장만 분석합니다.")
        imgs = imgs[:max_images]

    # 1) 비디오 처리
    if video:
        if not HAS_MOVIEPY:
            st.error("🚫 서버에 'moviepy'가 설치되지 않아 영상 분석을 건너뜁니다.")
        else:
            with st.spinner("🎬 영상 처리 중..."):
                audio_path = extract_audio_from_video(video)
                if audio_path:
                    text_chunks = process_audio_file(audio_path)
                    if text_chunks:
                        total_chunks = len(text_chunks)
                        for i, chunk_text in enumerate(text_chunks):
                            chunk_info = f"Segment {i+1}/{total_chunks}"
                            data = analyze_transcript_with_gpt(chunk_text, chunk_info).get("messages", [])
                            for item in data:
                                item = normalize_message_item(item)
                                item["source"] = "영상파일"
                                item["filename"] = video.name
                                if not item.get("context"):
                                    item["context"] = "영상 녹취"
                            final_data.extend(data)

                    if os.path.exists(audio_path):
                        os.remove(audio_path)

    # 2) 오디오 처리
    if audio:
        if not HAS_PYDUB:
            st.error("🚫 서버에 'pydub'이 설치되지 않아 오디오 분석을 건너뜁니다.")
        else:
            with st.spinner("🎙️ 녹음 분석 중..."):
                text_chunks = process_audio_file(audio)
                if text_chunks:
                    total_chunks = len(text_chunks)
                    for i, chunk_text in enumerate(text_chunks):
                        chunk_info = f"Part {i+1}/{total_chunks}"
                        data = analyze_transcript_with_gpt(chunk_text, chunk_info).get("messages", [])
                        for item in data:
                            item = normalize_message_item(item)
                            item["source"] = "녹음파일"
                            item["filename"] = audio.name
                            if not item.get("context"):
                                item["context"] = "통화 녹음"
                        final_data.extend(data)
                else:
                    st.warning(f"⚠️ 녹음파일 '{audio.name}'에서 대화를 인식하지 못했습니다.")

    # 3) 이미지 처리 (병렬)
    if imgs:
        batch_size = DEFAULT_BATCH_SIZE
        total_files = len(imgs)
        batch_indices = range(0, total_files, batch_size)
        total_batches = len(batch_indices)

        pbar = st.progress(0)
        status = st.empty()

        max_concurrent_workers = 3

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent_workers) as executor:
            futures = set()
            file_pointer = 0

            while file_pointer < total_files or futures:
                while len(futures) < max_concurrent_workers and file_pointer < total_files:
                    current_batch_files = imgs[file_pointer:file_pointer + batch_size]
                    batch_data = []
                    for f in current_batch_files:
                        f.seek(0)
                        batch_data.append((f.name, f.read()))

                    batch_start_index = file_pointer
                    fut = executor.submit(analyze_image_batch_worker, batch_data, OPENAI_KEY, batch_start_index)
                    futures.add(fut)
                    file_pointer += batch_size

                if futures:
                    done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in done:
                        futures.remove(fut)
                        try:
                            res_data, _ = fut.result()
                            final_data.extend(res_data)
                        except Exception as e:
                            print(f"Worker Exception: {e}")

                    processed_batches = (file_pointer // batch_size) - len(futures)
                    processed_batches = max(processed_batches, 0)
                    progress_val = min(processed_batches / max(total_batches, 1), 1.0)
                    pbar.progress(progress_val)
                    status.text(f"📷 이미지 분석 중... ({processed_batches}/{total_batches} 배치)")

        pbar.empty()
        status.empty()

    # 4) PDF 처리
    if pdf:
        if not HAS_PYPDF:
            st.error("🚫 서버에 'pypdf'가 설치되지 않아 PDF 분석을 건너뜁니다.")
        else:
            with st.spinner("📚 PDF 분석 중..."):
                try:
                    pdf.seek(0)  # 중요: 포인터 복구
                    reader = PdfReader(pdf)
                    full_text = ""
                    for page in reader.pages:
                        full_text += (page.extract_text() or "")

                    if not full_text.strip() or len(full_text.strip()) < 50:
                        st.warning(f"⚠️ 문서 '{pdf.name}'에서 텍스트를 거의 추출하지 못했습니다. 스캔본(이미지)일 수 있습니다.")
                    else:
                        text_len = len(full_text)
                        chunk_size = MAX_PDF_CHUNK_SIZE
                        chunks = [full_text[i:i + chunk_size] for i in range(0, text_len, chunk_size)]
                        total_chunks = len(chunks)

                        progress_text = st.empty()
                        for i, chunk in enumerate(chunks):
                            progress_text.text(f"📚 PDF 분석 중... ({i+1}/{total_chunks} 구간)")
                            page_info = f"전체 {total_chunks}구간 중 {i+1}번째 구간"

                            data = analyze_pdf_chunk(chunk, page_info).get("messages", [])
                            for item in data:
                                item = normalize_message_item(item)
                                item["source"] = "PDF문서"
                                item["filename"] = pdf.name
                                if not item.get("context"):
                                    item["context"] = "문서 내용"
                            final_data.extend(data)

                        progress_text.empty()

                except Exception as e:
                    st.error(f"PDF 오류: {e}")

    return final_data


# =====================================
# 5) 증거 ZIP 로직
# =====================================
def get_image_taken_time(uploaded_file):
    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as img:
            exif_date = None
            exif = img.getexif()
            if exif:
                for tag_id in [36867, 36868, 306]:
                    val = exif.get(tag_id)
                    if val:
                        exif_date = val
                        break
            if exif_date:
                try:
                    return datetime.strptime(str(exif_date), "%Y:%m:%d %H:%M:%S")
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        uploaded_file.seek(0)
    return None

def process_evidence_images_optimized(sorted_items):
    failed_files = []
    zip_data = None

    with tempfile.TemporaryDirectory() as src_dir:
        img_dir = os.path.join(src_dir, "images")
        os.makedirs(img_dir, exist_ok=True)

        html_lines = [
            "<!DOCTYPE html>",
            "<html><body><h1>Evidence Timeline</h1><hr/>"
        ]

        total = len(sorted_items)
        pbar = st.progress(0)
        status = st.empty()

        for idx, item in enumerate(sorted_items, 1):
            f = item["file"]
            ts = item["taken_at"]

            status.text(f"📷 처리 중... ({idx}/{total})")
            pbar.progress(idx / total)

            out_filename = f"{idx:04d}.jpg"
            out_path = os.path.join(img_dir, out_filename)

            try:
                f.seek(0)
                is_heic = f.name.lower().endswith((".heic", ".heif"))
                needs_conversion = is_heic

                if not is_heic:
                    try:
                        with Image.open(f) as img:
                            exif = img.getexif()
                            if exif and exif.get(274) and exif.get(274) > 1:
                                needs_conversion = True
                            if img.format not in ["JPEG", "JPG"]:
                                needs_conversion = True
                    except Exception:
                        needs_conversion = True

                f.seek(0)
                if not needs_conversion:
                    with open(out_path, "wb") as dest:
                        shutil.copyfileobj(f, dest)
                else:
                    with Image.open(f) as img:
                        img = ImageOps.exif_transpose(img)
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        img.save(out_path, "JPEG", quality=85)

                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "판독불가"
                html_lines.append(
                    f"<div><b>#{idx}</b> {ts_str}<br>"
                    f"<img src='images/{out_filename}' loading='lazy'/></div><hr/>"
                )

            except Exception as e:
                print(f"Evidence process fail: {f.name}, err={e}")
                failed_files.append(f.name)
                html_lines.append(
                    f"<div><b>#{idx}</b> [실패] {f.name}</div><hr/>"
                )

        html_lines.append("</body></html>")
        with open(os.path.join(src_dir, "timeline.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))

        status.text("📦 ZIP 압축 중...")

        with tempfile.TemporaryDirectory() as out_dir:
            base_name = os.path.join(out_dir, "evidence_result")
            shutil.make_archive(base_name, "zip", src_dir)
            zip_path = base_name + ".zip"

            file_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            if file_size_mb > MAX_ZIP_SIZE_MB:
                st.error(
                    f"❌ 생성된 ZIP 파일이 너무 큽니다 ({file_size_mb:.1f}MB). "
                    f"서버 안정을 위해 다운로드를 제한합니다. (제한: {MAX_ZIP_SIZE_MB}MB)"
                )
                zip_data = None
            else:
                with open(zip_path, "rb") as f:
                    zip_data = f.read()

    pbar.empty()
    status.empty()
    return zip_data, failed_files

# =====================================
# 6) 로그인 / 회원가입 페이지
# =====================================
def login_page():
    bg_color = "#0E1117" if st.session_state['is_dark_mode'] else "#f0f2f6"
    text_color = "#FAFAFA" if st.session_state['is_dark_mode'] else "#000000"
    
    st.markdown(f"""
    <style>
        [data-testid="stHeader"] {{ display: none; }}
        [data-testid="stToolbar"] {{ visibility: hidden; }}
        .stApp {{ background-color: {bg_color}; color: {text_color}; }}
        [data-testid="stSidebar"] {{ display: none !important; }}

        div.stButton > button {{
            width: 100%; height: 50px; font-size: 16px; font-weight: bold; border-radius: 8px;
        }}
        
        div[data-testid="stForm"] {{
            border: 1px solid #d1d5db;
            padding: 20px;
            border-radius: 10px;
            background-color: transparent; 
        }}

        div[data-baseweb="input"] {{
            background-color: #ffffff !important;
            border: 1px solid #d1d5db !important;
        }}
        input[type="text"], input[type="password"] {{
            background-color: #ffffff !important;
            color: #000000 !important;
            -webkit-text-fill-color: #000000 !important;
        }}
        label[data-testid="stLabel"] {{
            color: {text_color} !important;
            font-weight: bold !important;
        }}
    </style>
    """, unsafe_allow_html=True)

    top_col1, top_col2 = st.columns([8, 1])
    with top_col2:
        mode = st.toggle("🌙", value=st.session_state['is_dark_mode'], key="login_toggle")
        if mode != st.session_state['is_dark_mode']:
            st.session_state['is_dark_mode'] = mode
            st.rerun()

    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        st.markdown("<br><h1 style='text-align: center;'>⚖️ Timeline.Ai</h1>", unsafe_allow_html=True)
        st.markdown(f"<p style='text-align: center; color: {text_color};'>법적 증거 통합 분석 시스템</p><hr>", unsafe_allow_html=True)

        if st.session_state["auth_mode"] == "login":
            st.subheader("로그인")
            with st.form("login_form"):
                email = st.text_input("이메일")
                pw = st.text_input("비밀번호", type="password")
                st.markdown("<br>", unsafe_allow_html=True)
                login_submit = st.form_submit_button("로그인", type="primary", use_container_width=True)
            
            if login_submit:
                try:
                    res = supabase.auth.sign_in_with_password({"email": email, "password": pw})
                    if res.user:
                        st.session_state["user"] = {"id": res.user.id, "email": res.user.email}
                        st.success("로그인 성공!")
                        st.rerun()
                except Exception as e:
                    st.error(f"로그인 실패: {e}")
            
            st.write("") 
            if st.button("회원가입", use_container_width=True):
                st.session_state["auth_mode"] = "signup"
                st.rerun()

        elif st.session_state["auth_mode"] == "signup":
            st.subheader("회원가입")
            tab_email, tab_google = st.tabs(["📧 이메일", "🌐 Google"])
            
            with tab_email:
                with st.form("signup_form"):
                    new_email = st.text_input("이메일")
                    new_pw = st.text_input("비밀번호 (6자 이상)", type="password")
                    new_pw2 = st.text_input("비밀번호 확인", type="password")
                    st.markdown("<br>", unsafe_allow_html=True)
                    signup_submit = st.form_submit_button("회원가입", type="primary", use_container_width=True)

                if signup_submit:
                    if new_pw != new_pw2:
                        st.error("비밀번호가 일치하지 않습니다.")
                    elif len(new_pw) < 6:
                        st.error("비밀번호는 6자리 이상이어야 합니다.")
                    else:
                        try:
                            supabase.auth.sign_up({"email": new_email, "password": new_pw})
                            st.success("✅ 가입 메일 전송! 이메일 인증 후 로그인하세요.")
                        except Exception as e:
                            st.error(f"가입 실패: {e}")

            with tab_google:
                st.info("Google 계정으로 간편 가입/로그인")
                if st.button("Google로 계속하기", key="btn_google_join", use_container_width=True):
                    try:
                        res = supabase.auth.sign_in_with_oauth({
                            "provider": "google",
                            "options": {"redirect_to": REDIRECT_URL}
                        })
                        auth_url = getattr(res, "url", None) or getattr(res, "redirect_to", None)
                        if auth_url:
                            st.markdown(f'<a href="{auth_url}" target="_self">👉 Google 로그인 창 열기</a>', unsafe_allow_html=True)
                    except Exception as e:
                        st.error(f"Google 인증 오류: {e}")

            st.markdown("---")
            if st.button("⬅️ 뒤로가기", use_container_width=True):
                st.session_state["auth_mode"] = "login"
                st.rerun()

# =====================================
# 7) 메인 앱 화면 (Nuclear Option: 토글 강제 삭제 + 색상 강제 주입)
# =====================================
def main_app():
    # 1. 색상 정의 (다크모드/라이트모드에 따른 텍스트 색상 변수 설정)
    if st.session_state['is_dark_mode']:
        bg_color = "#0E1117"
        text_color = "#FAFAFA"  # 다크모드일 때 메인 글씨는 흰색
        sidebar_bg = "#262730"
    else:
        bg_color = "#FFFFFF"
        text_color = "#000000"  # 라이트모드일 때 메인 글씨는 검정
        sidebar_bg = "#F0F2F6"

    # 2. 강력한 CSS 스타일 주입
    st.markdown(f"""
    <style>
    /* [1] 사이드바 토글 및 헤더 숨김 */
    [data-testid="stSidebarCollapsedControl"],
    section[data-testid="stSidebar"] > div > div > button,
    [data-testid="stHeader"],
    [data-testid="stToolbar"],
    footer {{
        display: none !important;
    }}

    /* [2] 앱 기본 테마 (배경 및 기본 글자색) */
    .stApp {{ background-color: {bg_color}; }}
    
    /* 기본 텍스트들은 변수(text_color)를 따라감 -> 다크모드면 흰색 */
    h1, h2, h3, h4, h5, h6, p, li, span, div {{ 
        color: {text_color}; 
    }}

    /* [3] 사이드바 배경 */
    [data-testid="stSidebar"] {{
        background-color: {sidebar_bg} !important;
    }}
    /* 사이드바 기본 텍스트도 테마 색상 따라감 */
    [data-testid="stSidebar"] p, 
    [data-testid="stSidebar"] span, 
    [data-testid="stSidebar"] div {{
        color: {text_color};
    }}

    /* [4] ★★★ 이메일 박스 전용 (여기는 무조건 검정색 고정) ★★★ */
    .custom-email-box {{
        background-color: #FFFFFF !important; /* 흰색 배경 */
        padding: 15px !important;
        border-radius: 10px !important;
        border: 1px solid #ddd !important;
        text-align: center !important;
        margin-bottom: 20px !important;
    }}
    /* 이메일 박스 안의 모든 요소는 강제로 검정(#000000) */
    .custom-email-box * {{
        color: #000000 !important;
        -webkit-text-fill-color: #000000 !important;
        font-weight: bold !important;
    }}

    /* [5] ★★★ 파일 업로더 스타일링 분리 ★★★ */
    
    /* (A) 라벨 (ex: 1. 스크린샷, 2. 증거 문서) -> 테마 색상(text_color) 따름 */
    label[data-testid="stWidgetLabel"],
    label[data-testid="stWidgetLabel"] p,
    label[data-testid="stWidgetLabel"] span {{
        color: {text_color} !important; /* 다크모드: 흰색, 라이트: 검정 */
    }}

    /* (B) 드롭존 내부 (Drag and drop...) -> 배경이 흰색이므로 무조건 검정 */
    [data-testid="stFileUploaderDropzone"] {{
        background-color: #FFFFFF !important; 
    }}
    [data-testid="stFileUploaderDropzone"] small,
    [data-testid="stFileUploaderDropzone"] span,
    [data-testid="stFileUploaderDropzone"] div {{
        color: #000000 !important; /* 여기는 무조건 검정 */
        -webkit-text-fill-color: #000000 !important;
    }}
    
    /* (C) 업로드 버튼 (Browse files) */
    [data-testid="stFileUploader"] button {{
        background-color: #FFFFFF !important; 
        color: #000000 !important;
        border: 1px solid #d1d5db !important;
    }}

    /* [6] 사이드바 버튼 등 기타 */
    [data-testid="stSidebar"] .stButton button {{
        background-color: #FFFFFF !important;
        border: 1px solid #d1d5db !important;
    }}
    [data-testid="stSidebar"] .stButton button p {{
        color: #000000 !important; 
    }}
    </style>
    """, unsafe_allow_html=True)

    # --- 사이드바 영역 ---
    with st.sidebar:
        st.title("⚖️ Timeline.Ai")
        st.markdown("---")
        
        user_email = st.session_state['user']['email'] if st.session_state['user'] else "GUEST"
        
        st.markdown(f"""
        <div class="custom-email-box">
            <span style="font-size: 20px;">👋</span><br>
            <b style="font-size: 16px;">{user_email}</b>
            <span> 님</span>
        </div>
        """, unsafe_allow_html=True)

        st.write("⚙️ **설정**")
        mode = st.toggle("🌙 다크 모드", value=st.session_state['is_dark_mode'], key="sidebar_dark_mode")
        if mode != st.session_state['is_dark_mode']:
            st.session_state['is_dark_mode'] = mode
            st.rerun()

        st.header("요금제")
        st.info("Pro 요금제: 월 59,900원")
        plan_code = "pro"


        st.markdown("---")
        if st.button("🗑️ 분석 초기화", use_container_width=True):
            st.session_state.result_data = []
            st.rerun()

        if st.button("로그아웃", use_container_width=True):
            supabase.auth.sign_out()
            st.session_state["user"] = None
            st.session_state["auth_mode"] = "login"
            st.rerun()

    # --- 메인 컨텐츠 영역 ---
    st.title("⚖️ 타임라인 보고서 (Timeline.Ai)")
    st.subheader("법적 증거 통합 분석 시스템")

    with st.expander("ℹ️ 사용 가이드 및 주의사항 보기 (Click)"):
        st.write("""
                 **개인정보들은 절대 데이터에 남거나 학습되지 않습니다**
        1. **SNS 캡처**: 날짜와 시간이 잘 보이게 찍어주세요.
        2. **녹음 파일**: MP3, M4A 형식을 지원하며 1시간 이내 파일만 가능합니다.
        3. **주의사항**: 본 결과물은 법적 효력이 없는 **'초안(Draft)'**입니다. 최종 제출 전 반드시 원본과 대조하세요.
        4. **SNS 이미지**는 시간순서별로 올려주시길 바랍니다. 과거>최신순서.
        5. **증거자료가 대용량인 경우에는 2번~3번 나누어 올려주시길 바랍니다.** 예) 첫번째:이미지+PDF 두번째:녹음파일
        6. **PDF파일**에 이미지가 담겨있는 경우, 이미지는 분석이 되지 않습니다. 텍스트 이미지는 SNS텍스트 분석이나, 증거이미지인 경우엔 ZIP칸에 올려주세요
        """)

    tab1, tab2, tab3 = st.tabs(["📂 1. 증거 업로드", "📊 2. 분석 결과 확인", "🧾 3. 증거이미지 타임라인 ZIP 생성"])

    with tab1:
        imgs_in, audio_in, video_in, pdf_in = None, None, None, None

        st.success("💎 **Pro**: 모든 기능 무제한 (녹음/영상 포함)")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### 📷 이미지 / 🎤 녹음")
            imgs_in = st.file_uploader("1. SNS 스크린샷", type=['png', 'jpg', 'jpeg', 'heic'], accept_multiple_files=True, key="p_img")
            audio_in = st.file_uploader("3. 녹음 파일", type=['mp3', 'm4a', 'wav'], key="p_audio")

        with c2:
            st.markdown("### 📄 문서 / 🎬 영상")
            pdf_in = st.file_uploader("2. 증거 문서", type=['pdf'], key="p_pdf")
            video_in = st.file_uploader("4. 영상 파일", type=['mp4', 'avi'], key="p_video")

        st.write("")
        if st.button("통합 분석 시작 🚀", type="primary"):
            if not any([imgs_in, audio_in, video_in, pdf_in]):
                st.warning("파일을 하나라도 올려주세요.")
            else:
                res = run_analysis(imgs_in, audio_in, video_in, pdf_in, plan_code)
                st.session_state.result_data = res
                if res:
                    st.toast('분석 완료! 결과 탭을 확인하세요.', icon='✅')
                else:
                    st.toast('분석 결과가 없습니다.', icon='⚠️')


    with tab2:
        if 'result_data' in st.session_state and st.session_state.result_data:
            df = pd.DataFrame(st.session_state.result_data)
            if 'ID' not in df.columns: df.insert(0, 'ID', range(1, 1 + len(df)))

            # =========================================================
            # [수정 3/3] timestamp 기반 정렬 제거
            # - 대신 upload_index가 있으면 업로드 순서로 정렬
            # =========================================================
            if 'upload_index' in df.columns:
                df = df.sort_values(by='upload_index', na_position='last')
                df = df.drop(columns=['upload_index'])

            req_cols = ['ID', 'timestamp', 'date', 'time', 'context', 'sender', 'content', 'importance', 'source', 'link', 'filename', 'is_estimated']
            for c in req_cols:
                if c not in df.columns: df[c] = ""
            df = df[req_cols]

            st.subheader("📊 최종 분석 리포트")
            sub1, sub2 = st.tabs(["📋 결과 엑셀", "💯 정확도 검증"])
            
            with sub1:
                st.success("✅ 분석 완료! 아래 내용을 확인하고 수정하세요.")
                edited_df = st.data_editor(
                    df,
                    num_rows="dynamic", use_container_width=True,
                    column_config={
                        "is_estimated": st.column_config.CheckboxColumn("날짜추정"),
                        "importance": st.column_config.SelectboxColumn("중요도", options=["상", "중", "하", "미상"]),
                    },
                )

                if HAS_OPENPYXL:
                    export_buffer = io.BytesIO()
                    with pd.ExcelWriter(export_buffer, engine='openpyxl') as writer:
                        edited_df.to_excel(writer, sheet_name='전체 타임라인', index=False)
                        if 'source' in edited_df.columns:
                            unique_sources = edited_df['source'].unique()
                            for src in unique_sources:
                                if pd.isna(src) or src == "": safe_name = "기타"
                                else: safe_name = str(src).replace("/", "_").replace("\\", "_")[:30]
                                subset_df = edited_df[edited_df['source'] == src]
                                if not subset_df.empty:
                                    subset_df.to_excel(writer, sheet_name=safe_name, index=False)
                    
                    st.download_button(
                        label="📥 엑셀 다운로드 (시트 분리됨)",
                        data=export_buffer.getvalue(),
                        file_name="증거_타임라인_분석.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.warning("⚠️ 엑셀 저장 라이브러리(openpyxl)가 없어 CSV로 다운로드합니다.")
                    st.download_button(
                        label="📥 CSV 다운로드",
                        data=edited_df.to_csv(index=False).encode('utf-8-sig'),
                        file_name="증거_타임라인_분석.csv",
                        mime="text/csv",
                        use_container_width=True
                    )

            with sub2:
                st.info("🧐 **정답지(Ground Truth)를 업로드하면 AI 점수를 즉시 계산합니다.**")
                upl_truth = st.file_uploader("📂 정답 엑셀 파일 업로드", type=['xlsx'], key="truth_up")
                if upl_truth:
                    try:
                        df_truth = pd.read_excel(upl_truth)
                        score, rpt_df = evaluate_results(df_truth, df)
                        st.metric(label="🏆 AI 종합 정확도", value=f"{score:.1f}점")
                        st.dataframe(rpt_df, use_container_width=True)
                    except Exception as e: st.error(f"정답 파일 오류: {e}")
        else:
            st.info("👈 '증거 업로드' 탭에서 분석을 시작하면 여기에 결과가 표시됩니다.")

    with tab3:
        st.subheader("🧾 증거 이미지 ZIP 생성")
        st.info("이미지들의 EXIF 정보(촬영일)를 기준으로 자동 정렬하고, HTML 리포트와 함께 압축합니다.")
        
        e_imgs = st.file_uploader(
            "증거용 원본 이미지 업로드 (JPG, PNG, HEIC)",
            accept_multiple_files=True,
            type=["jpg", "png", "heic", "jpeg"],
            key="evi_zip"
        )
        
        if st.button("ZIP 생성 시작", type="primary", key="btn_zip"):
            if not e_imgs:
                st.warning("파일을 업로드해주세요.")
            else:
                items = []
                for f in e_imgs:
                    items.append({
                        "file": f,
                        "taken_at": get_image_taken_time(f)
                    })

                items.sort(
                    key=lambda x: (x["taken_at"] if x["taken_at"] else datetime.max)
                )

                zip_bytes, fails = process_evidence_images_optimized(items)
                if zip_bytes:
                    st.success("✅ ZIP 파일 생성 완료!")
                    st.download_button(
                        "📥 Evidence.zip 다운로드",
                        zip_bytes,
                        "evidence.zip",
                        "application/zip",
                        use_container_width=True
                    )
                if fails:
                    st.error(f"⚠️ {len(fails)}개 파일 처리 실패 (손상되었거나 지원하지 않는 형식)")

# =====================================
# 8) 실행 흐름 제어 및 세션 복구 (Auth Check)
# =====================================
if st.session_state["user"] is None:
    try:
        session = supabase.auth.get_session()
        if session and session.user:
            st.session_state["user"] = {"id": session.user.id, "email": session.user.email}
    except Exception:
        pass

if st.session_state["user"] is None:
    login_page()
else:
    main_app()
