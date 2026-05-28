"""
obscam
──────
OBS の graphics-hook64.dll を利用したゲームキャプチャライブラリ。
ketsugecam と同じインターフェースで使える。

返り値: torch.Tensor(CUDA, uint8, [H, W, 3], BGR)  ← YOLO 直投入

使い方:
    import obscam

    # 単発
    cam = obscam.create(
        game_title="Cyberpunk 2077",
        fov_width=640, fov_height=640,
        screen_width=1920, screen_height=1080,
    )
    frame = cam.grab()          # torch.Tensor CUDA BGR

    # 連続
    cam.start(target_fps=120)
    while True:
        frame = cam.get_latest_frame()
        if frame is not None:
            results = model(frame)

    # コンテキストマネージャ
    with obscam.create(...) as cam:
        frame = cam.grab()
"""

from .obscam import ObsCam


def create(
    game_title: str,
    fov_width: int,
    fov_height: int,
    screen_width: int,
    screen_height: int,
    obs_dir: str = "obs_stuff",
    cuda: bool = True,
) -> ObsCam:
    """
    ObsCam インスタンスを生成して返す。

    Args:
        game_title:    FindWindowW に渡すウィンドウタイトル（完全一致）
        fov_width:     キャプチャするROI幅 (px)  ※画面中央から切り出し
        fov_height:    キャプチャするROI高さ (px)
        screen_width:  ゲームの解像度 幅
        screen_height: ゲームの解像度 高さ
        obs_dir:       inject-helper64.exe / graphics-hook64.dll /
                       get-graphics-offsets64.exe が置かれたディレクトリ
        cuda:          True → torch.Tensor(CUDA) で返す

    Returns:
        ObsCam
    """
    return ObsCam(
        game_title=game_title,
        fov_width=fov_width,
        fov_height=fov_height,
        screen_width=screen_width,
        screen_height=screen_height,
        obs_dir=obs_dir,
        cuda=cuda,
    )


__all__ = ["create", "ObsCam"]