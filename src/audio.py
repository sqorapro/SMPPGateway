# -*- coding: utf-8 -*-
"""
Аудио для голосовых звонков E173.
Задача: любой входной файл -> сырой PCM 8000 Гц, моно, 16 бит little-endian.
Именно такой формат ждёт голосовой порт Huawei E173 (^CVOICE:0,8000,16,20).

Два пути загрузки:
  1) WAV — читается стандартным модулем wave + ресемплинг на чистом Python.
     Работает ВЕЗДЕ, без внешних зависимостей и без audioop (удалён в Python 3.13).
  2) mp3/ogg/прочее — через ffmpeg, ЕСЛИ он найден (в PATH или рядом с программой).

Публичное:
  load_pcm_8k_mono16(path) -> (pcm_bytes, duration_sec)
  probe_audio_support(path) -> (ok: bool, reason: str)
"""

import os
import sys
import wave
import struct
import shutil
import subprocess

TARGET_RATE = 8000
TARGET_WIDTH = 2      # 16 бит
TARGET_CHANNELS = 1   # моно


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    """Ищет ffmpeg: рядом с программой, затем в PATH. Возвращает путь или None."""
    for name in ("ffmpeg.exe", "ffmpeg"):
        local = os.path.join(_app_dir(), name)
        if os.path.isfile(local):
            return local
        # на уровень выше (dist/ffmpeg.exe при запуске из src)
        up = os.path.join(os.path.dirname(_app_dir()), name)
        if os.path.isfile(up):
            return up
    p = shutil.which("ffmpeg")
    return p


# ---------------------------------------------------------------------------
# Чистый Python: WAV -> PCM 8k mono 16
# ---------------------------------------------------------------------------

