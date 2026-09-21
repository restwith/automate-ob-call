"""OB콜 사운드보드 PoC.

헤드셋 마이크와 녹음 멘트를 섞어 VB-CABLE로 보낸다. Windows 기본 마이크를
CABLE Output으로 지정해 두면 '휴대폰과 연결' 통화에 그대로 실린다.

  헤드셋 마이크 ─┐
                 ├─ 믹서 ─→ CABLE Input ══ CABLE Output(Windows 기본 마이크) ─→ 휴대폰과 연결 ─→ 상대방
  녹음 멘트 ─────┤
                 └────────→ 내가 들을 장치 (멘트만)

실행: run.bat 더블클릭    점검: .venv\\Scripts\\python soundboard.py --check
"""

import argparse
import collections
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import av
import numpy as np
import sounddevice as sd

SR = 48000
HERE = Path(__file__).resolve().parent
CLIP_DIR = HERE / "clips"
SETTINGS_PATH = HERE / "settings.json"
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac"}
MIC_MAX_LAG = int(0.1 * SR)  # 마이크 버퍼 상한. 넘치면 오래된 소리를 버려 지연을 100ms 이내로 유지
RAMP = int(0.02 * SR)  # 마이크 켜기/끄기와 정지에 쓰는 페이드 길이 (딸깍 소리 방지)
TARGET_RMS = 10 ** (-20 / 20)  # 멘트마다 음량이 들쭉날쭉하지 않도록 이 수준으로 맞춤
NO_MONITOR = "(듣지 않음)"
FONT = "맑은 고딕"

logging.basicConfig(
    filename=HERE / "soundboard.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("soundboard")


# ── 장치 ──────────────────────────────────────────────

def wasapi():
    for i, h in enumerate(sd.query_hostapis()):
        if "WASAPI" in h["name"]:
            return i, h
    raise RuntimeError("WASAPI 오디오 장치를 찾을 수 없습니다.")


def devices(kind):
    api, _ = wasapi()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == api and d[key] > 0]


def windows_default(kind):
    _, h = wasapi()
    i = h["default_input_device" if kind == "input" else "default_output_device"]
    return sd.query_devices(i)["name"] if i >= 0 else ""


def is_cable(name):
    return "CABLE" in name.upper()


def default_choice(key, names, saved):
    if saved in names:
        return saved
    rules = {
        "mic": [lambda n: not is_cable(n)],
        "cable": [lambda n: "CABLE INPUT" in n.upper(), is_cable],
        "monitor": [lambda n: n == windows_default("output") and not is_cable(n),
                    lambda n: n != NO_MONITOR and not is_cable(n)],
    }[key]
    for rule in rules:
        for n in names:
            if rule(n):
                return n
    return names[0] if names else ""


def reset_audio():
    """Windows 장치 설정을 바꾼 뒤 목록을 새로 읽는다."""
    sd._terminate()
    sd._initialize()


# ── 멘트 파일 ─────────────────────────────────────────

def load_clip(path):
    chunks = []
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    x = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)
    return normalize(trim(x))


def trim(x, threshold=10 ** (-45 / 20), pad=int(0.05 * SR)):
    loud = np.flatnonzero(np.abs(x) > threshold)
    if loud.size == 0:
        return x
    return x[max(loud[0] - pad, 0):loud[-1] + pad]


def normalize(x):
    peak = float(np.max(np.abs(x), initial=0))
    if peak == 0:
        return x
    rms = float(np.sqrt(np.mean(x ** 2)))
    return x * min(TARGET_RMS / rms, 0.95 / peak)


def scan_clips():
    CLIP_DIR.mkdir(exist_ok=True)
    clips, failed = [], []
    for p in sorted(CLIP_DIR.iterdir()):
        if p.suffix.lower() not in AUDIO_EXT:
            continue
        try:
            label = re.sub(r"^\d+[\s._-]*", "", p.stem) or p.stem
            clips.append((label, load_clip(p)))
        except Exception:
            log.exception("멘트 파일을 읽지 못함: %s", p.name)
            failed.append(p.name)
    return clips, failed


