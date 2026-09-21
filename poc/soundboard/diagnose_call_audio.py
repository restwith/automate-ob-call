"""통화 중 어떤 프로그램이 어떤 오디오 장치를 쓰는지 기록한다.

0.5초마다 모든 입출력 장치의 음량과 그 장치를 열고 있는 프로그램을 기록하고,
통화(블루투스 장치 사용 또는 휴대폰과 연결 앱의 오디오)가 끝나면 요약을 출력한다.

실행: .venv\\Scripts\\python diagnose_call_audio.py
"""

import ctypes
import os
import time
from collections import defaultdict

import comtypes
from comtypes import CLSCTX_ALL
from pycaw.constants import CLSID_MMDeviceEnumerator
from pycaw.pycaw import (AudioUtilities, IAudioMeterInformation, IAudioSessionControl2,
                         IAudioSessionManager2, IMMDeviceEnumerator)

POLL = 0.5
TIMEOUT = 20 * 60  # 통화가 시작되지 않아도 20분 뒤 종료
AFTER_CALL = 20  # 통화 신호가 끊긴 뒤 이만큼 더 기다렸다 종료
PHONE_LINK = ("phoneexperiencehost", "yourphone", "phonelink")
CALL_DEVICE = ("bluetooth", "hands-free", "핸즈프리")  # 휴대폰 통화 장치 이름에 들어가는 말


def proc_name(pid, cache={}):
    if pid not in cache:
        name = "system" if pid == 0 else f"pid{pid}"
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid) if pid else None
        if h:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(260)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            ctypes.windll.kernel32.CloseHandle(h)
        cache[pid] = name
    return cache[pid]


def endpoints():
    enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                     comtypes.CLSCTX_INPROC_SERVER)
    result = []
    for flow, label in ((1, "마이크"), (0, "스피커")):
        col = enum.EnumAudioEndpoints(flow, 1)
        for i in range(col.GetCount()):
            dev = col.Item(i)
            try:
                name = AudioUtilities.CreateDevice(dev).FriendlyName
                meter = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None) \
                    .QueryInterface(IAudioMeterInformation)
                sessions = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None) \
                    .QueryInterface(IAudioSessionManager2)
                result.append((f"{label} | {name}", meter, sessions))
            except Exception as ex:
                print(f"건너뜀: {label} {ex}")
    return result


def active_processes(manager):
    names = set()
    sessions = manager.GetSessionEnumerator()
    for j in range(sessions.GetCount()):
        ctl = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
        if ctl.GetState() == 1:  # AudioSessionStateActive
            names.add(proc_name(ctl.GetProcessId()))
    return names


def main():
    comtypes.CoInitialize()
    eps = endpoints()
    print("감시 중인 장치:")
    for name, _, _ in eps:
        print(f"  {name}")
    print("\n휴대폰과 연결로 통화를 걸어 주세요. 통화가 끝나면 요약을 출력합니다.\n", flush=True)

    start = time.time()
    call_started = call_last_seen = None
    peak = defaultdict(float)  # (장치) -> 최대 음량
    users = defaultdict(set)  # (장치) -> 사용한 프로그램
    live_seconds = defaultdict(float)  # (장치) -> 소리가 흐른 시간

    seen = [name for name, _, _ in eps]
    tick = 0
    while time.time() - start < TIMEOUT:
        now = time.time()
        in_call = False
        tick += 1
        if tick % 4 == 0:  # 블루투스 통화 장치는 통화가 시작될 때 새로 생기므로 목록을 다시 읽는다
            eps = endpoints()
            for name, _, _ in eps:
                if name not in seen:
                    seen.append(name)
                    print(f"[{time.strftime('%H:%M:%S')}] 새 장치: {name}", flush=True)
        for name, meter, manager in eps:
            try:
                level = meter.GetPeakValue()
                procs = active_processes(manager)
            except Exception:
                continue
            if any(k in name.lower() for k in CALL_DEVICE) and (level > 0.001 or procs):
                in_call = True
            if any(p.lower().startswith(PHONE_LINK) for p in procs):
                in_call = True
            if call_started:
                peak[name] = max(peak[name], level)
                users[name] |= procs
                if level > 0.01:
                    live_seconds[name] += POLL
        if in_call:
            if not call_started:
                call_started = now
                print(f"[{time.strftime('%H:%M:%S')}] 통화 감지. 기록을 시작합니다.", flush=True)
            call_last_seen = now
        elif call_started and now - call_last_seen > AFTER_CALL:
            break
        time.sleep(POLL)

    if not call_started:
        print("시간 안에 통화가 감지되지 않았습니다.")
        return
    print(f"\n[통화 중 장치 사용 요약]  기록 {call_last_seen - call_started:.0f}초\n")
    for name in seen:
        if peak[name] > 0.001 or users[name]:
            print(f"{name}\n    최대 음량 {peak[name]:.3f}   소리가 흐른 시간 {live_seconds[name]:.1f}초"
                  f"   사용 프로그램: {', '.join(sorted(users[name])) or '(없음)'}")


if __name__ == "__main__":
    main()
