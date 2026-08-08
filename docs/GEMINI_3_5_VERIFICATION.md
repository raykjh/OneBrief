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

최초 최소 호출은 다음 항목을 증명했다.

- 현재 인증 계정에서 Vertex AI를 호출할 수 있다.
- `gemini-3.5-flash` 모델에 접근할 수 있다.
- OneBrief가 사용할 Google GenAI SDK가 정상 동작한다.

이후 OneBrief 통합 검증에서 Google ADK 2.6.2의 구조화된 요구사항 분석가와
제작자 → 독립 검증자 → 원 제작자 수정 루프를 실행했다. Cloud Run Job과
Cloud Storage 비동기 왕복, 전용 서비스 계정, 결과 패키지 무결성도 검증했다.
Firestore는 현재 제품에 필요하지 않아 사용하지 않으며 필수 구성요소로 주장하지 않는다.

최신 ADK 소프트웨어 수렴 버전의 Cloud Run 재배포와 실제 Gemini 포함 원격 실행은
별도의 배포 증거로 갱신한다.

## 보안

- API 키를 사용하거나 저장하지 않았다.
- Application Default Credentials를 사용했다.
- 인증 토큰과 크레딧 코드를 출력 파일에 기록하지 않았다.
