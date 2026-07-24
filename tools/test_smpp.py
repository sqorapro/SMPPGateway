# -*- coding: utf-8 -*-
"""
Локальный тест SMPP-сервера: bind -> submit_sm -> submit_sm_resp -> deliver_sm (DLR).
Модем не нужен: без модема отправка вернёт ошибку, а DLR придёт со статусом UNDELIV —
нам важно проверить, что протокол работает целиком (bind, ответы, статус доставки).

Запуск:  python tools/test_smpp.py
"""
import os
import sys
import time
import socket
import struct
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from smpp_server import SMPPServer
from modem_manager import ModemManager
from sms_handler import SmsHandler


class FakeDB:
    def log_sms(self, *a, **k): pass


def pack_pdu(cmd_id, status, seq, body=b''):
    return struct.pack('!IIII', 16 + len(body), cmd_id, status, seq) + body


def read_pdu(sock):
    hdr = b''
    while len(hdr) < 16:
        c = sock.recv(16 - len(hdr))
        if not c:
            return None
        hdr += c
    ln, cmd, status, seq = struct.unpack('!IIII', hdr)
    body = b''
    while len(body) < ln - 16:
        c = sock.recv(ln - 16 - len(body))
        if not c:
            break
        body += c
    return cmd, status, seq, body


def cstr(s):
    return s.encode('ascii') + b'\x00'


def main():
    PORT = 12775
    mm = ModemManager()
    db = FakeDB()
    sh = SmsHandler(mm, db)
    auth = {'system_id': 'testuser', 'password': 'testpass'}
    server = SMPPServer(host='127.0.0.1', port=PORT, auth=auth, sms_handler=sh, database=db)
    server.start()
    time.sleep(0.5)

    s = socket.socket()
    s.settimeout(5)
    s.connect(('127.0.0.1', PORT))

    # bind_transceiver
    body = cstr('testuser') + cstr('testpass') + cstr('') + struct.pack('!BBB', 0x34, 0, 0) + b'\x00'
    s.sendall(pack_pdu(0x00000009, 0, 1, body))
    cmd, status, seq, rb = read_pdu(s)
    print(f"BIND_RESP: cmd=0x{cmd:08X} status={status} system_id={rb.rstrip(chr(0).encode()).decode(errors='ignore')}")
    assert cmd == 0x80000009 and status == 0, "bind failed"

    # submit_sm с запросом статуса доставки (registered_delivery=1), UCS2 кириллица
    msg = "Привет".encode('utf-16-be')
    sbody = b''
    sbody += cstr('')                                  # service_type
    sbody += struct.pack('!BB', 1, 1) + cstr('SENDER') # source
    sbody += struct.pack('!BB', 1, 1) + cstr('998901234567')  # dest
    sbody += struct.pack('!BBB', 0, 0, 0)              # esm, protocol, priority
    sbody += cstr('') + cstr('')                       # schedule, validity
    sbody += struct.pack('!BBBB', 0x01, 0, 0x08, 0)    # reg_delivery=1, replace, dcs=UCS2, sm_default
    sbody += struct.pack('!B', len(msg)) + msg         # sm_length + short_message
    s.sendall(pack_pdu(0x00000004, 0, 2, sbody))

    cmd, status, seq, rb = read_pdu(s)
    mid = rb.rstrip(b'\x00').decode(errors='ignore')
    print(f"SUBMIT_RESP: cmd=0x{cmd:08X} status={status} message_id={mid}")
    assert cmd == 0x80000004 and status == 0, "submit failed"

    # ждём deliver_sm (DLR)
    cmd, status, seq, rb = read_pdu(s)
    print(f"DELIVER_SM (DLR): cmd=0x{cmd:08X}")
    assert cmd == 0x00000005, "no DLR received"
    txt = rb.decode('ascii', errors='ignore')
    print("  receipt:", txt[txt.find('id:'):txt.find('id:')+90] if 'id:' in txt else txt[:90])
    # ответить deliver_sm_resp
    s.sendall(pack_pdu(0x80000005, 0, seq, b'\x00'))

    # enquire_link
    s.sendall(pack_pdu(0x00000015, 0, 3))
    cmd, status, seq, rb = read_pdu(s)
    print(f"ENQUIRE_LINK_RESP: cmd=0x{cmd:08X} status={status}")
    assert cmd == 0x80000015

    # unbind
    s.sendall(pack_pdu(0x00000006, 0, 4))
    cmd, status, seq, rb = read_pdu(s)
    print(f"UNBIND_RESP: cmd=0x{cmd:08X}")
    s.close()
    server.stop()
    print("\nВСЁ ОК: bind, submit_sm, submit_resp, статус доставки (DLR), enquire_link, unbind — работают.")


if __name__ == "__main__":
    main()
