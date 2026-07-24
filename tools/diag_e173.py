#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E173 Voice Diagnostic Tool
===========================
Задача: понять, умеет ли конкретный E173 заказчика проигрывать аудио в звонок,
и каким именно способом (какой порт, какие AT-команды).

Зависимость только одна: pyserial  ->  pip install pyserial

РЕЖИМЫ:
  python diag_e173.py scan
      Безопасно. Перебирает все COM-порты, снимает модель/IMEI/voice-флаги.
      Ничего никуда не звонит. Запускать первым.

  python diag_e173.py tone COMxx +99890xxxxxxx
      Делает РЕАЛЬНЫЙ звонок на указанный номер и, когда ответят,
      пытается проиграть тестовый писк (1 кГц) четырьмя способами по очереди.
      Заказчик должен ОТВЕТИТЬ на звонок и слушать: слышен ли писк.
      COMxx — порт из результата scan (обычно "PC UI" или "Modem" интерфейс E173).

Весь вывод дублируется в файл diag_e173_log.txt рядом со скриптом.
"""

import sys
import os
import time
import math
import struct

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("НЕТ pyserial. Установите:  pip install pyserial")
    sys.exit(1)

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diag_e173_log.txt")
_logf = open(LOG_PATH, "a", encoding="utf-8")


def log(msg=""):
    line = str(msg)
    print(line)
    _logf.write(line + "\n")
    _logf.flush()


def hr():
    log("-" * 70)


# ----------------------------------------------------------------------------
# Низкоуровневая работа с портом
# ----------------------------------------------------------------------------

def open_port(port, baud=9600, timeout=1):
    return serial.Serial(port, baud, timeout=timeout, write_timeout=3)

def at(conn, cmd, wait=1.5):
    """Послать AT-команду, вернуть ответ строкой (с логом)."""
    try:
        conn.reset_input_buffer()
        conn.write((cmd + "\r").encode("ascii"))
        conn.flush()
    except Exception as e:
        log(f"    [write error] {e}")
        return ""
    time.sleep(0.2)
    end = time.time() + wait
    buf = b""
    while time.time() < end:
        n = conn.in_waiting
        if n:
            buf += conn.read(n)
            if b"OK" in buf or b"ERROR" in buf:
                break
        time.sleep(0.05)
    resp = buf.decode("ascii", errors="ignore").strip()
    log(f"    >> {cmd}")
    for l in resp.splitlines():
        if l.strip():
            log(f"       {l.strip()}")
    return resp


# ----------------------------------------------------------------------------
# РЕЖИМ scan — безопасная диагностика всех портов
# ----------------------------------------------------------------------------

def do_scan():
    hr()
    log("E173 SCAN  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    hr()

    ports = list(serial.tools.list_ports.comports())
    if not ports:
        log("COM-порты не найдены вообще. Модем воткнут? Драйвер Huawei стоит?")
        return

    log(f"Найдено COM-портов: {len(ports)}")
    for p in ports:
        log(f"  {p.device:8}  desc='{p.description}'  hwid='{p.hwid}'")
    hr()

    for p in ports:
        port = p.device
        log(f"### Порт {port}  ({p.description})")
        opened = None
        for baud in (9600, 115200):
            try:
                opened = open_port(port, baud)
                # проверка живости
                r = at(opened, "AT", 1.0)
                if "OK" in r:
                    log(f"    порт отвечает на AT (baud={baud})")
                    break
                opened.close()
                opened = None
            except Exception as e:
                log(f"    открыть {port}@{baud}: {e}")
                opened = None
        if not opened:
            log(f"    -> {port}: не отвечает на AT (это может быть не AT-порт E173)")
            hr()
            continue

        # Сбор возможностей
        at(opened, "ATI")                 # модель/прошивка
        at(opened, "AT+CGMM")             # модель
        at(opened, "AT+CGMR")             # версия прошивки
        at(opened, "AT+CGSN")             # IMEI
        at(opened, "AT+CSQ")              # сигнал
        at(opened, "AT^U2DIAG?")          # конфигурация портов Huawei
        at(opened, "AT+FCLASS=?")         # поддержка классов (8 = voice)
        at(opened, "AT^CVOICE=?")         # поддержка voice (ключевое!)
        at(opened, "AT^CVOICE?")          # текущее состояние voice
        at(opened, "AT^DDSETEX=?")        # маршрутизация voice-данных (ключевое!)
        at(opened, "AT+VTS=?")            # DTMF
        at(opened, "AT^CPCM?")            # параметры PCM
        try:
            opened.close()
        except Exception:
            pass
        hr()

    log("SCAN завершён. Пришли мне весь этот вывод (или файл diag_e173_log.txt).")
    log("Смотрю на: какой порт ответил на AT^CVOICE=? -> OK  (это voice-порт).")
    hr()


# ----------------------------------------------------------------------------
# Генерация тестового тона: 1 кГц, 8000 Гц, mono, 16-bit PCM
# ----------------------------------------------------------------------------

def gen_tone_pcm(seconds=6, freq=1000, rate=8000, amp=12000):
    n = int(seconds * rate)
    data = bytearray()
    for i in range(n):
        s = int(amp * math.sin(2 * math.pi * freq * (i / rate)))
        data += struct.pack("<h", s)
    return bytes(data)


def stream_pcm(conn, pcm, tag, monitor=None):
    """Стримит PCM чанками 320 байт (20 мс) с точным таймингом по «часам»."""
    chunk = 320
    sent = 0
    frame_sec = chunk / 2 / 8000.0
    start = time.perf_counter()
    frame = 0
    log(f"    [{tag}] стримлю {len(pcm)} байт ...")
    for i in range(0, len(pcm), chunk):
        try:
            conn.write(pcm[i:i + chunk])
        except Exception as e:
            log(f"    [{tag}] write error: {e}")
            return sent
        sent += chunk
        frame += 1
        target = start + frame * frame_sec
        dt = target - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        if monitor is not None and monitor.in_waiting:
            urc = monitor.read(monitor.in_waiting).decode("ascii", errors="ignore")
            if urc.strip():
                log(f"    [{tag}] URC: {urc.strip()}")
            if "^CEND" in urc or "NO CARRIER" in urc:
                log(f"    [{tag}] абонент завершил звонок")
                return sent
    log(f"    [{tag}] отправлено {sent} байт (~{sent//2//8000} c)")
    return sent


# ----------------------------------------------------------------------------
# РЕЖИМ tone — реальный звонок + попытки проиграть тон
# ----------------------------------------------------------------------------

def do_tone(at_port, voice_port, number):
    hr()
    log("E173 TONE TEST v3  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log(f"AT-порт={at_port}  VOICE-порт={voice_port}  номер={number}")
    log("Схема: команды на AT-порт, звук — в отдельный VOICE-порт.")
    log("Ответьте на звонок и слушайте — должен быть писк ~10 секунд.")
    hr()

    try:
        conn = open_port(at_port, 115200, timeout=1)   # рабочая прога открывает @115200
    except Exception as e:
        log(f"не открыть AT-порт {at_port}: {e}")
        return

    try:
        vconn = open_port(voice_port, 115200, timeout=1)
    except Exception as e:
        log(f"не открыть VOICE-порт {voice_port}: {e}")
        conn.close()
        return

    if "OK" not in at(conn, "AT", 1.0):
        log("AT-порт не отвечает на AT. Проверь номер порта.")
        conn.close(); vconn.close()
        return

    # подготовка
    at(conn, "AT+CVHU=0")      # не вешать трубку по DTR
    at(conn, "AT+CLIP=1")

    tone = gen_tone_pcm(seconds=10, freq=1000)

    # набор
    log(f"    набираю {number} ...")
    conn.reset_input_buffer()
    conn.write(f"ATD{number};\r".encode("ascii"))

    answered = False
    end = time.time() + 40
    resp = ""
    while time.time() < end:
        if conn.in_waiting:
            data = conn.read(conn.in_waiting).decode("ascii", errors="ignore")
            resp += data
            if data.strip():
                log(f"    URC: {data.strip()}")
            if "^CONN" in resp:
                answered = True
                break
            if any(x in resp for x in ("BUSY", "NO CARRIER", "NO ANSWER", "^CEND", "ERROR")):
                log("    звонок не состоялся (busy/no answer/error).")
                conn.write(b"ATH\r")
                conn.close(); vconn.close()
                return
        time.sleep(0.2)

    if not answered:
        log("    ^CONN не получен за 40 c. Заказчик не ответил.")
        conn.write(b"ATH\r")
        conn.close(); vconn.close()
        return

    log("    ^CONN получен — абонент ответил.")
    time.sleep(0.5)

    # Открыть голосовой мост на AT-порту, затем лить PCM в VOICE-порт
    hr()
    log("Открываю мост AT^DDSETEX=2 (на AT-порту), звук лью в VOICE-порт")
    r = at(conn, "AT^DDSETEX=2", 1.5)
    if "ERROR" in r:
        log("    DDSETEX вернул ERROR — стримлю всё равно.")
    time.sleep(0.2)
    sent = stream_pcm(vconn, tone, "VOICE", monitor=conn)

    hr()
    log("    вешаю трубку.")
    conn.write(b"ATH\r")
    time.sleep(0.5)
    conn.close(); vconn.close()

    hr()
    log("ГОТОВО. Напиши: слышал заказчик писк (да/нет)?")
    log(f"    (отправлено {sent} байт PCM в {voice_port})")
    hr()


# ----------------------------------------------------------------------------

def load_wav_pcm(path):
    """WAV -> PCM 8000/моно/16бит (чистый Python, тот же метод, что в приложении)."""
    import wave
    wf = wave.open(path, "rb")
    ch, width, rate, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
    raw = wf.readframes(n)
    wf.close()
    if width != 2:
        raise ValueError(f"нужен 16-бит WAV, а тут {width*8} бит")
    samples = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    if ch > 1:
        samples = [sum(samples[i:i+ch]) // ch for i in range(0, len(samples), ch)]
    if rate > 8000:
        # anti-alias low-pass (каскад 2x одно-полюсных, ~3400 Гц) до понижения частоты
        rc = 1.0 / (2.0 * math.pi * 3400.0)
        dt = 1.0 / rate
        alpha = dt / (rc + dt)
        fl = samples
        for _ in range(2):
            acc = float(fl[0]); tmp = [0.0] * len(fl)
            for i, x in enumerate(fl):
                acc += alpha * (x - acc); tmp[i] = acc
            fl = tmp
        samples = fl
    if rate != 8000:
        ratio = 8000 / rate
        out_len = int(len(samples) * ratio)
        out = [0] * out_len
        for i in range(out_len):
            sp = i / ratio
            i0 = int(sp); i1 = min(i0 + 1, len(samples) - 1); f = sp - i0
            out[i] = int(samples[i0] * (1 - f) + samples[i1] * f)
        samples = out
    data = bytearray()
    for s in samples:
        s = 32767 if s > 32767 else (-32768 if s < -32768 else s)
        data += struct.pack("<h", s)
    log(f"    аудио загружено: {len(data)} байт, ~{len(data)//2//8000} c")
    return bytes(data)


def do_play(at_port, voice_port, number, wav_path):
    hr()
    log("E173 PLAY TEST (реальный файл)  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log(f"AT={at_port} VOICE={voice_port} номер={number} файл={wav_path}")
    hr()
    if not os.path.isfile(wav_path):
        log(f"файл не найден: {wav_path}")
        return
    try:
        pcm = load_wav_pcm(wav_path)
    except Exception as e:
        log(f"ошибка загрузки WAV: {e}")
        return

    try:
        conn = open_port(at_port, 115200, timeout=1)
        vconn = open_port(voice_port, 115200, timeout=1)
    except Exception as e:
        log(f"не открыть порт: {e}")
        return

    if "OK" not in at(conn, "AT", 1.0):
        log("AT-порт молчит"); conn.close(); vconn.close(); return
    at(conn, "AT+CVHU=0")
    log(f"    набираю {number} ...")
    conn.reset_input_buffer()
    conn.write(f"ATD{number};\r".encode("ascii"))

    answered = False
    end = time.time() + 40
    resp = ""
    while time.time() < end:
        if conn.in_waiting:
            d = conn.read(conn.in_waiting).decode("ascii", errors="ignore")
            resp += d
            if d.strip():
                log(f"    URC: {d.strip()}")
            if "^CONN" in resp:
                answered = True; break
            if any(x in resp for x in ("BUSY", "NO CARRIER", "NO ANSWER", "^CEND", "ERROR")):
                log("    звонок не состоялся."); conn.write(b"ATH\r"); conn.close(); vconn.close(); return
        time.sleep(0.2)
    if not answered:
        log("    нет ответа за 40 c."); conn.write(b"ATH\r"); conn.close(); vconn.close(); return

    log("    ^CONN — отвечено. Открываю мост и проигрываю файл.")
    at(conn, "AT^DDSETEX=2", 1.5)
    time.sleep(0.2)
    sent = stream_pcm(vconn, pcm, "PLAY", monitor=conn)
    time.sleep(0.5)
    conn.write(b"ATH\r"); time.sleep(0.3)
    conn.close(); vconn.close()
    hr()
    log(f"ГОТОВО. Отправлено {sent} байт. Заказчик должен был услышать ГОЛОС из файла.")
    log("Напиши: слышал ли заказчик запись (да/нет), не искажена ли скорость/тон.")
    hr()


def usage():
    log("Использование:")
    log("  python diag_e173.py scan")
    log("  python diag_e173.py tone <AT-порт> <VOICE-порт> <номер>")
    log("  python diag_e173.py play <AT-порт> <VOICE-порт> <номер> <файл.wav>")
    log("  пример: python diag_e173.py play COM28 COM29 +99890XXXXXXX Рухшона_2.wav")


def main():
    if len(sys.argv) < 2:
        usage()
        return
    mode = sys.argv[1].lower()
    if mode == "scan":
        do_scan()
    elif mode == "tone":
        if len(sys.argv) < 5:
            usage()
            return
        do_tone(sys.argv[2], sys.argv[3], sys.argv[4])
    elif mode == "play":
        if len(sys.argv) < 6:
            usage()
            return
        do_play(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    else:
        usage()


if __name__ == "__main__":
    main()
