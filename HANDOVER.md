# 🪟 WINDO (AI 스마트 가상 창문) 프로젝트 완벽 인수인계서

> **문서 목적**: 본 문서는 **WINDO(AI Virtual Window)** 프로젝트의 전체 아키텍처, 구현 내역, 주요 비즈니스/디자인 규칙, 핵심 모듈 및 다음 작업자가 반드시 준수해야 하는 모든 지침을 상세히 정리한 인수인계서입니다.  
> 다른 계정이나 새로운 AI 세션에서 작업을 이어받을 때 본 문서 전체를 프롬프트로 복사·붙여넣기하여 즉시 동일한 컨텍스트를 동기화할 수 있습니다.

---

## 1. 📌 프로젝트 개요 및 비전

### 1-1. 프로젝트 정의
- **프로젝트명**: WINDO (윈도 - AI 기반 맞춤형 가상 스마트 창문 시스템)
- **핵심 목표**: 실제 창문이 없거나 뷰가 열악한 공간(지하, 오피스, 원룸 등)에 `100vw × 100vh` 전체화면으로 현실감 넘치는 풍경을 투영하고, 생체리듬 자연광 조명 제어, 실시간 환경/일정 HUD, 음성 AI 인식 및 생성형 AI 풍경 생성을 융합한 스마트 홈 솔루션.

### 1-2. 기술 스택 (Tech Stack)
- **Backend & Web Framework**: Python 3.12, FastHTML (FastAPI 기반 고성능 하이퍼미디어 프레임워크), HTMX (실시간 비동기 DOM 갱신, SSE/Polling)
- **Deep Learning & Image Generation**: PyTorch (CUDA RTX 5080 가속), Hugging Face Diffusers (FLUX.1-schnell / SDXL), 7-Node ComfyUI 파이프라인 에뮬레이터
- **Image Processing**: Pillow (PIL) - 16:9 와이드(1920×1080) 스마트 센터 크롭(Center-Crop) 및 Lanczos 초고화질 리샘플링
- **Voice / Speech**:
  - STT: Vosk / Whisper 기반 한국어 음성 인식
  - TTS: pyttsx3 (한국어 음성 피드백)
- **Web Search Engine**: Searlo 공식 이미지 검색 API (`https://api.searlo.tech/api/v1/search/images`), SerpApi, Google Custom Search, Bing Images, Unsplash NAPI, DuckDuckGo
- **Frontend & Design**: 순수 HTML/CSS 글래스모피즘(Glassmorphism), Pretendard 폰트, 반응형 오버레이 도크

---

## 2. 🚀 여태까지 수정 및 고도화된 전체 작업 내역 (History)

### Phase 1: ComfyUI 7-노드 모니터링 & 순수 실사 풍경 생성
1. **100vw × 100vh 전체화면 뷰포트**:
   - 하단 UI 바를 완전히 제거하고 순수 창문 풍경만 전체화면에 표시.
   - 풍경 전환 시 부드러운 1.6초 크로스페이드(Crossfade) 트랜지션 적용.
2. **ComfyUI 7-노드 파이프라인 시각화**:
   - `CheckpointLoaderSimple` → `LoraLoader` → `CLIPTextEncode (Positive)` → `CLIPTextEncode (Negative)` → `EmptyLatentImage` → `KSampler` → `VAEDecode & SaveImage`의 7단계 노드 실행 상태와 그래프를 모니터링 드로어에 실시간 렌더링.
3. **순수 실사 풍경 프롬프트 엔지니어링**:
   - 실사 사진(Photorealistic) 강화를 위해 사람, 캐릭터, 인물, 실내 창틀 왜곡, 텍스트 워터마크 등을 원천 배제하는 네거티브 프롬프트 및 광학 디테일 강화.

### Phase 2: 실시간 스마트 글래스 정보 창(HUD)
1. **시간·날짜 & 기온·습도**:
   - 대형 시계(300px급 미니멀 폰트), 한글 요일/날짜 표기.
   - 현재 기온, 날씨 상태, 습도, 체감온도 표시.
2. **24시간 기온 트렌드 바 차트**:
   - 순수 HTML/CSS 글래스 바 차트로 24시간 시간대별 기온 변화를 직관적으로 시각화.
3. **실내 공기질(IAQ) 모니터링 & AI 진단**:
   - 초미세먼지(PM2.5), 미세먼지(PM10), 이산화탄소(CO₂), 유해가스(TVOC) 실시간 수치 표시.
   - AI 위험도 판단: 수치가 기준치를 초과하면 공기청정기/환기 작동 멘트 및 위험 배지 표시.
4. **음성 명령어 계층 구조**:
   - `"정보 켜/꺼"`, `"전체 켜/꺼"`: HUD 정보창과 캘린더 전체를 한 번에 일괄 제어.
   - `"시간 꺼"`, `"날씨 꺼"`, `"공기질 꺼"`, `"캘린더 꺼"`: 개별 위젯 섹션만 선택적으로 켜고 끄기.

