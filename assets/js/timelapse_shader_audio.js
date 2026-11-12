/**
 * WINDO AI 스마트 창문 - 현재 창문 사진 기준 3단계 순환 WebGL 타임랩스 셰이더 & Web Audio GainNode 크로스페이더
 * (/assets/js/timelapse_shader_audio.js)
 *
 * 핵심 동작:
 * 1. 현재 창문에 띄워져 있는 사진의 시간대(아침 / 노을 / 저녁)를 분석한 뒤,
 *    - 아침이면: [1단계: 아침(원본 안바뀜)] ➔ [2단계: 노을] ➔ [3단계: 저녁]
 *    - 노을이면: [1단계: 노을(원본 안바뀜)] ➔ [2단계: 저녁] ➔ [3단계: 아침]
 *    - 저녁이면: [1단계: 저녁(원본 안바뀜)] ➔ [2단계: 아침] ➔ [3단계: 노을]
 *    순서로 텍스처(u_texStage0, u_texStage1, u_texStage2)를 WebGL GLSL 셰이더에서 스무스 크로스페이드합니다.
 * 2. 1단계(Stage 0)는 현재 띄워져 있는 원본 사진 그대로(무보정 100% 원본 유지)에서 시작합니다.
 * 3. 단계가 넘어갈 때마다 Web Audio API의 GainNode 이퀄-파워 크로스페이드(Equal-Power Crossfade)로
 *    배경 환경음(MP3)이 끊김 없이 부드럽게 교차됩니다.
 * 4. 영상 시연용 배속(1x, 2x, 5x, 10x, 20x) 및 단계별 즉시 이동, 흑백 깊이 맵(Depth Map) 보기를 지원합니다.
 */

