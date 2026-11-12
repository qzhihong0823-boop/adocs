"""
생체 리듬 기반 자연광 제어 모듈 (src/modules/circadian_lighting.py)

=============================================================================
[생체리듬 자동 광원 (Circadian Rhythm Auto-Lighting) 기능의 정확한 정의]
=============================================================================
1. 학술적·기능적 정의:
   - '생체리듬 자동 광원'이란 창문이 없는 실내(지하 공간, 반지하, 내측 사무실 등)에 머무는 사용자의
     24시간 생체시계(Circadian Rhythm)가 교란되지 않도록, **현재 실제 시각(Real-time Clock)의 태양 고도에 맞춰
     창문 화면 전체의 색온도(CCT, 2700K~6500K)·채도(Saturation)·밝기(Brightness)를 실시간 동기화하는 기능**입니다.
   - 인간 망막의 내인성 광민감성 망막신경절세포(ipRGC)는 아침·한낮의 청백색 파장(5500K~6500K)을 감지하면
     수면 호르몬인 '멜라토닌' 분비를 억제하고 각성 호르몬인 '코르티솔' 분비를 촉진해 집중력과 활력을 높입니다.
     반대로 일몰 이후 저녁·야간의 따뜻한 호박색 파장(2700K~3300K)에서는 멜라토닌 분비를 유도해 숙면을 돕습니다.

2. 코에룩스(CoeLux) 입체 광원 시스템과의 역할 구분:
   - [생체리듬 자동 광원 (본 모듈)]: 현재 '시계 시각(Time of Day)'을 기준으로 화면 전체의 색온도(K), 채도, 밝기 톤을
     하루 24시간 주기에 맞춰 부드럽게 변화시키는 **전역 색온도·생체시계 동기화 필터**입니다.
   - [가상 코에룩스(CoeLux) 조명 (virtual_coelux.py)]: 현재 '배경 이미지 속 공간 구도'를 분석해 해·노을·달이 있는
     정확한 위치(동쪽 아침 해, 천창 태양, 수평선 노을, 밤하늘 달)에 눈부신 국소 광원 코어와 방사형 빛줄기,
     창틀 입체 음영을 배치하여 실제 창밖을 보는 듯한 **입체적 시각 착시(Optical Depth Illusion)**를 만드는 기능입니다.

3. 핵심 제어 방식 (ON / OFF 토글 및 수동 제어):
   - [ON (자동 동기화 활성)]: Python datetime으로 현재 시각을 읽어 아크탄젠트(Arctangent) + 가우시안(Gaussian)
     수식으로 계산된 색온도·채도·밝기를 창문에 실시간 반영합니다.
   - [OFF (자동 광원 해제)]: 사용자가 설정창 버튼이나 음성("생체리듬 꺼줘", "자동 광원 꺼줘")으로 이 기능을 끄면,
     시간대별 색온도 오버레이와 필터 개입을 즉시 중단(채도 100%, 밝기 100%, 오버레이 0%)하여
     원본 풍경과 코에룩스 입체 광원만 왜곡 없이 선명하게 감상할 수 있습니다.
=============================================================================
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock


def _purge_mock_modules() -> None:
    """
    colour-science 내부(colour.plotting 등)가 matplotlib/scipy 미설치 환경에서
    sys.modules['matplotlib'] = MagicMock() 등을 주입하여
    diffusers/transformers의 importlib.util.find_spec 호출 시
    'ValueError: matplotlib.__spec__ is not set' 오류가 발생하는 현상을 원천 차단합니다.
    """
    for _mod_name, _mod_obj in list(sys.modules.items()):
        if isinstance(_mod_obj, MagicMock) or (
            _mod_name.startswith(("matplotlib", "scipy", "mpl_toolkits", "cycler"))
            and getattr(_mod_obj, "__spec__", None) is None
        ):
            sys.modules.pop(_mod_name, None)


_ORIG_FIND_SPEC = getattr(importlib.util, "_orig_find_spec", importlib.util.find_spec)


def _safe_find_spec(name: str, package: Optional[str] = None) -> Any:
    mod = sys.modules.get(name)
    if mod is not None and (isinstance(mod, MagicMock) or getattr(mod, "__spec__", None) is None):
        sys.modules.pop(name, None)
    try:
        return _ORIG_FIND_SPEC(name, package)
    except ValueError:
        sys.modules.pop(name, None)
        return None


setattr(importlib.util, "_orig_find_spec", _ORIG_FIND_SPEC)
importlib.util.find_spec = _safe_find_spec

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import colour
    # colour의 지연 로딩(Lazy Loading)을 모듈 로드 시점에 미리 완료시킨 뒤 가짜 모듈을 즉시 제거
    try:
        _init_xy = colour.CCT_to_xy(5800, method="Kang 2002")
        _init_xyz = colour.xy_to_XYZ(_init_xy)
        _ = colour.XYZ_to_sRGB(_init_xyz)
    except Exception:
        pass

_purge_mock_modules()

import numpy as np


@dataclass
class LightingPreset:
    """확장 가능한 시간대/분위기별 조명 및 채도 프리셋 정의"""
    key: str
    label: str
    keywords: Tuple[str, ...]
    cct_kelvin: int          # 색온도 (2700K ~ 6500K)
    saturation: float        # 채도 배율 (예: 아침 1.18, 노을 1.32, 저녁 0.85)
    brightness: float        # 밝기 배율 (예: 아침 1.06, 노을 0.96, 저녁 0.72)
    contrast: float          # 대비 배율
    overlay_opacity: float   # 자연광 RGB 오버레이 투과율 (0.0 ~ 0.45)


@dataclass
class LightingState:
    """프론트엔드 가상 창문에 전달되는 최종 자연광 및 채도 상태"""
    auto_mode: bool
    is_custom_manual: bool
    active_preset_key: str
    active_label: str
    cct_kelvin: int
    rgb_tuple: Tuple[int, int, int]
    rgb_hex: str
    overlay_rgba: str
    css_filter: str
    saturation_pct: int
    brightness_pct: int
    contrast_pct: int


# 기본 탑재된 조명 프리셋 (향후 새 시간대/날씨 테마를 딕셔너리에 추가하기만 하면 자동 확장됨)
# [설계 주의] overlay_opacity가 지나치게 높으면 화면 전체에 뿌연 먼지/안개가 낀 것처럼 대비가 죽으므로,
# 0.03 ~ 0.08 이하의 미세 색온도 틴트만 적용하고 선명도(contrast)를 유지합니다.
DEFAULT_LIGHTING_PRESETS: Dict[str, LightingPreset] = {
    "default": LightingPreset(
        key="default",
        label="기본 대기 모드",
        keywords=("기본", "기분", "대기", "대기화면", "원래대로", "초기화", "디폴트", "default"),
        cct_kelvin=5800,
        saturation=1.05,
        brightness=1.0,
        contrast=1.02,
        overlay_opacity=0.03,
    ),
    "morning": LightingPreset(
        key="morning",
        label="상쾌한 아침 자연광",
        keywords=(
            "아침", "마침", "안심", "아짐", "아친", "하침", "아치", "아췸",
            "굿모닝", "모닝", "morning", "새벽", "해뜰녘", "일출", "낮", "환하게", "밝게",
        ),
        cct_kelvin=6500,
        saturation=1.22,
        brightness=1.08,
        contrast=1.04,
        overlay_opacity=0.05,
    ),
    "sunset": LightingPreset(
        key="sunset",
        label="따뜻한 노을 자연광",
        keywords=(
            "노을", "너울", "누을", "노울", "노올", "노엘", "노루", "노일", "노은", "노유",
            "노래", "노랑", "노란", "너를", "너는", "노아", "노원", "로을", "롤", "논", "눈을",
            "석양", "해질녘", "해질", "일몰", "황혼", "오후", "선셋", "sunset",
        ),
        cct_kelvin=3300,
        saturation=1.35,
        brightness=0.96,
        contrast=1.06,
        overlay_opacity=0.08,
    ),
    "evening": LightingPreset(
        key="evening",
        label="차분한 저녁 자연광",
        keywords=(
            "저녁", "전역", "저년", "저녘", "정역", "천역", "전력", "저역",
            "밤", "야경", "밤하늘", "자정", "어둡게", "수면", "이브닝", "나이트", "evening", "night",
        ),
        cct_kelvin=2700,
        saturation=0.88,
        brightness=0.82,
        contrast=1.08,
        overlay_opacity=0.08,
    ),
}


class CircadianLightingManager:
    """
    [생체리듬 자동 광원 매니저 (CircadianLightingManager)]

    역할 정의:
    - 현재 시스템의 실제 시각(Real-time Clock)을 기준으로 인간의 일주기 생체시계(Circadian Rhythm)에 맞는
      자연광 색온도(CCT: 아침·낮 6500K 각성광 ↔ 저녁·밤 2700K 이완광)와 채도·밝기를 실시간 연산합니다.
    - 사용자는 언제든 이 기능을 켜거나(ON) 끌(OFF) 수 있습니다:
      * ON  (auto_mode=True) : 실시간 시계에 맞춰 색온도·채도·밝기가 자동 동기화됨.
      * OFF (auto_mode=False, is_disabled=True): 시간대별 자동 필터 개입을 완전히 끄고,
        원본 풍경 색감(채도 100%, 밝기 100%, 오버레이 0%)을 그대로 유지함.
    """

    FEATURE_TITLE: str = "생체리듬 자동 광원 (Circadian Rhythm Auto-Lighting)"
    FEATURE_SUMMARY: str = (
        "실제 현재 시각(06:30 일출 ~ 12:30 정오 ~ 18:30 일몰 ~ 야간)의 태양 고도 변화에 맞춰 "
        "화면의 색온도(2700K~6500K)·채도·밝기를 자동 조절하여 실내에서도 생체시계(각성·숙면 주기)를 유지해 주는 기능입니다. "
        "끄기(OFF)를 선택하면 시간대 자동 보정이 해제되어 원본 풍경 색감 그대로 표시됩니다."
    )

    CCT_MIN: int = 2700
    CCT_MAX: int = 6500
    SUNRISE_HOUR: float = 6.5   # 06:30 일출 기준
    SUNSET_HOUR: float = 18.5   # 18:30 일몰 기준

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.presets: Dict[str, LightingPreset] = dict(DEFAULT_LIGHTING_PRESETS)

        # 초기 상태: 생체리듬 자동 광원 ON 상태로 시작하되, 버튼이나 음성으로 즉시 OFF 전환 가능
        self.auto_mode: bool = True
        self.is_disabled: bool = False
        self.is_custom_manual: bool = False
        self.override_preset_key: Optional[str] = None
        self.override_custom_cct: Optional[int] = None
        self.override_custom_saturation: Optional[float] = None
        self.override_custom_brightness: Optional[float] = None
        self.override_custom_contrast: Optional[float] = None

    def register_preset(self, preset: LightingPreset) -> None:
        """새로운 시간대/분위기 조명 프리셋을 동적으로 추가할 수 있는 확장 메서드"""
        with self._lock:
            self.presets[preset.key] = preset

    # -------------------------------------------------------------------------
    # 1. 아크탄젠트(Arctangent) + 가우시안(Gaussian) 기반 시간별 색온도 계산
    # -------------------------------------------------------------------------
    @classmethod
    def calculate_time_based_cct(cls, target_dt: Optional[datetime] = None) -> Tuple[int, float, float]:
        """
        현재 시각(시+분)을 기반으로 2700K ~ 6500K 사이의 연속 색온도(CCT)와
        시간대 맞춤 채도(Saturation), 밝기(Brightness)를 계산합니다.
        """
        now = target_dt or datetime.now()
        hour_float = now.hour + (now.minute / 60.0) + (now.second / 3600.0)

        # 1) 일출/일몰 경계에서의 부드러운 전환을 위한 아크탄젠트(Arctangent) 포락선
        rise_transition = 0.5 + (math.atan((hour_float - cls.SUNRISE_HOUR) * 1.4) / math.pi)
        set_transition = 0.5 - (math.atan((hour_float - cls.SUNSET_HOUR) * 1.4) / math.pi)
        daylight_envelope = max(0.0, min(1.0, rise_transition * set_transition * 1.15))

        # 2) 한낮(12:30)에 6500K 정점을 찍는 가우시안(Gaussian) 곡선 결합
        sigma = 4.2
        gaussian_peak = math.exp(-((hour_float - 12.5) ** 2) / (2.0 * (sigma ** 2)))

        blend_factor = max(0.0, min(1.0, 0.55 * daylight_envelope + 0.45 * gaussian_peak))
        cct = int(round(cls.CCT_MIN + (cls.CCT_MAX - cls.CCT_MIN) * blend_factor))

        # 시간대별 자연스러운 채도 및 밝기 곡선
        saturation = round(0.88 + 0.30 * blend_factor, 2)
        brightness = round(0.78 + 0.28 * blend_factor, 2)
        return cct, saturation, brightness

    # -------------------------------------------------------------------------
    # 2. colour-science (CIE 1931) 기반 켈빈(K) -> sRGB 변환
    # -------------------------------------------------------------------------
    @staticmethod
    def cct_to_srgb(cct_kelvin: int) -> Tuple[int, int, int]:
        """
        colour-science 라이브러리를 사용해 색온도(K)를 CIE 1931 xy -> XYZ -> sRGB로 변환합니다.
        Kang 2002 메서드를 사용하여 2700K ~ 6500K 전 영역을 정밀하게 변환합니다.
        """
        clamped_cct = max(1800, min(25000, int(cct_kelvin)))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                xy = colour.CCT_to_xy(clamped_cct, method="Kang 2002")
                xyz = colour.xy_to_XYZ(xy)
                srgb = colour.XYZ_to_sRGB(xyz)
                _purge_mock_modules()
                srgb_clipped = np.clip(srgb, 0.0, 1.0)
                # 최대 채널 기준 정규화하여 화면 오버레이용 순수 색조 추출
                max_val = float(np.max(srgb_clipped))
                if max_val > 0:
                    srgb_clipped = srgb_clipped / max_val
                r, g, b = (int(round(float(c) * 255)) for c in srgb_clipped)
                return r, g, b
        except Exception:
            # 예외 시 안전한 색온도 근사 폴백
            return (255, 248, 240) if clamped_cct >= 5000 else (255, 195, 130)

    # -------------------------------------------------------------------------
    # 3. 음성 명령 의도 분석 및 조명/채도 수동 제어(Override & Custom) 상태 전환
    # -------------------------------------------------------------------------
    def apply_voice_intent(self, command_text: str) -> Optional[str]:
        """
        사용자의 음성 명령에서:
        1) 채도/밝기/색온도 직접 조절 명령("채도 150으로 해줘", "채도 높여줘", "채도 낮춰줘", "채도 알려줘") 감지
        2) 자동 모드 복귀 요청("자동 모드") 감지
        3) 시간대 프리셋("아침", "노을", "저녁", "기본") 키워드 감지
        반환값: 상태 코드 ('custom', 'query', 'auto', 또는 매칭된 프리셋 key, 없으면 None)
        """
        text = command_text.strip().lower()
        if not text:
            return None

        # 1) 현재 채도/색온도 상태 조회 명령 ("채도 알려줘", "현재 채도", "색온도 알려줘")
        if any(kw in text for kw in ("채도 알려", "채도 몇", "현재 채도", "색온도 알려", "상태 알려")):
            return "query"

        # 2) 음성으로 채도/밝기/색온도 직접 조절 ("채도 150", "채도 높여줘", "채도 낮춰줘" 등)
        cur = self.get_current_state()
        numbers = [int(n) for n in re.findall(r"\d+", text)]

        if "채도" in text:
            if numbers:
                self.set_custom_lighting(saturation_pct=numbers[0])
                return "custom"
            if any(w in text for w in ("높여", "올려", "진하게", "강하게", "더", "업", "up")):
                self.set_custom_lighting(saturation_pct=cur.saturation_pct + 20)
                return "custom"
            if any(w in text for w in ("낮춰", "내려", "연하게", "약하게", "줄여", "다운", "down")):
                self.set_custom_lighting(saturation_pct=cur.saturation_pct - 20)
                return "custom"

        if "밝기" in text and numbers:
            self.set_custom_lighting(brightness_pct=numbers[0])
            return "custom"

        if ("색온도" in text or "켈빈" in text) and numbers:
            self.set_custom_lighting(cct_kelvin=numbers[0])
            return "custom"

        # 3) 생체리듬 자동 광원 ON/OFF 음성 제어 ("생체리듬 켜줘", "생체리듬 꺼줘", "자동 광원 켜줘", "자동 광원 꺼줘")
        if any(kw in text for kw in ("생체리듬", "생체 리듬", "자동 광원", "자동광원", "자동 모드", "자동모드")):
            if any(off_w in text for off_w in ("꺼줘", "꺼", "끄기", "오프", "off", "중지", "해제")):
                self.disable_auto_mode()
                return "auto_off"
            self.enable_auto_mode()
            return "auto"

        if any(kw in text for kw in ("현재 시간", "실시간")):
            self.enable_auto_mode()
            return "auto"

        # 4) 등록된 프리셋(아침, 노을, 저녁, 기본 등) 키워드 매칭
        # 단, '~ 만들어줘', '~ 그려줘' 등 AI 이미지 생성 명령일 때는 생성 중에 기존 화면/광원이 바뀌면 안 되므로
        # 생성이 100% 완료된 시점에 광원이 전환되도록 여기서는 변경하지 않음!
        compact_t = text.replace(" ", "")
        if any(v in compact_t for v in ("만들어줘", "만들어", "그려줘", "그려", "생성해줘", "생성해")):
            return None

        with self._lock:
            for key, preset in self.presets.items():
                if any(kw in text for kw in preset.keywords):
                    self.auto_mode = False
                    self.is_disabled = False
                    self.is_custom_manual = False
                    self.override_preset_key = key
                    self.override_custom_cct = None
                    self.override_custom_saturation = None
                    self.override_custom_brightness = None
                    self.override_custom_contrast = None
                    return key

        return None

    def set_custom_lighting(
        self,
        saturation_pct: Optional[int] = None,
        brightness_pct: Optional[int] = None,
        cct_kelvin: Optional[int] = None,
        contrast_pct: Optional[int] = None,
    ) -> LightingState:
        """사용자가 슬라이더나 숫자 입력, 음성으로 채도/밝기/색온도를 직접 조절할 때 호출"""
        cur = self.get_current_state()
        with self._lock:
            base_sat = (
                max(0, min(250, int(saturation_pct))) / 100.0
                if saturation_pct is not None
                else (self.override_custom_saturation if self.override_custom_saturation is not None else cur.saturation_pct / 100.0)
            )
            base_bri = (
                max(30, min(180, int(brightness_pct))) / 100.0
                if brightness_pct is not None
                else (self.override_custom_brightness if self.override_custom_brightness is not None else cur.brightness_pct / 100.0)
            )
            base_cct = (
                max(2000, min(9000, int(cct_kelvin)))
                if cct_kelvin is not None
                else (self.override_custom_cct if self.override_custom_cct is not None else cur.cct_kelvin)
            )
            base_con = (
                max(50, min(160, int(contrast_pct))) / 100.0
                if contrast_pct is not None
                else (self.override_custom_contrast if self.override_custom_contrast is not None else cur.contrast_pct / 100.0)
            )

            self.auto_mode = False
            self.is_disabled = False
            self.is_custom_manual = True
            self.override_custom_saturation = round(base_sat, 2)
            self.override_custom_brightness = round(base_bri, 2)
            self.override_custom_cct = int(base_cct)
            self.override_custom_contrast = round(base_con, 2)

        return self.get_current_state()

    def set_preset_override(self, preset_key: str) -> bool:
        """UI 또는 외부 모듈에서 특정 프리셋('morning', 'sunset', 'evening', 'default')으로 강제 전환"""
        with self._lock:
            if preset_key not in self.presets:
                return False
            self.auto_mode = False
            self.is_disabled = False
            self.is_custom_manual = False
            self.override_preset_key = preset_key
            self.override_custom_cct = None
            self.override_custom_saturation = None
            self.override_custom_brightness = None
            self.override_custom_contrast = None
            return True

    def enable_auto_mode(self) -> None:
        """
        [생체리듬 자동 광원 켜기 (ON)]
        현재 실제 시각(datetime.now())에 맞춰 색온도(2700K~6500K)·채도·밝기를 실시간 동기화합니다.
        """
        with self._lock:
            self.auto_mode = True
            self.is_disabled = False
            self.is_custom_manual = False
            self.override_preset_key = None
            self.override_custom_cct = None
            self.override_custom_saturation = None
            self.override_custom_brightness = None
            self.override_custom_contrast = None

    def disable_auto_mode(self) -> None:
        """
        [생체리듬 자동 광원 끄기 (OFF)]
        시간대별 색온도 필터 및 오버레이 개입을 완전히 중단하고,
        배경 이미지 원본 그대로의 색감(채도 100%, 밝기 100%, 대비 100%, 오버레이 투명도 0%)으로 표시합니다.
        """
        with self._lock:
            self.auto_mode = False
            self.is_disabled = True
            self.is_custom_manual = False
            self.override_preset_key = None
            self.override_custom_cct = 5800
            self.override_custom_saturation = 1.00
            self.override_custom_brightness = 1.00
            self.override_custom_contrast = 1.00

    def toggle_auto_mode(self) -> bool:
        """
        [생체리듬 자동 광원 ON/OFF 토글]
        설정창에서 '생체리듬 자동 광원' 버튼을 클릭할 때마다 켜짐(ON)과 꺼짐(OFF) 상태를 전환합니다.
        반환값: 전환 후 auto_mode 활성 여부 (True=ON, False=OFF)
        """
        with self._lock:
            if self.auto_mode:
                self.disable_auto_mode()
            else:
                self.enable_auto_mode()
            return self.auto_mode

    def get_current_state(self) -> LightingState:
        """현재 창문에 적용할 색온도(K), sRGB 색상, CSS 채도/밝기 필터 상태를 반환"""
        with self._lock:
            if self.is_disabled and not self.auto_mode and not self.is_custom_manual:
                # 생체리듬 자동 광원 OFF 상태: 색온도 막(overlay)을 0.0으로 완전히 끄고 원본 선명도 100% 유지
                cct = 5800
                sat = 1.00
                bri = 1.00
                con = 1.00
                opacity = 0.00
                active_key = "off"
                active_label = "생체리듬 자동 광원 OFF (원본 무보정 100%)"
            elif self.is_custom_manual:
                cct = self.override_custom_cct if self.override_custom_cct is not None else 5800
                sat = self.override_custom_saturation if self.override_custom_saturation is not None else 1.10
                bri = self.override_custom_brightness if self.override_custom_brightness is not None else 1.00
                con = self.override_custom_contrast if self.override_custom_contrast is not None else 1.02
                opacity = 0.04 if cct >= 4500 else 0.07
                active_key = "custom"
                active_label = f"사용자 직접 조절 · {cct}K · 채도 {int(round(sat * 100))}%"
            elif not self.auto_mode and self.override_preset_key in self.presets:
                preset = self.presets[self.override_preset_key]
                cct = preset.cct_kelvin
                sat = preset.saturation
                bri = preset.brightness
                con = preset.contrast
                opacity = preset.overlay_opacity
                active_key = preset.key
                active_label = f"{preset.label} · {cct}K (수동 프리셋)"
            else:
                cct, sat, bri = self.calculate_time_based_cct()
                con = 1.03
                # 화면이 뿌옇게 흐려지지 않도록 색온도 틴트 투명도를 0.04~0.07로 미세 적용
                opacity = 0.04 if cct >= 4500 else 0.07
                active_key = "auto"
                active_label = f"생체리듬 자동 광원 ON ({cct}K 실시간 동기화)"

        r, g, b = self.cct_to_srgb(cct)
        rgb_hex = f"#{r:02x}{g:02x}{b:02x}"
        overlay_rgba = f"rgba({r}, {g}, {b}, {opacity:.2f})"
        css_filter = f"saturate({sat:.2f}) brightness({bri:.2f}) contrast({con:.2f})"

        return LightingState(
            auto_mode=self.auto_mode,
            is_custom_manual=self.is_custom_manual,
            active_preset_key=active_key,
            active_label=active_label,
            cct_kelvin=cct,
            rgb_tuple=(r, g, b),
            rgb_hex=rgb_hex,
            overlay_rgba=overlay_rgba,
            css_filter=css_filter,
            saturation_pct=int(round(sat * 100)),
            brightness_pct=int(round(bri * 100)),
            contrast_pct=int(round(con * 100)),
        )

