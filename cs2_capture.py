import sys
import os
import time
import warnings
warnings.filterwarnings("ignore")

# comtypes の __del__ エラーを抑制
_stderr_orig = sys.stderr

class _FilteredStderr:
    def __init__(self):
        self._orig = _stderr_orig
        self._buf  = ""
        self._skip = False
    def write(self, s):
        self._buf += s
        SKIP_KEYS = [
            "comtypes", "__del__", "access violation",
            "OSError: exception", "_compointer",
            "self.Release()", "__com_Release",
            "Exception ignored in",
            "Traceback (most recent call last)",
        ]
        if any(k in self._buf for k in SKIP_KEYS):
            self._skip = True
        if "\n" in s:
            if not self._skip:
                self._orig.write(self._buf)
            self._buf  = ""
            self._skip = False
        return len(s)
    def flush(self):
        self._orig.flush()
    def fileno(self):
        return self._orig.fileno()

sys.stderr = _FilteredStderr()

import obscam

# ─── 設定 ────────────────────────────────────────────────────────────────────

OBS_DIR       = r"C:\Program Files\obs-studio\data\obs-plugins\win-capture"
GAME_TITLE    = "Counter-Strike 2"
SCREEN_WIDTH  = 1920
SCREEN_HEIGHT = 1080
FOV_WIDTH     = 640
FOV_HEIGHT    = 640
TARGET_FPS    = 120

# ─── 単発キャプチャ ────────────────────────────────────────────────────────────

def single_grab_example():
    print("=== 単発キャプチャ モード ===")
    cam = obscam.create(
        game_title    = GAME_TITLE,
        fov_width     = FOV_WIDTH,
        fov_height    = FOV_HEIGHT,
        screen_width  = SCREEN_WIDTH,
        screen_height = SCREEN_HEIGHT,
        obs_dir       = OBS_DIR,
        cuda          = True,
    )
    print(cam)
    frame = cam.grab()
    if frame is None:
        print("フレーム取得失敗")
        cam.release()
        return
    print(f"取得成功: shape={frame.shape} dtype={frame.dtype} device={frame.device}")
    cam.release()


# ─── 連続キャプチャ ────────────────────────────────────────────────────────────

def continuous_capture_example():
    print("=== 連続キャプチャ モード ===")
    cam = obscam.create(
        game_title    = GAME_TITLE,
        fov_width     = FOV_WIDTH,
        fov_height    = FOV_HEIGHT,
        screen_width  = SCREEN_WIDTH,
        screen_height = SCREEN_HEIGHT,
        obs_dir       = OBS_DIR,
        cuda          = True,
    )
    print(cam)
    cam.start(target_fps=TARGET_FPS)
    try:
        count = 0
        t0 = time.perf_counter()
        while True:
            frame = cam.get_latest_frame(timeout=1.0)
            if frame is None:
                continue
            count += 1
            # ここに推論コードを書く: results = model(frame)
            elapsed = time.perf_counter() - t0
            if elapsed >= 2.0:
                print(f"FPS: {count/elapsed:.1f}  capture: {cam.capture_fps:.1f}"
                      f"  shape={frame.shape}  device={frame.device}")
                count = 0
                t0 = time.perf_counter()
    except KeyboardInterrupt:
        print("\n停止します")
    finally:
        cam.release()


# ─── YOLO連携 ─────────────────────────────────────────────────────────────────

def yolo_example():
    try:
        from ultralytics import YOLO
    except ImportError:
        print("pip install ultralytics が必要です")
        return

    model = YOLO("yolov8n.pt")
    model.to("cuda")

    cam = obscam.create(
        game_title    = GAME_TITLE,
        fov_width     = FOV_WIDTH,
        fov_height    = FOV_HEIGHT,
        screen_width  = SCREEN_WIDTH,
        screen_height = SCREEN_HEIGHT,
        obs_dir       = OBS_DIR,
        cuda          = True,
    )
    cam.start(target_fps=TARGET_FPS)
    print("推論開始 (Ctrl+C で停止)")
    try:
        while True:
            frame = cam.get_latest_frame(timeout=1.0)
            if frame is None:
                continue
            results = model(frame, verbose=False)
            for r in results:
                if r.boxes is not None and len(r.boxes):
                    for box in r.boxes:
                        cls  = int(box.cls[0])
                        conf = float(box.conf[0])
                        xyxy = box.xyxy[0].tolist()
                        print(f"  cls={cls} conf={conf:.2f} box={[round(v) for v in xyxy]}")
    except KeyboardInterrupt:
        print("\n停止します")
    finally:
        cam.release()


# ─── エントリポイント ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "single"
    if mode == "single":
        single_grab_example()
    elif mode == "loop":
        continuous_capture_example()
    elif mode == "yolo":
        yolo_example()
    else:
        print("使い方: python cs2_capture.py [single|loop|yolo]")