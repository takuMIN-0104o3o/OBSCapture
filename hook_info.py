"""
obscam/hook_info.py
OBS graphics-hook64.dll と共有する共有メモリのレイアウト定義。
C++ の hook_info.h を ctypes.Structure で再現。
"""
import ctypes
import ctypes.wintypes as wintypes
import enum


class CaptureType(enum.IntEnum):
    MEMORY  = 0
    TEXTURE = 1


class D3d8Offsets(ctypes.Structure):
    _fields_ = [("present", ctypes.c_uint32)]


class D3d9Offsets(ctypes.Structure):
    _fields_ = [
        ("present",           ctypes.c_uint32),
        ("present_ex",        ctypes.c_uint32),
        ("present_swap",      ctypes.c_uint32),
        ("d3d9_clsoff",       ctypes.c_uint32),
        ("is_d3d9ex_clsoff",  ctypes.c_uint32),
    ]


class D3d12Offsets(ctypes.Structure):
    _fields_ = [("execute_command_lists", ctypes.c_uint32)]


class DxgiOffsets(ctypes.Structure):
    _fields_ = [
        ("present",  ctypes.c_uint32),
        ("resize",   ctypes.c_uint32),
        ("present1", ctypes.c_uint32),
    ]


class DxgiOffsets2(ctypes.Structure):
    _fields_ = [("release", ctypes.c_uint32)]


class DdrawOffsets(ctypes.Structure):
    _fields_ = [
        ("surface_create",      ctypes.c_uint32),
        ("surface_restore",     ctypes.c_uint32),
        ("surface_release",     ctypes.c_uint32),
        ("surface_unlock",      ctypes.c_uint32),
        ("surface_blt",         ctypes.c_uint32),
        ("surface_flip",        ctypes.c_uint32),
        ("surface_set_palette", ctypes.c_uint32),
        ("palette_set_entries", ctypes.c_uint32),
    ]


class GraphicsOffsets(ctypes.Structure):
    _fields_ = [
        ("d3d8",  D3d8Offsets),
        ("d3d9",  D3d9Offsets),
        ("dxgi",  DxgiOffsets),
        ("ddraw", DdrawOffsets),
        ("dxgi2", DxgiOffsets2),
        ("d3d12", D3d12Offsets),
    ]


class HookInfo(ctypes.Structure):
    _fields_ = [
        # フックバージョン
        ("hook_ver_major",      ctypes.c_uint32),
        ("hook_ver_minor",      ctypes.c_uint32),
        # キャプチャ情報
        ("type",                ctypes.c_uint32),   # capture_type enum
        ("window",              ctypes.c_uint32),
        ("format",              ctypes.c_uint32),
        ("cx",                  ctypes.c_uint32),
        ("cy",                  ctypes.c_uint32),
        ("UNUSED_base_cx",      ctypes.c_uint32),
        ("UNUSED_base_cy",      ctypes.c_uint32),
        ("pitch",               ctypes.c_uint32),
        ("map_id",              ctypes.c_uint32),
        ("map_size",            ctypes.c_uint32),
        ("flip",                ctypes.c_bool),
        # 追加オプション
        ("frame_interval",      ctypes.c_uint64),
        ("UNUSED_use_scale",    ctypes.c_bool),
        ("force_shmem",         ctypes.c_bool),
        ("capture_overlay",     ctypes.c_bool),
        ("allow_srgb_alias",    ctypes.c_bool),
        # フックアドレス
        ("offsets",             GraphicsOffsets),
        # 予約領域
        ("reserved",            ctypes.c_uint32 * 126),
    ]


class ShtexData(ctypes.Structure):
    """共有テクスチャハンドルを持つ構造体"""
    _fields_ = [("tex_handle", ctypes.c_uint32)]