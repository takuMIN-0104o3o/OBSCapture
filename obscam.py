"""
obscam/obscam.py

OBS の graphics-hook64.dll を利用したゲームキャプチャ。
C++ の GameCapture クラスを Python + ctypes で完全再実装。

フロー:
    1. FindWindowW でゲームウィンドウを特定
    2. inject-helper64.exe で graphics-hook64.dll を対象プロセスに注入
    3. 共有メモリ (CaptureHook_HookInfo) から hook_info を読み取る
    4. 共有テクスチャハンドルを D3D11 OpenSharedResource で開く
    5. GPU 上でROI をコピーして torch.Tensor(CUDA, uint8, BGR) を返す

返り値: torch.Tensor(CUDA, uint8, [H, W, 3], BGR)  ← YOLO 直投入可

修正点 (2025-06):
    - _ID3D11Device1 をモジュールレベルに移動（関数内再定義による comtypes 型破壊を修正）
    - NT ハンドル (bit31=1) は DuplicateHandle でカレントプロセスへ複製してから使用
      （ゲームプロセスのハンドルを直接 OpenSharedResource1 に渡すとアクセス違反）
    - D3D11CreateDevice の restype を明示設定
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import subprocess
import time
from threading import Event, Thread
from typing import Optional, Tuple

import torch

from .hook_info import HookInfo, ShtexData, GraphicsOffsets
from .libs import (
    D3D11_BOX, D3D11_MAPPED_SUBRESOURCE, D3D11_TEXTURE2D_DESC,
    DXGI_SAMPLE_DESC,
    D3D11_CPU_ACCESS_READ, D3D11_USAGE_STAGING,
    DXGI_FORMAT_B8G8R8A8_UNORM,
    D3D_DRIVER_TYPE_HARDWARE, D3D11_SDK_VERSION,
    D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0,
    INFINITE, WAIT_OBJECT_0,
    FILE_MAP_ALL_ACCESS,
    EVENT_MODIFY_STATE, SYNCHRONIZE,
    S_OK,
    find_window, get_window_thread_process_id,
    open_file_mapping, map_view_of_file, unmap_view_of_file,
    close_handle, open_event, set_event, open_mutex, create_mutex,
    wait_for_single_object, create_process, get_exit_code_process,
)


# ─── D3D11 COM (comtypes 経由) ────────────────────────────────────────────────
import comtypes
import comtypes.client

_d3d11 = ctypes.windll.d3d11

# [修正③] D3D11CreateDevice の restype を明示設定（未設定だと再試行時にクラッシュすることがある）
_d3d11.D3D11CreateDevice.restype = ctypes.c_long

# IID
_IID_ID3D11Resource  = comtypes.GUID("{dc8e63f3-d12b-4952-b47b-5e45026a862d}")
_IID_ID3D11Texture2D = comtypes.GUID("{6f15aaf2-d208-4e89-9ab4-489535d34f9c}")

class _ID3D11DeviceChild(comtypes.IUnknown):
    _iid_    = comtypes.GUID("{1841e5c8-16b0-489b-bcc8-44cfb0d5deae}")
    _methods_ = [
        comtypes.STDMETHOD(None,             "GetDevice"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT, "SetPrivateDataInterface"),
    ]

class _ID3D11Resource(_ID3D11DeviceChild):
    _iid_    = _IID_ID3D11Resource
    _methods_ = [
        comtypes.STDMETHOD(None,          "GetType"),
        comtypes.STDMETHOD(None,          "SetEvictionPriority"),
        comtypes.STDMETHOD(ctypes.c_uint, "GetEvictionPriority"),
    ]

class _ID3D11Texture2D(_ID3D11Resource):
    _iid_    = _IID_ID3D11Texture2D
    _methods_ = [
        comtypes.STDMETHOD(None, "GetDesc",
            [ctypes.POINTER(D3D11_TEXTURE2D_DESC)]),
    ]

class _ID3D11DeviceContext(_ID3D11DeviceChild):
    _iid_    = comtypes.GUID("{c0bfa96c-e089-44fb-8eaf-26f8796190da}")
    _methods_ = [
        comtypes.STDMETHOD(None, "VSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "PSSetShaderResources"),
        comtypes.STDMETHOD(None, "PSSetShader"),
        comtypes.STDMETHOD(None, "PSSetSamplers"),
        comtypes.STDMETHOD(None, "VSSetShader"),
        comtypes.STDMETHOD(None, "DrawIndexed"),
        comtypes.STDMETHOD(None, "Draw"),
        comtypes.STDMETHOD(comtypes.HRESULT, "Map",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
             ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)]),
        comtypes.STDMETHOD(None, "Unmap",
            [ctypes.c_void_p, ctypes.c_uint]),
        comtypes.STDMETHOD(None, "PSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "IASetInputLayout"),
        comtypes.STDMETHOD(None, "IASetVertexBuffers"),
        comtypes.STDMETHOD(None, "IASetIndexBuffer"),
        comtypes.STDMETHOD(None, "DrawIndexedInstanced"),
        comtypes.STDMETHOD(None, "DrawInstanced"),
        comtypes.STDMETHOD(None, "GSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "GSSetShader"),
        comtypes.STDMETHOD(None, "IASetPrimitiveTopology"),
        comtypes.STDMETHOD(None, "VSSetShaderResources"),
        comtypes.STDMETHOD(None, "VSSetSamplers"),
        comtypes.STDMETHOD(None, "Begin"),
        comtypes.STDMETHOD(None, "End"),
        comtypes.STDMETHOD(comtypes.HRESULT, "GetData"),
        comtypes.STDMETHOD(None, "SetPredication"),
        comtypes.STDMETHOD(None, "GSSetShaderResources"),
        comtypes.STDMETHOD(None, "GSSetSamplers"),
        comtypes.STDMETHOD(None, "OMSetRenderTargets"),
        comtypes.STDMETHOD(None, "OMSetRenderTargetsAndUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "OMSetBlendState"),
        comtypes.STDMETHOD(None, "OMSetDepthStencilState"),
        comtypes.STDMETHOD(None, "SOSetTargets"),
        comtypes.STDMETHOD(None, "DrawAuto"),
        comtypes.STDMETHOD(None, "DrawIndexedInstancedIndirect"),
        comtypes.STDMETHOD(None, "DrawInstancedIndirect"),
        comtypes.STDMETHOD(None, "Dispatch"),
        comtypes.STDMETHOD(None, "DispatchIndirect"),
        comtypes.STDMETHOD(None, "RSSetState"),
        comtypes.STDMETHOD(None, "RSSetViewports"),
        comtypes.STDMETHOD(None, "RSSetScissorRects"),
        comtypes.STDMETHOD(None, "CopySubresourceRegion",
            [ctypes.c_void_p, ctypes.c_uint,
             ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
             ctypes.c_void_p, ctypes.c_uint,
             ctypes.POINTER(D3D11_BOX)]),
        comtypes.STDMETHOD(None, "CopyResource",
            [ctypes.c_void_p, ctypes.c_void_p]),
        comtypes.STDMETHOD(None, "UpdateSubresource"),
        comtypes.STDMETHOD(None, "CopyStructureCount"),
        comtypes.STDMETHOD(None, "ClearRenderTargetView"),
        comtypes.STDMETHOD(None, "ClearUnorderedAccessViewUint"),
        comtypes.STDMETHOD(None, "ClearUnorderedAccessViewFloat"),
        comtypes.STDMETHOD(None, "ClearDepthStencilView"),
        comtypes.STDMETHOD(None, "GenerateMips"),
        comtypes.STDMETHOD(None, "SetResourceMinLOD"),
        comtypes.STDMETHOD(None, "GetResourceMinLOD"),
        comtypes.STDMETHOD(None, "ResolveSubresource"),
        comtypes.STDMETHOD(None, "ExecuteCommandList"),
        comtypes.STDMETHOD(None, "HSSetShaderResources"),
        comtypes.STDMETHOD(None, "HSSetShader"),
        comtypes.STDMETHOD(None, "HSSetSamplers"),
        comtypes.STDMETHOD(None, "HSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "DSSetShaderResources"),
        comtypes.STDMETHOD(None, "DSSetShader"),
        comtypes.STDMETHOD(None, "DSSetSamplers"),
        comtypes.STDMETHOD(None, "DSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "CSSetShaderResources"),
        comtypes.STDMETHOD(None, "CSSetUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "CSSetShader"),
        comtypes.STDMETHOD(None, "CSSetSamplers"),
        comtypes.STDMETHOD(None, "CSSetConstantBuffers"),
        comtypes.STDMETHOD(None, "VSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "PSGetShaderResources"),
        comtypes.STDMETHOD(None, "PSGetShader"),
        comtypes.STDMETHOD(None, "PSGetSamplers"),
        comtypes.STDMETHOD(None, "VSGetShader"),
        comtypes.STDMETHOD(None, "PSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "IAGetInputLayout"),
        comtypes.STDMETHOD(None, "IAGetVertexBuffers"),
        comtypes.STDMETHOD(None, "IAGetIndexBuffer"),
        comtypes.STDMETHOD(None, "GSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "GSGetShader"),
        comtypes.STDMETHOD(None, "IAGetPrimitiveTopology"),
        comtypes.STDMETHOD(None, "VSGetShaderResources"),
        comtypes.STDMETHOD(None, "VSGetSamplers"),
        comtypes.STDMETHOD(None, "GetPredication"),
        comtypes.STDMETHOD(None, "GSGetShaderResources"),
        comtypes.STDMETHOD(None, "GSGetSamplers"),
        comtypes.STDMETHOD(None, "OMGetRenderTargets"),
        comtypes.STDMETHOD(None, "OMGetRenderTargetsAndUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "OMGetBlendState"),
        comtypes.STDMETHOD(None, "OMGetDepthStencilState"),
        comtypes.STDMETHOD(None, "SOGetTargets"),
        comtypes.STDMETHOD(None, "RSGetState"),
        comtypes.STDMETHOD(None, "RSGetViewports"),
        comtypes.STDMETHOD(None, "RSGetScissorRects"),
        comtypes.STDMETHOD(None, "HSGetShaderResources"),
        comtypes.STDMETHOD(None, "HSGetShader"),
        comtypes.STDMETHOD(None, "HSGetSamplers"),
        comtypes.STDMETHOD(None, "HSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "DSGetShaderResources"),
        comtypes.STDMETHOD(None, "DSGetShader"),
        comtypes.STDMETHOD(None, "DSGetSamplers"),
        comtypes.STDMETHOD(None, "DSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "CSGetShaderResources"),
        comtypes.STDMETHOD(None, "CSGetUnorderedAccessViews"),
        comtypes.STDMETHOD(None, "CSGetShader"),
        comtypes.STDMETHOD(None, "CSGetSamplers"),
        comtypes.STDMETHOD(None, "CSGetConstantBuffers"),
        comtypes.STDMETHOD(None, "ClearState"),
        comtypes.STDMETHOD(None, "Flush"),
    ]

class _ID3D11Device(comtypes.IUnknown):
    _iid_    = comtypes.GUID("{db6f6ddb-ac77-4e88-8253-819df9bbf140}")
    _methods_ = [
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateBuffer"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture1D"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture2D",
            [ctypes.POINTER(D3D11_TEXTURE2D_DESC),
             ctypes.c_void_p,
             ctypes.POINTER(ctypes.POINTER(_ID3D11Texture2D))]),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture3D"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateShaderResourceView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateUnorderedAccessView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateRenderTargetView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDepthStencilView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateInputLayout"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateVertexShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateGeometryShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateGeometryShaderWithStreamOutput"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreatePixelShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateHullShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDomainShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateComputeShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateClassLinkage"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateBlendState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDepthStencilState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateRasterizerState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateSamplerState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateQuery"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreatePredicate"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateCounter"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDeferredContext"),
        comtypes.STDMETHOD(comtypes.HRESULT, "OpenSharedResource",
            [ctypes.c_void_p,                    # hResource (HANDLE)
             ctypes.POINTER(comtypes.GUID),       # ReturnedInterface (REFIID)
             ctypes.POINTER(ctypes.c_void_p)]),   # ppResource (void**)
    ]


# [修正①] _ID3D11Device1 をモジュールレベルに移動
# 元のコードは _open_shared_resource 関数の内部で毎回クラスを再定義していた。
# comtypes は IID をキーにインターフェースをキャッシュするため、
# 同一 IID のクラスを再定義するとキャッシュ衝突が起き vtable が壊れる。
class _ID3D11Device1(comtypes.IUnknown):
    _iid_ = comtypes.GUID("{a04bfb29-08ef-43d6-a49c-a9bdbdcbe686}")
    _methods_ = [
        # --- ID3D11Device の 40 メソッドをすべて列挙（vtable 順に合わせること）---
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateBuffer"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture1D"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture2D"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateTexture3D"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateShaderResourceView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateUnorderedAccessView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateRenderTargetView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDepthStencilView"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateInputLayout"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateVertexShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateGeometryShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateGeometryShaderWithStreamOutput"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreatePixelShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateHullShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDomainShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateComputeShader"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateClassLinkage"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateBlendState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDepthStencilState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateRasterizerState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateSamplerState"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateQuery"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreatePredicate"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateCounter"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CreateDeferredContext"),
        comtypes.STDMETHOD(comtypes.HRESULT, "OpenSharedResource"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckFormatSupport"),
        comtypes.STDMETHOD(comtypes.HRESULT, "CheckMultisampleQualityLevels"),
        comtypes.STDMETHOD(None,              "CheckCounterInfo"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "CheckCounter"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "CheckFeatureSupport"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "GetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "SetPrivateData"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "SetPrivateDataInterface"),
        comtypes.STDMETHOD(None,              "GetFeatureLevel"),
        comtypes.STDMETHOD(None,              "GetCreationFlags"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "GetDeviceRemovedReason"),
        comtypes.STDMETHOD(None,              "GetImmediateContext"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "SetExceptionMode"),
        comtypes.STDMETHOD(None,              "GetExceptionMode"),
        # --- ID3D11Device1 追加分 (4 メソッド) ---
        comtypes.STDMETHOD(None,              "GetImmediateContext1"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "CreateDeferredContext1"),
        comtypes.STDMETHOD(comtypes.HRESULT,  "OpenSharedResource1",
            [ctypes.c_void_p,                   # hResource (HANDLE, 自プロセスの複製済みハンドル)
             ctypes.POINTER(comtypes.GUID),      # returnedInterface (REFIID)
             ctypes.POINTER(ctypes.c_void_p)]),  # ppResource (void**)
        comtypes.STDMETHOD(comtypes.HRESULT,  "OpenSharedResourceByName"),
    ]


def _create_d3d11_device():
    """D3D11Device + ImmediateContext を comtypes オブジェクトとして返す"""
    feature_levels = (ctypes.c_uint * 3)(
        D3D_FEATURE_LEVEL_11_0,
        D3D_FEATURE_LEVEL_10_1,
        D3D_FEATURE_LEVEL_10_0,
    )
    p_device  = ctypes.POINTER(_ID3D11Device)()
    p_context = ctypes.POINTER(_ID3D11DeviceContext)()
    hr = _d3d11.D3D11CreateDevice(
        None, D3D_DRIVER_TYPE_HARDWARE, None, 0,
        feature_levels, 3, D3D11_SDK_VERSION,
        ctypes.byref(p_device), None, ctypes.byref(p_context),
    )
    if hr != S_OK:
        raise RuntimeError(f"D3D11CreateDevice failed: hr=0x{hr & 0xFFFFFFFF:08X}")
    return p_device, p_context


def _com_release(obj):
    if obj is not None:
        try:
            # comtypes ポインタの場合は中身が NULL でないか確認してから Release
            if hasattr(obj, 'contents'):
                try:
                    _ = obj.contents
                except (ValueError, OSError):
                    return  # NULL ポインタ → スキップ
            obj.Release()
        except Exception:
            pass


# ─── Win32 DuplicateHandle ヘルパー ──────────────────────────────────────────
# use_last_error=True を付けて ctypes.get_last_error() が正しく機能するようにする
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.OpenProcess.restype       = ctypes.c_void_p
_k32.OpenProcess.argtypes      = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.DuplicateHandle.restype   = ctypes.c_int   # BOOL は 4バイト int
_k32.DuplicateHandle.argtypes  = [
    ctypes.c_void_p,  # hSourceProcessHandle
    ctypes.c_void_p,  # hSourceHandle
    ctypes.c_void_p,  # hTargetProcessHandle
    ctypes.POINTER(ctypes.c_void_p),  # lpTargetHandle
    ctypes.c_uint32,  # dwDesiredAccess
    ctypes.c_int,     # bInheritHandle (BOOL = int)
    ctypes.c_uint32,  # dwOptions
]
_k32.CloseHandle.restype  = ctypes.c_int
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

_PROCESS_DUP_HANDLE    = 0x0040
_DUPLICATE_SAME_ACCESS = 0x00000002


def _try_duplicate_handle(src_pid: int, src_handle: int) -> Optional[int]:
    """src_pid プロセスの src_handle をカレントプロセスに複製して返す。
    失敗した場合は None を返す（例外は送出しない）。"""
    src_proc = _k32.OpenProcess(_PROCESS_DUP_HANDLE, False, src_pid)
    if not src_proc:
        err = ctypes.get_last_error()
        print(f"[obscam] OpenProcess(PID={src_pid}) 失敗: LastError={err}")
        return None

    dup = ctypes.c_void_p(0)
    ok  = _k32.DuplicateHandle(
        ctypes.c_void_p(src_proc),
        ctypes.c_void_p(src_handle),
        _k32.GetCurrentProcess(),
        ctypes.byref(dup),
        0,
        0,   # bInheritHandle = FALSE
        _DUPLICATE_SAME_ACCESS,
    )
    _k32.CloseHandle(ctypes.c_void_p(src_proc))

    if not ok or not dup.value:
        err = ctypes.get_last_error()
        print(f"[obscam] DuplicateHandle(0x{src_handle:08X}) 失敗: LastError={err}")
        return None

    return dup.value


def _open_shared_resource(device, handle: int, src_pid: int = 0):
    """共有テクスチャを 3 段階フォールバックで開く。

    [試行 1] OpenSharedResource (legacy global handle)
        → bit31=0 のグローバルハンドルはこれだけで成功する。
          bit31=1 のハンドルも実体がカーネルグローバルであれば成功することがある。

    [試行 2] OpenSharedResource1 with direct handle
        → DuplicateHandle 不要のカーネルグローバル NT ハンドル向け。
          OBS が内部で使っているのもこの方式。

    [試行 3] OpenSharedResource1 with DuplicateHandle
        → プロセスローカル NT ハンドル向け（CS2 以外のゲームで稀に発生）。
    """
    raw = ctypes.c_void_p(handle)

    # ── 試行 1: OpenSharedResource (legacy) ──────────────────────────────────
    print(f"[obscam] 試行1 OpenSharedResource (handle=0x{handle:08X})")
    out = ctypes.c_void_p(0)
    iid = _IID_ID3D11Resource
    try:
        hr = device.OpenSharedResource(raw, ctypes.byref(iid), ctypes.byref(out))
        if hr == S_OK and out.value:
            print("[obscam] OpenSharedResource 成功")
            return ctypes.cast(out, ctypes.POINTER(_ID3D11Resource))
        print(f"[obscam] 試行1 失敗: hr=0x{hr & 0xFFFFFFFF:08X}")
    except Exception as e:
        print(f"[obscam] 試行1 例外: {e}")

    # ── 試行 2: OpenSharedResource1 ハンドル直接渡し ─────────────────────────
    # bit31=1 のカーネルグローバルハンドル（0xC0xxxxxx など）はDuplicateHandle
    # できないが、そのまま OpenSharedResource1 に渡せば動く（OBS と同じ動作）。
    print(f"[obscam] 試行2 OpenSharedResource1 直接 (handle=0x{handle:08X})")
    try:
        dev1 = device.QueryInterface(_ID3D11Device1)
        out2 = ctypes.c_void_p(0)
        iid2 = _IID_ID3D11Resource
        hr = dev1.OpenSharedResource1(raw, ctypes.byref(iid2), ctypes.byref(out2))
        if hr == S_OK and out2.value:
            print("[obscam] OpenSharedResource1 (直接) 成功")
            return ctypes.cast(out2, ctypes.POINTER(_ID3D11Resource))
        print(f"[obscam] 試行2 失敗: hr=0x{hr & 0xFFFFFFFF:08X}")
    except Exception as e:
        print(f"[obscam] 試行2 例外: {e}")

    # ── 試行 3: OpenSharedResource1 + DuplicateHandle ────────────────────────
    # プロセスローカル NT ハンドルの場合、先に複製が必要。
    if src_pid:
        print(f"[obscam] 試行3 OpenSharedResource1 + DuplicateHandle (pid={src_pid})")
        local_handle = _try_duplicate_handle(src_pid, handle)
        if local_handle:
            try:
                dev1 = device.QueryInterface(_ID3D11Device1)
                out3 = ctypes.c_void_p(0)
                iid3 = _IID_ID3D11Resource
                hr = dev1.OpenSharedResource1(
                    ctypes.c_void_p(local_handle),
                    ctypes.byref(iid3),
                    ctypes.byref(out3),
                )
                if hr == S_OK and out3.value:
                    print("[obscam] OpenSharedResource1 (DuplicateHandle) 成功")
                    return ctypes.cast(out3, ctypes.POINTER(_ID3D11Resource))
                print(f"[obscam] 試行3 失敗: hr=0x{hr & 0xFFFFFFFF:08X}")
            except Exception as e:
                print(f"[obscam] 試行3 例外: {e}")
            finally:
                _k32.CloseHandle(ctypes.c_void_p(local_handle))
        else:
            print("[obscam] 試行3 スキップ (DuplicateHandle 失敗)")

    raise RuntimeError(
        f"全ての OpenSharedResource 手法が失敗: handle=0x{handle:08X}"
    )


def _create_staging_texture(device, width: int, height: int, fmt: int = DXGI_FORMAT_B8G8R8A8_UNORM):
    """CPU読み取り用 Staging テクスチャを作成"""
    desc = D3D11_TEXTURE2D_DESC()
    desc.Width          = width
    desc.Height         = height
    desc.MipLevels      = 1
    desc.ArraySize      = 1
    desc.Format         = fmt
    desc.SampleDesc     = DXGI_SAMPLE_DESC(1, 0)
    desc.Usage          = D3D11_USAGE_STAGING
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ
    desc.BindFlags      = 0
    desc.MiscFlags      = 0
    tex = ctypes.POINTER(_ID3D11Texture2D)()
    hr = device.CreateTexture2D(ctypes.byref(desc), None, ctypes.byref(tex))
    if hr != S_OK:
        raise RuntimeError(f"CreateTexture2D failed: hr=0x{hr & 0xFFFFFFFF:08X}")
    return tex


def _copy_subresource_region(ctx, dst, src, box: D3D11_BOX):
    ctx.CopySubresourceRegion(dst, 0, 0, 0, 0, src, 0, ctypes.byref(box))


def _map_texture(ctx, tex) -> D3D11_MAPPED_SUBRESOURCE:
    mapped = D3D11_MAPPED_SUBRESOURCE()
    hr = ctx.Map(tex, 0, 1, 0, ctypes.byref(mapped))  # D3D11_MAP_READ=1
    if hr != S_OK:
        raise RuntimeError(f"Map failed: hr=0x{hr & 0xFFFFFFFF:08X}")
    return mapped


def _unmap_texture(ctx, tex):
    ctx.Unmap(tex, 0)


# ─── グラフィックスオフセット取得 ─────────────────────────────────────────────

def _run_get_graphics_offsets(exe_path: str) -> str:
    result = subprocess.run(
        [exe_path], capture_output=True, text=True, timeout=10
    )
    return result.stdout


def _parse_offsets(output: str, offsets: GraphicsOffsets):
    """get-graphics-offsets64.exe の INI 出力をパースして offsets に書き込む"""
    section = ""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            section = line[1:line.index("]")]
            continue
        if "=" not in line:
            continue
        key, _, val_str = line.partition("=")
        try:
            val = int(val_str, 16)
        except ValueError:
            continue

        if section == "d3d8":
            if key == "present":             offsets.d3d8.present = val
        elif section == "d3d9":
            if key == "present":             offsets.d3d9.present = val
            elif key == "present_ex":        offsets.d3d9.present_ex = val
            elif key == "present_swap":      offsets.d3d9.present_swap = val
            elif key == "d3d9_clsoff":       offsets.d3d9.d3d9_clsoff = val
            elif key == "is_d3d9ex_clsoff":  offsets.d3d9.is_d3d9ex_clsoff = val
        elif section == "dxgi":
            if key == "present":             offsets.dxgi.present = val
            elif key == "resize":            offsets.dxgi.resize = val
            elif key == "present1":          offsets.dxgi.present1 = val
            elif key == "release":           offsets.dxgi2.release = val


# ─── 共有メモリ名ヘルパー ─────────────────────────────────────────────────────

def _map_name(base: str, pid: int) -> str:
    return f"{base}{pid}"

def _data_map_name(window: int, map_id: int) -> str:
    return f"CaptureHook_Texture_{window}_{map_id}"


# ─── ObsCam ───────────────────────────────────────────────────────────────────

class ObsCam:
    """
    OBS graphics-hook を利用したゲームキャプチャ。

    Args:
        game_title:   FindWindowW に渡すウィンドウタイトル
        fov_width:    キャプチャするROI幅 (px)
        fov_height:   キャプチャするROI高さ (px)
        screen_width: ゲーム解像度幅
        screen_height:ゲーム解像度高さ
        obs_dir:      inject-helper64.exe 等があるディレクトリ
        cuda:         True → torch.Tensor(CUDA) で返す
    """

    INJECT_EXE          = "inject-helper64.exe"
    HOOK_DLL            = "graphics-hook64.dll"
    OFFSETS_EXE         = "get-graphics-offsets64.exe"

    def __init__(
        self,
        game_title: str,
        fov_width: int,
        fov_height: int,
        screen_width: int,
        screen_height: int,
        obs_dir: str = "obs_stuff",
    ):
        self.game_title    = game_title
        self._roi_w        = fov_width
        self._roi_h        = fov_height
        self._screen_w     = screen_width
        self._screen_h     = screen_height
        self._obs_dir      = obs_dir

        # ハンドル類
        self._hwnd         = 0
        self._pid          = 0
        self._tid          = 0

        self._keepalive    = None
        self._hook_restart = None
        self._hook_stop    = None
        self._hook_ready   = None
        self._hook_exit    = None
        self._hook_init    = None
        self._tex_mutex    = [None, None]
        self._info_map     = None
        self._data_map     = None

        self._hook_info_ptr  = None   # ctypes ポインタ (HookInfo)
        self._shtex_ptr      = None   # ctypes ポインタ (ShtexData)

        # D3D11
        self._device         = ctypes.c_void_p(0)
        self._ctx            = ctypes.c_void_p(0)
        self._shared_res     = ctypes.c_void_p(0)
        self._staging_tex    = ctypes.c_void_p(0)
        self._roi_box        = D3D11_BOX()

        # ピン留めバッファ（_to_tensor_cpu で再利用し確保コストを排除）
        self._pinned_buf: Optional[torch.Tensor] = None

        # 連続キャプチャ
        self._latest: Optional[torch.Tensor] = None
        self._frame_event = Event()
        self._stop_event  = Event()
        self._thread: Optional[Thread] = None
        self.is_capturing = False
        self._capture_fps = 0.0

        self._initialize()

    # ── 初期化 ────────────────────────────────────────────────────────────────

    def _initialize(self):
        # 1. ウィンドウを探す
        self._hwnd = find_window(self.game_title)
        if not self._hwnd:
            raise RuntimeError(f"ウィンドウが見つかりません: '{self.game_title}'")

        self._tid, self._pid = get_window_thread_process_id(self._hwnd)
        print(f"[obscam] PID={self._pid}, TID={self._tid}")

        # 2. KeepAlive ミューテックス
        self._keepalive = create_mutex(_map_name("CaptureHook_KeepAlive", self._pid))
        if not self._keepalive:
            raise RuntimeError("CreateKeepaliveMutex failed")

        # 3. 既にフック済みか確認
        self._hook_restart = open_event(
            EVENT_MODIFY_STATE | SYNCHRONIZE, False,
            _map_name("CaptureHook_Restart", self._pid),
        )
        already_hooked = bool(self._hook_restart)

        if already_hooked:
            print("[obscam] フック検出（OBS等が既に注入済み）→ Restart を送って再初期化")
            # Stop を送って既存キャプチャを停止させてから Restart
            _hook_stop_tmp = open_event(
                EVENT_MODIFY_STATE | SYNCHRONIZE, False,
                _map_name("CaptureHook_Stop", self._pid),
            )
            if _hook_stop_tmp:
                set_event(_hook_stop_tmp)
                close_handle(_hook_stop_tmp)
                time.sleep(0.3)
            set_event(self._hook_restart)
            time.sleep(0.5)   # フックが再起動するのを少し待つ
        else:
            print("[obscam] フックなし → DLL 注入開始")
            self._inject()

        # 4. HookInfo 共有メモリを待つ
        self._open_hook_info_map()

        # 5. グラフィックスオフセットを取得・書き込み
        offsets_exe = os.path.join(self._obs_dir, self.OFFSETS_EXE)
        output = _run_get_graphics_offsets(offsets_exe)
        _parse_offsets(output, self._hook_info.offsets)
        print("[obscam] オフセット書き込み完了")

        # 6. オプションを設定
        self._hook_info.capture_overlay   = False
        self._hook_info.UNUSED_use_scale  = False
        self._hook_info.allow_srgb_alias  = True
        self._hook_info.force_shmem       = False
        self._hook_info.frame_interval    = 0

        # 7. フックイベントを開く
        #    既にフック済みなら即座に開ける。注入直後なら待機が必要。
        wait_s = 1.0 if already_hooked else 15.0

        def _open_ev_wait(ev_name):
            deadline = time.time() + wait_s
            while time.time() < deadline:
                h = open_event(EVENT_MODIFY_STATE | SYNCHRONIZE, False,
                               _map_name(ev_name, self._pid))
                if h:
                    return h
                print(f"[obscam] {ev_name} 待機中...")
                time.sleep(0.3)
            return None

        self._hook_stop  = _open_ev_wait("CaptureHook_Stop")
        self._hook_ready = _open_ev_wait("CaptureHook_HookReady")
        self._hook_exit  = _open_ev_wait("CaptureHook_Exit")
        self._hook_init  = _open_ev_wait("CaptureHook_Initialize")

        if not all([self._hook_stop, self._hook_ready, self._hook_exit, self._hook_init]):
            raise RuntimeError("CaptureHook イベントのオープンに失敗")

        # 8. フック初期化イベントをセット → フック開始
        if not set_event(self._hook_init):
            raise RuntimeError("SetEvent(hook_init) failed")

        # 9. フック完了を待つ（既にフック済みなら hook_ready はすぐ返る）
        print("[obscam] hook_ready 待機中...")
        wait_for_single_object(self._hook_ready, 5000)   # 5秒タイムアウト
        print("[obscam] hook_ready 受信")

        # 10. テクスチャミューテックス（ReleaseMutexできるようMUTEX_ALL_ACCESSで開く）
        MUTEX_ALL_ACCESS = 0x1F0001
        for i, mname in enumerate(["CaptureHook_TextureMutex1", "CaptureHook_TextureMutex2"]):
            full_name = _map_name(mname, self._pid)
            h = open_mutex(MUTEX_ALL_ACCESS, False, full_name)
            if not h:
                # fallback: SYNCHRONIZE のみ
                h = open_mutex(SYNCHRONIZE, False, full_name)
            if not h:
                raise RuntimeError(f"OpenMutexPlusId failed: {mname}")
            self._tex_mutex[i] = h

        # 11. HookInfo を再度開く（フック後に更新される）
        self._open_hook_info_map()

        # 12. 共有テクスチャデータを取得（リトライあり）
        self._wait_for_shtex_data()

        # 13. D3D11 デバイス作成 & 共有リソースを開く（リトライあり）
        self._open_d3d11_resources()

        # 14. 共有テクスチャのフォーマットを取得してStagingテクスチャを作成
        try:
            tex2d = self._shared_res.QueryInterface(_ID3D11Texture2D)
            desc  = D3D11_TEXTURE2D_DESC()
            tex2d.GetDesc(ctypes.byref(desc))
            shared_fmt = desc.Format
            print(f"[obscam] 共有テクスチャ fmt={shared_fmt} ({desc.Width}x{desc.Height})")
        except Exception as e:
            print(f"[obscam] 共有テクスチャ desc 取得失敗 → BGRA fallback: {e}")
            shared_fmt = DXGI_FORMAT_B8G8R8A8_UNORM

        self._shared_fmt = shared_fmt
        self._staging_tex = _create_staging_texture(
            self._device, self._roi_w, self._roi_h, fmt=shared_fmt
        )
        self._roi_box = self._calc_roi_box()

        print(f"[obscam] 初期化完了  mode={self.mode}  "
              f"ROI={self._roi_w}x{self._roi_h}")

    def _inject(self):
        inject_exe = os.path.abspath(os.path.join(self._obs_dir, self.INJECT_EXE))
        hook_dll   = os.path.abspath(os.path.join(self._obs_dir, self.HOOK_DLL))

        print(f"[obscam] inject_exe: {inject_exe}")
        print(f"[obscam] hook_dll:   {hook_dll}")
        print(f"[obscam] exists inject: {os.path.exists(inject_exe)}")
        print(f"[obscam] exists dll:    {os.path.exists(hook_dll)}")

        if not os.path.exists(inject_exe):
            raise RuntimeError(f"inject-helper64.exe が見つかりません: {inject_exe}")
        if not os.path.exists(hook_dll):
            raise RuntimeError(f"graphics-hook64.dll が見つかりません: {hook_dll}")

        # lpApplicationName=None にして cmd に全部渡す (CreateProcessW の正しい使い方)
        cmd = f'"{inject_exe}" "{hook_dll}" 1 {self._tid}'
        print(f"[obscam] cmd: {cmd}")

        # TID で試してから失敗時は PID で再試行
        for target_id, label in [(self._tid, "TID"), (self._pid, "PID")]:
            cmd = f'"{inject_exe}" "{hook_dll}" 1 {target_id}'
            print(f"[obscam] 注入試行 ({label}={target_id}): {cmd}")
            proc, _ = create_process(inject_exe, cmd)
            if not proc:
                err = ctypes.get_last_error()
                print(f"[obscam] CreateProcess 失敗 ({label}): LastError={err}")
                continue
            wait_for_single_object(proc, INFINITE)
            code = get_exit_code_process(proc)
            close_handle(proc)
            print(f"[obscam] inject-helper64 終了コード={code} ({label})")
            if code == 0:
                print("[obscam] DLL 注入完了")
                return
            time.sleep(0.5)

        raise RuntimeError("inject-helper64.exe の注入がTID/PIDどちらでも失敗しました")

    def _open_hook_info_map(self, timeout_s: float = 10.0):
        """CaptureHook_HookInfo 共有メモリを開いて hook_info にマップ（リトライあり）"""
        if self._info_map:
            close_handle(self._info_map)
            self._info_map = None
        if self._hook_info_ptr:
            unmap_view_of_file(self._hook_info_ptr)
            self._hook_info_ptr = None

        name = _map_name("CaptureHook_HookInfo", self._pid)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._info_map = open_file_mapping(FILE_MAP_ALL_ACCESS, False, name)
            if self._info_map:
                break
            print("[obscam] CaptureHook_HookInfo 待機中...")
            time.sleep(0.5)

        if not self._info_map:
            raise RuntimeError("CaptureHook_HookInfo のオープンに失敗")

        ptr = map_view_of_file(
            self._info_map, FILE_MAP_ALL_ACCESS, 0, 0, ctypes.sizeof(HookInfo)
        )
        if not ptr:
            raise RuntimeError("MapViewOfFile (HookInfo) に失敗")

        self._hook_info_ptr = ptr
        if not ptr:
            raise RuntimeError("MapViewOfFile が NULL を返しました")
        self._hook_info = HookInfo.from_address(ptr)

    def _wait_for_shtex_data(self, timeout_s: float = 30.0):
        """共有テクスチャデータが使えるようになるまでポーリング"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._data_map:
                close_handle(self._data_map)
                self._data_map = None

            name = _data_map_name(
                self._hook_info.window, self._hook_info.map_id
            )
            print(f"[obscam] shtex map_name={name}  "
                  f"cx={self._hook_info.cx}  cy={self._hook_info.cy}  "
                  f"map_size={self._hook_info.map_size}  "
                  f"type={self._hook_info.type}")
            self._data_map = open_file_mapping(FILE_MAP_ALL_ACCESS, False, name)
            if self._data_map:
                ptr = map_view_of_file(
                    self._data_map, FILE_MAP_ALL_ACCESS,
                    0, 0, self._hook_info.map_size,
                )
                if ptr:
                    self._shtex_ptr = ptr
                    self._shtex     = ShtexData.from_address(ptr)
                    print(f"[obscam] tex_handle=0x{self._shtex.tex_handle:08X}")
                    return
            time.sleep(1.0)

        raise RuntimeError("共有テクスチャデータの取得がタイムアウト")

    def _open_d3d11_resources(self, max_retry: int = 5):
        """D3D11 デバイスを作り、共有テクスチャリソースを開く（最大 max_retry 回）"""
        for attempt in range(max_retry):
            self._device, self._ctx = _create_d3d11_device()

            handle = int(self._shtex.tex_handle)
            try:
                self._shared_res = _open_shared_resource(
                    self._device, handle, src_pid=self._pid
                )
                return
            except RuntimeError as e:
                print(f"[obscam] OpenSharedResource 失敗 (attempt {attempt+1}): {e}")
                _com_release(self._device)
                _com_release(self._ctx)
                self._device = ctypes.c_void_p(0)
                self._ctx    = ctypes.c_void_p(0)

                if attempt + 1 >= max_retry:
                    raise RuntimeError("OpenSharedResource が最大試行回数を超えました")

                # tex_handle が変わるまで少し待つ
                time.sleep(0.5)
                self._wait_for_shtex_data()

    def _calc_roi_box(self) -> D3D11_BOX:
        box = D3D11_BOX()
        box.left   = (self._screen_w - self._roi_w) // 2
        box.top    = (self._screen_h - self._roi_h) // 2
        box.front  = 0
        box.right  = box.left + self._roi_w
        box.bottom = box.top  + self._roi_h
        box.back   = 1
        return box

    # ── フレーム取得 ──────────────────────────────────────────────────────────

    def grab(self) -> Optional[torch.Tensor]:
        """単発キャプチャ。torch.Tensor(CUDA, uint8, [H,W,3], BGR) を返す。"""
        # restart イベントが来たら再初期化
        if wait_for_single_object(self._hook_restart or 0, 0) == WAIT_OBJECT_0:
            print("[obscam] hook_restart 検出 → 再初期化")
            self._initialize()

        return self._grab()

    def _grab(self) -> Optional[torch.Tensor]:
        # GPU 上で ROI をコピー（共有リソース → ステージング or interop テクスチャ）
        dst = self._staging_tex

        # texture_mutex でロックしてからコピー（OBS と同じプロトコル）
        locked_idx = -1
        for i, mx in enumerate(self._tex_mutex):
            if not mx:
                continue
            ret = wait_for_single_object(mx, 0)   # ノンブロッキング
            if ret == WAIT_OBJECT_0 or ret == 0x00000080:  # WAIT_ABANDONED も取得扱い
                locked_idx = i
                break

        if locked_idx == -1:
            return None

        try:
            _copy_subresource_region(
                self._ctx, dst, self._shared_res, self._roi_box
            )
            # Flush は非同期コマンドをGPUに送るだけで、CPU はブロックしない。
            # 次フレームで Map するまでに GPU が完了している前提（通常は十分間に合う）。
            self._ctx.Flush()
        except Exception as e:
            print(f"[obscam] CopySubresourceRegion 失敗: {e}")
            return None
        finally:
            ctypes.windll.kernel32.ReleaseMutex(
                ctypes.c_void_p(self._tex_mutex[locked_idx])
            )

        return self._to_tensor_cpu()

    def _to_tensor_cpu(self) -> Optional[torch.Tensor]:
        """CPU 経由フォールバック（ゼロコピー + 非同期CUDA転送）"""
        try:
            mapped = _map_texture(self._ctx, self._staging_tex)
        except RuntimeError as e:
            print(f"[obscam] Map 失敗: {e}")
            return None

        try:
            row_w = mapped.RowPitch // 4   # BGRA → 4 bytes/px

            # ── ゼロコピー: Map済みアドレスを直接 torch に見せる ──────────────
            # bytearray(buf) によるCPUコピーを排除。
            # torch.frombuffer は外部メモリへの参照を保持するので
            # Unmap 前に必ず contiguous clone するか CUDA 転送する。
            raw = torch.frombuffer(
                (ctypes.c_uint8 * (mapped.RowPitch * self._roi_h))
                .from_address(mapped.pData),
                dtype=torch.uint8,
            ).reshape(self._roi_h, row_w, 4)

            if row_w != self._roi_w:
                raw = raw[:, :self._roi_w, :]

            # ── ピン留めバッファ経由で非同期CUDA転送 ─────────────────────────
            # _pinned_buf はフレームサイズが変わらない限り再利用する。
            if torch.cuda.is_available():
                need_channels = 3
                h, w = self._roi_h, self._roi_w
                if (self._pinned_buf is None
                        or self._pinned_buf.shape != (h, w, need_channels)):
                    self._pinned_buf = torch.empty(
                        (h, w, need_channels), dtype=torch.uint8
                    ).pin_memory()

                # BGRA→BGR or RGBA→BGR をCPU側で確定してからピン留め先へ書く
                DXGI_FORMAT_R8G8B8A8_UNORM = 28
                if getattr(self, '_shared_fmt', DXGI_FORMAT_B8G8R8A8_UNORM) in (27, 28):
                    self._pinned_buf.copy_(raw[..., [2, 1, 0]])  # RGBA→BGR
                else:
                    self._pinned_buf.copy_(raw[..., :3])         # BGRA→BGR

                # non_blocking=True で CPU をブロックせずに転送開始
                t = self._pinned_buf.to("cuda", non_blocking=True)
            else:
                # CUDA なし: clone して Map を解放できるようにする
                DXGI_FORMAT_R8G8B8A8_UNORM = 28
                if getattr(self, '_shared_fmt', DXGI_FORMAT_B8G8R8A8_UNORM) in (27, 28):
                    t = raw[..., [2, 1, 0]].clone()
                else:
                    t = raw[..., :3].clone()

        finally:
            _unmap_texture(self._ctx, self._staging_tex)

        return t

    # ── 連続キャプチャ ────────────────────────────────────────────────────────

    def start(self, target_fps: float = 60.0):
        """バックグラウンドスレッドで連続キャプチャ開始"""
        if self.is_capturing:
            return
        self.is_capturing = True
        self._stop_event.clear()
        self._frame_event.clear()
        self._thread = Thread(
            target=self._capture_loop,
            args=(target_fps,),
            name="ObsCam",
            daemon=True,
        )
        self._thread.start()
        print(f"[obscam] 連続キャプチャ開始 target={target_fps}fps")

    def stop(self):
        """連続キャプチャ停止"""
        if not self.is_capturing:
            return
        self._stop_event.set()
        self._frame_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.is_capturing = False
        self._latest = None
        self._frame_event.clear()
        self._stop_event.clear()
        print("[obscam] 連続キャプチャ停止")

    def get_latest_frame(self, timeout: float = 1.0) -> Optional[torch.Tensor]:
        """最新フレームを返す（新フレームが来るまで最大 timeout 秒待機）"""
        self._frame_event.wait(timeout=timeout)
        self._frame_event.clear()
        return self._latest

    def _capture_loop(self, target_fps: float):
        interval  = 1.0 / target_fps
        count     = 0
        t_start   = time.perf_counter()

        while not self._stop_event.is_set():
            t0    = time.perf_counter()
            frame = self._grab()
            if frame is not None:
                self._latest = frame
                self._frame_event.set()
                count += 1

            elapsed_total = time.perf_counter() - t_start
            if elapsed_total >= 1.0:   # 1秒ごとにFPS更新（0.5秒では計測が荒い）
                self._capture_fps = count / elapsed_total
                count   = 0
                t_start = time.perf_counter()

            # 高精度スリープ: 残り時間の大半を sleep で返し、最後の2ms をビジーウェイト
            spent = time.perf_counter() - t0
            remaining = interval - spent
            if remaining > 0.002:
                time.sleep(remaining - 0.002)
            # ビジーウェイト（最後の2msを精度よく待機）
            while time.perf_counter() - t0 < interval:
                pass

    @property
    def capture_fps(self) -> float:
        return self._capture_fps

    @property
    def mode(self) -> str:
        return "cpu_fallback"

    # ── クリーンアップ ────────────────────────────────────────────────────────

    def release(self):
        self.stop()

        self._pinned_buf = None  # ピン留めメモリを解放

        for ptr in [self._staging_tex, self._shared_res, self._ctx, self._device]:
            _com_release(ptr)
        self._staging_tex = None
        self._shared_res  = None
        self._ctx         = None
        self._device      = None

        for h in [
            self._hook_stop, self._hook_ready, self._hook_exit,
            self._hook_init, self._hook_restart,
            self._tex_mutex[0], self._tex_mutex[1],
            self._keepalive,
        ]:
            close_handle(h)

        if self._hook_info_ptr:
            unmap_view_of_file(self._hook_info_ptr)
        if self._shtex_ptr:
            unmap_view_of_file(self._shtex_ptr)

        close_handle(self._info_map)
        close_handle(self._data_map)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass

    def __repr__(self):
        return (
            f"<ObsCam '{self.game_title}' "
            f"ROI={self._roi_w}x{self._roi_h} "
            f"screen={self._screen_w}x{self._screen_h} "
            f"mode={self.mode}>"
        )