### Phase 3: 구글 캘린더(Google Calendar) 글래스 위젯 전면 재디자인
1. **버튼 글로벌 스타일 충돌 완전 해결**:
   - `.smart-window-calendar button` 및 `.gcal-day-cell`에 `all: unset !important` 적용으로 기본 브라우저 버튼 패딩·마진 간섭 원천 차단.
2. **7열 1:1 완벽 정렬 그리드**:
   - 요일 헤더(`.gcal-weekdays`)와 날짜 그리드(`.gcal-grid`) 모두 `repeat(7, minmax(0, 1fr))` 및 `gap: 2px`로 통일하여 1픽셀의 오차도 없이 수직선 일치.
3. **답답했던 회색 네모 타일 제거 & 원형 날짜 배지**:
   - 모든 날짜 셀의 회색 배경을 투명(`background: transparent`)으로 변경.
   - 날짜 숫자를 28px 완전 원형(Circle) 배지로 구현.
   - 오늘 날짜(Today): 구글 캘린더 시그니처 블루 배지(`background: #1a73e8`) + 글로우 섀도우.
   - 선택 날짜(Selected): 세련된 2px 화이트 아웃라인 배지.
4. **35일 꽉 찬 먼슬리(Muted Adjacent Month Days)**:
   - 이전 달 및 다음 달 날짜를 은은한 반투명 텍스트(`opacity: 0.28`)로 채워, 30일 뒤 빈 칸이 뚝 끊기지 않는 완벽한 5행 7열 사각 그리드 완성.
5. **일정 리스트(Agenda List) 및 추가 폼 최적화**:
   - 2~3건의 일정도 스크롤바 없이 쾌적하게 표시되도록 높이 및 글래스 카드 톤앤매너 최적화.
   - 시간 인풋(`14:00`), 제목 인풋(`새 일정 입력`), 추가 버튼(`＋`)의 높이를 36px로 완벽 수평 일치.

### Phase 4: 이미지 출처별 분류 및 명명 규칙
사용자 피드백을 반영하여 프리셋 카드의 제목 명명 규칙을 엄격히 정립:
- **만든 거 (AI 생성)**: 뒤에 `(AI)` 표기 (예: `서울 야경 (AI)`, `바다 풍경 (AI)`)
- **찾은 거 (웹 검색)**: 뒤에 `(웹)` 표기 & 트리거 동사('찾아줘', '검색해줘' 등) 자동 제거 (예: `도쿄 야경 (웹)`)
- **직접 넣은 거 (사용자 추가 파일)**: **뒤에 괄호 치지 않음** (예: `강남 아파트`, `우리집 강아지`)
- **기본 설정**: **뒤에 괄호 치지 않음** (`기본 대기 화면`, `아침 풍경`, `노을 풍경`, `저녁 풍경`)

### Phase 5: 로딩 인디케이터 문구 심플화
- 기존의 길고 장황했던 `ComfyUI 풍경 생성 중 (5%) · ComfyUI 초고화질 생성 중: '여름_바닷가_풍경을.png' (기존 화면 유지)...` 문구를 전면 개선.
- 사용자 요청에 맞추어 **`배경 생성 중 (Step 1/4)` ~ `배경 생성 중 (Step 4/4)`** 및 **`배경 검색 중...`**으로 간결하게 통일.

### Phase 6: Searlo 공식 구글 이미지 검색 API 연동
- 사용자가 제공한 **Searlo 공식 API 키**를 메인 검색 엔진으로 전면 탑재:
  - **엔드포인트**: `https://api.searlo.tech/api/v1/search/images?q={query}`
  - **API Key**: `sk_aad76d78e142dd76e1d4ae88fa7a9b6cf4c589a1f504c36de1136906069d8284`
  - **인증 방식**: `x-api-key` 및 `Authorization: Bearer` 헤더
  - **수집 기준**: 실시간 구글 이미지 검색 결과 중 16:9 와이드(Aspect >= 1.15) 및 1920×1080 고해상도 원본 사진 우선 채택.
  - **설정 보존**: `.cache/google_search_config.json`에 영구 보존 및 UI 5번 카드 연동 완료.

---

## 3. 📂 디렉터리 및 주요 파일 구조

