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

from src.modules.audio_controller import AudioController
from src.modules.circadian_lighting import CircadianLightingManager
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
_LAST_RENDERED_SCENE_VERSION: int = landscape_mgr.get_state().version


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
        audio_ctrl.add_log("SYSTEM", "☀️ 생체 리듬 자동 자연광 모드로 전환되었습니다.")

    # 사용자가 단순히 "만들어줘", "생성된 풍경 적용해줘"라고만 한 경우
    compact_text = text.replace(" ", "")
    if compact_text in ("만들어줘", "만들어", "생성해줘", "적용해줘", "방금만든배경", "방금만든풍경", "ai풍경", "ai배경"):
        if landscape_mgr.is_generating:
            audio_ctrl.speak_generating()
            return
        applied_url = landscape_mgr.apply_latest_cached_landscape(label="최근 생성된 AI 풍경")
        if applied_url:
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
            if matched_light_key != "custom":
                lighting_mgr.set_preset_override(preset.lighting_key)
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
                    if stem not in landscape_mgr.presets:
                        landscape_mgr.presets[stem] = landscape_mgr.build_custom_preset(stem, is_ai=False)
                    landscape_mgr.switch_to_preset(stem)

                AudioController.register_custom_scene_keyword(stem)
                sync_custom_scene_keywords()

                if matched_light_key != "custom":
                    lighting_mgr.set_preset_override(inferred_light)

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
        # [핵심 요구사항] '~ 만들어줘', '~ 그려줘', '~ 찾아줘' 또는 등록된 프리셋 이름이 아닌 일반 발화에는 반응하지 않음
        audio_ctrl.add_log(
            "SYSTEM",
            f"ℹ️ 일반 발화 무시 (화면 유지): \"{text}\"",
            "이미지 생성은 '~ 만들어줘'/'~ 그려줘', 웹 이미지 검색은 '~ 찾아줘', 프리셋 전환은 등록된 이름('아침', '노을', '저녁', '야경', '최애' 등)에만 반응합니다.",
        )


audio_ctrl = AudioController(
    initial_wake_words=["윈도", "windo"],
    on_command_callback=handle_voice_command,
)
sync_custom_scene_keywords()

app, rt = fast_app(
    hdrs=(Link(rel="stylesheet", href="/assets/css/style.css?v=12"),),
)

IMAGE_FIT_MODE: str = "fill"


