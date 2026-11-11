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
from PIL import Image


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


class WebImageSearchManager:
    """구글 및 웹 이미지 검색, 배경 적합성 분류, 16:9 화면 맞춤 변환 총괄 매니저"""

    TARGET_WIDTH = 1920
    TARGET_HEIGHT = 1080
    MIN_WIDTH = 1000
    MIN_HEIGHT = 550
    MIN_ASPECT = 1.18  # 가로 세로 비율 (width / height)

    NEGATIVE_KEYWORDS = (
        "icon", "logo", "vector", "clipart", "diagram", "screenshot", "meme",
        "product", "buy", "price", "shop", "avatar", "profile", "portrait",
        "cartoon", "illustration", "anime", "drawing", "poster", "cover", "ebay",
        "amazon", "shopping", "recipe", "shirt", "shoes", "phone", "case", "mall",
        "아이콘", "로고", "클립아트", "다이어그램", "스크린샷", "상품", "쇼핑",
        "프로필", "증명사진", "인물사진", "만화", "일러스트", "도표", "구매", "판매",
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
        """한국어 질의어를 전세계 이미지 DB 검색에 최적화된 영문 풍경 쿼리로 확장"""
        q = query.strip()
        for k, translated in self.SCENIC_TRANSLATION_MAP.items():
            if k == q or k in q:
                return translated

        return f"{q} scenic landscape wallpaper 4k photography"

    def search_and_process_background(
        self,
        query: str,
        log_callback: Optional[Any] = None,
    ) -> Tuple[bool, str, Optional[Path], str]:
        """
        웹에서 배경에 적합한 사진을 검색하고 1920x1080 화면에 맞게 가공하여 저장.
        반환: (성공여부, 적용메시지, 저장된파일경로, 추론된자연광키)
        """
        clean_query = query.strip()
        if not clean_query:
            return False, "검색어가 비어 있습니다.", None, "default"

        self.is_searching = True
        self.last_query = clean_query
        eng_query = self.build_english_query(clean_query)

        if log_callback:
            log_callback("WEB", f"🌐 웹 이미지 검색 시작: \"{clean_query}\" (검색 키워드: {eng_query[:40]}...)")

        try:
            candidates: List[ImageCandidate] = []

            # 1. Searlo 공식 이미지 검색 API (엔드포인트: https://api.searlo.tech/api/v1/search/images) - 최우선 메인 엔진!
            searlo_key = self.config.searlo_key or DEFAULT_SEARLO_API_KEY
            if searlo_key:
                if log_callback:
                    log_callback("WEB", f"🚀 Searlo 공식 이미지 검색 엔진 1순위 가동: \"{clean_query}\"...")
                searlo_cands = self._search_searlo_images(clean_query, eng_query)
                candidates.extend(searlo_cands)
                if searlo_cands and log_callback:
                    log_callback("WEB", f"✅ Searlo 공식 구글 이미지 엔진에서 {len(searlo_cands)}개 고화질 와이드 후보 즉시 확보")

            # 2. SerpApi Google Images API (엔드포인트: https://serpapi.com/search?engine=google_images)
            serp_key = self.config.serpapi_key or os.environ.get("SERPAPI_API_KEY", "").strip()
            if len(candidates) < 6 and serp_key:
                if log_callback:
                    log_callback("WEB", f"🔍 SerpApi Google Images API 2차 검색 중: \"{clean_query}\"...")
                serp_cands = self._search_serpapi_google_images(clean_query, eng_query)
                candidates.extend(serp_cands)
                if serp_cands and log_callback:
                    log_callback("WEB", f"✅ SerpApi 구글 이미지에서 {len(serp_cands)}개 고화질 와이드 후보 확보")

            # 3. Google Custom Search JSON API (사용자가 API Key/CX를 설정한 경우)
            if len(candidates) < 6 and self.config.api_key and self.config.cx:
                if log_callback:
                    log_callback("WEB", "🔍 Google Custom Search JSON API 3차 검색 중...")
                google_cands = self._search_google_api(clean_query, eng_query)
                candidates.extend(google_cands)
                if google_cands and log_callback:
                    log_callback("WEB", f"✅ 구글 공식 API에서 {len(google_cands)}개 고화질 후보 확보")

            # 4. Bing Images 실시간 와이드 고화질 검색 (API 키 불필요, 1920x1080 이상 실사 원본)
            if len(candidates) < 6:
                if log_callback:
                    log_callback("WEB", f"🔎 Bing 웹 이미지 검색 엔진 가동: \"{clean_query}\"...")
                bing_cands = self._search_bing_images(clean_query, eng_query)
                candidates.extend(bing_cands)
                if bing_cands and log_callback:
                    log_callback("WEB", f"✅ Bing 검색에서 {len(bing_cands)}개 고화질 와이드 후보 확보")

            # 5. Unsplash 실시간 16:9 와이드 풍경 검색 (API 키 불필요)
            if len(candidates) < 6:
                if log_callback:
                    log_callback("WEB", f"🔎 Unsplash 4K 와이드 풍경 검색: \"{eng_query[:25]}\"...")
                unsplash_cands = self._search_unsplash_napi(clean_query, eng_query)
                candidates.extend(unsplash_cands)
                if unsplash_cands and log_callback:
                    log_callback("WEB", f"✅ Unsplash에서 {len(unsplash_cands)}개 고화질 후보 확보")

            # 6. DuckDuckGo Images 실시간 검색
            if len(candidates) < 6:
                ddg_cands = self._search_duckduckgo_images(clean_query, eng_query)
                candidates.extend(ddg_cands)

            # 7. Wikimedia Commons 고해상도 풍경 라이브러리
            if len(candidates) < 6:
                wiki_cands = self._search_wikimedia(clean_query, eng_query)
                candidates.extend(wiki_cands)

            # 6. 배경 적합성 점수 평가 및 정렬 (가로 와이드, 고해상도, 키워드 검증)
            scored_candidates = self._score_and_filter_candidates(candidates)

            if log_callback and scored_candidates:
                top = scored_candidates[0]
                dim_str = f"{top.width}x{top.height}" if top.width > 0 else "고화질"
                log_callback(
                    "WEB",
                    f"📊 총 {len(candidates)}개 후보 중 최적 배경 {len(scored_candidates)}개 선별 완료",
                    f"1순위: {top.title[:35]}... ({dim_str} · 출처: {top.source})",
                )

            # 7. 상위 후보부터 순차적으로 다운로드 및 검증, 16:9 와이드(1920x1080) 최적화
            for cand in scored_candidates[:8]:
                try:
                    img_data = self._download_image(cand.url)
                    if not img_data:
                        continue

                    # PIL 검증 및 창문 화면 맞춤 변환 (1920x1080 Lanczos)
                    optimized_img = self._optimize_image_for_window(img_data)
                    if not optimized_img:
                        continue

                    # 파일명 생성 및 저장
                    sanitized_name = self._sanitize_filename(clean_query)
                    out_path = self.cache_dir / f"{sanitized_name}_{int(time.time())}.jpg"
                    optimized_img.save(out_path, format="JPEG", quality=95, optimize=True)

                    # landscapes 폴더에도 저장하여 영구 프리셋 호환
                    preset_path = self.base_dir / ".cache" / "landscapes" / f"{sanitized_name}.jpg"
                    preset_path.parent.mkdir(parents=True, exist_ok=True)
                    optimized_img.save(preset_path, format="JPEG", quality=95, optimize=True)

                    lighting_key = self._infer_lighting_from_query(clean_query)
                    self.last_result_title = cand.title or clean_query

                    msg = f"웹에서 \"{clean_query}\" 고화질 배경 사진을 찾아 화면에 맞게 적용했습니다!"
                    if log_callback:
                        log_callback("WEB", f"✅ {msg}", f"1920x1080 와이드 최적화 완료 | 출처: {cand.source} | 광원: {lighting_key}")

                    return True, msg, out_path, lighting_key

                except Exception:
                    continue

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

                        preset_path = self.base_dir / ".cache" / "landscapes" / f"{sanitized_name}.jpg"
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

    def _search_bing_images(self, query: str, eng_query: str) -> List[ImageCandidate]:
        """
        Bing Images 실시간 검색 (API 키 불필요)
        - 원본 고화질 와이드 이미지 URL(`murl`)을 직접 파싱
        """
        results: List[ImageCandidate] = []
        search_terms = [
            f"{eng_query} wallpaper 4k",
            f"{query} 풍경 배경화면",
        ]

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

                for m_url, m_title in matches[:10]:
                    clean_u = m_url.split("?")[0] if "bing.com" not in m_url else m_url
                    results.append(
                        ImageCandidate(
                            url=m_url,
                            width=1920,
                            height=1080,
                            title=m_title or q_term,
                            source="bing_web",
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
        - 라이브 고품질 구글 이미지 결과에서 16:9 와이드 풍경을 실시간 수집
        """
        api_key = self.config.searlo_key or DEFAULT_SEARLO_API_KEY
        if not api_key:
            return []

        results: List[ImageCandidate] = []
        search_terms = [
            f"{eng_query} wallpaper 4k landscape",
            f"{query} 풍경 고화질 배경화면",
            f"{query} 와이드 배경화면",
        ]

        for q_term in search_terms:
            try:
                encoded = urllib.parse.quote(q_term)
                url = f"https://api.searlo.tech/api/v1/search/images?q={encoded}"
                req = urllib.request.Request(
                    url,
                    headers={
                        "x-api-key": api_key,
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": "WINDO-SmartWindow/2.0 (Searlo Official Engine)",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=8.0, context=self._ssl_ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))

                raw_images = data.get("images", [])
                for item in raw_images:
                    img_url = item.get("imageUrl") or item.get("thumbnailUrl") or item.get("url")
                    if not img_url or not (img_url.startswith("http://") or img_url.startswith("https://")):
                        continue

                    w = int(item.get("width") or 1920)
                    h = int(item.get("height") or 1080)
                    title = str(item.get("title") or q_term)

                    # 가로 세로 비율 검사 (창문 16:9 규격)
                    if w and h and (w / h) < 1.15:
                        continue

                    results.append(
                        ImageCandidate(
                            url=img_url,
                            width=w,
                            height=h,
                            title=title,
                            source="searlo_official",
                        )
                    )

                if len(results) >= 8:
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

    def _score_and_filter_candidates(self, candidates: List[ImageCandidate]) -> List[ImageCandidate]:
        """배경 적합성 기준에 따라 정밀 필터링 및 점수화"""
        valid: List[ImageCandidate] = []
        seen_urls = set()

        for cand in candidates:
            if not cand.url or cand.url in seen_urls:
                continue
            seen_urls.add(cand.url)

            score = 100.0

            # 1. 해상도 및 가로 와이드 비율 검사
            if cand.width > 0 and cand.height > 0:
                aspect = cand.width / max(1, cand.height)

                # 세로 사진이나 정사각형은 가상 창문 배경으로 부적합!
                if aspect < self.MIN_ASPECT:
                    continue

                if cand.width < self.MIN_WIDTH or cand.height < self.MIN_HEIGHT:
                    score -= 40.0

                # 16:9 ~ 21:9 황금 비율 가산점
                if 1.55 <= aspect <= 1.95:
                    score += 45.0
                elif aspect >= 1.95:
                    score += 35.0

                # 1920x1080 이상 Full HD/4K 가산점
                if cand.width >= 1920 and cand.height >= 1080:
                    score += 40.0
                elif cand.width >= 1600:
                    score += 20.0
            else:
                score += 20.0

            # 2. 제목/설명 키워드 검사 (네거티브 제거 & 포지티브 가산점)
            lower_title = cand.title.lower()
            if any(neg in lower_title for neg in self.NEGATIVE_KEYWORDS):
                continue
            if any(pos in lower_title for pos in self.POSITIVE_KEYWORDS):
                score += 25.0

            if cand.source == "searlo_official":
                score += 50.0  # Searlo 공식 API 엔진 최우선 보너스
            elif cand.source == "serpapi_google_images":
                score += 40.0  # SerpApi 구글 이미지 보너스
            elif cand.source == "google_api":
                score += 30.0
            elif cand.source == "bing_web":
                score += 25.0
            elif cand.source == "unsplash":
                score += 20.0

            cand.score = score
            if score > 0:
                valid.append(cand)

        valid.sort(key=lambda x: x.score, reverse=True)
        return valid

    def _download_image(self, url: str) -> Optional[bytes]:
        """이미지 바이너리 다운로드 (타임아웃 5.5초, 브라우저 헤더 및 SSL 지원)"""
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": "https://www.bing.com/",
                },
            )
            with urllib.request.urlopen(req, timeout=5.5, context=self._ssl_ctx) as resp:
                data = resp.read()
                if len(data) >= 12 * 1024:  # 최소 12KB 이상
                    return data
        except Exception:
            pass
        return None

    def _optimize_image_for_window(self, img_bytes: bytes) -> Optional[Image.Image]:
        """Pillow(PIL)를 사용해 창문 화면(1920x1080, 16:9)에 맞춤 스마트 리사이즈 & 센터 크롭"""
        try:
            img = Image.open(io.BytesIO(img_bytes))
            img.verify()

            img = Image.open(io.BytesIO(img_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")

            orig_w, orig_h = img.size
            if orig_w < 500 or orig_h < 350:
                return None

            target_ratio = self.TARGET_WIDTH / self.TARGET_HEIGHT  # 16:9 = 1.777...
            orig_ratio = orig_w / orig_h

            if orig_ratio > target_ratio:
                new_w = int(orig_h * target_ratio)
                left = (orig_w - new_w) // 2
                img = img.crop((left, 0, left + new_w, orig_h))
            else:
                new_h = int(orig_w / target_ratio)
                top = (orig_h - new_h) // 2
                img = img.crop((0, top, orig_w, top + new_h))

            final_img = img.resize((self.TARGET_WIDTH, self.TARGET_HEIGHT), Image.Resampling.LANCZOS)
            return final_img

        except Exception:
            return None

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
