# 정답 기록 (사용자 확인 사실)

실제 영상에 대해 플레이어(사용자)가 확인해 준 사실. 분석 결과를 이 기록과 비교해 개선 여부를 확인한다.
영상 파일은 저장소에 두지 않으므로 `video_sha256` 앞 12자(= 기본 run id 접두어)로 영상을 식별한다.

    python -m owcoach evaluate runs/<run_id> eval/<file>.yaml