# -------------------------------------------------------------------------
# 정적 파일 라우트 (CSS 및 프리셋/캐시 생성 이미지 서빙 - 한글 파일명 완벽 지원)
# -------------------------------------------------------------------------
@rt("/assets/css/style.css")
def get_css():
    return FileResponse(CSS_PATH, media_type="text/css")


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
    month_weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(year, month)
    event_colors = calendar_mgr.get_month_event_colors(year, month)

    day_cells = []
    for week in month_weeks:
        for col_idx, d in enumerate(week):
            if d == 0:
                day_cells.append(Div(cls="gcal-day-cell day-empty"))
                continue
            date_str = f"{year:04d}-{month:02d}-{d:02d}"
            cls_parts = ["gcal-day-cell"]
            if col_idx == 0:
                cls_parts.append("day-sun")
            elif col_idx == 6:
                cls_parts.append("day-sat")
            if date_str == today_str:
                cls_parts.append("day-today")
            if date_str == selected_date:
                cls_parts.append("day-selected")
            dots = [
                Span(cls=f"gcal-dot dot-{c if c in GCAL_COLOR_CLASSES else 'blue'}")
                for c in event_colors.get(d, [])[:3]
            ]
            dots_container = Div(*dots, cls="gcal-event-dots") if dots else Div(cls="gcal-event-dots empty")
            day_cells.append(
                Button(
                    Span(str(d), cls="gcal-day-num"),
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
            Span(f"📌 {selected_date}"),
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
    return (
        f"{s_state.version}-{int(s_state.is_generating)}-{s_state.generation_progress_pct}-"
        f"{l_state.active_preset_key}-{l_state.cct_kelvin}-{l_state.saturation_pct}-"
        f"{l_state.brightness_pct}-{l_state.contrast_pct}-{IMAGE_FIT_MODE}-{hud_sig}-{cal_sig}"
    )


def render_window_stage(oob: bool = False) -> FT:
    global _LAST_RENDERED_SCENE_VERSION
    l_state = lighting_mgr.get_current_state()
    s_state = landscape_mgr.get_state()
    state_sig = compute_window_sig()
    img_inline_style = (
        f"width:100vw; height:100vh; object-fit:{IMAGE_FIT_MODE}; "
        "object-position:center center; display:block;"
    )

    loading_badge = (
        Div(
            Div(cls="window-loading-spinner"),
            Span(f"ComfyUI 풍경 생성 중 ({s_state.generation_progress_pct}%) · {s_state.generation_status_text}"),
            Div(Span(), Span(), Span(), cls="window-loading-dots"),
            cls="window-loading-badge",
        )
        if s_state.is_generating
        else ""
    )

    extra_attrs = {"hx_swap_oob": "true"} if oob else {}
    # [핵심 보장] 실제 배경 버전(s_state.version)이 바뀐 순간에만 크로스페이드를 실행하고,
    # 이미지 생성 중이거나 HUD('정보 보여줘') 토글/시간 갱신 시에는 현재 화면을 100% 고정하여 깜빡임을 차단합니다.
    scene_changed = (s_state.version != _LAST_RENDERED_SCENE_VERSION) and (not s_state.is_generating)
    _LAST_RENDERED_SCENE_VERSION = s_state.version
    prev_img_url = s_state.previous_image_url if scene_changed else s_state.current_image_url
    curr_layer_style = "" if scene_changed else "animation:none; opacity:1;"

    hud_el = render_smart_window_hud()
    cal_el = render_smart_window_calendar()
    # 정보 창과 캘린더를 하나의 반응형 도크에 배치 → 정보 창이 꺼지면 캘린더가 자동으로 왼쪽으로 붙고,
    # 창 크기가 작아지면 자동으로 아래로 줄바꿈/축소됩니다.
    overlay_dock = Div(hud_el, cal_el, cls="smart-overlay-dock") if (hud_el or cal_el) else ""

    return Div(
        Div(
            Div(
                Img(src=prev_img_url, cls="window-layer-img", style=img_inline_style, alt=""),
                cls="window-layer window-layer-prev",
            ),
            Div(
                Img(src=s_state.current_image_url, cls="window-layer-img", style=img_inline_style, alt=""),
                key=f"scene-v{s_state.version}-{IMAGE_FIT_MODE}",
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
                style=f"background: radial-gradient(circle at 70% 25%, {l_state.rgb_hex}, transparent 65%);",
            ),
            id="window-filter-inner",
            cls="window-scene-stage",
            style=f"filter: {l_state.css_filter};",
        ),
        overlay_dock,
        loading_badge,
        id="window-scene-stage",
        cls="window-scene-stage",
        data_sig=state_sig,
        **extra_attrs,
    )


# -------------------------------------------------------------------------
# 2. ComfyUI 실시간 노드 실행 그래프 뷰어 & 회색 톤 LOG 모니터
# -------------------------------------------------------------------------
def render_comfy_node_graph(s_state: LandscapeState) -> FT:
    """설정을 열면 볼 수 있는 실시간 ComfyUI 7-노드 파이프라인 실행 모니터"""
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

    engine_badge = (
        "🟢 ComfyUI Node Graph Executing..."
        if s_state.is_generating
        else "⚪ ComfyUI Ready (1개씩 순차 생성 보장)"
    )

    return Div(
        Div(
            Span("🔗 ComfyUI 실시간 노드 실행 그래프 (7-Node Pipeline)"),
            Span(engine_badge),
            cls="comfy-canvas-header",
        ),
        Div(*node_cards, cls="comfy-nodes-flow"),
        Details(
            Summary("📄 ComfyUI API Workflow JSON 보기 (클릭하여 펼치기)"),
            Pre(s_state.workflow_json_str, cls="comfy-json-pre"),
            cls="comfy-json-details",
        ),
        cls="comfy-graph-canvas",
    )


def render_live_monitor() -> FT:
    """설정 모달 우측의 ComfyUI 실시간 노드 그래프 + LOG 창 (600ms 자동 갱신)"""
    logs = audio_ctrl.get_logs(limit=35)
    s_state = landscape_mgr.get_state()
    state_cls = f"status-badge status-{audio_ctrl.status_state}"

    last_cmd = audio_ctrl.last_recognized_command or "\"윈도\"라고 부른 뒤 \"저녁\", \"야경\", \"겨울 풍경 만들어줘\" 등을 말씀해 보세요"
    last_raw = audio_ctrl.last_raw_speech or "아직 감지된 음성이 없습니다."

    if s_state.is_generating:
        gen_badge_cls = "status-badge status-transcribing"
        gen_badge_txt = f"🔵 ComfyUI 노드 실행 중 ({s_state.generation_progress_pct}%)"
    elif s_state.generation_error_text:
        gen_badge_cls = "status-badge status-error"
        gen_badge_txt = "❌ 생성 오류 발생"
    elif s_state.generation_progress_pct == 100:
        gen_badge_cls = "status-badge status-listening"
        gen_badge_txt = "✅ 생성 완료 (100%)"
    else:
        gen_badge_cls = "status-badge status-idle"
        gen_badge_txt = "⚪ 생성 대기 중"

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
        ),
        # 실시간 AI 이미지 생성 진행률(%) & ComfyUI 노드 실행 그래프
        Div(
            Div(
                Span("🎨 ComfyUI 풍경 생성 상태: ", cls="highlight-label"),
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
            Div(s_state.generation_status_text, cls="gen-progress-desc"),
            Div(
                f"⚠️ 오류 상세: {s_state.generation_error_text}",
                cls="gen-progress-error",
            )
            if s_state.generation_error_text
            else "",
            render_comfy_node_graph(s_state),
            cls="gen-progress-panel",
        ),
        Div(*log_items, cls="terminal-body"),
        id="live-log-container",
        cls="live-log-container",
        hx_get="/api/logs",
        hx_trigger="every 600ms",
        hx_swap="outerHTML",
    )


def render_settings_controls(oob: bool = False) -> FT:
    """설정 모달 좌측 제어 카드들 (자연광/채도 + ComfyUI 긍정/부정 프롬프트/모델/LoRA 설정 + 이름 지정 이미지 추가 + 마이크)"""
    sync_custom_scene_keywords()
    l_state = lighting_mgr.get_current_state()
    s_state = landscape_mgr.get_state()
    cfg = landscape_mgr.comfy_config

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
                hx_swap="outerHTML",
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
        "const st=document.getElementById('window-filter-inner');"
        "if(st){st.style.filter=`saturate(${(s/100).toFixed(2)}) brightness(${(b/100).toFixed(2)}) contrast(1.02)`;}"
    )

    extra_attrs = {"hx_swap_oob": "true"} if oob else {}
    hud_vis, hud_time, _, hud_date_kor, _, hud_weather, iaq_state = hud_mgr.get_live_data()
    cal_vis = calendar_mgr.visible
    all_on = all(hud_mgr.sections.values()) and cal_vis

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
            hx_swap="outerHTML",
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
            hx_swap="outerHTML",
        )

    return Div(
        # 카드 1: 화면 정보 위젯 (전체 = 정보 / 상세 = 시간·날씨·공기질 / 캘린더)
        Div(
            Div(
                Span("1. 화면 정보 위젯"),
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
                    hx_swap="outerHTML",
                ),
                Button(
                    "전체 끄기",
                    type="button",
                    cls="secondary",
                    hx_post="/api/widgets/all?on=0",
                    hx_target="#settings-controls",
                    hx_swap="outerHTML",
                ),
                cls="btn-row-2",
            ),
            Div(
                _widget_toggle("🕒", "시간 · 날짜", "시간 / 날짜 (꺼)", hud_mgr.sections["time"], "/api/hud/section?key=time"),
                _widget_toggle("🌤️", "날씨 · 기온", "날씨 / 기온 (꺼)", hud_mgr.sections["weather"], "/api/hud/section?key=weather"),
                _widget_toggle("💨", "실내 공기질", "공기질 / 미세먼지 (꺼)", hud_mgr.sections["iaq"], "/api/hud/section?key=iaq"),
                _widget_toggle("📅", "구글 캘린더", "캘린더 (꺼)", cal_vis, "/api/calendar/toggle"),
                cls="widget-toggle-grid",
            ),
            cls="option-card selected",
        ),
        # 카드 2: 실내 공기질(IAQ) 임의값 입력 & AI 위험도 자동 판단
        Div(
            Div(
                Span("2. 실내 공기질 입력 · AI 위험도 판단"),
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
                hx_swap="outerHTML",
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
            cls="option-card",
        ),
        # 카드 3: 창문 풍경 & 채도·밝기·색온도 상태 및 직접 조절
        Div(
            Div(
                Span("3. 창문 풍경 & 자연광 채도"),
                Button(
                    "⏱️ 생체리듬 자동 광원",
                    type="button",
                    cls="success" if l_state.auto_mode else "secondary",
                    hx_post="/api/lighting/auto",
                    hx_target="#settings-controls",
                    hx_swap="outerHTML",
                ),
                cls="option-card-title",
            ),
            P(
                f"현재 풍경: {s_state.active_title} | 광원 모드: {l_state.active_label}",
                cls="option-card-desc",
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
                    Strong(f"{l_state.contrast_pct}%", cls="metric-value"),
                    cls="metric-box",
                ),
                cls="lighting-metrics-grid",
            ),
            Div(*preset_buttons, cls="preset-btn-grid"),
            Form(
                Div(
                    Span("🎛️ 채도 · 밝기 · 색온도 직접 조절 (슬라이더를 움직이면 즉시 반영됩니다)"),
                    cls="custom-slider-title",
                ),
                Div(
                    Span("채도 (0~250%)", cls="slider-label"),
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
                    Span("밝기 (30~180%)", cls="slider-label"),
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
                    Span("색온도 (2000~9000K)", cls="slider-label"),
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
                hx_swap="outerHTML",
                cls="custom-sliders-box",
            ),
            cls="option-card selected",
        ),
        # 카드 2: ComfyUI 워크플로우 설정 (긍정 프롬프트 / 부정 프롬프트 / 모델 설정 / LoRA 설정 / 샘플러)
        Div(
            Div(
                Span("4. ComfyUI 설정 (프롬프트 · 모델 · LoRA)"),
                cls="option-card-title",
            ),
            P(
                "ComfyUI 노드 그래프에 들어갈 베이스 모델, LoRA 파일/강도, 긍정(Positive) 및 부정(Negative) 프롬프트를 직접 설정합니다.",
                cls="option-card-desc",
            ),
            Form(
                # 모델 설정 & LoRA 파일 설정
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
                # LoRA 강도 & 트리거 워드
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
                # 긍정 프롬프트 (Positive Prompt)
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
                # 부정 프롬프트 (Negative Prompt)
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
                # KSampler 상세 설정 (Steps / CFG Scale / Sampler / Scheduler)
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
                Button("💾 ComfyUI 프롬프트 · 모델 · LoRA 설정 저장", type="submit", cls="success", style="width:100%;"),
                hx_post="/api/comfy/config",
                hx_target="#settings-controls",
                hx_swap="outerHTML",
                cls="control-group",
            ),
            cls="option-card selected",
        ),
        # 카드 5: 구글 이미지 검색 & 웹 배경 찾기 ('찾아줘' 트리거 및 API 설정)
        Div(
            Div(
                Span("5. 구글 이미지 검색 & 웹 배경 찾기"),
                Span("구글 API 연동" if (web_search_mgr.config.api_key and web_search_mgr.config.cx) else "스마트 웹 폴백", cls="card-status-pill"),
                cls="option-card-title",
            ),
            P(
                "\"~ 찾아줘\"라고 말씀하시면(예: '도쿄 야경 찾아줘', '스위스 알프스 찾아줘') 구글/웹에서 고화질 16:9 와이드 배경 사진만 선별하여 창문 화면에 맞춰 즉시 적용합니다.",
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
                hx_swap="outerHTML",
                cls="control-group",
            ),
            Details(
                Summary("🔑 Google Custom Search API Key & CX 설정 (클릭하여 열기)"),
                Form(
                    Div(
                        Div(
                            Span("Google Cloud API Key", cls="field-label"),
                            Input(
                                type="text",
                                name="api_key",
                                value=web_search_mgr.config.api_key,
                                placeholder="AIzaSy... (API Key)",
                            ),
                        ),
                        Div(
                            Span("Custom Search Engine ID (CX)", cls="field-label"),
                            Input(
                                type="text",
                                name="cx",
                                value=web_search_mgr.config.cx,
                                placeholder="b3a7... (Search Engine CX)",
                            ),
                        ),
                        cls="comfy-grid-2col",
                    ),
                    Button("💾 구글 검색 API 키 저장", type="submit", cls="secondary", style="width:100%; margin-top:6px;"),
                    hx_post="/api/web-search/config",
                    hx_target="#settings-controls",
                    hx_swap="outerHTML",
                    cls="control-group",
                ),
                cls="comfy-json-details",
                style="margin-top:2px;",
            ),
            P(
                "💡 API 키를 등록하지 않아도 고화질 스마트 웹 풍경 검색 폴백(Wikimedia/Unsplash)으로 바로 찾을 수 있습니다.",
                cls="option-card-desc",
                style="color: var(--ss-blue-dark); font-weight: 600;",
            ),
            cls="option-card",
        ),
        # 카드 6: 이름 지정 배경 이미지 추가 (예: '최애' -> 최애.jpg & "윈도 최애" 음성 호출) 및 기존 배경 교체
        Div(
            Div(Span("6. 배경 이미지 추가 · 교체"), cls="option-card-title"),
            P(
                "이름(예: 최애)을 입력하고 이미지를 추가하면 '최애.jpg'로 저장되며, 저녁·노을처럼 \"윈도 최애\"라고 부르면 바로 틀어집니다! (이름을 비워두면 아래 선택된 기존 배경을 교체합니다)",
                cls="option-card-desc",
            ),
            Form(
                Input(
                    type="text",
                    name="custom_name",
                    placeholder="새로 추가할 배경 이름 입력 (예: 최애, 우리집, 바다) — 입력 시 '윈도 최애' 음성 호출 등록!",
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
                hx_swap="outerHTML",
                cls="control-group",
            ),
            cls="option-card",
        ),
        # 카드 7: 마이크, 호출어('윈도', 'windo') 및 무응답 대기 시간 설정
        Div(
            Div(
                Span("7. 마이크 & 호출어"),
                Button(
                    "🔄 마이크 재탐색",
                    type="button",
                    cls="secondary",
                    hx_post="/api/mic/rescan",
                    hx_target="#settings-controls",
                    hx_swap="outerHTML",
                ),
                cls="option-card-title",
            ),
            Form(
                Select(
                    *mic_options,
                    name="device_index",
                    hx_post="/api/mic/select",
                    hx_target="#settings-controls",
                    hx_swap="outerHTML",
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
                hx_swap="outerHTML",
                cls="control-group",
            ),
            Form(
                Div(
                    Span("⏱️ 호출 후 대기 시간(초):", cls="timeout-label"),
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
                hx_swap="outerHTML",
                cls="control-group",
            ),
            Button(
                mode_btn_text,
                type="button",
                cls=mode_btn_cls,
                style="width:100%;",
                hx_post="/api/wake-words/toggle",
                hx_target="#settings-controls",
                hx_swap="outerHTML",
            ),
            cls="option-card",
        ),
        id="settings-controls",
        cls="drawer-col",
        **extra_attrs,
    )


@rt("/")
def get():
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
                type="button",
                title="WINDO 설정 및 ComfyUI 노드 모니터 열기",
                cls="floating-settings-trigger",
                onclick="document.getElementById('settings-backdrop').classList.add('open')",
            ),
            Div(
                Div(
                    Header(
                        Div(
                            Span("WINDO", cls="drawer-logo"),
                            Span("AI 가상 창문 설정 · ComfyUI 노드 실행 모니터", cls="drawer-title"),
                            cls="drawer-brand",
                        ),
                        Button(
                            "✕ 창문으로 돌아가기",
                            type="button",
                            cls="drawer-close-btn",
                            onclick="document.getElementById('settings-backdrop').classList.remove('open')",
                        ),
                        cls="drawer-header",
                    ),
                    Div(
                        "✨ \"정보 / 전체 (켜·꺼)\" → 모든 위젯 · \"~ 찾아줘\" → 웹 이미지 검색 · \"~ 만들어줘\" → AI 풍경 생성 · \"날씨/캘린더 꺼\" → 개별 끄기",
                        cls="samsung-blue-banner",
                    ),
                    Div(
                        render_settings_controls(),
                        Div(
                            Div(
                                Div(
                                    Span("8. 노드 실행 모니터 & 음성 로그"),
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
                                            placeholder="직접 명령어 입력 (예: 윈도 저녁 / 윈도 도쿄 야경 찾아줘 / 윈도 눈 내리는 숲 만들어줘)",
                                        ),
                                        Button("명령 실행", type="submit"),
                                        cls="inline-input-row",
                                    ),
                                    hx_post="/api/simulate",
                                    hx_swap="none",
                                    hx_on__after_request="this.reset()",
                                    cls="control-group",
                                ),
                                Div(
                                    Div(
                                        Span("COMFYUI NODE GRAPH & LOG", cls="terminal-title"),
                                        cls="terminal-header",
                                    ),
                                    render_live_monitor(),
                                    cls="terminal-wrapper terminal-wrapper-tall",
                                ),
                                cls="option-card",
                            ),
                            cls="drawer-col",
                        ),
                        cls="drawer-body",
                    ),
                    cls="settings-drawer",
                    onclick="event.stopPropagation()",
                ),
                id="settings-backdrop",
                cls="settings-backdrop",
                onclick="this.classList.remove('open')",
            ),
        ),
    )