```text
c:\Users\Administrator\Desktop\해커톤\
├── main.py                     # FastHTML 메인 엔트리포인트, HTMX 라우팅, HUD 및 캘린더 렌더러
├── HANDOVER.md                 # 본 인수인계 문서
├── requirements.txt            # 패키지 의존성 목록
├── .env.example                # 환경 변수 템플릿
├── assets/
│   ├── css/
│   │   └── style.css           # 전체화면 뷰포트, 글래스모피즘 HUD & 캘린더, 설정 드로어 CSS
│   └── images/
│       └── presets/            # 창문 배경 이미지 저장 디렉터리 (기본, AI 생성, 웹 검색, 사용자 업로드)
├── src/
│   └── modules/
│       ├── virtual_landscape.py# 가상 창문 매니저, ComfyUI 7-노드 에뮬레이터, IntentInferenceEngine
│       ├── web_image_search.py # Searlo 공식 API, SerpApi, Google/Bing/Unsplash 웹 이미지 검색기
│       ├── lighting_controller.py# 생체리듬 조명 및 색온도/채도/밝기 제어기
│       ├── audio_controller.py # 음성 인식(STT), 음성 합성(TTS), 호출어(Wake word) 제어기
│       └── smart_hud.py        # 시계/날씨/IAQ 데이터 매니저
├── .cache/                     # 런타임 캐시 폴더
│   ├── google_search_config.json # Searlo / Google 검색 API 키 영구 저장소
│   ├── calendar_events.json    # 구글 캘린더 등록 일정 데이터
│   ├── web_images/             # 웹 검색 다운로드 원본 캐시
│   └── landscapes/             # AI 생성 이미지 캐시
└── adocs/                      # 변경 이력 백업 및 git 동기화 디렉터리
```

---

## 4. ⚙️ 핵심 비즈니스 로직 및 규칙 요약

### 1) 음성 의도 분류 (`IntentInferenceEngine.infer_intent`)
- **생성 트리거**: 문장에 `"만들어줘"`, `"그려줘"`, `"생성해줘"`가 포함된 경우 → ComfyUI 로컬 생성(`route_type="generate"`) 실행.
- **검색 트리거**: 문장에 `"찾아줘"`, `"검색해줘"`, `"찾아봐"`가 포함된 경우 → Searlo 웹 이미지 검색(`route_type="search"`) 실행.
- **프리셋 전환 트리거**: 생성/검색 동사가 없고 등록된 프리셋 이름(예: "아침", "노을", "서울 야경", "우리집 강아지")을 부른 경우 → 즉시 풍경 전환(`route_type="preset"`).
- **무반응 필터링**: 위 조건에 해당하지 않는 잡음이나 일상 대화는 창문 오작동 방지를 위해 엄격히 무시(`route_type="ignore"`).

### 2) 프리셋 명명 및 괄호 규칙 (`build_custom_preset`)
```python
if source_type == "ai":
    title = f"{display_name} (AI)"
elif source_type == "web":
    title = f"{display_name} (웹)"
else:
    # "custom"(직접 넣은 파일) 및 "default"(기본 설정): 뒤에 괄호 없음!
    title = display_name
```
- 파일명 생성 시 트리거 문구("찾아줘", "만들어줘" 등)는 자동 제거되어 `도쿄_야경.jpg` 형태로 저장됩니다.

### 3) 로딩 뱃지 문구
- AI 생성 중: `배경 생성 중 (Step 1/4)` ~ `배경 생성 중 (Step 4/4)`
- 웹 검색 중: `배경 검색 중...`

### 4) Searlo API 연동 (`WebImageSearchManager`)
- **API Key**: `sk_aad76d78e142dd76e1d4ae88fa7a9b6cf4c589a1f504c36de1136906069d8284`
- **엔드포인트**: `https://api.searlo.tech/api/v1/search/images?q={query}`
- 헤더: `x-api-key`, `Authorization: Bearer`
- 가로/세로 비율 1.15 이상의 와이드 이미지만 선별하여 1920×1080 Lanczos 센터크롭 변환.

---

## 5. 🧑‍💻 다음 계정 / 작업자가 지켜야 할 필수 유의사항

1. **언어 규칙**: 사용자는 **모든 답변과 생각 과정(Thinking)을 반드시 한글로 작성**하도록 요청하고 있습니다.
2. **파일 링크 규칙**: 파일이나 심볼을 언급할 때는 반드시 `[파일명](file:///전체경로)` 형태의 마크다운 링크를 사용해야 합니다.
3. **가상환경 실행**:
   - Python 실행 시 `.venv\Scripts\python.exe main.py`를 사용합니다.
4. **Git 조작 시 주의**:
   - Windows 터미널에서 ownership 이슈가 발생할 수 있으므로 `git -c safe.directory=* ...` 형태로 실행하는 것이 안전합니다.
   - 코드 수정 시 작업 폴더와 `adocs/` 폴더를 함께 동기화하여 커밋을 남겨주세요.
5. **ComfyUI 생성 상태 유지**:
   - 이미지 생성 중에는 이전 화면을 유지하고, 생성이 100% 완료된 순간에만 새 화면으로 크로스페이드 전환되어야 합니다.

---

**인수인계 완료 상태**: 현재 모든 코드는 컴파일 검증 및 정상 동작 확인이 완료되었으며, `adocs/` 저장소에 커밋(`afdc6e3`)되어 안전하게 보존되어 있습니다.
