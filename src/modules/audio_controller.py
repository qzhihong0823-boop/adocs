"""
마이크 제어, 음성 증폭(Gain Boost + Silence Padding), TTS 응답 및 실시간 로깅 모듈 (src/modules/audio_controller.py)

핵심 디버깅 및 개선 사항:
1. TTS("네") 재생 중 스피커 소리가 마이크 버퍼에 들어가자마자 오인식되어 즉시 "무슨 말씀인지 듣지 못했습니다"가 뜨던 버그 수정:
   - TTS 재생 직후 마이크 입력 스트림 버퍼를 완전히 비우고(Flush), 설정된 대기 시간(기본 3.0초)을 끝까지 기다린 뒤에만 무응답 안내 재생
2. 짧은 2음절 단어('노을', '아침', '저녁', '윈도') 인식률 극대화:
   - 오디오 앞뒤에 0.25초/0.3초 무음 패딩(Silence Padding) 추가 및 피크 음량 자동 증폭(최대 10배)으로 짧은 단어 잘림 방지
   - Google STT 다중 후보(show_all=True) 전수 검사 및 발음 유사어('너울', '노울', '마침', '전역' 등) 자동 보정
3. 텍스트 입력창('명령 실행') 및 음성 입력 시 '노을', '아침', '저녁' 키워드가 들어오면 호출어 누락 여부와 관계없이 즉시 실행되도록 개선
"""

from __future__ import annotations

import io
import os
import threading
import time
import winsound
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np
import pyaudio
import speech_recognition as sr
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
AUDIO_ASSETS_DIR = BASE_DIR / "assets" / "audio"
TTS_YES_WAV = AUDIO_ASSETS_DIR / "tts_yes.wav"
TTS_TIMEOUT_WAV = AUDIO_ASSETS_DIR / "tts_timeout.wav"
TTS_ACK_WAV = AUDIO_ASSETS_DIR / "tts_ack.wav"
TTS_GENERATING_WAV = AUDIO_ASSETS_DIR / "tts_generating.wav"


@dataclass
class LogEntry:
    """로그창에 표시할 개별 로그 데이터 구조"""
    id: int
    timestamp: str
    level: str        # "SYSTEM", "MIC", "DETECT", "WAKE", "STT", "WARN", "ERROR"
    message: str
    detail: str = ""


@dataclass
class AudioDeviceInfo:
    """동적 탐색된 오디오 입력 장치 메타데이터"""
    index: int
    name: str
    max_input_channels: int
    default_sample_rate: int
    host_api: int
    is_target_wireless: bool = False
    is_working: bool = True


