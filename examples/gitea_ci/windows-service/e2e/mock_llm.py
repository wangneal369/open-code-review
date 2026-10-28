# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""端到端测试用的模拟 LLM（Anthropic messages 协议），行为照搬
open-code-review/cmd/opencodereview/retry_fake_llm_test.go：
带 tools 的请求直接调用 task_done 结束，否则回一段计划文本。只用于验证链路，不产生评审意见。

用法：python mock_llm.py [端口，默认 8765] [每次回复前延迟秒数，默认 0]；请求计数写到 stdout。
延迟用于让多个评审在时间上重叠，以验证并发。
"""

import http.server
import json
import sys
import time

DONE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "mock",
        "content": [{"type": "tool_use", "id": "tu_1", "name": "task_done",
                     "input": {"state": "DONE"}}],
        "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}
PLAN = {"id": "msg_1", "type": "message", "role": "assistant", "model": "mock",
        "content": [{"type": "text", "text": "plan: read the diff, then call task_done"}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}


DELAY = 0


class Handler(http.server.BaseHTTPRequestHandler):
    count = 0

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Handler.count += 1
        n = Handler.count  # 延迟期间其他请求会改动计数，先记下本次序号
        time.sleep(DELAY)
        reply = DONE if b'"tools"' in raw else PLAN
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print("%s request %d %s -> %s" % (time.strftime("%H:%M:%S"), n, self.path,
                                        "task_done" if reply is DONE else "plan"), flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
