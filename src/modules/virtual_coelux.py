"""
가상 코에룩스(CoeLux) 자연 채광(햇빛·노을빛·달빛) 시뮬레이션 모듈 (src/modules/virtual_coelux.py)

설계 근거 (Architectural Rationale):
1. 인위적인 카메라 아티팩트(보케 동그라미, 외곽 원형 링) 및 실내 창틀 모드 완전 배제:
   - 카메라 렌즈 플레어 동그라미나 인위적인 원형 테두리가 전혀 생기지 않도록 제거하고,
     오직 자연에 존재하는 '순수한 빛 그 자체(태양 코어, 구름 틈새 유기적 햇살 광선, 부드러운 노을 대기 산란,
     밤하늘 달빛과 은은한 달무리 및 수면 반사)'만 자연스럽게 렌더링합니다.
2. AI 광원 위치 및 색상 자동 분석 (Gemini Vision API + 컴퓨터 비전 색온도·휘도 역추적):
   - 배경 이미지의 하늘/수평선/구름 구조를 분석하여 아침·낮·노을·밤 시간대에 맞는 최적의 광원 위치(x_pct, y_pct)와
     배경 색조에 어우러지는 빛 색상(core_rgb, corona_rgb, ray_rgb)을 자동 추출합니다.
3. 사용자 맞춤 밝기·색상·위치 제어 (Settings UI & Voice Control):
   - 설정창(⚙️)에서 빛의 밝기(15%~200%), 빛의 색상(컬러 피커 및 프리셋), 빛의 위치(X/Y)를 실시간 조절 가능.
- 문자열 포맷팅 제약 준수: 본 모듈 내부에서는 f-string을 절대 사용하지 않고 .format() 및 콤마(,)만 사용함.
"""

import base64
import io
import json
import math
import os
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from PIL import Image, ImageFilter

try:
    import cv2  # type: ignore
