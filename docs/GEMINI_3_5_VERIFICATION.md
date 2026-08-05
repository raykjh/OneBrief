# Gemini 3.5 Minimal Verification

검증일: 2026-08-05

## 결과

- 상태: PASS
- 모델: `gemini-3.5-flash`
- 접근 방식: Google GenAI SDK 2.16.0을 통한 Vertex AI
- 위치: `global`
- Google Cloud 프로젝트: `ares-agentic-cinema-20260729`
- 응답: `ONEBRIEF_OK`

## 토큰 사용량

- 입력: 12 tokens
- 출력: 5 tokens
- 합계: 17 tokens
- 트래픽: On-demand

공식 목록 가격의 입력·출력 단가를 단순 적용한 추정비용은 약 `$0.000063`이다. 실제 청구액은 결제 계정의 크레딧, 반올림, 지역과 당시 가격 정책에 따라 달라질 수 있다.

## 검증 범위

이 검증은 다음 항목만 증명한다.

- 현재 인증 계정에서 Vertex AI를 호출할 수 있다.
- `gemini-3.5-flash` 모델에 접근할 수 있다.
- OneBrief가 사용할 Google GenAI SDK가 정상 동작한다.

Google ADK 연결, 구조화 출력, 도구 호출, 장문 입력, Firestore와 Cloud Run은 아직 검증하지 않았다.

## 보안

- API 키를 사용하거나 저장하지 않았다.
- Application Default Credentials를 사용했다.
- 인증 토큰과 크레딧 코드를 출력 파일에 기록하지 않았다.
