# -*- coding: utf-8 -*-
"""
Reverse SSH-туннель: пробрасывает локальный SMPP-порт на VPS, чтобы внешние
SMPP-клиенты подключались по домену (your-domain.example:2775), а трафик шёл
через VPS на ПК за NAT.

Схема:  клиент -> your-domain.example:2775 (VPS) --SSH--> этот ПК:local_port -> SMPP-сервер

Ключ туннеля (smpp_tunnel_key) лежит рядом с программой. Пользователь на VPS —
ограниченный, с правами только на port-forwarding (без shell).
"""

import os
import sys
import time
import socket
import select
import threading
import logging

logger = logging.getLogger(__name__)

try:
    import paramiko
    HAVE_PARAMIKO = True
except ImportError:
    HAVE_PARAMIKO = False


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_tunnel_key():
    """Ищет приватный ключ туннеля: рядом с программой и в упакованных ресурсах."""
    dirs = [_app_dir(), os.path.dirname(_app_dir())]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        dirs.append(sys._MEIPASS)  # ключ, вшитый в exe через datas
    for name in ("smpp_tunnel_key", "smpp_tunnel_key.txt"):
        for d in dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def _forward(chan, local_port):
    """Двусторонний перенос данных между SSH-каналом и локальным SMPP-портом."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(("127.0.0.1", local_port))
    except Exception as e:
        logger.error(f"tunnel: не подключиться к локальному {local_port}: {e}")
        chan.close()
        return
    sock.settimeout(None)
    try:
        while True:
            r, _, _ = select.select([sock, chan], [], [], 1.0)
            if sock in r:
                data = sock.recv(4096)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                sock.sendall(data)
    except Exception:
        pass
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


class ReverseTunnel:
    """Держит reverse SSH-туннель к VPS с авто-переподключением."""

    def __init__(self, vps_host, vps_user, key_path, remote_port, local_port,
                 vps_ssh_port=22, on_status=None):
        self.vps_host = vps_host
        self.vps_user = vps_user
        self.key_path = key_path
        self.remote_port = remote_port
        self.local_port = local_port
        self.vps_ssh_port = vps_ssh_port
        self.on_status = on_status
        self._running = False
        self._thread = None
        self._transport = None
        self._connected = False

    def _notify(self, up, msg):
        self._connected = up
        if self.on_status:
            try:
                self.on_status(up, msg)
            except Exception:
                pass
        logger.info(f"tunnel: {msg}")

    def is_up(self):
        return self._connected

    def start(self):
        if not HAVE_PARAMIKO:
            self._notify(False, "Модуль paramiko не установлен — туннель недоступен")
            return False
        if not self.key_path or not os.path.isfile(self.key_path):
            self._notify(False, "Ключ туннеля не найден рядом с программой")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._transport:
            try:
                self._transport.close()
            except Exception:
                pass
        self._connected = False

    def _load_key(self):
        # ключ ed25519; на всякий случай пробуем и RSA
        try:
            return paramiko.Ed25519Key.from_private_key_file(self.key_path)
        except Exception:
            return paramiko.RSAKey.from_private_key_file(self.key_path)

    def _loop(self):
        while self._running:
            try:
                key = self._load_key()
                self._transport = paramiko.Transport((self.vps_host, self.vps_ssh_port))
                self._transport.set_keepalive(30)
                self._transport.connect(username=self.vps_user, pkey=key)
                self._transport.request_port_forward("0.0.0.0", self.remote_port)
                self._notify(True, f"Туннель поднят: VPS:{self.remote_port} -> локальный SMPP:{self.local_port}")

                while self._running and self._transport.is_active():
                    chan = self._transport.accept(1000)
                    if chan is None:
                        continue
                    threading.Thread(target=_forward, args=(chan, self.local_port), daemon=True).start()
            except Exception as e:
                self._notify(False, f"Туннель разорван: {e}. Переподключение через 10 с")
            finally:
                if self._transport:
                    try:
                        self._transport.close()
                    except Exception:
                        pass
                self._connected = False
            if self._running:
                time.sleep(10)  # пауза перед переподключением
