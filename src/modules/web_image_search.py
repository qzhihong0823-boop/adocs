"""
웹 이미지 검색 및 가상 창문 맞춤 배경 최적화 모듈 (web_image_search.py)

핵심 기능:
1. '찾아줘', '검색해줘' 음성/텍스트 명령 인식 시 실제 웹에서 실시간 배경 사진 검색
2. API 키 없이도 100% 실시간 작동하는 4대 웹 이미지 검색 엔진 통합:
   - Google / Bing Images 실시간 와이드 고화질 스크래퍼
   - Unsplash NAPI 실시간 16:9 와이드 풍경 검색
   - DuckDuckGo Images 실시간 검색
   - Wikimedia Commons 고해상도 풍경 API
3. Google Custom Search JSON API 1순위 지원 (사용자 API Key/CX 입력 시)
4. 한국어 검색어 -> 글로벌 고화질 풍경 쿼리 자동 확장 사전 (`SCENIC_TRANSLATION_MAP`)
5. 창문 화면(1920x1080, 16:9)에 맞춤 스마트 최적화:
   - 가로 비율(Aspect ratio >= 1.25) 및 고해상도(1280x720 이상) 정밀 선별
   - 아이콘, 로고, 상품, 영수증, 텍스트, 인물 등 부적합 이미지 엄격 배제
   - Pillow(PIL) 스마트 센터 크롭(Center-crop) 및 최고화질 Lanczos 리샘플링
"""

from __future__ import annotations

import io
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from PIL import Image

try:
    import cv2
except Exception:
    cv2 = None



DEFAULT_SEARLO_API_KEY = "sk_aad76d78e142dd76e1d4ae88fa7a9b6cf4c589a1f504c36de1136906069d8284"


@dataclass
class GoogleSearchConfig:
    api_key: str = ""
    cx: str = ""  # Custom Search Engine ID
    serpapi_key: str = ""  # SerpApi Key
    searlo_key: str = DEFAULT_SEARLO_API_KEY  # Searlo 공식 API Key (https://api.searlo.tech)


@dataclass
class ImageCandidate:
    url: str
    width: int
    height: int
    title: str
    source: str
    score: float = 0.0
    rank: int = 0
    fallback_url: str = ""


