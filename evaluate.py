import pandas as pd
import difflib  # 텍스트 유사도 비교 도구

# --- 설정: 기본 비교 대상 파일 이름 (원하면 함수 인자로도 받을 수 있음) ---
TRUTH_FILE = "truth.xlsx"      # 사람이 검수한 정답 엑셀 (수정본)
AI_FILE = "ai_result.xlsx"     # AI가 방금 분석한 엑셀 (원본)

# 매칭 기준 최소 유사도 (이 점수 미만이면 매칭 실패로 간주)
MIN_MATCH_SIMILARITY = 50.0

# 가중치 설정
WEIGHT_CONTENT = 0.5    # 내용 (50%)
WEIGHT_DATE = 0.2       # 날짜 (20%)
WEIGHT_IMPORTANCE = 0.2 # 중요도 (20%)
WEIGHT_SENDER = 0.1     # 화자 (10%)


def calculate_similarity(s1, s2) -> float:
    """두 문장의 유사도를 0~100점 사이로 반환"""
    if pd.isna(s1):
        s1 = ""
    if pd.isna(s2):
        s2 = ""
    return difflib.SequenceMatcher(None, str(s1), str(s2)).ratio() * 100


def normalize_date(d) -> str:
    """
    날짜 비교용 정규화 함수
    - datetime, 문자열 등 모두 'YYYY-MM-DD' 형식 앞 10글자만 사용
    - NaN, None 등은 빈 문자열로 처리
    """
    if pd.isna(d):
        return ""
    return str(d).strip()[:10]


def find_best_match_for_row(truth_row: pd.Series,
                            df_ai: pd.DataFrame,
                            used_ai_indices: set):
    """
    정답 행(truth_row)과 가장 비슷한 AI 행 찾기
    - 이미 매칭된 AI 행은 건너뜀
    - content 기준 유사도 상위 1개 선택
    - MIN_MATCH_SIMILARITY 미만이면 매칭 실패 처리
    """
    best_idx = None
    best_sim = -1.0
    truth_content = truth_row.get("content", "")

    for idx, ai_row in df_ai.iterrows():
        if idx in used_ai_indices:
            continue  # 이미 매칭된 건 패스

        ai_content = ai_row.get("content", "")
        sim = calculate_similarity(truth_content, ai_content)

        if sim > best_sim:
            best_sim = sim
            best_idx = idx

    # 유사도가 너무 낮으면 매칭 안 함
    if best_idx is None or best_sim < MIN_MATCH_SIMILARITY:
        return None, best_sim

    return best_idx, best_sim


