#!/usr/bin/env python3
"""
127.0.0.1:9200 本機列印橋接（獨立輕量版）

只做一件事：接收 POS (app.py) 送來的 ESC/POS 資料，短暫連到真印表機送出後立刻斷線。
不做 UDP 3289 探索、不做常駐印表機連線、不假冒印表機身分 —— 目的是完全不佔用
印表機的 9100 連線插槽，避免跟平板直連互相搶連線。

跟 proxy.py / proxy_v2.py 完全獨立，不共用任何 port（proxy 系列目前皆已停用）。
"""
import socket
import threading
import datetime

LISTEN_HOST = '127.0.0.1'
LISTEN_PORT = 9200
PRINTER_IP  = '192.168.1.124'
PRINTER_PORT = 9100
CONNECT_TIMEOUT = 5

def ts():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def handle_conn(conn, addr):
    try:
        buf = bytearray()
        conn.settimeout(5)
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        conn.close()
        if not buf:
            return

        try:
            printer = socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=CONNECT_TIMEOUT)
        except Exception as e:
            print(f"[{ts()}][列印橋接] 連不上印表機 {PRINTER_IP}:{PRINTER_PORT}：{e}")
            return

        try:
            printer.sendall(bytes(buf))
            print(f"[{ts()}][列印橋接] 送出 {len(buf)} bytes 到印表機")
        except Exception as e:
            print(f"[{ts()}][列印橋接] 送出失敗：{e}")
        finally:
            printer.close()

    except Exception as e:
        print(f"[{ts()}][列印橋接] 處理連線錯誤：{e}")

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((LISTEN_HOST, LISTEN_PORT))
    s.listen(5)
    print(f"[{ts()}] 本機列印橋接就緒 {LISTEN_HOST}:{LISTEN_PORT} -> {PRINTER_IP}:{PRINTER_PORT}")
    while True:
        try:
            conn, addr = s.accept()
            threading.Thread(target=handle_conn, args=(conn, addr), daemon=True).start()
        except Exception as e:
            print(f"[{ts()}] Accept 錯誤：{e}")

if __name__ == '__main__':
    main()
