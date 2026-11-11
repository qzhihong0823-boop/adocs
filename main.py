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

import urllib.parse
from pathlib import Path
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

BASE_DIR = Path(__file__).resolve().parent
CSS_PATH = BASE_DIR / "assets" / "css" / "style.css"

lighting_mgr = CircadianLightingManager()
landscape_mgr = VirtualLandscapeManager(base_dir=BASE_DIR)


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
    else:
        # [핵심 요구사항] '~ 만들어줘', '~ 그려줘' 또는 등록된 프리셋 이름이 아닌 일반 발화에는 반응하지 않음
        audio_ctrl.add_log(
            "SYSTEM",
            f"ℹ️ 일반 발화 무시 (화면 유지): \"{text}\"",
            "이미지 생성은 '~ 만들어줘', '~ 그려줘'에만 반응하며, 프리셋 전환은 등록된 이름('아침', '노을', '저녁', '야경' 등)에만 반응합니다.",
        )


audio_ctrl = AudioController(
    initial_wake_words=["윈도", "windo"],
    on_command_callback=handle_voice_command,
)
sync_custom_scene_keywords()

app, rt = fast_app(
    hdrs=(Link(rel="stylesheet", href="/assets/css/style.css?v=9"),),
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
# 1. 전체화면 순수 가상 창문 뷰 (Cross-fade + 자연광 RGB/채도 필터 + 좌하단 로딩 표시)
# -------------------------------------------------------------------------
def compute_window_sig() -> str:
    l_state = lighting_mgr.get_current_state()
    s_state = landscape_mgr.get_state()
    return (
        f"{s_state.version}-{int(s_state.is_generating)}-{s_state.generation_progress_pct}-"
        f"{l_state.active_preset_key}-{l_state.cct_kelvin}-{l_state.saturation_pct}-"
        f"{l_state.brightness_pct}-{l_state.contrast_pct}-{IMAGE_FIT_MODE}"
    )


def render_window_stage(oob: bool = False) -> FT:
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
    # [핵심 보장] 이미지 생성 중에는 이전 화면(previous_image_url)이 비치거나 크로스페이드가 재실행되지 않도록
    # 두 레이어 모두 현재 화면(current_image_url)으로 고정하고 페이드 애니메이션을 끕니다.
    prev_img_url = s_state.current_image_url if s_state.is_generating else s_state.previous_image_url
    curr_layer_style = "animation:none; opacity:1;" if s_state.is_generating else ""

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

    return Div(
        # 카드 1: 창문 풍경 & 채도·밝기·색온도 상태 및 직접 조절
        Div(
            Div(
                Span("1. 창문 풍경 & 자연광 채도 제어"),
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
                Span("2. ComfyUI 설정 (긍정·부정 프롬프트 / 모델 / LoRA)"),
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
        # 카드 3: 이름 지정 배경 이미지 추가 (예: '최애' -> 최애.jpg & "윈도 최애" 음성 호출) 및 기존 배경 교체
        Div(
            Div(Span("3. 이름 지정 배경 이미지 추가 & 배경 변경"), cls="option-card-title"),
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
        # 카드 4: 마이크, 호출어('윈도', 'windo') 및 무응답 대기 시간 설정
        Div(
            Div(
                Span("4. 마이크 & 호출어(Wake Word) 설정"),
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
                        "✨ \"저녁\", \"야경\", \"최애\" 또는 \"~ 만들어줘\"라고 말씀하시면 끝에 \"네, 알겠습니다\"라고 응답하며, 이미 생성 중일 때는 \"이미지 생성 중입니다\"라고 안내합니다!",
                        cls="samsung-blue-banner",
                    ),
                    Div(
                        render_settings_controls(),
                        Div(
                            Div(
                                Div(
                                    Span("5. ComfyUI 실시간 노드 실행 창 & 음성 로그 모니터"),
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
                                            placeholder="직접 명령어 입력 (예: 윈도 저녁 / 윈도 산토리니 해변 노을 만들어줘)",
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
                                    cls="terminal-wrapper",
                                    style="height: 760px;",
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