def evaluate_performance(truth_file: str = TRUTH_FILE,
                         ai_file: str = AI_FILE,
                         output_file: str = "정확도_평가_리포트.xlsx"):
    """
    truth_file vs ai_file 엑셀을 비교하여 정확도를 평가하고,
    상세/요약 시트를 가진 리포트 엑셀(output_file)을 생성한다.
    """
    print(f"🔍 평가 시작: {truth_file} vs {ai_file}")

    # 1. 엑셀 불러오기
    try:
        df_truth = pd.read_excel(truth_file)
        df_ai = pd.read_excel(ai_file)
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {e}\n   → 파일 이름/경로/엑셀 형식(xlsx) 확인 필요")
        return

    if df_truth.empty:
        print("⚠️ 경고: 정답(truth) 엑셀이 비어 있습니다. 평가를 진행할 수 없습니다.")
        return

    print(f"📂 정답 데이터: {len(df_truth)}개 / AI 데이터: {len(df_ai)}개")

    used_ai_indices = set()
    detail_rows = []
    total_score = 0.0
    matched_count = 0

    # 2. 채점 루프
    for i in range(len(df_truth)):
        truth_row = df_truth.iloc[i]
        row_id = i + 1

        best_ai_idx, best_sim = find_best_match_for_row(truth_row, df_ai, used_ai_indices)

        # 매칭 실패 (AI가 이 정답 메시지를 못 맞춤)
        if best_ai_idx is None:
            detail_rows.append({
                "ID": row_id,
                "매칭상태": "❌ 미탐지",
                "정답_내용": truth_row.get("content"),
                "AI_내용": "-",
                "내용_유사도": 0.0,
                "날짜_일치": "X",
                "중요도_일치": "X",
                "화자_유사도": 0.0,
                "최종_점수": 0.0,
            })
            continue

        # 매칭 성공
        ai_row = df_ai.loc[best_ai_idx]
        used_ai_indices.add(best_ai_idx)
        matched_count += 1

        # --- 점수 계산 ---
        # 1) 내용 점수
        content_score = best_sim  # 0~100

        # 2) 날짜 점수 (normalize_date 적용)
        date_truth = normalize_date(truth_row.get("date"))
        date_ai = normalize_date(ai_row.get("date"))
        date_match = (date_truth == date_ai)
        date_score = 100.0 if date_match else 0.0

        # 3) 중요도 점수
        gt_imp = str(truth_row.get("importance")).strip()
        ai_imp = str(ai_row.get("importance")).strip()
        imp_match = (gt_imp == ai_imp)
        imp_score = 100.0 if imp_match else 0.0

        # 4) 화자 점수 (이름이 살짝 달라도 유사도 기반으로 평가)
        sender_score = calculate_similarity(truth_row.get("sender"), ai_row.get("sender"))

        # 최종 가중치 합산
        final_row_score = (
            content_score * WEIGHT_CONTENT +
            date_score * WEIGHT_DATE +
            imp_score * WEIGHT_IMPORTANCE +
            sender_score * WEIGHT_SENDER
        )
        total_score += final_row_score

        detail_rows.append({
            "ID": row_id,
            "매칭상태": f"✅ 매칭됨 (AI idx={best_ai_idx})",
            "정답_내용": truth_row.get("content"),
            "AI_내용": ai_row.get("content"),
            "내용_유사도": round(content_score, 1),
            "날짜_일치": "O" if date_match else "X",
            "중요도_일치": "O" if imp_match else "X",
            "화자_유사도": round(sender_score, 1),
            "최종_점수": round(final_row_score, 1),
        })

    # 3. 결과 요약
    unmatched_ai_count = len(df_ai) - len(used_ai_indices)
    avg_score = total_score / matched_count if matched_count > 0 else 0.0
    coverage = matched_count / len(df_truth) * 100 if len(df_truth) > 0 else 0.0

    print("\n------------------------------------------------")
    print(f"📊 평균 정확도: {avg_score:.2f}점 (0~100)")
    print(f"🎯 정답 매칭률: {coverage:.1f}% ({matched_count}/{len(df_truth)})")
    print(f"⚠️ 매칭 안 된 AI 행(환각 가능성): {unmatched_ai_count}개")
    print("------------------------------------------------")

    # 4. 엑셀 저장
    df_detail = pd.DataFrame(detail_rows)
    df_summary = pd.DataFrame([
        {"항목": "평균 정확도", "값": f"{avg_score:.2f}점"},
        {"항목": "매칭 성공률", "값": f"{coverage:.1f}%"},
        {"항목": "AI 환각(매칭 안 된 AI 행) 개수", "값": unmatched_ai_count},
        {"항목": "내용 가중치", "값": WEIGHT_CONTENT},
        {"항목": "날짜 가중치", "값": WEIGHT_DATE},
        {"항목": "중요도 가중치", "값": WEIGHT_IMPORTANCE},
        {"항목": "화자 가중치", "값": WEIGHT_SENDER},
    ])

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df_detail.to_excel(writer, sheet_name="상세", index=False)
        df_summary.to_excel(writer, sheet_name="요약", index=False)

    print(f"📂 '{output_file}' 저장 완료!")


if __name__ == "__main__":
    evaluate_performance()