# 마이크 품질이 낮을 때 자주 발생하는 STT 오인식 -> 핵심 풍경/시간 단어로 자동 보정하는 사전
SCENE_PHONETIC_MAP: Dict[str, Tuple[str, ...]] = {
    "아침": (
        "아침", "마침", "안심", "아짐", "아친", "하침", "아치", "아췸", "아침에", "아침으로",
        "아픔", "아직", "아기", "아줌", "모닝", "굿모닝", "morning", "새벽", "일출", "해뜰", "낮", "환하게", "밝게",
    ),
    "노을": (
        "노을", "너울", "누을", "노울", "노올", "노엘", "노루", "노일", "노은", "노유", "노을로", "노을빛",
        "노래", "노랑", "노란", "너를", "너는", "노아", "노원", "로을", "롤", "논", "눈을",
        "석양", "해질녘", "해질", "일몰", "황혼", "오후", "선셋", "sunset",
    ),
    "저녁": (
        "저녁", "전역", "저년", "저녘", "정역", "천역", "전력", "저역", "저녁에", "저녁으로",
        "저기", "전혀", "정현", "전형", "밤", "야경", "밤하늘", "자정", "어둡게", "수면", "이브닝", "나이트", "evening", "night",
    ),
    "기본": (
        "기본", "기분", "대기", "대기화면", "초기화면", "원래대로", "초기화", "디폴트", "default",
    ),
    "조명 꺼줘": (
        "조명 꺼줘", "조명 꺼 줘", "조명 꺼", "조명 끄기", "조명 오프", "조명 중지",
        "코에룩스 꺼줘", "코에룩스 꺼 줘", "코에룩스 꺼", "코에룩스 오프",
        "불 꺼줘", "불 꺼", "라이트 꺼줘", "라이트 꺼",
    ),
    "조명 켜줘": (
        "조명 켜줘", "조명 켜 줘", "조명 켜", "조명 켜기", "조명 온", "조명 틀어줘", "조명 틀어",
        "코에룩스 켜줘", "코에룩스 켜 줘", "코에룩스 켜", "코에룩스 온", "코에룩스", "toggle_coelux",
        "불 켜줘", "불 켜", "라이트 켜줘", "라이트 켜",
    ),
    "캘린더 꺼줘": (
        "캘린더 꺼줘", "캘린더 꺼 줘", "캘린더 꺼", "캘린더 끄기", "캘린더 숨겨줘", "캘린더 숨겨",
        "캘린더 닫아줘", "캘린더 닫아", "캘린더 없애줘", "캘린더 없애", "캘린더 치워",
        "달력 꺼줘", "달력 꺼", "달력 닫아", "달력 숨겨",
        "일정 꺼줘", "일정 꺼", "일정 닫아", "일정 숨겨",
        "켈린더 꺼", "켈린더 꺼줘", "칼렌더 꺼", "카렌다 꺼",
    ),
    "캘린더": (
        "캘린더", "캘린더 보여줘", "캘린더 보여 줘", "캘린더 켜줘", "캘린더 켜 줘", "캘린더 띄워줘", "캘린더 열기", "캘린더 열어줘",
        "구글 캘린더", "달력", "달력 보여줘", "달력 켜줘", "일정", "일정 보여줘", "일정 알려줘", "오늘 일정", "스케줄", "스케줄 보여줘",
        "켈린더", "칼렌더", "카렌다", "캘린더보여줘",
    ),
    "정보 꺼줘": (
        "정보 꺼줘", "정보 꺼 줘", "정보 꺼", "정보 끄기", "정보 숨겨줘", "정보 숨겨 줘", "정보 숨겨",
        "정보 닫아줘", "정보 닫아", "정보 없애줘", "정보 없애", "정보 가려줘", "정보 치워줘", "정보 안 보이게",
        "날씨 꺼줘", "날씨 꺼 줘", "날씨 꺼", "날씨 끄기", "날씨 숨겨", "날씨 닫아",
        "기온 꺼줘", "기온 꺼 줘", "기온 꺼", "기온 끄기", "기온 숨겨", "기온 닫아",
        "날짜 꺼줘", "날짜 꺼 줘", "날짜 꺼", "날짜 끄기", "날짜 숨겨", "날짜 닫아",
        "시간 꺼줘", "시간 꺼 줘", "시간 꺼", "시간 끄기", "시간 숨겨", "시간 닫아",
        "시계 꺼줘", "시계 꺼 줘", "시계 꺼", "시계 끄기", "시계 숨겨", "시계 닫아",
        "온도 꺼줘", "온도 꺼", "습도 꺼줘", "습도 꺼", "공기질 꺼줘", "공기질 꺼", "미세먼지 꺼",
    ),
    "정보": (
        "정보", "정보 보여줘", "정보 보여 줘", "정보 켜줘", "정보 켜 줘", "정보 띄워줘", "정보 알려줘",
        "날씨", "날씨 보여줘", "날씨 보여 줘", "날씨 알려줘", "날씨 켜줘", "오늘 날씨", "현재 날씨",
        "기온", "기온 보여줘", "기온 보여 줘", "기온 알려줘", "기온 켜줘", "현재 기온", "온도", "온도 보여줘", "온도 알려줘",
        "날짜", "날짜 보여줘", "날짜 보여 줘", "날짜 알려줘", "날짜 켜줘", "오늘 날짜",
        "시간", "시간 보여줘", "시간 보여 줘", "시간 알려줘", "시간 켜줘", "현재 시간",
        "시계", "시계 보여줘", "시계 켜줘", "습도", "습도 보여줘", "습도 알려줘",
        "공기질", "실내 공기질", "공기질 보여줘", "공기질 알려줘", "미세먼지", "미세먼지 보여줘", "미세먼지 알려줘",
        "창문 정보", "날씨 정보", "시간 정보", "정복", "전보", "점보",
    ),
}

# '윈도' / 'windo' 호출어 발음 유사어 확장
WAKE_WORD_ALIASES: Tuple[str, ...] = (
    "윈도우", "window", "윈도", "windo",
    "인도", "민도", "신도", "진도", "빈도", "윈동", "위도", "원도", "윤도", "림도", "웬도", "우인도", "핀도", "린도",
    "운동", "일도", "이도",
)


