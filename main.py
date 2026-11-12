"""
AI 가상 창문 (WINDO) - FastHTML 메인 애플리케이션 (main.py)

핵심 구조:
1. 메인 화면: 하단 바나 불필요한 UI 없이 100% 전체화면 순수 창문 풍경 + 생체리듬 자연광(CCT RGB) & 채도 필터 표시
2. ComfyUI 7-노드 워크플로우 엔진 연동:
   - 설정 모달에서 긍정 프롬프트(Positive), 부정 프롬프트(Negative), 베이스 모델(Model), LoRA 파일 및 강도(Strength), KSampler 설정 지원
   - 설정 모달을 열면 실시간으로 7개 ComfyUI 노드가 실행되는 그래프 뷰어(Node Execution Monitor) 확인 가능
3. 동시 1개 생성 강제 & TTS 음성 안내:
   - 이미 이미지 생성 중일 때 생성 명령이 들어오면 중복 생성을 차단하고 "이미지 생성 중입니다." TTS 음성 안내
   - "~ 만들어줘" 또는 "저녁", "야경", "노을", "아침", "최애" 등 명령 접수 시 끝에 "네, 알겠습니다." TTS 음성 안내
4. 이름 지정 배경 이미지 추가('최애.jpg' 등) & AI 이미지 연관 파일명('겨울_풍경.png' 등) 영구 프리셋 등록
"""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from starlette.datastructures import UploadFile
from starlette.responses import Response
from fasthtml.common import *

from src.modules.ambient_sound_manager import AmbientSoundManager
from src.modules.audio_controller import AudioController
from src.modules.circadian_lighting import CircadianLightingManager
from src.modules.scene_ingredients_engine import SceneIngredientsEngine
from src.modules.virtual_coelux import VirtualCoeLuxSystem
from src.modules.virtual_landscape import (
    DEFAULT_SCENE_PRESETS,
    IntentInferenceEngine,
    LandscapeState,
    VirtualLandscapeManager,
)
from src.modules.web_image_search import WebImageSearchManager

BASE_DIR = Path(__file__).resolve().parent
CSS_PATH = BASE_DIR / "assets" / "css" / "style.css"


# -------------------------------------------------------------------------
# 1. 스마트 창문 실시간 정보(시간 · 날짜 · 기온 · 습도 · 24시간 트렌드) & 실내 공기질(IAQ) 매니저
# -------------------------------------------------------------------------
@dataclass
class WeatherSnapshot:
    temp_c: int = 18
    feels_like_c: int = 17
    humidity_pct: int = 48
    weather_code: int = 0
    condition_kor: str = "맑음"
    condition_eng: str = "Clear Sky"
    icon_type: str = "sun"  # "sun" | "partly_cloudy" | "cloud" | "rain" | "snow"
    hourly_values: List[int] = field(default_factory=list)
    updated_ts: float = 0.0


@dataclass
class IndoorAirQualityState:
    """실내 공기질(IAQ) 임의 입력 수치 및 AI 위험도 판정·공기청정기 작동 안내 상태"""
    pm25: int = 12              # 초미세먼지 (㎍/㎥)
    pm10: int = 26              # 미세먼지 (㎍/㎥)
    co2_ppm: int = 540          # 이산화탄소 (ppm)
    tvoc_ppb: int = 110         # 총휘발성유기화합물 (ppb)
    grade_label: str = "매우 안전 (좋음)"
    badge_cls: str = "iaq-safe"
    is_dangerous: bool = False
    action_message: str = "✨ 실내 공기질이 매우 깨끗하고 안전합니다. 공기청정기를 저소음 에코 모드로 유지합니다."