(function () {
    "use strict";

    // =========================================================================
    // 1. Web Audio API GainNode 기반 Equal-Power Crossfade 오디오 매니저 클래스
    // =========================================================================
    class WindoWebAudioCrossfader {
        constructor() {
            this.ctx = null;
            this.masterGain = null;
            this.activeChannel = null;   // { url, audioEl, sourceNode, gainNode, slot, title, icon }
            this.fadingChannels = [];
            this.masterVolume = 0.78;
            this.isMuted = false;
            this.crossfadeDurationSec = 1.8;
            this._unlockBound = false;
            this._bindAutoUnlock();
        }

        _ensureContext() {
            if (!this.ctx) {
                const AudioCtx = window.AudioContext || window.webkitAudioContext;
                if (!AudioCtx) return null;
                this.ctx = new AudioCtx();
                this.masterGain = this.ctx.createGain();
                this.masterGain.gain.value = this.isMuted ? 0.0 : this.masterVolume;
                this.masterGain.connect(this.ctx.destination);
            }
            if (this.ctx && this.ctx.state === "suspended") {
                this.ctx.resume().catch(function () {});
            }
            return this.ctx;
        }

        _bindAutoUnlock() {
            if (this._unlockBound) return;
            this._unlockBound = true;
            const self = this;
            const unlockHandler = function () {
                if (self.ctx && self.ctx.state === "suspended") {
                    self.ctx.resume().catch(function () {});
                }
                if (self.activeChannel && self.activeChannel.audioEl && self.activeChannel.audioEl.paused) {
                    self.activeChannel.audioEl.play().catch(function () {});
                }
            };
            document.addEventListener("pointerdown", unlockHandler, { passive: true });
            document.addEventListener("keydown", unlockHandler, { passive: true });
        }

        /**
         * Equal-Power Crossfade 커브 생성 (cos / sin 곡선으로 두 사운드 교차 시 음량 저하 방지)
         */
        static createEqualPowerCurves(steps = 64) {
            const fadeIn = new Float32Array(steps);
            const fadeOut = new Float32Array(steps);
            for (let i = 0; i < steps; i++) {
                const t = i / (steps - 1);
                const angle = t * (Math.PI * 0.5);
                fadeIn[i] = Math.sin(angle);
                fadeOut[i] = Math.cos(angle);
            }
            return { fadeIn, fadeOut };
        }

        /**
         * 목표 환경음 URL로 부드럽게 Equal-Power Crossfade 전환
         */
        crossfadeTo(url, meta = {}, durationSec = 1.8) {
            if (!url) return;

            // 기존 일반 창문 앰비언트 오디오와 겹치지 않도록 일시 정지
            if (window.__windoAmbientAudio && !window.__windoAmbientAudio.paused) {
                window.__windoAmbientAudio.pause();
            }

            const ctx = this._ensureContext();
            if (!ctx) return;

            // 이미 같은 트랙이 재생 중이면 메타데이터만 갱신하고 끊김 없이 유지
            if (this.activeChannel && this.activeChannel.url === url) {
                this.activeChannel.slot = meta.slot || this.activeChannel.slot;
                this.activeChannel.title = meta.title || this.activeChannel.title;
                this.activeChannel.icon = meta.icon || this.activeChannel.icon;
                if (this.activeChannel.audioEl.paused) {
                    this.activeChannel.audioEl.play().catch(function () {});
                }
                return;
            }

            const prevChannel = this.activeChannel;
            const audioEl = new Audio(url);
            audioEl.loop = true;
            audioEl.preload = "auto";
            audioEl.crossOrigin = "anonymous";

            const gainNode = ctx.createGain();
            gainNode.gain.value = 0.0001;

            let sourceNode = null;
            try {
                sourceNode = ctx.createMediaElementSource(audioEl);
                sourceNode.connect(gainNode);
                gainNode.connect(this.masterGain);
            } catch (err) {
                console.warn("Web Audio MediaElementSource 연결 경고:", err);
            }

            const newChannel = {
                url: url,
                audioEl: audioEl,
                sourceNode: sourceNode,
                gainNode: gainNode,
                slot: meta.slot || "",
                title: meta.title || "환경음",
                icon: meta.icon || "🔊",
                reason: meta.reason_ko || "",
            };
            this.activeChannel = newChannel;

            const now = ctx.currentTime;
            const dur = Math.max(0.35, Number(durationSec) || this.crossfadeDurationSec);
            const curves = WindoWebAudioCrossfader.createEqualPowerCurves(64);

            // 1) 새 사운드 GainNode: Equal-Power Sine 커브로 부드럽게 페이드 인
            try {
                gainNode.gain.cancelScheduledValues(now);
                gainNode.gain.setValueCurveAtTime(curves.fadeIn, now, dur);
            } catch (e) {
                gainNode.gain.setValueAtTime(0.0001, now);
                gainNode.gain.linearRampToValueAtTime(1.0, now + dur);
            }

            const playPromise = audioEl.play();
            if (playPromise && typeof playPromise.catch === "function") {
                playPromise.catch(function () {});
            }

            // 2) 이전 사운드 GainNode: Equal-Power Cosine 커브로 부드럽게 페이드 아웃 후 정리
            if (prevChannel && prevChannel.gainNode) {
                this.fadingChannels.push(prevChannel);
                try {
                    prevChannel.gainNode.gain.cancelScheduledValues(now);
                    prevChannel.gainNode.gain.setValueCurveAtTime(curves.fadeOut, now, dur);
                } catch (e) {
                    prevChannel.gainNode.gain.linearRampToValueAtTime(0.0001, now + dur);
                }
                setTimeout(() => {
                    try {
                        if (prevChannel.audioEl) {
                            prevChannel.audioEl.pause();
                            prevChannel.audioEl.src = "";
                        }
                        if (prevChannel.gainNode) {
                            prevChannel.gainNode.disconnect();
                        }
                    } catch (e) {}
                    this.fadingChannels = this.fadingChannels.filter((c) => c !== prevChannel);
                }, Math.ceil(dur * 1000) + 120);
            }
        }

        setMasterVolume(vol0to1) {
            this.masterVolume = Math.max(0.0, Math.min(1.0, Number(vol0to1)));
            if (this.masterGain && this.ctx) {
                this.masterGain.gain.setTargetAtTime(
                    this.isMuted ? 0.0 : this.masterVolume,
                    this.ctx.currentTime,
                    0.08
                );
            }
        }

        toggleMute() {
            this.isMuted = !this.isMuted;
            this.setMasterVolume(this.masterVolume);
            return this.isMuted;
        }

        stopAll() {
            if (this.activeChannel) {
                try {
                    this.activeChannel.audioEl.pause();
                    this.activeChannel.audioEl.src = "";
                    this.activeChannel.gainNode.disconnect();
                } catch (e) {}
                this.activeChannel = null;
            }
            for (const ch of this.fadingChannels) {
                try {
                    ch.audioEl.pause();
                    ch.gainNode.disconnect();
                } catch (e) {}
            }
            this.fadingChannels = [];
        }
    }

    // =========================================================================
    // 2. WebGL GLSL 3단계 순환 멀티-텍스처 크로스페이드 & 2.5D 깊이 맵 셰이더
    // =========================================================================
    const VERTEX_SHADER_SRC = `
        attribute vec2 a_position;
        attribute vec2 a_texCoord;
        varying vec2 v_uv;
        void main() {
            gl_Position = vec4(a_position, 0.0, 1.0);
            v_uv = a_texCoord;
        }
    `;

    const FRAGMENT_SHADER_SRC = `
        precision highp float;
        varying vec2 v_uv;

        uniform sampler2D u_texStage0;   // 1단계: 현재 띄워진 사진 원본 (안 바뀜!)
        uniform sampler2D u_texStage1;   // 2단계: 다음 시간대 변환 텍스처
        uniform sampler2D u_texStage2;   // 3단계: 다다음 시간대 변환 텍스처
        uniform sampler2D u_texDepth;    // 흑백 깊이 맵 (Depth Map)

        uniform float u_stageProgress;   // 0.0 ~ 3.0 연속 순환 진행도
        uniform vec2 u_parallax;         // 마우스 + 자동 호흡 드리프트 오프셋
        uniform float u_showDepth;       // 1.0 이면 흑백 깊이 맵 직접 표시

        void main() {
            // 1. 흑백 깊이 맵 샘플링 (0.0: 원경 하늘, 1.0: 근경 지형/피사체)
            float depth = texture2D(u_texDepth, v_uv).r;

            if (u_showDepth > 0.5) {
                gl_FragColor = vec4(vec3(depth), 1.0);
                return;
            }

            // 2. 깊이 맵 기반 2.5D 입체 패럴랙스 오프셋
            float parallaxStrength = (depth - 0.30) * 0.018;
            vec2 uv = clamp(v_uv + u_parallax * parallaxStrength, vec2(0.002), vec2(0.998));

            // 3. 3단계 시간대 텍스처 샘플링
            //    col0 = 현재 창문 원본 사진 (100% 무보정 안 바뀜 보장)
            vec4 col0 = texture2D(u_texStage0, uv);
            vec4 col1 = texture2D(u_texStage1, uv);
            vec4 col2 = texture2D(u_texStage2, uv);

            // 4. u_stageProgress (0.0 ~ 3.0)에 따른 3단계 스무스 크로스페이드
            //    각 단계 시작 구간(0.0~0.28, 1.0~1.28, 2.0~2.28)에서는 해당 단계 이미지를 선명하게 유지하고,
            //    이후 구간(0.28~1.00 등)에서 smoothstep으로 다음 시간대 이미지로 스르륵 전환합니다.
            float p = mod(u_stageProgress, 3.0);
            vec4 blended = col0;

            if (p < 1.0) {
                float t = smoothstep(0.26, 0.96, p);
                blended = mix(col0, col1, t);
            } else if (p < 2.0) {
                float localP = p - 1.0;
                float t = smoothstep(0.26, 0.96, localP);
                blended = mix(col1, col2, t);
            } else {
                float localP = p - 2.0;
                float t = smoothstep(0.26, 0.96, localP);
                blended = mix(col2, col0, t);
            }

            gl_FragColor = vec4(clamp(blended.rgb, 0.0, 1.0), 1.0);
        }
    `;

    const PHASE_BADGE_NAMES = {
        morning: "🌅 아침",
        sunset: "🌇 노을",
        evening: "🌙 저녁",
    };

    class WindoTimeLapseController {
        constructor() {
            this.audioCrossfader = new WindoWebAudioCrossfader();
            this.canvas = null;
            this.gl = null;
            this.program = null;
            this.textures = {};
            this.loadedBundleId = "";
            this.bundleData = null;

            // 0.0 = 1단계(현재 사진 원본 안바뀜), 1.0 = 2단계, 2.0 = 3단계
            this.stageProgress = 0.0;
            this.isPlaying = true;
            this.speedMultiplier = 5.0;  // 1x, 2x, 5x, 10x, 20x
            this.showDepthMap = false;
            this.currentAudioStage = -1;

            this.mouseX = 0.0;
            this.mouseY = 0.0;
            this.targetMouseX = 0.0;
            this.targetMouseY = 0.0;
            this.lastFrameTs = performance.now();
            this._rafId = null;

            this._bindMouseParallax();
            this._startLoop();
        }

        _bindMouseParallax() {
            window.addEventListener("mousemove", (e) => {
                const nx = (e.clientX / Math.max(1, window.innerWidth)) * 2.0 - 1.0;
                const ny = (e.clientY / Math.max(1, window.innerHeight)) * 2.0 - 1.0;
                this.targetMouseX = nx;
                this.targetMouseY = -ny;
            }, { passive: true });
        }

        /**
         * DOM에 #windo-timelapse-webgl-canvas 가 마운트되었을 때 WebGL 초기화 및 새 번들 로드
         */
        mountIfPresent(runtimeConfig) {
            const canvasEl = document.getElementById("windo-timelapse-webgl-canvas");
            if (!canvasEl) {
                this.audioCrossfader.stopAll();
                this.currentAudioStage = -1;
                return;
            }

            if (runtimeConfig && runtimeConfig.bundle) {
                const incomingBundle = runtimeConfig.bundle;
                if (incomingBundle.bundle_id && incomingBundle.bundle_id !== this.loadedBundleId) {
                    this.bundleData = incomingBundle;
                    this.loadedBundleId = incomingBundle.bundle_id;
                    // 새 타임랩스 시작 시 무조건 1단계(현재 창문 사진 원본 = 안 바뀜, progress 0.0)에서 출발!
                    this.stageProgress = 0.0;
                    this.isPlaying = true;
                    this.showDepthMap = false;
                    this.currentAudioStage = -1;
                    this._texturesDirty = true;
                } else if (!this.bundleData) {
                    this.bundleData = incomingBundle;
                }
            }

            if (this.canvas !== canvasEl || !this.gl) {
                this.canvas = canvasEl;
                this._initWebGL(canvasEl);
                this._texturesDirty = true;
            }

            if (this._texturesDirty && this.bundleData && this.bundleData.textures) {
                this._texturesDirty = false;
                this._loadBundleTextures(this.bundleData);
            }

            this._syncAudioForCurrentStage();
            this._updateHudDom();
        }

        _initWebGL(canvas) {
            const gl = canvas.getContext("webgl", { alpha: false, antialias: true })
                || canvas.getContext("experimental-webgl");
            if (!gl) return;
            this.gl = gl;

            const compile = (type, src) => {
                const sh = gl.createShader(type);
                gl.shaderSource(sh, src);
                gl.compileShader(sh);
                if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
                    console.error("GLSL 셰이더 컴파일 오류:", gl.getShaderInfoLog(sh));
                }
                return sh;
            };

            const vs = compile(gl.VERTEX_SHADER, VERTEX_SHADER_SRC);
            const fs = compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER_SRC);
            const prog = gl.createProgram();
            gl.attachShader(prog, vs);
            gl.attachShader(prog, fs);
            gl.linkProgram(prog);
            gl.useProgram(prog);
            this.program = prog;

            const vertices = new Float32Array([
                -1.0, -1.0,  0.0, 1.0,
                 1.0, -1.0,  1.0, 1.0,
                -1.0,  1.0,  0.0, 0.0,
                -1.0,  1.0,  0.0, 0.0,
                 1.0, -1.0,  1.0, 1.0,
                 1.0,  1.0,  1.0, 0.0,
            ]);
            const buf = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, buf);
            gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

            const posLoc = gl.getAttribLocation(prog, "a_position");
            const uvLoc = gl.getAttribLocation(prog, "a_texCoord");
            gl.enableVertexAttribArray(posLoc);
            gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 16, 0);
            gl.enableVertexAttribArray(uvLoc);
            gl.vertexAttribPointer(uvLoc, 2, gl.FLOAT, false, 16, 8);

            const slots = ["stage0", "stage1", "stage2", "depth_map"];
            this.textures = {};
            slots.forEach((key, idx) => {
                const tex = gl.createTexture();
                gl.activeTexture(gl.TEXTURE0 + idx);
                gl.bindTexture(gl.TEXTURE_2D, tex);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
                gl.texImage2D(
                    gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
                    new Uint8Array([18, 28, 48, 255])
                );
                this.textures[key] = tex;
            });

            gl.uniform1i(gl.getUniformLocation(prog, "u_texStage0"), 0);
            gl.uniform1i(gl.getUniformLocation(prog, "u_texStage1"), 1);
            gl.uniform1i(gl.getUniformLocation(prog, "u_texStage2"), 2);
            gl.uniform1i(gl.getUniformLocation(prog, "u_texDepth"), 3);
        }

        _loadBundleTextures(bundle) {
            if (!this.gl || !bundle || !bundle.textures) return;
            const gl = this.gl;
            const texUrls = bundle.textures;
            const phaseOrder = bundle.phase_order || ["morning", "sunset", "evening"];

            const resolvedUrls = {
                stage0: texUrls.stage0 || texUrls[phaseOrder[0]] || texUrls.original,
                stage1: texUrls.stage1 || texUrls[phaseOrder[1]] || texUrls.sunset,
                stage2: texUrls.stage2 || texUrls[phaseOrder[2]] || texUrls.evening,
                depth_map: texUrls.depth_map || texUrls.stage0,
            };

            const slots = ["stage0", "stage1", "stage2", "depth_map"];
            slots.forEach((key, idx) => {
                const url = resolvedUrls[key];
                if (!url) return;
                const img = new Image();
                img.crossOrigin = "anonymous";
                img.onload = () => {
                    if (!this.gl || !this.textures[key]) return;
                    gl.activeTexture(gl.TEXTURE0 + idx);
                    gl.bindTexture(gl.TEXTURE_2D, this.textures[key]);
                    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
                };
                img.src = url;
            });
        }

        /**
         * 현재 stageProgress(0.0 ~ 3.0)에서 주도적인 단계 인덱스(0, 1, 2) 산출
         * 크로스페이드 중반(0.62, 1.62, 2.62)을 넘어가면 다음 단계 환경음으로 미리 크로스페이드
         */
        _getActiveStageIndex(prog) {
            const p = ((Number(prog) % 3.0) + 3.0) % 3.0;
            if (p < 0.62 || p >= 2.62) return 0;
            if (p < 1.62) return 1;
            return 2;
        }

        _getTrackForStage(stageIdx) {
            if (!this.bundleData || !this.bundleData.audio_tracks) return null;
            const tracks = this.bundleData.audio_tracks;
            const stageKey = "stage" + stageIdx;
            if (tracks[stageKey]) return tracks[stageKey];
            const order = this.bundleData.phase_order || ["morning", "sunset", "evening"];
            const phaseKey = order[stageIdx] || "morning";
            return tracks[phaseKey] || null;
        }

        _syncAudioForCurrentStage() {
            const stageIdx = this._getActiveStageIndex(this.stageProgress);
            if (stageIdx !== this.currentAudioStage) {
                this.currentAudioStage = stageIdx;
                const trackInfo = this._getTrackForStage(stageIdx);
                if (trackInfo && trackInfo.url) {
                    this.audioCrossfader.crossfadeTo(trackInfo.url, trackInfo, 1.8);
                }
            }
        }

        /**
         * 특정 단계(0: 1단계 원본 안바뀜, 1: 2단계, 2: 3단계)로 즉시 이동
         */
        setStage(stageIdx, pauseAuto = false) {
            const idx = ((Number(stageIdx) % 3) + 3) % 3;
            this.stageProgress = idx;
            if (pauseAuto) this.isPlaying = false;
            this._syncAudioForCurrentStage();
            this._updateHudDom();
        }

        /**
         * 슬라이더(0.0 ~ 3.0) 조작 시 진행도 직접 설정
         */
        setProgress(progVal, pauseAuto = true) {
            this.stageProgress = ((Number(progVal) % 3.0) + 3.0) % 3.0;
            if (pauseAuto) this.isPlaying = false;
            this._syncAudioForCurrentStage();
            this._updateHudDom();
        }

        /**
         * 하위 호환용 인터페이스
         */
        setHour(val, pauseAuto = false) {
            const num = Number(val);
            if (num <= 3.0) {
                this.setProgress(num, pauseAuto);
            } else {
                this.setProgress((num / 24.0) * 3.0, pauseAuto);
            }
        }

        togglePlay() {
            this.isPlaying = !this.isPlaying;
            this._updateHudDom();
            return this.isPlaying;
        }

        setSpeed(mult) {
            this.speedMultiplier = Number(mult) || 5.0;
            this.isPlaying = true;
            this._updateHudDom();
        }

        toggleDepthMap() {
            this.showDepthMap = !this.showDepthMap;
            this._updateHudDom();
            return this.showDepthMap;
        }

        _updateHudDom() {
            const clockEl = document.getElementById("tl-live-clock");
            const phaseEl = document.getElementById("tl-live-phase");
            const sliderEl = document.getElementById("tl-hour-slider");
            const playBtnEl = document.getElementById("tl-play-btn");
            const depthBtnEl = document.getElementById("tl-depth-btn");
            const audioBadgeEl = document.getElementById("tl-audio-status");

            const p = ((this.stageProgress % 3.0) + 3.0) % 3.0;
            const stageIdx = this._getActiveStageIndex(p);
            const nextIdx = (stageIdx + 1) % 3;

            const order = (this.bundleData && this.bundleData.phase_order) || ["morning", "sunset", "evening"];
            const labelsKo = (this.bundleData && this.bundleData.phase_labels_ko) || [
                "🌅 아침 (원본 유지 · 안 바뀜)",
                "🌇 노을 (AI 변환)",
                "🌙 저녁 (AI 변환)",
            ];

            if (clockEl) {
                const curShort = PHASE_BADGE_NAMES[order[stageIdx]] || `단계 ${stageIdx + 1}`;
                clockEl.textContent = `${stageIdx + 1}/3단계 · ${curShort}`;
            }
            if (phaseEl) {
                const curLabel = labelsKo[stageIdx] || "";
                const nextShort = PHASE_BADGE_NAMES[order[nextIdx]] || "";
                phaseEl.textContent = `${curLabel} ➔ 다음: ${nextShort}`;
            }
            if (sliderEl && document.activeElement !== sliderEl) {
                sliderEl.value = p.toFixed(2);
            }
            if (playBtnEl) {
                playBtnEl.textContent = this.isPlaying ? "⏸ 일시정지" : "▶ 타임랩스 재생";
            }
            if (depthBtnEl) {
                depthBtnEl.textContent = this.showDepthMap ? "🖼️ 컬러 타임랩스 보기" : "🗺️ 깊이 맵(Depth) 보기";
                depthBtnEl.style.background = this.showDepthMap
                    ? "linear-gradient(135deg, #0284c7, #2563eb)"
                    : "rgba(255,255,255,0.14)";
            }
            if (audioBadgeEl) {
                const tr = this._getTrackForStage(stageIdx);
                if (tr) {
                    audioBadgeEl.textContent = `${tr.icon} ${tr.title} (GainNode Crossfade)`;
                }
            }

            // 현재 단계 버튼 하이라이트
            document.querySelectorAll(".tl-stage-btn").forEach((btn) => {
                const st = Number(btn.getAttribute("data-stage"));
                if (st === stageIdx) {
                    btn.style.background = "linear-gradient(135deg, #f59e0b, #ea580c)";
                    btn.style.borderColor = "#fbbf24";
                } else {
                    btn.style.background = "rgba(255,255,255,0.13)";
                    btn.style.borderColor = "rgba(255,255,255,0.24)";
                }
            });

            // 배속 버튼 활성화 표시
            document.querySelectorAll(".tl-speed-btn").forEach((btn) => {
                const sp = Number(btn.getAttribute("data-speed"));
                if (Math.abs(sp - this.speedMultiplier) < 0.05) {
                    btn.classList.add("active");
                } else {
                    btn.classList.remove("active");
                }
            });
        }

        _startLoop() {
            const step = (nowTs) => {
                const dt = Math.min(0.1, (nowTs - this.lastFrameTs) / 1000.0);
                this.lastFrameTs = nowTs;

                if (this.canvas && document.body.contains(this.canvas) && this.gl && this.program) {
                    if (this.isPlaying) {
                        // 5배속 기준 초당 0.35 단계 진행 (1단계당 약 2.8초, 3단계 전체 순환 약 8.5초)
                        this.stageProgress = (this.stageProgress + dt * 0.07 * this.speedMultiplier) % 3.0;
                        this._syncAudioForCurrentStage();
                        this._updateHudDom();
                    }

                    const dpr = Math.min(2.0, window.devicePixelRatio || 1.0);
                    const dispW = Math.floor(this.canvas.clientWidth * dpr);
                    const dispH = Math.floor(this.canvas.clientHeight * dpr);
                    if (this.canvas.width !== dispW || this.canvas.height !== dispH) {
                        this.canvas.width = dispW;
                        this.canvas.height = dispH;
                        this.gl.viewport(0, 0, dispW, dispH);
                    }

                    // 부드러운 마우스 + 자동 호흡 패럴랙스 보간
                    const autoDriftX = Math.sin(nowTs * 0.00055) * 0.35;
                    const autoDriftY = Math.cos(nowTs * 0.00042) * 0.20;
                    this.mouseX += ((this.targetMouseX + autoDriftX) - this.mouseX) * 0.06;
                    this.mouseY += ((this.targetMouseY + autoDriftY) - this.mouseY) * 0.06;

                    const gl = this.gl;
                    gl.useProgram(this.program);
                    gl.uniform1f(gl.getUniformLocation(this.program, "u_stageProgress"), this.stageProgress);
                    gl.uniform2f(gl.getUniformLocation(this.program, "u_parallax"), this.mouseX, this.mouseY);
                    gl.uniform1f(
                        gl.getUniformLocation(this.program, "u_showDepth"),
                        this.showDepthMap ? 1.0 : 0.0
                    );
                    gl.drawArrays(gl.TRIANGLES, 0, 6);
                }

                this._rafId = requestAnimationFrame(step);
            };
            this._rafId = requestAnimationFrame(step);
        }
    }

    if (!window.__windoTimeLapseController) {
        window.__windoTimeLapseController = new WindoTimeLapseController();
    }
})();
