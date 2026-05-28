"""
cs2_pygame_viewer.py
────────────────────
obscam でキャプチャした CS2 の画面を pygame でリアルタイム表示する。

使い方:
    python cs2_pygame_viewer.py

終了: ウィンドウを閉じる か Esc / Q キー
"""

import sys
import warnings
warnings.filterwarnings("ignore")

# comtypes __del__ エラーを抑制
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

import time
import numpy as np
import pygame
import torch
import obscam

# ─── 設定 ────────────────────────────────────────────────────────────────────

OBS_DIR       = r"C:\Program Files\obs-studio\data\obs-plugins\win-capture"
GAME_TITLE    = "Counter-Strike 2"
SCREEN_WIDTH  = 1920
SCREEN_HEIGHT = 1080
FOV_WIDTH     = 416
FOV_HEIGHT    = 416
TARGET_FPS    = 240

# ─── メイン ──────────────────────────────────────────────────────────────────

def main():
    # ── obscam 初期化 ──────────────────────────────────────────────────────────
    print("[viewer] obscam 初期化中...")
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

    # ── pygame 初期化 ──────────────────────────────────────────────────────────
    pygame.init()
    screen = pygame.display.set_mode((FOV_WIDTH, FOV_HEIGHT))
    pygame.display.set_caption(f"obscam viewer – {GAME_TITLE}")
    font  = pygame.font.SysFont("consolas", 18)
    clock = pygame.time.Clock()

    # FPS 計測用
    frame_count = 0
    t0          = time.perf_counter()
    display_fps = 0.0

    print("[viewer] 表示開始 (Esc / Q で終了)")

    try:
        while True:
            # ── イベント処理 ──────────────────────────────────────────────────
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                if event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        return

            # ── フレーム取得 ──────────────────────────────────────────────────
            frame = cam.get_latest_frame(timeout=0.05)
            if frame is None:
                clock.tick(60)
                continue

            # BGR (CUDA/CPU) → RGB numpy → pygame Surface
            # frame: [H, W, 3] uint8 BGR
            rgb = torch.flip(frame, dims=[-1])  # BGR → RGB
            if rgb.is_cuda:
                rgb = rgb.cpu()             # GPU → CPU
            arr = rgb.numpy()               # torch → numpy (コピーなし)

            # pygame.surfarray は [W, H, 3] を期待するので転置
            surf = pygame.surfarray.make_surface(arr.transpose(1, 0, 2))
            screen.blit(surf, (0, 0))

            # ── FPS オーバーレイ ──────────────────────────────────────────────
            frame_count += 1
            elapsed = time.perf_counter() - t0
            if elapsed >= 0.5:
                display_fps = frame_count / elapsed
                frame_count = 0
                t0          = time.perf_counter()

            fps_text = (
                f"display: {display_fps:5.1f} fps  "
                f"capture: {cam.capture_fps:5.1f} fps  "
                f"mode: {cam.mode}"
            )
            label = font.render(fps_text, True, (0, 255, 0), (0, 0, 0))
            screen.blit(label, (8, 8))

            pygame.display.flip()
            clock.tick(TARGET_FPS)

    finally:
        print("[viewer] 終了処理中...")
        cam.release()
        pygame.quit()
        print("[viewer] 終了")


if __name__ == "__main__":
    main()
