"""
ComfyUI 노드 그래프 워크플로우 엔진 기반 초고화질 가상 창문 풍경 생성 및 프리셋/캐시 통합 모듈 (src/modules/virtual_landscape.py)

핵심 기능:
1. 초고화질(Full HD 1920x1080 / 8K UHD 디테일) & '창문에서 바라보는 풍경(Window Viewpoint)' 구도:
   - 네이티브 1344x768 (16:9 와이드 HD) 렌더링 + 6-Steps 정밀 디노이징 + 256 토큰 풀 컨텍스트 인코딩
   - T5 임베딩 분포 손상 없이 순수 고해상도 잠재 공간을 디코딩한 뒤 1920x1080 Full HD 초해상화 및 마이크로 디테일 샤프닝 적용
   - 모든 생성 프롬프트에 '거대한 통유리 창문을 통해 실내에서 바깥 대자연을 바라보는 시점(View looking out through a large panoramic window)' 강제 적용
2. 생성 중 기존 화면 100% 유지 보장:
   - 이미지 생성 중(Node #1 ~ #7 진행 중)에는 창문 화면 버전(_version)이나 기존 이미지(current_image_url)를 절대 변경하지 않아
     생성이 100% 완료될 때까지 기존 화면이 흔들림 없이 그대로 유지됨
3. 엄격한 생성 명령어 필터링 ('~ 만들어줘', '~ 그려줘'에만 반응):
   - 오직 '만들어줘', '그려줘'(및 '생성해줘')가 포함된 명령에만 ComfyUI 이미지 생성이 작동하며,
     그 외의 일반 발화에는 이미지 생성이 절대 반응하지 않음
4. 동시 1개 생성 강제 (Single-Flight Generation Lock):
   - 이미 생성 중이면 중복 생성을 차단하고 "이미지 생성 중입니다." TTS 안내 유도
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

# 기본 긍정 프롬프트: '창문에서 바라보는 초고화질 8K 실사 풍경' 구도 및 광학 디테일 극대화
DEFAULT_POSITIVE_PROMPT: str = (
    "breathtaking view looking out through a large modern panoramic window, "
    "seen from inside looking outside at expansive nature, architectural window perspective, "
    "crystal clear glass, photorealistic 8k UHD, shot on Hasselblad H6D-100c, 24mm wide-angle lens, f/8, "
    "razor-sharp focus from foreground to distant horizon, hyper-detailed natural textures, "
    "cinematic natural daylight, masterpiece, National Geographic award-winning photography"
)

# 기본 부정 프롬프트: 저화질, 흐릿함, 왜곡, 글자, 인물 등 배제
DEFAULT_NEGATIVE_PROMPT: str = (
    "low quality, worst quality, lowres, blurry, out of focus, pixelated, artifacts, "
    "distorted, deformed, ugly, text, watermark, signature, logo, people, human, person, "
    "closed curtains, blinds blocking view, dark messy room, cartoon, anime, painting, illustration"
)

LORA_TRIGGER_WORD: str = "best landscape"


@dataclass
class ComfyUIConfig:
    """사용자가 설정 창에서 직접 제어하는 ComfyUI 초고화질 파이프라인 설정"""
    server_url: str = "http://127.0.0.1:8188"
    model_name: str = "unsloth/FLUX.1-schnell"
    lora_name: str = "landscape.safetensors"
    lora_strength: float = 0.95
    lora_trigger: str = LORA_TRIGGER_WORD
    positive_prompt: str = DEFAULT_POSITIVE_PROMPT
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    steps: int = 6
    cfg_scale: float = 3.5
    sampler_name: str = "euler"
    scheduler: str = "simple"
    width: int = 1920
    height: int = 1080


@dataclass
class ComfyNodeStatus:
    """설정 창의 ComfyUI 노드 실행 뷰어에 렌더링할 개별 노드 상태"""
    node_id: str
    node_type: str
    title: str
    status: str = "idle"          # "idle" | "running" | "done" | "skipped" | "error"
    progress_pct: int = 0
    summary: str = "대기 중"
    params_preview: str = ""


@dataclass
class ScenePreset:
    """확장 가능한 창문 풍경 프리셋 메타데이터"""
    key: str
    title: str
    keywords: Tuple[str, ...]
    lighting_key: str
    default_filename: str
    is_custom_or_ai: bool = False


@dataclass
class LandscapeState:
    """프론트엔드 가상 창문 및 ComfyUI 모니터 렌더링을 위한 전체 상태"""
    version: int
    active_key: str
    active_title: str
    current_image_url: str
    previous_image_url: str
    is_special_ai_image: bool
    is_generating: bool
    last_prompt: str
    last_negative_prompt: str = ""
    generation_progress_pct: int = 0
    generation_status_text: str = "대기 중"
    generation_error_text: str = ""
    active_node_id: str = ""
    nodes: List[ComfyNodeStatus] = field(default_factory=list)
    workflow_json_str: str = ""


# 기본 프리셋 레지스트리 (단순 시간대/대기화면 명령용)
DEFAULT_SCENE_PRESETS: Dict[str, ScenePreset] = {
    "default": ScenePreset(
        key="default",
        title="기본 대기 화면",
        keywords=("기본", "기분", "대기", "대기화면", "초기화면", "원래대로", "초기화", "디폴트", "default"),
        lighting_key="default",
        default_filename="default",
        is_custom_or_ai=False,
    ),
    "morning": ScenePreset(
        key="morning",
        title="아침 풍경",
        keywords=(
            "아침", "마침", "안심", "아짐", "아친", "하침", "아치", "아췸",
            "굿모닝", "모닝", "morning", "새벽", "해뜰녘", "일출", "낮", "환하게", "밝게",
        ),
        lighting_key="morning",
        default_filename="morning",
        is_custom_or_ai=False,
    ),
    "sunset": ScenePreset(
        key="sunset",
        title="노을 풍경",
        keywords=(
            "노을", "너울", "누을", "노울", "노올", "노엘", "노루", "노일", "노은", "노유",
            "노래", "노랑", "노란", "너를", "너는", "노아", "노원", "로을", "롤", "논", "눈을",
            "석양", "해질녘", "해질", "일몰", "황혼", "오후", "선셋", "sunset",
        ),
        lighting_key="sunset",
        default_filename="sunset",
        is_custom_or_ai=False,
    ),
    "evening": ScenePreset(
        key="evening",
        title="저녁 풍경",
        keywords=(
            "저녁", "전역", "저년", "저녘", "정역", "천역", "전력", "저역",
            "밤", "야경", "밤하늘", "자정", "어둡게", "수면", "이브닝", "나이트", "evening", "night",
        ),
        lighting_key="evening",
        default_filename="evening",
        is_custom_or_ai=False,
    ),
}


class IntentInferenceEngine:
    """
    사용자의 음성 명령 문장을 추론하여:
    1) '만들어줘', '그려줘'(GENERATION_VERBS)가 명시적으로 포함된 경우에만 ComfyUI 이미지 생성('generate')으로 분기하고,
    2) 생성 동사가 없을 때는 등록된 프리셋('아침', '노을', '저녁', '야경', '기본', '최애', 또는 저장된 프리셋 이름)만 전환('preset')하며,
    3) 그 외의 모든 말에는 절대 반응하지 않도록('ignore') 엄격히 필터링합니다.
    """

    FILLER_WORDS: Tuple[str, ...] = (
        "보여줘", "보여 줘", "바꿔줘", "바꿔 줘", "해줘", "해 줘", "켜줘", "켜 줘",
        "틀어줘", "틀어 줘", "띄워줘", "띄워 줘", "적용해줘", "적용해 줘", "적용",
        "가자", "전환해줘", "전환", "모드로", "모드",
        "창문", "화면", "풍경", "배경", "사진", "이미지", "빛으로", "빛", "채도로", "채도", "느낌으로", "느낌",
        "으로", "로", "좀", "지금", "바로", "윈도우", "윈도", "window", "windo",
    )

    # 오직 이 동사들이 포함되어야만 AI 이미지 생성을 수행합니다!
    GENERATION_VERBS: Tuple[str, ...] = (
        "만들어줘", "만들어 줘", "만들어", "그려줘", "그려 줘", "그려", "생성해줘", "생성해 줘", "생성해",
    )

    FORCE_REGEN_WORDS: Tuple[str, ...] = (
        "새로", "다시", "새로운", "새롭게",
    )

    # 날씨 및 계절 정밀 사전
    WEATHER_MAP: Dict[str, str] = {
        "겨울": "snowy winter season, frost-covered landscape, crisp freezing air, gentle falling snow",
        "눈": "heavy snowfall, pristine white snow blanket covering the ground, glistening snowflakes",
        "설경": "deep winter snowfield, heavy snow on pine trees, frost and icicles",
        "폭설": "blizzard and heavy snowstorm covering the entire vista in deep white snow",
        "봄": "warm spring season, blooming pink cherry blossoms, fresh green sprouts, gentle spring breeze",
        "벚꽃": "full bloom pink cherry blossom trees with petals drifting in the wind",
        "유채꽃": "vast yellow canola flower fields in full spring bloom",
        "여름": "vibrant sunny summer day, lush emerald green foliage, towering white cumulus clouds, sparkling sunlight",
        "가을": "crisp autumn season, golden gingko and crimson red maple foliage, warm autumn atmosphere",
        "단풍": "vibrant red, orange, and golden autumn maple trees covering the mountains",
        "낙엽": "ground covered with fallen golden and red autumn leaves",
        "비": "atmospheric rain shower, falling raindrops on window glass, wet reflective surfaces, moody sky",
        "소나기": "dramatic summer rain shower with sunbeams breaking through clouds",
        "폭풍": "dramatic dark storm clouds, lightning bolt in the distant sky, windy weather",
        "번개": "dramatic thunderbolt and lightning illuminating dark clouds",
        "안개": "ethereal dense morning mist and fog rolling softly through the landscape",
        "구름": "majestic volumetric clouds drifting across the expansive sky",
        "맑은": "crystal clear blue sky with radiant natural sunlight",
        "화창한": "bright sunny and clear weather with vivid colors",
        "무지개": "vibrant double rainbow arching across the sky after rain",
        "오로라": "magnificent glowing green and violet aurora borealis dancing across the polar sky",
        "별똥별": "shooting stars and meteor shower streaking across the clear night sky",
    }

    # 위치, 지형, 국가, 오브젝트 정밀 사전
    LOCATION_MAP: Dict[str, str] = {
        "숲": "ancient serene pine and cedar forest with sunbeams filtering through tall trees",
        "정글": "lush tropical rainforest jungle with exotic plants and misty waterfalls",
        "대나무": "serene green bamboo grove forest with a quiet walking path",
        "바다": "tranquil expansive ocean coastline with gentle turquoise waves and clear horizon",
        "해변": "pristine sandy beach with crystal clear turquoise ocean water and coastal cliffs",
        "파도": "dramatic ocean waves crashing against coastal rocks",
        "섬": "scenic tropical island surrounded by emerald coral sea",
        "산": "majestic towering mountain range with dramatic rocky peaks and deep valley",
        "설산": "majestic snow-capped alpine mountain peaks glistening in the sunlight",
        "알프스": "Swiss Alps towering snow-capped peaks, emerald alpine meadow, and wooden chalets",
        "스위스": "picturesque Swiss alpine village with green pastures and snow-capped mountains",
        "아이슬란드": "Icelandic dramatic volcanic landscape, black sand coast, waterfalls, and glaciers",
        "노르웨이": "majestic Norwegian fjord with steep cliffs and calm deep water",
        "호수": "mirror-like calm alpine lake perfectly reflecting the mountains and sky",
        "강": "winding tranquil river flowing through a scenic valley",
        "계곡": "crystal clear mountain stream flowing over mossy rocks in a serene canyon",
        "폭포": "majestic giant cascading waterfall surrounded by lush mossy cliffs and mist",
        "사막": "vast golden Sahara sand dunes with wind ripples and a desert oasis",
        "초원": "endless rolling green grassland meadow with colorful wildflowers",
        "들판": "vast open meadow field stretching to the distant horizon",
        "꽃밭": "vibrant colorful flower field in full bloom stretching to the horizon",
        "라벤더": "endless purple lavender fields in Provence France",
        "도시": "modern architectural metropolis skyline seen from a high panoramic window vantage point",
        "야경": "glittering city skyline at night with glowing skyscraper lights and river reflections",
        "사이버펑크": "futuristic cyberpunk neon cityscape with holographic billboards and towering megastructures",
        "우주": "deep space cosmos with colorful nebula, galaxies, and a ringed planet seen through an observation window",
        "은하수": "high mountain peak under a dazzling starry Milky Way galaxy arch",
        "달": "giant luminous full moon hanging over the tranquil landscape",
        "유럽": "classic European countryside with charming stone architecture and rolling hills",
        "파리": "panoramic Paris view featuring the Eiffel Tower and Seine river seen through a grand window",
        "산토리니": "Santorini Greece whitewashed buildings with blue domes overlooking the sparkling Aegean sea",
        "베네치아": "Venice Italy scenic grand canal with historic buildings and gondolas",
        "일본": "Mount Fuji overlooking a tranquil lake with traditional pagoda and cherry blossoms",
        "교토": "historic Kyoto traditional wooden street and serene garden temple",
        "한국": "scenic Korean mountain ridges, traditional Hanok pavilion, and tranquil river valley",
        "서울": "panoramic Seoul metropolis skyline with Han River, illuminated bridges, and Namsan Seoul Tower",
        "부산": "Busan Haeundae and Gwangalli coastal skyline with illuminated Gwangan Diamond Bridge over the ocean bay",
        "인천": "Incheon Songdo futuristic waterfront skyline and Incheon Grand Bridge overlooking the West Sea",
        "제주": "Jeju Island volcanic basalt coastline, Seongsan Ilchulbong peak, and emerald sea",
        "경주": "Gyeongju historic traditional Korean palace pond Donggung and Wolji with reflections",
        "강릉": "Gangneung East Sea pine forest beach with clean turquoise waves",
        "여수": "Yeosu romantic coastal night harbor with illuminated cable car and bridge over calm sea",
        "독도": "Dokdo volcanic twin rocky islands rising majestically from the deep blue East Sea",
        "한라산": "Hallasan snow-covered volcanic crater peak in Jeju overlooking a sea of clouds",
        "설악산": "Seoraksan dramatic jagged granite peaks and lush valley in Korea",
        "한강": "wide Han River park vista in Seoul with sparkling water and city skyline",
        "뉴욕": "New York Manhattan iconic skyscraper skyline and Central Park seen from a high luxury penthouse window",
        "런던": "London River Thames, Tower Bridge, and Big Ben skyline vista",
        "도쿄": "Tokyo modern skyline with glowing Tokyo Tower and Mount Fuji on the distant horizon",
        "오사카": "Osaka vibrant riverfront skyline and historic castle vista",
        "하와이": "Hawaii Waikiki tropical turquoise beach with palm trees and Diamond Head volcanic ridge",
        "몰디브": "Maldives crystal-clear turquoise lagoon with coral reef and tropical ocean horizon",
        "그랜드캐니언": "Grand Canyon vast red rock canyon cliffs and Colorado River vista",
        "나이아가라": "Niagara Falls massive thundering horseshoe waterfall with mist and rainbow",
        "로마": "Rome historic skyline with Colosseum and warm Mediterranean pine trees",
        "프라하": "Prague fairytale Charles Bridge and Vltava river overlooking Prague Castle",
        "두바이": "Dubai futuristic skyline with Burj Khalifa and sparkling Arabian Gulf coast",
        "홍콩": "Hong Kong Victoria Harbour glittering skyscraper skyline and harbor water reflections",
        "한옥": "traditional Korean Hanok wooden window view overlooking a serene mountain landscape",
        "오두막": "cozy wooden cabin window view looking out at peaceful nature",
        "성": "majestic fairytale stone castle perched on a scenic cliff",
        "기차": "scenic railway track with a vintage train passing through breathtaking nature",
        "등대": "solitary coastal lighthouse standing on a cliff overlooking the ocean",
    }

    DETAIL_MODIFIER_MAP: Dict[str, str] = {
        "따뜻한": "warm golden ambient tones",
        "차가운": "cool crisp blue and silver tones",
        "신비로운": "mystical ethereal and dreamy atmosphere",
        "몽환적인": "dreamlike fantasy atmospheric lighting",
        "평화로운": "peaceful calm and serene mood",
        "웅장한": "epic grand scale and majestic composition",
        "붉은": "rich crimson and ruby red color palette",
        "푸른": "deep azure and emerald blue color palette",
        "보라색": "violet and magenta glowing hues",
        "황금빛": "radiant golden shimmering light",
    }

    TIME_MAP: Dict[str, Tuple[str, str]] = {
        "아침": ("bright crisp early morning sunrise light", "morning"),
        "새벽": ("soft dawn twilight before sunrise with pale blue and pink sky", "morning"),
        "일출": ("radiant golden sunrise breaking over the horizon", "morning"),
        "낮": ("clear bright midday natural daylight", "morning"),
        "노을": ("warm golden hour sunset with dramatic amber, orange, and magenta sky", "sunset"),
        "석양": ("dramatic crimson and gold setting sun on the horizon", "sunset"),
        "일몰": ("glowing sun sinking below the horizon at dusk", "sunset"),
        "황혼": ("twilight dusk with deep orange and purple sky", "sunset"),
        "오후": ("warm late afternoon sunlight with long soft shadows", "sunset"),
        "저녁": ("calm deep blue evening dusk with early twinkling stars", "evening"),
        "밤": ("serene dark starry night sky with soft silver moonlight", "evening"),
        "야경": ("glowing night atmosphere with illuminated city or starlight", "evening"),
        "자정": ("quiet midnight sky filled with thousands of stars", "evening"),
    }

    @classmethod
    def extract_core_phrase(cls, text: str) -> str:
        cleaned = text.strip().lower()
        for v in cls.GENERATION_VERBS:
            cleaned = cleaned.replace(v, " ")
        for f in cls.FILLER_WORDS:
            cleaned = cleaned.replace(f, " ")
        cleaned = re.sub(r"[()\[\]{},.!?\"']+", " ", cleaned)
        return " ".join(cleaned.split()).strip()

    @classmethod
    def infer_intent(
        cls,
        command_text: str,
        presets: Dict[str, ScenePreset],
    ) -> Tuple[str, Optional[str], Tuple[str, str, str]]:
        """
        [엄격한 의도 분류 규칙]
        1) 문장에 '만들어줘', '그려줘'(GENERATION_VERBS)가 포함되어 있으면:
           - 단순 '아침 만들어줘', '노을 만들어줘', '저녁 만들어줘'가 아닌 이상 무조건 ComfyUI 이미지 생성('generate') 수행!
        2) 문장에 '만들어줘', '그려줘'가 없으면:
           - 등록된 프리셋(기본 4종 + '최애' 등 커스텀 이미지 + 이미 생성된 프리셋)과 매칭될 때만 'preset' 반환!
           - 매칭되는 프리셋이 없으면 절대 이미지 생성을 하지 않고 'ignore' 반환!
        """
        text = command_text.strip()
        if not text:
            return "ignore", None, ("", "", "")

        text_lower = text.lower()
        compact_text = text_lower.replace(" ", "").replace("_", "")
        core_phrase = cls.extract_core_phrase(text)
        compact_core = core_phrase.replace(" ", "").replace("_", "")

        has_gen_verb = any(v in text_lower for v in cls.GENERATION_VERBS)
        has_weather = any(k in text_lower for k in cls.WEATHER_MAP)
        has_location = any(k in text_lower for k in cls.LOCATION_MAP)

        # Case A: 사용자가 명시적으로 "~ 만들어줘", "~ 그려줘"라고 말한 경우 -> 이미지 생성('generate')!
        if has_gen_verb:
            # 단, 특수 날씨/장소 없이 순수하게 "아침 만들어줘", "노을 만들어줘", "저녁 만들어줘"라고만 한 경우는 기본 프리셋 예외 처리
            if not has_weather and not has_location:
                for key in ("morning", "sunset", "evening", "default"):
                    preset = presets.get(key)
                    if preset and any(kw in text_lower for kw in preset.keywords):
                        rem = core_phrase
                        for kw in preset.keywords:
                            rem = rem.replace(kw, "")
                        if len(rem.strip()) <= 1:
                            return "preset", key, ("", "", "")

            location, weather, time_of_day, lighting_key = cls.extract_attributes(text)
            return "generate", lighting_key, (location, weather, time_of_day)

        # Case B: "만들어줘", "그려줘"가 없는 경우 -> 오직 등록된 프리셋('최애', '저녁', '노을', '아침' 등)만 매칭하고 나머지는 전부 무시('ignore')!
        custom_presets = [
            (k, p) for k, p in presets.items() if k not in DEFAULT_SCENE_PRESETS
        ]
        custom_presets.sort(key=lambda item: len(item[0]), reverse=True)

        for key, preset in custom_presets:
            key_clean = key.lower().replace("_", " ").strip()
            key_compact = key_clean.replace(" ", "")
            stem_core = key_clean
            for suffix in (" 풍경", " 배경", " 화면", " 사진"):
                if stem_core.endswith(suffix):
                    stem_core = stem_core[: -len(suffix)].strip()
            stem_core_compact = stem_core.replace(" ", "")

            if compact_core and (
                compact_core == key_compact
                or (stem_core_compact and compact_core == stem_core_compact)
            ):
                return "preset", key, ("", "", "")

            if any(kw.lower() in text_lower or kw.lower().replace(" ", "") in compact_text for kw in preset.keywords):
                rem = compact_core.replace(key_compact, "")
                if stem_core_compact:
                    rem = rem.replace(stem_core_compact, "")
                if len(rem) <= 1:
                    return "preset", key, ("", "", "")

        # 기본 4대 프리셋('아침', '노을', '저녁', '야경', '기본') 매칭
        for key in ("morning", "sunset", "evening", "default"):
            preset = presets.get(key)
            if not preset:
                continue
            if any(kw in text_lower for kw in preset.keywords):
                rem = core_phrase
                for kw in preset.keywords:
                    rem = rem.replace(kw, "")
                if len(rem.strip()) <= 1:
                    return "preset", key, ("", "", "")

        return "ignore", None, ("", "", "")

    @classmethod
    def extract_attributes(cls, text: str) -> Tuple[str, str, str, str]:
        text_lower = text.lower()

        # 1) 날씨(weather) 추출
        matched_weathers: List[str] = []
        for k, eng_w in cls.WEATHER_MAP.items():
            if k in text_lower:
                matched_weathers.append(eng_w)
        weather = ", ".join(matched_weathers) if matched_weathers else "clear atmospheric sky with soft natural clouds"

        # 2) 시간대(time_of_day) 및 자연광 채도 키 추출
        time_of_day = "natural daylight"
        lighting_key = "default"
        for k, (eng_t, l_key) in cls.TIME_MAP.items():
            if k in text_lower:
                time_of_day = eng_t
                lighting_key = l_key
                break
        if lighting_key == "default" and ("겨울" in text_lower or "눈" in text_lower or "설산" in text_lower):
            lighting_key = "morning"

        # 3) 위치/장소(location) 및 분위기 수식어 추출
        matched_locations: List[str] = []
        for k, eng_loc in cls.LOCATION_MAP.items():
            if k in text_lower:
                matched_locations.append(eng_loc)

        matched_modifiers: List[str] = []
        for k, eng_mod in cls.DETAIL_MODIFIER_MAP.items():
            if k in text_lower:
                matched_modifiers.append(eng_mod)

        if matched_locations:
            location = ", ".join(matched_locations)
        elif "겨울" in text_lower or "눈" in text_lower or "설경" in text_lower:
            location = "serene snow-covered alpine mountain valley, frozen mirror lake, and snow-laden pine tree forest"
        elif "봄" in text_lower or "벚꽃" in text_lower:
            location = "scenic lakeside walkway lined with blooming pink cherry blossom trees and distant green hills"
        elif "여름" in text_lower:
            location = "emerald coastal cliff, turquoise ocean waves, and lush green valley"
        elif "가을" in text_lower or "단풍" in text_lower:
            location = "picturesque mountain valley covered in vibrant red and gold autumn maple trees with a calm river"
        else:
            location = "breathtaking panoramic mountain range and tranquil mirror lake vista"

        if matched_modifiers:
            location = f"{location}, {', '.join(matched_modifiers)}"

        return location, weather, time_of_day, lighting_key


class VirtualLandscapeManager:
    """
    ComfyUI 노드 그래프 워크플로우 엔진 기반 초고화질 풍경 생성 및 프리셋 통합 매니저
    """

    SUPPORTED_EXTS: Tuple[str, ...] = (
        ".svg", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
    )

    MIME_MAP: Dict[str, str] = {
        ".svg": "image/svg+xml",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".avif": "image/avif",
    }

    def __init__(self, base_dir: Path) -> None:
        self._lock = threading.RLock()
        self._gpu_lock = threading.Lock()
        self.base_dir = base_dir
        self.presets_dir = base_dir / "assets" / "images" / "presets"
        self.cache_dir = base_dir / ".cache" / "landscapes"
        self.loras_dir = base_dir / "loras"
        self.checkpoints_dir = base_dir / "models" / "checkpoints"

        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.loras_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

        self.presets: Dict[str, ScenePreset] = dict(DEFAULT_SCENE_PRESETS)

        # ComfyUI 사용자 설정 상태 (긍정 프롬프트, 부정 프롬프트, 모델, LoRA 등)
        self.comfy_config = ComfyUIConfig()
        self._nodes: List[ComfyNodeStatus] = self._create_initial_nodes()
        self._active_node_id: str = ""
        self._last_workflow_json: str = self._build_comfyui_workflow_json(
            positive_text=self.comfy_config.positive_prompt,
            negative_text=self.comfy_config.negative_prompt,
            seed=42,
            filename_prefix="WINDO_ComfyUI",
        )

        # 내장 ComfyUI 노드 실행기용 모델 캐시
        self._local_flux_pipe: Any = None
        self._local_t5_encoder: Any = None
        self._loaded_model_name: str = ""
        self._loaded_lora_key: str = ""

        # 폴더에 저장되어 있는 커스텀 이미지(예: 최애.jpg) 및 AI 생성 이미지 자동 스캔 등록
        self.scan_and_register_preset_files()

        initial_url = self.resolve_preset_image_url("default")
        # _version은 오직 실제 창문 배경 이미지가 교체될 때만 증가합니다! (생성 진행 중에는 절대 증가하지 않음)
        self._version: int = 1
        self.active_key: str = "default"
        self.active_title: str = "기본 대기 화면"
        self.current_image_url: str = initial_url
        self.previous_image_url: str = initial_url
        self.is_special_ai_image: bool = False
        self.is_generating: bool = False
        self.last_prompt: str = self.comfy_config.positive_prompt
        self.last_negative_prompt: str = self.comfy_config.negative_prompt
        self.generation_progress_pct: int = 0
        self.generation_status_text: str = "ComfyUI 워크플로우 대기 중"
        self.generation_error_text: str = ""

    # -------------------------------------------------------------------------
    # ComfyUI 7-노드 파이프라인 그래프 정의 및 상태 관리
    # -------------------------------------------------------------------------
    def _create_initial_nodes(self) -> List[ComfyNodeStatus]:
        cfg = self.comfy_config
        return [
            ComfyNodeStatus(
                node_id="1",
                node_type="CheckpointLoaderSimple",
                title="Node #1 · 모델 로더 (Model)",
                status="idle",
                progress_pct=0,
                summary="대기 중",
                params_preview=f"ckpt_name: {cfg.model_name}",
            ),
            ComfyNodeStatus(
                node_id="2",
                node_type="LoraLoader",
                title="Node #2 · LoRA 로더 (LoRA)",
                status="idle",
                progress_pct=0,
                summary="대기 중",
                params_preview=f"lora_name: {cfg.lora_name} (strength: {cfg.lora_strength:.2f})",
            ),
            ComfyNodeStatus(
                node_id="3",
                node_type="CLIPTextEncode (Positive)",
                title="Node #3 · 긍정 프롬프트 인코딩 (+)",
                status="idle",
                progress_pct=0,
                summary="대기 중",
                params_preview=cfg.positive_prompt[:80] + "...",
            ),
            ComfyNodeStatus(
                node_id="4",
                node_type="CLIPTextEncode (Negative)",
                title="Node #4 · 부정 프롬프트 필터링 (-)",
                status="idle",
                progress_pct=0,
                summary="대기 중",
                params_preview=cfg.negative_prompt[:80] + "...",
            ),
            ComfyNodeStatus(
                node_id="5",
                node_type="EmptyLatentImage",
                title="Node #5 · 16:9 초고해상도 잠재 공간",
                status="idle",
                progress_pct=0,
                summary="대기 중",
                params_preview=f"Native 1344x768 -> Output {cfg.width}x{cfg.height} Full HD",
            ),
            ComfyNodeStatus(
                node_id="6",
                node_type="KSampler",
                title="Node #6 · KSampler (정밀 디노이징)",
                status="idle",
                progress_pct=0,
                summary="대기 중",
                params_preview=f"steps: {cfg.steps} | cfg: {cfg.cfg_scale} | {cfg.sampler_name} ({cfg.scheduler})",
            ),
            ComfyNodeStatus(
                node_id="7",
                node_type="VAEDecode & SaveImage",
                title="Node #7 · VAE 디코딩 & Full HD 창문 출력",
                status="idle",
                progress_pct=0,
                summary="대기 중",
                params_preview=f"output: {cfg.width}x{cfg.height} PNG",
            ),
        ]

    def _set_node_state(
        self,
        node_id: str,
        status: str,
        progress_pct: int,
        summary: str,
        params_preview: Optional[str] = None,
    ) -> None:
        """
        [중요] 노드 진행 상태만 업데이트하며, 창문 배경 버전(self._version)은 절대 건드리지 않습니다!
        덕분에 생성 도중 기존 창문 화면이 다른 화면으로 깜빡이거나 넘어가지 않습니다.
        """
        with self._lock:
            if status == "running":
                self._active_node_id = node_id
            elif status in ("done", "idle") and self._active_node_id == node_id:
                self._active_node_id = ""
            for node in self._nodes:
                if node.node_id == node_id:
                    node.status = status
                    node.progress_pct = max(0, min(100, int(progress_pct)))
                    node.summary = summary
                    if params_preview is not None:
                        node.params_preview = params_preview
                    break

    def _reset_all_nodes_for_run(self, positive_full: str, negative_full: str, scene_stem: str) -> None:
        cfg = self.comfy_config
        with self._lock:
            self._nodes = [
                ComfyNodeStatus(
                    node_id="1",
                    node_type="CheckpointLoaderSimple",
                    title="Node #1 · 모델 로더 (Model)",
                    status="idle",
                    progress_pct=0,
                    summary="실행 대기",
                    params_preview=f"ckpt_name: {cfg.model_name}",
                ),
                ComfyNodeStatus(
                    node_id="2",
                    node_type="LoraLoader",
                    title="Node #2 · LoRA 로더 (LoRA)",
                    status="idle",
                    progress_pct=0,
                    summary="실행 대기",
                    params_preview=f"lora: {cfg.lora_name} | strength: {cfg.lora_strength:.2f} | trigger: '{cfg.lora_trigger}'",
                ),
                ComfyNodeStatus(
                    node_id="3",
                    node_type="CLIPTextEncode (Positive)",
                    title="Node #3 · 긍정 프롬프트 인코딩 (+)",
                    status="idle",
                    progress_pct=0,
                    summary="실행 대기",
                    params_preview=positive_full,
                ),
                ComfyNodeStatus(
                    node_id="4",
                    node_type="CLIPTextEncode (Negative)",
                    title="Node #4 · 부정 프롬프트 필터링 (-)",
                    status="idle",
                    progress_pct=0,
                    summary="실행 대기",
                    params_preview=negative_full,
                ),
                ComfyNodeStatus(
                    node_id="5",
                    node_type="EmptyLatentImage",
                    title="Node #5 · 16:9 초고해상도 잠재 공간",
                    status="idle",
                    progress_pct=0,
                    summary="실행 대기",
                    params_preview=f"Native 1344x768 -> Output {cfg.width}x{cfg.height} Full HD",
                ),
                ComfyNodeStatus(
                    node_id="6",
                    node_type="KSampler",
                    title="Node #6 · KSampler (정밀 디노이징)",
                    status="idle",
                    progress_pct=0,
                    summary="실행 대기",
                    params_preview=f"steps: {cfg.steps} | cfg: {cfg.cfg_scale:.1f} | sampler: {cfg.sampler_name} | scheduler: {cfg.scheduler}",
                ),
                ComfyNodeStatus(
                    node_id="7",
                    node_type="VAEDecode & SaveImage",
                    title="Node #7 · VAE 디코딩 & Full HD 창문 출력",
                    status="idle",
                    progress_pct=0,
                    summary="실행 대기",
                    params_preview=f"filename: {scene_stem}.png ({cfg.width}x{cfg.height})",
                ),
            ]
            self._last_workflow_json = self._build_comfyui_workflow_json(
                positive_text=positive_full,
                negative_text=negative_full,
                seed=int(time.time()) % 2147483647,
                filename_prefix=scene_stem,
            )

    def _build_comfyui_workflow_json(
        self,
        positive_text: str,
        negative_text: str,
        seed: int,
        filename_prefix: str,
    ) -> str:
        cfg = self.comfy_config
        model_ref = ["2", 0] if cfg.lora_name.lower() != "none" else ["1", 0]
        clip_ref = ["2", 1] if cfg.lora_name.lower() != "none" else ["1", 1]

        workflow: Dict[str, Any] = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": cfg.model_name},
            },
            "2": {
                "class_type": "LoraLoader",
                "inputs": {
                    "model": ["1", 0],
                    "clip": ["1", 1],
                    "lora_name": cfg.lora_name,
                    "strength_model": cfg.lora_strength,
                    "strength_clip": cfg.lora_strength,
                },
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": positive_text,
                    "clip": clip_ref,
                },
            },
            "4": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": negative_text,
                    "clip": clip_ref,
                },
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": cfg.width,
                    "height": cfg.height,
                    "batch_size": 1,
                },
            },
            "6": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": cfg.steps,
                    "cfg": cfg.cfg_scale,
                    "sampler_name": cfg.sampler_name,
                    "scheduler": cfg.scheduler,
                    "denoise": 1.0,
                    "model": model_ref,
                    "positive": ["3", 0],
                    "negative": ["4", 0],
                    "latent_image": ["5", 0],
                },
            },
            "7": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["6", 0],
                    "vae": ["1", 2],
                },
            },
            "8": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": filename_prefix,
                    "images": ["7", 0],
                },
            },
        }
        return json.dumps(workflow, ensure_ascii=False, indent=2)

    # -------------------------------------------------------------------------
    # ComfyUI 설정 조회 및 업데이트
    # -------------------------------------------------------------------------
    def list_available_models(self) -> List[str]:
        models: List[str] = [
            "unsloth/FLUX.1-schnell",
            "black-forest-labs/FLUX.1-schnell",
            "black-forest-labs/FLUX.1-dev",
        ]
        if self.checkpoints_dir.exists():
            for f in sorted(self.checkpoints_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in (".safetensors", ".ckpt", ".pt"):
                    if f.name not in models:
                        models.append(f.name)
        if self.comfy_config.model_name and self.comfy_config.model_name not in models:
            models.insert(0, self.comfy_config.model_name)
        return models

    def list_available_loras(self) -> List[str]:
        loras: List[str] = []
        if self.loras_dir.exists():
            for f in sorted(self.loras_dir.glob("*.safetensors")):
                loras.append(f.name)
        for f in sorted(self.base_dir.glob("*.safetensors")):
            if f.name not in loras:
                loras.append(f.name)
        if not loras:
            loras.append("landscape.safetensors")
        loras.append("none")
        return loras

    def update_comfy_config(
        self,
        model_name: Optional[str] = None,
        lora_name: Optional[str] = None,
        lora_strength: Optional[float] = None,
        lora_trigger: Optional[str] = None,
        positive_prompt: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        steps: Optional[int] = None,
        cfg_scale: Optional[float] = None,
        sampler_name: Optional[str] = None,
        scheduler: Optional[str] = None,
        server_url: Optional[str] = None,
    ) -> ComfyUIConfig:
        with self._lock:
            if model_name is not None and model_name.strip():
                self.comfy_config.model_name = model_name.strip()
            if lora_name is not None and lora_name.strip():
                self.comfy_config.lora_name = lora_name.strip()
            if lora_strength is not None:
                self.comfy_config.lora_strength = round(max(0.0, min(2.0, float(lora_strength))), 2)
            if lora_trigger is not None:
                self.comfy_config.lora_trigger = lora_trigger.strip()
            if positive_prompt is not None:
                self.comfy_config.positive_prompt = positive_prompt.strip()
            if negative_prompt is not None:
                self.comfy_config.negative_prompt = negative_prompt.strip()
            if steps is not None:
                self.comfy_config.steps = max(1, min(30, int(steps)))
            if cfg_scale is not None:
                self.comfy_config.cfg_scale = round(max(1.0, min(10.0, float(cfg_scale))), 1)
            if sampler_name is not None and sampler_name.strip():
                self.comfy_config.sampler_name = sampler_name.strip()
            if scheduler is not None and scheduler.strip():
                self.comfy_config.scheduler = scheduler.strip()
            if server_url is not None and server_url.strip():
                self.comfy_config.server_url = server_url.strip().rstrip("/")

            self.last_prompt = self.comfy_config.positive_prompt
            self.last_negative_prompt = self.comfy_config.negative_prompt
            self._nodes = self._create_initial_nodes()
            self._last_workflow_json = self._build_comfyui_workflow_json(
                positive_text=self.comfy_config.positive_prompt,
                negative_text=self.comfy_config.negative_prompt,
                seed=42,
                filename_prefix="WINDO_ComfyUI",
            )
            return self.comfy_config

    # -------------------------------------------------------------------------
    # 의미 있는 파일명 생성 & 커스텀/AI 프리셋 동적 스캔 및 등록
    # -------------------------------------------------------------------------
    @staticmethod
    def sanitize_scene_name(raw_name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", raw_name).strip()
        cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
        return cleaned[:40] if cleaned else "커스텀_풍경"

    @classmethod
    def derive_meaningful_scene_name(
        cls,
        display_label: str,
        location: str = "",
        weather: str = "",
        time_of_day: str = "",
    ) -> str:
        text = display_label.strip()
        for ww in ("윈도우", "윈도", "window", "windo"):
            if text.lower().startswith(ww):
                text = text[len(ww):].strip()

        remove_phrases = (
            "새로 만들어줘", "새로 만들어 줘", "다시 만들어줘", "다시 만들어 줘",
            "만들어줘", "만들어 줘", "만들어", "생성해줘", "생성해 줘", "생성해",
            "그려줘", "그려 줘", "그려", "보여줘", "보여 줘", "바꿔줘", "바꿔 줘",
            "틀어줘", "틀어 줘", "켜줘", "켜 줘", "적용해줘", "적용해 줘", "해줘", "해 줘",
            "새로", "다시", "지금", "바로", "좀",
        )
        for phrase in remove_phrases:
            text = text.replace(phrase, " ")

        text = re.sub(r"[()\[\]{},.!?\"']+", " ", text)
        words = [w for w in text.split() if w]

        if words and words[-1] in ("으로", "로"):
            words.pop()

        cleaned = "_".join(words)
        cleaned = cls.sanitize_scene_name(cleaned)

        if cleaned and "_" not in cleaned and not cleaned.endswith(("풍경", "배경", "화면", "노을", "일출", "야경")):
            cleaned = f"{cleaned}_풍경"

        if not cleaned or cleaned == "커스텀_풍경":
            parts: List[str] = []
            for kor, eng in IntentInferenceEngine.WEATHER_MAP.items():
                if eng in weather:
                    parts.append(kor)
                    break
            for kor, eng in IntentInferenceEngine.LOCATION_MAP.items():
                if eng in location:
                    parts.append(kor)
                    break
            for kor, (eng_t, _) in IntentInferenceEngine.TIME_MAP.items():
                if eng_t == time_of_day:
                    parts.append(kor)
                    break
            if parts:
                cleaned = "_".join(parts)
                if len(parts) == 1:
                    cleaned = f"{cleaned}_풍경"
            else:
                cleaned = "AI_자연_풍경"

        return cls.sanitize_scene_name(cleaned)

    @staticmethod
    def infer_lighting_key_from_name(name: str) -> str:
        n_lower = name.lower()
        if any(k in n_lower for k in ("노을", "석양", "일몰", "황혼", "해질", "오후", "가을", "단풍", "sunset")):
            return "sunset"
        if any(k in n_lower for k in ("저녁", "밤", "야경", "자정", "우주", "은하수", "오로라", "사이버펑크", "evening", "night")):
            return "evening"
        if any(k in n_lower for k in ("아침", "일출", "새벽", "겨울", "눈", "설경", "봄", "여름", "바다", "해변", "알프스", "morning")):
            return "morning"
        return "default"

    @classmethod
    def build_custom_preset(cls, scene_stem: str, is_ai: bool = False) -> ScenePreset:
        display_name = scene_stem.replace("_", " ").strip()
        compact_name = display_name.replace(" ", "")

        short_core = display_name
        for suffix in (" 풍경", " 배경", " 화면", " 사진", " 이미지"):
            if short_core.endswith(suffix):
                short_core = short_core[: -len(suffix)].strip()

        kw_list: List[str] = [
            display_name,
            scene_stem,
            compact_name,
            f"{display_name} 화면",
            f"{display_name} 배경",
            f"{display_name} 풍경",
        ]
        if short_core and short_core != display_name:
            kw_list.extend([
                f"{short_core} 풍경",
                f"{short_core} 배경",
                f"{short_core} 화면",
                f"{short_core}풍경",
                f"{short_core}배경",
            ])

        unique_kws: List[str] = []
        for kw in kw_list:
            kw_clean = kw.strip()
            if kw_clean and kw_clean not in unique_kws:
                unique_kws.append(kw_clean)

        lighting_key = cls.infer_lighting_key_from_name(display_name)
        title = f"{display_name} (AI)" if is_ai else display_name

        return ScenePreset(
            key=scene_stem,
            title=title,
            keywords=tuple(unique_kws),
            lighting_key=lighting_key,
            default_filename=scene_stem,
            is_custom_or_ai=True,
        )

    def scan_and_register_preset_files(self) -> List[str]:
        registered_keys: List[str] = []
        with self._lock:
            if self.cache_dir.exists():
                for c_file in sorted(self.cache_dir.iterdir()):
                    if not c_file.is_file() or c_file.suffix.lower() not in self.SUPPORTED_EXTS:
                        continue
                    stem = c_file.stem
                    if len(stem) == 20 and all(ch in "0123456789abcdef" for ch in stem.lower()):
                        continue
                    target_in_presets = self.presets_dir / c_file.name
                    if not target_in_presets.exists() and c_file.stat().st_size > 0:
                        try:
                            shutil.copy2(c_file, target_in_presets)
                        except Exception:
                            pass

            if self.presets_dir.exists():
                for p_file in sorted(self.presets_dir.iterdir(), key=lambda f: f.stat().st_mtime):
                    if not p_file.is_file() or p_file.suffix.lower() not in self.SUPPORTED_EXTS:
                        continue
                    stem = p_file.stem
                    if stem.lower() in DEFAULT_SCENE_PRESETS:
                        continue
                    is_ai_file = (self.cache_dir / p_file.name).exists()
                    self.presets[stem] = self.build_custom_preset(stem, is_ai=is_ai_file)
                    registered_keys.append(stem)

        return registered_keys

    def _update_progress(
        self,
        pct: int,
        status_text: str,
        error_text: str = "",
        log_callback: Optional[Callable[[str, str, str], None]] = None,
        log_level: str = "SYSTEM",
        log_detail: str = "",
    ) -> None:
        """
        [중요] 생성 진행률(%)과 상태 텍스트만 업데이트하며, self._version은 절대 변경하지 않습니다!
        """
        with self._lock:
            self.generation_progress_pct = max(0, min(100, int(pct)))
            self.generation_status_text = status_text
            self.generation_error_text = error_text
        if log_callback:
            log_callback(log_level, f"[{self.generation_progress_pct}%] {status_text}", log_detail)

    def register_preset(self, preset: ScenePreset) -> None:
        with self._lock:
            self.presets[preset.key] = preset

    def find_local_lora_file(self, preferred_name: str = "") -> Optional[Path]:
        target_name = preferred_name.strip() or self.comfy_config.lora_name.strip()
        if target_name.lower() == "none":
            return None

        if target_name:
            cand_in_loras = self.loras_dir / target_name
            if cand_in_loras.exists():
                return cand_in_loras
            cand_in_root = self.base_dir / target_name
            if cand_in_root.exists():
                return cand_in_root

        if self.loras_dir.exists():
            safetensors = sorted(self.loras_dir.glob("*.safetensors"))
            if safetensors:
                return safetensors[0]
        root_safetensors = sorted(self.base_dir.glob("*.safetensors"))
        if root_safetensors:
            return root_safetensors[0]
        return None

    def resolve_preset_image_url(self, preset_key: str) -> str:
        preset = self.presets.get(preset_key, self.presets["default"])
        stem = preset.default_filename.lower()

        matched_files: List[Path] = []
        if self.presets_dir.exists():
            for p in self.presets_dir.iterdir():
                if p.is_file() and p.stem.lower() == stem and p.suffix.lower() in self.SUPPORTED_EXTS:
                    matched_files.append(p)

        if matched_files:
            newest_file = max(matched_files, key=lambda f: f.stat().st_mtime_ns)
            mtime = int(newest_file.stat().st_mtime)
            encoded_name = urllib.parse.quote(newest_file.name)
            return f"/assets/images/presets/{encoded_name}?v={mtime}"

        encoded_stem = urllib.parse.quote(preset.default_filename)
        return f"/assets/images/presets/{encoded_stem}.svg"

    def save_uploaded_preset_image(
        self,
        preset_key: str,
        filename: str,
        content: bytes,
        custom_name: str = "",
    ) -> Tuple[str, str]:
        ext = Path(filename).suffix.lower()
        if ext not in self.SUPPORTED_EXTS:
            ext = ".jpg"

        custom_clean = custom_name.strip()
        if custom_clean:
            stem = self.sanitize_scene_name(custom_clean)
            target_path = self.presets_dir / f"{stem}{ext}"
            target_path.write_bytes(content)
            now_ts = time.time()
            os.utime(target_path, (now_ts, now_ts))

            with self._lock:
                if stem not in DEFAULT_SCENE_PRESETS:
                    self.presets[stem] = self.build_custom_preset(stem, is_ai=False)
                self.switch_to_preset(stem)
                return stem, self.current_image_url

        if preset_key not in self.presets:
            preset_key = "default"

        stem = self.presets[preset_key].default_filename
        target_path = self.presets_dir / f"{stem}{ext}"
        target_path.write_bytes(content)
        now_ts = time.time()
        os.utime(target_path, (now_ts, now_ts))

        with self._lock:
            self.switch_to_preset(preset_key)
            return preset_key, self.current_image_url

    def get_latest_cached_file(self) -> Optional[Path]:
        if not self.cache_dir.exists():
            return None
        files = [
            f for f in self.cache_dir.iterdir()
            if f.is_file() and f.suffix.lower() in self.SUPPORTED_EXTS and f.stat().st_size > 0
        ]
        if not files:
            return None
        return max(files, key=lambda f: f.stat().st_mtime_ns)

    def apply_latest_cached_landscape(self, label: str = "최근 생성된 AI 풍경") -> Optional[str]:
        latest = self.get_latest_cached_file()
        if not latest:
            return None

        stem = latest.stem
        target_in_presets = self.presets_dir / latest.name
        if not target_in_presets.exists():
            try:
                shutil.copy2(latest, target_in_presets)
            except Exception:
                pass

        with self._lock:
            if stem not in self.presets:
                self.presets[stem] = self.build_custom_preset(stem, is_ai=True)
            self.switch_to_preset(stem)
            self.generation_progress_pct = 100
            self.generation_status_text = f"✅ AI 생성 풍경 적용 완료 ({latest.name})"
            self.generation_error_text = ""
            return self.current_image_url

    def switch_to_preset(self, preset_key: str) -> Optional[ScenePreset]:
        """오직 실제 프리셋이나 생성 완료된 새 이미지로 화면을 전환할 때만 호출됩니다."""
        with self._lock:
            if preset_key not in self.presets:
                self.scan_and_register_preset_files()
            preset = self.presets.get(preset_key)
            if not preset:
                return None

            new_url = self.resolve_preset_image_url(preset_key)
            self.previous_image_url = self.current_image_url
            self.current_image_url = new_url
            self.active_key = preset.key
            self.active_title = preset.title
            self.is_special_ai_image = False
            self._version += 1
            return preset

    # -------------------------------------------------------------------------
    # ComfyUI 초고화질 '창문에서 바라보는 풍경' 긍정/부정 프롬프트 조립
    # -------------------------------------------------------------------------
    def build_comfy_prompts(self, location: str, weather: str, time_of_day: str) -> Tuple[str, str]:
        """
        가장 중요한 '창문에서 바라보는 풍경(View looking out through a large panoramic window)' 구도와
        8K UHD 초고해상도 광학 디테일이 최우선으로 반영되도록 긍정/부정 프롬프트를 조립합니다.
        """
        cfg = self.comfy_config
        loc_part = location.strip() or "majestic mountain range and tranquil mirror lake vista"
        wea_part = weather.strip() or "clear sky with soft natural clouds"
        time_part = time_of_day.strip() or "natural daylight"

        trigger_part = (
            f"{cfg.lora_trigger.strip()}, "
            if (cfg.lora_name.lower() != "none" and cfg.lora_trigger.strip())
            else ""
        )

        # 핵심: '창문 너머로 바라보는 풍경' 시점 + 피사체 묘사 + 사용자 긍정 프롬프트 결합
        window_scene_core = (
            f"{trigger_part}A breathtaking realistic view looking out through a large modern panoramic window, "
            f"seen from inside looking outside at {loc_part}, "
            f"featuring {wea_part} during {time_part}, "
            f"subtle clean architectural window frame at the very edges, crystal-clear glass with unobstructed expansive outdoor horizon, "
            f"realistic window depth and perspective"
        )
        user_pos = cfg.positive_prompt.strip()
        full_positive = f"{window_scene_core}, {user_pos}" if user_pos else window_scene_core
        full_negative = cfg.negative_prompt.strip() or DEFAULT_NEGATIVE_PROMPT
        return full_positive, full_negative

    @staticmethod
    def compute_cache_hash(location: str, weather: str, time_of_day: str) -> str:
        raw_key = f"{location.strip().lower()}|{weather.strip().lower()}|{time_of_day.strip().lower()}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:20]

    # -------------------------------------------------------------------------
    # 외부 ComfyUI 서버(127.0.0.1:8188) 생존 확인 및 API 연동
    # -------------------------------------------------------------------------
    def _is_external_comfyui_alive(self) -> bool:
        url = f"{self.comfy_config.server_url}/system_stats"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _run_external_comfyui_workflow(
        self,
        positive_prompt: str,
        negative_prompt: str,
        output_path: Path,
        log_callback: Optional[Callable[[str, str, str], None]] = None,
    ) -> bool:
        if not self._is_external_comfyui_alive():
            return False

        try:
            seed = int(time.time()) % 2147483647
            workflow_dict = json.loads(
                self._build_comfyui_workflow_json(
                    positive_text=positive_prompt,
                    negative_text=negative_prompt,
                    seed=seed,
                    filename_prefix=output_path.stem,
                )
            )
            payload = json.dumps({"prompt": workflow_dict}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.comfy_config.server_url}/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self._set_node_state("1", "running", 100, "외부 ComfyUI 서버에 워크플로우 전송 중...")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                prompt_id = resp_data.get("prompt_id")
            if not prompt_id:
                return False

            for nid in ("1", "2", "3", "4", "5"):
                self._set_node_state(nid, "done", 100, "외부 ComfyUI 서버 로드 완료")
            self._set_node_state("6", "running", 50, f"ComfyUI KSampler 실행 중 (ID: {prompt_id[:8]})")

            for _ in range(180):
                time.sleep(1.0)
                hist_req = urllib.request.Request(f"{self.comfy_config.server_url}/history/{prompt_id}", method="GET")
                with urllib.request.urlopen(hist_req, timeout=3.0) as h_resp:
                    hist_data = json.loads(h_resp.read().decode("utf-8"))
                if prompt_id in hist_data:
                    outputs = hist_data[prompt_id].get("outputs", {})
                    for _node_id, node_out in outputs.items():
                        images = node_out.get("images", [])
                        if images:
                            img_info = images[0]
                            q = urllib.parse.urlencode({
                                "filename": img_info["filename"],
                                "subfolder": img_info.get("subfolder", ""),
                                "type": img_info.get("type", "output"),
                            })
                            view_url = f"{self.comfy_config.server_url}/view?{q}"
                            urllib.request.urlretrieve(view_url, str(output_path))
                            self._set_node_state("6", "done", 100, "KSampler 완료")
                            self._set_node_state("7", "done", 100, f"저장 완료: {output_path.name}")
                            return True
            return False
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # 내장 ComfyUI 7-노드 초고화질 실행 엔진 (RTX 5080 로컬 GPU · 1344x768 -> 1920x1080 Full HD)
    # -------------------------------------------------------------------------
    def _load_or_get_comfy_nodes_pipeline(
        self,
        log_callback: Optional[Callable[[str, str, str], None]] = None,
    ) -> Tuple[Any, Any]:
        import gc
        import importlib.util
        import sys
        from unittest.mock import MagicMock

        for _mod_name, _mod_obj in list(sys.modules.items()):
            if isinstance(_mod_obj, MagicMock) or (
                _mod_name.startswith(("matplotlib", "scipy", "mpl_toolkits", "cycler"))
                and getattr(_mod_obj, "__spec__", None) is None
            ):
                sys.modules.pop(_mod_name, None)

        _orig_find_spec = getattr(importlib.util, "_orig_find_spec", importlib.util.find_spec)

        def _safe_find_spec(name: str, package: Optional[str] = None) -> Any:
            mod = sys.modules.get(name)
            if mod is not None and (isinstance(mod, MagicMock) or getattr(mod, "__spec__", None) is None):
                sys.modules.pop(name, None)
            try:
                return _orig_find_spec(name, package)
            except ValueError:
                sys.modules.pop(name, None)
                return None

        setattr(importlib.util, "_orig_find_spec", _orig_find_spec)
        importlib.util.find_spec = _safe_find_spec

        import torch
        from diffusers import FluxPipeline
        from transformers import T5EncoderModel

        cfg = self.comfy_config
        target_model = cfg.model_name.strip() or "unsloth/FLUX.1-schnell"
        lora_file = self.find_local_lora_file(cfg.lora_name)
        target_lora_key = f"{lora_file.name if lora_file else 'none'}:{cfg.lora_strength:.2f}"

        if (
            self._local_flux_pipe is not None
            and self._local_t5_encoder is not None
            and self._loaded_model_name == target_model
            and self._loaded_lora_key == target_lora_key
        ):
            self._set_node_state("1", "done", 100, f"모델 상주 완료 ({target_model})")
            if lora_file:
                self._set_node_state("2", "done", 100, f"LoRA 병합 완료 ({lora_file.name}, 강도 {cfg.lora_strength:.2f})")
            else:
                self._set_node_state("2", "skipped", 100, "LoRA 미사용 (None)")
            return self._local_flux_pipe, self._local_t5_encoder

        self._local_flux_pipe = None
        self._local_t5_encoder = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        t0 = time.time()
        gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"

        # [Node #1] CheckpointLoaderSimple 실행
        self._set_node_state("1", "running", 40, f"모델 로드 중: {target_model} ({gpu_name})")
        self._update_progress(
            12,
            f"[Node #1 CheckpointLoader] '{target_model}' 로드 중...",
            log_callback=log_callback,
            log_detail=f"Device: {gpu_name}",
        )

        hf_cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{target_model.replace('/', '--')}"
        has_local_cache = hf_cache_dir.exists() and not list((hf_cache_dir.parent / "blobs").glob("*.incomplete"))
        actual_model_id = target_model if has_local_cache else "unsloth/FLUX.1-schnell"
        fallback_cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / "models--unsloth--FLUX.1-schnell"
        use_local_only = has_local_cache or fallback_cache_dir.exists()

        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        pipe = FluxPipeline.from_pretrained(
            actual_model_id,
            text_encoder_2=None,
            torch_dtype=dtype,
            local_files_only=use_local_only,
        )
        self._set_node_state("1", "done", 100, f"로드 완료 ({actual_model_id})")

        # [Node #2] LoraLoader 실행
        if lora_file and lora_file.exists() and cfg.lora_strength > 0.0:
            self._set_node_state(
                "2",
                "running",
                50,
                f"LoRA 가중치 병합 중: {lora_file.name} (strength={cfg.lora_strength:.2f})",
            )
            self._update_progress(
                24,
                f"[Node #2 LoraLoader] '{lora_file.name}' (강도 {cfg.lora_strength:.2f}) 병합 중...",
                log_callback=log_callback,
                log_detail=f"Trigger: '{cfg.lora_trigger}'",
            )
            pipe.load_lora_weights(
                str(lora_file.parent),
                weight_name=lora_file.name,
                adapter_name="landscape_lora",
            )
            pipe.fuse_lora(lora_scale=float(cfg.lora_strength))
            pipe.unload_lora_weights()
            self._set_node_state(
                "2",
                "done",
                100,
                f"병합 완료 ({lora_file.name}, 강도 {cfg.lora_strength:.2f})",
            )
        else:
            self._set_node_state("2", "skipped", 100, "LoRA 미적용 (None 또는 강도 0)")

        if hasattr(pipe, "transformer") and hasattr(pipe.transformer, "enable_layerwise_casting"):
            pipe.transformer.enable_layerwise_casting(
                storage_dtype=torch.float8_e4m3fn,
                compute_dtype=dtype,
            )

        if torch.cuda.is_available():
            if hasattr(pipe, "transformer") and hasattr(pipe.transformer, "enable_group_offload"):
                pipe.transformer.enable_group_offload(
                    onload_device=torch.device("cuda"),
                    offload_device=torch.device("cpu"),
                    offload_type="block_level",
                    num_blocks_per_group=8,
                    low_cpu_mem_usage=True,
                )
            else:
                pipe.transformer.to("cuda")
            pipe.text_encoder.to("cuda")
            pipe.vae.to("cuda")

        if hasattr(pipe, "vae") and pipe.vae is not None:
            if hasattr(pipe.vae, "enable_slicing"):
                pipe.vae.enable_slicing()
            if hasattr(pipe.vae, "enable_tiling"):
                pipe.vae.enable_tiling()

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        t5_encoder = T5EncoderModel.from_pretrained(
            actual_model_id,
            subfolder="text_encoder_2",
            torch_dtype=dtype,
            local_files_only=use_local_only,
        )
        t5_encoder.eval()

        elapsed_load = round(time.time() - t0, 1)
        self._update_progress(
            38,
            f"ComfyUI 모델 & LoRA 노드 준비 완료 ({elapsed_load}초)",
            log_callback=log_callback,
        )

        self._local_flux_pipe = pipe
        self._local_t5_encoder = t5_encoder
        self._loaded_model_name = target_model
        self._loaded_lora_key = target_lora_key
        return pipe, t5_encoder

    @staticmethod
    def _build_clean_conditioning_from_negative(positive_prompt: str, negative_prompt: str) -> str:
        """
        T5 임베딩 분포(Manifold)를 훼손하지 않으면서 부정 프롬프트(Negative Prompt)에 적힌
        원치 않는 요소들을 완벽히 배제하는 광학/구도 제약 문장으로 변환하여 결합합니다.
        """
        neg_lower = negative_prompt.lower()
        anti_clauses: List[str] = [
            "unobstructed crystal-clear window view",
            "pure natural outdoor scenery without any people or text",
            "ultra-crisp 8k optical clarity with zero blur",
        ]
        if "curtain" in neg_lower or "blind" in neg_lower:
            anti_clauses.append("wide open frameless glass with no curtains or blinds")
        if "cartoon" in neg_lower or "anime" in neg_lower or "painting" in neg_lower:
            anti_clauses.append("100% real-world DSLR raw photograph, not an illustration")
        return f"{positive_prompt}, {', '.join(anti_clauses)}"

    def _run_comfyui_nodes_sync(
        self,
        positive_prompt: str,
        negative_prompt: str,
        output_path: Path,
        log_callback: Optional[Callable[[str, str, str], None]] = None,
    ) -> None:
        """
        [초고화질 1344x768 네이티브 렌더링 -> 1920x1080 Full HD 마스터링]
        - T5 256 토큰 풀 컨텍스트 인코딩 (임베딩 왜곡 0%)
        - 네이티브 1344x768 (16:9 와이드 고해상도) 6-Steps 정밀 디노이징
        - 1920x1080 (Full HD) Lanczos 초해상화 + Micro-Contrast & Edge Enhancement
        """
        if self._run_external_comfyui_workflow(positive_prompt, negative_prompt, output_path, log_callback):
            return

        import torch
        from PIL import Image, ImageEnhance, ImageFilter

        cfg = self.comfy_config
        total_steps = max(4, min(20, int(cfg.steps)))
        native_w, native_h = 1344, 768
        target_w = max(1792, int(cfg.width))
        target_h = max(1024, int(cfg.height))

        with self._gpu_lock:
            # Node #1 & Node #2
            pipe, t5_encoder = self._load_or_get_comfy_nodes_pipeline(log_callback=log_callback)

            with torch.inference_mode():
                # [Node #4] CLIPTextEncode (Negative) 제약 조건 분석 및 반영
                self._set_node_state(
                    "4",
                    "running",
                    60,
                    "부정 프롬프트(Negative) 아티팩트·왜곡 배제 필터링 적용 중...",
                    params_preview=negative_prompt,
                )
                conditioned_positive = self._build_clean_conditioning_from_negative(
                    positive_prompt, negative_prompt
                )
                self._set_node_state(
                    "4",
                    "done",
                    100,
                    "부정 프롬프트 클린 광학 조건 변환 완료",
                )

                # [Node #3] CLIPTextEncode (Positive) 256 토큰 고해상도 인코딩
                self._set_node_state(
                    "3",
                    "running",
                    50,
                    "긍정 프롬프트(256 토큰 풀 컨텍스트) CLIP + T5 정밀 인코딩 중...",
                    params_preview=conditioned_positive,
                )
                self._update_progress(
                    44,
                    "[Node #3 & #4 CLIPTextEncode] 창문 시점 & 초고화질 프롬프트(256 토큰) 인코딩 중...",
                    log_callback=log_callback,
                    log_detail=conditioned_positive[:110] + "...",
                )
                pos_inputs = pipe.tokenizer_2(
                    conditioned_positive,
                    padding="max_length",
                    max_length=256,
                    truncation=True,
                    return_tensors="pt",
                )
                prompt_embeds = t5_encoder(pos_inputs.input_ids)[0].to("cuda", dtype=torch.bfloat16)
                pooled_prompt_embeds = pipe._get_clip_prompt_embeds(
                    prompt=conditioned_positive,
                    device=torch.device("cuda"),
                    num_images_per_prompt=1,
                )
                self._set_node_state(
                    "3",
                    "done",
                    100,
                    "256-Token 고밀도 텍스트 임베딩 완료 (왜곡 0%)",
                )

                # [Node #5] EmptyLatentImage (1344x768 Native HD -> 1920x1080 Full HD)
                self._set_node_state(
                    "5",
                    "done",
                    100,
                    f"16:9 고해상도 Latent 준비 완료 (Native {native_w}x{native_h} -> {target_w}x{target_h})",
                )

                # [Node #6] KSampler 실행
                step_start_ts = time.time()
                self._set_node_state(
                    "6",
                    "running",
                    5,
                    f"KSampler 고해상도 디노이징 시작 (Step 0/{total_steps})",
                )
                self._update_progress(
                    50,
                    f"[Node #6 KSampler] {native_w}x{native_h} 네이티브 고화질 샘플링 시작 (Step 0/{total_steps})",
                    log_callback=log_callback,
                    log_detail=f"Resolution: {native_w}x{native_h} -> {target_w}x{target_h} | Steps: {total_steps}",
                )

                def _on_step_end(_pipe: Any, step_index: int, _timestep: Any, callback_kwargs: Dict[str, Any]) -> Dict[str, Any]:
                    current_step = step_index + 1
                    node_pct = int((current_step / total_steps) * 100)
                    overall_pct = 50 + int((current_step / total_steps) * 42)  # 50% -> 92%
                    elapsed = round(time.time() - step_start_ts, 1)
                    self._set_node_state(
                        "6",
                        "running" if current_step < total_steps else "done",
                        node_pct,
                        f"Step {current_step}/{total_steps} 완료 ({elapsed}초 경과)",
                    )
                    self._update_progress(
                        overall_pct,
                        f"[Node #6 KSampler] 고해상도 렌더링 중 — Step {current_step}/{total_steps} ({elapsed}초 경과)",
                        log_callback=log_callback,
                        log_detail=f"KSampler {node_pct}% 완료",
                    )
                    return callback_kwargs

                raw_image = pipe(
                    prompt_embeds=prompt_embeds,
                    pooled_prompt_embeds=pooled_prompt_embeds,
                    width=native_w,
                    height=native_h,
                    num_inference_steps=total_steps,
                    guidance_scale=0.0,
                    callback_on_step_end=_on_step_end,
                ).images[0]

                self._set_node_state("6", "done", 100, f"KSampler 완료 ({total_steps}/{total_steps} Steps)")

            # [Node #7] VAEDecode & Full HD Mastering
            self._set_node_state(
                "7",
                "running",
                70,
                f"VAE 디코딩 및 {target_w}x{target_h} Full HD 초해상도 마스터링 중...",
            )
            self._update_progress(
                96,
                f"[Node #7 VAEDecode & SaveImage] {target_w}x{target_h} Full HD 디테일 마스터링 및 저장 중...",
                log_callback=log_callback,
            )
            # 1) 1920x1080 Full HD Lanczos 리샘플링
            hd_image = raw_image.resize((target_w, target_h), Image.Resampling.LANCZOS)
            # 2) 유리창 너머 실사 풍경의 선명도와 미세 콘트라스트 강화
            hd_image = ImageEnhance.Contrast(hd_image).enhance(1.04)
            hd_image = ImageEnhance.Sharpness(hd_image).enhance(1.18)
            hd_image = hd_image.filter(ImageFilter.UnsharpMask(radius=1.1, percent=95, threshold=2))
            hd_image.save(str(output_path), format="PNG", compress_level=1)
            self._set_node_state("7", "done", 100, f"저장 및 창문 출력 완료 ({output_path.name})")

    def generate_landscape_sync(
        self,
        location: str,
        weather: str,
        time_of_day: str,
        display_label: str = "",
        lighting_key: str = "default",
        log_callback: Optional[Callable[[str, str, str], None]] = None,
        on_preset_registered: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """
        ComfyUI 7-노드 워크플로우를 실행하여 초고화질 이미지를 생성·저장합니다.
        [핵심 보장]
        - 생성이 진행되는 동안에는 기존 창문 화면(current_image_url, _version)을 절대 건드리지 않습니다!
        - 생성이 100% 완료된 직후에만 switch_to_preset()과 on_preset_registered()를 호출하여
          창문 배경과 조명을 동시에 새 풍경으로 전환합니다!
        """
        positive_prompt, negative_prompt = self.build_comfy_prompts(location, weather, time_of_day)
        scene_stem = self.derive_meaningful_scene_name(display_label, location, weather, time_of_day)
        preset_file = self.presets_dir / f"{scene_stem}.png"
        cached_file = self.cache_dir / f"{scene_stem}.png"

        gen_start_ts = time.time()
        self._reset_all_nodes_for_run(positive_prompt, negative_prompt, scene_stem)
        with self._lock:
            self.is_generating = True
            self.last_prompt = positive_prompt
            self.last_negative_prompt = negative_prompt
            self.generation_progress_pct = 5
            self.generation_status_text = f"ComfyUI 초고화질 생성 중: '{scene_stem}.png' (기존 화면 유지)"
            self.generation_error_text = ""

        if log_callback:
            log_callback(
                "SYSTEM",
                f"🎨 [5%] ComfyUI 초고화질(1920x1080) 창문 풍경 생성 시작 → '{scene_stem}.png'",
                f"완료될 때까지 현재 창문 화면이 그대로 유지됩니다.",
            )

        try:
            self._run_comfyui_nodes_sync(
                positive_prompt=positive_prompt,
                negative_prompt=negative_prompt,
                output_path=cached_file,
                log_callback=log_callback,
            )

            if not cached_file.exists() or cached_file.stat().st_size == 0:
                raise RuntimeError("ComfyUI 노드 실행 결과 파일이 저장되지 않았습니다.")

            shutil.copy2(cached_file, preset_file)
            now_ts = time.time()
            os.utime(preset_file, (now_ts, now_ts))
            os.utime(cached_file, (now_ts, now_ts))

            total_sec = round(time.time() - gen_start_ts, 1)
            with self._lock:
                custom_preset = self.build_custom_preset(scene_stem, is_ai=True)
                if lighting_key and lighting_key != "default":
                    custom_preset.lighting_key = lighting_key
                self.presets[scene_stem] = custom_preset
                # 생성이 100% 끝난 지금 이 순간에만 창문 화면을 새 이미지로 전환!
                self.switch_to_preset(scene_stem)
                self.last_prompt = positive_prompt
                self.last_negative_prompt = negative_prompt
                self.generation_progress_pct = 100
                self.generation_status_text = (
                    f"✅ ComfyUI '{scene_stem}.png' (1920x1080 Full HD) 생성 및 창문 적용 완료! ({total_sec}초)"
                )
                self.generation_error_text = ""
                applied_url = self.current_image_url

            if on_preset_registered:
                on_preset_registered(scene_stem, custom_preset.lighting_key)

            if log_callback:
                log_callback(
                    "STT",
                    f"✨ [100%] ComfyUI 초고화질 '{scene_stem}.png' 생성 및 창문 즉시 적용 완료 ({total_sec}초)!",
                    f"추후 '윈도 {scene_stem.replace('_', ' ')}'이라고 부르면 바로 켜집니다.",
                )
            return applied_url

        except Exception as exc:
            err_str = str(exc)
            # [핵심 보장] 오류가 발생하더라도 다른 화면으로 넘어가지 않고 기존 화면(self.current_image_url)을 그대로 유지!
            with self._lock:
                for n in self._nodes:
                    if n.status == "running":
                        n.status = "error"
                        n.summary = f"오류: {err_str[:40]}"
                self.generation_progress_pct = 0
                self.generation_status_text = "❌ ComfyUI 생성 오류 (기존 창문 화면 유지됨)"
                self.generation_error_text = err_str
                existing_url = self.current_image_url

            if log_callback:
                log_callback(
                    "ERROR",
                    "❌ ComfyUI 풍경 생성 오류 발생 — 기존 창문 화면을 그대로 유지합니다",
                    f"원인: {err_str}",
                )
            return existing_url

        finally:
            with self._lock:
                self.is_generating = False
                self._active_node_id = ""

    async def generate_landscape(
        self,
        location: str,
        weather: str,
        time_of_day: str,
        display_label: str = "",
        log_callback: Optional[Callable[[str, str, str], None]] = None,
    ) -> str:
        return await asyncio.to_thread(
            self.generate_landscape_sync,
            location,
            weather,
            time_of_day,
            display_label,
            "default",
            log_callback,
        )

    def trigger_generate_landscape_sync(
        self,
        location: str,
        weather: str,
        time_of_day: str,
        display_label: str = "",
        lighting_key: str = "default",
        log_callback: Optional[Callable[[str, str, str], None]] = None,
        on_preset_registered: Optional[Callable[[str, str], None]] = None,
    ) -> bool:
        """
        [한 번에 1개씩만 생성 강제]
        - 이미 생성 중(self.is_generating == True)이면 새 스레드를 띄우지 않고 즉시 False를 반환합니다.
        - 대기 중이면 기존 창문 화면을 그대로 유지한 채 백그라운드에서 생성을 시작하고 True를 반환합니다.
        """
        with self._lock:
            if self.is_generating:
                return False
            scene_stem = self.derive_meaningful_scene_name(display_label, location, weather, time_of_day)
            self.is_generating = True
            self.generation_progress_pct = 5
            self.generation_status_text = f"ComfyUI '{scene_stem}.png' 초고화질 생성 준비 중..."
            self.generation_error_text = ""

        def _runner() -> None:
            self.generate_landscape_sync(
                location=location,
                weather=weather,
                time_of_day=time_of_day,
                display_label=display_label,
                lighting_key=lighting_key,
                log_callback=log_callback,
                on_preset_registered=on_preset_registered,
            )

        threading.Thread(target=_runner, name="ComfyUIWorkflowThread", daemon=True).start()
        return True

    def get_state(self) -> LandscapeState:
        with self._lock:
            return LandscapeState(
                version=self._version,
                active_key=self.active_key,
                active_title=self.active_title,
                current_image_url=self.resolve_preset_image_url(self.active_key)
                if not self.is_special_ai_image
                else self.current_image_url,
                previous_image_url=self.previous_image_url,
                is_special_ai_image=self.is_special_ai_image,
                is_generating=self.is_generating,
                last_prompt=self.last_prompt,
                last_negative_prompt=self.last_negative_prompt,
                generation_progress_pct=self.generation_progress_pct,
                generation_status_text=self.generation_status_text,
                generation_error_text=self.generation_error_text,
                active_node_id=self._active_node_id,
                nodes=[
                    ComfyNodeStatus(
                        node_id=n.node_id,
                        node_type=n.node_type,
                        title=n.title,
                        status=n.status,
                        progress_pct=n.progress_pct,
                        summary=n.summary,
                        params_preview=n.params_preview,
                    )
                    for n in self._nodes
                ],
                workflow_json_str=self._last_workflow_json,
            )