class SmartWindowHudManager:
    """
    레퍼런스 스마트 창문 사진과 동일한 좌측 글래스 HUD 오버레이 상태 + 실내 공기질 AI 판단 매니저
    - 초기 상태: 숨김 (self.visible = False, 처음에는 안 떴다가)
    - 표시 호출어: "정보", "날씨", "기온", "날짜", "시간", "시계", "온도", "습도", "공기질", "미세먼지" (+ "보여줘"/"켜줘")
    - 숨김 호출어: 위 호출어 + "꺼", "꺼줘", "숨겨", "닫아" (예: "날씨 꺼", "기온 꺼", "날짜 꺼", "정보 꺼")
    - 하단 실내 공기질: 사용자가 임의값을 넣으면 AI가 위험 여부를 판단하고, 위험 시 공기청정기/환기 작동 멘트를 자동 생성!
    """

    WEEKDAYS_ENG = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
    WEEKDAYS_KOR = ("월", "화", "수", "목", "금", "토", "일")
    MONTHS_ENG = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

    # [전체] "정보" = "전체" → 시간·날씨·공기질 + 캘린더까지 모두 켜기/끄기
    ALL_SUBJECTS = (
        "정보", "전체", "창문정보", "현재정보", "모든정보", "모두", "전부", "정복", "전보", "점보",
    )

    # [상세] 개별 섹션 호출어 → 해당 섹션만 켜기/끄기
    SECTION_SUBJECTS: Dict[str, Tuple[str, ...]] = {
        "time": ("날짜", "시간", "시계", "요일", "몇시"),
        "weather": ("날씨", "기온", "온도", "습도", "그래프"),
        "iaq": ("공기질", "실내공기", "미세먼지", "초미세먼지", "이산화탄소", "공기청정", "공기"),
    }

    SECTION_LABELS: Dict[str, str] = {
        "time": "시간·날짜",
        "weather": "날씨·기온",
        "iaq": "실내 공기질",
    }

    # 하위 호환용 (모든 정보 호출어 목록)
    INFO_SUBJECTS = ALL_SUBJECTS + tuple(w for ws in SECTION_SUBJECTS.values() for w in ws)

    OFF_SUFFIXES = (
        "꺼줘", "꺼", "끄기", "끄자", "숨겨줘", "숨겨", "숨기기", "닫아줘", "닫아",
        "없애줘", "없애", "치워줘", "치워", "가려줘", "가려", "안보이게", "그만",
    )

    ON_SUFFIXES = (
        "보여줘", "보여", "켜줘", "켜", "띄워줘", "띄워", "알려줘", "알려", "표시해줘", "표시", "열어줘", "열어",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.sections: Dict[str, bool] = {"time": False, "weather": False, "iaq": False}
        self.last_changed_sections: List[str] = []
        self._weather: WeatherSnapshot = self._build_time_synced_fallback_weather()
        self._iaq: IndoorAirQualityState = self.evaluate_air_quality(12, 26, 540, 110)
        self._fetching: bool = False
        self.trigger_weather_refresh_async()

    # 하나라도 켜져 있으면 정보 창(HUD)이 표시됨. visible = True/False 는 전체 섹션 켜기/끄기
    @property
    def visible(self) -> bool:
        return any(self.sections.values())

    @visible.setter
    def visible(self, value: bool) -> None:
        for k in self.sections:
            self.sections[k] = bool(value)

    def set_section(self, key: str, value: bool) -> None:
        with self._lock:
            if key in self.sections:
                self.sections[key] = bool(value)
        if value and key == "weather":
            self.trigger_weather_refresh_async()

    def toggle_section(self, key: str) -> bool:
        with self._lock:
            if key not in self.sections:
                return False
            self.sections[key] = not self.sections[key]
            now_on = self.sections[key]
        if now_on and key == "weather":
            self.trigger_weather_refresh_async()
        return now_on

    @staticmethod
    def evaluate_air_quality(
        pm25: int,
        pm10: int,
        co2_ppm: int = 540,
        tvoc_ppb: int = 110,
    ) -> IndoorAirQualityState:
        """
        사용자가 입력한 임의의 실내 공기질 수치(PM2.5, PM10, CO2, TVOC)를 바탕으로
        위험 여부를 자동 판단하고 상황에 꼭 맞는 공기청정기/환기 시스템 작동 멘트를 생성합니다.
        """
        pm25_v = max(0, min(999, int(pm25)))
        pm10_v = max(0, min(999, int(pm10)))
        co2_v = max(300, min(5000, int(co2_ppm)))
        tvoc_v = max(0, min(5000, int(tvoc_ppb)))

        # 위험도 단계 산출 (0: 좋음/안전, 1: 보통/안전, 2: 나쁨/위험, 3: 매우나쁨/고위험)
        dust_level = 0
        if pm25_v >= 76 or pm10_v >= 151:
            dust_level = 3
        elif pm25_v >= 36 or pm10_v >= 81:
            dust_level = 2
        elif pm25_v >= 16 or pm10_v >= 31:
            dust_level = 1

        co2_level = 0
        if co2_v >= 1501:
            co2_level = 3
        elif co2_v >= 1001:
            co2_level = 2
        elif co2_v >= 701:
            co2_level = 1

        tvoc_level = 0
        if tvoc_v >= 1001:
            tvoc_level = 3
        elif tvoc_v >= 501:
            tvoc_level = 2
        elif tvoc_v >= 251:
            tvoc_level = 1

        worst_level = max(dust_level, co2_level, tvoc_level)
        is_dangerous = (worst_level >= 2)

        if worst_level == 3:
            grade_label = "🚨 매우 위험 (매우 나쁨)"
            badge_cls = "iaq-danger"
            if dust_level >= 2 and co2_level >= 2:
                msg = (
                    f"🚨 미세먼지({pm25_v}㎍/㎥)와 이산화탄소({co2_v}ppm)가 모두 위험 수준입니다! "
                    "공기청정기를 터보(최대 풍량)로 작동시키고 스마트 청정 환기를 즉시 가동하겠습니다."
                )
            elif dust_level == 3:
                msg = (
                    f"🚨 실내 초미세먼지({pm25_v}㎍/㎥)가 매우 위험한 수준입니다! "
                    "공기청정기를 최대 출력(터보 청정 모드)으로 즉시 작동시키겠습니다."
                )
            elif co2_level == 3:
                msg = (
                    f"🚨 실내 이산화탄소({co2_v}ppm) 농도가 위험 수준입니다! 졸음·두통 예방을 위해 "
                    "공기청정기 순환 팬과 자동 환기 시스템을 즉시 작동시키겠습니다."
                )
            else:
                msg = (
                    f"🚨 실내 유해가스(TVOC {tvoc_v}ppb) 수치가 매우 위험합니다! "
                    "공기청정기 강력 활성탄 탈취 모드와 환기 팬을 즉시 작동시키겠습니다."
                )
        elif worst_level == 2:
            grade_label = "⚠️ 위험 (나쁨)"
            badge_cls = "iaq-warn"
            if dust_level >= 2 and co2_level >= 2:
                msg = (
                    "⚠️ 미세먼지와 이산화탄소 수치가 위험 기준을 넘었습니다. "
                    "공기청정기와 자동 환기 모드를 함께 작동시키겠습니다."
                )
            elif dust_level == 2:
                msg = (
                    f"💨 실내 미세먼지(PM2.5 {pm25_v}㎍/㎥) 수치가 높습니다. "
                    "공기청정기를 작동시키고 헤파(HEPA) 집중 청정 모드를 가동하겠습니다."
                )
            elif co2_level == 2:
                msg = (
                    f"🌿 실내 이산화탄소({co2_v}ppm) 수치가 상승해 공기가 탁합니다. "
                    "공기청정기 실내 순환 팬과 자동 환기 모드를 작동시키겠습니다."
                )
            else:
                msg = (
                    f"🧪 휘발성 유기화합물({tvoc_v}ppb) 수치가 높습니다. "
                    "공기청정기 탈취 필터 모드를 자동으로 작동시키겠습니다."
                )
        elif worst_level == 1:
            grade_label = "🟢 안전 (보통)"
            badge_cls = "iaq-moderate"
            msg = "🟢 실내 공기질이 안전한 범위입니다. 공기청정기를 오토(자동 감지) 모드로 유지하며 모니터링합니다."
        else:
            grade_label = "✨ 매우 안전 (좋음)"
            badge_cls = "iaq-safe"
            msg = "✨ 현재 실내 공기질이 매우 깨끗하고 안전합니다. 공기청정기를 저소음 에코 모드로 대기합니다."

        return IndoorAirQualityState(
            pm25=pm25_v,
            pm10=pm10_v,
            co2_ppm=co2_v,
            tvoc_ppb=tvoc_v,
            grade_label=grade_label,
            badge_cls=badge_cls,
            is_dangerous=is_dangerous,
            action_message=msg,
        )

    def set_air_quality(
        self,
        pm25: int,
        pm10: Optional[int] = None,
        co2_ppm: Optional[int] = None,
        tvoc_ppb: Optional[int] = None,
    ) -> IndoorAirQualityState:
        with self._lock:
            cur = self._iaq
            final_pm10 = int(pm10) if pm10 is not None else max(int(pm25), int(round(int(pm25) * 1.7)))
            final_co2 = int(co2_ppm) if co2_ppm is not None else cur.co2_ppm
            final_tvoc = int(tvoc_ppb) if tvoc_ppb is not None else cur.tvoc_ppb
            self._iaq = self.evaluate_air_quality(pm25, final_pm10, final_co2, final_tvoc)
            # 공기질 임의값을 넣으면 화면에서 바로 확인할 수 있도록 공기질 섹션을 함께 표시!
            self.sections["iaq"] = True
            return self._iaq

    @staticmethod
    def _wmo_code_to_condition(code: int, hour: int) -> Tuple[str, str, str]:
        is_night = (hour < 6 or hour >= 19)
        if code == 0:
            return ("맑음", "Clear" if is_night else "Sunny", "sun")
        if code in (1, 2):
            return ("구름 조금", "Partly Cloudy", "partly_cloudy")
        if code in (3, 45, 48):
            return ("흐림", "Cloudy", "cloud")
        if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
            return ("비", "Rainy", "rain")
        if code in (71, 73, 75, 77, 85, 86):
            return ("눈", "Snowy", "snow")
        return ("맑음", "Sunny", "sun")

    @classmethod
    def _build_time_synced_fallback_weather(cls) -> WeatherSnapshot:
        """네트워크 오프라인 시에도 현재 날짜·시각(datetime.now)의 실시간 일교차 곡선을 반영합니다."""
        now = datetime.now()
        monthly_base = {
            1: 0, 2: 3, 3: 9, 4: 15, 5: 21, 6: 25,
            7: 28, 8: 29, 9: 24, 10: 17, 11: 11, 12: 3,
        }.get(now.month, 18)

        hourly_vals: List[int] = []
        for h in range(24):
            wave = math.sin(((h - 5) / 24.0) * 2.0 * math.pi - (math.pi / 6.0))
            val = int(round(22 + 16 * ((wave + 1.0) / 2.0) + ((h * 3) % 5)))
            hourly_vals.append(max(8, min(48, val)))

        cur_wave = math.sin(((now.hour - 5) / 24.0) * 2.0 * math.pi - (math.pi / 6.0))
        cur_temp = int(round(monthly_base + 4.5 * cur_wave))
        cur_hum = int(round(52 - 14 * cur_wave))
        cond_kor, cond_eng, icon_t = cls._wmo_code_to_condition(0, now.hour)

        return WeatherSnapshot(
            temp_c=cur_temp,
            feels_like_c=cur_temp - 1,
            humidity_pct=max(20, min(90, cur_hum)),
            weather_code=0,
            condition_kor=cond_kor,
            condition_eng=cond_eng,
            icon_type=icon_t,
            hourly_values=hourly_vals,
            updated_ts=time.time(),
        )

    def trigger_weather_refresh_async(self) -> None:
        with self._lock:
            if self._fetching:
                return
            self._fetching = True

        def _worker() -> None:
            try:
                url = (
                    "https://api.open-meteo.com/v1/forecast?"
                    "latitude=37.5665&longitude=126.9780"
                    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code"
                    "&hourly=temperature_2m,relative_humidity_2m"
                    "&timezone=Asia%2FSeoul&forecast_days=1"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "WINDO-SmartWindow/2.0"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                cur = data.get("current", {})
                temp_c = int(round(float(cur.get("temperature_2m", 18.0))))
                feels_c = int(round(float(cur.get("apparent_temperature", temp_c))))
                hum_pct = int(round(float(cur.get("relative_humidity_2m", 48))))
                w_code = int(cur.get("weather_code", 0))

                now_hr = datetime.now().hour
                cond_kor, cond_eng, icon_t = self._wmo_code_to_condition(w_code, now_hr)

                hourly_temps = data.get("hourly", {}).get("temperature_2m", [])
                hourly_hums = data.get("hourly", {}).get("relative_humidity_2m", [])
                bars: List[int] = []
                if hourly_temps and len(hourly_temps) >= 24:
                    t_min = min(hourly_temps[:24])
                    t_max = max(hourly_temps[:24])
                    span = max(4.0, float(t_max - t_min))
                    for i in range(24):
                        norm = (float(hourly_temps[i]) - float(t_min)) / span
                        hum_factor = (float(hourly_hums[i]) / 100.0) * 10.0 if i < len(hourly_hums) else 5.0
                        bar_h = int(round(12 + norm * 28 + hum_factor))
                        bars.append(max(8, min(48, bar_h)))
                else:
                    bars = self._build_time_synced_fallback_weather().hourly_values

                with self._lock:
                    self._weather = WeatherSnapshot(
                        temp_c=temp_c,
                        feels_like_c=feels_c,
                        humidity_pct=hum_pct,
                        weather_code=w_code,
                        condition_kor=cond_kor,
                        condition_eng=cond_eng,
                        icon_type=icon_t,
                        hourly_values=bars,
                        updated_ts=time.time(),
                    )
            except Exception:
                with self._lock:
                    self._weather = self._build_time_synced_fallback_weather()
            finally:
                with self._lock:
                    self._fetching = False

        threading.Thread(target=_worker, name="SmartWindowWeatherThread", daemon=True).start()

    def apply_voice_command(self, text: str) -> Optional[str]:
        """
        1) '정보', '날씨', '기온', '날짜', '시간', '시계', '온도', '습도', '공기질', '미세먼지' + '꺼'/'꺼줘' -> 'hidden'
        2) '미세먼지 85', '공기질 90', '이산화탄소 1500' 등 임의 수치 입력 -> 'iaq_updated'
        3) '정보', '날씨', '기온', '날짜', '시간', '시계', '공기질' 단독 또는 + '보여줘'/'켜줘' -> 'shown'
        """
        raw = text.strip().lower()
        for ww in ("윈도우", "윈도", "window", "windo"):
            if raw.startswith(ww):
                raw = raw[len(ww):].strip()

        # 괄호 안의 원본 텍스트도 함께 검사 (예: "정보 꺼줘 (날씨 꺼)" 또는 "정보 (날씨)")
        candidates = [raw]
        if "(" in raw and ")" in raw:
            main_part = raw.split("(", 1)[0].strip()
            inner_part = raw.split("(", 1)[1].split(")", 1)[0].strip()
            candidates = [inner_part, main_part, raw]

        # AI 이미지 생성 동사("만들어줘", "그려줘")가 있으면 풍경 생성 명령이므로 제외
        if any(v in raw for v in ("만들어", "그려", "생성")):
            return None

        # A. 음성/텍스트로 실내 공기질 임의 수치 입력 ("미세먼지 85", "초미세먼지 90", "공기질 80", "이산화탄소 1500")
        nums = [int(n) for n in re.findall(r"\d+", raw)]
        if nums and any(k in raw for k in ("미세먼지", "초미세먼지", "공기질", "이산화탄소", "co2", "tvoc")):
            val = nums[0]
            if "이산화탄소" in raw or "co2" in raw:
                self.set_air_quality(pm25=self._iaq.pm25, pm10=self._iaq.pm10, co2_ppm=val)
            elif "tvoc" in raw or "유해가스" in raw:
                self.set_air_quality(pm25=self._iaq.pm25, pm10=self._iaq.pm10, tvoc_ppb=val)
            else:
                pm10_val = nums[1] if len(nums) >= 2 else max(val, int(round(val * 1.65)))
                self.set_air_quality(pm25=val, pm10=pm10_val)
            return "iaq_updated"

        for cand in candidates:
            comp = cand.replace(" ", "")
            if not comp:
                continue

            matched_sections = [
                key for key, words in self.SECTION_SUBJECTS.items()
                if any(w in comp for w in words)
            ]
            has_all = any(s in comp for s in self.ALL_SUBJECTS)
            if not matched_sections and not has_all:
                continue

            has_off = any(off in comp for off in self.OFF_SUFFIXES)
            has_on = any(on in comp for on in self.ON_SUFFIXES)
            is_bare = (not has_off and not has_on and len(comp) <= 6)

            # B. [상세] 날씨/기온/날짜/시간/공기질 → 해당 섹션만 켜기/끄기 ("날씨 정보 보여줘"처럼 섞여도 상세 우선)
            if matched_sections:
                if has_off:
                    with self._lock:
                        for key in matched_sections:
                            self.sections[key] = False
                    self.last_changed_sections = matched_sections
                    return "hidden"
                if has_on or is_bare:
                    with self._lock:
                        for key in matched_sections:
                            self.sections[key] = True
                    self.last_changed_sections = matched_sections
                    if "weather" in matched_sections:
                        self.trigger_weather_refresh_async()
                    return "shown"
                continue

            # C. [전체] "정보" / "전체" → 시간·날씨·공기질 + 캘린더까지 모두 켜기/끄기
            if has_off:
                with self._lock:
                    self.visible = False
                self.last_changed_sections = list(self.sections.keys())
                return "all_hidden"
            if has_on or is_bare:
                with self._lock:
                    self.visible = True
                self.last_changed_sections = list(self.sections.keys())
                self.trigger_weather_refresh_async()
                return "all_shown"

        return None

    def describe_sections(self, keys: List[str]) -> str:
        return " · ".join(self.SECTION_LABELS.get(k, k) for k in keys)

    def toggle(self) -> bool:
        """설정 화면 '전체' 토글: 하나라도 켜져 있으면 전체 끄기, 모두 꺼져 있으면 전체 켜기"""
        with self._lock:
            self.visible = not self.visible
            now_vis = self.visible
        if now_vis:
            self.trigger_weather_refresh_async()
        return now_vis

    def get_live_data(self) -> Tuple[bool, str, str, str, int, WeatherSnapshot, IndoorAirQualityState]:
        now = datetime.now()
        if time.time() - self._weather.updated_ts > 600:
            self.trigger_weather_refresh_async()

        time_str = now.strftime("%H:%M")
        w_eng = self.WEEKDAYS_ENG[now.weekday()]
        m_eng = self.MONTHS_ENG[now.month - 1]
        date_eng = f"{w_eng} {now.day:02d} {m_eng}"
        w_kor = self.WEEKDAYS_KOR[now.weekday()]
        date_kor = f"{now.year}. {now.month:02d}. {now.day:02d} ({w_kor})"

        with self._lock:
            return self.visible, time_str, date_eng, date_kor, now.hour, self._weather, self._iaq


# -------------------------------------------------------------------------
# 2. 스마트 창문 구글 캘린더(Google Calendar) 위젯 매니저 (정보 창 오른쪽 표시)
# -------------------------------------------------------------------------
@dataclass
class CalendarEvent:
    id: str
    date_str: str      # "YYYY-MM-DD"
    time_str: str      # "14:00" 또는 "14:00 - 15:30"
    title: str
    color: str = "blue"  # "blue" | "green" | "amber" | "rose"


class SmartWindowCalendarManager:
    """
    정보 창 오른쪽에 표시되는 구글 캘린더(Google Calendar) 기능의 스마트 글래스 위젯 매니저
    - 초기 상태: 숨김 (self.visible = False)
    - 음성 명령: "캘린더", "캘린더 보여줘", "달력", "일정" 말하면 켜지고, "캘린더 꺼", "달력 꺼", "일정 꺼" 말하면 꺼짐!
    - 주요 기능: 월간 달력 그리드, 이전/다음 달 및 오늘 이동, 날짜 선택, 일정 추가/삭제 및 영구 저장(.cache/calendar_events.json)
    """

    CAL_SUBJECTS = ("캘린더", "켈린더", "칼렌더", "카렌다", "구글캘린더", "달력", "일정", "스케줄")
    OFF_SUFFIXES = ("꺼줘", "꺼", "끄기", "숨겨줘", "숨겨", "닫아줘", "닫아", "없애줘", "없애", "치워", "가려")
    ON_SUFFIXES = ("보여줘", "보여", "켜줘", "켜", "띄워줘", "띄워", "열어줘", "열어", "알려줘", "알려", "표시")

    def __init__(self, base_dir: Path) -> None:
        self._lock = threading.RLock()
        self.visible: bool = False
        self.storage_file = base_dir / ".cache" / "calendar_events.json"
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        self.view_year: int = now.year
        self.view_month: int = now.month
        self.selected_date: str = now.strftime("%Y-%m-%d")
        self.events: List[CalendarEvent] = []
        self._load_or_seed_events()

    def _load_or_seed_events(self) -> None:
        today_dt = datetime.now()
        today_s = today_dt.strftime("%Y-%m-%d")
        ym_prefix = today_dt.strftime("%Y-%m")

        if self.storage_file.exists():
            try:
                raw_list = json.loads(self.storage_file.read_text(encoding="utf-8"))
                self.events = [CalendarEvent(**item) for item in raw_list]
                if self.events:
                    return
            except Exception:
                pass

        # 기본 샘플 일정 시드
        self.events = [
            CalendarEvent(
                id="ev-1",
                date_str=today_s,
                time_str="10:00 - 11:30",
                title="해커톤 프로젝트 최종 점검",
                color="blue",
            ),
            CalendarEvent(
                id="ev-2",
                date_str=today_s,
                time_str="14:00 - 15:30",
                title="AI 스마트 창문 WINDO 시연",
                color="green",
            ),
            CalendarEvent(
                id="ev-3",
                date_str=today_s,
                time_str="19:30 - 21:00",
                title="팀 디너 & 네트워킹",
                color="amber",
            ),
            CalendarEvent(
                id="ev-4",
                date_str=f"{ym_prefix}-15",
                time_str="15:00",
                title="디자인 리뷰 미팅",
                color="rose",
            ),
            CalendarEvent(
                id="ev-5",
                date_str=f"{ym_prefix}-20",
                time_str="11:00",
                title="스마트홈 IoT 연동 테스트",
                color="blue",
            ),
        ]
        self._save_events()

    def _save_events(self) -> None:
        try:
            data = [
                {
                    "id": e.id,
                    "date_str": e.date_str,
                    "time_str": e.time_str,
                    "title": e.title,
                    "color": e.color,
                }
                for e in self.events
            ]
            self.storage_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def apply_voice_command(self, text: str) -> Optional[str]:
        """
        '캘린더', '달력', '일정' -> 'shown'
        '캘린더 꺼', '달력 꺼', '일정 꺼' -> 'hidden'
        """
        raw = text.strip().lower()
        for ww in ("윈도우", "윈도", "window", "windo"):
            if raw.startswith(ww):
                raw = raw[len(ww):].strip()

        candidates = [raw]
        if "(" in raw and ")" in raw:
            main_part = raw.split("(", 1)[0].strip()
            inner_part = raw.split("(", 1)[1].split(")", 1)[0].strip()
            candidates = [inner_part, main_part, raw]

        for cand in candidates:
            comp = cand.replace(" ", "")
            if not comp:
                continue

            has_cal = any(s in comp for s in self.CAL_SUBJECTS)
            if not has_cal:
                continue

            if any(off in comp for off in self.OFF_SUFFIXES):
                with self._lock:
                    self.visible = False
                return "hidden"

            if comp in self.CAL_SUBJECTS or any(on in comp for on in self.ON_SUFFIXES) or comp in ("오늘일정", "이번달달력"):
                with self._lock:
                    now = datetime.now()
                    self.view_year = now.year
                    self.view_month = now.month
                    self.selected_date = now.strftime("%Y-%m-%d")
                    self.visible = True
                return "shown"

        return None

    def toggle(self) -> bool:
        with self._lock:
            self.visible = not self.visible
            return self.visible

    def set_visible(self, value: bool) -> None:
        with self._lock:
            if value and not self.visible:
                now = datetime.now()
                self.view_year = now.year
                self.view_month = now.month
                self.selected_date = now.strftime("%Y-%m-%d")
            self.visible = bool(value)

    def get_month_event_colors(self, year: int, month: int) -> Dict[int, List[str]]:
        """{일(day): [색상, ...]} — 달력 셀의 일정 점 표시용"""
        prefix = f"{year:04d}-{month:02d}-"
        result: Dict[int, List[str]] = {}
        with self._lock:
            for e in self.events:
                if e.date_str.startswith(prefix):
                    try:
                        day = int(e.date_str[len(prefix):])
                    except ValueError:
                        continue
                    result.setdefault(day, []).append(e.color)
        return result

    def change_month(self, delta: int) -> None:
        with self._lock:
            if delta == 0:
                now = datetime.now()
                self.view_year = now.year
                self.view_month = now.month
                self.selected_date = now.strftime("%Y-%m-%d")
                return
            m = self.view_month + delta
            y = self.view_year
            if m < 1:
                m = 12
                y -= 1
            elif m > 12:
                m = 1
                y += 1
            self.view_year = y
            self.view_month = m
            self.selected_date = f"{y:04d}-{m:02d}-01"

    def select_date(self, date_str: str) -> None:
        with self._lock:
            self.selected_date = date_str.strip()
            try:
                parts = self.selected_date.split("-")
                self.view_year = int(parts[0])
                self.view_month = int(parts[1])
            except Exception:
                pass

    def add_event(self, date_str: str, time_str: str, title: str, color: str = "blue") -> Optional[CalendarEvent]:
        clean_title = title.strip()
        if not clean_title:
            return None
        with self._lock:
            target_date = date_str.strip() or self.selected_date
            target_time = time_str.strip() or "종일"
            ev = CalendarEvent(
                id=f"ev-{int(time.time() * 1000)}",
                date_str=target_date,
                time_str=target_time,
                title=clean_title,
                color=color if color in ("blue", "green", "amber", "rose") else "blue",
            )
            self.events.append(ev)
            self.events.sort(key=lambda x: (x.date_str, x.time_str))
            self.selected_date = target_date
            self.visible = True
            self._save_events()
            return ev

    def delete_event(self, event_id: str) -> None:
        with self._lock:
            self.events = [e for e in self.events if e.id != event_id]
            self._save_events()

    def get_events_for_date(self, date_str: str) -> List[CalendarEvent]:
        with self._lock:
            return [e for e in self.events if e.date_str == date_str]


lighting_mgr = CircadianLightingManager()
landscape_mgr = VirtualLandscapeManager(base_dir=BASE_DIR)
hud_mgr = SmartWindowHudManager()
calendar_mgr = SmartWindowCalendarManager(base_dir=BASE_DIR)
web_search_mgr = WebImageSearchManager(base_dir=BASE_DIR)
coelux_mgr = VirtualCoeLuxSystem()
sound_mgr = AmbientSoundManager(mp3_dir=BASE_DIR / "mp3")
ingredients_mgr = SceneIngredientsEngine(base_dir=BASE_DIR, mp3_dir=BASE_DIR / "mp3")
_LAST_RENDERED_SCENE_VERSION: int = landscape_mgr.get_state().version
_LAST_RENDERED_TL_SWITCH_VERSION: int = 0


def reset_all_to_initial_defaults(clear_timelapse_files: bool = True) -> None:
    """
    창문 앱에 처음 들어가거나 다시 켰을 때 항상 기본 사진(default.png)과 초기 기본 설정으로 시작하도록 보장합니다:
    1) 배경 사진: default.png ('기본 대기 화면')
    2) 코에룩스 조명: OFF (안 켜진 상태, 밝기 100%, AI 자동 색상·위치)
    3) 타임랩스: 기존에 돌렸던 타임랩스 완전 제거 및 대기 상태 초기화
    4) 자연광/채도: 기본 생체리듬 자동 광원 상태
    5) 사운드: 재생 정지 및 자동 매칭 OFF
    6) 위젯/캘린더: 초기 숨김(OFF) 상태
    """
    global _LAST_RENDERED_SCENE_VERSION, _LAST_RENDERED_TL_SWITCH_VERSION, IMAGE_FIT_MODE
    IMAGE_FIT_MODE = "fill"
    landscape_mgr.reset_to_default()
    lighting_mgr.enable_auto_mode()
    coelux_mgr.reset_to_default()
    ingredients_mgr.reset_to_default(clear_disk_cache=clear_timelapse_files)
    sound_mgr.stop()
    sound_mgr.auto_match_bg = False
    hud_mgr.visible = False
    calendar_mgr.set_visible(False)
    _LAST_RENDERED_SCENE_VERSION = landscape_mgr.get_state().version
    _LAST_RENDERED_TL_SWITCH_VERSION = ingredients_mgr.switch_version


# 서버 기동 시 초기 기본 상태(default.png, 코에룩스 OFF, 타임랩스 제거) 강제 보장
reset_all_to_initial_defaults(clear_timelapse_files=True)


def get_active_background_path() -> Optional[Path]:
    """현재 창문에 표시 중인 배경 이미지(기본/웹 프리셋 또는 AI 생성 캐시)의 실제 로컬 파일 경로를 반환합니다."""
    s_state = landscape_mgr.get_state()
    raw_url = urllib.parse.unquote(s_state.current_image_url.split("?", 1)[0]).strip()
    for prefix, folder in (
        ("/assets/images/presets/", landscape_mgr.presets_dir),
        ("/presets/", landscape_mgr.presets_dir),
        ("/cache/landscapes/", landscape_mgr.cache_dir),
        ("/generated/", landscape_mgr.cache_dir),
    ):
        if raw_url.startswith(prefix):
            fname = raw_url[len(prefix):]
            cand = folder / fname
            if cand.exists() and cand.is_file():
                return cand

    preset = landscape_mgr.presets.get(landscape_mgr.active_key, landscape_mgr.presets.get("default"))
    if not preset:
        return None
    target_name = preset.default_filename.lower()
    target_stem = Path(preset.default_filename).stem.lower()
    for folder in (landscape_mgr.presets_dir, landscape_mgr.cache_dir):
        if folder.exists():
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in landscape_mgr.SUPPORTED_EXTS:
                    if p.name.lower() == target_name or p.stem.lower() == target_stem:
                        return p
    return None


def trigger_auto_bg_sound_if_enabled() -> None:
    """'배경 변경 시 자동 맞춤 소리(Auto-Sync)'가 켜져 있을 때 새 배경에 맞는 MP3를 자동으로 분석해 재생합니다."""
    if not sound_mgr.auto_match_bg:
        return
    s_st = landscape_mgr.get_state()
    tr = sound_mgr.match_and_play_for_background(
        get_active_background_path(),
        scene_hint=f"{s_st.active_title} {s_st.active_key}",
        on_gemini_updated=lambda g_tr, g_rsn: audio_ctrl.add_log(
            "SYSTEM",
            f"🤖 Gemini Vision 배경 맞춤 사운드 자동 전환: [{g_tr.icon} {g_tr.title}] ({g_tr.filename})",
            g_rsn,
        ),
    )
    if tr:
        st_info = sound_mgr.get_state()
        audio_ctrl.add_log(
            "SYSTEM",
            f"🔊 배경 전환에 따른 맞춤 소리 자동 재생: [{tr.icon} {tr.title}] ({tr.filename})",
            st_info["reason"],
        )


def sync_custom_scene_keywords() -> None:
    """
    assets/images/presets/ 및 .cache/landscapes/에 저장된 모든 커스텀 이미지(예: 최애.jpg)와
    AI 생성 이미지(예: 겨울_풍경.png)를 스캔하여 AudioController의 핵심 풍경어로 실시간 동기화합니다.
    """
    landscape_mgr.scan_and_register_preset_files()
    for key in landscape_mgr.presets:
        if key not in DEFAULT_SCENE_PRESETS:
            AudioController.register_custom_scene_keyword(key)


def handle_voice_command(command_text: str) -> None:
    """
    AudioController에서 변환된 전체 문장 명령을 받아:
    1) 채도/밝기/색온도 조회 및 직접 조절 처리
    2) 기본 프리셋('아침', '노을', '저녁', '야경', '기본') 및 커스텀/AI 프리셋('최애', '겨울 풍경' 등)이면
       즉시 화면 전환 후 끝에 "네, 알겠습니다." TTS 출력!
    3) 특수 배경 생성 명령('~ 만들어줘')일 때:
       - 이미 이미지 생성 중이면 중복 실행을 막고 "이미지 생성 중입니다." TTS 출력!
       - 대기 중이면 ComfyUI 7-노드 워크플로우 생성을 시작하고 끝에 "네, 알겠습니다." TTS 출력!
    """
    text = command_text.strip()
    if not text:
        return

    sync_custom_scene_keywords()

    # [핵심 신규 기능 1] "정보"/"전체" = 시간·날씨·공기질 + 캘린더 전체 켜기/끄기,
    #                  "날씨"/"기온"/"날짜"/"시간"/"공기질" = 상세 섹션만 켜기/끄기 + 음성 공기질 임의값 입력
    hud_action = hud_mgr.apply_voice_command(text)
    if hud_action == "all_shown":
        calendar_mgr.set_visible(True)
        _, t_str, _, d_kor, _, w_snap, iaq_state = hud_mgr.get_live_data()
        audio_ctrl.add_log(
            "STT",
            f"🪟 전체 정보 표시 ON (시간·날씨·공기질·캘린더) — {t_str} | {d_kor} | {w_snap.temp_c}°C · {iaq_state.grade_label} — 네, 알겠습니다.",
            "'날씨 꺼', '공기질 꺼', '캘린더 꺼'처럼 개별로 끄거나 '전체 꺼' / '정보 꺼'로 모두 끌 수 있습니다.",
        )
        audio_ctrl.speak_ack()
        return
    elif hud_action == "all_hidden":
        calendar_mgr.set_visible(False)
        audio_ctrl.add_log(
            "STT",
            f"🪟 전체 정보 표시 OFF (\"{text}\") — 순수 창문 풍경 모드 — 네, 알겠습니다.",
            "'전체 켜' 또는 '정보 켜'라고 말하면 모든 정보가 다시 표시됩니다.",
        )
        audio_ctrl.speak_ack()
        return
    elif hud_action == "shown":
        _, t_str, _, d_kor, _, w_snap, iaq_state = hud_mgr.get_live_data()
        labels = hud_mgr.describe_sections(hud_mgr.last_changed_sections)
        audio_ctrl.add_log(
            "STT",
            f"🕒 [{labels}] 표시 ON — 네, 알겠습니다.",
            f"{t_str} | {d_kor} | {w_snap.temp_c}°C ({w_snap.condition_kor}) · {iaq_state.grade_label}",
        )
        audio_ctrl.speak_ack()
        return
    elif hud_action == "hidden":
        labels = hud_mgr.describe_sections(hud_mgr.last_changed_sections)
        audio_ctrl.add_log(
            "STT",
            f"🪟 [{labels}] 표시 OFF (\"{text}\") — 네, 알겠습니다.",
            "다른 항목은 그대로 유지됩니다. 모두 끄려면 '전체 꺼' 또는 '정보 꺼'라고 말씀하세요.",
        )
        audio_ctrl.speak_ack()
        return
    elif hud_action == "iaq_updated":
        _, _, _, _, _, _, iaq_state = hud_mgr.get_live_data()
        audio_ctrl.add_log(
            "STT",
            f"💨 실내 공기질 수치 반영 및 AI 위험도 판단 완료: [{iaq_state.grade_label}] (PM2.5 {iaq_state.pm25} · PM10 {iaq_state.pm10} · CO2 {iaq_state.co2_ppm}ppm) — 네, 알겠습니다.",
            f"🤖 AI 조치: {iaq_state.action_message}",
        )
        audio_ctrl.speak_ack()
        return

    # [핵심 신규 기능 2] "캘린더" / "캘린더 꺼" 음성 명령 처리 (정보 창 오른쪽에 구글 캘린더 표시/숨김)
    cal_action = calendar_mgr.apply_voice_command(text)
    if cal_action == "shown":
        audio_ctrl.add_log(
            "STT",
            f"📅 스마트 창문 Google Calendar 표시 ON ({calendar_mgr.view_year}년 {calendar_mgr.view_month}월) — 네, 알겠습니다.",
            "좌측 정보 창 오른쪽에 구글 캘린더 일정 관리 창이 활성화되었습니다. ('캘린더 꺼'라고 말하면 닫힙니다)",
        )
        audio_ctrl.speak_ack()
        return
    elif cal_action == "hidden":
        audio_ctrl.add_log(
            "STT",
            f"📅 스마트 창문 Google Calendar 표시 OFF (\"{text}\") — 네, 알겠습니다.",
            "언제든 '캘린더'라고 말씀하시면 다시 표시됩니다.",
        )
        audio_ctrl.speak_ack()
        return

    # [핵심 신규 기능 3] "조명 켜줘" / "조명 꺼줘" / "조명 밝기 150" / "조명 노란색" 등 음성 및 텍스트 명령 처리
    coelux_action = coelux_mgr.apply_voice_command(text)
    if coelux_action in ("shown", "adjusted"):
        s_st = landscape_mgr.get_state()
        coelux_mgr.analyze_background_file(
            get_active_background_path(),
            scene_hint=f"{s_st.active_title} {s_st.active_key}",
        )
        p_info = coelux_mgr.get_placement_state()
        audio_ctrl.add_log(
            "STT",
            f"☀️ 가상 코에룩스 실내 창문 채광 {'조절 완료' if coelux_action == 'adjusted' else 'ON'} (\"{text}\") — 네, 알겠습니다.",
            f"광원: {p_info['label_ko']} | 위치 (X:{p_info['x_pct']:.0f}%, Y:{p_info['y_pct']:.0f}%) | 밝기 {p_info['intensity_pct']}% · 색상 {p_info['active_color_hex']}",
        )
        audio_ctrl.speak_ack()
        return
    elif coelux_action == "hidden":
        audio_ctrl.add_log(
            "STT",
            f"🌑 가상 코에룩스(CoeLux) 입체 채광 OFF (\"{text}\") — 네, 알겠습니다.",
            "언제든 '조명 켜줘' 또는 '코에룩스 켜줘'라고 말씀하시면 입체 광원이 다시 켜집니다.",
        )
        audio_ctrl.speak_ack()
        return

    # [핵심 신규 기능 4] "~ 들려줘" (비 소리 들려줘, 도시 소리 들려줘 등) & "배경에 맞는 소리 들려줘" & "소리 꺼줘" 음성/텍스트 명령 처리
    sound_action = sound_mgr.apply_voice_command(text)
    if sound_action == "played":
        st_info = sound_mgr.get_state()
        audio_ctrl.add_log(
            "STT",
            f"🔊 사운드 재생 시작: [{st_info['icon']} {st_info['title']}] ({st_info['filename']}) — 네, 알겠습니다.",
            f"볼륨 {st_info['volume_pct']}% · {st_info['reason']}",
        )
        audio_ctrl.speak_ack()
        return
    elif sound_action == "match_bg":
        s_st = landscape_mgr.get_state()
        matched_tr = sound_mgr.match_and_play_for_background(
            get_active_background_path(),
            scene_hint=f"{s_st.active_title} {s_st.active_key}",
            on_gemini_updated=lambda g_tr, g_rsn: audio_ctrl.add_log(
                "SYSTEM",
                f"🤖 Gemini Vision 배경 맞춤 사운드 확정: [{g_tr.icon} {g_tr.title}] ({g_tr.filename})",
                g_rsn,
            ),
        )
        if matched_tr:
            st_info = sound_mgr.get_state()
            audio_ctrl.add_log(
                "STT",
                f"🤖 배경 맞춤 소리 재생: [{matched_tr.icon} {matched_tr.title}] ({matched_tr.filename}) — 네, 알겠습니다.",
                f"현재 배경({s_st.active_title}) 분석 완료 · {st_info['reason']}",
            )
            audio_ctrl.speak_ack()
        return
    elif sound_action == "stopped":
        audio_ctrl.add_log(
            "STT",
            f"🔇 사운드 재생 정지 (\"{text}\") — 네, 알겠습니다.",
            "언제든 '비 소리 들려줘', '도시 소리 들려줘', '배경에 맞는 소리 들려줘'라고 말씀하시면 다시 재생됩니다.",
        )
        audio_ctrl.speak_ack()
        return
    elif sound_action == "volume":
        st_info = sound_mgr.get_state()
        audio_ctrl.add_log(
            "STT",
            f"🔉 사운드 볼륨 조절 완료: {st_info['volume_pct']}% — 네, 알겠습니다.",
            f"현재 트랙: {st_info['title']}",
        )
        audio_ctrl.speak_ack()
        return

    # [핵심 신규 기능 5] "타임랩스 만들어줘" / "타입랩스 만들어줘" / "타임랩스 켜줘" / "타임랩스 꺼줘" 음성·텍스트 명령 처리
    # 오직 현재 창문에 띄워져 있는 사진을 기준으로 시간대(아침/노을/저녁)를 분석해 다른 시간대 이미지 2개를 포함한 총 3개의 이미지를 생성한 뒤 순환 전환합니다.
    tl_comp = text.replace(" ", "").lower()
    if any(k in tl_comp for k in ("타임랩스", "타입랩스", "타임렙스", "타입렙스", "timelapse", "시간대별변환")):
        if any(off_w in tl_comp for off_w in ("꺼줘", "꺼", "끄기", "종료", "닫아", "중지", "오프", "off", "멈춰")):
            ingredients_mgr.set_active(False)
            audio_ctrl.add_log(
                "STT",
                f"🎬 타임랩스 종료 (\"{text}\") — 네, 알겠습니다.",
                "언제든 '타임랩스 만들어줘'라고 말씀하시거나 설정창의 [타임랩스 만들기] 버튼을 누르면 현재 창문 사진을 기준으로 다시 생성됩니다.",
            )
            audio_ctrl.speak_ack()
            return
        else:
            if ingredients_mgr.is_generating:
                audio_ctrl.add_log(
                    "WARN",
                    f"⏳ 이미 타임랩스 이미지 3개를 생성 중입니다 ({ingredients_mgr.generation_progress_pct}%) — \"이미지 생성 중입니다.\"",
                    "이미지 3개가 모두 완성되면 타임랩스가 자동으로 실행됩니다.",
                )
                audio_ctrl.speak_generating()
                return
            s_st = landscape_mgr.get_state()
            bg_p = get_active_background_path()
            started = ingredients_mgr.start_timelapse_generation_async(
                image_path=bg_p,
                title=s_st.active_title or "현재 창문 풍경",
                active_preset_key=s_st.active_key,
                try_gemini_api=True,
                log_callback=audio_ctrl.add_log,
            )
            if not started:
                audio_ctrl.speak_generating()
                return
            audio_ctrl.add_log(
                "STT",
                f"🎬 현재 창문 사진 [{s_st.active_title}] 기준 타임랩스 이미지 3개 생성 시작 (\"{text}\") — 네, 알겠습니다.",
                "Step 1/3(원본 시간대 분석) ➔ Step 2/3(다음 시간대 생성) ➔ Step 3/3(다다음 시간대 생성) 완료 후 자동 실행됩니다.",
            )
            audio_ctrl.speak_ack()
            return

    matched_light_key = lighting_mgr.apply_voice_intent(text)
    if matched_light_key == "query":
        l_state = lighting_mgr.get_current_state()
        audio_ctrl.add_log(
            "STT",
            f"📊 현재 창문 광원 상태 — 채도: {l_state.saturation_pct}% | 밝기: {l_state.brightness_pct}% | 색온도: {l_state.cct_kelvin}K ({l_state.rgb_hex})",
            f"모드: {l_state.active_label}",
        )
        return
    elif matched_light_key == "custom":
        l_state = lighting_mgr.get_current_state()
        audio_ctrl.add_log(
            "STT",
            f"🎛️ 사용자 직접 조절 적용 완료 — 채도 {l_state.saturation_pct}% · 밝기 {l_state.brightness_pct}% · 색온도 {l_state.cct_kelvin}K — 네, 알겠습니다.",
            f"CSS Filter: {l_state.css_filter}",
        )
        if not any(w in text for w in ("풍경", "배경", "만들어", "그려", "생성", "아침", "노을", "저녁", "야경", "기본")):
            audio_ctrl.speak_ack()
            return
    elif matched_light_key == "auto":
        l_state = lighting_mgr.get_current_state()
        audio_ctrl.add_log(
            "STT",
            f"⏱️ 생체리듬 자동 광원 ON ({l_state.cct_kelvin}K · 채도 {l_state.saturation_pct}%) — 네, 알겠습니다.",
            lighting_mgr.FEATURE_SUMMARY,
        )
        audio_ctrl.speak_ack()
        return
    elif matched_light_key == "auto_off":
        audio_ctrl.add_log(
            "STT",
            "⏱️ 생체리듬 자동 광원 OFF (원본 풍경 무보정 100% 모드) — 네, 알겠습니다.",
            "시간대별 색온도 자동 필터가 해제되었습니다. '생체리듬 켜줘'라고 말씀하시면 다시 켜집니다.",
        )
        audio_ctrl.speak_ack()
        return

    # 사용자가 단순히 "만들어줘", "생성된 풍경 적용해줘"라고만 한 경우
    compact_text = text.replace(" ", "")
    if compact_text in ("만들어줘", "만들어", "생성해줘", "적용해줘", "방금만든배경", "방금만든풍경", "ai풍경", "ai배경"):
        if landscape_mgr.is_generating:
            audio_ctrl.speak_generating()
            return
        applied_url = landscape_mgr.apply_latest_cached_landscape(label="최근 생성된 AI 풍경")
        if applied_url:
            ingredients_mgr.set_active(False)
            trigger_auto_bg_sound_if_enabled()
            l_state = lighting_mgr.get_current_state()
            audio_ctrl.add_log(
                "STT",
                f"🪟 생성되어 있던 AI 풍경 이미지를 창문에 즉시 적용했습니다! ({landscape_mgr.active_title}) — 네, 알겠습니다.",
                f"채도 {l_state.saturation_pct}% · 색온도 {l_state.cct_kelvin}K",
            )
            audio_ctrl.speak_ack()
            return

    route_type, target_key, (location, weather, time_of_day) = IntentInferenceEngine.infer_intent(
        text, landscape_mgr.presets
    )

    if route_type == "preset" and target_key:
        preset = landscape_mgr.switch_to_preset(target_key)
        if preset:
            ingredients_mgr.set_active(False)
            if matched_light_key != "custom":
                lighting_mgr.set_preset_override(preset.lighting_key)
            trigger_auto_bg_sound_if_enabled()
            l_state = lighting_mgr.get_current_state()
            audio_ctrl.add_log(
                "STT",
                f"🪟 창문 풍경 즉시 전환 완료: [{preset.title}] — 네, 알겠습니다.",
                f"색온도 {l_state.cct_kelvin}K · 채도 {l_state.saturation_pct}% · 밝기 {l_state.brightness_pct}% 적용",
            )
            audio_ctrl.speak_ack()
    elif route_type == "generate":
        # [핵심 요구사항] 이미지 생성은 한 번에 1개씩! 이미 생성 중이면 "이미지 생성 중입니다" TTS 출력
        if landscape_mgr.is_generating:
            audio_ctrl.add_log(
                "WARN",
                f"⏳ 이미 이미지 생성 중입니다 (\"{text}\" 요청 대기 차단) — \"이미지 생성 중입니다.\"",
                "한 번에 1개의 이미지만 생성할 수 있습니다.",
            )
            audio_ctrl.speak_generating()
            return

        # [핵심 요구사항] 생성 중에는 기존 화면과 기존 광원을 100% 그대로 유지하고,
        # 생성이 100% 완료된 직후에만 새 풍경과 맞춤 자연광으로 동시 전환합니다!
        def _on_generation_complete(registered_stem: str, final_lighting_key: str) -> None:
            AudioController.register_custom_scene_keyword(registered_stem)
            if final_lighting_key and matched_light_key != "custom":
                lighting_mgr.set_preset_override(final_lighting_key)
            trigger_auto_bg_sound_if_enabled()

        scene_stem = landscape_mgr.derive_meaningful_scene_name(text, location, weather, time_of_day)
        started = landscape_mgr.trigger_generate_landscape_sync(
            location=location,
            weather=weather,
            time_of_day=time_of_day,
            display_label=text,
            lighting_key=target_key or "default",
            log_callback=audio_ctrl.add_log,
            on_preset_registered=_on_generation_complete,
        )
        if not started:
            audio_ctrl.speak_generating()
            return

        audio_ctrl.add_log(
            "SYSTEM",
            f"🧠 ComfyUI 초고화질 창문 풍경 생성 시작: \"{text}\" → '{scene_stem}.png' (완료 전까지 기존 화면 유지) — 네, 알겠습니다.",
            f"Location: {location} | Weather: {weather} | Time: {time_of_day}",
        )
        audio_ctrl.speak_ack()
    elif route_type == "search" and target_key:
        # [신규 요구사항] 웹 이미지 검색 트리거: '~ 찾아줘'
        # 구글 커스텀 서치 API 또는 고화질 웹 풍경 폴백으로 배경에 맞는 16:9 와이드 사진 선별 & 화면 맞춤 변환
        search_kw = target_key
        if web_search_mgr.is_searching:
            audio_ctrl.add_log(
                "WARN",
                f"⏳ 이미 웹 이미지 검색 중입니다 (\"{text}\")",
                "이전 검색이 완료된 후 다시 시도해 주세요.",
            )
            return

        def _worker_search(query_str: str) -> None:
            ok, msg, img_path, inferred_light = web_search_mgr.search_and_process_background(
                query=query_str,
                log_callback=audio_ctrl.add_log,
            )
            if ok and img_path and img_path.exists():
                stem = web_search_mgr._sanitize_filename(query_str)
                # 프리셋 폴더에도 등록
                target_in_presets = landscape_mgr.presets_dir / f"{stem}.jpg"
                try:
                    import shutil
                    shutil.copy2(img_path, target_in_presets)
                except Exception:
                    pass

                with landscape_mgr._lock:
                    landscape_mgr.record_preset_source(stem, "web")
                    landscape_mgr.presets[stem] = landscape_mgr.build_custom_preset(stem, source_type="web")

                AudioController.register_custom_scene_keyword(stem)
                sync_custom_scene_keywords()
                landscape_mgr.switch_to_preset(stem)

                if matched_light_key != "custom":
                    lighting_mgr.set_preset_override(inferred_light)
                trigger_auto_bg_sound_if_enabled()

                l_state = lighting_mgr.get_current_state()
                audio_ctrl.add_log(
                    "STT",
                    f"🌐 웹 배경 선별 및 화면 적용 완료: [{query_str}] — 네, 알겠습니다.",
                    f"1920x1080 맞춤 최적화 · 광원: {l_state.active_label} ({l_state.cct_kelvin}K) · 채도 {l_state.saturation_pct}%",
                )
                audio_ctrl.speak_ack()
            else:
                audio_ctrl.add_log("WARN", f"❌ 웹 이미지 검색 실패: {msg}")

        audio_ctrl.add_log(
            "SYSTEM",
            f"🔍 웹 이미지 검색 시작: \"{search_kw}\" (배경 사진 정밀 선별) — 네, 알겠습니다.",
            "구글 이미지 서치 및 고화질 웹 풍경 탐색 중...",
        )
        audio_ctrl.speak_ack()
        threading.Thread(
            target=_worker_search,
            args=(search_kw,),
            name="WebImageSearchThread",
            daemon=True,
        ).start()
    else:
        # [핵심 요구사항] '~ 만들어줘', '~ 그려줘', '~ 찾아줘', '~ 들려줘' 또는 등록된 프리셋 이름이 아닌 일반 발화에는 반응하지 않음
        audio_ctrl.add_log(
            "SYSTEM",
            f"ℹ️ 일반 발화 무시 (화면 유지): \"{text}\"",
            "이미지 생성은 '~ 만들어줘', 웹 이미지 검색은 '~ 찾아줘', 사운드 재생은 '~ 들려줘'/'배경에 맞는 소리 들려줘', 프리셋 전환은 등록된 이름에만 반응합니다.",
        )


audio_ctrl = AudioController(
    initial_wake_words=["윈도", "windo"],
    on_command_callback=handle_voice_command,
)
sync_custom_scene_keywords()

GLOBAL_UI_LOCK_SCRIPT = """
(function(){
    window.toggleWindoSettings = function(forceState) {
        const bd = document.getElementById('settings-backdrop');
        const ws = document.getElementById('windo-main-workspace');
        const btn = document.getElementById('floating-settings-btn');
        if (!bd) return false;
        const isCurrentlyOpen = bd.classList.contains('open') && window.getComputedStyle(bd).display !== 'none';
        const willOpen = (typeof forceState === 'boolean') ? forceState : !isCurrentlyOpen;
        if (willOpen) {
            bd.classList.add('open');
            bd.style.setProperty('display', 'flex', 'important');
            document.body.classList.add('settings-docked-open');
            if (ws) ws.classList.add('settings-open');
            if (btn) btn.classList.add('active');
        } else {
            bd.classList.remove('open');
            bd.style.setProperty('display', 'none', 'important');
            document.body.classList.remove('settings-docked-open');
            if (ws) ws.classList.remove('settings-open');
            if (btn) btn.classList.remove('active');
        }
        return willOpen;
    };

    if (!window.__windoEscKeyBound) {
        window.__windoEscKeyBound = true;
        document.addEventListener('keydown', function(evt) {
            if (evt.key === 'Escape') {
                const bd = document.getElementById('settings-backdrop');
                if (bd && bd.classList.contains('open')) {
                    window.toggleWindoSettings(false);
                }
            }
        });
    }

    if (!window.__windoScrollLockBound) {
        window.__windoScrollLockBound = true;
        window.__windoSavedDrawerScrollTop = null;
        window.__windoSavedOpenDetails = [];

        function getDrawerScrollBox() {
            return document.getElementById('settings-drawer-scroll-body') || document.querySelector('.drawer-body');
        }

        function isSettingsControlEvent(evt) {
            const target = evt?.detail?.target || evt?.target;
            if (!target) return false;
            if (target.id === 'settings-controls' || target.closest?.('#settings-controls')) return true;
            return false;
        }

        function saveDrawerState() {
            const box = getDrawerScrollBox();
            if (!box) return;
            window.__windoSavedDrawerScrollTop = box.scrollTop;
            const openIds = [];
            box.querySelectorAll('#settings-controls details[open]').forEach(function(d, idx) {
                openIds.push(d.id || ('idx_' + idx));
            });
            window.__windoSavedOpenDetails = openIds;
        }

        function restoreDrawerState() {
            if (window.__windoSavedDrawerScrollTop === null) return;
            const savedTop = window.__windoSavedDrawerScrollTop;
            const openIds = window.__windoSavedOpenDetails || [];
            const applyRestore = function() {
                const box = getDrawerScrollBox();
                if (!box) return;
                if (openIds.length > 0) {
                    box.querySelectorAll('#settings-controls details').forEach(function(d, idx) {
                        const key = d.id || ('idx_' + idx);
                        if (openIds.indexOf(key) !== -1) {
                            d.setAttribute('open', '');
                        }
                    });
                }
                box.scrollTop = savedTop;
            };
            applyRestore();
            requestAnimationFrame(applyRestore);
            setTimeout(applyRestore, 16);
        }

        document.addEventListener('htmx:beforeRequest', function(evt) {
            if (isSettingsControlEvent(evt)) {
                saveDrawerState();
            }
        });

        document.addEventListener('htmx:beforeSwap', function(evt) {
            const box = getDrawerScrollBox();
            if (box) {
                saveDrawerState();
            }
        });

        document.addEventListener('htmx:oobBeforeSwap', function(evt) {
            const elt = evt?.detail?.elt;
            if (elt && elt.id === 'settings-controls') {
                saveDrawerState();
            }
        });

        document.addEventListener('htmx:afterSwap', function(evt) {
            restoreDrawerState();
        });

        document.addEventListener('htmx:oobAfterSwap', function(evt) {
            const elt = evt?.detail?.elt;
            if (elt && elt.id === 'settings-controls') {
                restoreDrawerState();
            }
        });

        document.addEventListener('htmx:afterSettle', function(evt) {
            restoreDrawerState();
        });
    }
})();
"""

app, rt = fast_app(
    hdrs=(
        Link(rel="stylesheet", href="/assets/css/style.css?v=19"),
        Script(GLOBAL_UI_LOCK_SCRIPT),
    ),
)

IMAGE_FIT_MODE: str = "fill"


# -------------------------------------------------------------------------
# 정적 파일 라우트 (CSS, JS, 프리셋/캐시 생성 이미지, 타임랩스 재료 및 mp3/ 사운드 파일 서빙)
# -------------------------------------------------------------------------
@rt("/assets/css/style.css")
def get_css():
    return FileResponse(
        CSS_PATH,
        media_type="text/css",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@rt("/assets/js/{filename}")
def get_js_asset(filename: str):
    decoded_name = urllib.parse.unquote(filename)
    file_path = BASE_DIR / "assets" / "js" / decoded_name
    if not file_path.exists() or not file_path.is_file():
        return Response(status_code=404)
    return FileResponse(file_path, media_type="application/javascript")


@rt("/cache/ingredients/{bundle_id}/{filename}")
def get_ingredient_asset(bundle_id: str, filename: str):
    dec_bundle = urllib.parse.unquote(bundle_id)
    dec_name = urllib.parse.unquote(filename)
    file_path = BASE_DIR / ".cache" / "ingredients" / dec_bundle / dec_name
    if not file_path.exists() or not file_path.is_file():
        return Response(status_code=404)
    ext = file_path.suffix.lower()
    mime_type = VirtualLandscapeManager.MIME_MAP.get(ext, "image/jpeg")
    return FileResponse(file_path, media_type=mime_type)


@rt("/api/timelapse/ingredients")
def get_timelapse_ingredients_json():
    """프론트엔드 WebGL 셰이더 및 Web Audio GainNode 크로스페이더에 현재 재료 번들 JSON을 한 번에 반환"""
    payload = ingredients_mgr.get_runtime_state()
    return Response(
        json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
    )


@rt("/assets/images/{folder}/{filename}")
def get_image(folder: str, filename: str):
    decoded_name = urllib.parse.unquote(filename)
    file_path = BASE_DIR / "assets" / "images" / folder / decoded_name
    if not file_path.exists():
        return Response(status_code=404)
    ext = file_path.suffix.lower()
    mime_type = VirtualLandscapeManager.MIME_MAP.get(ext)
    return FileResponse(file_path, media_type=mime_type)


@rt("/cache/landscapes/{filename}")
def get_cached_landscape(filename: str):
    decoded_name = urllib.parse.unquote(filename)
    file_path = BASE_DIR / ".cache" / "landscapes" / decoded_name
    if not file_path.exists():
        return Response(status_code=404)
    ext = file_path.suffix.lower()
    mime_type = VirtualLandscapeManager.MIME_MAP.get(ext, "image/png")
    return FileResponse(file_path, media_type=mime_type)


@rt("/mp3/{filename}")
def get_mp3_file(filename: str):
    decoded_name = urllib.parse.unquote(filename)
    file_path = sound_mgr.mp3_dir / decoded_name
    if not file_path.exists() or not file_path.is_file():
        return Response(status_code=404)
    return FileResponse(file_path, media_type="audio/mpeg")


# -------------------------------------------------------------------------
# 1. 전체화면 순수 가상 창문 뷰 + 스마트 창문 실시간 HUD 오버레이 (시간·날씨·기온·습도·그래프)
# -------------------------------------------------------------------------
def _get_weather_svg(icon_type: str) -> str:
    """레퍼런스 사진과 동일한 화이트 라인아트 SVG 날씨 아이콘"""
    if icon_type == "rain":
        return (
            '<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.95)" '
            'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M20 16.2A4.5 4.5 0 0 0 17.5 8h-1.8A7 7 0 1 0 4 14.9"/>'
            '<path d="M16 14v6"/><path d="M8 14v6"/><path d="M12 16v6"/></svg>'
        )
    if icon_type == "snow":
        return (
            '<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.95)" '
            'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M20 17.58A5 5 0 0 0 18 8h-1.26A8 8 0 1 0 4 16.25"/>'
            '<line x1="8" y1="16" x2="8.01" y2="16"/><line x1="8" y1="20" x2="8.01" y2="20"/>'
            '<line x1="12" y1="18" x2="12.01" y2="18"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg>'
        )
    if icon_type in ("cloud", "partly_cloudy"):
        return (
            '<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.95)" '
            'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M12 2v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="M20 12h2"/><path d="m19.07 4.93-1.41 1.41"/>'
            '<path d="M15.947 12.65a4 4 0 0 0-5.925-4.128"/><path d="M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6Z"/></svg>'
        )
    # 기본 태양(Sun) 아이콘 — 레퍼런스 사진과 완벽히 동일한 디자인
    return (
        '<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.95)" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/>'
        '<path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/>'
        '<path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>'
    )


def _get_humidity_cloud_svg() -> str:
    """레퍼런스 사진의 습도(Cloud) 라인아트 SVG 아이콘"""
    return (
        '<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.95)" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/></svg>'
    )


def render_smart_window_hud() -> FT:
    """
    좌측 투명 스마트 글래스 정보 창(HUD):
    - "정보"/"전체" → 시간·날씨·공기질 섹션 + 캘린더 모두 표시 / "전체 꺼"·"정보 꺼" → 모두 숨김
    - "날씨"/"기온" → 날씨 섹션, "시간"/"날짜" → 시계 섹션, "공기질"/"미세먼지" → 공기질 섹션만 개별 표시
    - "날씨 꺼", "시간 꺼", "공기질 꺼" → 해당 섹션만 숨김 (나머지는 그대로 유지)
    """
    is_vis, time_str, date_eng, date_kor, cur_hour, w_snap, iaq = hud_mgr.get_live_data()
    if not is_vis:
        return ""
    sec = dict(hud_mgr.sections)
    blocks: list = []

    # 1) 시간·날짜 섹션
    if sec.get("time"):
        live_clock_js = """
        (function(){
            if (window.__windoHudClockTimer) clearInterval(window.__windoHudClockTimer);
            const daysEng = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
            const daysKor = ['일','월','화','수','목','금','토'];
            const monthsEng = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
            function tickHudClock(){
                const elTime = document.getElementById('hud-live-time');
                const elDateEng = document.getElementById('hud-live-date-eng');
                const elDateKor = document.getElementById('hud-live-date-kor');
                if (!elTime) return;
                const now = new Date();
                const hh = String(now.getHours()).padStart(2, '0');
                const mm = String(now.getMinutes()).padStart(2, '0');
                const dd = String(now.getDate()).padStart(2, '0');
                const mo = String(now.getMonth() + 1).padStart(2, '0');
                elTime.textContent = hh + ':' + mm;
                if (elDateEng) elDateEng.textContent = daysEng[now.getDay()] + ' ' + dd + ' ' + monthsEng[now.getMonth()];
                if (elDateKor) elDateKor.textContent = now.getFullYear() + '. ' + mo + '. ' + dd + ' (' + daysKor[now.getDay()] + ')';
            }
            tickHudClock();
            window.__windoHudClockTimer = setInterval(tickHudClock, 1000);
        })();
        """
        blocks.append(
            Div(
                Div(time_str, id="hud-live-time", cls="hud-clock-text"),
                Div(date_eng, id="hud-live-date-eng", cls="hud-date-text"),
                Div(date_kor, id="hud-live-date-kor", cls="hud-date-sub"),
                Script(live_clock_js),
                cls="hud-time-section",
            )
        )

    # 2) 날씨·기온·습도 + 24시간 트렌드 그래프 섹션
    if sec.get("weather"):
        bars_src = w_snap.hourly_values if len(w_snap.hourly_values) >= 24 else [22] * 24
        chart_bars = []
        for idx in range(18):
            hour_idx = int(round((idx / 17.0) * 23))
            val = bars_src[hour_idx]
            pct_h = max(12, min(96, int(round((val / 50.0) * 100))))
            if hour_idx == cur_hour:
                bar_cls = "hud-chart-bar bar-active"
            elif idx % 2 == 1:
                bar_cls = "hud-chart-bar bar-blue"
            else:
                bar_cls = "hud-chart-bar bar-white"
            chart_bars.append(Div(cls=bar_cls, style=f"height:{pct_h}%;"))

        if blocks:
            blocks.append(Div(cls="hud-divider"))
        blocks.append(
            Div(
                Div(NotStr(_get_weather_svg(w_snap.icon_type)), cls="hud-icon-wrap"),
                Div(
                    Div(f"{w_snap.temp_c}°C", cls="hud-metric-primary"),
                    Div(f"{w_snap.condition_kor} · {w_snap.condition_eng}", cls="hud-metric-secondary"),
                    cls="hud-metric-info",
                ),
                cls="hud-metric-row",
            )
        )
        blocks.append(Div(cls="hud-divider"))
        blocks.append(
            Div(
                Div(NotStr(_get_humidity_cloud_svg()), cls="hud-icon-wrap"),
                Div(
                    Div(f"{w_snap.humidity_pct}%", cls="hud-metric-primary"),
                    Div(f"습도 · 체감 {w_snap.feels_like_c}°C", cls="hud-metric-secondary"),
                    cls="hud-metric-info",
                ),
                cls="hud-metric-row",
            )
        )
        blocks.append(
            Div(
                Div(
                    Div(Span("45"), Span("30"), Span("15"), Span("0"), cls="hud-chart-yaxis"),
                    Div(*chart_bars, cls="hud-chart-plot"),
                    cls="hud-chart-body",
                ),
                Div(
                    Span("00"), Span("04"), Span("08"), Span("12"), Span("16"), Span("20"), Span("24"),
                    cls="hud-chart-xaxis",
                ),
                cls="hud-chart-section",
            )
        )

    # 3) 실내 공기질(IAQ) + AI 위험도 판단 + 공기청정기 작동 안내 멘트 섹션
    if sec.get("iaq"):
        if blocks:
            blocks.append(Div(cls="hud-divider"))
        blocks.append(
            Div(
                Div(
                    Div(Span("💨"), Span("실내 공기질"), cls="hud-iaq-title-wrap"),
                    Span(iaq.grade_label, cls=f"hud-iaq-badge {iaq.badge_cls}"),
                    cls="hud-iaq-top",
                ),
                Div(
                    Div(Span("PM2.5", cls="hud-iaq-pill-label"), Span(f"{iaq.pm25}㎍", cls="hud-iaq-pill-val"), cls="hud-iaq-pill"),
                    Div(Span("PM10", cls="hud-iaq-pill-label"), Span(f"{iaq.pm10}㎍", cls="hud-iaq-pill-val"), cls="hud-iaq-pill"),
                    Div(Span("CO₂", cls="hud-iaq-pill-label"), Span(f"{iaq.co2_ppm}ppm", cls="hud-iaq-pill-val"), cls="hud-iaq-pill"),
                    Div(Span("TVOC", cls="hud-iaq-pill-label"), Span(f"{iaq.tvoc_ppb}ppb", cls="hud-iaq-pill-val"), cls="hud-iaq-pill"),
                    cls="hud-iaq-metrics",
                ),
                Div(
                    iaq.action_message,
                    cls=f"hud-iaq-action-note {'action-danger' if iaq.is_dangerous else ''}",
                ),
                cls="hud-iaq-section",
            )
        )

    return Div(*blocks, id="smart-window-hud", cls="smart-window-hud")


GCAL_COLOR_CLASSES = ("blue", "green", "amber", "rose")


def render_smart_window_calendar() -> FT:
    """
    정보 창 오른쪽에 표시되는 Google Calendar 기능의 스마트 글래스 창:
    - "캘린더"라고 말하면 켜지고, "캘린더 꺼"라고 말하면 꺼짐 ("전체/정보 켜·꺼"에도 함께 반응)
    - 월간 달력, 오늘 하이라이트, 이전/오늘/다음 달 이동, 날짜 선택, 일정 추가·삭제
    """
    if not calendar_mgr.visible:
        return ""

    year = calendar_mgr.view_year
    month = calendar_mgr.view_month
    selected_date = calendar_mgr.selected_date
    today_str = datetime.now().strftime("%Y-%m-%d")
    cal_dates = list(calendar.Calendar(firstweekday=6).itermonthdates(year, month))
    event_colors = calendar_mgr.get_month_event_colors(year, month)

    day_cells = []
    for idx, dt in enumerate(cal_dates):
        col_idx = idx % 7
        date_str = dt.strftime("%Y-%m-%d")
        is_cur_month = (dt.month == month)
        cls_parts = ["gcal-day-cell"]

        if col_idx == 0:
            cls_parts.append("day-sun")
        elif col_idx == 6:
            cls_parts.append("day-sat")

        if not is_cur_month:
            cls_parts.append("day-other-month")
            day_cells.append(
                Div(
                    Span(str(dt.day), cls="gcal-day-num"),
                    Div(cls="gcal-event-dots empty"),
                    cls=" ".join(cls_parts),
                )
            )
            continue

        if date_str == today_str:
            cls_parts.append("day-today")
        if date_str == selected_date:
            cls_parts.append("day-selected")

        dots = [
            Span(cls=f"gcal-dot dot-{c if c in GCAL_COLOR_CLASSES else 'blue'}")
            for c in event_colors.get(dt.day, [])[:3]
        ]
        dots_container = Div(*dots, cls="gcal-event-dots") if dots else Div(cls="gcal-event-dots empty")
        day_cells.append(
            Button(
                Span(str(dt.day), cls="gcal-day-num"),
                dots_container,
                type="button",
                cls=" ".join(cls_parts),
                hx_post=f"/api/calendar/select?date_str={date_str}",
                hx_target="#window-scene-stage",
                hx_swap="outerHTML",
            )
        )

    selected_events = calendar_mgr.get_events_for_date(selected_date)
    if selected_events:
        agenda_items = [
            Div(
                Div(
                    Span(ev.time_str, cls="gcal-event-time"),
                    Span(ev.title, cls="gcal-event-title", title=ev.title),
                    cls="gcal-event-main",
                ),
                Button(
                    "✕",
                    type="button",
                    title="일정 삭제",
                    cls="gcal-del-btn",
                    hx_post=f"/api/calendar/delete?event_id={urllib.parse.quote(ev.id)}",
                    hx_target="#window-scene-stage",
                    hx_swap="outerHTML",
                ),
                cls=f"gcal-event-item ev-{ev.color if ev.color in GCAL_COLOR_CLASSES else 'blue'}",
            )
            for ev in selected_events
        ]
    else:
        agenda_items = [Div("등록된 일정이 없습니다.", cls="gcal-empty-msg")]

    nav_btn = lambda label, delta, title: Button(  # noqa: E731
        label,
        type="button",
        cls="gcal-nav-btn",
        title=title,
        hx_post=f"/api/calendar/nav?delta={delta}",
        hx_target="#window-scene-stage",
        hx_swap="outerHTML",
    )

    return Div(
        Div(
            Div(
                Span("📅", cls="gcal-brand-icon"),
                Div(
                    Div("Google Calendar", cls="gcal-brand-sub"),
                    Div(f"{year}년 {month}월", cls="gcal-brand-title"),
                    cls="gcal-brand-text",
                ),
                cls="gcal-brand",
            ),
            Div(nav_btn("◀", -1, "이전 달"), nav_btn("오늘", 0, "오늘"), nav_btn("▶", 1, "다음 달"), cls="gcal-nav-btns"),
            cls="gcal-header",
        ),
        Div(
            Span("일", cls="gcal-weekday sun"),
            Span("월", cls="gcal-weekday"),
            Span("화", cls="gcal-weekday"),
            Span("수", cls="gcal-weekday"),
            Span("목", cls="gcal-weekday"),
            Span("금", cls="gcal-weekday"),
            Span("토", cls="gcal-weekday sat"),
            cls="gcal-weekdays",
        ),
        Div(*day_cells, cls="gcal-grid"),
        Div(cls="hud-divider"),
        Div(
            Div(
                Span("📅", cls="gcal-agenda-icon"),
                Span(f"{selected_date} 일정", cls="gcal-agenda-date"),
                cls="gcal-agenda-title-wrap",
            ),
            Span(f"{len(selected_events)}건", cls="gcal-agenda-count"),
            cls="gcal-agenda-header",
        ),
        Div(*agenda_items, cls="gcal-agenda-list"),
        Form(
            Input(type="hidden", name="date_str", value=selected_date),
            Input(type="text", name="time_str", value="14:00", placeholder="시간", cls="gcal-input-time"),
            Input(type="text", name="title", placeholder="새 일정 입력", required=True, cls="gcal-input-title"),
            Button("＋", type="submit", cls="gcal-add-btn", title="일정 추가"),
            hx_post="/api/calendar/add",
            hx_target="#window-scene-stage",
            hx_swap="outerHTML",
            cls="gcal-add-form",
        ),
        id="smart-window-calendar",
        cls="smart-window-calendar",
    )


def compute_window_sig() -> str:
    l_state = lighting_mgr.get_current_state()
    s_state = landscape_mgr.get_state()
    is_vis, time_str, _, _, _, w_snap, iaq = hud_mgr.get_live_data()
    sec_sig = "".join("1" if hud_mgr.sections.get(k) else "0" for k in ("time", "weather", "iaq"))
    hud_sig = (
        f"{sec_sig}-{time_str}-{w_snap.temp_c}-{w_snap.humidity_pct}-"
        f"{iaq.pm25}-{iaq.pm10}-{iaq.co2_ppm}-{iaq.tvoc_ppb}"
        if is_vis
        else "000"
    )
    cal_sig = (
        f"1-{calendar_mgr.view_year}-{calendar_mgr.view_month}-{calendar_mgr.selected_date}-"
        f"{len(calendar_mgr.events)}-{datetime.now().strftime('%Y%m%d')}"
        if calendar_mgr.visible
        else "0"
    )
    active_c_hex = coelux_mgr.get_active_light_palettes()[3]
    coelux_sig = (
        f"{int(coelux_mgr.is_enabled)}-{coelux_mgr.last_detected_time}-"
        f"{coelux_mgr.light_x_pct:.1f}-{coelux_mgr.light_y_pct:.1f}-"
        f"{coelux_mgr.intensity_pct}-{active_c_hex}"
    )
    snd_state = sound_mgr.get_state()
    sound_sig = (
        f"{int(snd_state['is_playing'])}-{snd_state['filename']}-"
        f"{snd_state['volume_pct']}-{int(snd_state['auto_match_bg'])}-{snd_state['play_version']}"
    )
    # 타임랩스 활성 상태(이미지 3개 완성 후)일 때 일정 시간 간격마다 다음 이미지로 전환 판정
    ingredients_mgr.tick_and_get_current_image()
    tl_bundle_id = ingredients_mgr.active_bundle.bundle_id if ingredients_mgr.active_bundle else "none"
    tl_sig = (
        f"{int(ingredients_mgr.is_generating)}-{ingredients_mgr.generation_progress_pct}-"
        f"{ingredients_mgr.current_step_num}-{int(ingredients_mgr.is_active)}-"
        f"{tl_bundle_id}-{ingredients_mgr.current_stage_idx}-"
        f"{ingredients_mgr.switch_version}-{ingredients_mgr.speed_multiplier:g}"
    )
    return (
        f"{s_state.version}-{int(s_state.is_generating)}-{s_state.generation_progress_pct}-"
        f"{l_state.active_preset_key}-{l_state.cct_kelvin}-{l_state.saturation_pct}-"
        f"{l_state.brightness_pct}-{l_state.contrast_pct}-{IMAGE_FIT_MODE}-{hud_sig}-{cal_sig}-{coelux_sig}-{sound_sig}-{tl_sig}"
    )


def render_window_stage(oob: bool = False) -> FT:
    global _LAST_RENDERED_SCENE_VERSION, _LAST_RENDERED_TL_SWITCH_VERSION
    l_state = lighting_mgr.get_current_state()
    s_state = landscape_mgr.get_state()
    if coelux_mgr.is_enabled:
        coelux_mgr.analyze_background_file(
            get_active_background_path(),
            scene_hint=f"{s_state.active_title} {s_state.active_key}",
        )
    state_sig = compute_window_sig()
    img_inline_style = (
        f"width:100%; height:100%; object-fit:{IMAGE_FIT_MODE}; "
        "object-position:center center; display:block;"
    )

    if s_state.is_generating:
        pct = s_state.generation_progress_pct
        if pct <= 40:
            step_label = "Step 1/4"
        elif pct <= 55:
            step_label = "Step 2/4"
        elif pct <= 92:
            step_label = "Step 3/4"
        else:
            step_label = "Step 4/4"
        loading_text = f"배경 생성 중 ({step_label})"
    elif ingredients_mgr.is_generating:
        tl_step = max(1, min(3, ingredients_mgr.current_step_num))
        tl_pct = ingredients_mgr.generation_progress_pct
        loading_text = f"타임랩스 이미지 생성 중 (Step {tl_step}/3 · {tl_pct}%)"
    elif web_search_mgr.is_searching:
        loading_text = "배경 검색 중..."
    else:
        loading_text = ""

    loading_badge = (
        Div(
            Div(cls="window-loading-spinner"),
            Span(loading_text),
            Div(Span(), Span(), Span(), cls="window-loading-dots"),
            cls="window-loading-badge",
        )
        if loading_text
        else ""
    )

    extra_attrs = {"hx_swap_oob": "true"} if oob else {}
    # [핵심 보장]
    # 1) 타임랩스가 실행 중(이미지 3개 모두 완성 후)이면, 기존 사진(1단계 원본)과 새로 생성된 다른 시간대 사진 2개(2단계, 3단계)가
    #    설정창에서 지정한 배속 간격마다 순서대로 다음 사진으로 바뀝니다.
    # 2) 일반 모드에서는 실제 배경 버전(s_state.version)이 바뀐 순간에만 크로스페이드를 실행합니다.
    tl_curr_url, tl_prev_url, tl_stage_idx, tl_stage_lbl = ingredients_mgr.tick_and_get_current_image()
    if ingredients_mgr.is_active and tl_curr_url:
        tl_switched = (ingredients_mgr.switch_version != _LAST_RENDERED_TL_SWITCH_VERSION) and (not ingredients_mgr.is_generating)
        _LAST_RENDERED_TL_SWITCH_VERSION = ingredients_mgr.switch_version
        _LAST_RENDERED_SCENE_VERSION = s_state.version
        curr_img_url = tl_curr_url
        prev_img_url = (tl_prev_url or tl_curr_url) if tl_switched else tl_curr_url
        curr_layer_style = "" if tl_switched else "animation:none; opacity:1;"
        curr_layer_key = f"tl-v{ingredients_mgr.switch_version}-s{tl_stage_idx}-{IMAGE_FIT_MODE}"
    else:
        scene_changed = (s_state.version != _LAST_RENDERED_SCENE_VERSION) and (not s_state.is_generating) and (not ingredients_mgr.is_generating)
        _LAST_RENDERED_SCENE_VERSION = s_state.version
        _LAST_RENDERED_TL_SWITCH_VERSION = ingredients_mgr.switch_version
        curr_img_url = s_state.current_image_url
        prev_img_url = s_state.previous_image_url if scene_changed else s_state.current_image_url
        curr_layer_style = "" if scene_changed else "animation:none; opacity:1;"
        curr_layer_key = f"scene-v{s_state.version}-{IMAGE_FIT_MODE}"

    hud_el = render_smart_window_hud()
    cal_el = render_smart_window_calendar()
    # 정보 창과 캘린더를 하나의 반응형 도크에 배치 → 정보 창이 꺼지면 캘린더가 자동으로 왼쪽으로 붙고,
    # 창 크기가 작아지면 자동으로 아래로 줄바꿈/축소됩니다.
    overlay_dock = Div(hud_el, cal_el, cls="smart-overlay-dock") if (hud_el or cal_el) else ""

    coelux_layers = ()
    if coelux_mgr.is_enabled:
        p = coelux_mgr.get_placement_state()
        t_mode = p["time_of_day"]
        sx = p["x_pct"]
        sy = p["y_pct"]
        core_rgb = p["core_rgb"]
        corona_rgb = p["corona_rgb"]
        ray_rgb = p["ray_rgb"]
        scale = p["intensity_scale"]
        active_hex = p["active_color_hex"]

        # =====================================================================
        # [1] 밤 / 저녁 (EVENING): 참고 이미지(구름 속 은은한 달빛) 완벽 재현
        # - 광원 아래의 인공적인 가로 줄무늬(Water Shimmer Strips) 및 딱딱한 동그라미 테두리 완전 제거!
        # - 구름 사이에서 부드럽게 번져 나오는 은청색·백색 달빛 코어 +
        #   주변 구름 결을 따라 은은하게 퍼지는 다층 구름 달무리(Soft Cloud-Diffused Moonlight)만 자연스럽게 표현
        # =====================================================================
        if t_mode == "EVENING":
            m_op1 = min(1.0, 0.94 * scale)
            m_op2 = min(0.88, 0.56 * scale)
            m_op3 = min(0.78, 0.28 * scale)
            m_op4 = min(0.68, 0.12 * scale)
            m_op5 = min(0.55, 0.04 * scale)

            coelux_layers = (
                # 1) 인위적인 원형 경계선 없이 구름 사이에서 부드럽게 퍼져 나오는 자연 달빛 코어 & 가우시안 달무리
                Div(
                    id="coelux-natural-moon",
                    style=(
                        "position:absolute; inset:0; pointer-events:none; z-index:4; "
                        f"background: radial-gradient(circle at {sx}% {sy}%, "
                        f"rgba(255,255,255,{m_op1:.2f}) 0%, "
                        f"rgba({core_rgb},{m_op1 * 0.88:.2f}) 1.8%, "
                        f"rgba({corona_rgb},{m_op2:.2f}) 4.8%, "
                        f"rgba({corona_rgb},{m_op3:.2f}) 10.5%, "
                        f"rgba({ray_rgb},{m_op4:.2f}) 20.0%, "
                        f"rgba({ray_rgb},{m_op5:.2f}) 34.0%, "
                        "transparent 52%); "
                        "filter: blur(8px); mix-blend-mode: screen; transition: all 0.5s ease;"
                    ),
                ),
                # 2) 주변 밤구름 결을 따라 비대칭 타원형으로 은은하게 번지는 은청색 구름 확산 달빛 (줄무늬 일절 없음!)
                Div(
                    id="coelux-natural-moon-cloud-bloom",
                    style=(
                        "position:absolute; inset:0; pointer-events:none; z-index:5; "
                        f"background: radial-gradient(ellipse 28% 15% at {sx}% {sy}%, "
                        f"rgba({core_rgb},{min(0.82, 0.34 * scale):.2f}) 0%, "
                        f"rgba({corona_rgb},{min(0.72, 0.20 * scale):.2f}) 38%, "
                        f"rgba({ray_rgb},{min(0.60, 0.08 * scale):.2f}) 70%, "
                        "transparent 100%); "
                        "filter: blur(18px); mix-blend-mode: screen; transition: all 0.5s ease;"
                    ),
                ),
                # 3) 우측 상단 상태 배지
                Div(
                    Span("🌙 자연 달빛 ", style=f"font-weight:800; color:{active_hex};"),
                    Span(
                        f"{p['label_ko']} · 밝기 {p['intensity_pct']}% · 위치({sx:.0f}%, {sy:.0f}%)"
                    ),
                    style=(
                        "position:absolute; top:18px; right:76px; z-index:35; pointer-events:none; "
                        "padding:6px 14px; border-radius:999px; font-size:0.76rem; color:#fff; "
                        "background:rgba(15,23,42,0.68); border:1px solid rgba(255,255,255,0.28); "
                        "backdrop-filter:blur(10px); box-shadow:0 4px 16px rgba(0,0,0,0.28);"
                    ),
                ),
            )

        # =====================================================================
        # [2] 노을 / 석양 (SUNSET): 참고 이미지(구름 사이 황금빛 석양 노을) 완벽 재현
        # - 광원 아래의 인공적인 가로 줄무늬(Sunset Water Strips) 및 인위적인 원형 링 완전 제거!
        # - 구름·수평선 사이에서 빛나는 따뜻한 황금-백색 태양 코어 +
        #   주변 구름과 하늘을 가로로 부드럽게 물들이는 황금-주황 노을 대기 산란 광휘만 순수하게 표현!
        # =====================================================================
        elif t_mode == "SUNSET":
            s_op1 = min(1.0, 0.98 * scale)
            s_op2 = min(0.92, 0.64 * scale)
            s_op3 = min(0.85, 0.35 * scale)
            s_op4 = min(0.75, 0.16 * scale)
            s_op5 = min(0.65, 0.06 * scale)

            coelux_layers = (
                # 1) 응집된 황금-백색 노을 태양 코어 & 부드러운 가우시안 대기 산란 (원형 테두리 링 없음!)
                Div(
                    id="coelux-natural-sunset-core",
                    style=(
                        "position:absolute; inset:0; pointer-events:none; z-index:4; "
                        f"background: radial-gradient(circle at {sx}% {sy}%, "
                        f"rgba(255,255,245,{s_op1:.2f}) 0%, "
                        f"rgba({core_rgb},{s_op1 * 0.92:.2f}) 2.2%, "
                        f"rgba({corona_rgb},{s_op2:.2f}) 6.0%, "
                        f"rgba({corona_rgb},{s_op3:.2f}) 13.5%, "
                        f"rgba({ray_rgb},{s_op4:.2f}) 25.0%, "
                        f"rgba({ray_rgb},{s_op5:.2f}) 40.0%, "
                        "transparent 60%); "
                        "filter: blur(8px); mix-blend-mode: screen; transition: all 0.5s ease;"
                    ),
                ),
                # 2) 수평선과 구름 결을 따라 가로로 넓고 부드럽게 번지는 황금-주황 노을 광휘 (줄무늬 일절 없음!)
                Div(
                    id="coelux-natural-sunset-horizon-glow",
                    style=(
                        "position:absolute; inset:0; pointer-events:none; z-index:5; "
                        f"background: radial-gradient(ellipse 46% 18% at {sx}% {sy}%, "
                        f"rgba({core_rgb},{min(0.88, 0.46 * scale):.2f}) 0%, "
                        f"rgba({corona_rgb},{min(0.78, 0.28 * scale):.2f}) 38%, "
                        f"rgba({ray_rgb},{min(0.68, 0.12 * scale):.2f}) 72%, "
                        "transparent 100%); "
                        "filter: blur(18px); mix-blend-mode: screen; transition: all 0.5s ease;"
                    ),
                ),
                # 3) 우측 상단 상태 배지
                Div(
                    Span("🌇 자연 노을빛 ", style=f"font-weight:800; color:{active_hex};"),
                    Span(
                        f"{p['label_ko']} · 밝기 {p['intensity_pct']}% · 위치({sx:.0f}%, {sy:.0f}%)"
                    ),
                    style=(
                        "position:absolute; top:18px; right:76px; z-index:35; pointer-events:none; "
                        "padding:6px 14px; border-radius:999px; font-size:0.76rem; color:#fff; "
                        "background:rgba(15,23,42,0.68); border:1px solid rgba(255,255,255,0.28); "
                        "backdrop-filter:blur(10px); box-shadow:0 4px 16px rgba(0,0,0,0.28);"
                    ),
                ),
            )

        # =====================================================================
        # [3] 낮 / 아침 (NOON / MORNING): 긍정 예시 4, 5번 사진 완벽 재현
        # - 인위적인 카메라 보케 동그라미나 화면 구석을 하얗게 덮는 거대 원형 덩어리를 완전히 제거!
        # - 눈부신 태양 코어 + 태양 중심에서 사방 구름 사이로 뻗어나가며 끝이 부드럽게 사라지는
        #   비대칭 유기적 햇살 줄기(Organic Soft Sunbeams)만 순수하게 표현!
        # =====================================================================
        else:
            d_op1 = min(1.0, 0.98 * scale)
            d_op2 = min(0.92, 0.62 * scale)
            d_op3 = min(0.84, 0.30 * scale)
            d_op4 = min(0.72, 0.13 * scale)
            d_op5 = min(0.60, 0.04 * scale)

            sunbeams = p.get("sunbeams", [])
            sunbeam_polys = "".join(
                f'<polygon points="{bm["points"]}" fill="url(#coelux-sunbeam-fade)" '
                f'opacity="{min(0.95, bm["opacity"] * scale):.3f}" '
                f'filter="url(#coelux-beam-blur-{bm["blur"]})" />'
                for bm in sunbeams
            )

            sunbeam_svg = (
                '<svg viewBox="0 0 100 100" preserveAspectRatio="none" '
                'style="position:absolute; inset:0; width:100%; height:100%; pointer-events:none; z-index:5; mix-blend-mode:screen;">'
                "<defs>"
                f'<radialGradient id="coelux-sunbeam-fade" cx="{sx}%" cy="{sy}%" r="78%" fx="{sx}%" fy="{sy}%" gradientUnits="userSpaceOnUse">'
                f'<stop offset="0%" stop-color="rgb(255,255,255)" stop-opacity="0.96"/>'
                f'<stop offset="12%" stop-color="rgb({core_rgb})" stop-opacity="0.72"/>'
                f'<stop offset="34%" stop-color="rgb({corona_rgb})" stop-opacity="0.34"/>'
                f'<stop offset="65%" stop-color="rgb({ray_rgb})" stop-opacity="0.10"/>'
                f'<stop offset="100%" stop-color="rgb({ray_rgb})" stop-opacity="0.0"/>'
                "</radialGradient>"
                '<filter id="coelux-beam-blur-soft" x="-30%" y="-30%" width="160%" height="160%">'
                '<feGaussianBlur stdDeviation="1.35" />'
                "</filter>"
                '<filter id="coelux-beam-blur-wide" x="-35%" y="-35%" width="170%" height="170%">'
                '<feGaussianBlur stdDeviation="2.45" />'
                "</filter>"
                "</defs>"
                f"{sunbeam_polys}"
                "</svg>"
            )

            coelux_layers = (
                # 1) 응집된 눈부신 태양 코어 & 경계선 없이 하늘로 스며드는 가우시안 대기 산란 코로나 (긍정 예시 4, 5번 사진)
                Div(
                    id="coelux-natural-sun-core",
                    style=(
                        "position:absolute; inset:0; pointer-events:none; z-index:4; "
                        f"background: radial-gradient(circle at {sx}% {sy}%, "
                        f"rgba(255,255,255,{d_op1:.2f}) 0%, "
                        f"rgba({core_rgb},{d_op1 * 0.95:.2f}) 2.4%, "
                        f"rgba({corona_rgb},{d_op2:.2f}) 6.2%, "
                        f"rgba({corona_rgb},{d_op3:.2f}) 13.5%, "
                        f"rgba({ray_rgb},{d_op4:.2f}) 25.0%, "
                        f"rgba({ray_rgb},{d_op5:.2f}) 40.0%, "
                        "transparent 58%); "
                        "mix-blend-mode: screen; transition: all 0.5s ease;"
                    ),
                ),
                # 2) 구름 사이로 시원하고 부드럽게 뻗어나가는 비대칭 유기적 햇살 줄기 (긍정 예시 4, 5번 사진)
                NotStr(sunbeam_svg),
                # 3) 우측 상단 상태 배지
                Div(
                    Span("☀️ 자연 햇살 ", style=f"font-weight:800; color:{active_hex};"),
                    Span(
                        f"{p['label_ko']} · 밝기 {p['intensity_pct']}% · 위치({sx:.0f}%, {sy:.0f}%)"
                    ),
                    style=(
                        "position:absolute; top:18px; right:76px; z-index:35; pointer-events:none; "
                        "padding:6px 14px; border-radius:999px; font-size:0.76rem; color:#fff; "
                        "background:rgba(15,23,42,0.68); border:1px solid rgba(255,255,255,0.28); "
                        "backdrop-filter:blur(10px); box-shadow:0 4px 16px rgba(0,0,0,0.28);"
                    ),
                ),
            )

    # 생체리듬 자동 광원이 OFF이거나 코에룩스가 켜진 상태에서는 배경을 흐리게 만드는 구형 앰비언트 글로우를 숨김
    show_legacy_glow = (not coelux_mgr.is_enabled) and (l_state.active_preset_key != "off")

    # 앰비언트 사운드(MP3) 브라우저 무끊김 오디오 동기화 및 상단 상태 배지
    snd = sound_mgr.get_state()
    tl_active = ingredients_mgr.is_active
    snd_playing_js = "true" if snd["is_playing"] else "false"
    snd_url_js = json.dumps(snd["url_path"])
    snd_vol_js = f"{max(0.0, min(1.0, snd['volume_pct'] / 100.0)):.2f}"
    sound_sync_js = f"""
    (function(){{
        const shouldPlay = {snd_playing_js};
        const targetUrl = {snd_url_js};
        const targetVol = {snd_vol_js};
        if (!window.__windoAmbientAudio) {{
            const a = new Audio();
            a.loop = true;
            a.preload = 'auto';
            window.__windoAmbientAudio = a;
            window.__windoAmbientCurrentUrl = '';
        }}
        const audio = window.__windoAmbientAudio;
        audio.volume = targetVol;
        if (!shouldPlay || !targetUrl) {{
            if (!audio.paused) audio.pause();
            window.__windoAmbientCurrentUrl = '';
            return;
        }}
        if (window.__windoAmbientCurrentUrl !== targetUrl) {{
            window.__windoAmbientCurrentUrl = targetUrl;
            audio.src = targetUrl;
            audio.currentTime = 0;
        }}
        if (audio.paused) {{
            const p = audio.play();
            if (p && typeof p.catch === 'function') {{
                p.catch(function(){{
                    if (!window.__windoAudioUnlockBound) {{
                        window.__windoAudioUnlockBound = true;
                        const unlock = function(){{
                            window.__windoAudioUnlockBound = false;
                            if (window.__windoAmbientAudio && window.__windoAmbientCurrentUrl) {{
                                window.__windoAmbientAudio.play().catch(function(){{}});
                            }}
                            document.removeEventListener('click', unlock);
                            document.removeEventListener('keydown', unlock);
                        }};
                        document.addEventListener('click', unlock, {{ once: true }});
                        document.addEventListener('keydown', unlock, {{ once: true }});
                    }}
                }});
            }}
        }}
        const gearBtn = document.getElementById('floating-settings-btn');
        if (gearBtn && !gearBtn.__windoBound && window.toggleWindoSettings) {{
            gearBtn.__windoBound = true;
            gearBtn.onclick = function(e) {{
                if (e) e.stopPropagation();
                window.toggleWindoSettings();
            }};
        }}
    }})();
    """

    top_offset = 18
    if coelux_mgr.is_enabled:
        top_offset += 36
    sound_badge_top = f"{top_offset}px"
    if snd["is_playing"]:
        top_offset += 36
    tl_badge_top = f"{top_offset}px"

    sound_badge = (
        Div(
            Span(f"{snd['icon']} ", style="font-size:0.88rem;"),
            Span(f"{snd['title']}", style="font-weight:800; color:#38bdf8; margin-right:6px;"),
            Span(f"볼륨 {snd['volume_pct']}% · {snd['source']}"),
            style=(
                f"position:absolute; top:{sound_badge_top}; right:76px; z-index:35; pointer-events:none; "
                "padding:6px 14px; border-radius:999px; font-size:0.75rem; color:#fff; "
                "background:rgba(15,23,42,0.72); border:1px solid rgba(56,189,248,0.38); "
                "backdrop-filter:blur(10px); box-shadow:0 4px 16px rgba(0,0,0,0.28);"
            ),
        )
        if snd["is_playing"]
        else ""
    )

    # 타임랩스 실행 중일 때(3개 이미지 모두 완성 후) 우측 상단 작은 상태 배지만 표시 (동영상 컨트롤 바 및 플로팅 버튼 제거 완료)
    timelapse_badge = (
        Div(
            Span("🎬 타임랩스 ", style="font-weight:800; color:#38bdf8; margin-right:4px;"),
            Span(f"{tl_stage_idx + 1}/3단계: {tl_stage_lbl}", style="font-weight:700; color:#fde68a; margin-right:6px;"),
            Span(f"({ingredients_mgr.speed_multiplier:g}x · {ingredients_mgr.interval_sec:.0f}초 간격 전환)"),
            style=(
                f"position:absolute; top:{tl_badge_top}; right:76px; z-index:35; pointer-events:none; "
                "padding:6px 14px; border-radius:999px; font-size:0.75rem; color:#fff; "
                "background:rgba(15,23,42,0.74); border:1px solid rgba(56,189,248,0.42); "
                "backdrop-filter:blur(10px); box-shadow:0 4px 16px rgba(0,0,0,0.28);"
            ),
        )
        if (tl_active and tl_curr_url)
        else ""
    )

    return Div(
        Div(
            Div(
                Img(src=prev_img_url, cls="window-layer-img", style=img_inline_style, alt=""),
                cls="window-layer window-layer-prev",
            ),
            Div(
                Img(src=curr_img_url, cls="window-layer-img", style=img_inline_style, alt=""),
                key=curr_layer_key,
                cls="window-layer window-layer-curr",
                style=curr_layer_style,
            ),
            Div(
                id="window-tint-layer",
                cls="circadian-tint-layer",
                style=f"background-color: {l_state.overlay_rgba};",
            ),
            Div(
                id="window-glow-layer",
                cls="circadian-ambient-glow",
                style=(
                    f"background: radial-gradient(circle at 70% 25%, {l_state.rgb_hex}, transparent 65%);"
                    if show_legacy_glow
                    else "display:none;"
                ),
            ),
            *coelux_layers,
            id="window-filter-inner",
            cls="window-scene-stage",
            style=f"filter: {l_state.css_filter};",
        ),
        overlay_dock,
        sound_badge,
        timelapse_badge,
        loading_badge,
        Script(sound_sync_js),
        id="window-scene-stage",
        cls="window-scene-stage",
        data_sig=state_sig,
        **extra_attrs,
    )


# -------------------------------------------------------------------------
# 2. ComfyUI 실시간 노드 실행 그래프 뷰어 & 회색 톤 LOG 모니터
#    (<details>와 로그 스크롤 컨테이너를 폴링 교체 영역 바깥에 영구 고정하여
#     펼침 상태가 절대 닫히지 않고 마우스 휠 스크롤이 끊기지 않도록 보장)
# -------------------------------------------------------------------------
def render_comfy_live_progress_panel(s_state: LandscapeState, oob: bool = False) -> FT:
    """5번 ComfyUI 칸 아래에 표시되는 실시간 생성 진행률 및 단계 상태 헤더 (600ms OOB 갱신)"""
    if s_state.is_generating:
        gen_badge_cls = "status-badge status-transcribing"
        gen_badge_txt = f"🔵 ComfyUI 실시간 작동 중 ({s_state.generation_progress_pct}%)"
    elif s_state.generation_error_text:
        gen_badge_cls = "status-badge status-error"
        gen_badge_txt = "❌ 생성 오류 발생"
    elif s_state.generation_progress_pct == 100:
        gen_badge_cls = "status-badge status-listening"
        gen_badge_txt = "✅ 생성 완료 (100%)"
    else:
        gen_badge_cls = "status-badge status-idle"
        gen_badge_txt = "⚪ 생성 대기 중"

    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(
        Div(
            Span("⚡ ComfyUI 실시간 작동 상태", cls="comfy-live-head-title"),
            Span(gen_badge_txt, cls=gen_badge_cls),
            cls="gen-progress-header",
        ),
        Div(
            Div(
                cls="gen-progress-fill",
                style=f"width: {s_state.generation_progress_pct}%;",
            ),
            cls="gen-progress-track",
        ),
        Div(
            s_state.generation_status_text or "명령어('~ 만들어줘') 또는 위 생성 버튼을 누르면 실시간 노드 작동이 시작됩니다.",
            cls="gen-progress-desc",
        ),
        Div(
            f"⚠️ 오류 상세: {s_state.generation_error_text}",
            cls="gen-progress-error",
        )
        if s_state.generation_error_text
        else "",
        id="comfy-live-progress-box",
        cls="comfy-live-progress-box",
        **extra,
    )


def render_comfy_nodes_flow(s_state: LandscapeState, oob: bool = False) -> FT:
    """ComfyUI 7-노드 카드 목록 (5번 ComfyUI 칸 아래에서 실시간으로 작동 모습을 보여줌)"""
    node_cards = []
    for n in s_state.nodes:
        span_cls = "node-span-2" if n.node_id in ("3", "4", "7") else ""
        state_cls = f"node-{n.status}"
        badge_cls = f"comfy-node-badge badge-{n.status}"
        badge_label = {
            "idle": "IDLE",
            "running": f"RUNNING {n.progress_pct}%",
            "done": "DONE ✓",
            "skipped": "BYPASS",
            "error": "ERROR ✕",
        }.get(n.status, n.status.upper())

        node_cards.append(
            Div(
                Div(
                    Span(n.title, cls="comfy-node-title"),
                    Span(badge_label, cls=badge_cls),
                    cls="comfy-node-top",
                ),
                Div(n.summary, cls="comfy-node-summary"),
                Div(n.params_preview, cls="comfy-node-params", title=n.params_preview),
                Div(
                    Div(cls="comfy-node-bar-fill", style=f"width: {n.progress_pct}%;"),
                    cls="comfy-node-bar-track",
                ),
                cls=f"comfy-node-card {span_cls} {state_cls}",
            )
        )
    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(*node_cards, id="comfy-nodes-flow-box", cls="comfy-nodes-flow", **extra)


def render_comfy_node_graph(s_state: LandscapeState) -> FT:
    """5번 ComfyUI 생성 칸 바로 아래에 상시 표시되는 실시간 ComfyUI 작동 모니터"""
    return Div(
        render_comfy_live_progress_panel(s_state, oob=False),
        Div(
            Div(
                Span("🔗 실시간 7-노드 파이프라인 작동 모니터"),
                Span("7-Node Live Graph", cls="comfy-sub-pill"),
                cls="comfy-canvas-header",
            ),
            render_comfy_nodes_flow(s_state, oob=False),
            Details(
                Summary("📄 ComfyUI API Workflow JSON 보기 (클릭하여 펼치기)"),
                Pre(s_state.workflow_json_str, id="comfy-json-pre-box", cls="comfy-json-pre"),
                id="comfy-json-details-box",
                cls="comfy-json-details",
            ),
            cls="comfy-graph-canvas",
        ),
        id="comfy-live-monitor-wrapper",
        cls="comfy-live-monitor-wrapper",
    )


def compute_log_items_sig() -> str:
    """로그 목록만의 MD5 해시: 새 로그가 추가/삭제되었을 때만 로그 리스트 내부를 갱신"""
    logs = audio_ctrl.get_logs(limit=45)
    raw = "|".join(f"{e.timestamp}:{e.level}:{e.message}:{e.detail}" for e in logs)
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def compute_log_status_sig() -> str:
    """상단 마이크/음성 상태바 + ComfyUI 실시간 작동 상태 + 로그 해시 통합 시그니처"""
    s_state = landscape_mgr.get_state()
    node_sig = ",".join(f"{n.status}:{n.progress_pct}" for n in s_state.nodes)
    raw = (
        f"{compute_log_items_sig()}|{audio_ctrl.status_state}|{audio_ctrl.status_text}|"
        f"{audio_ctrl.selected_device_index}|{audio_ctrl.last_raw_speech}|"
        f"{audio_ctrl.last_recognized_command}|{int(s_state.is_generating)}|"
        f"{s_state.generation_progress_pct}|{s_state.generation_status_text}|"
        f"{s_state.generation_error_text}|{node_sig}"
    )
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]


def render_log_items_inner(oob: bool = False) -> FT:
    """스크롤 컨테이너(#terminal-log-scroll-box) 안의 로그 행들만 렌더링 (스크롤 컨테이너 자체는 절대 파괴하지 않음)"""
    logs = audio_ctrl.get_logs(limit=45)
    log_items = [
        Div(
            Span(entry.timestamp, cls="log-time"),
            Span(entry.level, cls=f"log-tag tag-{entry.level}"),
            Span(
                entry.message,
                cls=f"log-msg {'log-msg-STT' if entry.level == 'STT' else ''}",
            ),
            Span(f"({entry.detail})" if entry.detail else "", cls="log-detail"),
            cls="log-row",
        )
        for entry in reversed(logs)
    ]
    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(
        *log_items,
        id="terminal-log-items-inner",
        data_logsig=compute_log_items_sig(),
        style="display:flex; flex-direction:column; gap:6px;",
        **extra,
    )


def render_live_status_poller() -> FT:
    """600ms마다 폴링되는 상단 음성·마이크 상태 표시줄 (ComfyUI 모니터는 5번 칸 아래로 이동 완료)"""
    status_sig = compute_log_status_sig()
    log_items_sig = compute_log_items_sig()
    state_cls = f"status-badge status-{audio_ctrl.status_state}"

    last_cmd = audio_ctrl.last_recognized_command or "\"윈도\"라고 부른 뒤 \"저녁\", \"야경\", \"겨울 풍경 만들어줘\" 등을 말씀해 보세요"
    last_raw = audio_ctrl.last_raw_speech or "아직 감지된 음성이 없습니다."

    return Div(
        Div(
            Div(
                Span("🎙️ 마이크: ", cls="mic-label"),
                Strong(
                    f"{audio_ctrl.selected_device_name} (Idx: {audio_ctrl.selected_device_index})",
                    cls="mic-name",
                ),
            ),
            Div(Span(audio_ctrl.status_text, cls=state_cls)),
            cls="mic-status-bar",
        ),
        Div(
            Div(
                Span("🗣️ 감지된 음성: ", cls="highlight-label"),
                Strong(f"\"{last_raw}\"", cls="highlight-raw"),
                Br(),
                Span("✨ 실행된 명령: ", cls="highlight-label"),
                Strong(f"\"{last_cmd}\"", cls="highlight-cmd"),
            ),
            cls="speech-highlight-bar",
            style="border-bottom:none;",
        ),
        id="live-log-status-poller",
        data_sig=status_sig,
        data_logsig=log_items_sig,
        hx_get="/api/logs",
        hx_trigger="every 600ms",
        hx_vals="js:{sig: document.getElementById('live-log-status-poller')?.dataset?.sig || '', logsig: document.getElementById('terminal-log-items-inner')?.dataset?.logsig || ''}",
        hx_swap="outerHTML",
    )


def render_live_monitor() -> FT:
    """설정창 상단 음성 & 시스템 로그 모니터 컨테이너 (ComfyUI 작동 뷰어는 5번 ComfyUI 칸 아래로 분리됨)"""
    return Div(
        render_live_status_poller(),
        Div(
            render_log_items_inner(oob=False),
            id="terminal-log-scroll-box",
            cls="terminal-body",
        ),
        id="live-log-container",
        cls="live-log-container",
    )


_LAST_POLL_SETTINGS_SIG: str = ""


def compute_settings_sig() -> str:
    """시계(분/초) 및 ComfyUI 실시간 스텝 변화를 제외한 순수 설정창 상태 시그니처 (설정창 스크롤 및 열린 <details> 리셋 방지)"""
    s_state = landscape_mgr.get_state()
    l_state = lighting_mgr.get_current_state()
    snd_state = sound_mgr.get_state()
    active_c_hex = coelux_mgr.get_active_light_palettes()[3]
    coelux_sig = f"{int(coelux_mgr.is_enabled)}-{coelux_mgr.last_detected_time}-{coelux_mgr.light_x_pct:.1f}-{coelux_mgr.light_y_pct:.1f}-{coelux_mgr.intensity_pct}-{active_c_hex}"
    tl_sig = f"{int(ingredients_mgr.is_generating)}-{ingredients_mgr.generation_progress_pct}-{ingredients_mgr.current_step_num}-{int(ingredients_mgr.is_active)}-{ingredients_mgr.current_stage_idx}-{ingredients_mgr.speed_multiplier:g}"
    hud_sec_sig = "".join("1" if v else "0" for v in hud_mgr.sections.values()) + ("1" if calendar_mgr.visible else "0")
    raw = (
        f"{s_state.version}|{s_state.active_key}|"
        f"{l_state.active_preset_key}|{l_state.saturation_pct}|{l_state.brightness_pct}|{l_state.cct_kelvin}|"
        f"{int(snd_state['is_playing'])}|{snd_state['filename']}|{snd_state['volume_pct']}|"
        f"{coelux_sig}|{tl_sig}|{hud_sec_sig}"
    )
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]



def render_settings_controls(oob: bool = False) -> FT:
    """설정창 1~8번 제어 카드들 (조작 시 스크롤 점프 원천 차단: show:none focus-scroll:false + Scroll Lock 연동)"""
    global _LAST_POLL_SETTINGS_SIG
    CTRL_SWAP = "outerHTML show:none focus-scroll:false"
    sync_custom_scene_keywords()
    l_state = lighting_mgr.get_current_state()
    s_state = landscape_mgr.get_state()
    cfg = landscape_mgr.comfy_config
    coelux_mgr.analyze_background_file(
        get_active_background_path(),
        scene_hint=f"{s_state.active_title} {s_state.active_key}",
    )

    preset_buttons = []
    for key, preset in landscape_mgr.presets.items():
        l_preset = lighting_mgr.presets.get(preset.lighting_key)
        cct_info = f"{l_preset.cct_kelvin}K · 채도 {int(l_preset.saturation * 100)}%" if l_preset else ""
        if key not in DEFAULT_SCENE_PRESETS:
            voice_hint = key.replace("_", " ")
            sub_label = f"🗣️ \"윈도 {voice_hint}\" · {cct_info}"
        else:
            sub_label = cct_info
        is_active = (s_state.active_key == key and not s_state.is_special_ai_image)
        preset_buttons.append(
            Button(
                Span(preset.title),
                Span(sub_label, cls="preset-btn-sub"),
                type="button",
                cls=f"preset-btn {'active' if is_active else ''}",
                hx_post=f"/api/scene/preset?key={urllib.parse.quote(key)}",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
            )
        )

    model_options = [
        Option(m, value=m, selected=(m == cfg.model_name))
        for m in landscape_mgr.list_available_models()
    ]

    lora_options = [
        Option(
            "사용 안 함 (None)" if l_name == "none" else f"LoRA: {l_name}",
            value=l_name,
            selected=(l_name == cfg.lora_name),
        )
        for l_name in landscape_mgr.list_available_loras()
    ]

    sampler_options = [
        Option(s, value=s, selected=(s == cfg.sampler_name))
        for s in ("euler", "euler_ancestral", "dpmpp_2m", "heun")
    ]

    scheduler_options = [
        Option(sch, value=sch, selected=(sch == cfg.scheduler))
        for sch in ("simple", "normal", "karras", "sgm_uniform")
    ]

    mic_options = [
        Option(
            f"[{dev.index}] {dev.name} ({'★추천/무선' if dev.is_target_wireless else '활성'})",
            value=str(dev.index),
            selected=(dev.index == audio_ctrl.selected_device_index),
        )
        for dev in audio_ctrl.available_devices
    ]

    preset_select_options = [
        Option(f"기존 배경 교체: {preset.title}", value=key, selected=(key == "default"))
        for key, preset in landscape_mgr.presets.items()
    ]

    wake_badges = [Span(f"#{w}", cls="wake-badge") for w in audio_ctrl.wake_words]
    ww_joined = ", ".join(audio_ctrl.wake_words)
    mode_btn_text = (
        "호출어 모드 ON (클릭 시 호출어 없이 '아침/노을/저녁' 즉시 인식)"
        if audio_ctrl.require_wake_word
        else "연속 인식 모드 ON (호출어 없이 모든 말 즉시 인식 중)"
    )
    mode_btn_cls = "secondary" if audio_ctrl.require_wake_word else "success"

    live_preview_js = (
        "const f=this.closest('form');"
        "const s=f.querySelector('[name=saturation_pct]').value;"
        "const b=f.querySelector('[name=brightness_pct]').value;"
        "const c=f.querySelector('[name=cct_kelvin]').value;"
        "document.getElementById('sat-val-disp').textContent=s+'%';"
        "document.getElementById('bri-val-disp').textContent=b+'%';"
        "document.getElementById('cct-val-disp').textContent=c+'K';"
        "const flt=`saturate(${(s/100).toFixed(2)}) brightness(${(b/100).toFixed(2)}) contrast(1.02)`;"
        "const st=document.getElementById('window-filter-inner');"
        "if(st){st.style.filter=flt;}"
        "if(window.opener&&!window.opener.closed){"
        "const ost=window.opener.document.getElementById('window-filter-inner');"
        "if(ost){ost.style.filter=flt;}"
        "}"
    )

    extra_attrs = {"hx_swap_oob": "true"} if oob else {}
    hud_vis, hud_time, _, hud_date_kor, _, hud_weather, iaq_state = hud_mgr.get_live_data()
    cal_vis = calendar_mgr.visible
    all_on = all(hud_mgr.sections.values()) and cal_vis
    snd_state = sound_mgr.get_state()
    sound_tracks = sound_mgr.list_tracks()
    tl_rt = ingredients_mgr.get_runtime_state()
    tl_bundle_dict = tl_rt.get("bundle", {})
    tl_is_gen = bool(tl_rt.get("is_generating", False))
    tl_gen_pct = int(tl_rt.get("generation_progress_pct", 0))
    tl_gen_status = str(tl_rt.get("generation_status_text", ""))
    tl_cur_step = int(tl_rt.get("current_step_num", 0))
    tl_steps_list = tl_rt.get("steps", [])
    tl_cur_stage_idx = int(tl_rt.get("current_stage_idx", 0))
    tl_cur_speed = float(tl_rt.get("speed_multiplier", 1.0))
    tl_cur_interval = float(tl_rt.get("interval_sec", 8.0))

    tl_speed_buttons = []
    for sp_val, sp_sec in ((1, 8), (2, 4), (5, 2), (10, 1)):
        is_sp_active = abs(tl_cur_speed - float(sp_val)) < 0.1
        tl_speed_buttons.append(
            Button(
                Span(f"⚡ {sp_val}x 배속", style="font-weight:800; display:block; font-size:0.78rem;"),
                Span(f"{sp_sec}초마다 다음 사진", cls="preset-btn-sub"),
                type="button",
                cls=f"preset-btn {'active' if is_sp_active else ''}",
                hx_post=f"/api/timelapse/speed?speed={sp_val}",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
            )
        )

    tl_step_cards = []
    for st_item in tl_steps_list[:3]:
        s_num = int(st_item.get("step_num", 1))
        s_idx = s_num - 1
        s_lbl = str(st_item.get("label_ko", f"{s_num}단계"))
        s_status = str(st_item.get("status", "idle"))
        s_pct = int(st_item.get("progress_pct", 0))
        s_img = str(st_item.get("image_url", ""))
        s_detail = str(st_item.get("detail_ko", ""))
        is_curr_showing = ingredients_mgr.is_active and (not tl_is_gen) and (tl_cur_stage_idx == s_idx)

        if s_status == "done":
            st_badge_txt = "현재 창문 표시 중 ●" if is_curr_showing else "생성 완료 ✓"
            st_badge_cls = "tl-step-badge active" if is_curr_showing else "tl-step-badge done"
        elif s_status == "running":
            st_badge_txt = f"생성 중 {s_pct}%"
            st_badge_cls = "tl-step-badge running"
        else:
            st_badge_txt = "대기 중"
            st_badge_cls = "tl-step-badge idle"

        if s_status == "done" and s_img:
            visual_box = Img(
                src=s_img,
                alt=s_lbl,
                cls=f"tl-step-thumb {'active' if is_curr_showing else ''}",
            )
        elif s_status == "running":
            visual_box = Div(
                Div(cls="window-loading-spinner", style="width:18px; height:18px; border-width:2px; margin-bottom:4px;"),
                Span(f"Step {s_num}/3 생성 중", style="font-size:0.68rem; font-weight:700; color:#1d4ed8;"),
                cls="tl-step-placeholder running",
            )
        else:
            visual_box = Div(
                Span(f"Step {s_num}/3 대기", style="font-size:0.68rem; font-weight:600; color:#94a3b8;"),
                cls="tl-step-placeholder idle",
            )

        card_attrs = {}
        if s_status == "done" and ingredients_mgr.is_active and (not tl_is_gen):
            card_attrs = {
                "hx_post": f"/api/timelapse/stage?idx={s_idx}",
                "hx_target": "#settings-controls",
                "hx_swap": CTRL_SWAP,
                "title": "클릭 시 이 시간대 사진을 창문에 즉시 표시합니다.",
            }

        tl_step_cards.append(
            Div(
                visual_box,
                Div(
                    Div(
                        Span(f"Step {s_num}/3", cls="tl-step-num"),
                        Span(st_badge_txt, cls=st_badge_cls),
                        cls="tl-step-top-row",
                    ),
                    Span(f"{s_num}단계: {s_lbl}", cls="tl-step-title"),
                    Span(s_detail, cls="tl-step-desc"),
                    Div(
                        Div(
                            cls=f"tl-step-bar-fill {'done' if s_status == 'done' else ''}",
                            style=f"width:{s_pct}%;",
                        ),
                        cls="tl-step-bar-track",
                    ),
                    cls="tl-step-info",
                ),
                cls=f"tl-step-row-card {'active' if is_curr_showing else ''} {'clickable' if card_attrs else ''}",
                **card_attrs,
            )
        )

    sound_track_buttons = []
    for tr in sound_tracks:
        is_tr_active = snd_state["is_playing"] and (snd_state["filename"] == tr.filename)
        sound_track_buttons.append(
            Button(
                Span(f"{tr.icon} {tr.title}"),
                Span(f"🗣️ \"{tr.title} 들려줘\"", cls="preset-btn-sub"),
                type="button",
                cls=f"preset-btn {'active' if is_tr_active else ''}",
                hx_post=f"/api/sound/play?filename={urllib.parse.quote(tr.filename)}",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
            )
        )

    def _widget_toggle(icon: str, label: str, voice: str, is_on: bool, url: str) -> FT:
        return Button(
            Span(icon, cls="widget-toggle-icon"),
            Span(
                Span(label, cls="widget-toggle-label"),
                Span(f"🗣️ {voice}", cls="widget-toggle-voice"),
                cls="widget-toggle-text",
            ),
            Span("ON" if is_on else "OFF", cls="widget-toggle-state"),
            type="button",
            cls=f"widget-toggle {'on' if is_on else ''}",
            hx_post=url,
            hx_target="#settings-controls",
            hx_swap=CTRL_SWAP,
        )

    def _iaq_field(label: str, unit: str, name: str, value: int, vmin: str, vmax: str) -> FT:
        return Label(
            Span(label, cls="field-label"),
            Div(
                Input(type="number", name=name, value=str(value), min=vmin, max=vmax),
                Span(unit, cls="field-unit"),
                cls="field-with-unit",
            ),
            cls="field-block",
        )

    def _iaq_quick(label: str, query: str) -> FT:
        return Button(
            label,
            type="button",
            cls="secondary quick-btn",
            hx_post=f"/api/iaq/update?{query}",
            hx_target="#settings-controls",
            hx_swap=CTRL_SWAP,
        )

    _LAST_POLL_SETTINGS_SIG = compute_settings_sig()

    return Div(
        # =====================================================================
        # 1. 위젯 (화면 정보 위젯: 전체 = 정보 / 상세 = 시간·날씨·공기질·캘린더·코에룩스·사운드·타임랩스)
        # =====================================================================
        Div(
            Div(
                Span("1. 위젯 (화면 정보 표시 제어)"),
                Span("전체 ON" if all_on else ("일부 ON" if (hud_vis or cal_vis) else "모두 OFF"), cls="card-status-pill"),
                cls="option-card-title",
            ),
            P(
                "\"정보\" 또는 \"전체\"는 아래 모든 항목을 한 번에 켜고 끕니다. "
                "\"날씨\", \"시간\", \"공기질\", \"캘린더\"처럼 상세 이름 뒤에 \"꺼\"를 붙이면 그 항목만 꺼집니다.",
                cls="option-card-desc",
            ),
            Div(
                Button(
                    "전체 켜기",
                    type="button",
                    cls="success",
                    hx_post="/api/widgets/all?on=1",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                ),
                Button(
                    "전체 끄기",
                    type="button",
                    cls="secondary",
                    hx_post="/api/widgets/all?on=0",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                ),
                cls="btn-row-2",
            ),
            Div(
                _widget_toggle("🕒", "시간 · 날짜", "시간 / 날짜 (꺼)", hud_mgr.sections["time"], "/api/hud/section?key=time"),
                _widget_toggle("🌤️", "날씨 · 기온", "날씨 / 기온 (꺼)", hud_mgr.sections["weather"], "/api/hud/section?key=weather"),
                _widget_toggle("💨", "실내 공기질", "공기질 / 미세먼지 (꺼)", hud_mgr.sections["iaq"], "/api/hud/section?key=iaq"),
                _widget_toggle("📅", "구글 캘린더", "캘린더 (꺼)", cal_vis, "/api/calendar/toggle"),
                _widget_toggle("☀️", "코에룩스 조명", "조명 켜줘 / 조명 꺼줘", coelux_mgr.is_enabled, "/api/coelux/toggle"),
                _widget_toggle("🔊", "배경 맞춤 사운드", "배경에 맞는 소리 들려줘 / 소리 꺼줘", snd_state["is_playing"], "/api/sound/toggle"),
                _widget_toggle("🎬", "타임랩스 만들기", "타임랩스 만들어줘 / 타임랩스 꺼줘", ingredients_mgr.is_active, "/api/timelapse/toggle"),
                cls="widget-toggle-grid",
            ),
            id="sec-card-1",
            cls="option-card selected",
        ),
        # =====================================================================
        # 2. 창문풍경 & 자연광 채도 (배경 프리셋 + 코에룩스 자연 빛 + 생체리듬/채도/밝기/색온도 + 내 배경 추가·교체)
        # =====================================================================
        Div(
            Div(
                Span("2. 창문풍경 & 자연광 채도"),
                Span(
                    f"☀️ 코에룩스 {'ON' if coelux_mgr.is_enabled else 'OFF'} · {s_state.active_title}",
                    cls="card-status-pill",
                ),
                cls="option-card-title",
            ),
            Div(
                Button(
                    f"☀️ 코에룩스 입체 광원 {'ON' if coelux_mgr.is_enabled else 'OFF'}",
                    type="button",
                    cls="success" if coelux_mgr.is_enabled else "secondary",
                    hx_post="/api/coelux/toggle",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    title="이미지 내 광원(동쪽 아침 해·천창 태양·노을 해·밤하늘 달) 위치를 예측해 입체 광원을 배치합니다.",
                ),
                Button(
                    f"⏱️ 생체리듬 자동 광원 {'ON' if l_state.auto_mode else 'OFF'}",
                    type="button",
                    cls="success" if l_state.auto_mode else "secondary",
                    hx_post="/api/lighting/auto",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    title="클릭할 때마다 실시간 생체리듬 색온도 자동 동기화를 켜거나(ON) 끕니다(OFF).",
                ),
                cls="btn-row-2",
            ),
            P(
                f"현재 풍경: {s_state.active_title} | 광원 모드: {l_state.active_label}"
                + (
                    f" | ☀️ 코에룩스: {coelux_mgr.light_label_ko} (X:{coelux_mgr.light_x_pct:.0f}%, Y:{coelux_mgr.light_y_pct:.0f}%)"
                    if coelux_mgr.is_enabled
                    else ""
                ),
                cls="option-card-desc",
            ),
            Div(
                Div(
                    Strong("☀️ 코에룩스 자연 광원: ", style="color:#0284c7;"),
                    Span(
                        "인위적인 줄무늬나 원형 링 없이, 배경 이미지를 AI로 분석하여 "
                        "☀️ 낮·아침에는 눈부신 태양 코어와 구름 사이로 퍼지는 부드러운 햇살 줄기를, "
                        "🌇 노을에는 황금빛 석양 해와 구름 노을 번짐을, 🌙 밤에는 구름 사이로 은은하게 번지는 자연 달빛과 달무리를 배치합니다."
                    ),
                    style="margin-bottom:4px;",
                ),
                Div(
                    Strong("⏱️ 생체리듬 자동 광원: ", style="color:#059669;"),
                    Span(lighting_mgr.FEATURE_SUMMARY),
                ),
                cls="coelux-info-callout",
            ),
            Div(
                Div(
                    Span("현재 채도", cls="metric-label"),
                    Strong(f"{l_state.saturation_pct}%", id="sat-val-disp", cls="metric-value metric-blue"),
                    cls="metric-box",
                ),
                Div(
                    Span("현재 밝기", cls="metric-label"),
                    Strong(f"{l_state.brightness_pct}%", id="bri-val-disp", cls="metric-value"),
                    cls="metric-box",
                ),
                Div(
                    Span("색온도 (CCT)", cls="metric-label"),
                    Div(
                        Span(cls="cct-color-dot", style=f"background:{l_state.rgb_hex};"),
                        Strong(f"{l_state.cct_kelvin}K", id="cct-val-disp", cls="metric-value"),
                        cls="metric-cct-row",
                    ),
                    cls="metric-box",
                ),
                Div(
                    Span("화면 대비", cls="metric-label"),
                    Strong(f"{l_state.contrast_pct}%", id="metric-contrast-disp", cls="metric-value"),
                    cls="metric-box",
                ),
                cls="lighting-metrics-grid",
            ),
            Div(*preset_buttons, cls="preset-btn-grid"),
            # 코에룩스 자연 빛(햇살·노을·달빛) 밝기 · 색상 · 위치 조절 패널 (AI 자동 분석 연동)
            Div(
                Div(
                    Span("☀️ 코에룩스 빛 밝기 · 색상 · 위치 조절"),
                    Button(
                        "🤖 AI 자동 복원",
                        type="button",
                        cls=f"{'success' if (coelux_mgr.auto_color and coelux_mgr.auto_position) else 'secondary'} coelux-ai-reset-btn",
                        hx_post="/api/coelux/ai-reset",
                        hx_target="#settings-controls",
                        hx_swap=CTRL_SWAP,
                    ),
                    cls="coelux-subpanel-header",
                ),
                Div(
                    Span("빠른 빛 색상 선택", cls="field-label"),
                    Div(
                        Button(
                            "☀️ 황금 햇살",
                            type="button",
                            cls="secondary quick-btn",
                            hx_post="/api/coelux/color?hex_code=%23ffc86e",
                            hx_target="#settings-controls",
                            hx_swap=CTRL_SWAP,
                            style="border-left:4px solid #ffc86e;",
                        ),
                        Button(
                            "🤍 맑은 백색광",
                            type="button",
                            cls="secondary quick-btn",
                            hx_post="/api/coelux/color?hex_code=%23fffdf5",
                            hx_target="#settings-controls",
                            hx_swap=CTRL_SWAP,
                            style="border-left:4px solid #cbd5e1;",
                        ),
                        Button(
                            "🌇 노을 주황빛",
                            type="button",
                            cls="secondary quick-btn",
                            hx_post="/api/coelux/color?hex_code=%23ff883e",
                            hx_target="#settings-controls",
                            hx_swap=CTRL_SWAP,
                            style="border-left:4px solid #ff883e;",
                        ),
                        Button(
                            "🌙 은청색 달빛",
                            type="button",
                            cls="secondary quick-btn",
                            hx_post="/api/coelux/color?hex_code=%23a8d0ff",
                            hx_target="#settings-controls",
                            hx_swap=CTRL_SWAP,
                            style="border-left:4px solid #a8d0ff;",
                        ),
                        cls="quick-btn-grid",
                    ),
                    cls="control-group",
                    style="gap:5px;",
                ),
                Form(
                    Div(
                        Span(
                            f"빛 밝기 ({coelux_mgr.intensity_pct}%)",
                            id="coelux-int-disp",
                            cls="slider-label",
                        ),
                        Input(
                            type="range",
                            name="intensity_pct",
                            min="15",
                            max="200",
                            step="5",
                            value=str(coelux_mgr.intensity_pct),
                            oninput="document.getElementById('coelux-int-disp').textContent='빛 밝기 ('+this.value+'%)';",
                            cls="custom-range-slider",
                        ),
                        cls="slider-row",
                    ),
                    Div(
                        Span(
                            f"빛 색상 ({'AI 자동: ' if coelux_mgr.auto_color else '직접 선택: '}{coelux_mgr.get_active_light_palettes()[3]})",
                            cls="slider-label",
                        ),
                        Div(
                            Input(
                                type="color",
                                name="color_hex",
                                value=coelux_mgr.get_active_light_palettes()[3],
                                cls="coelux-color-picker",
                            ),
                            Span(
                                "컬러 피커로 원하는 빛 색상을 직접 고를 수 있습니다.",
                                cls="coelux-color-hint",
                            ),
                            cls="coelux-color-row",
                        ),
                        cls="slider-row",
                    ),
                    Div(
                        Span(
                            f"빛 가로 위치 X ({coelux_mgr.light_x_pct:.0f}%)",
                            id="coelux-x-disp",
                            cls="slider-label",
                        ),
                        Input(
                            type="range",
                            name="x_pct",
                            min="8",
                            max="92",
                            step="1",
                            value=str(int(round(coelux_mgr.light_x_pct))),
                            oninput="document.getElementById('coelux-x-disp').textContent='빛 가로 위치 X ('+this.value+'%)';",
                            cls="custom-range-slider",
                        ),
                        cls="slider-row",
                    ),
                    Div(
                        Span(
                            f"빛 세로 위치 Y ({coelux_mgr.light_y_pct:.0f}%)",
                            id="coelux-y-disp",
                            cls="slider-label",
                        ),
                        Input(
                            type="range",
                            name="y_pct",
                            min="8",
                            max="65",
                            step="1",
                            value=str(int(round(coelux_mgr.light_y_pct))),
                            oninput="document.getElementById('coelux-y-disp').textContent='빛 세로 위치 Y ('+this.value+'%)';",
                            cls="custom-range-slider",
                        ),
                        cls="slider-row",
                    ),
                    hx_post="/api/coelux/update",
                    hx_trigger="change",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    cls="slider-form-stack",
                ),
                cls="custom-sliders-box coelux-sliders-box",
            ),
            Form(
                Div(
                    Span("🎛️ 배경 자체 채도 · 밝기 · 색온도 조절"),
                    cls="custom-slider-title",
                ),
                Div(
                    Span(f"채도 ({l_state.saturation_pct}% · 0~250%)", cls="slider-label"),
                    Input(
                        type="range",
                        name="saturation_pct",
                        min="0",
                        max="250",
                        step="1",
                        value=str(l_state.saturation_pct),
                        oninput=live_preview_js,
                        cls="custom-range-slider",
                    ),
                    cls="slider-row",
                ),
                Div(
                    Span(f"밝기 ({l_state.brightness_pct}% · 30~180%)", cls="slider-label"),
                    Input(
                        type="range",
                        name="brightness_pct",
                        min="30",
                        max="180",
                        step="1",
                        value=str(l_state.brightness_pct),
                        oninput=live_preview_js,
                        cls="custom-range-slider",
                    ),
                    cls="slider-row",
                ),
                Div(
                    Span(f"색온도 ({l_state.cct_kelvin}K · 2000~9000K)", cls="slider-label"),
                    Input(
                        type="range",
                        name="cct_kelvin",
                        min="2000",
                        max="9000",
                        step="100",
                        value=str(l_state.cct_kelvin),
                        oninput=live_preview_js,
                        cls="custom-range-slider",
                    ),
                    cls="slider-row",
                ),
                hx_post="/api/lighting/custom",
                hx_trigger="change",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
                cls="custom-sliders-box",
            ),
            # 내 사진으로 배경 이미지 추가·교체 서브 섹션
            Details(
                Summary("🖼️ 내 사진으로 창문 배경 이미지 추가 · 교체 ('최애.jpg' 등)"),
                Form(
                    P(
                        "이름(예: 최애)을 입력하고 이미지를 추가하면 '최애.jpg'로 저장되며, 저녁·노을처럼 \"윈도 최애\"라고 부르면 바로 틀어집니다!",
                        cls="option-card-desc",
                        style="margin-bottom:6px;",
                    ),
                    Input(
                        type="text",
                        name="custom_name",
                        placeholder="새로 추가할 배경 이름 입력 (예: 최애, 우리집, 바다)",
                    ),
                    Select(*preset_select_options, name="preset_key"),
                    Div(
                        Input(
                            type="file",
                            name="image_file",
                            accept=".svg,.jpg,.jpeg,.png,.webp,.gif,.bmp,.avif,image/svg+xml,image/jpeg,image/png,image/*",
                            required=True,
                        ),
                        Button("이름 저장 & 즉시 적용", type="submit"),
                        cls="inline-input-row",
                    ),
                    hx_post="/api/scene/upload",
                    hx_encoding="multipart/form-data",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    cls="control-group",
                    style="margin-top:8px;",
                ),
                id="details-custom-bg-upload",
                cls="sub-collapsible-box",
            ),
            id="sec-card-2",
            cls="option-card selected",
        ),
        # =====================================================================
        # 3. 사운드 (mp3/ 폴더 소리 들려주기 & Gemini AI 배경 맞춤 소리)
        # =====================================================================
        Div(
            Div(
                Span("3. 사운드 (mp3 소리 & AI 배경 맞춤 소리)"),
                Span(
                    f"재생 중: {snd_state['icon']} {snd_state['title']}" if snd_state["is_playing"] else "🔇 재생 중지됨",
                    cls="card-status-pill",
                ),
                cls="option-card-title",
            ),
            P(
                "① \"비 소리 들려줘\", \"도시 소리 들려줘\"처럼 말하거나 아래 버튼을 누르면 mp3 폴더의 소리를 들려줍니다. "
                "② \"배경에 맞는 소리 들려줘\"라고 말하거나 AI 분석 버튼을 누르면 Gemini API가 현재 배경을 분석해 알맞은 mp3를 자동 재생합니다.",
                cls="option-card-desc",
            ),
            Div(
                Div(
                    Strong(
                        f"{snd_state['icon']} 현재 상태: {snd_state['title']}"
                        + (f" ({snd_state['filename']})" if snd_state["is_playing"] else ""),
                        style="color:#0284c7; display:block; margin-bottom:4px;",
                    ),
                    Span(snd_state["reason"], style="font-size:0.76rem; color:#334155;"),
                ),
                cls="sound-status-callout",
            ),
            Div(
                Button(
                    "🤖 현재 배경에 맞는 소리 AI 분석 & 재생",
                    type="button",
                    cls="success",
                    hx_post="/api/sound/match-bg",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    title="Gemini Vision API로 현재 창문 배경 이미지를 분석하여 가장 잘 어울리는 mp3를 즉시 틀어줍니다.",
                ),
                Button(
                    f"🔄 배경 변경 시 자동 맞춤 소리: {'ON' if snd_state['auto_match_bg'] else 'OFF'}",
                    type="button",
                    cls="success" if snd_state["auto_match_bg"] else "secondary",
                    hx_post="/api/sound/auto-bg",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    title="켜두면 배경 풍경이 바뀔 때마다 AI가 새 배경에 어울리는 mp3로 자동 전환합니다.",
                ),
                Button(
                    "⏹️ 소리 끄기",
                    type="button",
                    cls="secondary",
                    hx_post="/api/sound/stop",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                ),
                cls="sound-action-row",
            ),
            Form(
                Div(
                    Span(
                        f"🔉 사운드 볼륨 ({snd_state['volume_pct']}%)",
                        id="sound-vol-disp",
                        cls="slider-label",
                    ),
                    Input(
                        type="range",
                        name="volume_pct",
                        min="0",
                        max="100",
                        step="5",
                        value=str(snd_state["volume_pct"]),
                        oninput=(
                            "document.getElementById('sound-vol-disp').textContent='🔉 사운드 볼륨 ('+this.value+'%)';"
                            "if(window.__windoAmbientAudio){window.__windoAmbientAudio.volume=this.value/100.0;}"
                        ),
                        cls="custom-range-slider",
                    ),
                    cls="slider-row",
                ),
                hx_post="/api/sound/volume",
                hx_trigger="change",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
                cls="custom-sliders-box",
            ),
            Div(
                Span(
                    f"🎵 mp3 폴더 사운드 목록 ({len(sound_tracks)}개 — 클릭하거나 \"~ 들려줘\"라고 말씀하세요)",
                    cls="field-label",
                    style="margin-bottom:6px; display:block;",
                ),
                Div(*sound_track_buttons, cls="preset-btn-grid"),
            ),
            id="sec-card-3",
            cls="option-card selected",
        ),
        # =====================================================================
        # 4. 실내 공기질 입력 (IAQ 임의값 입력 & AI 위험도 자동 판단)
        # =====================================================================
        Div(
            Div(
                Span("4. 실내 공기질 입력 · AI 위험도 판단"),
                Span(iaq_state.grade_label, cls=f"hud-iaq-badge {iaq_state.badge_cls}"),
                cls="option-card-title",
            ),
            Div(
                Span("🤖 AI 판단", cls="iaq-status-head"),
                Span(iaq_state.action_message, cls="iaq-status-msg"),
                cls=f"iaq-status-box {'danger' if iaq_state.is_dangerous else ''}",
            ),
            Form(
                Div(
                    _iaq_field("초미세먼지 PM2.5", "㎍/㎥", "pm25", iaq_state.pm25, "0", "999"),
                    _iaq_field("미세먼지 PM10", "㎍/㎥", "pm10", iaq_state.pm10, "0", "999"),
                    _iaq_field("이산화탄소 CO₂", "ppm", "co2", iaq_state.co2_ppm, "300", "5000"),
                    _iaq_field("유해가스 TVOC", "ppb", "tvoc", iaq_state.tvoc_ppb, "0", "5000"),
                    cls="iaq-input-grid",
                ),
                Button("입력값 적용 & AI 판단", type="submit", cls="success", style="width:100%;"),
                hx_post="/api/iaq/update",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
                cls="control-group",
            ),
            Div(
                Span("빠른 테스트", cls="field-label"),
                Div(
                    _iaq_quick("🟢 쾌적", "pm25=11&pm10=24&co2=460&tvoc=95"),
                    _iaq_quick("🟠 미세먼지 나쁨", "pm25=55&pm10=110&co2=650&tvoc=180"),
                    _iaq_quick("🟡 CO₂ 높음", "pm25=14&pm10=28&co2=1350&tvoc=150"),
                    _iaq_quick("🚨 매우 위험", "pm25=120&pm10=210&co2=1800&tvoc=650"),
                    cls="quick-btn-grid",
                ),
                cls="control-group",
                style="gap:6px;",
            ),
            P(
                "기준: PM2.5 36↑ / PM10 81↑ / CO₂ 1001ppm↑ / TVOC 501ppb↑ 이면 위험으로 판단하고 공기청정기·환기를 작동시킵니다.",
                cls="option-card-desc",
            ),
            id="sec-card-4",
            cls="option-card",
        ),
        # =====================================================================
        # 5. ComfyUI (풍경 생성 · 프롬프트/모델/LoRA 설정 · 하단 실시간 작동 모니터)
        # =====================================================================
        Div(
            Div(
                Span("5. ComfyUI (배경 생성 · 설정 · 실시간 작동 모니터)"),
                Span(
                    f"🔵 작동 중 {s_state.generation_progress_pct}%"
                    if s_state.is_generating
                    else ("✅ 완료 100%" if s_state.generation_progress_pct == 100 else "⚪ 대기 중"),
                    cls="card-status-pill",
                ),
                cls="option-card-title",
            ),
            P(
                "\"~ 만들어줘\"라고 말하거나 아래 생성창에 원하는 풍경을 입력해 ComfyUI를 실행하세요. "
                "설정 칸 바로 아래에서 ComfyUI 7-노드 파이프라인이 실시간으로 작동하는 모습을 확인할 수 있습니다.",
                cls="option-card-desc",
            ),
            Form(
                Div(
                    Input(
                        type="text",
                        name="gen_prompt",
                        placeholder="생성할 창문 풍경 입력 (예: 눈 내리는 스위스 설산, 비 오는 도쿄 밤거리)",
                        required=True,
                    ),
                    Button("🎨 ComfyUI 생성", type="submit", cls="success"),
                    cls="inline-input-row",
                ),
                hx_post="/api/comfy/generate",
                hx_swap="none",
                hx_on__after_request="this.reset()",
                cls="control-group",
            ),
            Form(
                Div(
                    Div(
                        Span("📦 베이스 모델 (Checkpoint / Model)", cls="comfy-field-label"),
                        Select(*model_options, name="model_name"),
                    ),
                    Div(
                        Span("🎨 LoRA 파일 (loras/ 폴더)", cls="comfy-field-label"),
                        Select(*lora_options, name="lora_name"),
                    ),
                    cls="comfy-grid-2col",
                ),
                Div(
                    Div(
                        Span(f"⚡ LoRA 강도 (현재: {cfg.lora_strength:.2f})", cls="comfy-field-label"),
                        Input(
                            type="number",
                            name="lora_strength",
                            value=str(cfg.lora_strength),
                            step="0.05",
                            min="0.0",
                            max="2.0",
                        ),
                    ),
                    Div(
                        Span("🔑 LoRA 트리거 워드", cls="comfy-field-label"),
                        Input(
                            type="text",
                            name="lora_trigger",
                            value=cfg.lora_trigger,
                            placeholder="예: best landscape",
                        ),
                    ),
                    cls="comfy-grid-2col",
                ),
                Div(
                    Span("➕ 긍정 프롬프트 (Positive Prompt — CLIPTextEncode +)", cls="comfy-field-label"),
                    Textarea(
                        cfg.positive_prompt,
                        name="positive_prompt",
                        rows="3",
                        placeholder="생성할 이미지에 반드시 포함할 고화질/스타일 키워드 입력...",
                        cls="comfy-prompt-textarea",
                    ),
                ),
                Div(
                    Span("➖ 부정 프롬프트 (Negative Prompt — CLIPTextEncode -)", cls="comfy-field-label"),
                    Textarea(
                        cfg.negative_prompt,
                        name="negative_prompt",
                        rows="3",
                        placeholder="생성할 이미지에서 배제할 요소(흐릿함, 왜곡, 글자, 실내 창틀 등) 입력...",
                        cls="comfy-prompt-textarea comfy-prompt-negative",
                    ),
                ),
                Div(
                    Div(
                        Span("Steps", cls="comfy-field-label"),
                        Input(type="number", name="steps", value=str(cfg.steps), min="1", max="20"),
                    ),
                    Div(
                        Span("CFG (부정 배제력)", cls="comfy-field-label"),
                        Input(type="number", name="cfg_scale", value=str(cfg.cfg_scale), step="0.5", min="1.0", max="10.0"),
                    ),
                    Div(
                        Span("Sampler", cls="comfy-field-label"),
                        Select(*sampler_options, name="sampler_name"),
                    ),
                    Div(
                        Span("Scheduler", cls="comfy-field-label"),
                        Select(*scheduler_options, name="scheduler"),
                    ),
                    cls="comfy-grid-4col",
                ),
                Button("💾 ComfyUI 프롬프트 · 모델 · LoRA 설정 저장", type="submit", cls="secondary", style="width:100%;"),
                hx_post="/api/comfy/config",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
                cls="control-group",
            ),
            # [핵심 요청 반영] 기존 상단 로그에 붙이지 않고 5번 ComfyUI 칸 바로 아래에서 실시간으로 작동하는 모습을 보여줌!
            render_comfy_node_graph(s_state),
            id="sec-card-5",
            cls="option-card selected",
        ),

        # =====================================================================
        # 6. 구글 이미지 검색 ('~ 찾아줘' 웹 배경 선별 및 Searlo/Google API 설정)
        # =====================================================================
        Div(
            Div(
                Span("6. 구글 이미지 검색 & 웹 배경 찾기"),
                Span(
                    "🟢 Searlo 공식 API 연동 중" if web_search_mgr.config.searlo_key
                    else ("구글 API 연동" if (web_search_mgr.config.api_key and web_search_mgr.config.cx) else "스마트 웹 폴백"),
                    cls="card-status-pill",
                ),
                cls="option-card-title",
            ),
            P(
                "\"~ 찾아줘\"라고 말씀하시면(예: '도쿄 야경 찾아줘', '스위스 알프스 찾아줘') Searlo 공식 초고속 구글 이미지 엔진(1순위) 및 웹에서 고화질 16:9 와이드 배경 사진만 선별하여 창문 화면에 맞춰 즉시 적용합니다.",
                cls="option-card-desc",
            ),
            Form(
                Div(
                    Input(
                        type="text",
                        name="search_query",
                        placeholder="웹에서 찾을 배경 입력 (예: 발리 해변 노을, 파리 에펠탑 야경)",
                        required=True,
                    ),
                    Button("🌐 배경 찾아 적용", type="submit", cls="success"),
                    cls="inline-input-row",
                ),
                hx_post="/api/web-search/execute",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
                cls="control-group",
            ),
            Details(
                Summary("🔑 Searlo 공식 API & 구글 검색 API 설정 (클릭하여 열기)"),
                Form(
                    Div(
                        Div(
                            Span("Searlo 공식 API Key (최우선 메인 엔진: https://api.searlo.tech)", cls="field-label"),
                            Input(
                                type="text",
                                name="searlo_key",
                                value=web_search_mgr.config.searlo_key,
                                placeholder="sk_... (Searlo API Key)",
                            ),
                            style="grid-column: 1 / -1;",
                        ),
                        Div(
                            Span("SerpApi Key (선택: https://serpapi.com)", cls="field-label"),
                            Input(
                                type="text",
                                name="serpapi_key",
                                value=web_search_mgr.config.serpapi_key,
                                placeholder="SerpApi Secret Key",
                            ),
                            style="grid-column: 1 / -1;",
                        ),
                        Div(
                            Span("Google Cloud API Key (선택)", cls="field-label"),
                            Input(
                                type="text",
                                name="api_key",
                                value=web_search_mgr.config.api_key,
                                placeholder="AIzaSy... (API Key)",
                            ),
                        ),
                        Div(
                            Span("Custom Search Engine ID (CX, 선택)", cls="field-label"),
                            Input(
                                type="text",
                                name="cx",
                                value=web_search_mgr.config.cx,
                                placeholder="b3a7... (Search Engine CX)",
                            ),
                        ),
                        cls="comfy-grid-2col",
                    ),
                    Button("💾 Searlo 및 검색 API 설정 저장", type="submit", cls="secondary", style="width:100%; margin-top:6px;"),
                    hx_post="/api/web-search/config",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    cls="control-group",
                ),
                id="details-web-search-api",
                cls="comfy-json-details",
                style="margin-top:2px;",
            ),
            P(
                "🚀 Searlo 공식 API (sk_aad...)가 기본 탑재되어 실시간 고화질 와이드 이미지를 가장 빠르고 정확하게 찾아옵니다.",
                cls="option-card-desc",
                style="color: var(--ss-blue-dark); font-weight: 600;",
            ),
            id="sec-card-6",
            cls="option-card",
        ),
        # =====================================================================
        # 7. 현재 창문 사진 기준 타임랩스 만들기 (기존 이미지 1장 + 다른 시간대 이미지 2장 = 총 3장 완성 후 순환)
        # =====================================================================
        Div(
            Div(
                Span("7. 현재 창문 사진 기준 타임랩스 만들기"),
                Span(
                    f"⏳ 생성 중 (Step {max(1, tl_cur_step)}/3 · {tl_gen_pct}%)"
                    if tl_is_gen
                    else (
                        f"🟢 실행 중 ({tl_cur_stage_idx + 1}/3단계 · {tl_cur_speed:g}x)"
                        if ingredients_mgr.is_active
                        else f"⚪ 현재 창문: {s_state.active_title}"
                    ),
                    cls="card-status-pill",
                ),
                cls="option-card-title",
            ),
            P(
                "\"타임랩스 만들어줘\"라고 말하거나 아래 [🎬 타임랩스 만들기] 버튼을 누르면, "
                "현재 창문에 띄워져 있는 사진(1단계 원본 안바뀜)에 다른 시간대 이미지 2개(2단계·3단계)를 생성합니다. "
                "이미지 3개가 모두 완성되면 설정한 배속 간격마다 다음 사진으로 자동 전환됩니다.",
                cls="option-card-desc",
            ),
            Div(
                Button(
                    f"⏳ 타임랩스 이미지 3개 생성 중... ({tl_gen_pct}%)"
                    if tl_is_gen
                    else f"🎬 타임랩스 만들기 (현재 사진: {s_state.active_title})",
                    type="button",
                    cls="secondary" if tl_is_gen else "success",
                    hx_post="/api/timelapse/from-current",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                    title="현재 창문에 띄워져 있는 사진을 기준으로 다른 시간대 이미지 2개를 생성하여 3장이 모두 완성되면 타임랩스를 실행합니다.",
                ),
                Button(
                    "⏹️ 타임랩스 끄기",
                    type="button",
                    cls="secondary",
                    hx_post="/api/timelapse/stop",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                ),
                cls="timelapse-action-row",
            ),
            Div(
                Span(
                    f"⚡ 타임랩스 사진 전환 배속 설정 (현재: {tl_cur_speed:g}x 배속 · {tl_cur_interval:.0f}초마다 다음 사진)",
                    cls="timelapse-subbox-title",
                ),
                Div(
                    *tl_speed_buttons,
                    cls="timelapse-speed-grid",
                ),
                cls="timelapse-speed-box",
            ),
            Div(
                Div(
                    Span(
                        "🛠️ 타임랩스 이미지 3개 생성 과정 (3개 완성 시 자동 실행)"
                        if tl_is_gen
                        else (
                            f"✅ 타임랩스 3단계 완성 ({tl_bundle_dict.get('phase_order_ko', '1단계 ➔ 2단계 ➔ 3단계')})"
                            if tl_bundle_dict
                            else "🛠️ 타임랩스 이미지 생성 대기 중 (3개 완성 후 실행)"
                        ),
                        cls="timelapse-progress-head-txt",
                    ),
                    Span(
                        f"{tl_gen_pct}%",
                        cls="timelapse-progress-pct-txt",
                    ),
                    cls="timelapse-progress-head-row",
                ),
                Div(
                    Div(
                        cls="gen-progress-fill",
                        style=f"width: {tl_gen_pct}%;",
                    ),
                    cls="gen-progress-track",
                    style="margin-bottom:6px;",
                ),
                Div(
                    tl_gen_status,
                    cls="timelapse-progress-status-txt",
                ),
                Div(
                    *tl_step_cards,
                    cls="timelapse-steps-stack",
                ),
                cls="timelapse-progress-box",
            ),
            id="sec-card-7",
            cls="option-card selected",
        ),
        # =====================================================================
        # 8. 마이크 & 호출어 ('윈도', 'windo' 및 무응답 대기 시간 설정)
        # =====================================================================
        Div(
            Div(
                Span("8. 마이크 & 호출어"),
                Button(
                    "🔄 마이크 재탐색",
                    type="button",
                    cls="secondary",
                    hx_post="/api/mic/rescan",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                ),
                cls="option-card-title",
            ),
            Form(
                Select(
                    *mic_options,
                    name="device_index",
                    hx_post="/api/mic/select",
                    hx_target="#settings-controls",
                    hx_swap=CTRL_SWAP,
                ),
                cls="control-group",
            ),
            Div(*wake_badges, cls="wake-badge-row"),
            Form(
                Div(
                    Input(
                        type="text",
                        name="wake_words",
                        value=ww_joined,
                        placeholder="호출어 입력 (예: 윈도, windo)",
                    ),
                    Button("호출어 저장", type="submit"),
                    cls="inline-input-row",
                ),
                hx_post="/api/wake-words",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
                cls="control-group",
            ),
            Form(
                Div(
                    Span("⏱️ 호출 후 대기(초):", cls="timeout-label"),
                    Input(
                        type="number",
                        name="timeout_sec",
                        value=str(audio_ctrl.command_timeout_sec),
                        step="0.5",
                        min="1.0",
                        max="15.0",
                    ),
                    Button("시간 저장", type="submit"),
                    cls="timeout-setting-row",
                ),
                hx_post="/api/wake-words/timeout",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
                cls="control-group",
            ),
            Button(
                mode_btn_text,
                type="button",
                cls=mode_btn_cls,
                style="width:100%;",
                hx_post="/api/wake-words/toggle",
                hx_target="#settings-controls",
                hx_swap=CTRL_SWAP,
            ),
            id="sec-card-8",
            cls="option-card",
        ),
        id="settings-controls",
        cls="drawer-col",
        **extra_attrs,
    )


def render_settings_drawer_content(is_standalone: bool = False) -> FT:
    """설정창 내부 컨텐츠 (창문이 왼쪽으로 밀려나면서 오른쪽에 생기는 슬림 설정창)"""
    close_js = (
        "window.close();"
        if is_standalone
        else (
            "if(window.toggleWindoSettings){window.toggleWindoSettings(false);}else{"
            "const bd=document.getElementById('settings-backdrop');"
            "if(bd){bd.classList.remove('open');bd.style.setProperty('display','none','important');}"
            "document.body.classList.remove('settings-docked-open');"
            "const ws=document.getElementById('windo-main-workspace');if(ws)ws.classList.remove('settings-open');"
            "const btn=document.getElementById('floating-settings-btn');if(btn)btn.classList.remove('active');"
            "}"
        )
    )
    return Div(
        Header(
            Div(
                Span("WINDO", cls="drawer-logo"),
                Span("AI 가상 창문 설정", cls="drawer-title"),
                cls="drawer-brand",
            ),
            Div(
                Button(
                    "⚙️",
                    id="drawer-gear-close-btn",
                    type="button",
                    title="설정 톱니바퀴 눌러 설정창 닫기",
                    cls="drawer-gear-close-btn",
                    onclick=close_js,
                ),
                Button(
                    "✕ 닫기",
                    type="button",
                    cls="drawer-close-btn",
                    onclick=close_js,
                ),
                cls="drawer-header-actions",
            ),
            cls="drawer-header",
        ),
        Div(
            "✨ \"정보(켜·꺼)\" · \"~ 찾아줘\"(웹검색) · \"~ 만들어줘\"(AI생성) · \"~ 들려줘\"(사운드) · \"타임랩스 만들어줘\"",
            cls="samsung-blue-banner",
        ),
        Div(
            Button("🖥️ 로그", type="button", cls="quick-nav-chip log-chip", onclick="document.getElementById('sec-card-log')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("1. 위젯", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-1')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("2. 창문·채도", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-2')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("3. 사운드", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-3')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("4. 공기질", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-4')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("5. ComfyUI", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-5')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("6. 이미지검색", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-6')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("7. 타임랩스", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-7')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            Button("8. 마이크", type="button", cls="quick-nav-chip", onclick="document.getElementById('sec-card-8')?.scrollIntoView({behavior:'smooth', block:'start'})"),
            cls="drawer-quick-nav",
        ),
        Div(
            # [맨 위쪽] 실시간 음성 & 시스템 로그 모니터 + 직접 명령어 입력 (ComfyUI 작동 모니터는 5번 칸 아래로 분리됨)
            Div(
                Div(
                    Span("🖥️ 실시간 음성 & 시스템 로그 모니터"),
                    Button(
                        "로그 비우기",
                        type="button",
                        cls="secondary",
                        hx_post="/api/logs/clear",
                        hx_swap="none",
                    ),
                    cls="option-card-title",
                ),
                Form(
                    Div(
                        Input(
                            type="text",
                            name="sim_text",
                            placeholder="직접 명령어 입력 (예: 윈도 타임랩스 만들어줘 / 윈도 비 소리 들려줘)",
                        ),
                        Button("실행", type="submit"),
                        cls="inline-input-row",
                    ),
                    hx_post="/api/simulate",
                    hx_swap="none",
                    hx_on__after_request="this.reset()",
                    cls="control-group",
                ),
                Div(
                    Div(
                        Span("LIVE VOICE & SYSTEM EXECUTION LOG (마우스 휠 스크롤)", cls="terminal-title"),
                        cls="terminal-header",
                    ),
                    render_live_monitor(),
                    cls="terminal-wrapper terminal-wrapper-top",
                ),
                id="sec-card-log",
                cls="option-card top-log-card",
            ),

            # [1번 ~ 8번 순서대로 나열]
            render_settings_controls(),
            id="settings-drawer-scroll-body",
            cls="drawer-body",
        ),
        cls=f"settings-drawer {'standalone-drawer' if is_standalone else ''}",
        onclick="event.stopPropagation()",
    )


@rt("/settings")
def get_standalone_settings():
    """창문 화면 밖 별도 팝업 창으로 열리는 독립 설정창"""
    return (
        Title("WINDO | 설정 및 실시간 로그 모니터"),
        Main(
            render_settings_drawer_content(is_standalone=True),
            cls="standalone-settings-page",
        ),
    )


@rt("/api/system/reset")
def post_system_reset():
    """앱 재실행 또는 초기화 요청 시 기본 사진(default.png) 및 모든 기본 설정(코에룩스 OFF, 타임랩스 제거) 복원"""
    reset_all_to_initial_defaults(clear_timelapse_files=True)
    return Response(json.dumps({"ok": True, "active_key": "default"}), media_type="application/json")


@rt("/")
def get():
    global _LAST_POLL_SETTINGS_SIG
    # [핵심 요구사항] 창문 화면에 새로 들어가거나 다시 켤 때마다:
    # 1) 기본 사진(default.png)부터 시작
    # 2) 기존에 코에룩스를 켜고 껐더라도 안 켜진 상태(OFF)로 초기화
    # 3) 타임랩스를 돌렸어도 완전히 없애고 초기 기본 설정 유지
    reset_all_to_initial_defaults(clear_timelapse_files=True)
    _LAST_POLL_SETTINGS_SIG = compute_settings_sig()

    gear_toggle_js = (
        "if(window.toggleWindoSettings){window.toggleWindoSettings();}else{"
        "const bd=document.getElementById('settings-backdrop');"
        "if(bd){"
        "const openNow=!(bd.classList.contains('open')&&window.getComputedStyle(bd).display!=='none');"
        "bd.classList.toggle('open',openNow);"
        "bd.style.setProperty('display',openNow?'flex':'none','important');"
        "document.body.classList.toggle('settings-docked-open',openNow);"
        "const ws=document.getElementById('windo-main-workspace');if(ws)ws.classList.toggle('settings-open',openNow);"
        "this.classList.toggle('active',openNow);"
        "}"
        "}"
    )
    return (
        Title("WINDO | AI 가상 창문 (ComfyUI Edition)"),
        Main(
            Section(
                render_window_stage(),
                id="window-viewport-container",
                cls="pure-window-viewport",
                hx_get="/api/window/poll",
                hx_trigger="every 600ms",
                hx_vals="js:{sig: document.getElementById('window-scene-stage')?.dataset?.sig || ''}",
                hx_target="#window-scene-stage",
                hx_swap="outerHTML",
            ),
            Button(
                "⚙️",
                id="floating-settings-btn",
                type="button",
                title="설정창 열기 / 닫기 (한 번 더 누르면 닫힙니다)",
                cls="floating-settings-trigger",
                onclick=gear_toggle_js,
            ),
            Div(
                render_settings_drawer_content(is_standalone=False),
                id="settings-backdrop",
                cls="settings-backdrop",
                style="display:none;",
            ),
            id="windo-main-workspace",
            cls="windo-split-workspace",
        ),
    )


# -------------------------------------------------------------------------
# HTMX 비동기 API 엔드포인트
# -------------------------------------------------------------------------
@rt("/api/window/poll")
def get_window_poll(sig: str = ""):
    global _LAST_POLL_SETTINGS_SIG
    current_sig = compute_window_sig()
    if sig == current_sig:
        return Response(status_code=204)
    curr_settings_sig = compute_settings_sig()
    if curr_settings_sig != _LAST_POLL_SETTINGS_SIG:
        _LAST_POLL_SETTINGS_SIG = curr_settings_sig
        return render_window_stage(), render_settings_controls(oob=True)
    return render_window_stage()


@rt("/api/logs")
def get_logs(sig: str = "", logsig: str = ""):
    if sig and sig == compute_log_status_sig():
        return Response(status_code=204)
    s_state = landscape_mgr.get_state()
    parts = [
        render_live_status_poller(),
        render_comfy_live_progress_panel(s_state, oob=True),
        render_comfy_nodes_flow(s_state, oob=True),
    ]
    if logsig != compute_log_items_sig():
        parts.append(render_log_items_inner(oob=True))
    return tuple(parts)


@rt("/api/comfy/generate")
def post_comfy_generate(gen_prompt: str = ""):
    q = gen_prompt.strip()
    if not q:
        return Response(status_code=204)
    if not any(v in q for v in ("만들어줘", "만들어", "그려줘", "생성해줘")):
        q_cmd = f"윈도 {q} 만들어줘"
    else:
        q_cmd = f"윈도 {q}"
    audio_ctrl.simulate_voice_input(q_cmd)
    s_state = landscape_mgr.get_state()
    return (
        render_comfy_live_progress_panel(s_state, oob=True),
        render_comfy_nodes_flow(s_state, oob=True),
        render_window_stage(oob=True),
    )



@rt("/api/comfy/config")
def post_comfy_config(
    model_name: str = "unsloth/FLUX.1-schnell",
    lora_name: str = "landscape.safetensors",
    lora_strength: float = 0.95,
    lora_trigger: str = "best landscape",
    positive_prompt: str = "",
    negative_prompt: str = "",
    steps: int = 6,
    cfg_scale: float = 3.5,
    sampler_name: str = "euler",
    scheduler: str = "simple",
):
    cfg = landscape_mgr.update_comfy_config(
        model_name=model_name,
        lora_name=lora_name,
        lora_strength=lora_strength,
        lora_trigger=lora_trigger,
        positive_prompt=positive_prompt,
        negative_prompt=negative_prompt,
        steps=steps,
        cfg_scale=cfg_scale,
        sampler_name=sampler_name,
        scheduler=scheduler,
    )
    audio_ctrl.add_log(
        "SYSTEM",
        f"⚙️ ComfyUI 설정 저장 완료 — 모델: {cfg.model_name} | LoRA: {cfg.lora_name} ({cfg.lora_strength:.2f}) | Steps: {cfg.steps} | CFG: {cfg.cfg_scale}",
        f"Positive({len(cfg.positive_prompt)}자) · Negative({len(cfg.negative_prompt)}자) 반영됨",
    )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/hud/toggle")
def post_hud_toggle():
    now_vis = hud_mgr.toggle()
    audio_ctrl.add_log("SYSTEM", f"🕒 정보 창(시간·날씨·공기질) 전체 {'ON' if now_vis else 'OFF'}")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/hud/section")
def post_hud_section(key: str):
    now_on = hud_mgr.toggle_section(key)
    label = hud_mgr.SECTION_LABELS.get(key, key)
    audio_ctrl.add_log("SYSTEM", f"🕒 [{label}] 위젯 {'ON' if now_on else 'OFF'}")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/widgets/all")
def post_widgets_all(on: int = 1):
    hud_mgr.visible = bool(on)
    calendar_mgr.set_visible(bool(on))
    if on:
        hud_mgr.trigger_weather_refresh_async()
    audio_ctrl.add_log("SYSTEM", f"🪟 전체 위젯(시간·날씨·공기질·캘린더) {'ON' if on else 'OFF'}")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/iaq/update")
def post_iaq_update(pm25: int = 12, pm10: int = 26, co2: int = 540, tvoc: int = 110):
    iaq = hud_mgr.set_air_quality(pm25=pm25, pm10=pm10, co2_ppm=co2, tvoc_ppb=tvoc)
    audio_ctrl.add_log(
        "WARN" if iaq.is_dangerous else "SYSTEM",
        f"💨 실내 공기질 입력 → [{iaq.grade_label}] PM2.5 {iaq.pm25} · PM10 {iaq.pm10} · CO2 {iaq.co2_ppm}ppm · TVOC {iaq.tvoc_ppb}ppb",
        f"🤖 {iaq.action_message}",
    )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/calendar/toggle")
def post_calendar_toggle():
    now_vis = not calendar_mgr.visible
    calendar_mgr.set_visible(now_vis)
    audio_ctrl.add_log("SYSTEM", f"📅 구글 캘린더 {'ON' if now_vis else 'OFF'}")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/coelux/toggle")
def post_coelux_toggle():
    now_on = coelux_mgr.handle_command("TOGGLE_COELUX")
    if now_on:
        s_st = landscape_mgr.get_state()
        coelux_mgr.analyze_background_file(
            get_active_background_path(),
            scene_hint=f"{s_st.active_title} {s_st.active_key}",
        )
        p_info = coelux_mgr.get_placement_state()
        audio_ctrl.add_log(
            "SYSTEM",
            f"☀️ 가상 코에룩스(CoeLux) 실내 창문 채광 ON — {p_info['label_ko']} | 위치 (X:{p_info['x_pct']:.0f}%, Y:{p_info['y_pct']:.0f}%) | 색상 {p_info['active_color_hex']}",
        )
    else:
        audio_ctrl.add_log("SYSTEM", "☀️ 가상 코에룩스(CoeLux) 실내 창문 채광 OFF")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/coelux/update")
def post_coelux_update(
    intensity_pct: int = 100,
    color_hex: str = "",
    x_pct: float = 50.0,
    y_pct: float = 20.0,
):
    coelux_mgr.set_enabled(True)
    cur_hex = coelux_mgr.get_active_light_palettes()[3]
    color_changed = bool(color_hex.strip()) and (color_hex.strip().lower() != cur_hex.lower())
    pos_changed = (
        abs(float(x_pct) - coelux_mgr.light_x_pct) >= 0.8
        or abs(float(y_pct) - coelux_mgr.light_y_pct) >= 0.8
    )
    coelux_mgr.set_light_controls(
        intensity_pct=intensity_pct,
        color_hex=color_hex if color_changed else None,
        x_pct=x_pct if pos_changed else None,
        y_pct=y_pct if pos_changed else None,
    )
    p_info = coelux_mgr.get_placement_state()
    audio_ctrl.add_log(
        "SYSTEM",
        f"☀️ 실내 창문 빛 조절: 밝기 {p_info['intensity_pct']}% · 색상 {p_info['active_color_hex']} · 위치 (X:{p_info['x_pct']:.0f}%, Y:{p_info['y_pct']:.0f}%)",
    )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/coelux/color")
def post_coelux_color(hex_code: str = "#ffc86e"):
    decoded_hex = urllib.parse.unquote(hex_code).strip()
    coelux_mgr.set_enabled(True)
    coelux_mgr.set_light_controls(color_hex=decoded_hex)
    audio_ctrl.add_log("SYSTEM", f"🎨 실내 창문 빛 색상 변경: {decoded_hex}")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/coelux/ai-reset")
def post_coelux_ai_reset():
    coelux_mgr.set_enabled(True)
    coelux_mgr.set_light_controls(
        intensity_pct=100,
        use_ai_color=True,
        use_ai_position=True,
    )
    s_st = landscape_mgr.get_state()
    coelux_mgr.analyze_background_file(
        get_active_background_path(),
        scene_hint=f"{s_st.active_title} {s_st.active_key}",
    )
    p_info = coelux_mgr.get_placement_state()
    audio_ctrl.add_log(
        "SYSTEM",
        f"🤖 AI 빛 위치·색상 자동 분석 복원: {p_info['label_ko']} | 위치 (X:{p_info['x_pct']:.0f}%, Y:{p_info['y_pct']:.0f}%) | 색상 {p_info['active_color_hex']}",
    )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/calendar/nav")
def post_calendar_nav(delta: int = 0):
    calendar_mgr.change_month(delta)
    return render_window_stage()


@rt("/api/calendar/select")
def post_calendar_select(date_str: str):
    calendar_mgr.select_date(date_str)
    return render_window_stage()


@rt("/api/calendar/add")
def post_calendar_add(date_str: str = "", time_str: str = "", title: str = ""):
    ev = calendar_mgr.add_event(date_str=date_str, time_str=time_str, title=title)
    if ev:
        audio_ctrl.add_log("SYSTEM", f"📅 일정 추가: {ev.date_str} {ev.time_str} · {ev.title}")
    return render_window_stage()


@rt("/api/calendar/delete")
def post_calendar_delete(event_id: str):
    calendar_mgr.delete_event(urllib.parse.unquote(event_id))
    return render_window_stage()


@rt("/api/sound/play")
def post_sound_play(filename: str = ""):
    decoded_name = urllib.parse.unquote(filename).strip()
    tr = sound_mgr.play_track(
        decoded_name,
        reason=f"설정창에서 '{decoded_name}' 사운드를 직접 선택하여 재생합니다.",
        source_label="설정창 직접 선택",
    )
    if tr:
        audio_ctrl.add_log(
            "SYSTEM",
            f"🔊 사운드 수동 재생: [{tr.icon} {tr.title}] ({tr.filename})",
            f"볼륨 {sound_mgr.volume_pct}% · 무한 반복(Loop) 재생",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/sound/stop")
def post_sound_stop():
    sound_mgr.stop(reason="설정창에서 소리 재생을 중지했습니다.")
    audio_ctrl.add_log("SYSTEM", "🔇 사운드 재생 정지")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/sound/toggle")
def post_sound_toggle():
    if sound_mgr.is_playing:
        sound_mgr.stop(reason="위젯 토글로 소리를 껐습니다.")
        audio_ctrl.add_log("SYSTEM", "🔇 배경 맞춤 사운드 OFF")
    else:
        s_st = landscape_mgr.get_state()
        tr = sound_mgr.match_and_play_for_background(
            get_active_background_path(),
            scene_hint=f"{s_st.active_title} {s_st.active_key}",
            on_gemini_updated=lambda g_tr, g_rsn: audio_ctrl.add_log(
                "SYSTEM",
                f"🤖 Gemini Vision 배경 맞춤 사운드 확정: [{g_tr.icon} {g_tr.title}] ({g_tr.filename})",
                g_rsn,
            ),
        )
        if tr:
            st_info = sound_mgr.get_state()
            audio_ctrl.add_log(
                "SYSTEM",
                f"🔊 배경 맞춤 사운드 ON: [{tr.icon} {tr.title}] ({tr.filename})",
                st_info["reason"],
            )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/sound/match-bg")
def post_sound_match_bg():
    s_st = landscape_mgr.get_state()
    tr = sound_mgr.match_and_play_for_background(
        get_active_background_path(),
        scene_hint=f"{s_st.active_title} {s_st.active_key}",
        on_gemini_updated=lambda g_tr, g_rsn: audio_ctrl.add_log(
            "SYSTEM",
            f"🤖 Gemini Vision 배경 맞춤 사운드 확정: [{g_tr.icon} {g_tr.title}] ({g_tr.filename})",
            g_rsn,
        ),
    )
    if tr:
        st_info = sound_mgr.get_state()
        audio_ctrl.add_log(
            "SYSTEM",
            f"🤖 AI 배경 맞춤 사운드 분석 및 재생: [{tr.icon} {tr.title}] ({tr.filename})",
            f"대상 배경: {s_st.active_title} | {st_info['reason']}",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/sound/auto-bg")
def post_sound_auto_bg():
    now_auto = sound_mgr.toggle_auto_match_bg()
    if now_auto:
        s_st = landscape_mgr.get_state()
        tr = sound_mgr.match_and_play_for_background(
            get_active_background_path(),
            scene_hint=f"{s_st.active_title} {s_st.active_key}",
            on_gemini_updated=lambda g_tr, g_rsn: audio_ctrl.add_log(
                "SYSTEM",
                f"🤖 Gemini Vision 배경 맞춤 사운드 확정: [{g_tr.icon} {g_tr.title}] ({g_tr.filename})",
                g_rsn,
            ),
        )
        audio_ctrl.add_log(
            "SYSTEM",
            f"🔄 배경 변경 시 자동 맞춤 소리 ON" + (f" — 현재 배경 맞춤 재생: [{tr.icon} {tr.title}]" if tr else ""),
            "이제 배경이 바뀔 때마다 Gemini AI가 새 배경에 어울리는 mp3를 자동으로 찾아 재생합니다.",
        )
    else:
        audio_ctrl.add_log(
            "SYSTEM",
            "🔄 배경 변경 시 자동 맞춤 소리 OFF",
            "배경이 바뀌어도 현재 사운드 상태를 그대로 유지합니다.",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/sound/volume")
def post_sound_volume(volume_pct: int = 75):
    v = sound_mgr.set_volume(volume_pct)
    audio_ctrl.add_log("SYSTEM", f"🔉 사운드 볼륨 조절: {v}%")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/timelapse/toggle")
def post_timelapse_toggle():
    if ingredients_mgr.is_active or ingredients_mgr.is_generating:
        ingredients_mgr.set_active(False)
        audio_ctrl.add_log("SYSTEM", "🎬 타임랩스 OFF (일반 창문 모드로 복귀)")
    else:
        s_st = landscape_mgr.get_state()
        bg_path = get_active_background_path()
        started = ingredients_mgr.start_timelapse_generation_async(
            image_path=bg_path,
            title=s_st.active_title or "현재 창문 풍경",
            active_preset_key=s_st.active_key,
            try_gemini_api=True,
            log_callback=audio_ctrl.add_log,
        )
        if started:
            audio_ctrl.add_log(
                "SYSTEM",
                f"🎬 현재 창문 사진 [{s_st.active_title}] 기준 타임랩스 이미지 3개 생성 시작",
                "Step 1/3(원본 시간대 분석) ➔ Step 2/3(다음 시간대 생성) ➔ Step 3/3(다다음 시간대 생성) 완료 후 자동 실행됩니다.",
            )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/timelapse/create")
def post_timelapse_create():
    s_st = landscape_mgr.get_state()
    bg_path = get_active_background_path()
    started = ingredients_mgr.start_timelapse_generation_async(
        image_path=bg_path,
        title=s_st.active_title or "현재 창문 풍경",
        active_preset_key=s_st.active_key,
        try_gemini_api=True,
        log_callback=audio_ctrl.add_log,
    )
    if started:
        audio_ctrl.add_log(
            "SYSTEM",
            f"🎬 [타임랩스 만들기] 현재 창문 사진 [{s_st.active_title}] 기준 타임랩스 이미지 3개 생성 시작",
            "이미지 3개가 모두 완성되면 타임랩스가 자동으로 실행됩니다.",
        )
    return render_window_stage(), render_settings_controls(oob=True)


@rt("/api/timelapse/stop")
def post_timelapse_stop():
    ingredients_mgr.set_active(False)
    audio_ctrl.add_log("SYSTEM", "⏹️ 타임랩스 종료")
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/timelapse/from-current")
def post_timelapse_from_current():
    if ingredients_mgr.is_generating:
        audio_ctrl.add_log(
            "WARN",
            f"⏳ 이미 타임랩스 이미지 3개를 생성 중입니다 ({ingredients_mgr.generation_progress_pct}%)",
            "이미지 3개가 모두 완성되면 타임랩스가 자동으로 실행됩니다.",
        )
        return render_settings_controls(), render_window_stage(oob=True)
    s_st = landscape_mgr.get_state()
    bg_path = get_active_background_path()
    started = ingredients_mgr.start_timelapse_generation_async(
        image_path=bg_path,
        title=s_st.active_title or "현재 창문 풍경",
        active_preset_key=s_st.active_key,
        try_gemini_api=True,
        log_callback=audio_ctrl.add_log,
    )
    if started:
        audio_ctrl.add_log(
            "SYSTEM",
            f"🎬 [타임랩스 만들기] 현재 창문 사진 [{s_st.active_title}] 기준 타임랩스 이미지 3개 생성 시작",
            "Step 1/3 ➔ Step 2/3 ➔ Step 3/3 이미지 생성 완료 후 자동 실행됩니다.",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/timelapse/speed")
def post_timelapse_speed(speed: float = 1.0):
    sp, interval = ingredients_mgr.set_speed_multiplier(speed)
    audio_ctrl.add_log(
        "SYSTEM",
        f"⚡ 타임랩스 배속 설정 변경: {sp:g}x 배속 ({interval:.0f}초마다 다음 사진으로 전환)",
    )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/timelapse/stage")
def post_timelapse_stage(idx: int = 0):
    new_idx = ingredients_mgr.set_stage_index(idx)
    audio_ctrl.add_log(
        "SYSTEM",
        f"🎬 타임랩스 {new_idx + 1}단계 사진으로 즉시 이동",
    )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/scene/preset")
def post_scene_preset(key: str):
    decoded_key = urllib.parse.unquote(key)
    preset = landscape_mgr.switch_to_preset(decoded_key)
    if preset:
        ingredients_mgr.set_active(False)
        lighting_mgr.set_preset_override(preset.lighting_key)
        trigger_auto_bg_sound_if_enabled()
        l_state = lighting_mgr.get_current_state()
        audio_ctrl.add_log(
            "SYSTEM",
            f"🪟 프리셋 수동 선택: [{preset.title}] (색온도 {l_state.cct_kelvin}K · 채도 {l_state.saturation_pct}%)",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/scene/apply-cached")
def post_scene_apply_cached():
    applied_url = landscape_mgr.apply_latest_cached_landscape(label="최근 생성된 AI 풍경")
    if applied_url:
        ingredients_mgr.set_active(False)
        trigger_auto_bg_sound_if_enabled()
        l_state = lighting_mgr.get_current_state()
        audio_ctrl.add_log(
            "STT",
            f"🪟 생성된 AI 풍경 이미지를 창문에 즉시 적용했습니다! ({landscape_mgr.active_title})",
            f"채도 {l_state.saturation_pct}% · 밝기 {l_state.brightness_pct}% · 색온도 {l_state.cct_kelvin}K",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/lighting/auto")
def post_lighting_auto():
    now_auto = lighting_mgr.toggle_auto_mode()
    l_state = lighting_mgr.get_current_state()
    if now_auto:
        audio_ctrl.add_log(
            "SYSTEM",
            f"⏱️ 생체리듬 자동 광원 ON ({l_state.cct_kelvin}K · 채도 {l_state.saturation_pct}% 실시간 동기화)",
            lighting_mgr.FEATURE_SUMMARY,
        )
    else:
        audio_ctrl.add_log(
            "SYSTEM",
            "⏱️ 생체리듬 자동 광원 OFF (원본 풍경 무보정 100% 모드)",
            "시간대별 색온도 자동 보정이 해제되었습니다. 버튼을 다시 누르면 켜집니다.",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/lighting/custom")
def post_lighting_custom(saturation_pct: int = 110, brightness_pct: int = 100, cct_kelvin: int = 5800):
    l_state = lighting_mgr.set_custom_lighting(
        saturation_pct=saturation_pct,
        brightness_pct=brightness_pct,
        cct_kelvin=cct_kelvin,
    )
    audio_ctrl.add_log(
        "SYSTEM",
        f"🎛️ 사용자 직접 채도/광원 조절: 채도 {l_state.saturation_pct}% · 밝기 {l_state.brightness_pct}% · 색온도 {l_state.cct_kelvin}K",
        f"CSS Filter: {l_state.css_filter}",
    )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/scene/upload")
async def post_scene_upload(preset_key: str = "default", image_file: UploadFile = None, custom_name: str = ""):
    if image_file is None:
        return render_settings_controls(), render_window_stage(oob=True)
    content = await image_file.read()
    if content:
        applied_key, _ = landscape_mgr.save_uploaded_preset_image(
            preset_key=preset_key,
            filename=image_file.filename or "custom.jpg",
            content=content,
            custom_name=custom_name,
        )
        sync_custom_scene_keywords()
        preset = landscape_mgr.presets.get(applied_key)
        if preset:
            lighting_mgr.set_preset_override(preset.lighting_key)
        trigger_auto_bg_sound_if_enabled()
        if custom_name.strip():
            ext = Path(image_file.filename or "custom.jpg").suffix.lower() or ".jpg"
            audio_ctrl.add_log(
                "STT",
                f"🖼️ 새 배경 이미지 ['{applied_key}{ext}'] 등록 및 창문 즉시 적용 완료!",
                f"이제 '윈도 {applied_key.replace('_', ' ')}' 또는 '{applied_key.replace('_', ' ')}'이라고 말하면 바로 틀어집니다!",
            )
        else:
            audio_ctrl.add_log(
                "SYSTEM",
                f"🖼️ '{applied_key}' 배경 이미지가 새 파일({image_file.filename})로 교체 및 적용되었습니다.",
            )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/mic/select")
def post_mic_select(device_index: int):
    audio_ctrl.scan_and_bind_microphone(preferred_index=device_index)
    return render_settings_controls()


@rt("/api/mic/rescan")
def post_mic_rescan():
    audio_ctrl.scan_and_bind_microphone()
    return render_settings_controls()


@rt("/api/wake-words")
def post_wake_words(wake_words: str):
    audio_ctrl.set_wake_words_from_string(wake_words)
    return render_settings_controls()


@rt("/api/wake-words/timeout")
def post_wake_words_timeout(timeout_sec: float):
    audio_ctrl.set_command_timeout(timeout_sec)
    return render_settings_controls()


@rt("/api/wake-words/toggle")
def post_toggle_wake_word_mode():
    audio_ctrl.toggle_wake_word_mode()
    return render_settings_controls()


@rt("/api/web-search/config")
def post_web_search_config(api_key: str = "", cx: str = "", serpapi_key: str = "", searlo_key: str = ""):
    web_search_mgr.save_config(api_key=api_key, cx=cx, serpapi_key=serpapi_key, searlo_key=searlo_key)
    audio_ctrl.add_log(
        "SYSTEM",
        f"⚙️ Searlo 공식 검색 API 설정 저장 완료 (Searlo: {'연동 활성' if (searlo_key or web_search_mgr.config.searlo_key) else '미등록'})",
        "https://api.searlo.tech/api/v1/search/images 1순위 구글 이미지 실시간 수집 활성화",
    )
    return render_settings_controls()


@rt("/api/web-search/execute")
def post_web_search_execute(search_query: str = ""):
    q = search_query.strip()
    if not q:
        return render_settings_controls()

    def _worker(query_str: str) -> None:
        ok, msg, img_path, inferred_light = web_search_mgr.search_and_process_background(
            query=query_str,
            log_callback=audio_ctrl.add_log,
        )
        if ok and img_path and img_path.exists():
            stem = web_search_mgr._sanitize_filename(query_str)
            target_in_presets = landscape_mgr.presets_dir / f"{stem}.jpg"
            try:
                import shutil
                shutil.copy2(img_path, target_in_presets)
            except Exception:
                pass

            with landscape_mgr._lock:
                landscape_mgr.record_preset_source(stem, "web")
                landscape_mgr.presets[stem] = landscape_mgr.build_custom_preset(stem, source_type="web")

            AudioController.register_custom_scene_keyword(stem)
            sync_custom_scene_keywords()
            landscape_mgr.switch_to_preset(stem)
            lighting_mgr.set_preset_override(inferred_light)
            trigger_auto_bg_sound_if_enabled()

            l_state = lighting_mgr.get_current_state()
            audio_ctrl.add_log(
                "STT",
                f"🌐 웹 배경 선별 및 화면 적용 완료: [{query_str}] — 네, 알겠습니다.",
                f"1920x1080 맞춤 최적화 · 광원: {l_state.active_label} ({l_state.cct_kelvin}K)",
            )
            audio_ctrl.speak_ack()
        else:
            audio_ctrl.add_log("WARN", f"❌ 웹 이미지 검색 실패: {msg}")

    audio_ctrl.add_log("SYSTEM", f"🔍 웹 이미지 검색 시작: \"{q}\" (배경 사진 정밀 선별) — 네, 알겠습니다.")
    audio_ctrl.speak_ack()
    threading.Thread(target=_worker, args=(q,), name="WebImageSearchThread", daemon=True).start()
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/simulate")
def post_simulate(sim_text: str):
    audio_ctrl.simulate_voice_input(sim_text)
    return render_settings_controls(oob=True), render_window_stage(oob=True)


@rt("/api/logs/clear")
def post_clear_logs():
    audio_ctrl.clear_logs()
    return render_log_items_inner(oob=True)


@rt("/ppt")
def get_ppt_guide():
    ppt_html_path = BASE_DIR / "PPT_발표자료_가이드.html"
    if ppt_html_path.exists():
        return HTMLResponse(ppt_html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>PPT 가이드 파일을 찾을 수 없습니다.</h1>", status_code=404)



# [핵심 404 방지] FastHTML의 fast_app()이 기본 등록하는 와일드카드 정적 라우트('/{fname:path}.{ext:static}')가
# app.routes[0]에 있으면 '/cache/ingredients/{bundle_id}/morning.jpg' 요청을 가로채어
# 점('.') 없는 'cache/ingredients/...' 경로를 찾다가 404 Not Found가 발생합니다.
# 따라서 커스텀 라우트(@rt)가 항상 먼저 매칭되도록 기본 정적 라우트를 맨 뒤로 재배치합니다.
for _r_idx, _r in enumerate(list(app.routes)):
    if getattr(_r, "path", "") == "/{fname:path}.{ext:static}":
        app.routes.append(app.routes.pop(_r_idx))
        break


if __name__ == "__main__":
    import uvicorn
    import webbrowser

    audio_ctrl.start_listening()

    if os.environ.get("WINDO_AUTO_BROWSER") == "1":
        def _open_browser_once() -> None:
            time.sleep(1.5)
            webbrowser.open("http://127.0.0.1:5001")

        threading.Thread(target=_open_browser_once, daemon=True).start()

    uvicorn.run(app, host="127.0.0.1", port=5001, reload=False)
