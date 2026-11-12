"""
앰비언트 사운드(MP3) 재생 제어 및 Gemini Vision API 기반 배경 맞춤 사운드 매칭 모듈
(src/modules/ambient_sound_manager.py)

핵심 기능:
1. 명령어 기반 MP3 사운드 재생 ("~ 들려줘", "~ 소리 틀어줘", "소리 꺼줘"):
   - 프로젝트 루트의 `mp3/` 폴더에 있는 MP3 파일들(비 소리, 비번개 소리, 도시 소리, 거친 바다 소리,
     해변소리, 숲 소리, 밤 소리, 시골 소리, 캠프파이어 소리, 조용히 들리는 바람소리, 소름끼치는 바람소리, 폭우 소리 등)을
     자동 스캔하고, 사용자의 음성/텍스트 명령에 맞는 소리를 즉시 반복 재생(Loop)합니다.
2. Gemini Vision API 기반 '배경에 맞는 소리' 자동 분석 및 재생:
   - 현재 창문 배경 이미지와 씬 정보를 Gemini API(및 컴퓨터 비전 + 시맨틱 분석 폴백)로 분석하여,
     `mp3/` 폴더 내 파일 중 배경 분위기에 가장 잘 어울리는 MP3 파일을 자동 선별해 재생합니다.
   - '배경 변경 시 자동 맞춤 소리 재생(Auto-Sync)' 모드를 켜두면 배경이 바뀔 때마다 자동으로 알맞은 소리로 전환됩니다.
3. 설정창(⚙️) UI 완벽 연동:
   - 소리 켜기/끄기, 볼륨 조절(0~100%), 개별 MP3 선택 재생, AI 배경 맞춤 소리 즉시 실행 및 자동 동기화 토글을 지원합니다.
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import queue
import re
import shutil
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image


BASE_DIR = Path(__file__).resolve().parent.parent.parent


@dataclass
class SoundTrackInfo:
    """개별 MP3 트랙 메타데이터"""
    filename: str          # 예: "비 소리.mp3"
    title: str             # 예: "비 소리"
    icon: str              # 예: "🌧️"
    category: str          # 예: "rain"
    keywords: Tuple[str, ...]
    file_path: Path
    url_path: str          # 예: "/mp3/%EB%B9%84%20%EC%86%8C%EB%A6%AC.mp3"


class AmbientSoundManager:
    """
    `mp3/` 폴더의 효과음/자연음 MP3 파일들을 관리하고,
    1) "~ 들려줘" 음성/텍스트 명령 매칭 재생
    2) Gemini API 기반 배경 이미지 분석 -> 최적 MP3 자동 매칭 재생
    3) Windows 네이티브 오디오(winmm MCI) + 브라우저 오디오 연동을 수행하는 매니저
    """

    DEFAULT_GEMINI_API_KEY = "AQ.Ab8RN6IZwGOGjcJnOJSzxNnvym-uYhRSsHpjiDrR9GpbGrG2Ug"

    # 파일명(stem)별 아이콘 및 풍부한 한국어/영어 동의어·연관어 사전
    TRACK_METADATA_PRESETS: Dict[str, Dict[str, Any]] = {
        "비 소리": {
            "icon": "🌧️",
            "category": "rain",
            "keywords": (
                "비 소리", "비소리", "빗소리", "비 내리는 소리", "비오는 소리", "비 오는 소리",
                "잔잔한 비", "우천", "장마", "부슬비", "가랑비", "비", "rain",
            ),
        },
        "폭우 소리": {
            "icon": "🌧️",
            "category": "heavy_rain",
            "keywords": (
                "폭우 소리", "폭우소리", "폭우", "소나기", "장대비", "억수", "강한 비",
                "세찬 비", "폭풍우", "호우", "heavy rain", "downpour",
            ),
        },
        "비번개 소리": {
            "icon": "⛈️",
            "category": "thunder",
            "keywords": (
                "비번개 소리", "비번개소리", "비번개", "번개 소리", "번개소리", "번개",
                "천둥 소리", "천둥소리", "천둥", "천둥번개", "뇌우", "벼락", "먹구름", "thunder", "lightning",
            ),
        },
        "도시 소리": {
            "icon": "🏙️",
            "category": "city",
            "keywords": (
                "도시 소리", "도시소리", "도시", "도심", "거리 소리", "거리소리", "거리",
                "명동", "명동거리", "런던", "시계탑", "빌딩", "자동차", "차 소리", "길거리",
                "서울", "도쿄", "뉴욕", "야경 도시", "번화가", "시내", "city", "street", "urban",
            ),
        },
        "거친 바다 소리": {
            "icon": "🌊",
            "category": "rough_sea",
            "keywords": (
                "거친 바다 소리", "거친바다소리", "거친 바다", "거친바다", "거친 파도",
                "파도 소리", "파도소리", "강한 파도", "깊은 바다", "망망대해", "바다 폭풍", "ocean wave",
            ),
        },
        "해변소리": {
            "icon": "🏖️",
            "category": "beach",
            "keywords": (
                "해변소리", "해변 소리", "해변", "바닷가 소리", "바닷가", "바다 소리", "바다소리",
                "바다", "잔잔한 바다", "잔잔한 파도", "해수욕장", "모래사장", "윤슬", "섬", "beach", "sea",
            ),
        },
        "숲 소리": {
            "icon": "🌲",
            "category": "forest",
            "keywords": (
                "숲 소리", "숲소리", "숲속 소리", "숲속", "숲", "나무 소리", "나무",
                "산 소리", "산", "새소리", "새 소리", "공원", "자연 소리", "자연", "녹음",
                "아침 숲", "아침 풍경", "청량한", "forest", "woods", "birds", "nature",
            ),
        },
        "밤 소리": {
            "icon": "🌙",
            "category": "night",
            "keywords": (
                "밤 소리", "밤소리", "밤하늘 소리", "밤", "저녁 소리", "저녁소리", "저녁",
                "풀벌레", "귀뚜라미", "벌레 소리", "새벽", "달빛", "고요한 밤", "여름밤", "가을밤",
                "night", "evening", "cricket",
            ),
        },
        "시골 소리": {
            "icon": "🏡",
            "category": "countryside",
            "keywords": (
                "시골 소리", "시골소리", "시골", "농촌", "들판", "논밭", "고향",
                "마을", "초원", "목장", "평화로운", "한적한", "오두막", "countryside", "village", "farm",
            ),
        },
        "캠프파이어 소리": {
            "icon": "🔥",
            "category": "campfire",
            "keywords": (
                "캠프파이어 소리", "캠프파이어소리", "캠프파이어", "모닥불 소리", "모닥불소리",
                "모닥불", "장작 소리", "장작", "불멍", "불 소리", "불소리", "벽난로", "캠핑",
                "따뜻한", "노을 캠핑", "campfire", "bonfire", "fire",
            ),
        },
        "조용히 들리는 바람소리": {
            "icon": "🍃",
            "category": "gentle_wind",
            "keywords": (
                "조용히 들리는 바람소리", "조용히 들리는 바람 소리", "조용한 바람", "잔잔한 바람",
                "바람 소리", "바람소리", "바람", "산들바람", "미풍", "선선한 바람", "구름", "하늘",
                "언덕", "평원", "wind", "breeze",
            ),
        },
        "소름끼치는 바람소리": {
            "icon": "🌬️",
            "category": "strong_wind",
            "keywords": (
                "소름끼치는 바람소리", "소름끼치는 바람 소리", "소름끼치는 바람", "무서운 바람",
                "강풍", "칼바람", "매서운 바람", "겨울 바람", "눈보라", "설산", "절벽", "황량한",
                "howling wind", "blizzard", "storm wind",
            ),
        },
    }

    def __init__(self, mp3_dir: Optional[Path] = None, gemini_api_key: Optional[str] = None) -> None:
        self._lock = threading.RLock()

        # mp3 폴더 경로 탐색 (현재 폴더의 mp3 또는 상위 폴더의 mp3)
        cand_dir = mp3_dir or (BASE_DIR / "mp3")
        if not cand_dir.exists() and (BASE_DIR.parent / "mp3").exists():
            cand_dir = BASE_DIR.parent / "mp3"
        self.mp3_dir: Path = cand_dir

        self.gemini_api_key: str = (
            gemini_api_key
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or self.DEFAULT_GEMINI_API_KEY
        )

        self.tracks: Dict[str, SoundTrackInfo] = {}
        self.is_playing: bool = False
        self.current_filename: str = ""
        self.current_title: str = "재생 중인 소리 없음"
        self.current_icon: str = "🔇"
        self.volume_pct: int = 75            # 0 ~ 100%
        self.auto_match_bg: bool = False     # 배경 전환 시 자동으로 배경에 맞는 소리 재생 여부
        self.last_match_reason: str = "원하는 소리를 선택하거나 '🤖 배경에 맞는 소리 들려줘'라고 말씀해 보세요."
        self.last_match_source: str = "대기 중"
        self.play_version: int = 0

        # 배경별 Gemini 추천 캐시: cache_key -> (filename, reason_ko, source_label)
        self._bg_sound_cache: Dict[str, Tuple[str, str, str]] = {}

        self.scan_mp3_folder()

    # =========================================================================
    # 1. mp3/ 폴더 실시간 스캔 및 트랙 메타데이터 구성
    # =========================================================================
    def scan_mp3_folder(self) -> List[SoundTrackInfo]:
        if not self.mp3_dir.exists():
            return []

        found: Dict[str, SoundTrackInfo] = {}
        for fpath in sorted(self.mp3_dir.glob("*.mp3")):
            if not fpath.is_file():
                continue
            fname = fpath.name
            stem = fpath.stem.strip()
            preset = self.TRACK_METADATA_PRESETS.get(stem, {})
            icon = str(preset.get("icon", "🎵"))
            category = str(preset.get("category", "custom"))
            base_kws = list(preset.get("keywords", ()))

            # 파일명 자체의 변형들도 키워드에 자동 포함
            clean_stem = stem.replace("_", " ").strip()
            compact_stem = clean_stem.replace(" ", "")
            no_sori = clean_stem.replace("소리", "").strip()
            auto_kws = [clean_stem, compact_stem]
            if no_sori:
                auto_kws.append(no_sori)
                auto_kws.append(no_sori.replace(" ", ""))
            for kw in auto_kws:
                if kw and kw not in base_kws:
                    base_kws.append(kw)

            url_encoded = "/mp3/" + urllib.parse.quote(fname)
            found[fname] = SoundTrackInfo(
                filename=fname,
                title=clean_stem,
                icon=icon,
                category=category,
                keywords=tuple(base_kws),
                file_path=fpath,
                url_path=url_encoded,
            )

        with self._lock:
            self.tracks = found
        return list(found.values())

    def list_tracks(self) -> List[SoundTrackInfo]:
        self.scan_mp3_folder()
        with self._lock:
            return list(self.tracks.values())

    # =========================================================================
    # 3. 트랙 재생 / 정지 / 볼륨 제어 API
    # =========================================================================
    def play_track(
        self,
        filename: str,
        reason: str = "사용자 직접 선택",
        source_label: str = "수동/음성 선택",
    ) -> Optional[SoundTrackInfo]:
        self.scan_mp3_folder()
        with self._lock:
            track = self.tracks.get(filename)
            if track is None:
                # stem 이름으로 찾은 경우 허용
                for t in self.tracks.values():
                    if t.title == filename or t.file_path.stem == filename:
                        track = t
                        break
            if track is None:
                return None

            self.is_playing = True
            self.current_filename = track.filename
            self.current_title = track.title
            self.current_icon = track.icon
            self.last_match_reason = reason
            self.last_match_source = source_label
            self.play_version += 1

        return track

    def stop(self, reason: str = "재생 중지됨") -> None:
        with self._lock:
            self.is_playing = False
            self.last_match_reason = reason
            self.play_version += 1

    def set_volume(self, volume_pct: int) -> int:
        clamped = int(np.clip(int(volume_pct), 0, 100))
        with self._lock:
            self.volume_pct = clamped
            self.play_version += 1
        return clamped

    def toggle_auto_match_bg(self, enable: Optional[bool] = None) -> bool:
        with self._lock:
            if enable is None:
                self.auto_match_bg = not self.auto_match_bg
            else:
                self.auto_match_bg = bool(enable)
            return self.auto_match_bg

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            track = self.tracks.get(self.current_filename)
            url_path = track.url_path if track else ""
            return {
                "is_playing": self.is_playing,
                "filename": self.current_filename,
                "title": self.current_title if self.is_playing else "재생 중지됨",
                "icon": self.current_icon if self.is_playing else "🔇",
                "url_path": url_path if self.is_playing else "",
                "volume_pct": self.volume_pct,
                "auto_match_bg": self.auto_match_bg,
                "reason": self.last_match_reason,
                "source": self.last_match_source,
                "play_version": self.play_version,
            }

    # =========================================================================
    # 4. [기능 1] 음성/텍스트 명령어로 소리 찾기 & 들려주기 ("비 소리 들려줘", "도시 소리 들려줘" 등)
    # =========================================================================
    def find_best_track_for_query(self, query_text: str) -> Optional[SoundTrackInfo]:
        self.scan_mp3_folder()
        q_clean = query_text.strip().lower()
        q_compact = q_clean.replace(" ", "")

        # 불필요한 어미/조사 제거
        for suffix in (
            "들려줘", "들려주세요", "들려주라", "들려", "틀어줘", "틀어주세요", "틀어",
            "재생해줘", "재생해", "켜줘", "켜", "소리", "음악", "사운드", "효과음", "브금",
            "윈도우", "윈도", "windo", "window",
        ):
            q_compact = q_compact.replace(suffix, "")

        with self._lock:
            track_list = list(self.tracks.values())

        if not track_list:
            return None

        # 1단계: 정확한 stem 매칭 우선 검사
        # 예: "비번개 소리 들려줘" vs "비 소리 들려줘", "거친 바다 소리" vs "해변소리"
        best_track: Optional[SoundTrackInfo] = None
        best_score: float = 0.0

        raw_compact = query_text.strip().lower().replace(" ", "")

        for tr in track_list:
            score = 0.0
            stem_compact = tr.title.replace(" ", "").lower()
            stem_no_sori = stem_compact.replace("소리", "")

            if stem_compact and stem_compact in raw_compact:
                score += 100.0 + len(stem_compact) * 5.0
            if stem_no_sori and (stem_no_sori == q_compact):
                score += 120.0 + len(stem_no_sori) * 8.0
            elif stem_no_sori and (stem_no_sori in q_compact or stem_no_sori in raw_compact):
                score += 65.0 + len(stem_no_sori) * 6.0

            for kw in tr.keywords:
                kw_comp = kw.replace(" ", "").lower()
                if not kw_comp:
                    continue
                if kw_comp == q_compact:
                    score = max(score, 110.0 + len(kw_comp) * 6.0)
                elif kw_comp in raw_compact:
                    score = max(score, 50.0 + len(kw_comp) * 4.5)

            if score > best_score:
                best_score = score
                best_track = tr

        return best_track if best_score >= 45.0 else None

    def apply_voice_command(self, raw_text: str) -> Optional[str]:
        """
        음성 또는 텍스트 입력에서 사운드 제어 의도를 파악합니다.
        반환값:
        - "played": 특정 MP3 재생 시작됨
        - "match_bg": '배경에 맞는 소리 들려줘' 요청 감지됨 (호출 측에서 match_and_play_for_background 실행)
        - "stopped": 소리 재생 중지됨
        - "volume": 볼륨 조절됨
        - None: 사운드 명령이 아님
        """
        text = str(raw_text).strip()
        if not text:
            return None

        cleaned = text.lower()
        for ww in ("윈도우", "윈도", "window", "windo"):
            if cleaned.startswith(ww):
                cleaned = cleaned[len(ww):].strip()

        # 이미지 생성/검색 명령과 충돌 방지
        if any(v in cleaned for v in ("만들어", "그려", "생성", "찾아", "검색")):
            return None

        compact = cleaned.replace(" ", "")

        # 1) 소리 끄기 / 정지 명령 ("소리 꺼줘", "소리 멈춰", "음악 꺼줘", "조용히 해")
        stop_phrases = (
            "소리꺼", "소리멈춰", "소리중지", "소리끄기", "소리닫아",
            "음악꺼", "음악멈춰", "음악중지", "사운드꺼", "사운드멈춰",
            "노래꺼", "노래멈춰", "브금꺼", "효과음꺼", "그만들려줘", "소리안나게",
        )
        if any(sp in compact for sp in stop_phrases):
            self.stop(reason=f"음성 명령(\"{text}\")으로 소리를 껐습니다.")
            return "stopped"

        # 2) 볼륨 조절 명령 ("소리 볼륨 80", "볼륨 50", "소리 크게", "소리 작게")
        if any(v_kw in compact for v_kw in ("볼륨", "음량", "소리크기")):
            nums = [int(n) for n in re.findall(r"\d+", cleaned)]
            if nums:
                self.set_volume(nums[0])
                return "volume"
        if any(w in compact for w in ("소리크게", "소리더크게", "볼륨업", "볼륨올려", "음량올려")):
            self.set_volume(self.volume_pct + 20)
            return "volume"
        if any(w in compact for w in ("소리작게", "소리줄여", "볼륨다운", "볼륨낮춰", "음량줄여")):
            self.set_volume(self.volume_pct - 20)
            return "volume"

        # 3) "배경에 맞는 소리 들려줘" / "배경 소리 들려줘" / "어울리는 소리 들려줘" 감지
        bg_match_phrases = (
            "배경에맞는소리", "배경맞는소리", "배경소리", "배경에어울리는소리",
            "어울리는소리", "맞는소리", "알맞는소리", "알맞은소리",
            "배경음악", "풍경에맞는소리", "풍경소리", "화면에맞는소리", "분위기에맞는소리",
        )
        has_play_trigger = any(
            t in compact
            for t in ("들려줘", "들려", "틀어줘", "틀어", "재생", "켜줘", "켜", "해줘", "소리")
        )
        if has_play_trigger and any(bp in compact for bp in bg_match_phrases):
            return "match_bg"

        # 4) "~ 들려줘" / "~ 소리 틀어줘" / "~ 소리 켜줘" 특정 MP3 재생 감지
        is_explicit_sound_req = any(
            t in compact for t in ("들려줘", "들려주세요", "들려주라", "소리틀어", "소리켜", "소리재생")
        ) or ("소리" in compact and any(t in compact for t in ("틀어", "켜줘", "재생", "들려")))

        if is_explicit_sound_req:
            matched_track = self.find_best_track_for_query(cleaned)
            if matched_track:
                self.play_track(
                    matched_track.filename,
                    reason=f"음성 명령(\"{text}\")에 따라 '{matched_track.title}'를 재생합니다.",
                    source_label="음성 명령 매칭",
                )
                return "played"
            # 만약 단순히 "소리 들려줘" / "소리 켜줘"라고만 한 경우 -> 현재 배경에 맞는 소리로 자동 연결!
            if compact in ("소리들려줘", "소리켜줘", "소리틀어줘", "음악들려줘", "음악틀어줘", "사운드켜줘"):
                return "match_bg"

        return None

    # =========================================================================
    # 5. [기능 2] 배경 이미지 분석 (Gemini Vision API + CV/시맨틱 폴백) -> 알맞은 MP3 자동 매칭
    # =========================================================================
    def _heuristic_match_background(
        self,
        bg_file_path: Optional[Path],
        scene_hint: str = "",
    ) -> Tuple[str, str]:
        """
        1차 즉시 응답용 스마트 분석기:
        씬 제목/파일명 키워드 및 이미지 픽셀(바다 청록색, 숲 녹색, 밤 어두움, 노을 붉은빛 등)을 분석하여
        가장 알맞은 MP3 파일명과 선정 이유를 반환합니다.
        """
        self.scan_mp3_folder()
        stem = bg_file_path.stem if bg_file_path else ""
        combined = f"{scene_hint} {stem}".lower()

        # 1순위: 텍스트 힌트 기반 시맨틱 매칭
        rules: List[Tuple[Tuple[str, ...], str, str]] = [
            (("번개", "뇌우", "천둥", "벼락", "thunder", "lightning"), "비번개 소리.mp3", "천둥·번개가 치는 날씨 풍경에 맞춰 '비번개 소리'를 선별했습니다."),
            (("폭우", "소나기", "폭풍우", "호우", "storm"), "폭우 소리.mp3", "강하게 쏟아지는 비 풍경에 맞춰 '폭우 소리'를 선별했습니다."),
            (("비", "우천", "장마", "빗줄기", "rain"), "비 소리.mp3", "촉촉하게 비가 내리는 풍경에 맞춰 '비 소리'를 선별했습니다."),
            (("거친 바다", "파도", "폭풍 바다", "망망대해"), "거친 바다 소리.mp3", "역동적인 바다 물결 풍경에 맞춰 '거친 바다 소리'를 선별했습니다."),
            (("바다", "해변", "해수욕장", "바닷가", "섬", "윤슬", "ocean", "beach", "sea"), "해변소리.mp3", "시원하게 펼쳐진 바다·해변 풍경에 맞춰 '해변소리'를 선별했습니다."),
            (("명동", "거리", "도시", "도심", "런던", "시계탑", "서울", "도쿄", "뉴욕", "빌딩", "반지하", "스카이돔", "고척", "city", "street"), "도시 소리.mp3", "도심 거리와 건축물이 어우러진 풍경에 맞춰 '도시 소리'를 선별했습니다."),
            (("캠프", "모닥불", "불멍", "벽난로", "장작", "노을", "석양", "일몰", "황혼", "sunset"), "캠프파이어 소리.mp3", "따뜻한 노을·감성적인 휴식 분위기에 맞춰 '캠프파이어 소리'를 선별했습니다."),
            (("저녁", "밤", "야경", "달빛", "새벽", "새녁", "evening", "night", "moon"), "밤 소리.mp3", "고요한 밤하늘과 저녁 분위기에 맞춰 풀벌레가 우는 '밤 소리'를 선별했습니다."),
            (("설산", "눈보라", "절벽", "강풍", "겨울 바람", "유미", "전투"), "소름끼치는 바람소리.mp3", "긴장감 있거나 매서운 바람이 부는 분위기에 맞춰 '소름끼치는 바람소리'를 선별했습니다."),
            (("시골", "농촌", "들판", "논밭", "마을", "고향", "오두막"), "시골 소리.mp3", "정겹고 한적한 풍경에 맞춰 '시골 소리'를 선별했습니다."),
            (("숲", "나무", "산", "공원", "자연", "아침", "일출", "morning", "forest"), "숲 소리.mp3", "상쾌한 아침과 자연 녹음 풍경에 맞춰 새소리가 담긴 '숲 소리'를 선별했습니다."),
        ]

        for kws, target_mp3, reason_str in rules:
            if any(k in combined for k in kws) and target_mp3 in self.tracks:
                return target_mp3, reason_str

        # 2순위: 이미지 픽셀 색채·명도 분포 분석
        if bg_file_path and bg_file_path.exists():
            try:
                pil_img = Image.open(bg_file_path).convert("RGB").resize((160, 90), Image.Resampling.BILINEAR)
                hsv_arr = np.array(pil_img.convert("HSV"), dtype=np.float32)
                h_ch = hsv_arr[:, :, 0] * (180.0 / 255.0)
                s_ch = hsv_arr[:, :, 1]
                v_ch = hsv_arr[:, :, 2]

                mean_v = float(np.mean(v_ch))
                mean_s = float(np.mean(s_ch))

                # 어두운 밤 이미지
                if mean_v < 78.0 and "밤 소리.mp3" in self.tracks:
                    return "밤 소리.mp3", "어두운 밤하늘/야간 풍경으로 분석되어 '밤 소리'를 매칭했습니다."

                # 푸른 바다/청록색 비율 (하단 55% 영역에 청색/청록색이 많음)
                lower_h = h_ch[40:, :]
                lower_s = s_ch[40:, :]
                sea_mask = (lower_h >= 85) & (lower_h <= 130) & (lower_s >= 55)
                if float(np.count_nonzero(sea_mask)) / float(lower_h.size) >= 0.30 and "해변소리.mp3" in self.tracks:
                    return "해변소리.mp3", "수평선과 푸른 바다 물결이 감지되어 '해변소리'를 매칭했습니다."

                # 초록색 숲/산 비율
                green_mask = (h_ch >= 35) & (h_ch <= 85) & (s_ch >= 45) & (v_ch >= 50)
                if float(np.count_nonzero(green_mask)) / float(h_ch.size) >= 0.22 and "숲 소리.mp3" in self.tracks:
                    return "숲 소리.mp3", "울창한 숲과 자연 녹음이 감지되어 '숲 소리'를 매칭했습니다."

                # 노을/따뜻한 주황빛 비율
                warm_mask = ((h_ch <= 22) | (h_ch >= 162)) & (s_ch >= 80) & (v_ch >= 90)
                if float(np.count_nonzero(warm_mask)) / float(h_ch.size) >= 0.25 and "캠프파이어 소리.mp3" in self.tracks:
                    return "캠프파이어 소리.mp3", "따뜻한 석양/노을 색조가 감지되어 '캠프파이어 소리'를 매칭했습니다."

                # 채도가 낮고 흐린 회색빛 하늘 (비/바람)
                if mean_s < 42.0 and mean_v < 145.0 and "비 소리.mp3" in self.tracks:
                    return "비 소리.mp3", "차분하고 흐린 하늘 분위기가 감지되어 '비 소리'를 매칭했습니다."
            except Exception:
                pass

        # 기본 대기 화면(푸른 하늘과 구름/산들바람) -> 조용히 들리는 바람소리 또는 숲 소리
        if "조용히 들리는 바람소리.mp3" in self.tracks:
            return "조용히 들리는 바람소리.mp3", "탁 트인 하늘과 평화로운 창문 풍경에 맞춰 '조용히 들리는 바람소리'를 선별했습니다."
        first_key = next(iter(self.tracks.keys()), "")
        return first_key, "배경 풍경에 어울리는 기본 자연 사운드를 선별했습니다."

    def _query_gemini_for_sound(
        self,
        bg_file_path: Path,
        scene_hint: str,
        available_filenames: List[str],
        timeout_sec: float = 3.2,
    ) -> Optional[Tuple[str, str]]:
        """
        Gemini Vision API를 호출하여 배경 이미지를 분석하고,
        `available_filenames` 중에서 가장 알맞은 MP3 파일명과 한국어 선정 이유를 반환합니다.
        """
        api_key = self.gemini_api_key
        if not api_key or not bg_file_path.exists() or not available_filenames:
            return None

        try:
            pil_img = Image.open(bg_file_path).convert("RGB").resize((512, 288), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=82)
            b64_img = base64.b64encode(buf.getvalue()).decode("ascii")

            files_json_list = json.dumps(available_filenames, ensure_ascii=False)
            prompt_text = (
                "당신은 스마트 창문(WINDO)의 AI 사운드 디렉터입니다. "
                "첨부된 창문 배경 이미지와 힌트(" + str(scene_hint) + ")를 분석하여, "
                "다음 MP3 파일 목록 중에서 이 배경과 가장 잘 어울리는 단 하나의 MP3 파일을 골라주세요.\n"
                "선택 가능한 MP3 파일 목록: " + files_json_list + "\n"
                "반드시 목록에 있는 정확한 파일명(selected_filename)만 선택하고, "
                "왜 그 소리를 골랐는지 한국어로 1문장(reason_ko)으로 작성하여 JSON으로만 응답하세요:\n"
                "{\"selected_filename\": \"파일명.mp3\", \"reason_ko\": \"선정 이유\"}"
            )

            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": prompt_text},
                            {"inlineData": {"mimeType": "image/jpeg", "data": b64_img}},
                        ],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                },
            }
            body_bytes = json.dumps(payload).encode("utf-8")

            endpoints = [
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key=" + api_key,
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + api_key,
                "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-2.5-flash:generateContent?key=" + api_key,
            ]

            for url in endpoints:
                try:
                    req = urllib.request.Request(
                        url,
                        data=body_bytes,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                        resp_data = json.loads(resp.read().decode("utf-8"))
                    candidates = resp_data.get("candidates") or []
                    if not candidates:
                        continue
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if not parts:
                        continue
                    raw_txt = str(parts[0].get("text", "")).strip()
                    if raw_txt.startswith("```"):
                        raw_txt = raw_txt.strip("`")
                        if raw_txt.startswith("json"):
                            raw_txt = raw_txt[4:].strip()
                    parsed = json.loads(raw_txt)
                    sel_file = str(parsed.get("selected_filename", "")).strip()
                    reason_ko = str(parsed.get("reason_ko", "")).strip()

                    # 정확한 파일명 매칭 또는 부분 매칭 확인
                    matched_fname = ""
                    if sel_file in available_filenames:
                        matched_fname = sel_file
                    else:
                        sel_stem = sel_file.replace(".mp3", "").replace(" ", "")
                        for cand_f in available_filenames:
                            if sel_stem and sel_stem in cand_f.replace(" ", ""):
                                matched_fname = cand_f
                                break

                    if matched_fname:
                        if not reason_ko:
                            reason_ko = f"Gemini Vision AI가 배경 이미지를 분석하여 '{matched_fname}'를 추천했습니다."
                        return matched_fname, reason_ko
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def match_and_play_for_background(
        self,
        bg_file_path: Optional[Path],
        scene_hint: str = "",
        on_gemini_updated: Optional[Callable[[SoundTrackInfo, str], None]] = None,
    ) -> Optional[SoundTrackInfo]:
        """
        현재 배경 이미지를 분석하여 가장 잘 어울리는 MP3를 즉시 재생하고,
        Gemini Vision API 분석 결과가 도착하면(또는 캐시되어 있으면) 이를 반영합니다.
        """
        self.scan_mp3_folder()
        with self._lock:
            avail_files = list(self.tracks.keys())
        if not avail_files:
            return None

        path_str = str(bg_file_path) if bg_file_path else "none"
        cache_key = f"{path_str}|{scene_hint}"

        with self._lock:
            cached = self._bg_sound_cache.get(cache_key)

        if cached:
            fname, reason_ko, src_label = cached
            return self.play_track(fname, reason=reason_ko, source_label=src_label)

        # 1) 즉시 응답을 위해 스마트 시맨틱/CV 분석 결과로 먼저 재생 시작
        heur_fname, heur_reason = self._heuristic_match_background(bg_file_path, scene_hint=scene_hint)
        initial_track = self.play_track(
            heur_fname,
            reason=f"🤖 AI 배경 분석: {heur_reason}",
            source_label="AI 배경 사운드 분석기",
        )

        # 2) 백그라운드에서 Gemini Vision API로 실제 배경 이미지를 정밀 분석하여 업데이트
        if bg_file_path and bg_file_path.exists():
            def _gemini_worker() -> None:
                res = self._query_gemini_for_sound(bg_file_path, scene_hint, avail_files, timeout_sec=4.0)
                if res:
                    gem_fname, gem_reason = res
                    full_reason = f"🤖 Gemini Vision 분석: {gem_reason}"
                    with self._lock:
                        self._bg_sound_cache[cache_key] = (gem_fname, full_reason, "Gemini Vision AI")
                    # 여전히 재생 중이라면 Gemini 추천 사운드로 반영 (동일 파일이면 이유/출처만 갱신)
                    with self._lock:
                        still_playing = self.is_playing
                        cur_f = self.current_filename
                    if still_playing:
                        if gem_fname != cur_f:
                            tr = self.play_track(gem_fname, reason=full_reason, source_label="Gemini Vision AI")
                            if tr and on_gemini_updated:
                                on_gemini_updated(tr, full_reason)
                        else:
                            with self._lock:
                                self.last_match_reason = full_reason
                                self.last_match_source = "Gemini Vision AI"
                                self.play_version += 1
                else:
                    with self._lock:
                        self._bg_sound_cache[cache_key] = (
                            heur_fname,
                            f"🤖 AI 배경 분석: {heur_reason}",
                            "AI 배경 사운드 분석기",
                        )

            threading.Thread(target=_gemini_worker, daemon=True, name="GeminiSoundMatchThread").start()

        return initial_track
