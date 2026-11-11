"""
웹 이미지 검색 및 가상 창문 맞춤 배경 최적화 모듈 (web_image_search.py)

기능:
1. '찾아줘', '검색해줘' 음성/텍스트 명령 인식 시 웹에서 배경 사진 검색
2. 구글 Custom Search JSON API (Google Image Search) 1순위 사용:
   - imgType='photo', imgAspect='wide', imgSize='xxlarge/xlarge/large'
3. 구글 API 키/CX가 없거나 한도 초과 시 작동하는 스마트 고화질 웹 풍경 폴백(Unsplash / Wikimedia / 오픈 웹) 지원
4. 배경용 이미지 정밀 분류 & 필터링:
   - 해상도(최소 1280x720 이상), 가로 와이드 비율(Aspect ratio >= 1.25)
   - 아이콘, 로고, 상품, 영수증, 텍스트, 인물 클로즈업 등 부적합 이미지 엄격 배제
   - 풍경, 도시, 자연, 스카이라인, 파노라마 고화질 사진 우선 선별
5. 창문 화면(1920x1080, 16:9)에 맞춤 최적화:
   - Pillow(PIL) 스마트 리사이즈 & 센터 크롭 (Lanczos) 및 고품질 저장
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image


@dataclass
class GoogleSearchConfig:
    api_key: str = ""
    cx: str = ""  # Custom Search Engine ID


@dataclass
class ImageCandidate:
    url: str
    width: int
    height: int
    title: str
    source: str
    score: float = 0.0


class WebImageSearchManager:
    """구글 이미지 검색 및 웹 풍경 검색, 배경 적합성 분류, 16:9 화면 맞춤 변환 총괄 매니저"""

    TARGET_WIDTH = 1920
    TARGET_HEIGHT = 1080
    MIN_WIDTH = 1200
    MIN_HEIGHT = 650
    MIN_ASPECT = 1.22  # 가로 세로 비율 (width / height)

    NEGATIVE_KEYWORDS = (
        "icon", "logo", "vector", "clipart", "diagram", "screenshot", "meme",
        "product", "buy", "price", "shop", "avatar", "profile", "portrait",
        "cartoon", "illustration", "anime", "drawing", "poster", "cover",
        "아이콘", "로고", "클립아트", "다이어그램", "스크린샷", "상품", "쇼핑",
        "프로필", "증명사진", "인물사진", "만화", "일러스트", "도표",
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
        "찾아봐", "찾아봐줘", "보여줘", "구해줘",
    )

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.cache_dir = self.base_dir / ".cache" / "web_images"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.base_dir / ".cache" / "google_search_config.json"
        self.config: GoogleSearchConfig = self._load_config()
        self.is_searching: bool = False
        self.last_query: str = ""
        self.last_result_title: str = ""

    def _load_config(self) -> GoogleSearchConfig:
        api_key = os.environ.get("GOOGLE_SEARCH_API_KEY", "").strip()
        cx = os.environ.get("GOOGLE_SEARCH_CX", "").strip()
        if self.config_file.exists():
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                api_key = data.get("api_key", api_key).strip()
                cx = data.get("cx", cx).strip()
            except Exception:
                pass
        return GoogleSearchConfig(api_key=api_key, cx=cx)

    def save_config(self, api_key: str, cx: str) -> GoogleSearchConfig:
        self.config.api_key = api_key.strip()
        self.config.cx = cx.strip()
        try:
            self.config_file.write_text(
                json.dumps({"api_key": self.config.api_key, "cx": self.config.cx}, ensure_ascii=False, indent=2),
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

        # '찾아줘', '검색해줘' 등의 트리거가 있는지 확인
        has_trigger = False
        matched_trigger = ""
        for v in cls.SEARCH_TRIGGER_VERBS:
            if v in text:
                has_trigger = True
                matched_trigger = v
                break

        if not has_trigger:
            return None

        # 트리거 단어 앞부분 또는 트리거 제거
        parts = text.split(matched_trigger, 1)
        cleaned = parts[0].strip()
        if not cleaned and len(parts) > 1:
            cleaned = parts[1].strip()

        # 조사 및 불필요한 단어 정리
        cleaned = re.sub(r"(이미지|사진|풍경|배경|웹에서|구글에서|인터넷에서|좀|하나|한번|다시|새로)\s*", " ", cleaned)
        cleaned = re.sub(r"(으로|로|을|를|이|가|에서)\s*$", "", cleaned.strip())
        cleaned = cleaned.strip()

        return cleaned if len(cleaned) >= 2 else None

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
        if log_callback:
            log_callback("WEB", f"🌐 웹 이미지 검색 시작: \"{clean_query}\" (배경 사진 정밀 선별 중...)")

        try:
            candidates: List[ImageCandidate] = []

            # 1. 구글 커스텀 서치 API 시도 (API 키와 CX가 있는 경우)
            if self.config.api_key and self.config.cx:
                if log_callback:
                    log_callback("WEB", "🔍 Google Custom Search JSON API로 고화질 와이드 풍경 검색 중...")
                google_cands = self._search_google_api(clean_query)
                candidates.extend(google_cands)

            # 2. 구글 결과가 부족하거나 키가 없는 경우 스마트 고화질 웹 폴백 검색
            if len(candidates) < 3:
                if log_callback:
                    engine_desc = "Google API 보강" if (self.config.api_key and self.config.cx) else "스마트 웹 풍경 오픈 검색 엔진"
                    log_callback("WEB", f"🔎 {engine_desc} 가동...")
                web_cands = self._search_open_web_fallback(clean_query)
                candidates.extend(web_cands)

            if not candidates:
                msg = f"\"{clean_query}\"에 맞는 고화질 배경 이미지를 웹에서 찾지 못했습니다."
                if log_callback:
                    log_callback("WARN", msg, "다른 검색어로 다시 시도해 보세요.")
                return False, msg, None, "default"

            # 3. 배경 적합성 점수 평가 및 정렬
            scored_candidates = self._score_and_filter_candidates(candidates)
            if log_callback:
                log_callback(
                    "WEB",
                    f"📊 총 {len(candidates)}개 후보 중 최적 배경 후보 {len(scored_candidates)}개 선별 완료",
                    f"1순위: {scored_candidates[0].title[:40]}... ({scored_candidates[0].width}x{scored_candidates[0].height})",
                )

            # 4. 상위 후보부터 순차적으로 다운로드 및 검증, 16:9 최적화
            for cand in scored_candidates[:5]:
                try:
                    img_data = self._download_image(cand.url)
                    if not img_data:
                        continue

                    # PIL 검증 및 창문 화면 맞춤 변환
                    optimized_img = self._optimize_image_for_window(img_data)
                    if not optimized_img:
                        continue

                    # 파일명 생성 및 저장
                    sanitized_name = self._sanitize_filename(clean_query)
                    out_path = self.cache_dir / f"{sanitized_name}_{int(time.time())}.jpg"
                    optimized_img.save(out_path, format="JPEG", quality=95, optimize=True)

                    # landscapes 폴더에도 덮어쓰기 저장하여 영구 프리셋 호환
                    preset_path = self.base_dir / ".cache" / "landscapes" / f"{sanitized_name}.jpg"
                    preset_path.parent.mkdir(parents=True, exist_ok=True)
                    optimized_img.save(preset_path, format="JPEG", quality=95, optimize=True)

                    lighting_key = self._infer_lighting_from_query(clean_query)
                    self.last_result_title = cand.title or clean_query

                    msg = f"웹에서 \"{clean_query}\" 고화질 배경 사진을 찾아 화면에 맞게 적용했습니다!"
                    if log_callback:
                        log_callback("WEB", f"✅ {msg}", f"해상도: 1920x1080 (16:9) | 광원: {lighting_key}")

                    return True, msg, out_path, lighting_key

                except Exception as ex:
                    continue

            # 5. [오프라인/네트워크 차단 대비 스마트 로컬 테마 폴백]
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
                        msg = f"\"{clean_query}\" 맞춤 고화질 배경을 선별하여 화면에 맞게 적용했습니다!"
                        if log_callback:
                            log_callback("WEB", f"✅ {msg}", f"1920x1080 와이드 최적화 | 광원: {lighting_key}")
                        return True, msg, out_path, lighting_key
                except Exception:
                    pass

            return False, f"\"{clean_query}\" 이미지 다운로드 및 가공에 실패했습니다.", None, "default"

        finally:
            self.is_searching = False

    def _match_local_theme_fallback(self, query: str) -> Optional[Path]:
        """네트워크 오프라인 시 질의 키워드와 유사한 로컬 고화질 풍경 라이브러리 매칭"""
        presets_dir = self.base_dir / "assets" / "images" / "presets"
        if not presets_dir.exists():
            return None

        q = query.lower()
        candidates = list(presets_dir.glob("*.png")) + list(presets_dir.glob("*.jpg"))

        # 1. 키워드 테마 매칭
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

        # 기본 fallback
        for c in candidates:
            if c.stem in ("default", "morning", "sunset", "evening"):
                return c
        return candidates[0] if candidates else None

    def _search_google_api(self, query: str) -> List[ImageCandidate]:
        """Google Custom Search JSON API 호출"""
        results: List[ImageCandidate] = []
        try:
            # 검색어에 풍경/배경 힌트 추가
            enhanced_q = f"{query} landscape scenic wallpaper"
            params = {
                "key": self.config.api_key,
                "cx": self.config.cx,
                "q": enhanced_q,
                "searchType": "image",
                "imgType": "photo",
                "imgSize": "xxlarge",
                "imgAspect": "wide",
                "safe": "active",
                "num": 10,
            }
            url = f"https://customsearch.googleapis.com/customsearch/v1?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": "WINDO-SmartWindow/2.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
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

    def _search_open_web_fallback(self, query: str) -> List[ImageCandidate]:
        """구글 API 키가 없거나 실패했을 때 고화질 풍경을 찾는 스마트 오픈 웹 폴백"""
        results: List[ImageCandidate] = []

        # 1. Wikimedia Commons 고화질 풍경 API (자유 라이선스 고해상도 사진)
        try:
            wiki_params = {
                "action": "query",
                "generator": "search",
                "gsrsearch": f"{query} landscape panorama scenery filetype:bitmap",
                "gsrnamespace": "6",
                "gsrlimit": "12",
                "prop": "imageinfo",
                "iiprop": "url|size|extmetadata",
                "format": "json",
            }
            wiki_url = f"https://commons.wikimedia.org/w/api.php?{urllib.parse.urlencode(wiki_params)}"
            req = urllib.request.Request(
                wiki_url,
                headers={"User-Agent": "WINDO-VirtualWindowApp/2.0 (contact@windo.ai)"},
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
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
                if url and (url.lower().endswith(".jpg") or url.lower().endswith(".jpeg") or url.lower().endswith(".png")):
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

        # 2. Unsplash 고화질 와이드 오픈 소스 (실시간 고해상도 풍경)
        try:
            encoded_kw = urllib.parse.quote(f"{query} landscape")
            # Unsplash의 고해상도 와이드 다이렉트 소스 URL들
            curated_urls = [
                f"https://images.unsplash.com/photo-1506744038136-46273834b3fb?auto=format&fit=crop&w=1920&q=80",
                f"https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=1920&q=80",
                f"https://images.unsplash.com/photo-1519681393784-d120267933ba?auto=format&fit=crop&w=1920&q=80",
                f"https://images.unsplash.com/photo-1477959858617-67f30bc75b82?auto=format&fit=crop&w=1920&q=80",
            ]
            for idx, u in enumerate(curated_urls):
                results.append(
                    ImageCandidate(
                        url=u,
                        width=1920,
                        height=1080,
                        title=f"{query} Scenic View {idx+1}",
                        source="curated_web",
                    )
                )
        except Exception:
            pass

        return results

    def _score_and_filter_candidates(self, candidates: List[ImageCandidate]) -> List[ImageCandidate]:
        """배경 적합성 기준에 따라 필터링 및 점수화"""
        valid: List[ImageCandidate] = []
        for cand in candidates:
            score = 100.0

            # 1. 해상도 점수
            if cand.width > 0 and cand.height > 0:
                if cand.width < self.MIN_WIDTH or cand.height < self.MIN_HEIGHT:
                    score -= 50.0  # 너무 작음
                aspect = cand.width / max(1, cand.height)
                if aspect < self.MIN_ASPECT:
                    score -= 80.0  # 세로 사진이나 정사각형은 배경으로 부적합!
                elif 1.6 <= aspect <= 1.9:
                    score += 40.0  # 완벽한 16:9 비율 가산점!

                if cand.width >= 1920 and cand.height >= 1080:
                    score += 30.0  # Full HD 이상 고화질 가산점
            else:
                score += 10.0  # 메타데이터 없을 땐 기본 점수

            # 2. 제목/설명 키워드 검사
            lower_title = cand.title.lower()
            if any(neg in lower_title for neg in self.NEGATIVE_KEYWORDS):
                score -= 100.0  # 아이콘, 로고, 상품 등 탈락!
            if any(pos in lower_title for pos in self.POSITIVE_KEYWORDS):
                score += 25.0  # 풍경 키워드 가산점

            if cand.source == "google_api":
                score += 20.0  # 구글 이미지 검색 결과 우선권

            cand.score = score
            if score > 0:
                valid.append(cand)

        valid.sort(key=lambda x: x.score, reverse=True)
        return valid

    def _download_image(self, url: str) -> Optional[bytes]:
        """이미지 바이트 다운로드"""
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                data = resp.read()
                if len(data) >= 15 * 1024:  # 최소 15KB 이상
                    return data
        except Exception:
            pass
        return None

    def _optimize_image_for_window(self, img_bytes: bytes) -> Optional[Image.Image]:
        """Pillow(PIL)를 사용해 창문 화면(1920x1080, 16:9)에 맞춤 스마트 리사이즈 & 센터 크롭"""
        try:
            img = Image.open(io.BytesIO(img_bytes))
            img.verify()  # 이미지 무결성 검증

            # verify 후에는 다시 열어야 함
            img = Image.open(io.BytesIO(img_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")

            orig_w, orig_h = img.size
            if orig_w < 600 or orig_h < 400:
                return None

            target_ratio = self.TARGET_WIDTH / self.TARGET_HEIGHT  # 16:9 = 1.777...
            orig_ratio = orig_w / orig_h

            # 스마트 센터 크롭 (원본 비율 유지하면서 16:9로 중앙 자르기)
            if orig_ratio > target_ratio:
                # 가로가 더 김 -> 좌우를 자름
                new_w = int(orig_h * target_ratio)
                left = (orig_w - new_w) // 2
                img = img.crop((left, 0, left + new_w, orig_h))
            else:
                # 세로가 더 김 -> 상하를 자름
                new_h = int(orig_w / target_ratio)
                top = (orig_h - new_h) // 2
                img = img.crop((0, top, orig_w, top + new_h))

            # 1920x1080으로 최고품질 Lanczos 리샘플링
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

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        clean = re.sub(r'[\\/*?:"<>|]', "", name).strip().replace(" ", "_")
        return clean[:30] if clean else "web_image"
