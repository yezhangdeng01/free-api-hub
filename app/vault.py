"""密钥加密存储：Windows DPAPI（CryptProtectData）

- 加密密钥由 Windows 派生，绑定当前用户 + 本机，无法在别的机器/账户解密
- config.json 中的密文带 dpapi: 前缀（base64）
- 非 Windows 环境降级为明文存储（开源跨平台时由使用者自行权衡）
"""
import base64
import ctypes
import logging
import os

logger = logging.getLogger("api-hub")

PREFIX = "dpapi:"


class _DATA_BLOB(ctypes.Structure):
    # cbData 用 c_ulong：Windows 上 DWORD 即 32 位无符号（与平台指针位宽无关），
    # 避免顶层依赖 ctypes.wintypes（非 Windows 无此模块，会导致 import 即崩）
    _fields_ = [("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _protect(data: bytes) -> bytes:
    blob_in, blob_out = _blob(data), _DATA_BLOB()
    desc = ctypes.c_wchar_p("api-hub")
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), desc, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:
    blob_in, blob_out = _blob(data), _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encrypt(plain: str) -> str:
    """明文 → dpapi:密文；空值/已是密文则原样返回"""
    if not plain or plain.startswith(PREFIX):
        return plain
    if os.name != "nt":
        return plain  # 非 Windows：明文降级
    try:
        return PREFIX + base64.b64encode(_protect(plain.encode("utf-8"))).decode("ascii")
    except OSError as e:
        logger.error("密钥加密失败，将以明文保存: %s", e)
        return plain


def decrypt(value: str) -> str:
    """dpapi:密文 → 明文；非密文原样返回；解密失败返回空串"""
    if not value or not value.startswith(PREFIX):
        return value
    try:
        blob = base64.b64decode(value[len(PREFIX):])
        return _unprotect(blob).decode("utf-8")
    except Exception as e:
        logger.error("密钥解密失败（文件可能来自其他机器或账户），请重新填写: %s", e)
        return ""
