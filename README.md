# 인도 eGazette 모니터링

인도 정부 eGazette에서 지정한 부처의 신규 공보를 30분마다 확인해,
설정한 키워드에 매칭되면 **PDF를 첨부한** 알림 메일을 Gmail로 보냅니다.
GitHub Actions의 **Public 저장소**로 운영 → 완전 무료·무제한.

## 동작 방식
- 감시 부처: 기본 Commerce and Industry (여러 부처 동시 감시 가능)
- 감시 키워드: 기본 "copper"
- 매칭된 공보의 **PDF를 자동 다운로드해 메일에 첨부** (클릭 없이 바로 확인)
- 첨부 총합이 커지면(20MB 초과) 초과분은 PDF 직접 링크로 대체
- 첫 실행부터 현재 떠 있는 매칭 공보를 즉시 알림
- 한 번 알린 공보는 다시 알리지 않음 (state.json으로 기억)
- 월 경계 대비: 현재 월 + 직전 월 함께 조회
- 검색 결과가 여러 페이지여도 전체 순회

## PDF는 저장소에 쌓이지 않습니다
PDF는 실행 중 러너(일회용 클라우드 컨테이너)에 잠깐 받았다가 메일 발송 후
러너와 함께 폐기됩니다. 저장소에 커밋되는 건 state.json(수 KB 텍스트)뿐이라
저장공간 걱정이 없습니다.

## 설치 (10분)

### 1. GitHub 저장소 생성 (반드시 Public)
- 새 저장소를 **Public**으로 생성
- 이 폴더 파일들을 그대로 업로드:
  monitor.py, requirements.txt, state.json, README.md,
  .github/workflows/monitor.yml

> 안전: Gmail 비밀번호는 코드가 아니라 GitHub Secrets에 암호화 저장됩니다.
> 저장소가 Public이어도 노출되지 않으며, 코드에는 민감정보가 없습니다.

### 2. Gmail 앱 비밀번호 발급
- Google 계정 → 보안 → 2단계 인증 켜기
- 2단계 인증 → 앱 비밀번호 → 새로 생성 (16자리)
- 이 16자리를 GMAIL_APP_PASSWORD로 사용 (일반 로그인 비번 아님)

### 3. GitHub Secrets 등록
저장소 → Settings → Secrets and variables → Actions → New repository secret

| 이름 | 값 |
|------|-----|
| GMAIL_USER | 보내는 gmail 주소 |
| GMAIL_APP_PASSWORD | 발급한 16자리 앱 비밀번호 |
| ALERT_TO | 알림 받을 주소 (미설정 시 GMAIL_USER로 발송) |

### 4. 동작 확인
- Actions 탭 → "eGazette Monitor" → Run workflow (수동 실행)
- 초록 체크가 뜨고, 현재 떠 있는 copper 공보가 있으면 PDF 첨부 메일이 옵니다.

## 설정 수정 (monitor.py 상단 "설정 구역"만)

### 키워드
```python
KEYWORDS = ["copper", "brass"]
```

### 감시 부처 (여러 개 가능)
```python
MINISTRIES = {
    "9": "Commerce and Industry",
    "34": "Steel",
    "83": "Bureau of Indian Standards",
}
```
부처 번호 참조표는 monitor.py 주석 참고.

### PDF 첨부 끄기 (링크만 받기)
```python
ATTACH_PDF = False
```

## 참고
- 무료 러너 특성상 30분 주기가 몇 분~수십 분 밀릴 수 있음
- PDF URL은 페이지에서 자동 탐지 → 실패 시 링크로 안전하게 폴백
- 사이트/PDF 조회 실패 시 Actions가 빨간 X로 표시되어 즉시 인지 가능
- state.json은 자동 관리되므로 직접 건드리지 마세요