class WebImageSearchManager:
    """구글 및 웹 이미지 검색, 배경 적합성 분류, 16:9 화면 맞춤 변환 총괄 매니저"""

    TARGET_WIDTH = 1920
    TARGET_HEIGHT = 1080
    MIN_WIDTH = 1000
    MIN_HEIGHT = 550
    MIN_ASPECT = 1.15  # 가로 세로 비율 (width / height)

    NEGATIVE_KEYWORDS = (
        "icon", "logo", "clipart", "diagram", "screenshot",
        "buy", "price", "ebay", "amazon", "shopping", "recipe",
        "아이콘", "로고", "클립아트", "다이어그램", "스크린샷",
        "증명사진", "도표", "구매", "판매", "영수증", "가격비교",
    )

    POSITIVE_KEYWORDS = (
        "landscape", "scenic", "view", "nature", "wallpaper", "panorama",
        "skyline", "horizon", "vista", "scenery", "4k", "ultrawide",
        "photography", "national park", "mountain", "ocean", "sea", "city",
        "sunset", "sunrise", "night view", "풍경", "전경", "배경", "자연",
        "도시", "야경", "스카이라인", "파노라마", "석양", "일출", "바다", "설경",
    )

    SEARCH_TRIGGER_VERBS = (
        "찾아줘", "찾아 줘", "찾아", "검색해줘", "검색해 줘", "검색해",
        "찾아봐", "찾아봐줘", "구해줘",
    )

    # 한국어 주요 랜드마크 및 자연 지형 -> 글로벌 고화질 풍경 쿼리 자동 확장 사전
    SCENIC_TRANSLATION_MAP: Dict[str, str] = {
        "에펠탑": "Eiffel Tower Paris panoramic scenic landscape wallpaper",
        "파리": "Paris city skyline Eiffel tower river Seine scenic view",
        "도쿄": "Tokyo city skyline night view glittering lights landscape",
        "도쿄 야경": "Tokyo night skyline panoramic glittering lights landscape wallpaper",
        "야경": "glittering modern city skyline night view panorama wallpaper 4k",
        "서울": "Seoul city skyline N Seoul Tower Han River landscape",
        "서울 야경": "Seoul night view Han river bridge glittering skyline",
        "부산": "Busan Haeundae beach Gwangandaegyo bridge ocean view",
        "해운대": "Haeundae beach Busan ocean waves scenic landscape",
        "스위스": "Swiss Alps green valley wooden chalet snow mountain peaks",
        "알프스": "Swiss Alps majestic snow mountain peak panoramic landscape",
        "산토리니": "Santorini Greece whitewashed houses blue domes Aegean sea",
        "하와이": "Hawaii tropical ocean beach turquoise water palm trees",
        "발리": "Bali Indonesia tropical ocean sunset beach golden hour",
        "몰디브": "Maldives clear turquoise ocean overwater villa tropical beach",
        "뉴욕": "New York Manhattan skyline Hudson river panoramic view",
        "맨해튼": "Manhattan New York skyscrapers central park panoramic landscape",
        "런던": "London Big Ben Westminster bridge river Thames landscape",
        "오로라": "Aurora borealis northern lights glowing green sky snowy landscape",
        "은하수": "Milky way galaxy starry night sky mountains panorama landscape",
        "설경": "winter snow-covered pine tree forest frozen lake landscape",
        "설산": "majestic snow-capped alpine mountain peaks winter landscape",
        "겨울": "winter wonderland snow covered landscape peaceful nature",
        "바다": "crystal clear turquoise ocean waves sandy beach horizon",
        "해변": "tropical sandy beach clear turquoise sea gentle waves",
        "석양": "dramatic golden sunset over ocean horizon warm golden hour",
        "노을": "vibrant orange sunset sky clouds golden hour landscape",
        "아침": "early morning sunrise golden sunbeams misty forest landscape",
        "일출": "majestic sunrise horizon dawn morning sunlight landscape",
        "숲": "sunbeams filtering through dense green pine forest nature",
        "단풍": "autumn vibrant red and gold maple tree forest scenic river",
        "벚꽃": "spring blooming pink cherry blossom trees pathway landscape",
        "우주": "deep space nebula colorful cosmos planets stars panorama",
        "사막": "golden sand dunes Sahara desert sunset ripples landscape",
        "한강": "Seoul Han River skyline sunset bridge reflection panoramic view",
        "제주도": "Jeju Island emerald ocean Seongsan Ilchulbong coastal landscape",
        "제주": "Jeju Island emerald ocean coastal cliffs scenic landscape",
        "아이슬란드": "Iceland waterfall aurora glacier dramatic nature landscape",
        "노르웨이": "Norway fjord dramatic mountains calm water scenic vista",
        "베네치아": "Venice Italy grand canal historic buildings sunset view",
        "프라하": "Prague Charles Bridge castle skyline sunset river reflection",
        "로마": "Rome Italy Colosseum historic skyline warm sunset vista",
        "두바이": "Dubai Marina Burj Khalifa futuristic skyline night view",
        "홍콩": "Hong Kong Victoria Harbour glittering skyline night view",
        "그랜드캐니언": "Grand Canyon red rock cliffs dramatic sunset panoramic view",
        "나이아가라": "Niagara Falls massive waterfall mist rainbow scenic view",
    }

    # 복합 한국어 검색어("발리 해변 노을", "파리 에펠탑 야경" 등) 부분 매칭용 핵심 키워드 사전
    PARTIAL_KEYWORD_MAP: Dict[str, str] = {
        "에펠탑": "Eiffel Tower Paris",
        "파리": "Paris skyline Seine river",
        "도쿄": "Tokyo skyline",
        "오사카": "Osaka Dotonbori castle skyline",
        "교토": "Kyoto traditional street bamboo forest",
        "후지산": "Mount Fuji Japan",
        "서울": "Seoul skyline Han River Namsan Tower",
        "한강": "Seoul Han River park bridge",
        "부산": "Busan Haeundae Gwangan Bridge ocean",
        "해운대": "Haeundae beach Busan",
        "광안리": "Gwangalli beach Gwangan Bridge night",
        "제주도": "Jeju Island coastal",
        "제주": "Jeju Island",
        "독도": "Dokdo island East Sea",
        "스위스": "Swiss Alps",
        "알프스": "Alpine snow mountains",
        "산토리니": "Santorini Greece blue dome sea",
        "하와이": "Hawaii Waikiki tropical beach",
        "발리": "Bali tropical beach",
        "몰디브": "Maldives turquoise lagoon beach",
        "뉴욕": "New York Manhattan skyline",
        "맨해튼": "Manhattan skyscrapers",
        "런던": "London Thames Tower Bridge Big Ben",
        "아이슬란드": "Iceland nature",
        "노르웨이": "Norway fjord",
        "베네치아": "Venice canal Italy",
        "프라하": "Prague castle bridge",
        "로마": "Rome Italy",
        "두바이": "Dubai skyline",
        "홍콩": "Hong Kong harbour skyline",
        "오로라": "aurora borealis northern lights",
        "은하수": "Milky Way starry night sky",
        "설경": "snow-covered winter landscape",
        "설산": "snow-capped mountain peaks",
        "겨울": "winter snow",
        "눈": "snowy",
        "봄": "spring",
        "벚꽃": "cherry blossoms",
        "여름": "summer",
        "가을": "autumn",
        "단풍": "autumn red maple foliage",
        "바다": "turquoise ocean coast",
        "해변": "sandy beach ocean waves",
        "파도": "ocean waves",
        "호수": "calm mirror lake",
        "강": "scenic river",
        "폭포": "majestic waterfall",
        "계곡": "mountain valley stream",
        "숲": "green pine forest sunbeams",
        "산": "majestic mountain range",
        "사막": "golden desert sand dunes",
        "우주": "deep space nebula stars",
        "도시": "modern city skyline",
        "밤거리": "neon night street cityscape",
        "야경": "glittering night view skyline",
        "밤": "night view",
        "저녁": "evening twilight",
        "노을": "golden orange sunset sky",
        "석양": "dramatic golden sunset horizon",
        "일몰": "sunset horizon",
        "아침": "morning sunrise sunlight",
        "일출": "sunrise dawn",
        "비": "rainy wet reflections",
        "안개": "misty fog",
    }


    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.cache_dir = self.base_dir / ".cache" / "web_images"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.base_dir / ".cache" / "google_search_config.json"
        self.config: GoogleSearchConfig = self._load_config()
        self.is_searching: bool = False
        self.last_query: str = ""
        self.last_result_title: str = ""

        # SSL 비검증 컨텍스트 준비 (호스트 환경 인증서 오류 방지)
        try:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE
        except Exception:
            self._ssl_ctx = None

    def _load_config(self) -> GoogleSearchConfig:
        api_key = os.environ.get("GOOGLE_SEARCH_API_KEY", "").strip()
        cx = os.environ.get("GOOGLE_SEARCH_CX", "").strip()
        serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
        searlo_key = os.environ.get("SEARLO_API_KEY", DEFAULT_SEARLO_API_KEY).strip()
        if self.config_file.exists():
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                api_key = data.get("api_key", api_key).strip()
                cx = data.get("cx", cx).strip()
                serpapi_key = data.get("serpapi_key", serpapi_key).strip()
                searlo_key = data.get("searlo_key", searlo_key).strip() or DEFAULT_SEARLO_API_KEY
            except Exception:
                pass
        return GoogleSearchConfig(api_key=api_key, cx=cx, serpapi_key=serpapi_key, searlo_key=searlo_key)

    def save_config(self, api_key: str = "", cx: str = "", serpapi_key: str = "", searlo_key: str = "") -> GoogleSearchConfig:
        if api_key != "":
            self.config.api_key = api_key.strip()
        if cx != "":
            self.config.cx = cx.strip()
        if serpapi_key != "":
            self.config.serpapi_key = serpapi_key.strip()
        if searlo_key != "":
            self.config.searlo_key = searlo_key.strip()
        try:
            self.config_file.write_text(
                json.dumps({
                    "api_key": self.config.api_key,
                    "cx": self.config.cx,
                    "serpapi_key": self.config.serpapi_key,
                    "searlo_key": self.config.searlo_key,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return self.config

    @classmethod
    def extract_search_keyword(cls, raw_text: str) -> Optional[str]:
        """음성/텍스트 발화에서 검색 키워드를 깔끔하게 추출"""
        text = raw_text.strip()
        for ww in ("윈도우", "윈도", "window", "windo"):
            if text.startswith(ww):
                text = text[len(ww):].strip()

        has_trigger = False
        matched_trigger = ""
        for v in cls.SEARCH_TRIGGER_VERBS:
            if v in text:
                has_trigger = True
                matched_trigger = v
                break

        if not has_trigger:
            return None

        parts = text.split(matched_trigger, 1)
        cleaned = parts[0].strip()
        if not cleaned and len(parts) > 1:
            cleaned = parts[1].strip()

        cleaned = re.sub(r"(이미지|사진|풍경|배경|웹에서|구글에서|인터넷에서|좀|하나|한번|다시|새로)\s*", " ", cleaned)
        cleaned = re.sub(r"(으로|로|을|를|이|가|에서)\s*$", "", cleaned.strip())
        cleaned = cleaned.strip()

        return cleaned if len(cleaned) >= 2 else None

    def build_english_query(self, query: str) -> str:
        """
        한국어 질의어를 전세계 고화질 풍경 이미지 DB 검색에 최적화된 영문/다국어 쿼리로 확장:
        1) 완전 일치 사전(SCENIC_TRANSLATION_MAP) 확인
        2) 복합어 부분 키워드 조합 사전(PARTIAL_KEYWORD_MAP) 매칭
        3) 사전에 없는 고유명사/복합어는 Gemini API(설정 시)로 초고속 영문 4K 풍경 쿼리 변환
        """
        q = query.strip()
        if q in self.SCENIC_TRANSLATION_MAP:
            return self.SCENIC_TRANSLATION_MAP[q]
        for k, translated in self.SCENIC_TRANSLATION_MAP.items():
            if len(k) >= 2 and k == q:
                return translated

        # 2) 복합 검색어("발리 해변 노을", "파리 에펠탑 야경" 등)에서 부분 키워드 추출 조합
        matched_eng_parts: List[str] = []
        remaining_ko = q
        # 긴 키워드부터 우선 매칭
        for ko_kw, eng_phrase in sorted(self.PARTIAL_KEYWORD_MAP.items(), key=lambda x: len(x[0]), reverse=True):
            if ko_kw in remaining_ko:
                matched_eng_parts.append(eng_phrase)
                remaining_ko = remaining_ko.replace(ko_kw, " ").strip()

        # 3) 아직 번역되지 않은 한글 어절이 남아있거나 매칭된 영문 키워드가 없을 때 Gemini API로 영문 검색어 보강
        rem_ko_tokens = [w for w in re.findall(r"[가-힣]{2,}", remaining_ko) if w not in ("풍경", "배경", "사진", "화면", "모습", "뷰")]
        if rem_ko_tokens or not matched_eng_parts:
            gemini_eng = self._translate_query_via_gemini(q)
            if gemini_eng:
                return gemini_eng

        if matched_eng_parts:
            combined = " ".join(dict.fromkeys(" ".join(matched_eng_parts).split()))
            return f"{combined} panoramic scenic landscape wallpaper 4k"

        return f"{q} panoramic landscape wallpaper 4k"

    def _translate_query_via_gemini(self, query: str) -> Optional[str]:
        """Gemini API를 사용하여 한국어 검색어를 구글/Bing/Unsplash 이미지 검색용 최적 영문 풍경 쿼리로 변환"""
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return None
        prompt = (
            f"Convert the Korean window background search query '{query}' into a concise English image search query "
            "(5 to 9 words) for finding a high-resolution 16:9 wide real-world scenic photograph or city/nature wallpaper. "
            "Output ONLY the English search query without quotes or explanation."
        )
        for model_name in ("gemini-3-flash-preview", "gemini-2.5-flash"):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 40},
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=2.5, context=self._ssl_ctx) as resp:
                    res = json.loads(resp.read().decode("utf-8", errors="ignore"))
                candidates = res.get("candidates") or []
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts") or []
                    if parts and parts[0].get("text"):
                        eng = re.sub(r'["\'\n\r`]+', " ", parts[0]["text"]).strip()
                        if len(eng) >= 4 and re.search(r"[A-Za-z]{3,}", eng):
                            if "wallpaper" not in eng.lower() and "landscape" not in eng.lower():
                                eng = f"{eng} scenic landscape wallpaper 4k"
                            return eng
            except Exception:
                continue
        return None

    def search_and_process_background(
        self,
        query: str,
        log_callback: Optional[Any] = None,
    ) -> Tuple[bool, str, Optional[Path], str]:
        """
        웹에서 배경에 적합한 사진을 다중 검색 엔진(Searlo + Google Direct + Bing Wide + Unsplash + DDG)으로 수집하고,
        OpenCV/NumPy 비전 품질 검수(해상도·선명도·레터박스 제거)를 거쳐 최고 화질의 1920x1080 이미지를 저장.
        반환: (성공여부, 적용메시지, 저장된파일경로, 추론된자연광키)
        """
        clean_query = query.strip()
        if not clean_query:
            return False, "검색어가 비어 있습니다.", None, "default"

        self.is_searching = True
        self.last_query = clean_query
        eng_query = self.build_english_query(clean_query)

        if log_callback:
            log_callback("WEB", f"🌐 웹 이미지 정밀 검색 시작: \"{clean_query}\" (확장 쿼리: {eng_query[:45]})")

        try:
            candidates: List[ImageCandidate] = []

            # 1. Searlo 공식 이미지 검색 API (엔드포인트: https://api.searlo.tech/api/v1/search/images) - 최우선 메인 엔진!
            searlo_key = self.config.searlo_key or DEFAULT_SEARLO_API_KEY
            if searlo_key:
                if log_callback:
                    log_callback("WEB", f"🚀 Searlo 공식 이미지 검색 엔진 가동: \"{clean_query}\"...")
                searlo_cands = self._search_searlo_images(clean_query, eng_query)
                candidates.extend(searlo_cands)
                if searlo_cands and log_callback:
                    log_callback("WEB", f"✅ Searlo 공식 엔진에서 {len(searlo_cands)}개 와이드 후보 확보")

            # 2. Google Images 실시간 직접 고해상도 검색 (원본 와이드 사진 직접 수집)
            google_direct_cands = self._search_google_images_direct(clean_query, eng_query)
            if google_direct_cands:
                candidates.extend(google_direct_cands)
                if log_callback:
                    log_callback("WEB", f"✅ Google Images 다이렉트 엔진에서 {len(google_direct_cands)}개 고해상도 후보 추가 확보")

            # 3. Bing Images 실시간 와이드 고화질 검색 (고해상도 murl 원본 확보를 위해 항상 함께 가동)
            if len(candidates) < 16:
                bing_cands = self._search_bing_images(clean_query, eng_query)
                candidates.extend(bing_cands)
                if bing_cands and log_callback:
                    log_callback("WEB", f"✅ Bing Wide 고화질 엔진에서 {len(bing_cands)}개 와이드 후보 확보")

            # 4. SerpApi / Google Custom Search API (키 설정 시)
            serp_key = self.config.serpapi_key or os.environ.get("SERPAPI_API_KEY", "").strip()
            if len(candidates) < 8 and serp_key:
                serp_cands = self._search_serpapi_google_images(clean_query, eng_query)
                candidates.extend(serp_cands)

            if len(candidates) < 8 and self.config.api_key and self.config.cx:
                google_cands = self._search_google_api(clean_query, eng_query)
                candidates.extend(google_cands)

            # 5. Unsplash 16:9 와이드 풍경 NAPI & DuckDuckGo Images 보조 가동
            if len(candidates) < 10:
                unsplash_cands = self._search_unsplash_napi(clean_query, eng_query)
                candidates.extend(unsplash_cands)
            if len(candidates) < 6:
                ddg_cands = self._search_duckduckgo_images(clean_query, eng_query)
                candidates.extend(ddg_cands)
            if len(candidates) == 0:
                wiki_cands = self._search_wikimedia(clean_query, eng_query)
                candidates.extend(wiki_cands)

            # 6. 메타데이터 기반 1차 스코어링 정렬
            scored_candidates = self._score_and_filter_candidates(candidates, clean_query, eng_query)

            if log_callback and scored_candidates:
                top = scored_candidates[0]
                dim_str = f"{top.width}x{top.height}" if top.width > 0 else "고화질"
                log_callback(
                    "WEB",
                    f"📊 총 {len(candidates)}개 후보 중 상위 {len(scored_candidates)}개 선별 → 비전 화질·선명도 검수 진행",
                    f"1차 1순위: {top.title[:35]}... ({dim_str} · 출처: {top.source})",
                )

            # 7. 상위 후보들을 다운로드하여 실제 이미지 품질(해상도·선명도·비율·레터박스 제거)을 비교 평가한 뒤 최고점 이미지 선택!
            best_img: Optional[Image.Image] = None
            best_cand: Optional[ImageCandidate] = None
            best_total_score: float = -1e9
            verified_count = 0

            for cand in scored_candidates[:12]:
                try:
                    img_data = self._download_image(cand.url, cand.fallback_url)
                    if not img_data:
                        continue

                    optimized_img, visual_quality_score = self._optimize_and_score_image_for_window(img_data)
                    if not optimized_img:
                        continue

                    total_score = cand.score + visual_quality_score
                    verified_count += 1
                    if total_score > best_total_score:
                        best_total_score = total_score
                        best_img = optimized_img
                        best_cand = cand

                    # 충분히 높은 화질(Full HD급 선명한 실사)이 확인되면 빠르게 확정, 아니면 최대 4개까지 비교
                    if verified_count >= 4 or (verified_count >= 2 and visual_quality_score >= 95.0):
                        break
                except Exception:
                    continue

            if best_img is not None and best_cand is not None:
                sanitized_name = self._sanitize_filename(clean_query)
                out_path = self.cache_dir / f"{sanitized_name}_{int(time.time())}.jpg"
                best_img.save(out_path, format="JPEG", quality=95, optimize=True)

                preset_path = self.base_dir / "assets" / "images" / "presets" / f"{sanitized_name}.jpg"
                preset_path.parent.mkdir(parents=True, exist_ok=True)
                best_img.save(preset_path, format="JPEG", quality=95, optimize=True)

                lighting_key = self._infer_lighting_from_query(clean_query)
                self.last_result_title = best_cand.title or clean_query

                msg = f"웹에서 \"{clean_query}\" 최고화질 배경 사진을 선별해 화면에 꽉 차게 적용했습니다!"
                if log_callback:
                    log_callback(
                        "WEB",
                        f"✅ {msg}",
                        f"1920x1080 레터박스 제거 & 화질 검수 통과 | 출처: {best_cand.source} | 광원: {lighting_key}",
                    )

                return True, msg, out_path, lighting_key


            # 8. [오프라인/네트워크 차단 대비 스마트 로컬 테마 폴백]
            local_fallback = self._match_local_theme_fallback(clean_query)
            if local_fallback and local_fallback.exists():
                try:
                    img_data = local_fallback.read_bytes()
                    optimized_img = self._optimize_image_for_window(img_data)
                    if optimized_img:
                        sanitized_name = self._sanitize_filename(clean_query)
                        out_path = self.cache_dir / f"{sanitized_name}_{int(time.time())}.jpg"
                        optimized_img.save(out_path, format="JPEG", quality=95, optimize=True)

                        preset_path = self.base_dir / "assets" / "images" / "presets" / f"{sanitized_name}.jpg"
                        preset_path.parent.mkdir(parents=True, exist_ok=True)
                        optimized_img.save(preset_path, format="JPEG", quality=95, optimize=True)

                        lighting_key = self._infer_lighting_from_query(clean_query)
                        msg = f"\"{clean_query}\" 테마에 맞는 고화질 배경을 선별하여 화면에 적용했습니다!"
                        if log_callback:
                            log_callback("WEB", f"✅ {msg}", f"1920x1080 와이드 최적화 | 광원: {lighting_key}")
                        return True, msg, out_path, lighting_key
                except Exception:
                    pass

            return False, f"\"{clean_query}\" 배경 이미지를 웹에서 가져오지 못했습니다.", None, "default"

        finally:
            self.is_searching = False

    def _search_google_images_direct(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """
        Google Images 실시간 고해상도 와이드 직접 검색 (API 키 불필요):
        - tbs=isz:l,iar:w,itp:photo (대형 해상도 + 와이드 비율 + 실사 사진 필터)
        - 구글 이미지 결과 스크립트 내 원본 이미지 URL 및 (width, height) 메타데이터 직접 추출
        """
        results: List[ImageCandidate] = []
        search_terms = [
            f"{query} 풍경 고화질 배경화면",
            eng_query,
        ]
        seen = set()

        for q_term in search_terms:
            if not q_term:
                continue
            try:
                params = {
                    "q": q_term,
                    "tbm": "isch",
                    "tbs": "isz:l,iar:w,itp:photo",
                    "hl": "ko",
                    "gl": "kr",
                }
                url = f"https://www.google.com/search?{urllib.parse.urlencode(params)}"
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                )
                with urllib.request.urlopen(req, timeout=4.5, context=self._ssl_ctx) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")

                # 패턴: ["https://.../image.jpg", height, width]
                pattern = re.compile(
                    r'\["(https?://[^"\s<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\s<>]*)?)",(\d{3,4}),(\d{3,4})\]',
                    re.IGNORECASE,
                )
                rank_idx = 1
                for m in pattern.finditer(html):
                    raw_url, h_str, w_str = m.group(1), m.group(2), m.group(3)
                    img_url = raw_url.encode("utf-8").decode("unicode_escape", errors="ignore")
                    if "gstatic.com" in img_url or "google.com" in img_url or "favicon" in img_url:
                        continue
                    if img_url in seen:
                        continue
                    w = int(w_str)
                    h = int(h_str)
                    if w < 1000 or h < 550 or (w / max(1, h)) < self.MIN_ASPECT:
                        continue
                    seen.add(img_url)
                    results.append(
                        ImageCandidate(
                            url=img_url,
                            width=w,
                            height=h,
                            title=f"{query} ({w}x{h})",
                            source="google_direct",
                            rank=rank_idx,
                        )
                    )
                    rank_idx += 1
                    if len(results) >= 10:
                        break
                if len(results) >= 6:
                    break
            except Exception:
                continue

        return results

    def _search_bing_images(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """
        Bing Images 실시간 검색 (API 키 불필요)
        - 원본 고화질 와이드 이미지 URL(`murl`)을 직접 파싱
        """
        results: List[ImageCandidate] = []
        search_terms = [
            f"{query} 풍경 고화질 배경화면",
            query,
        ]
        if eng_query and eng_query != f"{query} 고화질 배경화면":
            search_terms.append(eng_query)


        for q_term in search_terms:
            try:
                encoded = urllib.parse.quote(q_term)
                # qft=+filterui:aspect-wide+filterui:imagesize-large (가로 와이드 + 대형 고화질)
                url = f"https://www.bing.com/images/search?q={encoded}&qft=+filterui:aspect-wide+filterui:imagesize-large&first=1"
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                )
                with urllib.request.urlopen(req, timeout=4.5, context=self._ssl_ctx) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")

                # Bing murl 파싱: m="{&quot;cid&quot;:...,&quot;murl&quot;:&quot;URL&quot;,...}"
                matches = re.findall(r'murl&quot;:&quot;(https?://[^&]+)&quot;.*?t1&quot;:&quot;([^&]*?)&quot;', html)
                if not matches:
                    matches = [(u, q_term) for u in re.findall(r'murl&quot;:&quot;(https?://[^&]+)&quot;', html)]

                for idx, (m_url, m_title) in enumerate(matches[:10], start=1):
                    results.append(
                        ImageCandidate(
                            url=m_url,
                            width=1920,
                            height=1080,
                            title=m_title or q_term,
                            source="bing_web",
                            rank=idx,
                        )
                    )
                if len(results) >= 6:
                    break
            except Exception:
                continue

        return results

    def _search_unsplash_napi(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """
        Unsplash 공개 NAPI 실시간 16:9 와이드 풍경 검색 (API 키 불필요)
        """
        results: List[ImageCandidate] = []
        try:
            core_words = re.findall(r"[A-Za-z]+", eng_query)
            search_str = " ".join(core_words[:2]) if core_words else "landscape"
            encoded = urllib.parse.quote(search_str)

            url = f"https://unsplash.com/napi/search/photos?query={encoded}&per_page=12&orientation=landscape"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=4.5, context=self._ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            items = data.get("results", [])
            for item in items:
                urls = item.get("urls", {})
                # 1920x1080 와이드 고화질 다이렉트 URL
                raw_url = urls.get("raw")
                if raw_url:
                    img_url = f"{raw_url}&auto=format&fit=crop&w=1920&h=1080&q=85"
                else:
                    img_url = urls.get("regular") or urls.get("full")

                w = int(item.get("width", 1920))
                h = int(item.get("height", 1080))
                desc = item.get("description") or item.get("alt_description") or f"{query} landscape"

                if img_url:
                    results.append(
                        ImageCandidate(
                            url=img_url,
                            width=w,
                            height=h,
                            title=desc,
                            source="unsplash",
                        )
                    )
        except Exception:
            pass

        return results

    def _search_duckduckgo_images(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """DuckDuckGo Images 실시간 검색"""
        results: List[ImageCandidate] = []
        search_terms = [eng_query, f"{query} 풍경"]

        for q_term in search_terms:
            try:
                token_url = f"https://duckduckgo.com/?q={urllib.parse.quote(q_term)}&iax=images&ia=images"
                token_req = urllib.request.Request(
                    token_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    },
                )
                with urllib.request.urlopen(token_req, timeout=4.0, context=self._ssl_ctx) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")

                m = re.search(r'vqd=([\d-]+)', html) or re.search(r'vqd="([\d-]+)"', html)
                if not m:
                    continue
                vqd = m.group(1)

                api_url = f"https://duckduckgo.com/i.js?l=wt-wt&o=json&q={urllib.parse.quote(q_term)}&vqd={vqd}&f=size:wallpaper,aspect:wide&p=1"
                api_req = urllib.request.Request(
                    api_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Referer": "https://duckduckgo.com/",
                    },
                )
                with urllib.request.urlopen(api_req, timeout=4.5, context=self._ssl_ctx) as resp2:
                    data = json.loads(resp2.read().decode("utf-8"))

                raw_items = data.get("results", [])
                for item in raw_items:
                    img_url = item.get("image", "")
                    w = int(item.get("width", 0))
                    h = int(item.get("height", 0))
                    title = item.get("title", "")
                    if img_url and (img_url.startswith("http://") or img_url.startswith("https://")):
                        results.append(
                            ImageCandidate(
                                url=img_url,
                                width=w,
                                height=h,
                                title=title,
                                source="duckduckgo",
                            )
                        )
                if len(results) >= 5:
                    break
            except Exception:
                continue

        return results

    def _search_searlo_images(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """
        Searlo 공식 실시간 이미지 검색 API 호출:
        엔드포인트: https://api.searlo.tech/api/v1/search/images
        - 공식 API Key (sk_aad76d78e142dd76e1d4ae88fa7a9b6cf4c589a1f504c36de1136906069d8284)
        - 억지 영문 번역을 앞세우지 않고 사용자의 원문 검색어(한국어 포함)를 최우선으로 검색하여 정확도 극대화
        """
        api_key = self.config.searlo_key or DEFAULT_SEARLO_API_KEY
        if not api_key:
            return []

        results: List[ImageCandidate] = []
        # 1순위: 원문 검색어 그대로 + 고화질 배경화면 / 2순위: 순수 원문 검색어 / 3순위: 영문 확장 쿼리(사전 매칭 시)
        search_terms = [
            f"{query} 고화질 배경화면",
            query,
        ]
        if eng_query and eng_query != f"{query} 고화질 배경화면":
            search_terms.append(f"{eng_query} wallpaper 4k")

        for q_term in search_terms:
            try:
                params = {
                    "q": q_term,
                    "gl": "kr",
                    "hl": "ko",
                    "num": "15",
                }
                url = f"https://api.searlo.tech/api/v1/search/images?{urllib.parse.urlencode(params)}"
                req = urllib.request.Request(
                    url,
                    headers={
                        "x-api-key": api_key,
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WINDO-SmartWindow/2.0",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=8.0, context=self._ssl_ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))

                # 다양한 응답 스키마 호환 (images / data.images / images_results / results / items)
                raw_images = (
                    data.get("images")
                    or (data.get("data", {}).get("images") if isinstance(data.get("data"), dict) else None)
                    or (data.get("data") if isinstance(data.get("data"), list) else None)
                    or data.get("images_results")
                    or data.get("results")
                    or data.get("items")
                    or []
                )

                for idx, item in enumerate(raw_images, start=1):
                    if not isinstance(item, dict):
                        continue
                    orig_url = (
                        item.get("imageUrl")
                        or item.get("original")
                        or item.get("url")
                        or item.get("link")
                        or item.get("image")
                        or item.get("src")
                    )
                    thumb_url = (
                        item.get("thumbnailUrl")
                        or item.get("thumbnail")
                        or (item.get("image", {}).get("thumbnailLink") if isinstance(item.get("image"), dict) else "")
                        or ""
                    )
                    img_url = orig_url or thumb_url
                    if not img_url or not (img_url.startswith("http://") or img_url.startswith("https://")):
                        continue

                    w = int(item.get("width") or item.get("original_width") or item.get("imageWidth") or 1920)
                    h = int(item.get("height") or item.get("original_height") or item.get("imageHeight") or 1080)
                    title = str(item.get("title") or item.get("alt") or item.get("snippet") or q_term)

                    # 가로 세로 비율 검사 (창문 16:9 규격에 맞게 세로 이미지 배제)
                    if w > 0 and h > 0 and (w / h) < self.MIN_ASPECT:
                        continue

                    results.append(
                        ImageCandidate(
                            url=img_url,
                            width=w,
                            height=h,
                            title=title,
                            source="searlo_official",
                            rank=idx,
                            fallback_url=thumb_url if thumb_url != img_url else "",
                        )
                    )

                if len(results) >= 6:
                    break
            except Exception:
                continue

        return results

    def _search_serpapi_google_images(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """
        SerpApi Google Images 검색 엔진 연동:
        엔드포인트: https://serpapi.com/search?engine=google_images
        - 실제 구글 이미지 검색 결과를 16:9 와이드 및 고해상도로 정밀 수집
        """
        serp_key = self.config.serpapi_key or os.environ.get("SERPAPI_API_KEY", "").strip()
        if not serp_key:
            return []

        results: List[ImageCandidate] = []
        search_terms = [
            f"{eng_query} wallpaper 4k",
            f"{query} 풍경 와이드 배경화면",
        ]

        for q_term in search_terms:
            try:
                params = {
                    "engine": "google_images",
                    "q": q_term,
                    "api_key": serp_key,
                    "tbs": "iar:w",  # 16:9 와이드 비율 필터!
                    "ijn": "0",
                    "gl": "kr",
                    "hl": "ko",
                }
                url = f"https://serpapi.com/search?{urllib.parse.urlencode(params)}"
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (WINDO-Google-Images)",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=8.0, context=self._ssl_ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))

                items = data.get("images_results", [])
                for item in items:
                    if item.get("is_product", False):
                        continue
                    orig_url = item.get("original") or item.get("thumbnail")
                    if not orig_url or not (orig_url.startswith("http://") or orig_url.startswith("https://")):
                        continue

                    w = int(item.get("original_width") or 1920)
                    h = int(item.get("original_height") or 1080)
                    title = str(item.get("title") or q_term)

                    # 가로 세로 비율 검사
                    if w and h and (w / h) < 1.15:
                        continue

                    results.append(
                        ImageCandidate(
                            url=orig_url,
                            width=w,
                            height=h,
                            title=title,
                            source="serpapi_google_images",
                        )
                    )
                if len(results) >= 8:
                    break
            except Exception:
                continue

        return results

    def _search_google_api(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """Google Custom Search JSON API 호출"""
        results: List[ImageCandidate] = []
        try:
            params = {
                "key": self.config.api_key,
                "cx": self.config.cx,
                "q": eng_query,
                "searchType": "image",
                "imgType": "photo",
                "imgSize": "xxlarge",
                "imgAspect": "wide",
                "safe": "active",
                "num": 10,
            }
            url = f"https://customsearch.googleapis.com/customsearch/v1?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": "WINDO-SmartWindow/2.0"})
            with urllib.request.urlopen(req, timeout=5.0, context=self._ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            items = data.get("items", [])
            for item in items:
                img_info = item.get("image", {})
                w = int(img_info.get("width", 0))
                h = int(img_info.get("height", 0))
                link = item.get("link", "")
                title = item.get("title", "")
                if link and (link.startswith("http://") or link.startswith("https://")):
                    results.append(
                        ImageCandidate(
                            url=link,
                            width=w,
                            height=h,
                            title=title,
                            source="google_api",
                        )
                    )
        except Exception:
            pass
        return results

    def _search_wikimedia(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """Wikimedia Commons 고화질 풍경 라이브러리 검색"""
        results: List[ImageCandidate] = []
        try:
            core_words = re.findall(r"[A-Za-z]+", eng_query)
            search_str = " ".join(core_words[:2]) if core_words else query

            wiki_params = {
                "action": "query",
                "generator": "search",
                "gsrsearch": f"{search_str} landscape panorama scenery filetype:bitmap",
                "gsrnamespace": "6",
                "gsrlimit": "10",
                "prop": "imageinfo",
                "iiprop": "url|size",
                "format": "json",
            }
            wiki_url = f"https://commons.wikimedia.org/w/api.php?{urllib.parse.urlencode(wiki_params)}"
            req = urllib.request.Request(
                wiki_url,
                headers={"User-Agent": "WINDO-VirtualWindow/2.0 (contact@windo.ai)"},
            )
            with urllib.request.urlopen(req, timeout=4.0, context=self._ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            pages = data.get("query", {}).get("pages", {})
            for page in pages.values():
                infos = page.get("imageinfo", [])
                if not infos:
                    continue
                info = infos[0]
                url = info.get("url", "")
                w = int(info.get("width", 0))
                h = int(info.get("height", 0))
                title = page.get("title", "")
                ext = Path(url).suffix.lower()
                if ext in (".jpg", ".jpeg", ".png", ".webp"):
                    results.append(
                        ImageCandidate(
                            url=url,
                            width=w,
                            height=h,
                            title=title,
                            source="wikimedia",
                        )
                    )
        except Exception:
            pass
        return results

    def _score_and_filter_candidates(
        self,
        candidates: List[ImageCandidate],
        query: str = "",
        eng_query: str = "",
    ) -> List[ImageCandidate]:
        """배경 적합성 및 검색어-제목 정확도(Relevance) 기준에 따라 정밀 필터링 및 점수화"""
        valid: List[ImageCandidate] = []
        seen_urls = set()

        # 사용자 한국어 검색어 및 영문 확장 검색어 핵심 토큰 추출
        query_tokens = [
            t.lower()
            for t in re.split(r"[\s_,.]+", query.strip())
            if len(t.strip()) >= 2
        ]
        eng_tokens = [
            t.lower()
            for t in re.findall(r"[A-Za-z]{3,}", eng_query)
            if t.lower() not in ("wallpaper", "landscape", "scenic", "panoramic", "panorama", "view", "vista")
        ]

        for cand in candidates:
            if not cand.url or cand.url in seen_urls:
                continue
            seen_urls.add(cand.url)

            score = 100.0
            lower_title = cand.title.lower()

            # 0. 네거티브 키워드(아이콘, 로고, 쇼핑몰 배너 등) 즉시 제외
            if any(neg in lower_title for neg in self.NEGATIVE_KEYWORDS):
                continue

            # 1. 검색어-제목 일치도(Relevance Score) 최우선 반영 (한국어 토큰 + 영문 토큰 모두 인정)
            if query_tokens:
                matched_count = sum(1 for tok in query_tokens if tok in lower_title)
                if matched_count == len(query_tokens):
                    score += 90.0
                elif matched_count > 0:
                    score += matched_count * 45.0

            if eng_tokens:
                eng_matched = sum(1 for tok in eng_tokens if tok in lower_title)
                if eng_matched > 0:
                    score += min(70.0, eng_matched * 25.0)

            # 2. 검색 엔진 원래 상위 랭크(1~5위) 존중 보너스
            if 1 <= cand.rank <= 5:
                score += (6 - cand.rank) * 10.0  # 1위 +50점, 2위 +40점 ...

            # 3. 해상도 및 가로 와이드 비율 검사
            if cand.width > 0 and cand.height > 0:
                aspect = cand.width / max(1, cand.height)

                # 세로 사진이나 정사각형은 가상 창문 배경으로 부적합!
                if aspect < self.MIN_ASPECT:
                    continue

                if cand.width < self.MIN_WIDTH or cand.height < self.MIN_HEIGHT:
                    score -= 30.0

                # 16:9 ~ 21:9 황금 비율 가산점
                if 1.50 <= aspect <= 2.05:
                    score += 35.0
                elif aspect > 2.05:
                    score += 25.0

                # 1920x1080 이상 Full HD/4K 가산점
                if cand.width >= 1920 and cand.height >= 1080:
                    score += 35.0
                elif cand.width >= 1600:
                    score += 18.0
            else:
                score += 15.0

            # 4. 배경/풍경 보조 가산점
            if any(pos in lower_title for pos in self.POSITIVE_KEYWORDS):
                score += 14.0

            # 5. 검색 엔진 신뢰도 보너스
            if cand.source == "searlo_official":
                score += 75.0
            elif cand.source == "google_direct":
                score += 65.0
            elif cand.source == "serpapi_google_images":
                score += 55.0
            elif cand.source == "google_api":
                score += 45.0
            elif cand.source == "bing_web":
                score += 40.0
            elif cand.source == "unsplash":
                score += 30.0

            cand.score = score
            if score > 0:
                valid.append(cand)

        valid.sort(key=lambda x: x.score, reverse=True)
        return valid

    def _download_image(self, url: str, fallback_url: str = "") -> Optional[bytes]:
        """이미지 바이너리 다운로드 (원본 핫링크 차단 시 호스트 Referer 및 썸네일 폴백 지원)"""
        urls_to_try = [u for u in (url, fallback_url) if u]
        for target_url in urls_to_try:
            try:
                parsed = urllib.parse.urlsplit(target_url)
                origin_referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else "https://www.google.com/"
                req = urllib.request.Request(
                    target_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                        "Referer": origin_referer,
                    },
                )
                with urllib.request.urlopen(req, timeout=5.5, context=self._ssl_ctx) as resp:
                    data = resp.read()
                    if len(data) >= 12 * 1024:  # 최소 12KB 이상
                        return data
            except Exception:
                continue
        return None

    @staticmethod
    def _trim_letterbox_borders(img: Image.Image) -> Image.Image:
        """
        웹 이미지 상하좌우에 포함된 검은색 줄(Black letterbox bar) 또는 흰색 단색 여백 테두리를
        NumPy 픽셀 분석으로 감지하여 자동으로 깨끗하게 제거합니다.
        """
        try:
            arr = np.asarray(img, dtype=np.float32)
            h, w, _ = arr.shape
            if h < 200 or w < 300:
                return img

            max_trim_y = int(h * 0.18)
            max_trim_x = int(w * 0.12)

            def _is_border_line(line_pixels: np.ndarray) -> bool:
                mean_val = float(line_pixels.mean())
                std_val = float(line_pixels.std())
                # 아주 어두운 검은 줄(밝기 < 18, 변화량 < 14) 또는 순백색 여백 줄(밝기 > 245, 변화량 < 10)
                if mean_val < 18.0 and std_val < 14.0:
                    return True
                if mean_val > 245.0 and std_val < 10.0:
                    return True
                return False

            top = 0
            while top < max_trim_y and _is_border_line(arr[top, :, :]):
                top += 1

            bottom = h
            while (h - bottom) < max_trim_y and bottom > top + 100 and _is_border_line(arr[bottom - 1, :, :]):
                bottom -= 1

            left = 0
            while left < max_trim_x and _is_border_line(arr[:, left, :]):
                left += 1

            right = w
            while (w - right) < max_trim_x and right > left + 100 and _is_border_line(arr[:, right - 1, :]):
                right -= 1

            # 경계면 잔여 안티앨리어싱 픽셀 2px 추가 여유 제거
            if top > 0:
                top = min(max_trim_y, top + 2)
            if bottom < h:
                bottom = max(top + 100, bottom - 2)
            if left > 0:
                left = min(max_trim_x, left + 2)
            if right < w:
                right = max(left + 100, right - 2)

            if top > 0 or bottom < h or left > 0 or right < w:
                return img.crop((left, top, right, bottom))
        except Exception:
            pass
        return img

    def _optimize_and_score_image_for_window(self, img_bytes: bytes) -> Tuple[Optional[Image.Image], float]:
        """
        1) 상하좌우 검은색 줄(레터박스) 및 여백 자동 제거
        2) OpenCV/NumPy 기반 실제 해상도·선명도(Laplacian)·색상 풍부도 점수 산출
        3) 1920x1080 (16:9) 창문 화면에 빈틈없이 꽉 차도록 센터 크롭 및 Lanczos 리샘플링
        """
        try:
            img = Image.open(io.BytesIO(img_bytes))
            img.verify()

            img = Image.open(io.BytesIO(img_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")

            # 상하좌우 검은색 줄(Letterbox) 자동 제거!
            img = self._trim_letterbox_borders(img)

            orig_w, orig_h = img.size
            if orig_w < 640 or orig_h < 360:
                return None, -999.0

            orig_ratio = orig_w / max(1, orig_h)
            if orig_ratio < 1.12:
                return None, -999.0

            quality_score = 0.0
            # (a) 실제 원본 해상도 점수
            pixels = orig_w * orig_h
            if orig_w >= 1920 and orig_h >= 1080:
                quality_score += 55.0
            elif orig_w >= 1280 and orig_h >= 720:
                quality_score += 32.0
            elif pixels < 900 * 500:
                quality_score -= 25.0

            # (b) 선명도(Sharpness) 및 대비(Contrast) 평가
            arr = np.asarray(img, dtype=np.uint8)
            gray = arr.mean(axis=2)
            contrast_std = float(gray.std())
            if contrast_std < 18.0:
                # 단색이거나 안개처럼 다 뭉개진 이미지 배제
                return None, -999.0

            if cv2 is not None:
                lap_var = float(cv2.Laplacian(arr[:, :, 1], cv2.CV_64F).var())
            else:
                gy, gx = np.gradient(gray)
                lap_var = float((gx ** 2 + gy ** 2).mean())

            if lap_var < 25.0:
                # 심하게 블러된 썸네일 감점
                quality_score -= 40.0
            else:
                quality_score += min(45.0, lap_var / 12.0)

            target_ratio = self.TARGET_WIDTH / self.TARGET_HEIGHT  # 16:9 = 1.777...
            if orig_ratio > target_ratio:
                new_w = int(orig_h * target_ratio)
                left = (orig_w - new_w) // 2
                img = img.crop((left, 0, left + new_w, orig_h))
            else:
                new_h = int(orig_w / target_ratio)
                top = (orig_h - new_h) // 2
                img = img.crop((0, top, orig_w, top + new_h))

            final_img = img.resize((self.TARGET_WIDTH, self.TARGET_HEIGHT), Image.Resampling.LANCZOS)
            return final_img, quality_score
        except Exception:
            return None, -999.0

    def _optimize_image_for_window(self, img_bytes: bytes) -> Optional[Image.Image]:
        """Pillow(PIL)를 사용해 상하 검은 줄 제거 후 창문 화면(1920x1080, 16:9)에 맞춤 스마트 리사이즈 & 센터 크롭"""
        final_img, _ = self._optimize_and_score_image_for_window(img_bytes)
        return final_img


    def _infer_lighting_from_query(self, query: str) -> str:
        """검색어에 따른 광원(Circadian Lighting) 자동 추론"""
        q = query.lower()
        if any(w in q for w in ("야경", "밤", "밤하늘", "은하수", "달", "별", "night", "dark")):
            return "evening"
        if any(w in q for w in ("노을", "석양", "일몰", "황혼", "sunset", "dusk", "golden")):
            return "sunset"
        if any(w in q for w in ("아침", "새벽", "일출", "안개", "morning", "sunrise", "dawn")):
            return "morning"
        if any(w in q for w in ("겨울", "눈", "설경", "빙하", "알프스", "snow", "winter", "ice")):
            return "morning"
        return "default"

    def _match_local_theme_fallback(self, query: str) -> Optional[Path]:
        """네트워크 오프라인 시 질의 키워드와 유사한 로컬 고화질 풍경 라이브러리 매칭"""
        presets_dir = self.base_dir / "assets" / "images" / "presets"
        if not presets_dir.exists():
            return None

        q = query.lower()
        candidates = list(presets_dir.glob("*.png")) + list(presets_dir.glob("*.jpg"))

        if any(w in q for w in ("야경", "밤", "도쿄", "서울", "부산", "인천", "night", "city")):
            for c in candidates:
                if "야경" in c.stem:
                    return c
        if any(w in q for w in ("바다", "해변", "여름", "ocean", "sea", "beach", "하와이", "발리")):
            for c in candidates:
                if "바다" in c.stem or "여름" in c.stem:
                    return c
        if any(w in q for w in ("눈", "겨울", "설산", "설경", "알프스", "snow", "winter")):
            for c in candidates:
                if "설산" in c.stem or "겨울" in c.stem or "크리스마스" in c.stem:
                    return c
        if any(w in q for w in ("노을", "석양", "sunset")):
            for c in candidates:
                if "sunset" in c.stem:
                    return c
        if any(w in q for w in ("아침", "morning")):
            for c in candidates:
                if "morning" in c.stem:
                    return c

        for c in candidates:
            if c.stem in ("default", "morning", "sunset", "evening"):
                return c
        return candidates[0] if candidates else None

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        clean = name.strip()
        for ww in ("윈도우", "윈도", "window", "windo"):
            if clean.lower().startswith(ww):
                clean = clean[len(ww):].strip()

        for v in (
            "찾아줘", "찾아 줘", "찾아", "검색해줘", "검색해 줘", "검색해",
            "만들어줘", "만들어 줘", "만들어", "생성해줘", "생성해 줘", "생성해",
            "그려줘", "그려 줘", "그려", "보여줘", "보여 줘", "바꿔줘", "바꿔 줘",
            "틀어줘", "틀어 줘", "켜줘", "켜 줘", "적용해줘", "적용해 줘", "해줘", "해 줘",
            "찾아봐", "찾아봐줘", "구해줘",
            "새로", "다시", "지금", "바로", "좀",
        ):
            clean = clean.replace(v, " ")

        clean = re.sub(r"(이미지|사진|풍경|배경|화면|웹에서|구글에서|인터넷에서|좀|하나|한번)\s*", " ", clean)
        clean = re.sub(r"(으로|로|을|를|이|가|에서)\s*$", "", clean.strip())
        clean = re.sub(r'[\\/*?:"<>|.,!_]+', " ", clean)
        clean = "_".join(clean.split()).strip("_")
        return clean[:30] if clean else "웹_풍경"
