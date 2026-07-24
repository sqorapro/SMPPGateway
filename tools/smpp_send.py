# -*- coding: utf-8 -*-
"""
Простой SMPP-клиент для теста SMPP-сервера (в т.ч. Android-приложения партнёра).
Подключается к серверу, авторизуется и отправляет одну SMS.

Использование:
  python smpp_send.py <host> <port> <login> <password> <номер> "<текст>"

Пример (телефон с APK в той же Wi-Fi, его IP 192.168.1.50):
  python smpp_send.py 192.168.1.50 2775 test test123 998901234567 "Привет тест"

Кириллица и длинный текст поддерживаются (шлётся UCS2, при длине >70 — частями).
"""
import sys
import time
import socket
import struct


def cs(s):
    return s.encode("ascii", "ignore") + b"\x00"


def pdu(cmd, status, seq, body=b""):
    return struct.pack("!IIII", 16 + len(body), cmd, status, seq) + body


def read_pdu(s):
    h = b""
    while len(h) < 16:
        c = s.recv(16 - len(h))
        if not c:
            return None
        h += c
    ln, cmd, st, seq = struct.unpack("!IIII", h)
    b = b""
    while len(b) < ln - 16:
        c = s.recv(ln - 16 - len(b))
        if not c:
            break
        b += c
    return cmd, st, seq, b


def submit_body(dest, text_ucs2, udh=b""):
    esm = 0x40 if udh else 0x00
    sm = udh + text_ucs2
    b = cs("")                                   # service_type
    b += struct.pack("!BB", 1, 1) + cs("SMPP")   # source
    b += struct.pack("!BB", 1, 1) + cs(dest)     # dest
    b += struct.pack("!BBB", esm, 0, 0)          # esm, protocol, priority
    b += cs("") + cs("")                         # schedule, validity
    b += struct.pack("!BBBB", 1, 0, 0x08, 0)     # reg_delivery=1, replace, dcs=UCS2, default
    b += struct.pack("!B", len(sm)) + sm         # sm_length + short_message
    return b


def main():
    if len(sys.argv) < 7:
        print(__doc__)
        return
    host, port, login, pwd, dest, text = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]

    s = socket.socket()
    s.settimeout(10)
    print(f"Подключаюсь к {host}:{port} ...")
    s.connect((host, port))

    # bind_transceiver
    body = cs(login) + cs(pwd) + cs("") + struct.pack("!BBB", 0x34, 0, 0) + b"\x00"
    s.sendall(pdu(0x00000009, 0, 1, body))
    r = read_pdu(s)
    print("BIND:", "OK" if r and r[1] == 0 else f"ОШИБКА status={r[1] if r else '-'}")
    if not r or r[1] != 0:
        s.close()
        return

    # разбиваем на части по 67 символов (UCS2), если длинное
    parts = [text[i:i + 67] for i in range(0, len(text), 67)] or [""]
    seq = 2
    if len(parts) == 1:
        s.sendall(pdu(0x00000004, 0, seq, submit_body(dest, text.encode("utf-16-be"))))
        r = read_pdu(s)
        print("SUBMIT:", "принято сервером" if r and r[1] == 0 else f"ОШИБКА {r[1] if r else '-'}")
        seq += 1
    else:
        ref = int(time.time()) & 0xFF
        for i, part in enumerate(parts, 1):
            udh = bytes([0x05, 0x00, 0x03, ref, len(parts), i])
            s.sendall(pdu(0x00000004, 0, seq, submit_body(dest, part.encode("utf-16-be"), udh)))
            r = read_pdu(s)
            print(f"SUBMIT часть {i}/{len(parts)}:", "OK" if r and r[1] == 0 else f"ОШИБКА {r[1] if r else '-'}")
            seq += 1

    time.sleep(1)
    s.sendall(pdu(0x00000006, 0, seq))  # unbind
    s.close()
    print("Готово. Проверьте, пришла ли SMS на телефон", dest)


if __name__ == "__main__":
    main()
