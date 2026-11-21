"""
Конвертирует видео в формат, максимально близкий к Android MediaRecorder.
Выходной файл должен пройти быструю метаданные-проверку сервера Twinby.

Использование:
    from convert_to_android import convert_for_liveness
    output_bytes = convert_for_liveness(input_path_or_bytes)
"""

import struct
import subprocess
import tempfile
import os
import time

# avc1 box из успешного Android-видео (640x480, Baseline@4.1, SPS/PPS от MediaCodec)
# Включает avcC с правильными SPS/PPS, pasp (1:1), colr (nclx)
_ANDROID_AVC1_HEX = (
    "000000a461766331000000000000000100000000000000000000000000000000"
    "028001e0004800000048000000000000000100000000000000000000000000000000"
    "000000000000000000000000000000000018ffff"
    "0000002b617663430142c029ffe100126742c0298d680a03da42000003000f08846a"
    "01000668ce01a835c8"
    "0000001070617370000100000001000000000013636f6c726e636c7800010001000180"
)
_ANDROID_AVC1 = bytes.fromhex(_ANDROID_AVC1_HEX)


def _ffmpeg_reencode(input_path: str, output_path: str, vflip: bool = False) -> None:
    """Перекодирует видео под формат Android фронталки."""
    vf = "scale=640:480,vflip" if vflip else "scale=640:480"
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-r", "30",
        "-c:v", "libx264",
        "-profile:v", "baseline",
        "-level", "4.1",
        "-preset", "fast",
        "-b:v", "3000k",
        "-g", "30",
        "-bf", "0",
        "-refs", "1",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        "-brand", "isom",
        "-map_metadata", "-1",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        "-f", "mp4",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg ошибка: {result.stderr[-500:]}")


def _find_atom(data, name, start=0, end=None):
    """Находит атом по имени. Возвращает (offset, size) или (None, None)."""
    if end is None:
        end = len(data)
    name_bytes = name.encode("ascii") if isinstance(name, str) else name
    offset = start
    while offset < end - 8:
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        aname = data[offset + 4:offset + 8]
        if size < 8 or offset + size > end:
            break
        if aname == name_bytes:
            return offset, size
        offset += size
    return None, None


def _stco_to_co64(data, stbl_offset, stbl_size):
    """Конвертирует stco в co64 внутри stbl."""
    stco_off, stco_size = _find_atom(data, b"stco", stbl_offset + 8, stbl_offset + stbl_size)
    if stco_off is None:
        return data, 0

    version_flags = data[stco_off + 8:stco_off + 12]
    entry_count = struct.unpack(">I", data[stco_off + 12:stco_off + 16])[0]

    offsets = []
    for i in range(entry_count):
        off = struct.unpack(">I", data[stco_off + 16 + i * 4:stco_off + 16 + i * 4 + 4])[0]
        offsets.append(off)

    co64_size = 16 + entry_count * 8
    co64 = struct.pack(">I", co64_size) + b"co64" + version_flags
    co64 += struct.pack(">I", entry_count)
    for off in offsets:
        co64 += struct.pack(">Q", off)

    size_diff = co64_size - stco_size
    data = data[:stco_off] + bytearray(co64) + data[stco_off + stco_size:]
    return data, size_diff