# -------------------------------------------------------------------------
# HTMX 비동기 API 엔드포인트
# -------------------------------------------------------------------------
@rt("/api/window/poll")
def get_window_poll(sig: str = ""):
    current_sig = compute_window_sig()
    if sig == current_sig:
        return Response(status_code=204)
    return render_window_stage(), render_settings_controls(oob=True)


@rt("/api/logs")
def get_logs():
    return render_live_monitor()


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


@rt("/api/scene/preset")
def post_scene_preset(key: str):
    decoded_key = urllib.parse.unquote(key)
    preset = landscape_mgr.switch_to_preset(decoded_key)
    if preset:
        lighting_mgr.set_preset_override(preset.lighting_key)
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
        l_state = lighting_mgr.get_current_state()
        audio_ctrl.add_log(
            "STT",
            f"🪟 생성된 AI 풍경 이미지를 창문에 즉시 적용했습니다! ({landscape_mgr.active_title})",
            f"채도 {l_state.saturation_pct}% · 밝기 {l_state.brightness_pct}% · 색온도 {l_state.cct_kelvin}K",
        )
    return render_settings_controls(), render_window_stage(oob=True)


@rt("/api/lighting/auto")
def post_lighting_auto():
    lighting_mgr.enable_auto_mode()
    l_state = lighting_mgr.get_current_state()
    audio_ctrl.add_log("SYSTEM", f"☀️ 생체 리듬 자동 자연광 모드 활성화 ({l_state.cct_kelvin}K · 채도 {l_state.saturation_pct}%)")
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
def post_web_search_config(api_key: str = "", cx: str = ""):
    web_search_mgr.save_config(api_key=api_key, cx=cx)
    audio_ctrl.add_log(
        "SYSTEM",
        f"⚙️ Google Custom Search API 설정 저장 완료 (Key: {'등록됨' if api_key else '미등록'} | CX: {'등록됨' if cx else '미등록'})",
        "키가 없어도 스마트 고화질 웹 풍경 폴백으로 자동 검색됩니다.",
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
                if stem not in landscape_mgr.presets:
                    landscape_mgr.presets[stem] = landscape_mgr.build_custom_preset(stem, is_ai=False)
                landscape_mgr.switch_to_preset(stem)

            AudioController.register_custom_scene_keyword(stem)
            sync_custom_scene_keywords()
            lighting_mgr.set_preset_override(inferred_light)

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
    return ""


if __name__ == "__main__":
    import uvicorn
    audio_ctrl.start_listening()
    uvicorn.run(app, host="127.0.0.1", port=5001, reload=False)