except ImportError:
    class _Cv2Compat:
        COLOR_BGR2HSV = 40
        INTER_AREA = 3
        LINE_AA = 16

        @staticmethod
        def cvtColor(src: np.ndarray, code: int) -> np.ndarray:
            if code == _Cv2Compat.COLOR_BGR2HSV:
                rgb = src[:, :, ::-1].astype(np.uint8)
                pil_hsv = Image.fromarray(rgb, mode="RGB").convert("HSV")
                hsv = np.array(pil_hsv, dtype=np.uint8)
                hsv[:, :, 0] = (hsv[:, :, 0].astype(np.uint16) * 179 // 255).astype(np.uint8)
                return hsv
            return src.copy()

        @staticmethod
        def split(m: np.ndarray) -> Tuple[np.ndarray, ...]:
            return tuple(m[:, :, i] for i in range(m.shape[2]))

        @staticmethod
        def inRange(src: np.ndarray, lowerb: int, upperb: int) -> np.ndarray:
            return np.where((src >= lowerb) & (src <= upperb), 255, 0).astype(np.uint8)

        @staticmethod
        def countNonZero(src: np.ndarray) -> int:
            return int(np.count_nonzero(src))

        @staticmethod
        def add(src1: np.ndarray, src2: np.ndarray) -> np.ndarray:
            return np.clip(src1.astype(np.int32) + src2.astype(np.int32), 0, 255).astype(np.uint8)

        @staticmethod
        def addWeighted(
            src1: np.ndarray,
            alpha: float,
            src2: np.ndarray,
            beta: float,
            gamma: float,
        ) -> np.ndarray:
            blended = src1.astype(np.float32) * float(alpha) + src2.astype(np.float32) * float(beta) + float(gamma)
            return np.clip(blended, 0, 255).astype(np.uint8)

        @staticmethod
        def GaussianBlur(
            src: np.ndarray,
            ksize: Tuple[int, int],
            sigmaX: float = 0.0,
            sigmaY: float = 0.0,
        ) -> np.ndarray:
            radius = float(sigmaX) if sigmaX > 0 else max(1.0, float(ksize[0]) / 6.0)
            rgb = src[:, :, ::-1].astype(np.uint8)
            blurred_pil = Image.fromarray(rgb, mode="RGB").filter(ImageFilter.GaussianBlur(radius=radius))
            blurred_rgb = np.array(blurred_pil, dtype=np.uint8)
            return blurred_rgb[:, :, ::-1].copy()

        @staticmethod
        def resize(
            src: np.ndarray,
            dsize: Tuple[int, int],
            interpolation: int = 3,
        ) -> np.ndarray:
            w, h = int(dsize[0]), int(dsize[1])
            if src.shape[2] == 4:
                rgba = src[:, :, [2, 1, 0, 3]].astype(np.uint8)
                pil_img = Image.fromarray(rgba, mode="RGBA").resize((w, h), Image.Resampling.LANCZOS)
                arr = np.array(pil_img, dtype=np.uint8)
                return arr[:, :, [2, 1, 0, 3]].copy()
            rgb = src[:, :, ::-1].astype(np.uint8)
            pil_img = Image.fromarray(rgb, mode="RGB").resize((w, h), Image.Resampling.LANCZOS)
            arr = np.array(pil_img, dtype=np.uint8)
            return arr[:, :, ::-1].copy()

    cv2 = _Cv2Compat()  # type: ignore

try:
    from sklearn.cluster import KMeans  # type: ignore
except ImportError:
    class KMeans:  # type: ignore
        def __init__(self, n_clusters: int = 5, n_init: int = 10, random_state: int = 42) -> None:
            self.n_clusters = int(n_clusters)
            self.n_init = int(n_init)
            self.random_state = int(random_state)
            self.cluster_centers_: np.ndarray = np.zeros((self.n_clusters, 3), dtype=np.float32)

        def fit_predict(self, X: np.ndarray) -> np.ndarray:
            rng = np.random.default_rng(seed=self.random_state)
            n_samples = X.shape[0]
            k = min(self.n_clusters, n_samples)
            idx = rng.choice(n_samples, size=k, replace=False)
            centers = X[idx].astype(np.float32).copy()
            labels = np.zeros(n_samples, dtype=np.int32)
            for _ in range(18):
                dists = np.linalg.norm(X[:, np.newaxis, :] - centers[np.newaxis, :, :], axis=2)
                new_labels = np.argmin(dists, axis=1)
                if np.array_equal(labels, new_labels):
                    break
                labels = new_labels
                for c_idx in range(k):
                    mask = labels == c_idx
                    if np.any(mask):
                        centers[c_idx] = np.mean(X[mask], axis=0)
            self.cluster_centers_ = centers
            return labels


class VirtualCoeLuxSystem:
    """
    [가상 코에룩스(CoeLux) 순수 자연 채광(햇빛·노을·달빛) 시뮬레이션 시스템]

    핵심 설계:
    - 인위적인 원형 링이나 카메라 보케 동그라미를 일절 생성하지 않고,
      긍정 레퍼런스 사진처럼 순수한 '빛 그 자체'만 자연스럽게 표현합니다.
    1. 주간/아침(NOON / MORNING):
       - 눈부신 태양 코어 + 부드러운 대기 산란 광휘 + 구름 사이로 자연스럽게 뻗어나가는
         비대칭 유기적 햇살 광선 다발(Organic Soft Sunbeams - 끝단이 둥글게 페더링되어 경계선이 없는 자연 광선)
    2. 노을/석양(SUNSET):
       - 구름·수평선 사이에서 빛나는 황금빛 태양 코어 + 주변 구름과 하늘을 부드럽게 물들이는
         황금-주황 대기 노을 번짐(Soft Sunset Atmospheric Glow) + 수평선 아래 잔잔한 노을 수면 반사
    3. 밤/달빛(EVENING):
       - 밤하늘에 선명하고 은은하게 떠 있는 달(보름달) 본체 + 구름을 감싸는 부드러운 달무리(Soft Lunar Halo)
         + 하단 바다·수면 위로 자연스럽게 일렁이는 달빛 수면 윤슬(Lunar Water Shimmer)
    """

    COMMAND_TOGGLE = "TOGGLE_COELUX"

    TIME_MORNING = "MORNING"
    TIME_NOON = "NOON"
    TIME_SUNSET = "SUNSET"
    TIME_EVENING = "EVENING"

    DEFAULT_GEMINI_API_KEY = "AQ.Ab8RN6IZwGOGjcJnOJSzxNnvym-uYhRSsHpjiDrR9GpbGrG2Ug"

    def __init__(
        self,
        n_clusters: int = 5,
        glow_blur_ksize: Tuple[int, int] = (71, 71),
        glow_sigma: float = 28.0,
        glow_weight: float = 0.25,
        edge_light_intensity: float = 0.72,
        gemini_api_key: Optional[str] = None,
    ) -> None:
        self.is_enabled: bool = False
        self.n_clusters: int = max(2, int(n_clusters))

        kx = int(glow_blur_ksize[0]) | 1
        ky = int(glow_blur_ksize[1]) | 1
        self.glow_blur_ksize: Tuple[int, int] = (kx, ky)
        self.glow_sigma: float = float(glow_sigma)
        self.glow_weight: float = float(np.clip(glow_weight, 0.05, 0.85))
        self.edge_light_intensity: float = float(np.clip(edge_light_intensity, 0.1, 1.0))

        self.gemini_api_key: str = (
            gemini_api_key
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or self.DEFAULT_GEMINI_API_KEY
        )

        self.last_detected_time: str = self.TIME_MORNING
        self.last_trade_color_bgr: Tuple[int, int, int] = (140, 210, 255)
        self.origin_corner: str = "top_right"
        self.custom_illust_bgra: Optional[np.ndarray] = None
        self.custom_illust_name: str = ""
        self._last_analyzed_bg_path: str = ""
        self._last_scene_hint: str = ""
        self._placement_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        # AI가 분석한 기본 광원 상태
        self.ai_x_pct: float = 68.0
        self.ai_y_pct: float = 22.0
        self.ai_core_rgb: Tuple[int, int, int] = (255, 254, 248)
        self.ai_corona_rgb: Tuple[int, int, int] = (255, 215, 140)
        self.ai_ray_rgb: Tuple[int, int, int] = (255, 235, 185)
        self.ai_color_hex: str = "#ffd78c"
        self.light_source_type: str = "NATURAL_SUN"
        self.light_label_ko: str = "자연 햇살"
        self.analysis_source: str = "AI Vision Analyzer"
        self.has_water_reflection: bool = False

        # 사용자 수동 조절 상태 (밝기, 색상, 위치)
        self.intensity_pct: int = 100  # 15 ~ 200 (%)
        self.auto_color: bool = True
        self.custom_color_hex: str = "#ffd78c"
        self.auto_position: bool = True
        self.custom_x_pct: Optional[float] = None
        self.custom_y_pct: Optional[float] = None

        self.time_light_colors_bgr: Dict[str, Tuple[int, int, int]] = {
            self.TIME_MORNING: (110, 205, 255),
            self.TIME_NOON: (245, 250, 255),
            self.TIME_SUNSET: (70, 145, 255),
            self.TIME_EVENING: (255, 220, 185),
        }

    # =========================================================================
    # 유틸리티: HEX <-> RGB 변환 및 활성 상태 조회
    # =========================================================================
    @staticmethod
    def hex_to_rgb(hex_str: str) -> Tuple[int, int, int]:
        clean = str(hex_str).strip().lstrip("#")
        if len(clean) == 3:
            clean = "".join(c * 2 for c in clean)
        if len(clean) != 6:
            return (255, 215, 140)
        try:
            r = int(clean[0:2], 16)
            g = int(clean[2:4], 16)
            b = int(clean[4:6], 16)
            return (r, g, b)
        except Exception:
            return (255, 215, 140)

    @staticmethod
    def rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(
            int(np.clip(rgb[0], 0, 255)),
            int(np.clip(rgb[1], 0, 255)),
            int(np.clip(rgb[2], 0, 255)),
        )

    @property
    def light_x_pct(self) -> float:
        if not self.auto_position and self.custom_x_pct is not None:
            return float(self.custom_x_pct)
        return float(self.ai_x_pct)

    @property
    def light_y_pct(self) -> float:
        if not self.auto_position and self.custom_y_pct is not None:
            return float(self.custom_y_pct)
        return float(self.ai_y_pct)

    def get_active_light_palettes(self) -> Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int], str]:
        """현재 적용 중인 (core_rgb, corona_rgb, ray_rgb, active_hex)를 반환합니다."""
        if not self.auto_color and self.custom_color_hex:
            ur, ug, ub = self.hex_to_rgb(self.custom_color_hex)
            core = (
                int(np.clip(ur * 0.22 + 255 * 0.78, 228, 255)),
                int(np.clip(ug * 0.22 + 255 * 0.78, 228, 255)),
                int(np.clip(ub * 0.22 + 255 * 0.78, 228, 255)),
            )
            corona = (ur, ug, ub)
            ray = (
                int(np.clip(ur * 0.72 + 255 * 0.28, 0, 255)),
                int(np.clip(ug * 0.72 + 255 * 0.28, 0, 255)),
                int(np.clip(ub * 0.72 + 255 * 0.28, 0, 255)),
            )
            return core, corona, ray, self.rgb_to_hex(corona)
        return self.ai_core_rgb, self.ai_corona_rgb, self.ai_ray_rgb, self.ai_color_hex

    def set_light_controls(
        self,
        intensity_pct: Optional[int] = None,
        color_hex: Optional[str] = None,
        x_pct: Optional[float] = None,
        y_pct: Optional[float] = None,
        use_ai_color: Optional[bool] = None,
        use_ai_position: Optional[bool] = None,
        space_mode: Optional[str] = None,
    ) -> None:
        """사용자가 설정창에서 빛의 밝기, 색상, 위치를 조절할 때 호출합니다."""
        with self._lock:
            if intensity_pct is not None:
                self.intensity_pct = int(np.clip(int(intensity_pct), 15, 200))
            if use_ai_color is True:
                self.auto_color = True
            elif color_hex is not None and str(color_hex).strip():
                self.auto_color = False
                self.custom_color_hex = str(color_hex).strip()
            if use_ai_position is True:
                self.auto_position = True
                self.custom_x_pct = None
                self.custom_y_pct = None
            else:
                if x_pct is not None:
                    self.auto_position = False
                    self.custom_x_pct = float(np.clip(float(x_pct), 5.0, 95.0))
                if y_pct is not None:
                    self.auto_position = False
                    self.custom_y_pct = float(np.clip(float(y_pct), 5.0, 75.0))

    # =========================================================================
    # 유기적 자연 햇살 광선 다발 생성기 (긍정 예시 4, 5번 사진 완벽 재현)
    # =========================================================================
    @staticmethod
    def _build_organic_sunbeams(sx: float, sy: float, time_of_day: str) -> List[Dict[str, Any]]:
        """
        [긍정 레퍼런스 4번·5번 사진 재현: 비대칭 유기적 구름 틈새 햇살 광선]
        인위적인 동그라미 보케나 수레바퀴 줄무늬 없이, 태양 중심(sx, sy)에서
        구름 틈새로 퍼져나가는 불규칙한 각도·길이·폭·투명도의 부드러운 햇살 줄기(Crepuscular Sunbeams)와
        광학 스타버스트(Soft Starburst Glint) 좌표를 생성합니다.
        """
        if time_of_day == "EVENING":
            return []

        # 태양의 좌우 위치에 따라 아래쪽 하늘/풍경 방향으로 더 길고 풍부하게 뻗어나가도록 비대칭 설계
        # 각 튜플: (각도 deg, 길이 반경, 시작 폭, 끝 폭, 기본 불투명도, 블러 강도 구분)
        if time_of_day == "SUNSET":
            # 노을(긍정 예시 3, 6번)은 강한 직선 광선 대신 수평선과 구름으로 부드럽게 퍼지는 은은한 광선만 최소 배치
            beam_specs = [
                (-12.0, 38.0, 1.2, 8.5, 0.22, "wide"),
                (12.0, 38.0, 1.2, 8.5, 0.22, "wide"),
                (168.0, 38.0, 1.2, 8.5, 0.22, "wide"),
                (192.0, 38.0, 1.2, 8.5, 0.22, "wide"),
                (78.0, 32.0, 1.0, 7.0, 0.18, "soft"),
                (104.0, 34.0, 1.0, 7.5, 0.18, "soft"),
            ]
        else:
            # 낮/아침(긍정 예시 4, 5번): 구름 틈새로 시원하게 뻗어나가는 비대칭 유기적 햇살 줄기들
            beam_specs = [
                # 아래쪽 & 대각선으로 길게 뻗는 주 햇살 줄기 (Crepuscular Cloud Rays)
                (22.0, 68.0, 0.7, 7.2, 0.44, "soft"),
                (38.0, 82.0, 0.9, 9.8, 0.52, "wide"),
                (57.0, 64.0, 0.6, 5.8, 0.38, "soft"),
                (74.0, 92.0, 1.0, 11.5, 0.56, "wide"),
                (88.0, 72.0, 0.7, 6.4, 0.46, "soft"),
                (106.0, 86.0, 0.9, 10.2, 0.50, "wide"),
                (128.0, 76.0, 0.8, 8.4, 0.45, "soft"),
                (149.0, 66.0, 0.7, 6.8, 0.40, "soft"),
                (166.0, 58.0, 0.6, 5.5, 0.34, "soft"),
                # 위쪽 & 옆쪽으로 짧고 날렵하게 뻗는 태양 광채 줄기 (긍정 예시 5번 사진)
                (-18.0, 44.0, 0.6, 4.2, 0.38, "soft"),
                (-46.0, 38.0, 0.5, 3.6, 0.34, "soft"),
                (-78.0, 34.0, 0.5, 3.2, 0.30, "soft"),
                (-115.0, 36.0, 0.5, 3.5, 0.32, "soft"),
                (-148.0, 42.0, 0.6, 4.0, 0.36, "soft"),
                (188.0, 48.0, 0.6, 4.6, 0.35, "soft"),
            ]

        beams: List[Dict[str, Any]] = []
        for deg, length, w_start, w_end, op, blur_kind in beam_specs:
            rad = math.radians(float(deg))
            dx = math.cos(rad)
            dy = math.sin(rad)
            # 수직 법선 벡터
            nx = -dy
            ny = dx

            p1x = sx + nx * (w_start * 0.5)
            p1y = sy + ny * (w_start * 0.5)
            p2x = sx - nx * (w_start * 0.5)
            p2y = sy - ny * (w_start * 0.5)

            ex = sx + dx * length
            ey = sy + dy * length
            p3x = ex - nx * (w_end * 0.5)
            p3y = ey - ny * (w_end * 0.5)
            p4x = ex + nx * (w_end * 0.5)
            p4y = ey + ny * (w_end * 0.5)

            pts = "{:.2f},{:.2f} {:.2f},{:.2f} {:.2f},{:.2f} {:.2f},{:.2f}".format(
                p1x, p1y, p2x, p2y, p3x, p3y, p4x, p4y
            )
            beams.append({
                "points": pts,
                "opacity": round(float(op), 3),
                "blur": blur_kind,
            })
        return beams

    # =========================================================================
    # 1. 제어 명령어 (Toggle Command)
    # =========================================================================
    def handle_command(self, command_text: str) -> bool:
        normalized_cmd = str(command_text).strip().upper()
        if normalized_cmd == self.COMMAND_TOGGLE:
            self.is_enabled = not self.is_enabled
            state_str = "ON" if self.is_enabled else "OFF"
            print("[CoeLux System] 명령어 '{}' 수신 -> 조명 상태 전환: {}".format(
                self.COMMAND_TOGGLE, state_str
            ))
        else:
            print("[CoeLux System] 알 수 없는 명령어 무시: '{}'".format(command_text))
        return self.is_enabled

    # =========================================================================
    # 2. 시간대 및 분위기 분석 (Background Analysis)
    # =========================================================================
    def analyze_time_of_day(self, bg_bgr: np.ndarray, scene_hint: str = "") -> str:
        if bg_bgr is None or bg_bgr.size == 0:
            raise ValueError("배경 이미지가 비어 있거나 유효하지 않습니다.")

        hint_lower = str(scene_hint).lower()
        if any(w in hint_lower for w in ("노을", "석양", "일몰", "황혼", "sunset")):
            self.last_detected_time = self.TIME_SUNSET
            return self.TIME_SUNSET
        if any(w in hint_lower for w in ("저녁", "밤", "야경", "달빛", "evening", "night", "moon")):
            self.last_detected_time = self.TIME_EVENING
            return self.TIME_EVENING
        if any(w in hint_lower for w in ("아침", "일출", "동쪽", "morning", "sunrise")):
            self.last_detected_time = self.TIME_MORNING
            return self.TIME_MORNING
        if any(w in hint_lower for w in ("바다", "낮", "정오", "맑은", "기본", "noon", "daylight", "default")):
            self.last_detected_time = self.TIME_NOON
            return self.TIME_NOON

        hsv_img = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2HSV)
        h_channel, s_channel, v_channel = cv2.split(hsv_img)

        mean_h = float(np.mean(h_channel))
        mean_v = float(np.mean(v_channel))

        sky_h = max(1, int(bg_bgr.shape[0] * 0.55))
        sky_hue = h_channel[:sky_h, :]
        sky_sat = s_channel[:sky_h, :]
        sky_val = v_channel[:sky_h, :]
        sky_mean_v = float(np.mean(sky_val))

        if mean_v < 78.0 or sky_mean_v < 72.0:
            detected_time = self.TIME_EVENING
        else:
            sunset_mask = ((sky_hue <= 18) | (sky_hue >= 165)) & (sky_sat >= 105) & (sky_val >= 110)
            sunset_ratio = float(np.count_nonzero(sunset_mask)) / float(sky_hue.size)

            warm_mask = (h_channel >= 8) & (h_channel <= 38) & (s_channel >= 42) & (v_channel >= 110)
            warm_ratio = float(np.count_nonzero(warm_mask)) / float(h_channel.size)

            blue_sky_mask = (sky_hue >= 92) & (sky_hue <= 128) & (sky_sat >= 45) & (sky_val >= 145)
            blue_sky_ratio = float(np.count_nonzero(blue_sky_mask)) / float(sky_hue.size)

            if blue_sky_ratio >= 0.24 and mean_v >= 145.0:
                detected_time = self.TIME_NOON
            elif sunset_ratio >= 0.24 and blue_sky_ratio < 0.10:
                detected_time = self.TIME_SUNSET
            elif warm_ratio >= 0.10 or (10.0 <= mean_h <= 42.0) or mean_v < 140.0:
                detected_time = self.TIME_MORNING
            else:
                detected_time = self.TIME_NOON

        self.last_detected_time = detected_time
        return detected_time

    # =========================================================================
    # 2-B. AI 광원 위치 & 이미지 맞춤형 광원 색상 자동 분석기
    # =========================================================================
    def _detect_light_source_cv(
        self,
        bg_bgr: np.ndarray,
        time_of_day: str,
    ) -> Dict[str, Any]:
        """
        배경 이미지에서 태양·노을·달이 위치할 가장 자연스러운 좌표(x_pct, y_pct)와
        배경과 조화되는 순수한 빛 색상을 자동 산출합니다.
        """
        h, w = bg_bgr.shape[:2]
        hsv = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1].astype(np.float32)
        val = hsv[:, :, 2].astype(np.float32)

        b_ch = bg_bgr[:, :, 0].astype(np.float32)
        g_ch = bg_bgr[:, :, 1].astype(np.float32)
        r_ch = bg_bgr[:, :, 2].astype(np.float32)

        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        x_norm = xx / float(max(1, w - 1))
        y_norm = yy / float(max(1, h - 1))

        # 가장자리에 너무 치우쳐 화면 구석이 하얗게 날아가지 않도록 x: 18~82%, y: 12~52% 영역 내에서 광원 탐색
        inner_window = (
            np.clip(np.sin(np.pi * np.clip(x_norm, 0.12, 0.88)), 0.20, 1.0)
            * np.where((y_norm >= 0.10) & (y_norm <= 0.54), 1.0, 0.08)
        )
        bright_hotspot = np.power(val / 255.0, 3.4) * inner_window
        hs_u8 = np.clip(bright_hotspot * 255.0, 0, 255).astype(np.uint8)
        hs_blur = cv2.GaussianBlur(np.stack([hs_u8, hs_u8, hs_u8], axis=2), (25, 25), sigmaX=8.5)[:, :, 0]
        peak_val = float(np.max(hs_blur))
        peak_idx = int(np.argmax(hs_blur))
        peak_y, peak_x = divmod(peak_idx, w)
        peak_x_pct = float(np.clip((float(peak_x) / float(w)) * 100.0, 20.0, 80.0))
        peak_y_pct = float(np.clip((float(peak_y) / float(h)) * 100.0, 14.0, 48.0))

        # 광원 주변 후광 픽셀에서 고유 색조 샘플링
        r0, r1 = max(0, peak_y - 18), min(h, peak_y + 19)
        c0, c1 = max(0, peak_x - 18), min(w, peak_x + 19)
        patch_r = r_ch[r0:r1, c0:c1]
        patch_g = g_ch[r0:r1, c0:c1]
        patch_b = b_ch[r0:r1, c0:c1]
        patch_v = val[r0:r1, c0:c1]

        halo_mask = (patch_v >= 90.0) & (patch_v <= 248.0)
        if np.any(halo_mask):
            sample_r = float(np.mean(patch_r[halo_mask]))
            sample_g = float(np.mean(patch_g[halo_mask]))
            sample_b = float(np.mean(patch_b[halo_mask]))
        else:
            sample_r = float(np.mean(patch_r))
            sample_g = float(np.mean(patch_g))
            sample_b = float(np.mean(patch_b))

        max_ch = max(sample_r, sample_g, sample_b, 1.0)
        norm_r = int(np.clip((sample_r / max_ch) * 255.0, 140, 255))
        norm_g = int(np.clip((sample_g / max_ch) * 255.0, 120, 255))
        norm_b = int(np.clip((sample_b / max_ch) * 255.0, 95, 255))

        # -----------------------------------------------------------------
        # 1) 밤/저녁 (EVENING: 구름 사이로 부드럽게 퍼지는 은청색 달빛 코어 & 은은한 구름 달무리, 인공 줄무늬 완전 제거)
        # -----------------------------------------------------------------
        if time_of_day == self.TIME_EVENING:
            sky_mask = np.where((y_norm >= 0.10) & (y_norm <= 0.36) & (x_norm >= 0.20) & (x_norm <= 0.80), 1.0, 0.02)
            moon_cand = (val / 255.0) * (1.0 - np.clip(sat / 255.0, 0.0, 0.6) * 0.45) * sky_mask
            m_u8 = np.clip(moon_cand * 255.0, 0, 255).astype(np.uint8)
            m_blur = cv2.GaussianBlur(np.stack([m_u8, m_u8, m_u8], axis=2), (19, 19), sigmaX=6.0)[:, :, 0]
            if float(np.max(m_blur)) >= 60.0:
                b_idx = int(np.argmax(m_blur))
                by, bx = divmod(b_idx, w)
                x_pct = float(np.clip((float(bx) / float(w)) * 100.0, 22.0, 78.0))
                y_pct = float(np.clip((float(by) / float(h)) * 100.0, 14.0, 32.0))
            else:
                x_pct, y_pct = 54.0, 20.0

            corona_c = (
                int(np.clip(norm_r * 0.28 + 180 * 0.72, 160, 236)),
                int(np.clip(norm_g * 0.30 + 214 * 0.70, 192, 248)),
                255,
            )
            return {
                "time_of_day": self.TIME_EVENING,
                "x_pct": round(x_pct, 1),
                "y_pct": round(y_pct, 1),
                "source_type": "NATURAL_MOON",
                "label_ko": "구름 속 은은한 밤 달빛 & 달무리",
                "core_rgb": (250, 253, 255),
                "corona_rgb": corona_c,
                "ray_rgb": (175, 212, 252),
                "has_water_reflection": False,
                "analysis_source": "AI Lunar Analyzer",
            }

        # -----------------------------------------------------------------
        # 2) 노을/석양 (SUNSET: 황금빛 노을 태양 코어 & 부드러운 구름·수평선 노을 번짐, 인공 줄무늬 완전 제거)
        # -----------------------------------------------------------------
        if time_of_day == self.TIME_SUNSET:
            red_warm = np.clip((r_ch * 1.35 + g_ch * 0.78 - b_ch * 0.75) / 255.0, 0.1, 2.2)
            sunset_score = (
                np.power(val / 255.0, 2.6)
                * red_warm
                * np.where((y_norm >= 0.14) & (y_norm <= 0.62) & (x_norm >= 0.18) & (x_norm <= 0.82), 1.0, 0.06)
            )
            s_u8 = np.clip(sunset_score * 130.0, 0, 255).astype(np.uint8)
            s_blur = cv2.GaussianBlur(np.stack([s_u8, s_u8, s_u8], axis=2), (23, 23), sigmaX=7.5)[:, :, 0]
            b_idx = int(np.argmax(s_blur))
            by, bx = divmod(b_idx, w)
            x_pct = float(np.clip((float(bx) / float(w)) * 100.0, 20.0, 80.0))
            y_pct = float(np.clip((float(by) / float(h)) * 100.0, 20.0, 52.0))

            corona_c = (
                255,
                int(np.clip(norm_g * 0.45 + 165 * 0.55, 140, 210)),
                int(np.clip(norm_b * 0.30 + 70 * 0.70, 55, 130)),
            )
            return {
                "time_of_day": self.TIME_SUNSET,
                "x_pct": round(x_pct, 1),
                "y_pct": round(y_pct, 1),
                "source_type": "NATURAL_SUNSET",
                "label_ko": "석양 노을 해 & 구름 노을 번짐",
                "core_rgb": (255, 252, 232),
                "corona_rgb": corona_c,
                "ray_rgb": (255, 175, 92),
                "has_water_reflection": False,
                "analysis_source": "AI Sunset Analyzer",
            }

        # -----------------------------------------------------------------
        # 3) 아침 (MORNING - 긍정 예시 4, 5번 사진의 따뜻한 아침 버전)
        # -----------------------------------------------------------------
        if time_of_day == self.TIME_MORNING:
            if peak_val >= 72.0:
                x_pct, y_pct = peak_x_pct, min(peak_y_pct, 34.0)
            else:
                x_pct, y_pct = 70.0, 22.0

            corona_c = (
                255,
                int(np.clip(norm_g * 0.45 + 222 * 0.55, 200, 246)),
                int(np.clip(norm_b * 0.35 + 150 * 0.65, 130, 210)),
            )
            return {
                "time_of_day": self.TIME_MORNING,
                "x_pct": round(x_pct, 1),
                "y_pct": round(y_pct, 1),
                "source_type": "NATURAL_MORNING_SUN",
                "label_ko": "따뜻한 아침 햇살 & 구름 광선",
                "core_rgb": (255, 254, 246),
                "corona_rgb": corona_c,
                "ray_rgb": (255, 238, 190),
                "has_water_reflection": False,
                "analysis_source": "AI Morning Sun Analyzer",
            }

        # -----------------------------------------------------------------
        # 4) 낮/주간 (NOON - 긍정 예시 4, 5번 사진: 구름 사이 눈부신 태양 & 자연스러운 햇살 광선)
        # -----------------------------------------------------------------
        sky_sun_mask = np.where((y_norm >= 0.10) & (y_norm <= 0.36) & (x_norm >= 0.18) & (x_norm <= 0.82), 1.0, 0.05)
        noon_sun_score = np.power(val / 255.0, 3.8) * sky_sun_mask
        n_u8 = np.clip(noon_sun_score * 255.0, 0, 255).astype(np.uint8)
        n_blur = cv2.GaussianBlur(np.stack([n_u8, n_u8, n_u8], axis=2), (23, 23), sigmaX=7.5)[:, :, 0]
        if float(np.max(n_blur)) >= 65.0:
            b_idx = int(np.argmax(n_blur))
            by, bx = divmod(b_idx, w)
            x_pct = float(np.clip((float(bx) / float(w)) * 100.0, 22.0, 76.0))
            y_pct = float(np.clip((float(by) / float(h)) * 100.0, 14.0, 30.0))
        else:
            x_pct, y_pct = 64.0, 20.0

        corona_c = (
            255,
            int(np.clip(norm_g * 0.40 + 248 * 0.60, 232, 255)),
            int(np.clip(norm_b * 0.40 + 238 * 0.60, 215, 255)),
        )
        return {
            "time_of_day": self.TIME_NOON,
            "x_pct": round(x_pct, 1),
            "y_pct": round(y_pct, 1),
            "source_type": "NATURAL_DAYLIGHT_SUN",
            "label_ko": "맑은 하늘 태양 & 구름 햇살 광선",
            "core_rgb": (255, 255, 254),
            "corona_rgb": corona_c,
            "ray_rgb": (245, 250, 255),
            "has_water_reflection": False,
            "analysis_source": "AI Daylight Sun Analyzer",
        }

    def _query_gemini_light_source_async(
        self,
        cache_key: str,
        bg_file_path: Path,
        scene_hint: str,
    ) -> None:
        api_key = self.gemini_api_key
        if not api_key:
            return

        def _worker() -> None:
            try:
                pil_img = Image.open(bg_file_path).convert("RGB").resize((512, 288), Image.Resampling.BILINEAR)
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=82)
                b64_img = base64.b64encode(buf.getvalue()).decode("ascii")

                prompt_text = (
                    "Analyze this landscape image for natural sun or moon placement. "
                    "Context: " + str(scene_hint) + ". "
                    "Find the most natural sky position for the sun or moon at x_pct (20-80) and y_pct (14-48), "
                    "and natural light colors (core_rgb, corona_rgb, ray_rgb). "
                    "Return ONLY JSON: "
                    "{\"time_of_day\":\"MORNING|NOON|SUNSET|EVENING\","
                    "\"x_pct\":float,\"y_pct\":float,\"core_rgb\":[r,g,b],\"corona_rgb\":[r,g,b],"
                    "\"ray_rgb\":[r,g,b],\"label_ko\":string}"
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
                        with urllib.request.urlopen(req, timeout=5.0) as resp:
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
                        x_val = float(np.clip(float(parsed.get("x_pct", self.ai_x_pct)), 20.0, 80.0))
                        y_val = float(np.clip(float(parsed.get("y_pct", self.ai_y_pct)), 14.0, 48.0))
                        t_day = str(parsed.get("time_of_day", self.last_detected_time)).upper()
                        if t_day not in (self.TIME_MORNING, self.TIME_NOON, self.TIME_SUNSET, self.TIME_EVENING):
                            t_day = self.last_detected_time

                        with self._lock:
                            cached = dict(self._placement_cache.get(cache_key, {}))
                            cached["x_pct"] = round(x_val, 1)
                            cached["y_pct"] = round(y_val, 1)
                            cached["time_of_day"] = t_day
                            cached["analysis_source"] = "Gemini Vision AI"
                            if isinstance(parsed.get("core_rgb"), list) and len(parsed["core_rgb"]) == 3:
                                cached["core_rgb"] = tuple(int(np.clip(int(c), 0, 255)) for c in parsed["core_rgb"])
                            if isinstance(parsed.get("corona_rgb"), list) and len(parsed["corona_rgb"]) == 3:
                                cached["corona_rgb"] = tuple(int(np.clip(int(c), 0, 255)) for c in parsed["corona_rgb"])
                            if isinstance(parsed.get("ray_rgb"), list) and len(parsed["ray_rgb"]) == 3:
                                cached["ray_rgb"] = tuple(int(np.clip(int(c), 0, 255)) for c in parsed["ray_rgb"])
                            if parsed.get("label_ko"):
                                cached["label_ko"] = str(parsed["label_ko"])
                            self._placement_cache[cache_key] = cached
                            self._apply_placement_dict(cached)
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _apply_placement_dict(self, info: Dict[str, Any]) -> None:
        self.last_detected_time = str(info.get("time_of_day", self.TIME_MORNING))
        self.ai_x_pct = float(info.get("x_pct", 68.0))
        self.ai_y_pct = float(info.get("y_pct", 22.0))
        self.light_source_type = str(info.get("source_type", "NATURAL_SUN"))
        self.light_label_ko = str(info.get("label_ko", "자연 햇살"))
        self.ai_core_rgb = tuple(info.get("core_rgb", (255, 254, 248)))  # type: ignore
        self.ai_corona_rgb = tuple(info.get("corona_rgb", (255, 215, 140)))  # type: ignore
        self.ai_ray_rgb = tuple(info.get("ray_rgb", (255, 235, 185)))  # type: ignore
        self.ai_color_hex = self.rgb_to_hex(self.ai_corona_rgb)
        if self.auto_color:
            self.custom_color_hex = self.ai_color_hex
        self.has_water_reflection = bool(info.get("has_water_reflection", False))
        self.analysis_source = str(info.get("analysis_source", "AI Vision Analyzer"))

    # =========================================================================
    # 3. 트레이드 색상 추출 (Trade Color Extraction)
    # =========================================================================
    def extract_trade_color(
        self,
        illust_bgra: np.ndarray,
        alpha_threshold: int = 30,
    ) -> Tuple[int, int, int]:
        if illust_bgra is None or illust_bgra.ndim != 3 or illust_bgra.shape[2] != 4:
            raise ValueError("일러스트 이미지는 반드시 알파 채널을 포함한 4채널(BGRA) 형식이어야 합니다.")

        bgr_channels = illust_bgra[:, :, :3]
        alpha_channel = illust_bgra[:, :, 3]
        valid_mask = alpha_channel > int(alpha_threshold)
        foreground_pixels = bgr_channels[valid_mask]

        if foreground_pixels.shape[0] == 0:
            return (255, 255, 255)

        max_samples = 10000
        if foreground_pixels.shape[0] > max_samples:
            rng = np.random.default_rng(seed=42)
            indices = rng.choice(foreground_pixels.shape[0], size=max_samples, replace=False)
            sample_pixels = foreground_pixels[indices].astype(np.float32)
        else:
            sample_pixels = foreground_pixels.astype(np.float32)

        actual_clusters = min(self.n_clusters, sample_pixels.shape[0])
        kmeans = KMeans(n_clusters=actual_clusters, n_init=10, random_state=42)
        labels = kmeans.fit_predict(sample_pixels)
        centers = kmeans.cluster_centers_

        unique_labels, counts = np.unique(labels, return_counts=True)
        sorted_order = np.argsort(-counts)

        dominant_idx = int(unique_labels[sorted_order[0]])
        for rank_idx in sorted_order:
            cluster_id = int(unique_labels[rank_idx])
            ratio = float(counts[rank_idx]) / float(sample_pixels.shape[0])
            center_bgr = np.uint8([[centers[cluster_id]]])
            center_hsv = cv2.cvtColor(center_bgr, cv2.COLOR_BGR2HSV)[0, 0]
            if ratio >= 0.15 and int(center_hsv[1]) >= 35 and 35 <= int(center_hsv[2]) <= 245:
                dominant_idx = cluster_id
                break

        trade_bgr = centers[dominant_idx]
        trade_color = (
            int(np.clip(round(trade_bgr[0]), 0, 255)),
            int(np.clip(round(trade_bgr[1]), 0, 255)),
            int(np.clip(round(trade_bgr[2]), 0, 255)),
        )
        self.last_trade_color_bgr = trade_color
        return trade_color

    # =========================================================================
    # 4. 가장자리 및 자연 광원 투영 효과 (Edge Lighting)
    # =========================================================================
    def apply_edge_lighting(
        self,
        frame_bgr: np.ndarray,
        time_of_day: str,
        origin_corner: str = "top_right",
    ) -> np.ndarray:
        height, width = frame_bgr.shape[:2]
        x_lin = np.linspace(0.0, 1.0, width, dtype=np.float32)
        y_lin = np.linspace(0.0, 1.0, height, dtype=np.float32)
        xv, yv = np.meshgrid(x_lin, y_lin)

        sx = float(np.clip(self.light_x_pct / 100.0, 0.08, 0.92))
        sy = float(np.clip(self.light_y_pct / 100.0, 0.08, 0.85))
        aspect = float(width) / float(max(1, height))
        dx = (xv - sx) * aspect
        dy = yv - sy
        dist = np.sqrt(dx * dx + dy * dy)

        scale = float(self.intensity_pct) / 100.0
        core_mask = np.exp(-(dist * dist) / 0.008) * scale
        corona_mask = np.exp(-(dist * dist) / 0.065) * 0.48 * scale

        _, corona_rgb, _, _ = self.get_active_light_palettes()
        light_bgr = (corona_rgb[2], corona_rgb[1], corona_rgb[0])

        edge_light_layer = np.zeros_like(frame_bgr, dtype=np.uint8)
        for ch_idx in range(3):
            channel_energy = (
                core_mask * 255.0 * 0.90
                + corona_mask * float(light_bgr[ch_idx]) * self.edge_light_intensity
            )
            edge_light_layer[:, :, ch_idx] = np.clip(channel_energy, 0, 255).astype(np.uint8)

        return cv2.add(frame_bgr, edge_light_layer)

    # =========================================================================
    # 5. 은은한 발광 효과 (Soft Glow)
    # =========================================================================
    def apply_soft_glow(
        self,
        base_bgr: np.ndarray,
        illust_bgra: np.ndarray,
        trade_color_bgr: Tuple[int, int, int],
    ) -> np.ndarray:
        target_h, target_w = base_bgr.shape[:2]
        if illust_bgra.shape[0] != target_h or illust_bgra.shape[1] != target_w:
            resized_illust = cv2.resize(
                illust_bgra,
                (target_w, target_h),
                interpolation=cv2.INTER_AREA,
            )
        else:
            resized_illust = illust_bgra

        illust_bgr = resized_illust[:, :, :3].astype(np.float32)
        alpha_norm = (resized_illust[:, :, 3].astype(np.float32) / 255.0)[:, :, np.newaxis]

        silhouette_mask = resized_illust[:, :, 3].astype(np.float32) / 255.0
        color_mask = np.zeros_like(base_bgr, dtype=np.uint8)
        color_mask[:, :, 0] = np.clip(silhouette_mask * float(trade_color_bgr[0]), 0, 255).astype(np.uint8)
        color_mask[:, :, 1] = np.clip(silhouette_mask * float(trade_color_bgr[1]), 0, 255).astype(np.uint8)
        color_mask[:, :, 2] = np.clip(silhouette_mask * float(trade_color_bgr[2]), 0, 255).astype(np.uint8)

        blurred_glow = cv2.GaussianBlur(
            color_mask,
            self.glow_blur_ksize,
            sigmaX=self.glow_sigma,
            sigmaY=self.glow_sigma,
        )

        composited_scene = (
            illust_bgr * alpha_norm + base_bgr.astype(np.float32) * (1.0 - alpha_norm)
        ).astype(np.uint8)

        glowed_scene = cv2.addWeighted(
            composited_scene,
            1.0,
            blurred_glow,
            self.glow_weight,
            0.0,
        )

        core_alpha = np.power(alpha_norm, 1.4) * 0.65
        final_frame = (
            illust_bgr * core_alpha + glowed_scene.astype(np.float32) * (1.0 - core_alpha)
        )
        return np.clip(final_frame, 0, 255).astype(np.uint8)

    # =========================================================================
    # 통합 렌더링 파이프라인 (Full Pipeline Execution)
    # =========================================================================
    def process_frame(
        self,
        bg_bgr: np.ndarray,
        illust_bgra: np.ndarray,
        origin_corner: str = "top_right",
    ) -> Tuple[np.ndarray, Dict[str, object]]:
        if bg_bgr is None or illust_bgra is None:
            raise ValueError("배경 이미지(BGR)와 일러스트 이미지(BGRA)가 모두 필요합니다.")

        target_h, target_w = bg_bgr.shape[:2]
        if illust_bgra.shape[0] != target_h or illust_bgra.shape[1] != target_w:
            aligned_illust = cv2.resize(
                illust_bgra,
                (target_w, target_h),
                interpolation=cv2.INTER_AREA,
            )
        else:
            aligned_illust = illust_bgra

        if not self.is_enabled:
            fg_bgr = aligned_illust[:, :, :3].astype(np.float32)
            alpha = (aligned_illust[:, :, 3].astype(np.float32) / 255.0)[:, :, np.newaxis]
            basic_composite = (
                fg_bgr * alpha + bg_bgr.astype(np.float32) * (1.0 - alpha)
            ).astype(np.uint8)
            return basic_composite, {"coelux_enabled": False, "time_of_day": "N/A", "trade_color_bgr": None}

        detected_time = self.analyze_time_of_day(bg_bgr)
        placement = self._detect_light_source_cv(bg_bgr, detected_time)
        self._apply_placement_dict(placement)
        trade_color = self.extract_trade_color(aligned_illust)
        glow_composited = self.apply_soft_glow(bg_bgr, aligned_illust, trade_color)
        final_illuminated = self.apply_edge_lighting(
            glow_composited,
            time_of_day=detected_time,
            origin_corner=origin_corner,
        )
        return final_illuminated, {
            "coelux_enabled": True,
            "time_of_day": detected_time,
            "trade_color_bgr": trade_color,
            "light_x_pct": self.light_x_pct,
            "light_y_pct": self.light_y_pct,
        }

    # =========================================================================
    # 6. 음성/텍스트 제어 및 실시간 배경 파일 광원 분석
    # =========================================================================
    def apply_voice_command(self, raw_text: str) -> Optional[str]:
        text = str(raw_text).strip()
        if not text:
            return None

        upper_t = text.upper()
        if "TOGGLE_COELUX" in upper_t:
            self.handle_command(self.COMMAND_TOGGLE)
            return "shown" if self.is_enabled else "hidden"

        cleaned = text.lower()
        for ww in ("윈도우", "윈도", "window", "windo"):
            if cleaned.startswith(ww):
                cleaned = cleaned[len(ww):].strip()

        if any(v in cleaned for v in ("만들어", "그려", "생성", "찾아", "검색")):
            return None
        if any(c_kw in cleaned for c_kw in ("생체리듬", "생체 리듬", "자동 광원", "자동광원")):
            return None

        compact = cleaned.replace(" ", "")
        light_subjects = ("조명", "코에룩스", "coelux", "코에룩스조명", "창문조명", "불빛", "라이트", "불켜", "불꺼", "햇빛", "채광", "달빛")
        if not any(s in compact for s in light_subjects):
            return None

        # 1) 음성으로 조명 밝기 조절 ("조명 밝기 150", "조명 밝게", "조명 어둡게")
        nums = [int(n) for n in re.findall(r"\d+", cleaned)]
        if "밝기" in compact and nums:
            self.is_enabled = True
            self.set_light_controls(intensity_pct=nums[0])
            return "adjusted"
        if any(w in compact for w in ("더밝게", "밝게해", "강하게")):
            self.is_enabled = True
            self.set_light_controls(intensity_pct=self.intensity_pct + 30)
            return "adjusted"
        if any(w in compact for w in ("어둡게", "약하게", "은은하게")):
            self.is_enabled = True
            self.set_light_controls(intensity_pct=self.intensity_pct - 30)
            return "adjusted"

        # 2) 음성으로 조명 색상 변경 ("조명 노란색", "조명 주황색", "조명 하얀색", "조명 푸른색")
        color_map = {
            "황금": "#ffc86e",
            "노란": "#ffd166",
            "노랑": "#ffd166",
            "따뜻": "#ffb84d",
            "주황": "#ff8c42",
            "오렌지": "#ff8c42",
            "노을": "#ff7e38",
            "하얀": "#fffdf8",
            "백색": "#fffdf8",
            "화이트": "#fffdf8",
            "푸른": "#93c5fd",
            "파란": "#93c5fd",
            "달빛": "#b8d8ff",
        }
        for c_kw, c_hex in color_map.items():
            if c_kw in compact and ("색" in compact or "빛" in compact or "조명" in compact):
                self.is_enabled = True
                self.set_light_controls(color_hex=c_hex)
                return "adjusted"

        off_words = ("꺼줘", "꺼", "끄기", "끄자", "오프", "off", "숨겨", "닫아", "중지")
        on_words = ("켜줘", "켜", "켜기", "켜자", "온", "on", "틀어", "적용", "시작", "보여")
        toggle_words = ("토글", "전환", "바꿔")

        if any(w in compact for w in off_words):
            self.is_enabled = False
            return "hidden"
        if any(w in compact for w in on_words):
            self.is_enabled = True
            return "shown"
        if any(w in compact for w in toggle_words) or compact in ("조명", "코에룩스", "coelux"):
            self.handle_command(self.COMMAND_TOGGLE)
            return "shown" if self.is_enabled else "hidden"
        return None

    def reset_to_default(self) -> None:
        """코에룩스 조명 상태를 초기 기본값(OFF, 밝기 100%, AI 자동 색상·위치)으로 초기화합니다."""
        with self._lock:
            self.is_enabled = False
            self.intensity_pct = 100
            self.auto_color = True
            self.auto_position = True
            self.custom_x_pct = None
            self.custom_y_pct = None
            self.last_detected_time = self.TIME_MORNING
            self.ai_x_pct = 68.0
            self.ai_y_pct = 22.0
            self.ai_core_rgb = (255, 254, 248)
            self.ai_corona_rgb = (255, 215, 140)
            self.ai_ray_rgb = (255, 235, 185)
            self.ai_color_hex = "#ffd78c"
            self.custom_color_hex = "#ffd78c"
            self.light_source_type = "NATURAL_SUN"
            self.light_label_ko = "자연 햇살"
            self.has_water_reflection = False

    def set_enabled(self, enabled: bool) -> bool:
        self.is_enabled = bool(enabled)
        return self.is_enabled

    def toggle(self) -> bool:
        return self.handle_command(self.COMMAND_TOGGLE)

    def analyze_background_file(self, bg_file_path: Optional[Path], scene_hint: str = "") -> str:
        if bg_file_path is None or not bg_file_path.exists():
            return self.last_detected_time

        path_str = str(bg_file_path)
        cache_key = "{}|{}".format(path_str, scene_hint)

        with self._lock:
            if cache_key in self._placement_cache:
                self._apply_placement_dict(self._placement_cache[cache_key])
                return self.last_detected_time

        try:
            pil_img = Image.open(bg_file_path).convert("RGB").resize((320, 180), Image.Resampling.BILINEAR)
            rgb_arr = np.array(pil_img, dtype=np.uint8)
            bgr_arr = rgb_arr[:, :, ::-1].copy()

            detected_time = self.analyze_time_of_day(bgr_arr, scene_hint=scene_hint)
            placement = self._detect_light_source_cv(bgr_arr, detected_time)

            h, w = bgr_arr.shape[:2]
            bgra_sample = np.zeros((h, w, 4), dtype=np.uint8)
            bgra_sample[:, :, :3] = bgr_arr
            bgra_sample[: int(h * 0.65), :, 3] = 255
            self.extract_trade_color(bgra_sample)

            with self._lock:
                self._placement_cache[cache_key] = placement
                self._apply_placement_dict(placement)
                self._last_analyzed_bg_path = path_str
                self._last_scene_hint = scene_hint

            self._query_gemini_light_source_async(cache_key, bg_file_path, scene_hint)
        except Exception:
            pass
        return self.last_detected_time

    def get_placement_state(self) -> Dict[str, Any]:
        with self._lock:
            sx = self.light_x_pct
            sy = self.light_y_pct
            core_rgb, corona_rgb, ray_rgb, active_hex = self.get_active_light_palettes()
            cr, cg, cb = core_rgb
            hr, hg, hb = corona_rgb
            rr, rg, rb = ray_rgb
            tr, tg, tb = self.get_trade_color_rgb()
            sunbeams = self._build_organic_sunbeams(sx, sy, self.last_detected_time)
            scale = float(self.intensity_pct) / 100.0

            return {
                "enabled": self.is_enabled,
                "time_of_day": self.last_detected_time,
                "x_pct": sx,
                "y_pct": sy,
                "ai_x_pct": self.ai_x_pct,
                "ai_y_pct": self.ai_y_pct,
                "sunbeams": sunbeams,
                "has_water_reflection": self.has_water_reflection,
                "intensity_pct": self.intensity_pct,
                "intensity_scale": round(scale, 2),
                "auto_color": self.auto_color,
                "auto_position": self.auto_position,
                "active_color_hex": active_hex,
                "ai_color_hex": self.ai_color_hex,
                "source_type": self.light_source_type,
                "label_ko": self.light_label_ko,
                "core_rgb": "{},{},{}".format(cr, cg, cb),
                "corona_rgb": "{},{},{}".format(hr, hg, hb),
                "ray_rgb": "{},{},{}".format(rr, rg, rb),
                "trade_rgb": "{},{},{}".format(tr, tg, tb),
                "analysis_source": self.analysis_source,
            }

    def get_edge_light_rgb(self) -> Tuple[int, int, int]:
        _, corona_rgb, _, _ = self.get_active_light_palettes()
        return corona_rgb

    def get_trade_color_rgb(self) -> Tuple[int, int, int]:
        b, g, r = self.last_trade_color_bgr
        return (int(r), int(g), int(b))

    def get_trade_color_hex(self) -> str:
        r, g, b = self.get_trade_color_rgb()
        return "#{:02x}{:02x}{:02x}".format(r, g, b)

    def get_edge_light_hex(self) -> str:
        r, g, b = self.get_edge_light_rgb()
        return "#{:02x}{:02x}{:02x}".format(r, g, b)


if __name__ == "__main__":
    coelux = VirtualCoeLuxSystem()
    coelux.handle_command("TOGGLE_COELUX")
    print("CoeLux Initialized:", coelux.get_placement_state()["label_ko"])