class AudioController:
    """
    마이크 동적 바인딩, 음성 증폭, 호출어('윈도', 'windo') 감지, TTS 응답("네" / 무응답 안내),
    OpenAI Whisper STT 변환 및 비동기 UI 로그 스트림을 총괄하는 컨트롤러
    """

    TARGET_KEYWORDS: Tuple[str, ...] = (
        "buds",
        "hands-free",
        "bluetooth",
        "머리에 거는 수화기",
        "ag audio",
    )

    def __init__(
        self,
        initial_wake_words: Optional[List[str]] = None,
        on_command_callback: Optional[Callable[[str], None]] = None,
        max_logs: int = 100,
    ) -> None:
        self._lock = threading.RLock()
        self._log_counter = 0
        self._logs: Deque[LogEntry] = deque(maxlen=max_logs)

        self.wake_words: List[str] = initial_wake_words or ["윈도", "windo"]
        self.require_wake_word: bool = True
        self.command_timeout_sec: float = 3.0

        self.available_devices: List[AudioDeviceInfo] = []
        self.selected_device_index: Optional[int] = None
        self.selected_device_name: str = "탐색 전"
        self.selected_sample_rate: int = 16000

        self.is_listening: bool = False
        self.status_text: str = "초기화 중..."
        self.status_state: str = "idle"
        self.last_recognized_command: str = ""
        self.last_raw_speech: str = ""

        self._stop_event = threading.Event()
        self._listen_thread: Optional[threading.Thread] = None
        self._on_command_callback = on_command_callback

        # 호출어 단독 발화 시 직후 명령어 대기 데드라인 타임스탬프
        self._followup_deadline: float = 0.0
        self._active_wake_word: str = ""
        self._needs_buffer_flush: bool = False

        self.add_log("SYSTEM", "WINDO 오디오 컨트롤러가 시작되었습니다.")
        self.scan_and_bind_microphone()

    # -------------------------------------------------------------------------
    # 1. 스레드 안전 로그 관리
    # -------------------------------------------------------------------------
    def add_log(self, level: str, message: str, detail: str = "") -> LogEntry:
        with self._lock:
            self._log_counter += 1
            now_str = datetime.now().strftime("%H:%M:%S")
            entry = LogEntry(
                id=self._log_counter,
                timestamp=now_str,
                level=level.upper(),
                message=message,
                detail=detail,
            )
            self._logs.append(entry)
            return entry

    def get_logs(self, limit: int = 40) -> List[LogEntry]:
        with self._lock:
            items = list(self._logs)
            return items[-limit:]

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()
        self.add_log("SYSTEM", "로그가 초기화되었습니다.")

    # -------------------------------------------------------------------------
    # 2. TTS 음성 출력 ("네", "네, 알겠습니다", "이미지 생성 중입니다", "무슨 말씀인지 듣지 못했습니다")
    # -------------------------------------------------------------------------
    def speak_yes(self) -> None:
        """호출어 단독 감지 시 '네' TTS 음성을 재생하고 마이크 버퍼 플러시 플래그를 켭니다."""
        self.add_log("WAKE", "🔊 TTS 응답: \"네\"")
        if TTS_YES_WAV.exists():
            try:
                winsound.PlaySound(str(TTS_YES_WAV), winsound.SND_FILENAME)
            except Exception:
                pass
        with self._lock:
            self._needs_buffer_flush = True

    def speak_ack(self) -> None:
        """풍경 전환('저녁', '야경', '노을' 등) 또는 생성 명령('~ 만들어줘') 접수 시 끝에 '네, 알겠습니다.' TTS 음성을 재생합니다."""
        self.add_log("WAKE", "🔊 TTS 응답: \"네, 알겠습니다.\"")
        if TTS_ACK_WAV.exists():
            try:
                winsound.PlaySound(str(TTS_ACK_WAV), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass
        with self._lock:
            self._needs_buffer_flush = True

    def speak_generating(self) -> None:
        """이미 이미지 생성이 진행 중일 때 추가 생성 요청이 들어오면 '이미지 생성 중입니다.' TTS 음성을 재생합니다."""
        self.add_log("WARN", "🔊 TTS 안내: \"이미지 생성 중입니다.\" (동시 1개만 생성 가능)")
        if TTS_GENERATING_WAV.exists():
            try:
                winsound.PlaySound(str(TTS_GENERATING_WAV), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass
        with self._lock:
            self._needs_buffer_flush = True

    def speak_timeout(self) -> None:
        """무응답 시간 초과 시 '무슨 말씀인지 듣지 못했습니다' TTS 음성을 재생합니다."""
        self.add_log("WARN", f"🔊 TTS 안내: \"무슨 말씀인지 듣지 못했습니다\" ({self.command_timeout_sec:.1f}초 무응답)")
        if TTS_TIMEOUT_WAV.exists():
            try:
                winsound.PlaySound(str(TTS_TIMEOUT_WAV), winsound.SND_FILENAME)
            except Exception:
                pass
        with self._lock:
            self._needs_buffer_flush = True

    def set_command_timeout(self, timeout_sec: float) -> float:
        clamped = round(max(1.0, min(15.0, float(timeout_sec))), 1)
        with self._lock:
            self.command_timeout_sec = clamped
        self.add_log("SYSTEM", f"호출 후 무응답 대기 시간이 {clamped}초로 변경되었습니다.")
        return clamped

    # -------------------------------------------------------------------------
    # 3. 저감도 마이크용 소프트웨어 음량 증폭 + 앞뒤 무음 패딩 (짧은 단어 인식 핵심!)
    # -------------------------------------------------------------------------
    @staticmethod
    def _enhance_audio_data(audio_data: sr.AudioData) -> sr.AudioData:
        """
        1) 16-bit PCM 오디오의 DC 오프셋을 제거하고 피크 음량을 자동 증폭(최대 10배)합니다.
        2) '노을', '아침', '저녁' 같은 짧은 2음절 단어를 Google STT/Whisper가 놓치지 않도록
           오디오 앞(0.25초)과 뒤(0.30초)에 깨끗한 무음 패딩(Silence Padding)을 추가합니다.
        """
        try:
            target_rate = 16000
            raw_bytes = audio_data.get_raw_data(convert_rate=target_rate, convert_width=2)
            samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            if samples.size == 0:
                return audio_data

            # DC 오프셋 제거 및 피크 증폭
            samples = samples - np.mean(samples)
            peak = float(np.max(np.abs(samples)))
            if peak > 50.0:
                target_peak = 27000.0
                gain = min(10.0, max(1.0, target_peak / peak))
                samples = np.clip(samples * gain, -32767.0, 32767.0)

            # 앞 0.25초, 뒤 0.30초 무음 패딩 추가 (짧은 2음절 단어 인식률 극대화)
            pad_head = np.zeros(int(target_rate * 0.25), dtype=np.int16)
            pad_tail = np.zeros(int(target_rate * 0.30), dtype=np.int16)
            pcm_int16 = samples.astype(np.int16)
            padded = np.concatenate([pad_head, pcm_int16, pad_tail])

            return sr.AudioData(padded.tobytes(), target_rate, 2)
        except Exception:
            return audio_data

    @staticmethod
    def _flush_mic_stream(mic_source: sr.Microphone, sample_rate: int) -> None:
        """
        TTS('네')가 스피커로 재생되는 동안 마이크에 녹음된 잔향/에코 데이터를
        전부 읽어서 버립니다. (스피커 소리를 사용자 명령으로 오인식하는 버그 원천 차단)
        """
        try:
            stream = getattr(mic_source, "stream", None)
            if stream is None:
                return
            # 약 0.35초 분량의 버퍼를 비워 스피커 잔향 제거
            chunks_to_flush = max(1, int((sample_rate / 1024) * 0.35))
            for _ in range(chunks_to_flush):
                try:
                    stream.read(1024, exception_on_overflow=False)
                except Exception:
                    break
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # 4. PyAudio 기반 동적 마이크 탐색 및 실제 스트림 작동 검증
    # -------------------------------------------------------------------------
    @staticmethod
    def _probe_device_stream(pa: pyaudio.PyAudio, idx: int, default_rate: int) -> Tuple[bool, int]:
        candidate_rates = [16000, default_rate, 44100, 48000, 8000]
        seen = set()
        for rate in candidate_rates:
            if rate <= 0 or rate in seen:
                continue
            seen.add(rate)
            try:
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=rate,
                    input=True,
                    input_device_index=idx,
                    frames_per_buffer=1024,
                )
                stream.close()
                return True, rate
            except Exception:
                continue
        return False, default_rate

    def discover_input_devices(self) -> List[AudioDeviceInfo]:
        discovered: List[AudioDeviceInfo] = []
        pa = pyaudio.PyAudio()
        try:
            device_count = pa.get_device_count()
            seen_names_in_mme = set()

            for idx in range(device_count):
                try:
                    info = pa.get_device_info_by_index(idx)
                except Exception:
                    continue

                max_in = int(info.get("maxInputChannels", 0))
                if max_in <= 0:
                    continue

                raw_name = info.get("name", f"Unknown Device {idx}")
                name = self._normalize_device_name(raw_name)
                host_api = int(info.get("hostApi", 0))
                default_rate = int(float(info.get("defaultSampleRate", 16000)))

                if host_api == 0:
                    seen_names_in_mme.add(name)
                elif name in seen_names_in_mme:
                    continue

                is_working, verified_rate = self._probe_device_stream(pa, idx, default_rate)
                if not is_working:
                    continue

                name_lower = name.lower()
                is_target = any(kw in name_lower for kw in self.TARGET_KEYWORDS)

                discovered.append(
                    AudioDeviceInfo(
                        index=idx,
                        name=name,
                        max_input_channels=max_in,
                        default_sample_rate=verified_rate,
                        host_api=host_api,
                        is_target_wireless=is_target,
                        is_working=True,
                    )
                )
        finally:
            pa.terminate()

        return discovered

    @staticmethod
    def _normalize_device_name(raw_name: object) -> str:
        if isinstance(raw_name, bytes):
            for enc in ("utf-8", "cp949", "euc-kr", "latin1"):
                try:
                    return raw_name.decode(enc)
                except Exception:
                    continue
            return raw_name.decode("utf-8", errors="replace")
        return str(raw_name).strip()

    def scan_and_bind_microphone(self, preferred_index: Optional[int] = None) -> Optional[AudioDeviceInfo]:
        devices = self.discover_input_devices()
        with self._lock:
            self.available_devices = devices

        if not devices:
            with self._lock:
                self.selected_device_index = None
                self.selected_device_name = "연결된 마이크 없음"
                self.status_state = "error"
                self.status_text = "작동 가능한 마이크 장치를 찾을 수 없습니다."
            self.add_log("ERROR", "현재 입력 스트림을 열 수 있는 마이크가 없습니다.")
            return None

        chosen: Optional[AudioDeviceInfo] = None

        if preferred_index is not None:
            for d in devices:
                if d.index == preferred_index:
                    chosen = d
                    break

        if chosen is None:
            for d in devices:
                if d.is_target_wireless:
                    chosen = d
                    break

        if chosen is None:
            real_hw = [
                d for d in devices
                if "매퍼" not in d.name
                and "mapper" not in d.name.lower()
                and "주 사운드" not in d.name
            ]
            chosen = real_hw[0] if real_hw else devices[0]

        with self._lock:
            self.selected_device_index = chosen.index
            self.selected_device_name = chosen.name
            self.selected_sample_rate = chosen.default_sample_rate

        self.add_log(
            "MIC",
            f"마이크 연결 완료: {chosen.name}",
            f"Idx: {chosen.index} | {chosen.default_sample_rate}Hz",
        )
        return chosen

    # -------------------------------------------------------------------------
    # 5. 사용자 지정 호출어(Wake Word) 설정 관리
    # -------------------------------------------------------------------------
    def set_wake_words_from_string(self, raw_input: str) -> List[str]:
        words = [
            w.strip()
            for w in raw_input.replace(",", " ").split()
            if w.strip()
        ]
        if not words:
            words = ["윈도", "windo"]
        with self._lock:
            self.wake_words = words
        self.add_log("SYSTEM", f"호출어 설정 변경: {', '.join(words)}")
        return words

    def toggle_wake_word_mode(self, require: Optional[bool] = None) -> bool:
        with self._lock:
            if require is None:
                self.require_wake_word = not self.require_wake_word
            else:
                self.require_wake_word = require
            mode_state = self.require_wake_word

        if mode_state:
            self.add_log("SYSTEM", f"호출어 감지 모드 ON (호출어: {', '.join(self.wake_words)})")
        else:
            self.add_log("SYSTEM", "연속 인식 모드 ON (호출어 없이 '아침', '노을', '저녁'만 말해도 즉시 작동)")
        return mode_state

    # -------------------------------------------------------------------------
    # 6. 백그라운드 오디오 리스닝 & TTS 인터랙션 루프
    # -------------------------------------------------------------------------
    def start_listening(self) -> None:
        if self.is_listening and self._listen_thread and self._listen_thread.is_alive():
            return

        self._stop_event.clear()
        self.is_listening = True
        self._listen_thread = threading.Thread(
            target=self._listen_loop,
            name="AudioControllerListenThread",
            daemon=True,
        )
        self._listen_thread.start()
        self.add_log("SYSTEM", "실시간 마이크 음성 감지 스레드가 시작되었습니다.")

    def stop_listening(self) -> None:
        self._stop_event.set()
        self.is_listening = False
        with self._lock:
            self.status_state = "idle"
            self.status_text = "일시 정지됨"
        self.add_log("SYSTEM", "마이크 감지가 일시 정지되었습니다.")

    def _listen_loop(self) -> None:
        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = False
        recognizer.energy_threshold = 55
        # 호출어 발화 후 사용자의 긴 문장("겨울 배경 만들어줘" 등)이 끝날 때까지 끊기지 않고 다 적도록 여유 확보
        recognizer.pause_threshold = 0.85
        recognizer.non_speaking_duration = 0.35

        current_mic_idx: Optional[int] = None

        while not self._stop_event.is_set():
            with self._lock:
                target_idx = self.selected_device_index
                target_rate = self.selected_sample_rate
                dev_name = self.selected_device_name

            if target_idx is None:
                self.scan_and_bind_microphone()
                time.sleep(2.0)
                continue

            mic_source: Optional[sr.Microphone] = None
            try:
                mic_source = sr.Microphone(
                    device_index=target_idx,
                    sample_rate=target_rate,
                    chunk_size=1024,
                )
                mic_source.__enter__()

                if current_mic_idx != target_idx:
                    current_mic_idx = target_idx
                    recognizer.adjust_for_ambient_noise(mic_source, duration=0.35)
                    recognizer.energy_threshold = min(max(recognizer.energy_threshold * 0.75, 45), 120)
                    self.add_log(
                        "MIC",
                        f"마이크 고감도 모드 준비 완료 ({dev_name})",
                        f"감도 임계값: {int(recognizer.energy_threshold)}",
                    )

                while not self._stop_event.is_set():
                    if self.selected_device_index != current_mic_idx:
                        break

                    # TTS 재생 직후라면 스피커 소리가 들어간 마이크 버퍼를 깨끗이 비움
                    with self._lock:
                        should_flush = self._needs_buffer_flush
                        self._needs_buffer_flush = False
                    if should_flush:
                        self._flush_mic_stream(mic_source, target_rate)

                    now = time.time()
                    with self._lock:
                        deadline = self._followup_deadline
                        is_followup = (deadline > 0.0 and now < deadline)
                        expired_followup = (deadline > 0.0 and now >= deadline)
                        if expired_followup:
                            self._followup_deadline = 0.0

                    if expired_followup:
                        self.speak_timeout()
                        continue

                    with self._lock:
                        if is_followup:
                            rem_sec = max(0.5, self._followup_deadline - time.time())
                            self.status_state = "wake_detected"
                            self.status_text = f"🟢 '네' 응답 완료 — 명령(아침/노을/저녁) 대기 중 ({rem_sec:.1f}초)"
                        elif self.require_wake_word:
                            ww_str = ", ".join(self.wake_words)
                            self.status_state = "listening"
                            self.status_text = f"👂 듣는 중 (호출어: {ww_str})"
                        else:
                            self.status_state = "listening"
                            self.status_text = "👂 듣는 중 (연속 인식 모드)"

                    try:
                        wait_timeout = max(0.6, self._followup_deadline - time.time()) if is_followup else 2.0
                        audio_data = recognizer.listen(
                            mic_source,
                            timeout=wait_timeout,
                            phrase_time_limit=9.0,
                        )
                    except sr.WaitTimeoutError:
                        with self._lock:
                            if self._followup_deadline > 0.0 and time.time() >= self._followup_deadline:
                                self._followup_deadline = 0.0
                                do_timeout = True
                            else:
                                do_timeout = False
                        if do_timeout:
                            self.speak_timeout()
                        continue

                    with self._lock:
                        self.status_state = "transcribing"
                        self.status_text = "🔵 음성 분석 중..."

                    self._process_captured_audio(recognizer, audio_data, is_followup)

            except Exception as exc:
                err_msg = str(exc)
                if "'NoneType' object has no attribute 'close'" in err_msg:
                    err_msg = "오디오 장치 스트림 재설정 중"
                with self._lock:
                    self.status_state = "error"
                    self.status_text = "마이크 재연결 중..."
                self.add_log("WARN", f"마이크 스트림 재연결: {err_msg}")
                current_mic_idx = None
                self.scan_and_bind_microphone()
                time.sleep(1.2)
            finally:
                if mic_source is not None:
                    try:
                        if getattr(mic_source, "stream", None) is not None:
                            mic_source.stream.close()
                    except Exception:
                        pass
                    try:
                        if getattr(mic_source, "audio", None) is not None:
                            mic_source.audio.terminate()
                    except Exception:
                        pass

    # -------------------------------------------------------------------------
    # 7. 다중 후보(show_all=True) + 발음 유사어 보정 기반 고감도 음성 분석
    # -------------------------------------------------------------------------
    def _recognize_candidates(self, recognizer: sr.Recognizer, boosted_audio: sr.AudioData) -> List[str]:
        candidates: List[str] = []
        try:
            raw_res = recognizer.recognize_google(boosted_audio, language="ko-KR", show_all=True)
            if isinstance(raw_res, dict) and "alternative" in raw_res:
                for alt in raw_res["alternative"]:
                    t = str(alt.get("transcript", "")).strip()
                    if t and t not in candidates:
                        candidates.append(t)
            elif isinstance(raw_res, str) and raw_res.strip():
                candidates.append(raw_res.strip())
        except sr.UnknownValueError:
            return []
        except Exception as exc:
            self.add_log("WARN", f"음성 인식 통신 오류: {exc}")
            return []
        return candidates

    @staticmethod
    def register_custom_scene_keyword(scene_name: str) -> None:
        """
        사용자가 이름을 붙여 추가한 배경 이미지(예: '최애' -> 최애.jpg)나
        AI가 생성한 배경 이미지(예: '겨울_풍경' -> 겨울_풍경.png)를
        '아침', '노을', '저녁'과 동일한 핵심 풍경어로 실시간 등록합니다.
        """
        clean = scene_name.replace("_", " ").strip()
        if not clean:
            return
        compact = clean.replace(" ", "")
        variants = [
            clean.lower(),
            compact.lower(),
            scene_name.lower(),
            f"{clean.lower()} 화면",
            f"{clean.lower()} 배경",
            f"{clean.lower()} 풍경",
        ]
        # 중복 제거
        unique_variants = tuple(dict.fromkeys(v for v in variants if v))
        SCENE_PHONETIC_MAP[clean] = unique_variants

    @staticmethod
    def match_canonical_scene(text: str) -> Optional[str]:
        """텍스트 안에 '아침', '노을', '저녁', '기본' 및 사용자가 추가한 커스텀 배경 이름(예: '최애')이 있으면 반환합니다."""
        lower = text.strip().lower()
        compact = lower.replace(" ", "")
        if not lower:
            return None
        for canonical, variants in SCENE_PHONETIC_MAP.items():
            for v in variants:
                v_low = v.lower()
                if v_low in lower or v_low.replace(" ", "") in compact:
                    return canonical
        return None

    @classmethod
    def normalize_scene_keyword(cls, text: str) -> str:
        """
        짧은 단독 발화(예: '너울', '마침', '전역')일 때만 표준 프리셋 단어('노을', '아침', '저녁')로 발음 보정하고,
        '겨울 배경 만들어줘' 같은 문장형 명령어는 원문 전체를 그대로 보존합니다.
        """
        cleaned = text.strip()
        compact = cleaned.replace(" ", "")
        if len(compact) <= 4:
            canonical = cls.match_canonical_scene(cleaned)
            if canonical and canonical not in cleaned:
                return f"{canonical} ({cleaned})"
        return cleaned

    @classmethod
    def _pick_best_candidate(cls, candidates: List[str], wake_words: List[str]) -> str:
        if not candidates:
            return ""

        # 1순위: 호출어 + 풍경 단어가 함께 들어있는 후보
        for cand in candidates:
            mw, rem = cls._match_wake_word(cand, wake_words)
            if mw and rem and cls.match_canonical_scene(rem):
                return cand

        # 2순위: 핵심 풍경 단어(아침, 노을, 저녁, 기본) 또는 발음 유사어가 들어있는 후보
        for cand in candidates:
            if cls.match_canonical_scene(cand):
                return cand

        # 3순위: 호출어가 들어있는 후보
        for cand in candidates:
            mw, _ = cls._match_wake_word(cand, wake_words)
            if mw:
                return cand

        return candidates[0]

    def _process_captured_audio(
        self,
        recognizer: sr.Recognizer,
        audio_data: sr.AudioData,
        is_followup: bool,
    ) -> None:
        boosted_audio = self._enhance_audio_data(audio_data)
        candidates = self._recognize_candidates(recognizer, boosted_audio)

        with self._lock:
            active_wake_words = list(self.wake_words)
            require_ww = self.require_wake_word

        # A. '윈도' 호출 후 '네' 응답 뒤의 후속 명령 대기 상태인 경우
        if is_followup:
            if not candidates:
                # 아직 대기 시간(_followup_deadline)이 남아있으면 바로 실패 처리하지 않고 계속 대기!
                with self._lock:
                    expired = (self._followup_deadline > 0.0 and time.time() >= self._followup_deadline)
                    if expired:
                        self._followup_deadline = 0.0
                if expired:
                    self.speak_timeout()
                return

            best_text = self._pick_best_candidate(candidates, active_wake_words)
            # 혹시 사용자가 또 '윈도'만 말한 경우 타이머만 연장
            mw_only, rem_only = self._match_wake_word(best_text, active_wake_words)
            if mw_only and not rem_only and not self.match_canonical_scene(best_text):
                self.speak_yes()
                with self._lock:
                    self._followup_deadline = time.time() + self.command_timeout_sec
                return

            # 명령 인식 성공 -> 대기 종료 및 실행
            with self._lock:
                self._followup_deadline = 0.0

            normalized_cmd = self.normalize_scene_keyword(best_text)
            self._transcribe_command_buffer(
                recognizer,
                boosted_audio,
                trigger_label=f"'{self._active_wake_word}' 호출 후 명령",
                strip_wake_word=self._active_wake_word,
                fallback_text=normalized_cmd,
            )
            return

        # B. 평상시 대기 상태
        if not candidates:
            return

        best_text = self._pick_best_candidate(candidates, active_wake_words)
        with self._lock:
            self.last_raw_speech = best_text

        # 연속 인식 모드인 경우 즉시 실행
        if not require_ww:
            normalized_cmd = self.normalize_scene_keyword(best_text)
            self._transcribe_command_buffer(
                recognizer,
                boosted_audio,
                trigger_label="연속 인식 모드",
                fallback_text=normalized_cmd,
            )
            return

        matched_wake, remainder_hint = self._match_wake_word(best_text, active_wake_words)

        # 마이크가 앞부분 '윈도'를 잘라먹고 '노을', '아침', '저녁' 핵심 단어나 '~ 만들어줘', '~ 그려줘', '~ 찾아줘' 명령을 잡은 경우에만 실행!
        direct_scene = self.match_canonical_scene(best_text)
        has_special_cmd = any(
            kw in best_text
            for kw in (
                "만들어줘", "만들어 줘", "만들어", "그려줘", "그려 줘", "그려", "생성해줘", "생성해 줘",
                "찾아줘", "찾아 줘", "찾아", "검색해줘", "검색해 줘", "검색해",
                "들려줘", "들려 줘", "들려주라", "틀어줘", "틀어 줘", "소리 꺼", "소리꺼", "소리 멈춰", "소리 켜",
                "조명 켜", "조명켜", "조명 꺼", "조명꺼", "코에룩스",
                "타임랩스", "타임 랩스", "타입랩스", "타입 랩스", "타임렙스", "타입렙스", "배속", "깊이 맵", "깊이맵",
            )
        )
        if not matched_wake and (direct_scene or has_special_cmd):
            normalized_cmd = self.normalize_scene_keyword(best_text)
            label_desc = f"핵심 풍경어('{direct_scene}') 직접 감지" if direct_scene else "특수 배경 명령 직접 감지"
            self._transcribe_command_buffer(
                recognizer,
                boosted_audio,
                trigger_label=label_desc,
                fallback_text=normalized_cmd,
            )
            return

        if not matched_wake:
            self.add_log(
                "DETECT",
                f"감지된 음성: \"{best_text}\"",
                f"호출어({', '.join(active_wake_words)}) 또는 풍경 명령어 미포함",
            )
            return

        # C. 호출어('윈도' / 'windo') 감지 성공!
        if remainder_hint:
            normalized_rem = self.normalize_scene_keyword(remainder_hint)
            self._transcribe_command_buffer(
                recognizer,
                boosted_audio,
                trigger_label=f"호출어 '{matched_wake}'",
                strip_wake_word=matched_wake,
                fallback_text=normalized_rem,
            )
        else:
            # '윈도' 단독 호출 -> TTS로 "네" 응답 후 정확히 command_timeout_sec(기본 3.0초) 타이머 시작!
            self._active_wake_word = matched_wake
            self.speak_yes()
            with self._lock:
                self._followup_deadline = time.time() + self.command_timeout_sec
            self.add_log(
                "WAKE",
                f"호출어 '{matched_wake}' 감지 완료 — {self.command_timeout_sec:.1f}초 내에 명령(아침/노을/저녁)을 말씀하세요",
            )

    @staticmethod
    def _match_wake_word(text: str, wake_words: List[str]) -> Tuple[Optional[str], str]:
        normalized_text = text.strip()
        compact_text = normalized_text.replace(" ", "").lower()

        expanded_candidates: List[Tuple[str, str]] = []
        for ww in wake_words:
            ww_clean = ww.strip()
            if not ww_clean:
                continue
            if ww_clean.lower() in ("윈도", "windo", "윈도우", "window"):
                for alias in WAKE_WORD_ALIASES:
                    expanded_candidates.append((ww_clean, alias))
            else:
                expanded_candidates.append((ww_clean, ww_clean))

        for display_ww, candidate in expanded_candidates:
            cand_lower = candidate.replace(" ", "").lower()

            idx = normalized_text.lower().find(candidate.lower())
            if idx != -1:
                after = normalized_text[idx + len(candidate):].strip()
                for particle in ("야 ", "아 ", ",", "!", "?"):
                    if after.startswith(particle):
                        after = after[len(particle):].strip()
                if after in ("야", "아", "우"):
                    after = ""
                return display_ww, after

            if compact_text.startswith(cand_lower):
                after_compact = compact_text[len(cand_lower):].strip()
                return display_ww, after_compact

        return None, ""

    def _transcribe_command_buffer(
        self,
        recognizer: sr.Recognizer,
        audio_data: sr.AudioData,
        trigger_label: str,
        strip_wake_word: Optional[str] = None,
        fallback_text: str = "",
    ) -> None:
        start_ts = time.time()

        final_text = fallback_text.strip()
        engine_used = "고감도 음성인식 & 문장 추론 엔진"

        if not final_text:
            candidates = self._recognize_candidates(recognizer, audio_data)
            if candidates:
                final_text = candidates[0].strip()

        if final_text and strip_wake_word:
            _, stripped = self._match_wake_word(final_text, [strip_wake_word])
            if stripped:
                final_text = stripped

        final_text = self.normalize_scene_keyword(final_text)
        elapsed_ms = int((time.time() - start_ts) * 1000)

        if not final_text:
            self.speak_timeout()
            return

        with self._lock:
            self.last_recognized_command = final_text
            self.last_raw_speech = final_text

        self.add_log(
            "STT",
            f"명령 인식 완료: \"{final_text}\"",
            f"{trigger_label} | {engine_used} ({elapsed_ms}ms)",
        )

        if self._on_command_callback:
            try:
                self._on_command_callback(final_text)
            except Exception as exc:
                self.add_log("ERROR", f"명령 처리 오류: {exc}")

    # -------------------------------------------------------------------------
    # 8. UI 텍스트 입력('명령 실행') 완벽 호환 처리
    # -------------------------------------------------------------------------
    def simulate_voice_input(self, simulated_text: str) -> None:
        """
        사용자가 설정 창의 '명령 실행' 입력칸에
        1) '윈도'만 입력 -> "네" TTS 후 대기 모드 진입
        2) '윈도 노을' 입력 -> "네" TTS 후 즉시 노을 전환
        3) '노을', '아침', '저녁'만 단독 입력 -> 호출어 없어도 즉시 해당 풍경으로 전환! (진행 중이던 무응답 타이머도 자동 해제)
        """
        text = simulated_text.strip()
        if not text:
            return

        with self._lock:
            active_wake_words = list(self.wake_words)
            self.last_raw_speech = text
            was_followup = (self._followup_deadline > 0.0 and time.time() < self._followup_deadline)
            # 텍스트 명령이 들어왔으므로 기존 무응답 타이머 즉시 해제 ("무슨 말씀인지 듣지 못했습니다" 오작동 방지)
            self._followup_deadline = 0.0

        matched_wake, remainder = self._match_wake_word(text, active_wake_words)

        # Case 1: '윈도'만 단독으로 친 경우
        if matched_wake and not remainder and not self.match_canonical_scene(text):
            self._active_wake_word = matched_wake
            self.speak_yes()
            with self._lock:
                self._followup_deadline = time.time() + self.command_timeout_sec
            self.add_log(
                "WAKE",
                f"호출어 '{matched_wake}' 호출됨 — {self.command_timeout_sec:.1f}초 내에 명령어를 입력하거나 말씀하세요",
            )
            return

        # Case 2: '윈도 노을'처럼 호출어 + 명령어를 친 경우
        if matched_wake and remainder:
            norm = self.normalize_scene_keyword(remainder)
            with self._lock:
                self.last_recognized_command = norm
            self.add_log("STT", f"명령 인식 완료: \"{norm}\"", f"호출어 '{matched_wake}' (텍스트 실행)")
            if self._on_command_callback:
                self._on_command_callback(norm)
            return

        # Case 3: '노을', '아침', '저녁' 등 명령어만 바로 친 경우 -> 무조건 즉시 실행!
        norm = self.normalize_scene_keyword(text)
        with self._lock:
            self.last_recognized_command = norm
        trigger_desc = "호출 후 후속 명령" if was_followup else "직접 명령 실행"
        self.add_log("STT", f"명령 인식 완료: \"{norm}\"", trigger_desc)
        if self._on_command_callback:
            self._on_command_callback(norm)
