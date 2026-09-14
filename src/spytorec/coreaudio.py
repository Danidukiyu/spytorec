"""macOS CoreAudio: pins the capture device to unity gain.

The system volume slider writes to a loopback device's own volume, on both
scopes, and that attenuation lands in the recording. No-op off macOS.
"""

import sys
import ctypes
import struct
import logging

_IS_MACOS = sys.platform == 'darwin'

# kAudioObjectSystemObject
_SYSTEM_OBJECT = 1

# kAudioObjectPropertyElementMain - the whole device rather than one channel.
_ELEMENT_MAIN = 0


def _fourcc(code: str) -> int:
    return struct.unpack('>I', code.encode())[0]


_DEVICES = _fourcc('dev#')          # kAudioHardwarePropertyDevices
_NAME = _fourcc('lnam')             # kAudioObjectPropertyName
_STREAMS = _fourcc('slay')          # kAudioDevicePropertyStreamConfiguration
_VOLUME = _fourcc('volm')           # kAudioDevicePropertyVolumeScalar
_MUTE = _fourcc('mute')             # kAudioDevicePropertyMute

_SCOPE_GLOBAL = _fourcc('glob')
_SCOPE_INPUT = _fourcc('inpt')
_SCOPE_OUTPUT = _fourcc('outp')


class _Address(ctypes.Structure):
    _fields_ = [('selector', ctypes.c_uint32),
                ('scope', ctypes.c_uint32),
                ('element', ctypes.c_uint32)]


_ca = None
_cf = None


def _load() -> bool:
    """Binds the two frameworks on first use. False if they are unavailable."""
    global _ca, _cf

    if _ca is not None:
        return _ca is not False

    if not _IS_MACOS:
        _ca = _cf = False
        return False

    try:
        ca = ctypes.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    except OSError as e:
        logging.debug(f"CoreAudio unavailable: {e}")
        _ca = _cf = False
        return False

    p_addr = ctypes.POINTER(_Address)

    ca.AudioObjectHasProperty.restype = ctypes.c_ubyte
    ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, p_addr]

    ca.AudioObjectIsPropertySettable.restype = ctypes.c_int32
    ca.AudioObjectIsPropertySettable.argtypes = [
        ctypes.c_uint32, p_addr, ctypes.POINTER(ctypes.c_ubyte)]

    ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    ca.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, p_addr, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32)]

    ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, p_addr, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]

    ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
    ca.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32, p_addr, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_void_p]

    cf.CFStringGetCString.restype = ctypes.c_ubyte
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    _ca, _cf = ca, cf
    return True


def _has(obj: int, selector: int, scope: int, element: int = _ELEMENT_MAIN) -> bool:
    return bool(_ca.AudioObjectHasProperty(obj, ctypes.byref(_Address(selector, scope, element))))


def _settable(obj: int, selector: int, scope: int, element: int = _ELEMENT_MAIN) -> bool:
    out = ctypes.c_ubyte(0)
    addr = _Address(selector, scope, element)
    if _ca.AudioObjectIsPropertySettable(obj, ctypes.byref(addr), ctypes.byref(out)):
        return False
    return bool(out.value)


def _get(obj: int, selector: int, scope: int, ctype, element: int = _ELEMENT_MAIN):
    addr = _Address(selector, scope, element)
    size = ctypes.c_uint32(ctypes.sizeof(ctype))
    value = ctype()
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None,
                                      ctypes.byref(size), ctypes.byref(value)):
        return None
    return value.value


def _set(obj: int, selector: int, scope: int, ctype, value, element: int = _ELEMENT_MAIN) -> bool:
    addr = _Address(selector, scope, element)
    payload = ctype(value)
    return _ca.AudioObjectSetPropertyData(obj, ctypes.byref(addr), 0, None,
                                          ctypes.sizeof(ctype), ctypes.byref(payload)) == 0


def _device_name(device: int):
    ref = _get(device, _NAME, _SCOPE_GLOBAL, ctypes.c_void_p)
    if not ref:
        return None
    try:
        buf = ctypes.create_string_buffer(512)
        if not _cf.CFStringGetCString(ref, buf, 512, 0x08000100):  # kCFStringEncodingUTF8
            return None
        return buf.value.decode('utf-8', 'replace')
    finally:
        _cf.CFRelease(ref)


def _input_channels(device: int) -> int:
    """Channel count on the device's input side, 0 if it cannot record."""
    addr = _Address(_STREAMS, _SCOPE_INPUT, _ELEMENT_MAIN)
    size = ctypes.c_uint32()
    if _ca.AudioObjectGetPropertyDataSize(device, ctypes.byref(addr), 0, None,
                                          ctypes.byref(size)):
        return 0

    buf = ctypes.create_string_buffer(size.value)
    if _ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None,
                                      ctypes.byref(size), buf):
        return 0

    # AudioBufferList: UInt32 mNumberBuffers, 4 bytes of padding, then an
    # AudioBuffer every 16 bytes opening with its own channel count.
    raw = buf.raw
    count = struct.unpack_from('I', raw, 0)[0]
    return sum(struct.unpack_from('I', raw, 8 + i * 16)[0]
               for i in range(count) if 8 + i * 16 + 4 <= len(raw))


def _find_input_device(name: str):
    """The input device called `name`; a playback device can share the name."""
    addr = _Address(_DEVICES, _SCOPE_GLOBAL, _ELEMENT_MAIN)
    size = ctypes.c_uint32()
    if _ca.AudioObjectGetPropertyDataSize(_SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                          ctypes.byref(size)):
        return None

    devices = (ctypes.c_uint32 * (size.value // ctypes.sizeof(ctypes.c_uint32)))()
    if _ca.AudioObjectGetPropertyData(_SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                      ctypes.byref(size), devices):
        return None

    for device in devices:
        if _device_name(device) == name and _input_channels(device) > 0:
            return device
    return None


def force_unity_gain(device_name: str) -> list:
    """Pins `device_name` to full volume, unmuted, on both scopes.
    Returns a description of each change made.
    """
    if not _load() or not device_name:
        return []

    try:
        device = _find_input_device(device_name)
        if device is None:
            logging.debug(f"No CoreAudio input device named '{device_name}'")
            return []

        changed = []

        for scope, label in ((_SCOPE_OUTPUT, 'output'), (_SCOPE_INPUT, 'input')):
            if _has(device, _VOLUME, scope) and _settable(device, _VOLUME, scope):
                level = _get(device, _VOLUME, scope, ctypes.c_float)
                # An untouched device reads a shade under 1.0
                if level is not None and level < 0.999:
                    if _set(device, _VOLUME, scope, ctypes.c_float, 1.0):
                        changed.append(f"{label} volume {level:.2f} -> 1.00")

            if _has(device, _MUTE, scope) and _settable(device, _MUTE, scope):
                if _get(device, _MUTE, scope, ctypes.c_uint32):
                    if _set(device, _MUTE, scope, ctypes.c_uint32, 0):
                        changed.append(f"{label} unmuted")

        return changed

    except Exception as e:
        logging.debug(f"Unity gain failed for '{device_name}': {e}")
        return []
