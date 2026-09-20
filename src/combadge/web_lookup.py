"""Bound web research per Live delegation, without giving the model a reset switch."""

import asyncio
import time
from dataclasses import dataclass, field

from combadge.browserbase import public_url

MAX_LOOKUP_ACTIONS = 8
MAX_LOOKUP_SEARCHES = 2
LOOKUP_SECONDS = 60


def terminal_text(value: str) -> str:
    return "".join(char if char.isprintable() else " " for char in value[:4096])


@dataclass
class Lookup:
    started: float = field(default_factory=time.monotonic)
    actions: int = 0
    searches: int = 0
    cache: dict = field(default_factory=dict)


class WebLookup:
    def __init__(self, client, report):
        self.client = client
        self.report = report
        self.lookups = {}

    async def execute(self, name, args, delegation_id):
        key = {
            "search_web": "query",
            "read_web_page": "url",
            "browse_web_page": "url",
            "follow_web_link": "link_id",
            "read_more_web_page": None,
        }[name]
        if set(args) != ({key} if key else set()) or (key and not isinstance(args[key], str)):
            raise ValueError("Unexpected or missing web tool arguments.")
        if key == "url":
            public_url(args[key])
        lookup = self.lookups.setdefault(delegation_id, Lookup())
        remaining = LOOKUP_SECONDS - (time.monotonic() - lookup.started)
        if remaining <= 0 or lookup.actions >= MAX_LOOKUP_ACTIONS:
            raise RuntimeError(
                "Lookup budget exhausted. Stop browsing this question and answer with the "
                "verified facts plus what remains unknown. "
                "Do not treat missing evidence as absence."
            )
        lookup.actions += 1
        signature = (name, " ".join(args.get(key, "").split()).casefold())
        # URLs are case-sensitive. Normalize only search terms.
        if name != "search_web":
            signature = (name, args.get(key, ""))
        if signature in lookup.cache:
            self.report("\nAlready checked; reusing this question's previous result.\n")
            result = {**lookup.cache[signature], "cached": True}
        else:
            if name == "search_web":
                if lookup.searches >= MAX_LOOKUP_SEARCHES:
                    raise RuntimeError(
                        "Search limit reached for this question. Do not rephrase again. "
                        "Read relevant existing URLs, use the rendered browser, "
                        "or state uncertainty."
                    )
                lookup.searches += 1
            try:
                async with asyncio.timeout(remaining):
                    result = await self._execute(name, args)
            except TimeoutError:
                raise RuntimeError(
                    "Lookup deadline reached. Stop browsing and report what could not be verified."
                ) from None
            # Navigation snapshots have page-specific IDs; never replay them from cache.
            if name in ("search_web", "read_web_page"):
                lookup.cache[signature] = dict(result)
        return {
            **result,
            "lookup_budget": {
                "actions_remaining": max(0, MAX_LOOKUP_ACTIONS - lookup.actions),
                "searches_remaining": max(0, MAX_LOOKUP_SEARCHES - lookup.searches),
                "seconds_remaining": max(
                    0, int(LOOKUP_SECONDS - (time.monotonic() - lookup.started))
                ),
            },
        }

    async def _execute(self, name, args):
        if name == "search_web":
            self.report(f"\nSearching the web: {terminal_text(args['query'])}\n")
            result = await self.client.search(args["query"])
            sources = result.get("sources", [])
            if sources:
                self.report("Search results (not yet read):\n")
                for index, source in enumerate(sources, start=1):
                    title = terminal_text(source.get("title") or "Untitled page")
                    self.report(f"  {index}. {title}\n     {terminal_text(source['url'])}\n")
            else:
                self.report("No matching sources found.\n")
        elif name == "read_web_page":
            self.report(f"\nOpening source: {terminal_text(args['url'])}\n")
            result = await self.client.read_page(args["url"])
        elif name == "browse_web_page":
            self.report(f"\nOpening in browser: {terminal_text(args['url'])}\n")
            result = await self.client.browse(args["url"])
        elif name == "follow_web_link":
            self.report(f"\nFollowing page link: {terminal_text(args['link_id'])}\n")
            result = await self.client.follow(args["link_id"])
        else:
            self.report("\nReading more of the current page…\n")
            result = await self.client.more()
        if name != "search_web" and result.get("status") == "ok":
            suffix = " (more text available)" if result.get("truncated") else ""
            url = terminal_text(result.get("url", args.get("url", "")))
            mode = "browser" if result.get("mode") == "rendered_browser" else "fetch"
            self.report(f"Read source [{mode}]: {url}{suffix}\n")
        return result