# ── 오디오 엔진 ───────────────────────────────────────

class Fifo:
    """마이크 입력과 통화 출력이 서로 다른 장치 시계로 돌아가므로 그 사이를 잇는 버퍼."""

    def __init__(self, max_samples):
        self.lock = threading.Lock()
        self.chunks = collections.deque()
        self.size = 0
        self.max = max_samples

    def write(self, x):
        with self.lock:
            self.chunks.append(x)
            self.size += len(x)
            while self.size > self.max:
                self.size -= len(self.chunks.popleft())

    def read(self, n):
        out = np.zeros(n, np.float32)
        i = 0
        with self.lock:
            while i < n and self.chunks:
                head = self.chunks[0]
                k = min(n - i, len(head))
                out[i:i + k] = head[:k]
                i += k
                self.size -= k
                if k == len(head):
                    self.chunks.popleft()
                else:
                    self.chunks[0] = head[k:]
        return out


class Engine:
    """마이크와 멘트를 섞어 통화 장치로, 멘트만 따로 내가 들을 장치로 보낸다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mic_fifo = Fifo(MIC_MAX_LAG)
        self.mic_on = True  # 인삿말은 육성으로 하므로 수동 모드로 시작
        self.gain = 1.0
        self.clip = None
        self.clip_index = None
        self.pos = 0
        self.monitor_pos = 0
        self.mic_level = 0.0
        self.out_level = 0.0
        self.xruns = 0
        self.streams = []

    def open(self, mic, cable, monitor):
        self.close()
        settings = sd.WasapiSettings(auto_convert=True)

        def stream(cls, device, key, callback):
            return cls(device=device, channels=min(2, sd.query_devices(device)[key]),
                       samplerate=SR, dtype="float32", latency="low",
                       extra_settings=settings, callback=callback)

        try:
            self.streams.append(stream(sd.InputStream, mic, "max_input_channels", self._on_mic))
            self.streams.append(stream(sd.OutputStream, cable, "max_output_channels", self._on_cable))
            if monitor is not None:
                self.streams.append(
                    stream(sd.OutputStream, monitor, "max_output_channels", self._on_monitor))
            for s in self.streams:
                s.start()
        except Exception:
            self.close()
            raise

    def close(self):
        for s in self.streams:
            try:
                s.close()
            except Exception:
                log.exception("오디오 스트림 닫기 실패")
        self.streams = []

    def play(self, index, data):
        with self.lock:
            self.clip, self.clip_index = data, index
            self.pos = self.monitor_pos = 0
        self.mic_on = False  # 멘트와 내 목소리가 겹치지 않게 자동 모드로

    def stop(self):
        with self.lock:
            if not self.playing():
                return
            end = min(self.pos + RAMP, len(self.clip))
            clip = self.clip[:end].copy()
            clip[self.pos:end] *= np.linspace(1, 0, end - self.pos, dtype=np.float32)
            self.clip = clip

    def playing(self):
        return self.clip is not None and self.pos < len(self.clip)

    def progress(self):
        with self.lock:
            if not self.playing():
                return None, 0, 0
            return self.clip_index, self.pos, len(self.clip)

    def _next(self, frames, monitor):
        out = np.zeros(frames, np.float32)
        with self.lock:
            if self.clip is None:
                return out
            pos = self.monitor_pos if monitor else self.pos
            chunk = self.clip[pos:pos + frames]
            out[:len(chunk)] = chunk
            if monitor:
                self.monitor_pos += frames
            else:
                self.pos += frames
        return out

    def _on_mic(self, indata, frames, time_info, status):
        if status:
            self.xruns += 1
        x = indata.mean(axis=1)
        self.mic_level = float(np.max(np.abs(x), initial=0))
        self.mic_fifo.write(x)

    def _on_cable(self, outdata, frames, time_info, status):
        if status:
            self.xruns += 1
        target = 1.0 if self.mic_on else 0.0
        step = frames / RAMP
        end = self.gain + max(-step, min(step, target - self.gain))
        out = self.mic_fifo.read(frames) * np.linspace(self.gain, end, frames, dtype=np.float32)
        self.gain = end
        out += self._next(frames, monitor=False)
        np.clip(out, -1, 1, out=out)
        self.out_level = float(np.max(np.abs(out), initial=0))
        outdata[:] = out[:, None]

    def _on_monitor(self, outdata, frames, time_info, status):
        outdata[:] = self._next(frames, monitor=True)[:, None]


# ── 화면 ──────────────────────────────────────────────

def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def meter(peak):
    """-60dB ~ 0dB를 0 ~ 1로."""
    return 0.0 if peak <= 1e-3 else min(1.0, 1 + 20 * np.log10(peak) / 60)


class App:
    def __init__(self, root):
        self.root = root
        self.engine = Engine()
        self.clips = []
        self.clip_buttons = []
        self.settings = load_settings()

        root.title("OB콜 사운드보드 (PoC)")
        root.minsize(540, 620)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.report_callback_exception = self._on_error

        self._build()
        for n in range(1, 10):
            root.bind(f"<Key-{n}>", lambda e, i=n - 1: self.play(i))
        root.bind("<space>", lambda e: self.toggle())
        root.bind("<Escape>", lambda e: self.stop())

        self.reload_clips()
        self.open_devices()
        self._tick()

    def _build(self):
        pad = {"padx": 10, "pady": 4}

        self.device_frame = ttk.LabelFrame(self.root, text="장치 (처음 한 번만 설정)")
        self.device_frame.pack(fill="x", **pad)
        self.device_vars = {}
        rows = [("mic", "헤드셋 마이크", "input"),
                ("cable", "통화로 보낼 장치", "output"),
                ("monitor", "내가 들을 장치", "output")]
        for r, (key, label, kind) in enumerate(rows):
            ttk.Label(self.device_frame, text=label).grid(row=r, column=0, sticky="w", padx=6, pady=2)
            var = tk.StringVar()
            box = ttk.Combobox(self.device_frame, textvariable=var, state="readonly", width=46)
            box.grid(row=r, column=1, sticky="ew", padx=6, pady=2)
            box.bind("<<ComboboxSelected>>", lambda e: (self.open_devices(), self.root.focus_set()))
            self.device_vars[key] = (var, box, kind)
        self.device_frame.columnconfigure(1, weight=1)

        self.warning = ttk.Frame(self.root)
        self.warning_label = tk.Label(self.warning, fg="#c62828", justify="left",
                                      anchor="w", wraplength=380)
        self.warning_label.pack(side="left", fill="x", expand=True)
        buttons = ttk.Frame(self.warning)
        buttons.pack(side="right")
        ttk.Button(buttons, text="녹음 장치 설정 열기", takefocus=False,
                   command=lambda: subprocess.Popen(["control", "mmsys.cpl,,1"])).pack(fill="x")
        ttk.Button(buttons, text="장치 다시 확인", takefocus=False,
                   command=self.recheck_devices).pack(fill="x", pady=(2, 0))

        self.mode_button = tk.Button(self.root, font=(FONT, 14, "bold"), fg="white",
                                     activeforeground="white", height=2, takefocus=0,
                                     command=self.toggle)
        self.mode_button.pack(fill="x", **pad)

        self.clip_frame = ttk.LabelFrame(self.root, text="녹음 멘트 (숫자 키로도 재생)")
        self.clip_frame.pack(fill="both", expand=True, **pad)

        prog = ttk.Frame(self.root)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, maximum=1.0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.remaining = ttk.Label(prog, width=10, anchor="e")
        self.remaining.pack(side="right")

        tk.Button(self.root, text="■  정지하고 내가 말하기    [Esc]", font=(FONT, 12, "bold"),
                  bg="#c62828", fg="white", activebackground="#b71c1c", activeforeground="white",
                  takefocus=0, command=self.stop).pack(fill="x", **pad)

        meters = ttk.Frame(self.root)
        meters.pack(fill="x", **pad)
        self.meters = {}
        for c, (key, label) in enumerate([("mic", "마이크 입력"), ("out", "통화로 나가는 소리")]):
            ttk.Label(meters, text=label).grid(row=0, column=c * 2, padx=(0, 4))
            bar = ttk.Progressbar(meters, maximum=1.0, length=140)
            bar.grid(row=0, column=c * 2 + 1, padx=(0, 14))
            self.meters[key] = bar

        tools = ttk.Frame(self.root)
        tools.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(tools, text="멘트 폴더 열기", takefocus=False,
                   command=lambda: os.startfile(CLIP_DIR)).pack(side="left")
        ttk.Button(tools, text="멘트 다시 불러오기", takefocus=False,
                   command=self.reload_clips).pack(side="left", padx=6)

    # 동작

    def play(self, index):
        if index < len(self.clips) and self.engine.streams:
            label, data = self.clips[index]
            log.info("재생: %s", label)
            self.engine.play(index, data)

    def stop(self):
        self.engine.stop()
        self.engine.mic_on = True
        log.info("정지 → 수동")

    def toggle(self):
        self.engine.mic_on = not self.engine.mic_on
        log.info("모드: %s", "수동" if self.engine.mic_on else "자동")

    def reload_clips(self):
        self.engine.stop()
        self.clips, failed = scan_clips()
        for w in self.clip_frame.winfo_children():
            w.destroy()
        self.clip_buttons = []
        for i, (label, data) in enumerate(self.clips):
            key = f"{i + 1}   " if i < 9 else "     "
            b = tk.Button(self.clip_frame, text=f"{key}{label}    ({len(data) / SR:.1f}초)",
                          anchor="w", font=(FONT, 12), takefocus=0,
                          command=lambda i=i: self.play(i))
            b.pack(fill="x", padx=6, pady=3)
            self.clip_buttons.append(b)
        if not self.clips:
            ttk.Label(self.clip_frame, justify="center",
                      text="멘트 폴더에 녹음 파일(.m4a 등)을 넣고\n[멘트 다시 불러오기]를 누르세요.\n\n"
                           "파일 이름 앞의 숫자로 순서가 정해집니다.\n예: 1_회사소개.m4a, 2_본인소개.m4a"
                      ).pack(expand=True, pady=20)
        if failed:
            messagebox.showwarning("멘트 파일 오류", "다음 파일을 읽지 못했습니다.\n\n" + "\n".join(failed))

    def open_devices(self):
        lists = {kind: devices(kind) for kind in ("input", "output")}
        choice = {}
        for key, (var, box, kind) in self.device_vars.items():
            names = [n for _, n in lists[kind]]
            if key == "monitor":
                names = [NO_MONITOR] + names
            box["values"] = names
            if var.get() not in names:
                var.set(default_choice(key, names, self.settings.get(key)))
            choice[key] = var.get()

        ids = {kind: {n: i for i, n in lists[kind]} for kind in lists}
        mic = ids["input"].get(choice["mic"])
        cable = ids["output"].get(choice["cable"])
        monitor = None if choice["monitor"] == NO_MONITOR else ids["output"].get(choice["monitor"])
        self.engine.close()
        if mic is None or cable is None:
            messagebox.showerror("장치 오류", "헤드셋 마이크와 통화로 보낼 장치를 선택하세요.")
        else:
            try:
                self.engine.open(mic, cable, monitor)
                log.info("장치 열림: %s", choice)
            except Exception as ex:
                log.exception("장치 열기 실패: %s", choice)
                messagebox.showerror("장치 오류", f"오디오 장치를 열지 못했습니다.\n\n{ex}")
        self.settings.update(choice)
        save_settings(self.settings)
        self._check_setup(choice)

    def recheck_devices(self):
        self.engine.close()
        reset_audio()
        self.open_devices()

    def _check_setup(self, choice):
        problems = []
        default_mic = windows_default("input")
        if "CABLE OUTPUT" not in default_mic.upper():
            problems.append(f"Windows 기본 마이크가 '{default_mic}'입니다. "
                            "녹음 탭에서 CABLE Output을 우클릭해 '기본 장치'와 "
                            "'기본 통신 장치'로 지정하세요.")
        if not is_cable(choice["cable"]):
            problems.append("'통화로 보낼 장치'는 CABLE Input이어야 합니다.")
        if is_cable(choice["mic"]):
            problems.append("'헤드셋 마이크'에 CABLE 장치를 고르면 소리가 되돌아 울립니다.")
        if problems:
            self.warning_label["text"] = "\n".join("⚠ " + p for p in problems)
            self.warning.pack(after=self.device_frame, fill="x", padx=10, pady=4)
        else:
            self.warning.pack_forget()

    def _tick(self):
        e = self.engine
        if e.mic_on:
            self.mode_button.config(text="●  수동 — 내 목소리가 나가는 중    [Space]",
                                    bg="#2e7d32", activebackground="#2e7d32")
        else:
            self.mode_button.config(text="▶  자동 — 마이크 꺼짐, 멘트만 나감    [Space]",
                                    bg="#37474f", activebackground="#37474f")
        index, pos, length = e.progress()
        self.progress["value"] = pos / length if length else 0
        self.remaining["text"] = f"남은 {(length - pos) / SR:.1f}초" if length else ""
        for i, b in enumerate(self.clip_buttons):
            b.config(bg="#fff59d" if i == index else "SystemButtonFace")
        self.meters["mic"]["value"] = meter(e.mic_level)
        self.meters["out"]["value"] = meter(e.out_level)
        self.root.after(50, self._tick)

    def _on_error(self, exc, value, tb):
        log.error("화면 처리 오류", exc_info=(exc, value, tb))
        messagebox.showerror("오류", f"{value}\n\n자세한 내용은 soundboard.log를 확인하세요.")

    def quit(self):
        self.engine.close()
        self.root.destroy()


# ── 점검 모드 ─────────────────────────────────────────

def check():
    """화면 없이 장치 목록, 멘트 파일, 오디오 스트림 열기를 점검한다."""
    for kind, title in (("input", "입력 장치"), ("output", "출력 장치")):
        print(f"[{title}]")
        for i, n in devices(kind):
            print(f"  {i:3d}  {n}")
    default_mic = windows_default("input")
    print(f"\nWindows 기본 마이크: {default_mic}"
          f"  {'OK' if 'CABLE OUTPUT' in default_mic.upper() else '<- CABLE Output으로 바꿔야 함'}")
    print(f"Windows 기본 스피커: {windows_default('output')}")

    clips, failed = scan_clips()
    print(f"\n[멘트 파일] {len(clips)}개")
    for label, data in clips:
        print(f"  {label}  {len(data) / SR:.1f}초")
    for name in failed:
        print(f"  읽기 실패: {name}")

    settings = load_settings()
    lists = {kind: devices(kind) for kind in ("input", "output")}
    names = {k: [n for _, n in v] for k, v in lists.items()}
    choice = {
        "mic": default_choice("mic", names["input"], settings.get("mic")),
        "cable": default_choice("cable", names["output"], settings.get("cable")),
        "monitor": default_choice("monitor", names["output"], settings.get("monitor")),
    }
    ids = {kind: {n: i for i, n in lists[kind]} for kind in lists}
    print("\n[선택될 장치]")
    for k, v in choice.items():
        print(f"  {k}: {v}")

    engine = Engine()
    engine.open(ids["input"][choice["mic"]], ids["output"][choice["cable"]],
                ids["output"].get(choice["monitor"]))
    peak = 0.0
    for _ in range(20):
        time.sleep(0.05)
        peak = max(peak, engine.mic_level)
    engine.close()
    print(f"\n오디오 스트림 1초 동작 OK  (마이크 최대 레벨 {peak:.3f}, 끊김 {engine.xruns}회)")


def main():
    parser = argparse.ArgumentParser(description="OB콜 사운드보드 PoC")
    parser.add_argument("--check", action="store_true", help="화면 없이 장치와 멘트 파일만 점검")
    args = parser.parse_args()
    if args.check:
        check()
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        App(root)
    except Exception as ex:
        log.exception("시작 실패")
        messagebox.showerror("시작 실패", f"{ex}\n\n자세한 내용은 soundboard.log를 확인하세요.")
        root.destroy()
        sys.exit(1)
    root.mainloop()


if __name__ == "__main__":
    main()
