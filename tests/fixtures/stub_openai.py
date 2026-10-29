"""一个假的 OpenAI 兼容接口，用来测翻译链路。

为什么不在测试里连真接口：真接口要 key、要钱、会限流、会改返回格式。
测试要验的是**我们的代码**对不对——请求体构造、编号解析、错误码映射——
这些都和对方模型是谁无关。

返回的「译文」在原句前加 `中文：`，这样既像中文（能通过 ``is_chinese`` 检查），
长度又和原文接近（不会被长度预算判成超长而触发重译）。

单独运行可以给手工联调用：

    python tests/fixtures/stub_openai.py 8899
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_NUM = re.compile(r"^(\d{1,3})\.\s*(.+)$")


def fake_translate(user_prompt: str) -> str:
    """把 "1. hello" 变成 "1. 中文：hello"。

    单条重译的提示词不分编号（形如「只输出译文：\\n<原句>」），
    这种情况直接对最后一行加前缀。
    """
    out = []
    for line in user_prompt.splitlines():
        m = _NUM.match(line.strip())
        if m:
            out.append(f"{m.group(1)}. 中文：{m.group(2)}")
    if out:
        return "\n".join(out)
    tail = [ln for ln in user_prompt.splitlines() if ln.strip()]
    return f"中文：{tail[-1]}" if tail else ""


class handler(BaseHTTPRequestHandler):  # noqa: N801 - 沿用 http.server 的命名
    fail_with: int = 0        # 让测试能把状态码改成 401 / 429

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        if handler.fail_with:
            self.send_response(handler.fail_with)
            self.end_headers()
            self.wfile.write(b'{"error":"stub"}')
            return

        user = ""
        for msg in body.get("messages", []):
            if msg.get("role") == "user":
                user = msg.get("content", "")
        text = fake_translate(user)

        payload = json.dumps({
            "id": "stub",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass          # 别污染测试输出


def make_server(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve_in_thread(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = make_server(port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    srv = make_server(p)
    print(f"stub OpenAI 兼容接口：http://127.0.0.1:{p}/v1", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
