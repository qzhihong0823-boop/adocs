"""
현재 창문에 띄워져 있는 사진 기준 3단계 시간대 타임랩스 생성 & 자동 순환 엔진
(src/modules/scene_ingredients_engine.py)

핵심 동작:
1. 오직 현재 창문에 띄워져 있는 사진 1장을 기준으로 동작합니다 (외부 예시 사진 사용 안 함).
2. 현재 창문 사진의 시간대(아침 / 노을 / 저녁)를 분석하여 3단계 순환 순서를 결정합니다:
   - 현재 사진이 아침(morning)이면: 1단계 아침(원본 안바뀜) ➔ 2단계 노을 ➔ 3단계 저녁
   - 현재 사진이 노을(sunset)이면:  1단계 노을(원본 안바뀜) ➔ 2단계 저녁 ➔ 3단계 아침
   - 현재 사진이 저녁(evening)이면: 1단계 저녁(원본 안바뀜) ➔ 2단계 아침 ➔ 3단계 노을
3. 타임랩스 만들기 실행 시:
   - Step 1/3: 현재 사진 시간대 분석 및 1단계 원본 이미지(안 바뀜) 준비
   - Step 2/3: 2단계 다음 시간대 이미지 생성 (Gemini 3 API 연동 + 정밀 시간대 조명 변환)
   - Step 3/3: 3단계 마지막 시간대 이미지 생성 (Gemini 3 API 연동 + 정밀 시간대 조명 변환)
   - 위 3개의 이미지가 모두 완성되어야 비로소 타임랩스가 실행됩니다!
4. 타임랩스 실행 시:
   - 동영상 플레이어나 조절 바 없이, 완성된 3개의 이미지가 설정창에서 지정한 배속(일정 시간 간격)마다
     1단계(원본) ➔ 2단계 ➔ 3단계 ➔ 1단계(원본) 순으로 창문 화면에서 자동 전환됩니다.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter

BASE_DIR = Path(__file__).resolve().parents[2]


def _bgr_to_hsv_np(bgr: np.ndarray) -> np.ndarray:
    """OpenCV 없이 PIL을 이용해 BGR uint8 이미지를 OpenCV 호환 HSV(H:0~180, S:0~255, V:0~255)로 변환합니다."""
    rgb = bgr[:, :, ::-1]
    pil_hsv = Image.fromarray(rgb, mode="RGB").convert("HSV")
    hsv = np.asarray(pil_hsv, dtype=np.float32).copy()
    hsv[:, :, 0] = hsv[:, :, 0] * (180.0 / 255.0)
    return hsv


def _gaussian_blur_2d(arr_2d: np.ndarray, radius: float = 2.0) -> np.ndarray:
    """OpenCV 없이 PIL GaussianBlur로 2D float 배열(0.0~1.0)을 부드럽게 블러링합니다."""
    u8 = np.clip(arr_2d * 255.0, 0.0, 255.0).astype(np.uint8)
    pil_l = Image.fromarray(u8, mode="L").filter(ImageFilter.GaussianBlur(radius=float(radius)))
    return np.asarray(pil_l, dtype=np.float32) / 255.0


# 현재 사진의 시간대에 따른 3단계 변화 순서 정의
# - 아침(morning) 기준: 아침(안바뀜) -> 노을 -> 저녁
# - 노을(sunset) 기준:  노을(안바뀜) -> 저녁 -> 아침
# - 저녁(evening) 기준: 저녁(안바뀜) -> 아침 -> 노을
PHASE_SEQUENCE_MAP: Dict[str, List[str]] = {
    "morning": ["morning", "sunset", "evening"],
    "sunset": ["sunset", "evening", "morning"],
    "evening": ["evening", "morning", "sunset"],
}

PHASE_LABEL_KO: Dict[str, str] = {
    "morning": "🌅 아침",
    "sunset": "🌇 노을",
    "evening": "🌙 저녁",
}

# 배속(Speed)별 다음 사진으로 넘어가는 시간 간격(초)
SPEED_INTERVAL_MAP: Dict[float, float] = {
    1.0: 8.0,   # 1x: 8초마다 다음 사진으로 변경
    2.0: 4.0,   # 2x: 4초마다 다음 사진으로 변경
    5.0: 2.0,   # 5x: 2초마다 다음 사진으로 변경
    10.0: 1.0,  # 10x: 1초마다 다음 사진으로 변경
}


@dataclass
class TimelapseStepState:
    """타임랩스 이미지 3개(Step 1/3, Step 2/3, Step 3/3) 개별 생성 상태"""
    step_num: int          # 1, 2, 3
    phase_key: str         # "morning" | "sunset" | "evening"
    label_ko: str          # 예: "1단계: 🌅 아침 (원본 안바뀜)"
    status: str = "idle"   # "idle" | "running" | "done" | "error"
    progress_pct: int = 0  # 0 ~ 100
    image_url: str = ""    # 완성된 이미지 URL (완성 전에는 "")
    detail_ko: str = "대기 중"


@dataclass
class SceneIngredientsBundle:
    """완성된 3개의 시간대 이미지 번들 데이터"""
    bundle_id: str
    title: str
    source_image_name: str
    created_at: str
    api_mode: str
    base_phase: str                    # 현재 사진의 분석된 시간대: "morning" | "sunset" | "evening"
    phase_order: List[str]             # 예: ["sunset", "evening", "morning"]
    phase_order_ko: str                # 예: "🌇 노을(원본 안바뀜) ➔ 🌙 저녁 ➔ 🌅 아침"
    textures: Dict[str, str] = field(default_factory=dict)  # stage0, stage1, stage2, morning, sunset, evening

    def to_dict(self) -> Dict[str, Any]:
        labels_ko = [
            f"{PHASE_LABEL_KO.get(p, p)}{' (원본 안바뀜)' if idx == 0 else ''}"
            for idx, p in enumerate(self.phase_order)
        ]
        return {
            "bundle_id": self.bundle_id,
            "title": self.title,
            "source_image_name": self.source_image_name,
            "created_at": self.created_at,
            "api_mode": self.api_mode,
            "base_phase": self.base_phase,
            "base_phase_label_ko": PHASE_LABEL_KO.get(self.base_phase, "🌅 아침"),
            "phase_order": self.phase_order,
            "phase_labels_ko": labels_ko,
            "phase_order_labels_ko": labels_ko,
            "phase_order_ko": self.phase_order_ko,
            "textures": self.textures,
        }


class SceneIngredientsEngine:
    """
    현재 창문에 띄워져 있는 사진 1장을 기준으로:
    1) 현재 사진의 시간대(아침 / 노을 / 저녁)를 분석하고,
    2) 현재 시간대 이미지(1단계 원본, 안 바뀜) + 다른 두 시간대 이미지(2단계, 3단계) 총 3개를 순차 생성한 뒤,
    3) 이미지 3개가 모두 완성되면 설정된 배속(시간 간격)마다 다음 시간대 사진으로 창문 배경을 자동 전환합니다.
    """

    DEFAULT_GEMINI_API_KEY = "AQ.Ab8RN6IZwGOGjcJnOJSzxNnvym-uYhRSsHpjiDrR9GpbGrG2Ug"

    GEMINI_3_VISION_MODELS: Tuple[str, ...] = (
        "gemini-3-flash-preview",
        "gemini-3.1-pro-preview",
        "gemini-2.5-flash",
    )
    GEMINI_3_IMAGE_MODELS: Tuple[str, ...] = (
        "gemini-3-pro-image-preview",
        "gemini-3-pro-image",
        "gemini-3.1-flash-image-preview",
        "gemini-3.1-flash-image",
        "gemini-2.5-flash-image",
    )

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        mp3_dir: Optional[Path] = None,
        gemini_api_key: Optional[str] = None,
    ) -> None:
        self.base_dir = base_dir or BASE_DIR
        self.mp3_dir = mp3_dir or (self.base_dir / "mp3")
        self.cache_root = self.base_dir / ".cache" / "ingredients"
        self.cache_root.mkdir(parents=True, exist_ok=True)

        self.gemini_api_key: str = (
            gemini_api_key
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or self.DEFAULT_GEMINI_API_KEY
        )

        self._lock = threading.RLock()
        self.is_active: bool = False          # 이미지 3개가 모두 완성되어 타임랩스가 작동 중인지 여부
        self.is_generating: bool = False      # 현재 이미지 3개를 생성 중인지 여부
        self.generation_progress_pct: int = 0 # 전체 생성 진행률 (0 ~ 100%)
        self.generation_status_text: str = "대기 중 (현재 창문 사진으로 타임랩스를 만들 수 있습니다)"
        self.current_step_num: int = 0        # 1, 2, 3
        self.steps: List[TimelapseStepState] = [
            TimelapseStepState(1, "morning", "현재 사진 (원본 안바뀜)", "idle", 0, "", "대기 중"),
            TimelapseStepState(2, "sunset", "다음 시간대 이미지 생성", "idle", 0, "", "대기 중"),
            TimelapseStepState(3, "evening", "마지막 시간대 이미지 생성", "idle", 0, "", "대기 중"),
        ]

        self.active_bundle: Optional[SceneIngredientsBundle] = None
        self._bundles_cache: Dict[str, SceneIngredientsBundle] = {}

        # 타임랩스 사진 전환 상태
        self.speed_multiplier: float = 2.0    # 기본 2x (4초마다 다음 사진으로 변경)
        self.current_stage_idx: int = 0       # 0(1단계 원본) -> 1(2단계) -> 2(3단계)
        self.previous_image_url: str = ""
        self.last_switch_ts: float = 0.0
        self.switch_version: int = 0

    # =========================================================================
    # 1. 유니코드(한글 경로) 안전 이미지 I/O
    # =========================================================================
    @staticmethod
    def _imread_unicode(path: Path, max_width: int = 1600) -> Optional[np.ndarray]:
        if not path.exists() or not path.is_file():
            return None
        try:
            pil_img = Image.open(path).convert("RGB")
            w, h = pil_img.size
            if w > max_width and w > 0:
                new_h = max(64, int(round(h * (max_width / float(w)))))
                pil_img = pil_img.resize((max_width, new_h), Image.Resampling.LANCZOS)
            rgb = np.asarray(pil_img, dtype=np.uint8)
            return rgb[:, :, ::-1].copy()
        except Exception:
            return None

    @staticmethod
    def _imwrite_unicode(path: Path, img: np.ndarray, quality: int = 92) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if img.ndim == 2:
                pil_img = Image.fromarray(img, mode="L")
            else:
                rgb = img[:, :, ::-1]
                pil_img = Image.fromarray(rgb, mode="RGB")
            ext = path.suffix.lower()
            if ext == ".png":
                pil_img.save(path, format="PNG")
            else:
                pil_img.save(path, format="JPEG", quality=int(quality), subsampling=0)
            return True
        except Exception:
            return False

    # =========================================================================
    # 2. 현재 사진의 시간대(아침 / 노을 / 저녁) 정밀 판별
    # =========================================================================
    @staticmethod
    def _detect_current_photo_phase_cv(
        img_bgr: np.ndarray,
        title_hint: str = "",
        active_preset_key: str = "",
    ) -> str:
        combined_hint = f"{title_hint} {active_preset_key}".lower()

        if any(k in combined_hint for k in ("노을", "석양", "일몰", "황혼", "해질", "sunset", "dusk")):
            return "sunset"
        if any(k in combined_hint for k in ("저녁", "저녘", "밤", "야경", "자정", "새녁", "달빛", "은하수", "오로라", "evening", "night")):
            return "evening"
        if any(k in combined_hint for k in ("아침", "일출", "낮", "정오", "주간", "morning", "noon")):
            return "morning"

        hsv = _bgr_to_hsv_np(img_bgr)
        h_rows = hsv.shape[0]
        sky_hsv = hsv[: max(16, int(h_rows * 0.50)), :, :]
        sky_bgr = img_bgr[: max(16, int(h_rows * 0.50)), :, :].astype(np.float32)

        mean_v = float(np.mean(sky_hsv[:, :, 2]))
        full_v = float(np.mean(hsv[:, :, 2]))
        mean_r = float(np.mean(sky_bgr[:, :, 2]))
        mean_g = float(np.mean(sky_bgr[:, :, 1]))
        mean_b = float(np.mean(sky_bgr[:, :, 0]))

        if full_v < 78.0 or mean_v < 85.0:
            return "evening"

        h_ch = sky_hsv[:, :, 0]
        s_ch = sky_hsv[:, :, 1]
        v_ch = sky_hsv[:, :, 2]
        warm_sunset_mask = (((h_ch <= 24) | (h_ch >= 145)) & (s_ch >= 68) & (v_ch >= 80))
        warm_ratio = float(np.count_nonzero(warm_sunset_mask)) / float(h_ch.size)

        if warm_ratio >= 0.24 or (mean_r - mean_b >= 32.0 and mean_r > mean_g * 1.12):
            return "sunset"

        return "morning"

    @staticmethod
    def _detect_sky_and_horizon(img_bgr: np.ndarray) -> Tuple[np.ndarray, float, float, float]:
        h, w = img_bgr.shape[:2]
        small_w, small_h = 320, 180
        pil_small = Image.fromarray(img_bgr[:, :, ::-1], mode="RGB").resize(
            (small_w, small_h), Image.Resampling.BILINEAR
        )
        small = np.asarray(pil_small, dtype=np.uint8)[:, :, ::-1]
        hsv = _bgr_to_hsv_np(small)
        v_ch = hsv[:, :, 2] / 255.0

        gray = np.mean(small.astype(np.float32), axis=2)
        gy, gx = np.gradient(gray)
        grad_mag = np.sqrt(gx * gx + gy * gy)

        row_grad = np.mean(grad_mag, axis=1)
        search_top = int(small_h * 0.22)
        search_bot = int(small_h * 0.72)
        horizon_row = search_top + int(np.argmax(row_grad[search_top:search_bot]))
        horizon_y_pct = float(np.clip((horizon_row / float(small_h)) * 100.0, 30.0, 68.0))

        y_grid = np.linspace(0.0, 1.0, small_h, dtype=np.float32)[:, None]
        hz_norm = horizon_y_pct / 100.0
        pos_prior = np.clip((hz_norm + 0.08 - y_grid) / 0.20, 0.0, 1.0)
        smooth_prior = np.clip(1.0 - (grad_mag / 48.0), 0.15, 1.0)
        sky_prob_small = np.clip(pos_prior * (0.65 + 0.35 * smooth_prior), 0.0, 1.0)
        sky_prob_small = _gaussian_blur_2d(sky_prob_small, radius=4.5)

        pil_mask = Image.fromarray(
            np.clip(sky_prob_small * 255.0, 0.0, 255.0).astype(np.uint8),
            mode="L",
        ).resize((w, h), Image.Resampling.BILINEAR)
        sky_mask = np.asarray(pil_mask, dtype=np.float32) / 255.0

        sky_cutoff_r = max(12, int(small_h * min(0.55, hz_norm)))
        sky_v = v_ch[:sky_cutoff_r, :]
        thresh = float(np.percentile(sky_v, 96))
        ys, xs = np.where(sky_v >= thresh)
        if len(xs) > 0:
            sun_x_pct = float(np.clip((np.mean(xs) / float(small_w)) * 100.0, 15.0, 85.0))
            sun_y_pct = float(np.clip((np.mean(ys) / float(small_h)) * 100.0, 12.0, 48.0))
        else:
            sun_x_pct, sun_y_pct = 28.0, 26.0

        return sky_mask, horizon_y_pct, sun_x_pct, sun_y_pct

    # =========================================================================
    # 3. Gemini 3 API 시간대 분석 & 이미지 생성 호출
    # =========================================================================
    def _query_gemini_3_phase(
        self,
        image_path: Path,
        title_hint: str = "",
        timeout_sec: float = 3.5,
    ) -> Optional[Tuple[str, str]]:
        """Gemini 3 API로 현재 사진의 시간대(morning | sunset | evening)를 판별합니다."""
        api_key = self.gemini_api_key
        if not api_key or not image_path.exists():
            return None

        try:
            pil_img = Image.open(image_path).convert("RGB")
            pil_img.thumbnail((512, 512), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=82)
            b64_img = base64.b64encode(buf.getvalue()).decode("ascii")

            prompt = (
                "Analyze this window background image and classify its current time of day into one of: "
                "'morning' (morning or daytime), 'sunset' (golden hour / sunset / dusk), or 'evening' (night / dark evening).\n"
                f"Title hint: {title_hint}\n"
                'Return strict JSON only: {"detected_phase": "morning|sunset|evening"}'
            )
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": prompt},
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

            for model_name in self.GEMINI_3_VISION_MODELS:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
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
                    phase = str(parsed.get("detected_phase", "")).strip().lower()
                    if phase in ("morning", "sunset", "evening"):
                        return phase, model_name
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _try_gemini_3_image_generation(
        self,
        source_bgr: np.ndarray,
        target_phase: str,
        timeout_sec: float = 4.0,
    ) -> Optional[Tuple[np.ndarray, str]]:
        """
        Gemini 3 이미지 생성/편집 모델(`gemini-3-pro-image-preview`, `gemini-3.1-flash-image-preview`)을 호출해
        원본 사진의 구도를 유지하면서 목표 시간대(`morning` | `sunset` | `evening`) 이미지를 생성합니다.
        """
        api_key = self.gemini_api_key
        if not api_key:
            return None

        phase_prompts = {
            "morning": (
                "Keep the exact same camera angle, landscape, buildings, and composition of this image, "
                "and transform the lighting into a bright, clear morning sunrise scene with warm morning sunlight."
            ),
            "sunset": (
                "Keep the exact same camera angle, landscape, buildings, and composition of this image, "
                "and transform the lighting into a dramatic golden-hour sunset scene with orange and violet sunset sky."
            ),
            "evening": (
                "Keep the exact same camera angle, landscape, buildings, and composition of this image, "
                "and transform the lighting into a deep night/evening scene with dark night sky and warm glowing lights."
            ),
        }
        prompt_text = phase_prompts.get(target_phase, phase_prompts["sunset"])

        try:
            h, w = source_bgr.shape[:2]
            pil_in = Image.fromarray(source_bgr[:, :, ::-1], mode="RGB").resize((768, 432), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            pil_in.save(buf, format="JPEG", quality=85)
            b64_in = base64.b64encode(buf.getvalue()).decode("ascii")

            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": prompt_text},
                            {"inlineData": {"mimeType": "image/jpeg", "data": b64_in}},
                        ],
                    }
                ],
                "generationConfig": {
                    "responseModalities": ["IMAGE", "TEXT"],
                },
            }
            body_bytes = json.dumps(payload).encode("utf-8")

            for model_name in self.GEMINI_3_IMAGE_MODELS[:2]:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                try:
                    req = urllib.request.Request(
                        url,
                        data=body_bytes,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    for p in parts:
                        inline = p.get("inlineData")
                        if inline and inline.get("data"):
                            raw_bytes = base64.b64decode(inline["data"])
                            out_pil = Image.open(io.BytesIO(raw_bytes)).convert("RGB").resize(
                                (w, h), Image.Resampling.LANCZOS
                            )
                            out_rgb = np.asarray(out_pil, dtype=np.uint8)
                            return out_rgb[:, :, ::-1].copy(), model_name
                except Exception:
                    continue
        except Exception:
            pass
        return None

    # =========================================================================
    # 4. 단일 목표 시간대(morning / sunset / evening) 이미지 생성
    # =========================================================================
    @classmethod
    def _synthesize_single_phase_variant(
        cls,
        img_bgr: np.ndarray,
        base_phase: str,
        target_phase: str,
        sky_mask: np.ndarray,
        horizon_y_pct: float,
        sun_x_pct: float,
        sun_y_pct: float,
    ) -> np.ndarray:
        """
        원본 사진(`img_bgr`)에서 목표 시간대(`target_phase`) 이미지를 생성합니다.
        `target_phase == base_phase`이면 100% 원본 그대로 반환합니다(절대 안 바뀜).
        """
        if target_phase == base_phase:
            return img_bgr.copy()

        h, w = img_bgr.shape[:2]
        base_f = img_bgr.astype(np.float32) / 255.0
        sky_3d = sky_mask[:, :, None]
        ground_3d = 1.0 - sky_3d

        y_norm = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
        x_norm = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
        hz_norm = float(np.clip(horizon_y_pct / 100.0, 0.25, 0.75))
        sx_norm = float(np.clip(sun_x_pct / 100.0, 0.1, 0.9))
        sy_norm = float(np.clip(sun_y_pct / 100.0, 0.08, 0.65))

        dist_sq = ((x_norm - sx_norm) * 1.35) ** 2 + ((y_norm - sy_norm) * 1.8) ** 2
        sun_glow = np.exp(-dist_sq / (2.0 * (0.24 ** 2)))[:, :, None]
        horizon_band = np.exp(-((y_norm - hz_norm) ** 2) / (2.0 * (0.13 ** 2)))[:, :, None]

        luma = (
            0.114 * base_f[:, :, 0]
            + 0.587 * base_f[:, :, 1]
            + 0.299 * base_f[:, :, 2]
        )[:, :, None]

        if base_phase == "evening":
            lifted_luma = np.clip(luma ** 0.52, 0.0, 1.0)
            color_boost = np.clip(base_f ** 0.62, 0.0, 1.0)
            neutral_sky_grad = (
                np.clip(1.0 - (y_norm / max(0.2, hz_norm)) * 0.35, 0.55, 1.0)[:, :, None]
                * np.array([0.96, 0.82, 0.68], dtype=np.float32)
            )
            neutral_day_f = np.clip(
                sky_3d * (color_boost * 0.55 + neutral_sky_grad * 0.45)
                + ground_3d * (color_boost * 0.75 + lifted_luma * np.array([0.78, 0.84, 0.86], dtype=np.float32) * 0.35),
                0.0,
                1.0,
            )
        elif base_phase == "sunset":
            neutral_sky = (
                luma * 0.72 * np.array([1.02, 0.94, 0.84], dtype=np.float32)
                + base_f * np.array([1.18, 1.02, 0.78], dtype=np.float32) * 0.35
            )
            neutral_ground = (base_f ** 0.85) * np.array([1.08, 1.04, 0.92], dtype=np.float32)
            neutral_day_f = np.clip(sky_3d * neutral_sky + ground_3d * neutral_ground, 0.0, 1.0)
        else:
            neutral_day_f = base_f

        if target_phase == "morning":
            morning_sky = neutral_day_f * np.array([0.95, 1.03, 1.12], dtype=np.float32)
            morning_sun_add = (
                (sun_glow * 0.38 + horizon_band * 0.24)
                * sky_3d
                * np.array([0.16, 0.38, 0.56], dtype=np.float32)
            )
            morning_ground = (neutral_day_f ** 0.92) * np.array([0.94, 1.02, 1.08], dtype=np.float32) + (
                sun_glow * ground_3d * np.array([0.04, 0.09, 0.15], dtype=np.float32)
            )
            morning_f = np.clip(sky_3d * (morning_sky + morning_sun_add) + ground_3d * morning_ground, 0.0, 1.0)
            return (morning_f * 255.0).astype(np.uint8)

        if target_phase == "sunset":
            src_for_sunset = neutral_day_f
            src_luma = (
                0.114 * src_for_sunset[:, :, 0]
                + 0.587 * src_for_sunset[:, :, 1]
                + 0.299 * src_for_sunset[:, :, 2]
            )[:, :, None]
            cloud_bright = np.clip((src_luma - 0.35) / 0.55, 0.0, 1.0)
            upper_sky_weight = np.clip((hz_norm - y_norm) / max(0.15, hz_norm), 0.0, 1.0)[:, :, None]

            sunset_violet_top = src_for_sunset * np.array([0.86, 0.62, 0.96], dtype=np.float32)
            sunset_orange_low = (
                src_for_sunset * np.array([0.45, 0.78, 1.28], dtype=np.float32)
                + cloud_bright * np.array([0.08, 0.38, 0.72], dtype=np.float32)
            )
            sunset_sky_mix = upper_sky_weight * sunset_violet_top + (1.0 - upper_sky_weight) * sunset_orange_low
            sunset_core_add = (
                (horizon_band * 0.52 + sun_glow * 0.45)
                * sky_3d
                * np.array([0.06, 0.36, 0.74], dtype=np.float32)
            )
            sunset_ground = (src_for_sunset ** 1.12) * np.array([0.74, 0.78, 0.98], dtype=np.float32) + (
                horizon_band * ground_3d * np.array([0.04, 0.11, 0.24], dtype=np.float32)
            )
            sunset_f = np.clip(sky_3d * (sunset_sky_mix + sunset_core_add) + ground_3d * sunset_ground, 0.0, 1.0)
            return (sunset_f * 255.0).astype(np.uint8)

        # target_phase == "evening"
        luma_2d = luma[:, :, 0]
        blur_fine = _gaussian_blur_2d(luma_2d, radius=1.6)
        blur_coarse = _gaussian_blur_2d(luma_2d, radius=4.5)
        highpass_fine = np.clip(luma_2d - blur_fine, 0.0, 1.0)
        highpass_mid = np.clip(blur_fine - blur_coarse, 0.0, 1.0)

        raw_sparks = (highpass_fine * 3.2 + highpass_mid * 1.8) * ground_3d[:, :, 0]
        ground_dist_weight = np.clip((y_norm - hz_norm + 0.02) / 0.12, 0.0, 1.0) * np.clip(
            (0.96 - y_norm) / 0.20, 0.25, 1.0
        )
        raw_sparks = raw_sparks * ground_dist_weight

        spark_thresh = (
            float(np.percentile(raw_sparks[y_norm[:, 0] > hz_norm], 86))
            if np.any(y_norm[:, 0] > hz_norm)
            else 0.08
        )
        spark_mask = np.clip((raw_sparks - spark_thresh * 0.75) / max(0.04, spark_thresh * 1.5), 0.0, 1.0)
        spark_glow = _gaussian_blur_2d(spark_mask, radius=2.2)[:, :, None]
        spark_core = (spark_mask ** 1.4)[:, :, None]

        warm_city_light = (
            spark_core * np.array([0.62, 0.92, 1.00], dtype=np.float32)
            + spark_glow * np.array([0.18, 0.52, 0.85], dtype=np.float32)
        )

        night_sky = (neutral_day_f ** 1.35) * np.array([0.58, 0.34, 0.20], dtype=np.float32)
        night_ground = (neutral_day_f ** 1.75) * np.array([0.22, 0.16, 0.12], dtype=np.float32)
        evening_f = np.clip(
            sky_3d * night_sky + ground_3d * night_ground + warm_city_light * 1.12,
            0.0,
            1.0,
        )
        return (evening_f * 255.0).astype(np.uint8)

    # =========================================================================
    # 5. 단계별 이미지 생성 (Step 1/3 -> Step 2/3 -> Step 3/3 완성 후 타임랩스 실행!)
    # =========================================================================
    def start_timelapse_generation_async(
        self,
        image_path: Optional[Path],
        title: str = "현재 창문 풍경",
        active_preset_key: str = "",
        try_gemini_api: bool = True,
        log_callback: Optional[Callable[..., None]] = None,
        on_complete: Optional[Callable[[SceneIngredientsBundle], None]] = None,
    ) -> bool:
        """
        백그라운드 스레드에서 3개의 시간대 이미지를 순차적으로 준비/생성합니다.
        - 생성 중에는 `is_generating = True`, `is_active = False` 상태를 유지하여
          창문 화면과 설정창 양쪽에 이미지 생성 로딩 과정(Step 1/3 -> Step 2/3 -> Step 3/3)을 실시간 표시합니다.
        - 이미지 3개가 모두 완성되면 `is_generating = False`, `is_active = True`로 전환하여
          그때부터 일정 시간 간격마다 다음 시간대 사진으로 바뀌는 타임랩스를 실행합니다!
        """
        with self._lock:
            if self.is_generating:
                return False
            self.is_generating = True
            self.is_active = False
            self.generation_progress_pct = 8
            self.current_step_num = 1
            self.generation_status_text = f"Step 1/3 · 현재 창문 사진 [{title}] 시간대 분석 및 원본 준비 중..."
            self.steps = [
                TimelapseStepState(1, "morning", "현재 사진 시간대 분석 중...", "running", 25, "", "시간대 분석 중"),
                TimelapseStepState(2, "sunset", "다음 시간대 이미지 대기", "idle", 0, "", "대기 중"),
                TimelapseStepState(3, "evening", "마지막 시간대 이미지 대기", "idle", 0, "", "대기 중"),
            ]

        def _worker() -> None:
            try:
                bundle = self._run_three_step_generation(
                    image_path=image_path,
                    title=title,
                    active_preset_key=active_preset_key,
                    try_gemini_api=try_gemini_api,
                    step_delay_sec=0.55,
                )
                if log_callback:
                    try:
                        log_callback(
                            "SYSTEM",
                            f"✅ 타임랩스 이미지 3개 완성 및 실행 시작: [{bundle.title}] ({bundle.phase_order_ko})",
                            f"배속: {self.speed_multiplier:g}x ({self.get_interval_sec():.0f}초 간격 전환) | {bundle.api_mode}",
                        )
                    except Exception:
                        pass
                if bundle and on_complete:
                    try:
                        on_complete(bundle)
                    except Exception:
                        pass
            except Exception as exc:
                with self._lock:
                    self.is_generating = False
                    self.generation_status_text = f"⚠️ 타임랩스 이미지 생성 오류: {exc}"

        threading.Thread(target=_worker, name="Timelapse3ImageGenThread", daemon=True).start()
        return True

    def _run_three_step_generation(
        self,
        image_path: Optional[Path],
        title: str = "현재 창문 풍경",
        active_preset_key: str = "",
        try_gemini_api: bool = True,
        step_delay_sec: float = 0.0,
    ) -> SceneIngredientsBundle:
        target_path = image_path
        if not target_path or not target_path.exists():
            target_path = self.base_dir / "assets" / "images" / "presets" / "default.png"

        stat = target_path.stat()
        raw_hash = hashlib.md5(
            f"{target_path.resolve()}_{stat.st_size}_{stat.st_mtime_ns}_{active_preset_key}".encode("utf-8")
        ).hexdigest()[:12]
        bundle_id = f"tl_{raw_hash}"
        out_dir = self.cache_root / bundle_id
        out_dir.mkdir(parents=True, exist_ok=True)

        img_bgr = self._imread_unicode(target_path, max_width=1600)
        if img_bgr is None:
            img_bgr = np.zeros((720, 1280, 3), dtype=np.uint8)
            img_bgr[:, :] = (180, 140, 90)

        # ---------------------------------------------------------------------
        # [Step 1/3] 현재 사진의 시간대 분석 & 1단계 원본 이미지(안 바뀜) 준비
        # ---------------------------------------------------------------------
        detected_phase = self._detect_current_photo_phase_cv(
            img_bgr,
            title_hint=title,
            active_preset_key=active_preset_key,
        )
        api_mode_str = "Gemini 3 API 시간대 변환 엔진"
        if try_gemini_api:
            gem_phase_res = self._query_gemini_3_phase(target_path, title_hint=title, timeout_sec=3.2)
            if gem_phase_res:
                g_phase, used_m = gem_phase_res
                api_mode_str = f"Gemini 3 API ({used_m})"
                if active_preset_key not in ("morning", "sunset", "evening"):
                    detected_phase = g_phase

        if detected_phase not in PHASE_SEQUENCE_MAP:
            detected_phase = "morning"

        phase_order = list(PHASE_SEQUENCE_MAP[detected_phase])
        phase_order_ko = (
            f"{PHASE_LABEL_KO[phase_order[0]]}(원본 안바뀜) ➔ "
            f"{PHASE_LABEL_KO[phase_order[1]]} ➔ "
            f"{PHASE_LABEL_KO[phase_order[2]]}"
        )

        ver_q = f"?v={int(time.time())}"
        textures: Dict[str, str] = {}

        # Step 1/3 원본 저장 (100% 안 바뀜!)
        p0 = phase_order[0]
        p1 = phase_order[1]
        p2 = phase_order[2]
        dst0 = out_dir / f"{p0}.jpg"
        self._imwrite_unicode(dst0, img_bgr)
        url0 = f"/cache/ingredients/{bundle_id}/{p0}.jpg{ver_q}"
        textures[p0] = url0
        textures["stage0"] = url0

        with self._lock:
            self.current_step_num = 2
            self.generation_progress_pct = 34
            self.generation_status_text = (
                f"Step 2/3 · 2단계 [{PHASE_LABEL_KO[p1]}] 시간대 이미지 생성 중... (순서: {phase_order_ko})"
            )
            self.steps = [
                TimelapseStepState(
                    1,
                    p0,
                    f"{PHASE_LABEL_KO[p0]} (원본 안바뀜)",
                    "done",
                    100,
                    url0,
                    "원본 유지 완료 ✓",
                ),
                TimelapseStepState(
                    2,
                    p1,
                    f"{PHASE_LABEL_KO[p1]} 이미지",
                    "running",
                    45,
                    "",
                    "Gemini 3 시간대 조명 변환 중...",
                ),
                TimelapseStepState(
                    3,
                    p2,
                    f"{PHASE_LABEL_KO[p2]} 이미지",
                    "idle",
                    0,
                    "",
                    "대기 중",
                ),
            ]

        if step_delay_sec > 0:
            time.sleep(step_delay_sec)

        sky_mask, hz_y_pct, sun_x_pct, sun_y_pct = self._detect_sky_and_horizon(img_bgr)

        # ---------------------------------------------------------------------
        # [Step 2/3] 두 번째 시간대(p1) 이미지 생성
        # ---------------------------------------------------------------------
        img1_bgr: Optional[np.ndarray] = None
        if try_gemini_api:
            gem_img1 = self._try_gemini_3_image_generation(img_bgr, target_phase=p1, timeout_sec=3.8)
            if gem_img1 is not None:
                img1_bgr, used_img_m = gem_img1
                api_mode_str = f"Gemini 3 API ({used_img_m})"

        if img1_bgr is None:
            img1_bgr = self._synthesize_single_phase_variant(
                img_bgr=img_bgr,
                base_phase=detected_phase,
                target_phase=p1,
                sky_mask=sky_mask,
                horizon_y_pct=hz_y_pct,
                sun_x_pct=sun_x_pct,
                sun_y_pct=sun_y_pct,
            )

        dst1 = out_dir / f"{p1}.jpg"
        self._imwrite_unicode(dst1, img1_bgr)
        url1 = f"/cache/ingredients/{bundle_id}/{p1}.jpg{ver_q}"
        textures[p1] = url1
        textures["stage1"] = url1

        with self._lock:
            self.current_step_num = 3
            self.generation_progress_pct = 68
            self.generation_status_text = (
                f"Step 3/3 · 3단계 [{PHASE_LABEL_KO[p2]}] 시간대 이미지 생성 중... (순서: {phase_order_ko})"
            )
            self.steps[1] = TimelapseStepState(
                2,
                p1,
                f"{PHASE_LABEL_KO[p1]} 이미지",
                "done",
                100,
                url1,
                "생성 완료 ✓",
            )
            self.steps[2] = TimelapseStepState(
                3,
                p2,
                f"{PHASE_LABEL_KO[p2]} 이미지",
                "running",
                50,
                "",
                "Gemini 3 시간대 조명 변환 중...",
            )

        if step_delay_sec > 0:
            time.sleep(step_delay_sec)

        # ---------------------------------------------------------------------
        # [Step 3/3] 세 번째 시간대(p2) 이미지 생성
        # ---------------------------------------------------------------------
        img2_bgr: Optional[np.ndarray] = None
        if try_gemini_api:
            gem_img2 = self._try_gemini_3_image_generation(img_bgr, target_phase=p2, timeout_sec=3.8)
            if gem_img2 is not None:
                img2_bgr, used_img_m = gem_img2
                api_mode_str = f"Gemini 3 API ({used_img_m})"

        if img2_bgr is None:
            img2_bgr = self._synthesize_single_phase_variant(
                img_bgr=img_bgr,
                base_phase=detected_phase,
                target_phase=p2,
                sky_mask=sky_mask,
                horizon_y_pct=hz_y_pct,
                sun_x_pct=sun_x_pct,
                sun_y_pct=sun_y_pct,
            )

        dst2 = out_dir / f"{p2}.jpg"
        self._imwrite_unicode(dst2, img2_bgr)
        url2 = f"/cache/ingredients/{bundle_id}/{p2}.jpg{ver_q}"
        textures[p2] = url2
        textures["stage2"] = url2

        bundle = SceneIngredientsBundle(
            bundle_id=bundle_id,
            title=title,
            source_image_name=target_path.name,
            created_at=time.strftime("%H:%M:%S"),
            api_mode=api_mode_str,
            base_phase=detected_phase,
            phase_order=phase_order,
            phase_order_ko=phase_order_ko,
            textures=textures,
        )

        # 이미지 3개가 모두 완성되었으므로 이제 타임랩스 실행(is_active = True) 시작!
        with self._lock:
            self.steps[2] = TimelapseStepState(
                3,
                p2,
                f"{PHASE_LABEL_KO[p2]} 이미지",
                "done",
                100,
                url2,
                "생성 완료 ✓",
            )
            self._bundles_cache[bundle_id] = bundle
            self.active_bundle = bundle
            self.is_generating = False
            self.generation_progress_pct = 100
            self.current_step_num = 3
            self.generation_status_text = (
                f"✅ 이미지 3개 생성 완료! 타임랩스 작동 중 ({phase_order_ko})"
            )
            self.is_active = True
            self.current_stage_idx = 0  # 항상 1단계(현재 사진 원본 = 안 바뀜)부터 시작!
            self.previous_image_url = url0
            self.last_switch_ts = time.time()
            self.switch_version += 1

        return bundle

    # =========================================================================
    # 6. 동기 생성 및 즉시 실행 (단위 테스트 및 호환용)
    # =========================================================================
    def extract_ingredients_from_image(
        self,
        image_path: Path,
        title: str = "현재 창문 풍경",
        active_preset_key: str = "",
        try_gemini_api: bool = True,
    ) -> SceneIngredientsBundle:
        return self._run_three_step_generation(
            image_path=image_path,
            title=title,
            active_preset_key=active_preset_key,
            try_gemini_api=try_gemini_api,
            step_delay_sec=0.0,
        )

    def build_and_start_from_current_window(
        self,
        image_path: Optional[Path],
        title: str = "현재 창문 풍경",
        active_preset_key: str = "",
        try_gemini_api: bool = True,
    ) -> SceneIngredientsBundle:
        return self._run_three_step_generation(
            image_path=image_path,
            title=title,
            active_preset_key=active_preset_key,
            try_gemini_api=try_gemini_api,
            step_delay_sec=0.0,
        )

    # =========================================================================
    # 7. 일정 시간 간격마다 다음 시간대 사진으로 자동 전환 & 배속 설정
    # =========================================================================
    @property
    def interval_sec(self) -> float:
        return self.get_interval_sec()

    def get_interval_sec(self) -> float:
        with self._lock:
            return SPEED_INTERVAL_MAP.get(self.speed_multiplier, max(1.0, 8.0 / max(0.5, self.speed_multiplier)))

    def set_speed_multiplier(self, mult: float) -> Tuple[float, float]:
        with self._lock:
            val = float(mult)
            if val not in SPEED_INTERVAL_MAP:
                val = 2.0
            self.speed_multiplier = val
            self.last_switch_ts = time.time()
            return self.speed_multiplier, self.get_interval_sec()

    def set_stage_index(self, idx: int) -> int:
        with self._lock:
            if not self.active_bundle:
                return 0
            new_idx = int(idx) % 3
            if new_idx != self.current_stage_idx:
                cur_url = self.active_bundle.textures.get(f"stage{self.current_stage_idx}", "")
                if cur_url:
                    self.previous_image_url = cur_url
                self.current_stage_idx = new_idx
                self.switch_version += 1
            self.last_switch_ts = time.time()
            return self.current_stage_idx

    def tick_and_get_current_image(self) -> Tuple[str, str, int, str]:
        """
        타임랩스가 실행 중(`is_active == True` and `not is_generating`)일 때 호출됩니다.
        설정된 배속 간격(`get_interval_sec()`)이 지났으면 자동으로 다음 단계 사진(0 -> 1 -> 2 -> 0)으로 넘기고
        `(current_image_url, previous_image_url, current_stage_idx, current_stage_label_ko)`를 반환합니다.
        비활성 상태일 때는 `("", "", 0, "")`을 반환합니다.
        """
        with self._lock:
            if not self.is_active or self.is_generating or not self.active_bundle:
                return "", "", 0, ""

            now = time.time()
            interval = self.get_interval_sec()
            if self.last_switch_ts <= 0.0:
                self.last_switch_ts = now
            elif (now - self.last_switch_ts) >= interval:
                prev_url = self.active_bundle.textures.get(f"stage{self.current_stage_idx}", "")
                if prev_url:
                    self.previous_image_url = prev_url
                self.current_stage_idx = (self.current_stage_idx + 1) % 3
                self.last_switch_ts = now
                self.switch_version += 1

            cur_url = self.active_bundle.textures.get(f"stage{self.current_stage_idx}", "")
            prev_url = self.previous_image_url or cur_url
            phase_key = self.active_bundle.phase_order[self.current_stage_idx]
            suffix = " (원본 안바뀜)" if self.current_stage_idx == 0 else ""
            label_ko = f"{PHASE_LABEL_KO.get(phase_key, phase_key)}{suffix}"
            return cur_url, prev_url, self.current_stage_idx, label_ko

    def reset_to_default(self, clear_disk_cache: bool = False) -> None:
        """타임랩스 실행 상태 및 생성된 번들·단계 상태를 초기 기본값(없음)으로 완전히 초기화합니다."""
        import shutil
        with self._lock:
            self.is_active = False
            self.is_generating = False
            self.generation_progress_pct = 0
            self.generation_status_text = "대기 중 (현재 창문 사진으로 타임랩스를 만들 수 있습니다)"
            self.current_step_num = 0
            self.current_stage_idx = 0
            self.previous_image_url = ""
            self.last_switch_ts = 0.0
            self.speed_multiplier = 2.0
            self.active_bundle = None
            self._bundles_cache.clear()
            self.steps = [
                TimelapseStepState(1, "morning", "현재 사진 (원본 안바뀜)", "idle", 0, "", "대기 중"),
                TimelapseStepState(2, "sunset", "다음 시간대 이미지 생성", "idle", 0, "", "대기 중"),
                TimelapseStepState(3, "evening", "마지막 시간대 이미지 생성", "idle", 0, "", "대기 중"),
            ]
            self.switch_version += 1

        if clear_disk_cache and self.cache_root.exists():
            for child in list(self.cache_root.iterdir()):
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    elif child.is_file():
                        child.unlink(missing_ok=True)
                except Exception:
                    pass

    def set_active(self, active: bool) -> bool:
        with self._lock:
            if not active:
                self.reset_to_default(clear_disk_cache=False)
                return False
            self.is_active = True
            self.last_switch_ts = time.time()
            return self.is_active

    def get_runtime_state(self) -> Dict[str, Any]:
        with self._lock:
            b_dict = self.active_bundle.to_dict() if self.active_bundle else {}
            return {
                "is_active": self.is_active,
                "is_generating": self.is_generating,
                "generation_progress_pct": self.generation_progress_pct,
                "generation_status_text": self.generation_status_text,
                "current_step_num": self.current_step_num,
                "current_stage_idx": self.current_stage_idx,
                "speed_multiplier": self.speed_multiplier,
                "interval_sec": self.get_interval_sec(),
                "switch_version": self.switch_version,
                "steps": [vars(s) for s in self.steps],
                "bundle": b_dict,
            }