def _read_wav_raw(path):
    """Читает WAV, приводит к моно 16 бит (без ресемплинга). Возвращает (samples[list int], rate)."""
    wf = wave.open(path, "rb")
    ch = wf.getnchannels()
    width = wf.getsampwidth()
    rate = wf.getframerate()
    n = wf.getnframes()
    raw = wf.readframes(n)
    wf.close()

    # привести разрядность к 16 бит
    if width == 2:
        samples = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    elif width == 1:
        # 8-бит без знака -> 16 бит со знаком
        samples = [(b - 128) << 8 for b in raw]
    elif width == 4:
        vals = struct.unpack("<%di" % (len(raw) // 4), raw)
        samples = [v >> 16 for v in vals]
    else:
        raise ValueError(f"неподдерживаемая разрядность WAV: {width*8} бит")

    # свести в моно усреднением каналов
    if ch > 1:
        mono = []
        for i in range(0, len(samples), ch):
            frame = samples[i:i + ch]
            mono.append(sum(frame) // len(frame))
        samples = mono

    return samples, rate


def _lowpass(samples, rate, fc=3400.0, poles=2):
    """
    Простой каскадный одно-полюсный low-pass. Убирает частоты выше телефонной
    полосы ДО понижения частоты — иначе они «сворачиваются» в шипящий алиасинг.
    """
    if not samples:
        return samples
    import math
    rc = 1.0 / (2.0 * math.pi * fc)
    dt = 1.0 / rate
    alpha = dt / (rc + dt)
    out = samples
    for _ in range(poles):
        acc = float(out[0])
        filt = [0.0] * len(out)
        for i, x in enumerate(out):
            acc += alpha * (x - acc)
            filt[i] = acc
        out = filt
    return out


def _resample_linear(samples, src_rate, dst_rate=TARGET_RATE):
    """Anti-alias фильтр (при понижении частоты) + линейная интерполяция."""
    if not samples:
        return samples
    if src_rate == dst_rate:
        return samples
    # фильтруем только при понижении частоты (src > dst)
    if src_rate > dst_rate:
        samples = _lowpass(samples, src_rate, fc=3400.0, poles=2)
    ratio = dst_rate / src_rate
    out_len = int(len(samples) * ratio)
    out = [0] * out_len
    for i in range(out_len):
        src_pos = i / ratio
        i0 = int(src_pos)
        i1 = min(i0 + 1, len(samples) - 1)
        frac = src_pos - i0
        out[i] = int(samples[i0] * (1 - frac) + samples[i1] * frac)
    return out


def _samples_to_bytes(samples):
    # клип в диапазон int16
    clipped = bytearray()
    for s in samples:
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        clipped += struct.pack("<h", s)
    return bytes(clipped)


def _load_wav_pure(path):
    samples, rate = _read_wav_raw(path)
    samples = _resample_linear(samples, rate, TARGET_RATE)
    pcm = _samples_to_bytes(samples)
    dur = len(pcm) / 2 / TARGET_RATE
    return pcm, dur


# ---------------------------------------------------------------------------
# ffmpeg: любой формат -> PCM 8k mono 16
# ---------------------------------------------------------------------------

def _load_via_ffmpeg(path, ffmpeg):
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-ar", str(TARGET_RATE), "-ac", str(TARGET_CHANNELS),
        "-f", "s16le", "-acodec", "pcm_s16le",
        "pipe:1",
    ]
    flags = 0
    if os.name == "nt":
        flags = 0x08000000  # CREATE_NO_WINDOW — не мигать консолью
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg: " + proc.stderr.decode("utf-8", "ignore")[:300])
    pcm = proc.stdout
    dur = len(pcm) / 2 / TARGET_RATE
    return pcm, dur


# ---------------------------------------------------------------------------
# Публичное API
# ---------------------------------------------------------------------------

def _load_via_miniaudio(path):
    """Декодирует любой формат (mp3/wav/ogg/flac) -> PCM 8000/моно/16 без внешних программ.
    Читаем файл сами и декодируем из памяти — обходит проблему кириллицы в пути."""
    import miniaudio
    with open(path, "rb") as f:
        data = f.read()
    dec = miniaudio.decode(
        data,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=TARGET_CHANNELS,
        sample_rate=TARGET_RATE,
    )
    pcm = bytes(dec.samples)
    dur = len(pcm) / 2 / TARGET_RATE
    return pcm, dur


def load_pcm_8k_mono16(path):
    """
    Возвращает (pcm_bytes, duration_sec) в формате 8000/моно/16бит.
    Порядок: miniaudio (mp3/wav/ogg, без зависимостей) -> WAV чистым Python -> ffmpeg.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    ext = os.path.splitext(path)[1].lower()

    # 1) miniaudio — читает и mp3, и wav, без внешних программ
    try:
        import miniaudio  # noqa: F401
        return _load_via_miniaudio(path)
    except ImportError:
        pass
    except Exception as e:
        logger_msg = str(e)
        # для WAV попробуем чистый Python ниже; для mp3 — пробросим/ffmpeg
        if ext != ".wav":
            ffmpeg = find_ffmpeg()
            if ffmpeg:
                return _load_via_ffmpeg(path, ffmpeg)
            raise RuntimeError(f"Не удалось прочитать {ext}: {logger_msg}")

    # 2) WAV чистым Python (fallback без miniaudio)
    if ext == ".wav":
        try:
            return _load_wav_pure(path)
        except Exception:
            ffmpeg = find_ffmpeg()
            if ffmpeg:
                return _load_via_ffmpeg(path, ffmpeg)
            raise

    # 3) прочие форматы без miniaudio — только ffmpeg
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        return _load_via_ffmpeg(path, ffmpeg)
    raise RuntimeError(
        f"Формат {ext} не поддержан: нет miniaudio и ffmpeg. Используйте WAV."
    )


def probe_audio_support(path):
    """Проверка перед стартом кампании. (ok, причина)."""
    try:
        pcm, dur = load_pcm_8k_mono16(path)
        if not pcm:
            return False, "пустой аудиопоток"
        return True, f"OK, {dur:.1f}с"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    # быстрый тест: python audio.py <файл>
    if len(sys.argv) > 1:
        ok, msg = probe_audio_support(sys.argv[1])
        print("ffmpeg:", find_ffmpeg())
        print("результат:", ok, msg)
