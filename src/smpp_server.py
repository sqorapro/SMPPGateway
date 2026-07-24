import socket
import struct
import threading
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Глобальный потокобезопасный счётчик message_id
_msg_id_lock = threading.Lock()
_msg_id_counter = [0]


def next_message_id():
    with _msg_id_lock:
        _msg_id_counter[0] = (_msg_id_counter[0] + 1) & 0x7FFFFFFF
        return _msg_id_counter[0]


import os
import sys
from datetime import datetime as _dt

_smpp_dbg_lock = threading.Lock()


def _smpp_debug_path():
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
    return os.path.join(base, "smpp_debug.log")


def smpp_debug(msg):
    """Пишет события SMPP-сервера в smpp_debug.log для диагностики."""
    try:
        with _smpp_dbg_lock:
            with open(_smpp_debug_path(), "a", encoding="utf-8") as f:
                f.write(f"[{_dt.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

SMPP_NPI_UNKNOWN = 0x00
SMPP_NPI_ISDN = 0x01
SMPP_TON_UNKNOWN = 0x00
SMPP_TON_INTERNATIONAL = 0x01
SMPP_TON_NATIONAL = 0x02

CMD_BIND_TRANSMITTER = 0x00000002
CMD_BIND_TRANSMITTER_RESP = 0x80000002
CMD_BIND_RECEIVER = 0x00000001
CMD_BIND_RECEIVER_RESP = 0x80000001
CMD_BIND_TRANSCEIVER = 0x00000009
CMD_BIND_TRANSCEIVER_RESP = 0x80000009
CMD_SUBMIT_SM = 0x00000004
CMD_SUBMIT_SM_RESP = 0x80000004
CMD_DELIVER_SM = 0x00000005
CMD_DELIVER_SM_RESP = 0x80000005
CMD_ENQUIRE_LINK = 0x00000015
CMD_ENQUIRE_LINK_RESP = 0x80000015
CMD_UNBIND = 0x00000006
CMD_UNBIND_RESP = 0x80000006
CMD_GENERIC_NACK = 0x80000000

DATA_CODING_DEFAULT = 0x00
DATA_CODING_UCS2 = 0x08


def encode_coctet(s):
    if not s:
        return b'\x00'
    return s.encode('ascii') + b'\x00'


def decode_coctet(data, offset):
    end = data.index(b'\x00', offset)
    val = data[offset:end].decode('ascii', errors='ignore')
    return val, end + 1


def encode_address(addr, ton=SMPP_TON_INTERNATIONAL, npi=SMPP_NPI_ISDN):
    if not addr:
        return struct.pack('!BB', ton, npi) + b'\x00'
    return struct.pack('!BB', ton, npi) + addr.encode('ascii') + b'\x00'


def decode_address(data, offset):
    ton, npi = struct.unpack('!BB', data[offset:offset+2])
    offset += 2
    addr, offset = decode_coctet(data, offset)
    return {'ton': ton, 'npi': npi, 'addr': addr}, offset


class SMPPSession(threading.Thread):
    def __init__(self, conn, addr, auth, sms_handler, database, callback=None):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.auth = auth
        self.sms_handler = sms_handler
        self.database = database
        self.callback = callback
        self.bound = False
        self.system_id = None
        self._running = True
        self.sequence = 1
        self._seq_lock = threading.Lock()
        self._send_lock = threading.Lock()   # conn.sendall из разных потоков (DLR)
        self._concat = {}                     # буфер частей длинных SMS: (dest, ref) -> {seq: text}

    def run(self):
        logger.info(f"SMPP session started from {self.addr}")
        smpp_debug(f"сессия открыта с {self.addr}")
        reason = "обрыв соединения"
        try:
            while self._running:
                pdu = self._read_pdu()
                if pdu is None:
                    break
                if pdu is False:
                    continue  # таймаут ожидания — сессия жива, ждём следующий PDU
                self._handle_pdu(pdu)
            if not self._running:
                reason = "остановка сервера"
        except Exception as e:
            reason = f"ошибка: {e}"
            logger.error(f"SMPP session error: {e}")
        finally:
            try:
                self.conn.close()
            except Exception:
                pass
            logger.info(f"SMPP session closed from {self.addr}")
            smpp_debug(f"сессия закрыта ({reason}) с {self.addr}")
            self._cb("session_closed", str(self.addr))

    def _read_pdu(self):
        # None = обрыв; False = таймаут ожидания (сессия жива); dict = PDU
        header = self._recv_exact(16, first=True)
        if header is None:
            return None
        if header is False:
            return False
        cmd_len, cmd_id, status, seq = struct.unpack('!IIII', header)
        body_len = cmd_len - 16
        body = b''
        if body_len > 0:
            body = self._recv_exact(body_len)
            if not body:
                return None
        return {'cmd_id': cmd_id, 'status': status, 'seq': seq, 'body': body}

    def _recv_exact(self, n, first=False):
        """Читает ровно n байт. None=обрыв, False=таймаут на границе PDU (сессия жива)."""
        data = b''
        while len(data) < n:
            try:
                chunk = self.conn.recv(n - len(data))
                if not chunk:
                    return None  # реальный обрыв
                data += chunk
            except socket.timeout:
                if first and len(data) == 0:
                    return False  # нового PDU пока нет — не рвём сессию, ждём
                continue           # PDU начался — дочитываем остаток
            except Exception as e:
                logger.error(f"recv error: {e}")
                return None
        return data

    def _send_pdu(self, cmd_id, status, seq, body=b''):
        cmd_len = 16 + len(body)
        header = struct.pack('!IIII', cmd_len, cmd_id, status, seq)
        try:
            with self._send_lock:
                self.conn.sendall(header + body)
        except Exception as e:
            logger.error(f"send error: {e}")

    def _next_seq(self):
        with self._seq_lock:
            self.sequence = (self.sequence + 1) & 0x7FFFFFFF
            return self.sequence

    def _cb(self, event, data):
        """Безопасный вызов GUI-callback: ошибка интерфейса не должна рушить SMPP-сессию."""
        if self.callback:
            try:
                self.callback(event, data)
            except Exception as e:
                logger.error(f"SMPP callback error ({event}): {e}")
                smpp_debug(f"callback error ({event}): {e}")

    _CMD_NAMES = {
        0x00000009: "bind_transceiver", 0x00000002: "bind_transmitter",
        0x00000001: "bind_receiver", 0x00000004: "submit_sm",
        0x00000015: "enquire_link", 0x00000006: "unbind",
        0x80000005: "deliver_sm_resp",
    }

    def _handle_pdu(self, pdu):
        cmd_id = pdu['cmd_id']
        smpp_debug(f"<- {self._CMD_NAMES.get(cmd_id, hex(cmd_id))} seq={pdu['seq']}")
        if cmd_id == CMD_BIND_TRANSCEIVER:
            self._handle_bind(pdu, CMD_BIND_TRANSCEIVER_RESP)
        elif cmd_id == CMD_BIND_TRANSMITTER:
            self._handle_bind(pdu, CMD_BIND_TRANSMITTER_RESP)
        elif cmd_id == CMD_BIND_RECEIVER:
            self._handle_bind(pdu, CMD_BIND_RECEIVER_RESP)
        elif cmd_id == CMD_SUBMIT_SM:
            self._handle_submit_sm(pdu)
        elif cmd_id == CMD_ENQUIRE_LINK:
            self._send_pdu(CMD_ENQUIRE_LINK_RESP, 0, pdu['seq'])
        elif cmd_id == CMD_DELIVER_SM_RESP:
            pass  # клиент подтвердил получение нашего delivery receipt
        elif cmd_id == CMD_UNBIND:
            self._send_pdu(CMD_UNBIND_RESP, 0, pdu['seq'])
            self._running = False
            self.bound = False
        else:
            logger.warning(f"Unknown SMPP command: 0x{cmd_id:08X}")
            self._send_pdu(CMD_GENERIC_NACK, 0, pdu['seq'])

    def _handle_bind(self, pdu, resp_cmd=CMD_BIND_TRANSCEIVER_RESP):
        body = pdu['body']
        try:
            system_id, offset = decode_coctet(body, 0)
            password, offset = decode_coctet(body, offset)
            system_type, offset = decode_coctet(body, offset)
            interface_version = body[offset]
            addr_ton = body[offset + 1]
            addr_npi = body[offset + 2]
            address_range, offset = decode_coctet(body, offset + 3)
        except Exception as e:
            logger.error(f"Bind parse error: {e}")
            self._send_pdu(resp_cmd, 0x0000000F, pdu['seq'])
            return

        if self.auth and (system_id != self.auth.get('system_id') or password != self.auth.get('password')):
            logger.warning(f"SMPP auth failed for {system_id} from {self.addr}")
            self._send_pdu(resp_cmd, 0x0000000E, pdu['seq'])
            return

        self.bound = True
        self.system_id = system_id
        logger.info(f"SMPP bound: {system_id} from {self.addr}")
        smpp_debug(f"-> bind_resp OK, system_id={system_id} (авторизация успешна)")
        resp_body = encode_coctet(system_id)
        # TLV sc_interface_version (0x0210) = SMPP 3.4 — иначе строгие клиенты
        # считают сервер несовместимым и делают unbind, не отправив submit_sm
        resp_body += struct.pack('!HHB', 0x0210, 1, 0x34)
        self._send_pdu(resp_cmd, 0, pdu['seq'], resp_body)
        self._cb("session_bound", system_id)

    def _handle_submit_sm(self, pdu):
        body = pdu['body']
        try:
            service_type, offset = decode_coctet(body, 0)
            source_addr_ton = body[offset]
            source_addr_npi = body[offset + 1]
            offset += 2
            source_addr, offset = decode_coctet(body, offset)
            dest_addr_ton = body[offset]
            dest_addr_npi = body[offset + 1]
            offset += 2
            dest_addr, offset = decode_coctet(body, offset)
            esm_class = body[offset]
            protocol_id = body[offset + 1]
            priority_flag = body[offset + 2]
            schedule_delivery_time, offset = decode_coctet(body, offset + 3)
            validity_period, offset = decode_coctet(body, offset)
            registered_delivery = body[offset]
            replace_if_present = body[offset + 1]
            data_coding = body[offset + 2]
            sm_default_msg_id = body[offset + 3]
            sm_length = body[offset + 4]
            offset += 5
            short_message = body[offset:offset + sm_length]
            offset += sm_length
        except Exception as e:
            logger.error(f"submit_sm parse error: {e}")
            self._send_pdu(CMD_SUBMIT_SM_RESP, 0x0000000A, pdu['seq'])
            return

        want_dlr = bool(registered_delivery & 0x01)

        # Подробный лог входящего запроса — для анализа интеграции (какой водитель/данные
        # присылает внешняя система в полях отправителя, service_type и тексте).
        smpp_debug(f"RAW submit_sm: service_type='{service_type}' "
                   f"source(from)='{source_addr}' (ton={source_addr_ton},npi={source_addr_npi}) "
                   f"dest(to)='{dest_addr}' esm=0x{esm_class:02X} dcs={data_coding}")

        # Многочастное сообщение: клиент шлёт части с заголовком склейки (UDH).
        # Надо убрать UDH, собрать части по номеру и отправить одним сообщением.
        if (esm_class & 0x40) and sm_length > 0:
            udhl = short_message[0]
            udh = short_message[1:1 + udhl]
            payload = short_message[1 + udhl:]
            concat = self._parse_concat(udh)
            part_text = self._decode_message(payload, data_coding)
            # на каждую часть сразу отвечаем OK
            msg_id = f"{next_message_id():08X}"
            self._send_pdu(CMD_SUBMIT_SM_RESP, 0, pdu['seq'], encode_coctet(msg_id))
            if concat:
                ref, total, seq = concat
                key = (dest_addr, ref)
                buf = self._concat.setdefault(key, {})
                buf[seq] = part_text
                smpp_debug(f"submit_sm (часть {seq}/{total}): to={dest_addr} dcs={data_coding} ref={ref}")
                if len(buf) >= total:
                    full = ''.join(buf.get(i, '') for i in range(1, total + 1))
                    del self._concat[key]
                    smpp_debug(f"собрано длинное SMS: to={dest_addr} len={len(full)} msg='{full[:40]}'")
                    self._cb("sms_received", {"from": source_addr, "to": dest_addr, "message": full})
                    threading.Thread(target=self._deliver_worker,
                                     args=(msg_id, source_addr, dest_addr, full, want_dlr),
                                     daemon=True).start()
                return
            # UDH есть, но не склейка — просто отправляем текст без заголовка
            message = part_text
        else:
            message = self._decode_message(short_message, data_coding)

        logger.info(f"SMPP submit_sm: to={dest_addr}, from={source_addr}, msg={message[:50]}...")
        smpp_debug(f"submit_sm: to={dest_addr} dcs={data_coding} reg={registered_delivery} "
                   f"len={sm_length} msg='{message[:200]}'")

        self._cb("sms_received", {"from": source_addr, "to": dest_addr, "message": message})

        msg_id = f"{next_message_id():08X}"
        self._send_pdu(CMD_SUBMIT_SM_RESP, 0, pdu['seq'], encode_coctet(msg_id))
        threading.Thread(target=self._deliver_worker,
                         args=(msg_id, source_addr, dest_addr, message, want_dlr),
                         daemon=True).start()

    @staticmethod
    def _parse_concat(udh):
        """Ищет в UDH информацию о склейке. Возвращает (ref, total, seq) или None."""
        i = 0
        while i + 1 < len(udh):
            iei = udh[i]
            iedl = udh[i + 1]
            ied = udh[i + 2:i + 2 + iedl]
            if iei == 0x00 and iedl == 3:        # 8-битный ref
                return ied[0], ied[1], ied[2]
            if iei == 0x08 and iedl == 4:        # 16-битный ref
                return (ied[0] << 8) | ied[1], ied[2], ied[3]
            i += 2 + iedl
        return None

    def _deliver_worker(self, msg_id, source_addr, dest_addr, message, want_dlr):
        """Фон: отправка SMS через модем + (при запросе) статус доставки клиенту."""
        try:
            ok, info = self.sms_handler.send_single(dest_addr, message, source=f"smpp:{self.system_id}")
        except Exception as e:
            ok, info = False, str(e)
        smpp_debug(f"отправка через модем: {dest_addr} -> {'OK' if ok else 'ОШИБКА: ' + str(info)}")
        self._cb("sms_sent", {"to": dest_addr, "success": ok, "info": info, "id": msg_id})
        if want_dlr and self.bound:
            try:
                self._send_delivery_receipt(msg_id, source_addr, dest_addr, message, ok)
            except Exception as e:
                logger.error(f"DLR send error: {e}")

    def _send_delivery_receipt(self, msg_id, orig_source, orig_dest, message, delivered):
        """Отправляет deliver_sm со статусом доставки (delivery receipt) клиенту."""
        now = datetime.now().strftime("%y%m%d%H%M")
        stat = "DELIVRD" if delivered else "UNDELIV"
        err = "000" if delivered else "001"
        dlvrd = "001" if delivered else "000"
        text = message[:20]
        receipt = (f"id:{msg_id} sub:001 dlvrd:{dlvrd} submit date:{now} done date:{now} "
                   f"stat:{stat} err:{err} text:{text}")

        body = b''
        body += encode_coctet("")                          # service_type
        # в receipt адреса меняются местами: отправитель = получатель SMS
        body += struct.pack('!BB', SMPP_TON_INTERNATIONAL, SMPP_NPI_ISDN)
        body += encode_coctet(orig_dest)                   # source_addr
        body += struct.pack('!BB', SMPP_TON_INTERNATIONAL, SMPP_NPI_ISDN)
        body += encode_coctet(orig_source or "")           # dest_addr
        body += struct.pack('!B', 0x04)                    # esm_class = delivery receipt
        body += struct.pack('!B', 0)                       # protocol_id
        body += struct.pack('!B', 0)                       # priority_flag
        body += encode_coctet("")                          # schedule_delivery_time
        body += encode_coctet("")                          # validity_period
        body += struct.pack('!B', 0)                       # registered_delivery
        body += struct.pack('!B', 0)                       # replace_if_present
        body += struct.pack('!B', 0)                       # data_coding
        body += struct.pack('!B', 0)                       # sm_default_msg_id
        rec = receipt.encode('ascii', errors='replace')
        body += struct.pack('!B', len(rec))                # sm_length
        body += rec
        # TLV: message_state (0x0427) и receipted_message_id (0x001E)
        state = 2 if delivered else 8                      # DELIVERED / REJECTED
        body += struct.pack('!HHB', 0x0427, 1, state)
        rid = msg_id.encode('ascii') + b'\x00'
        body += struct.pack('!HH', 0x001E, len(rid)) + rid

        self._send_pdu(CMD_DELIVER_SM, 0, self._next_seq(), body)

    def _decode_message(self, data, data_coding):
        if data_coding == DATA_CODING_UCS2:
            try:
                return data.decode('utf-16-be')
            except Exception:
                return data.decode('ascii', errors='ignore')
        return data.decode('ascii', errors='ignore')


class SMPPServer(threading.Thread):
    def __init__(self, host='0.0.0.0', port=2775, auth=None, sms_handler=None, database=None, callback=None):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.auth = auth
        self.sms_handler = sms_handler
        self.database = database
        self.callback = callback
        self._running = False
        self._sock = None
        self.sessions = []

    def run(self):
        self._running = True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(5)
            self._sock.settimeout(1)
            logger.info(f"SMPP server listening on {self.host}:{self.port}")
            self._cb("server_started", f"{self.host}:{self.port}")
            while self._running:
                try:
                    conn, addr = self._sock.accept()
                    conn.settimeout(5)
                    session = SMPPSession(conn, addr, self.auth, self.sms_handler, self.database, self.callback)
                    session.start()
                    self.sessions.append(session)
                except socket.timeout:
                    continue
                except Exception as e:
                    if self._running:
                        logger.error(f"SMPP accept error: {e}")
        except Exception as e:
            logger.error(f"SMPP server error: {e}")
            self._cb("server_error", str(e))
        finally:
            if self._sock:
                self._sock.close()
            self._cb("server_stopped", "")

    def _cb(self, event, data):
        if self.callback:
            try:
                self.callback(event, data)
            except Exception as e:
                logger.error(f"SMPP server callback error ({event}): {e}")

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def is_running(self):
        return self._running

    def session_count(self):
        return sum(1 for s in self.sessions if s.bound)