def _update_ancestors(data, changed_offset, size_diff):
    """Обновляет размеры только тех контейнеров, которые СОДЕРЖАТ changed_offset."""
    containers = [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd", b"edts"]
    for name in containers:
        search = 0
        while True:
            pos = data.find(name, search)
            if pos < 4:
                break
            off = pos - 4
            sz = struct.unpack(">I", data[off:off + 4])[0]
            # Контейнер содержит changed_offset?
            if off < changed_offset < off + sz:
                data[off:off + 4] = struct.pack(">I", sz + size_diff)
            search = pos + 8


def _patch_mp4_atoms(data: bytearray, debug_print=None) -> bytearray:
    """Пост-обработка: превращает FFmpeg-выход в Android MediaRecorder формат."""
    if debug_print is None:
        debug_print = lambda msg: None

    # === 1. ftyp: isom, minor=131072, brands=[isom,iso2,mp41] ===
    ftyp_size = struct.unpack(">I", data[0:4])[0]
    new_ftyp = struct.pack(">I", 28) + b"ftyp" + b"isom"
    new_ftyp += struct.pack(">I", 131072) + b"isom" + b"iso2" + b"mp41"
    data = bytearray(new_ftyp) + data[ftyp_size:]

    # === 2. Удаляем edts (FFmpeg-маркер, Android не создаёт) ===
    moov_off, moov_size = _find_atom(data, b"moov")
    trak_off, trak_size = _find_atom(data, b"trak", moov_off + 8, moov_off + moov_size)
    edts_off, edts_size = _find_atom(data, b"edts", trak_off + 8, trak_off + trak_size)
    if edts_off is not None:
        debug_print(f"  edts удалён ({edts_size} б)")
        data = data[:edts_off] + data[edts_off + edts_size:]
        _update_ancestors(data, edts_off, -edts_size)

    # === 3. Удаляем все udta ===
    while True:
        moov_off, moov_size = _find_atom(data, b"moov")
        if moov_off is None:
            break
        udta_pos = data.find(b"udta", moov_off, moov_off + moov_size)
        if udta_pos < 4:
            break
        udta_offset = udta_pos - 4
        udta_size = struct.unpack(">I", data[udta_offset:udta_offset + 4])[0]
        if udta_size < 8 or udta_offset + udta_size > len(data):
            break
        debug_print(f"  udta удалён ({udta_size} б)")
        data = data[:udta_offset] + data[udta_offset + udta_size:]
        _update_ancestors(data, udta_offset, -udta_size)

    # === 4-9. Навигируемся внутрь moov→trak→mdia→minf→stbl для всех внутренних патчей ===

    # 4a. handler_name: FFmpeg = "VideoHandler\x00" (13б), Android = "VideoHandle\x00" (12б)
    # Разница в 1 байт ("r" убирается)
    ffmpeg_vid = b"VideoHandler\x00"
    android_vid = b"VideoHandle\x00"
    hdlr_vid_pos = data.find(ffmpeg_vid)
    if hdlr_vid_pos >= 0:
        data = data[:hdlr_vid_pos] + bytearray(android_vid) + data[hdlr_vid_pos + len(ffmpeg_vid):]
        hdlr_name_pos = data.rfind(b"hdlr", 0, hdlr_vid_pos)
        if hdlr_name_pos > 4:
            hdlr_off = hdlr_name_pos - 4
            old_sz = struct.unpack(">I", data[hdlr_off:hdlr_off + 4])[0]
            data[hdlr_off:hdlr_off + 4] = struct.pack(">I", old_sz - 1)
            _update_ancestors(data, hdlr_off, -1)
        debug_print(f"  hdlr: VideoHandler→VideoHandle (-1 б)")

    ffmpeg_snd = b"SoundHandler\x00"
    android_snd = b"SoundHandle\x00"
    hdlr_snd_pos = data.find(ffmpeg_snd)
    if hdlr_snd_pos >= 0:
        data = data[:hdlr_snd_pos] + bytearray(android_snd) + data[hdlr_snd_pos + len(ffmpeg_snd):]
        hdlr_name_pos = data.rfind(b"hdlr", 0, hdlr_snd_pos)
        if hdlr_name_pos > 4:
            hdlr_off = hdlr_name_pos - 4
            old_sz = struct.unpack(">I", data[hdlr_off:hdlr_off + 4])[0]
            data[hdlr_off:hdlr_off + 4] = struct.pack(">I", old_sz - 1)
            _update_ancestors(data, hdlr_off, -1)

    # 4b. compressorname
    lavc_marker = b"\x0cLavc libx264"
    lavc_pos = data.find(lavc_marker)
    if lavc_pos >= 0:
        data[lavc_pos:lavc_pos + 32] = b"\x00" * 32

    # 5. creation_time (mvhd, tkhd, mdhd) — ищем через data.find
    mp4_epoch_offset = 2082844800
    now_mp4 = int(time.time()) + mp4_epoch_offset
    for atom_name in [b"mvhd", b"tkhd", b"mdhd"]:
        search_start = 0
        while True:
            pos = data.find(atom_name, search_start)
            if pos < 0:
                break
            version = data[pos + 4]
            if version == 0:
                data[pos + 8:pos + 12] = struct.pack(">I", now_mp4 & 0xFFFFFFFF)
                data[pos + 12:pos + 16] = struct.pack(">I", now_mp4 & 0xFFFFFFFF)
            search_start = pos + 8

    # 6. Timescale: mvhd=10000, mdhd=90000
    mvhd_pos = data.find(b"mvhd")
    if mvhd_pos > 0 and data[mvhd_pos + 4] == 0:
        old_ts = struct.unpack(">I", data[mvhd_pos + 16:mvhd_pos + 20])[0]
        old_dur = struct.unpack(">I", data[mvhd_pos + 20:mvhd_pos + 24])[0]
        new_ts = 10000
        new_dur = int(old_dur * new_ts / old_ts) if old_ts else old_dur
        data[mvhd_pos + 16:mvhd_pos + 20] = struct.pack(">I", new_ts)
        data[mvhd_pos + 20:mvhd_pos + 24] = struct.pack(">I", new_dur)
        debug_print(f"  mvhd timescale {old_ts}→{new_ts}")

    mdhd_pos = data.find(b"mdhd")
    if mdhd_pos > 0 and data[mdhd_pos + 4] == 0:
        old_ts = struct.unpack(">I", data[mdhd_pos + 16:mdhd_pos + 20])[0]
        old_dur = struct.unpack(">I", data[mdhd_pos + 20:mdhd_pos + 24])[0]
        new_ts = 90000
        new_dur = int(old_dur * new_ts / old_ts) if old_ts else old_dur
        data[mdhd_pos + 16:mdhd_pos + 20] = struct.pack(">I", new_ts)
        data[mdhd_pos + 20:mdhd_pos + 24] = struct.pack(">I", new_dur)
        debug_print(f"  mdhd timescale {old_ts}→{new_ts}")

    # 7-9. Работаем внутри stbl (навигация через контейнеры)
    moov_off, moov_size = _find_atom(data, b"moov")
    trak_off, trak_size = _find_atom(data, b"trak", moov_off + 8, moov_off + moov_size)
    mdia_off, mdia_size = _find_atom(data, b"mdia", trak_off + 8, trak_off + trak_size)
    minf_off, minf_size = _find_atom(data, b"minf", mdia_off + 8, mdia_off + mdia_size)
    stbl_off, stbl_size = _find_atom(data, b"stbl", minf_off + 8, minf_off + minf_size)

    # 7. Заменяем avc1 на Android'овский (SPS/PPS, pasp, colr)
    stsd_off, stsd_size = _find_atom(data, b"stsd", stbl_off + 8, stbl_off + stbl_size)
    if stsd_off:
        avc1_off, avc1_size = _find_atom(data, b"avc1", stsd_off + 16, stsd_off + stsd_size)
        if avc1_off:
            new_avc1 = _ANDROID_AVC1
            size_diff = len(new_avc1) - avc1_size
            data = data[:avc1_off] + bytearray(new_avc1) + data[avc1_off + avc1_size:]
            if size_diff != 0:
                _update_ancestors(data, avc1_off, size_diff)
            debug_print(f"  avc1→Android (SPS 18б, pasp, colr) [{size_diff:+d} б]")

    # 8. stco → co64 (перечитываем позиции после замены avc1)
    moov_off, moov_size = _find_atom(data, b"moov")
    trak_off, trak_size = _find_atom(data, b"trak", moov_off + 8, moov_off + moov_size)
    mdia_off, mdia_size = _find_atom(data, b"mdia", trak_off + 8, trak_off + trak_size)
    minf_off, minf_size = _find_atom(data, b"minf", mdia_off + 8, mdia_off + mdia_size)
    stbl_off, stbl_size = _find_atom(data, b"stbl", minf_off + 8, minf_off + minf_size)
    stco_off, _ = _find_atom(data, b"stco", stbl_off + 8, stbl_off + stbl_size)
    if stco_off is not None:
        data, sd = _stco_to_co64(data, stbl_off, stbl_size)
        if sd:
            _update_ancestors(data, stco_off, sd)
            debug_print(f"  stco→co64 (+{sd} б)")

    # === 10. Большой free перед mdat (как Android) ===
    moov_off, moov_size = _find_atom(data, b"moov")
    moov_end = moov_off + moov_size

    # Удаляем маленький free
    free_off, free_size = _find_atom(data, b"free", moov_end)
    old_free_size = 0
    if free_off is not None and free_off < moov_end + 100:
        old_free_size = free_size
        data = data[:free_off] + data[free_off + free_size:]

    mdat_off, mdat_size = _find_atom(data, b"mdat")
    if mdat_off is None:
        raise RuntimeError("mdat не найден")

    # === 10a. Конвертируем mdat в extended-size (64-бит) как у Android ===
    # Android MediaRecorder: [00 00 00 01] [mdat] [8-byte size] [data...]
    # FFmpeg:                 [4-byte size] [mdat] [data...]
    mdat_data = data[mdat_off + 8:mdat_off + mdat_size]  # чистые данные mdat
    ext_mdat_total = 16 + len(mdat_data)  # 4(sz=1) + 4(mdat) + 8(ext_size) + data
    ext_mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", ext_mdat_total)
    data = data[:mdat_off] + bytearray(ext_mdat) + bytearray(mdat_data) + data[mdat_off + mdat_size:]
    mdat_header_growth = 8  # extended header на 8 байт длиннее обычного
    debug_print(f"  mdat→extended size (64-бит, +{mdat_header_growth} б)")

    target_free_size = 300 * 1024
    free_atom = struct.pack(">I", target_free_size) + b"free" + (b"\x00" * (target_free_size - 8))
    data = data[:mdat_off] + bytearray(free_atom) + data[mdat_off:]

    # Обновляем chunk offsets: free вставлен + mdat header вырос
    offset_shift = target_free_size - old_free_size + mdat_header_growth
    co64_pos = data.find(b"co64")
    if co64_pos > 0:
        entry_count = struct.unpack(">I", data[co64_pos + 8:co64_pos + 12])[0]
        for i in range(entry_count):
            eo = co64_pos + 12 + i * 8
            old = struct.unpack(">Q", data[eo:eo + 8])[0]
            data[eo:eo + 8] = struct.pack(">Q", old + offset_shift)
    debug_print(f"  free {old_free_size}→{target_free_size}")

    # === 11. wide atom в конце (как в эталоне) ===
    wide_atom = struct.pack(">I", 170) + b"wide" + (b"\x00" * 162)
    data += bytearray(wide_atom)

    return data


def convert_for_liveness(input_data, debug_print=None, vflip: bool = False) -> bytes:
    """
    Принимает путь к файлу (str) или bytes.
    Возвращает bytes MP4 в формате Android MediaRecorder.
    """
    if debug_print is None:
        debug_print = lambda msg: None

    with tempfile.TemporaryDirectory() as tmpdir:
        if isinstance(input_data, (str, os.PathLike)):
            input_path = str(input_data)
        else:
            input_path = os.path.join(tmpdir, "input.mp4")
            with open(input_path, "wb") as f:
                f.write(input_data)

        output_path = os.path.join(tmpdir, "output.mp4")

        flip_str = " + vflip" if vflip else ""
        debug_print(f"FFmpeg: 640x480, Baseline, no audio{flip_str}...")
        _ffmpeg_reencode(input_path, output_path, vflip=vflip)

        with open(output_path, "rb") as f:
            raw = bytearray(f.read())
        debug_print(f"FFmpeg: {len(raw)} байт")

        debug_print("Патч atoms → Android MediaRecorder...")
        patched = _patch_mp4_atoms(raw, debug_print=debug_print)

        debug_print(f"Итого: {len(patched)} байт")
        return bytes(patched)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("python convert_to_android.py input.mp4 [output.mp4]")
        sys.exit(1)
    result = convert_for_liveness(sys.argv[1], debug_print=print)
    out = sys.argv[2] if len(sys.argv) > 2 else "android_output.mp4"
    with open(out, "wb") as f:
        f.write(result)
    print(f"→ {out}")
