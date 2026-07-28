import argparse
import base64
import hashlib
import json
import os
import socket
import ssl


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def load_token(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    token = data.get("auth_token")
    device_id = data.get("device_id")
    if not token:
        raise RuntimeError("token.json no contiene auth_token")
    return token, device_id


def recv_until(sock, marker, timeout=10.0):
    sock.settimeout(timeout)
    buf = b""
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def parse_headers(raw):
    head, _, leftover = raw.partition(b"\r\n\r\n")
    text = head.decode("utf-8", "ignore")
    lines = text.split("\r\n")
    status = lines[0] if lines else ""
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return status, headers, leftover


def build_ws_frame_text(payload_text):
    payload = payload_text.encode("utf-8")
    first = 0x81
    ln = len(payload)
    if ln <= 125:
        header = bytes([first, 0x80 | ln])
    elif ln <= 0xFFFF:
        header = bytes([first, 0x80 | 126]) + ln.to_bytes(2, "big")
    else:
        header = bytes([first, 0x80 | 127]) + ln.to_bytes(8, "big")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


def parse_ws_frames(buf):
    messages = []
    pos = 0
    while pos + 2 <= len(buf):
        b0 = buf[pos]
        b1 = buf[pos + 1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        plen = b1 & 0x7F
        pos += 2
        if plen == 126:
            if pos + 2 > len(buf):
                break
            plen = int.from_bytes(buf[pos:pos + 2], "big")
            pos += 2
        elif plen == 127:
            if pos + 8 > len(buf):
                break
            plen = int.from_bytes(buf[pos:pos + 8], "big")
            pos += 8
        if masked:
            if pos + 4 > len(buf):
                break
            mask = buf[pos:pos + 4]
            pos += 4
        else:
            mask = None
        if pos + plen > len(buf):
            break
        payload = buf[pos:pos + plen]
        pos += plen
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x1:
            messages.append(("text", payload.decode("utf-8", "ignore")))
        elif opcode == 0x8:
            messages.append(("close", payload.hex()))
        elif opcode == 0x9:
            messages.append(("ping", payload.hex()))
        elif opcode == 0xA:
            messages.append(("pong", payload.hex()))
        else:
            messages.append((f"opcode_{opcode}", payload.hex()))
    return messages, buf[pos:]


def connect_and_probe(host, path, token, include_auth_header, include_query_token, timeout=10.0):
    query = "?token={}".format(token) if include_query_token else ""
    request_path = path + query
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
    ).decode("ascii")

    raw = socket.create_connection((host, 443), timeout=timeout)
    tls = ssl.create_default_context().wrap_socket(raw, server_hostname=host)

    headers = [
        "GET {} HTTP/1.1".format(request_path),
        "Host: {}".format(host),
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: {}".format(key),
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Protocol: actioncable-v1-json",
        "Origin: https://{}".format(host),
        "Cache-Control: no-cache",
        "Pragma: no-cache",
    ]
    if include_auth_header:
        headers.append("Authorization: Bearer {}".format(token))
    request = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
    tls.sendall(request)

    raw_headers = recv_until(tls, b"\r\n\r\n", timeout=timeout)
    status, response_headers, leftover = parse_headers(raw_headers)
    accept_ok = response_headers.get("sec-websocket-accept", "") == expected_accept

    result = {
        "status": status,
        "accept_ok": accept_ok,
        "headers": response_headers,
        "request_path": request_path,
        "messages_before_subscribe": [],
        "messages_after_subscribe": [],
    }

    buf = leftover
    if buf:
        msgs, buf = parse_ws_frames(buf)
        result["messages_before_subscribe"].extend(msgs)

    ident = json.dumps({"channel": "DeviceCommandsChannel"}, separators=(",", ":"))
    subscribe = json.dumps(
        {"command": "subscribe", "identifier": ident},
        separators=(",", ":"),
    )
    try:
        tls.sendall(build_ws_frame_text(subscribe))
    except Exception as exc:
        result["subscribe_send_error"] = str(exc)
        tls.close()
        return result

    try:
        tls.settimeout(timeout)
        while True:
            chunk = tls.recv(4096)
            if not chunk:
                break
            buf += chunk
            msgs, buf = parse_ws_frames(buf)
            result["messages_after_subscribe"].extend(msgs)
            text_msgs = [m for m in result["messages_after_subscribe"] if m[0] == "text"]
            if text_msgs:
                parsed = []
                for _, txt in text_msgs:
                    try:
                        parsed.append(json.loads(txt))
                    except Exception:
                        continue
                if any(obj.get("type") in ("confirm_subscription", "reject_subscription", "disconnect") for obj in parsed):
                    break
                if len(result["messages_after_subscribe"]) >= 4:
                    break
    except socket.timeout:
        result["timed_out"] = True
    finally:
        try:
            tls.close()
        except Exception:
            pass
    return result


def print_result(name, result):
    print("\n=== {} ===".format(name))
    print("status:", result.get("status"))
    print("request_path:", result.get("request_path"))
    print("sec-websocket-accept ok:", result.get("accept_ok"))
    if result.get("subscribe_send_error"):
        print("subscribe_send_error:", result["subscribe_send_error"])
    if result.get("messages_before_subscribe"):
        print("before_subscribe:")
        for kind, payload in result["messages_before_subscribe"]:
            print(" -", kind, payload)
    if result.get("messages_after_subscribe"):
        print("after_subscribe:")
        for kind, payload in result["messages_after_subscribe"]:
            print(" -", kind, payload)
    if result.get("timed_out"):
        print("timed_out: True")


def main():
    parser = argparse.ArgumentParser(description="Prueba el WebSocket ActionCable del backend Infrakey.")
    parser.add_argument("--token", help="auth_token a usar. Si no se indica, se lee desde token.json")
    parser.add_argument("--token-file", default="token.json", help="ruta a token.json")
    parser.add_argument("--host", default="infrakey.fasttrack.cloud", help="host del WebSocket")
    parser.add_argument("--path", default="/cable", help="path del WebSocket")
    parser.add_argument("--timeout", type=float, default=8.0, help="timeout por fase en segundos")
    args = parser.parse_args()

    if args.token:
        token = args.token
        device_id = None
    else:
        token, device_id = load_token(args.token_file)

    print("host:", args.host)
    print("path:", args.path)
    print("device_id:", device_id)
    print("token_prefix:", token[:8] + "..." if len(token) > 8 else token)

    cases = [
        ("auth_header_only", True, False),
        ("query_token_only", False, True),
        ("auth_header_and_query_token", True, True),
    ]
    for name, include_auth_header, include_query_token in cases:
        result = connect_and_probe(
            host=args.host,
            path=args.path,
            token=token,
            include_auth_header=include_auth_header,
            include_query_token=include_query_token,
            timeout=args.timeout,
        )
        print_result(name, result)


if __name__ == "__main__":
    main()
