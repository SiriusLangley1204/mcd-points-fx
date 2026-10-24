"""麦当劳 MCP 客户端 —— 纯标准库实现，零第三方依赖。

设计约束（为什么这么写）：
1. 不引 requests / httpx：参赛项目要让人 clone 下来就能跑，
   环境越轻，Star 转化率越高。
2. 麦当劳 MCP 是无状态的 Streamable HTTP：每次 POST 一个 JSON-RPC，
   服务端可能返回 application/json，也可能返回 text/event-stream（SSE），
   两种情况都要能解析，否则接入方换个网络环境就挂。
3. 工具返回值是「人类可读说明 + 原始 JSON」混排的 markdown，
   必须自己从里面把 JSON 抠出来，不能当纯 JSON 解析。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://mcp.mcd.cn"


class McpError(RuntimeError):
    """MCP 调用失败（鉴权、限流、业务错误统一收敛到这里）。"""


class McdMcpClient:
    """麦当劳 MCP 的薄封装。

    用法::

        with McdMcpClient() as mcd:
            account = mcd.call("query-my-account")
    """

    def __init__(self, token: str | None = None, url: str | None = None,
                 timeout: float = 40.0, max_retry: int = 2):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "").strip()
        self.url = url or os.environ.get("MCD_MCP_URL", DEFAULT_URL).strip()
        self.timeout = timeout
        self.max_retry = max_retry
        self._id = 0
        if not self.token:
            raise McpError(
                "缺少 MCD_MCP_TOKEN。请先到 https://open.mcd.cn 控制台申请，"
                "然后写入环境变量（或项目根目录 .env）后再运行。"
            )

    # ---------------- 底层传输 ----------------

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "mcd-points-fx/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 401:
                raise McpError("MCP Token 无效或已过期（401），请重新申请。") from exc
            if code == 429:
                raise McpError("触发限流（429，600 次/分钟），请降低调用频率。") from exc
            raise McpError(f"HTTP {code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"网络不可达: {exc.reason}") from exc
        return self._parse_transport(raw)

    @staticmethod
    def _parse_transport(raw: str) -> dict:
        """兼容裸 JSON 与 SSE 两种响应体。"""
        head = raw.lstrip()
        if head.startswith("{"):
            return json.loads(head)
        # SSE: 取最后一条 data 行（服务端可能先发 ping）
        payloads = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        for body in reversed(payloads):
            if body and body != "[DONE]":
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    continue
        raise McpError(f"无法解析的响应体（前 200 字符）: {raw[:200]}")

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        last_exc: Exception | None = None
        for attempt in range(self.max_retry + 1):
            try:
                return self._post(payload)
            except McpError as exc:
                last_exc = exc
                if "429" in str(exc) and attempt < self.max_retry:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # pragma: no cover

    # ---------------- 协议层 ----------------

    def initialize(self) -> dict:
        return self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcd-points-fx", "version": "1.0"},
        })

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list", {}).get("result", {}).get("tools", [])

    def call(self, name: str, arguments: dict | None = None):
        """调用工具，返回解析后的业务数据。

        - 工具返回里能抠出 JSON 就返回 dict / list
        - 抠不出（纯文本类工具）就返回 {"_text": 原文}
        - 业务层 success=false 也抛 McpError，避免脏数据往下游流
        """
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in res:
            err = res["error"]
            raise McpError(f"{name} 调用失败: {err.get('message', err)}")

        content = res.get("result", {}).get("content") or []
        chunks = [c.get("text", "") for c in content if c.get("type") == "text"]
        text = "\n".join(chunks)
        data = extract_json(text)
        if isinstance(data, dict) and data.get("success") is False:
            raise McpError(f"{name} 业务失败: {data.get('message')}")
        return data if data is not None else {"_text": text}

    # ---------------- 便捷方法 ----------------

    def account(self) -> dict:
        return self.call("query-my-account").get("data", {}) or {}

    def my_coupons(self) -> list[dict]:
        """券列表接口只返回渲染好的 markdown，这里退化为摘要。"""
        raw = self.call("query-my-coupons")
        text = raw.get("_text", "")
        return parse_coupon_text(text)

    def mall_products(self) -> list[dict]:
        raw = self.call("mall-points-products")
        if isinstance(raw, dict) and "data" in raw:
            return raw["data"] or []
        if isinstance(raw, list):
            return raw
        return []

    def nearby_stores(self, city: str = "上海", keyword: str = "",
                      be_type: int = 1) -> list[dict]:
        raw = self.call("query-nearby-stores", {
            "beType": be_type, "searchType": 2, "city": city, "keyword": keyword,
        })
        if isinstance(raw, dict) and "data" in raw:
            return raw["data"] or []
        return []

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *exc):
        return False


# ---------------- 解析工具 ----------------

_JSON_START = re.compile(r'\{"success"')


def extract_json(text: str):
    """从「说明文档 + 原始 JSON」混排文本里抠出那段 JSON。"""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # 优先找 {"success"...} 这种业务响应
    for m in reversed(list(_JSON_START.finditer(text))):
        candidate = text[m.start():].strip()
        # 去掉可能尾随的 markdown 围栏
        candidate = candidate.rstrip().rstrip("`").rstrip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 逐字符回退，找到可闭合的位置
            depth, in_str, esc = 0, False, False
            for i, ch in enumerate(candidate):
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[: i + 1])
                        except json.JSONDecodeError:
                            break
    return None


_COUPON_BLOCK = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.M)


def parse_coupon_text(text: str) -> list[dict]:
    """query-my-coupons 返回的是渲染好的 markdown，这里做最小解析。"""
    if not text:
        return []
    coupons: list[dict] = []
    blocks = re.split(r"\n##\s+", text)
    for block in blocks[1:]:
        lines = block.splitlines()
        title = lines[0].strip()
        item = {"title": title, "price": None, "valid": None, "tags": []}
        for line in lines[1:]:
            s = line.strip()
            if s.startswith("- **优惠**"):
                m = re.search(r"¥\s*([\d.]+)", s)
                if m:
                    item["price"] = float(m.group(1))
            elif s.startswith("- **有效期**"):
                item["valid"] = s.split("**", 2)[-1].lstrip("：: ").strip()
            elif s.startswith("- **标签**"):
                item["tags"] = [t.strip() for t in s.split("**", 2)[-1]
                                .lstrip("：: ").split("、") if t.strip()]
        if title:
            coupons.append(item)
    return coupons
