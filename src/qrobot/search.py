from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchUnavailable(RuntimeError):
    pass


_CURRENT_INFO_PATTERNS = (
    re.compile(
        r"(?:今天|明天|刚刚|目前|现在|当前|最近|最新|实时|本周|本月|今年|新闻|天气|气温|"
        r"价格|票价|汇率|股价|比分|赛程|排名|版本|发布|更新|政策|公告|热搜)"
    ),
    re.compile(r"(?:搜一下|搜索|查一下|查查|联网查|帮我查|web\s*search)", re.I),
)


def should_search(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CURRENT_INFO_PATTERNS)


class WebSearcher:
    def __init__(self, max_results: int = 5, timeout_seconds: float = 15.0) -> None:
        self.max_results = max(1, min(max_results, 20))
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str) -> list[SearchResult]:
        query = _clean_query(query)
        if not query:
            return []
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._search_sync, query), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise SearchUnavailable("联网搜索超时") from exc

    def _search_sync(self, query: str) -> list[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise SearchUnavailable("未安装 ddgs，请执行 pip install -e '.[search]'") from exc
        try:
            raw_results: list[dict[str, Any]] = list(
                DDGS().text(query, max_results=self.max_results)
            )
        except Exception as exc:
            raise SearchUnavailable(f"联网搜索失败: {exc}") from exc
        results: list[SearchResult] = []
        for item in raw_results:
            title = str(item.get("title") or "无标题").strip()[:200]
            url = str(item.get("href") or item.get("url") or "").strip()[:1000]
            snippet = str(item.get("body") or item.get("snippet") or "").strip()[:600]
            if url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))
        return results


def format_search_context(results: list[SearchResult]) -> str:
    if not results:
        return ""
    lines = ["以下是刚刚获得的公开网页搜索结果。它们可能不准确，请交叉判断并在回复中附链接："]
    for index, result in enumerate(results, 1):
        lines.append(f"{index}. {result.title}\n   {result.snippet}\n   来源: {result.url}")
    return "\n".join(lines)


def _clean_query(value: str) -> str:
    value = re.sub(r"^(?:请|麻烦)?(?:帮我)?(?:搜一下|搜索|查一下|查查|联网查)\s*[：:]?", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:300]
