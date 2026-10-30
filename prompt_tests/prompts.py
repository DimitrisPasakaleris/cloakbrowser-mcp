"""Ten in-depth test prompts for the CloakBrowser MCP, each with checks that decide pass or fail.

A prompt is written for an LLM agent that has the cloakbrowser tools. It ends with a fixed answer format so the answer can be
parsed, and each check is verified against GROUND TRUTH where possible (what the tools returned, the usage log, the memory
database), not only against what the agent says. {base} is replaced with the local fixture server's URL.

Run the suite: python prompt_tests/run_prompts.py        Print the prompts to paste by hand: --markdown
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------- what a check can look at
@dataclass
class Ctx:
    answer: str = ""
    calls: list = field(default_factory=list)  # [{"tool", "arguments", "result"}] for the cloakbrowser tools, in order
    usage: list = field(default_factory=list)  # rows of the server's usage log during this prompt
    mem: object = None  # a Memory on this prompt's home (read-only use)
    home: Path | None = None
    base: str = ""
    truth: dict = field(default_factory=dict)

    # -- helpers
    def field(self, key: str) -> str:
        m = re.search(rf"(?im)^[\s>*`_-]*{re.escape(key)}\s*[=:]\s*(.+?)\s*$", self.answer)
        return m.group(1).strip().strip("`*") if m else ""

    def tools(self, *names) -> list:
        return [c for c in self.calls if c["tool"] in names]

    def results(self, *names) -> list[str]:
        return [c["result"] for c in (self.tools(*names) if names else self.calls)]

    def any_result_has(self, needle: str, *names) -> bool:
        return any(needle in r for r in self.results(*names))

    def sql(self, query: str, *args) -> list[dict]:
        if self.mem is None or not self.mem.db_path.exists():
            return []
        return [dict(r) for r in self.mem.store().conn().execute(query, args).fetchall()]


Check = tuple  # (label, passed, detail)


def ok(label, cond, detail="") -> Check:
    return (label, bool(cond), str(detail)[:160])


@dataclass
class Prompt:
    id: int
    title: str
    tests: str  # which feature(s) this exercises
    text: str
    check: Callable[[Ctx], list]
    fixtures: bool = False  # needs the local fixture server (and CLOAK_ALLOW_PRIVATE / CLOAK_MEMORY_ALLOW_IP)
    needs_net: tuple = ()  # public hosts that must be reachable
    success: str = ""  # human-readable pass criteria


def _lines_with(answer: str, *needles) -> bool:
    return any(all(n.lower() in ln.lower() for n in needles) for ln in answer.splitlines())


# ================================================================ 1
def check_1(c: Ctx):
    rd = c.tools("read_url")
    return [
        ok("book 1 title+price", _lines_with(c.answer, "A Light in the", "51.77"), c.answer[:120]),
        ok("book 2 title+price", _lines_with(c.answer, "Tipping the Velvet", "53.74")),
        ok("book 3 title+price", _lines_with(c.answer, "Soumission", "50.10")),
        ok("used read_url (open + read in one call)", len(rd) >= 1, [x["tool"] for x in c.calls]),
        ok("at most 3 cloakbrowser calls", len(c.calls) <= 3, len(c.calls)),
        ok("content came back wrapped as untrusted page data", any("<untrusted_page_content>" in r for r in c.results("read_url"))),
    ]


# ================================================================ 2
def check_2(c: Ctx):
    searched = c.tools("find_in_page") or [x for x in c.tools("read_url") if x["arguments"].get("query")]
    tags = c.field("TAGS").lower()
    biggest = max((len(r) for r in c.results()), default=0)
    return [
        ok("author is Albert Einstein", "einstein" in c.field("AUTHOR").lower(), c.field("AUTHOR")),
        ok("all four tags", all(t in tags for t in ("change", "deep-thoughts", "thinking", "world")), c.field("TAGS")),
        ok("searched the page instead of reading it all", len(searched) >= 1, [x["tool"] for x in c.calls]),
        ok("no tool result larger than 3000 chars", biggest <= 3000, biggest),
    ]


# ================================================================ 3
def check_3(c: Ctx):
    rs = c.tools("run_steps")
    errs = [r for r in c.results("run_steps") if re.search(r"^\d+ \w+: ERROR", r, re.M)]
    return [
        ok("agent says logged in", c.field("LOGGED_IN").lower().startswith("yes"), c.field("LOGGED_IN")),
        ok("used run_steps to batch the form", len(rs) >= 1, [x["tool"] for x in c.calls]),
        ok("run_steps reported no step errors", rs and not errs, errs[:1]),
        ok("the page really shows Logout (ground truth)", c.any_result_has("Logout")),
        ok("at most 4 cloakbrowser calls", len(c.calls) <= 4, len(c.calls)),
    ]


# ================================================================ 4
def check_4(c: Ctx):
    rows = c.sql("SELECT * FROM extractors")
    imp = c.sql("SELECT * FROM impact WHERE kind='extractor'")
    used = c.any_result_has("learned extractor, 20 items", "read_url")
    return [
        ok("20 items", re.search(r"\b20\b", c.field("ITEMS")), c.field("ITEMS")),
        ok("first title", "a light in the attic" in c.field("FIRST").lower(), c.field("FIRST")),
        ok("recipe stored in memory after >= 2 successes", rows and rows[0]["ok"] >= 1.9, rows[:1]),
        ok("read_url really used the learned recipe (ground truth)", used),
        ok("agent reported that correctly", c.field("LEARNED").lower().startswith("yes") == used, c.field("LEARNED")),
        ok("memory counted the application", bool(imp) and imp[0]["applied"] >= 1, imp),
    ]


# ================================================================ 5
def check_5(c: Ctx):
    n = re.search(r"\d+", c.field("QUOTES"))
    scrolled = c.tools("scroll_to_load", "scroll") or [x for x in c.tools("run_steps") if "scroll" in json.dumps(x["arguments"])]
    return [
        ok("loaded at least 30 quotes", n and int(n.group()) >= 30, c.field("QUOTES")),
        ok("25th quote's author (ground truth: Jim Henson)", "jim henson" in c.field("AUTHOR25").lower(), c.field("AUTHOR25")),
        ok("scrolled to trigger the loading", len(scrolled) >= 1, [x["tool"] for x in c.calls]),
    ]


# ================================================================ 6
def check_6(c: Ctx):
    offs = [x["arguments"].get("offset") for x in c.calls if x["tool"] in ("get_markdown", "read_url", "get_text")]
    truth = " ".join(c.truth.get("md", "")[6000:6400].split())
    got = " ".join(c.field("CHUNK3").split())
    cut = [r for r in c.results("snapshot") if "list cut at 10" in r]
    return [
        ok("OFFSET1 = 3000", c.field("OFFSET1").strip().startswith("3000"), c.field("OFFSET1")),
        ok("paged with offset=3000 and offset=6000 (no re-reading from the start)", 3000 in offs and 6000 in offs, offs),
        ok("chunk 3 text matches the page (ground truth)", len(got) > 8 and truth.lower().startswith(got.lower()), f"{got!r} vs {truth[:40]!r}"),
        ok("snapshot said the list was cut off", len(cut) >= 1 and "list cut at 10" in c.field("CUTNOTE"), c.field("CUTNOTE")),
    ]


# ================================================================ 7
def check_7(c: Ctx):
    inline = [u for u in c.usage if u["tool"] == "screenshot"]
    path = Path(c.field("FILE").strip("`'\" "))
    width = 0
    if path.is_file():
        from cloakbrowser_mcp import server as s  # only for the image-size reader

        width = s._image_size(path.read_bytes())[0]
    return [
        ok("took an inline screenshot", len(inline) >= 1, [u["tool"] for u in c.usage]),
        ok("inline image is scaled down (<= 800 estimated tokens; full size is ~1600)", inline and inline[0]["tokens"] <= 800, inline[:1]),
        ok("saved a file with screenshot_file", len(c.tools("screenshot_file")) >= 1),
        ok("the saved file exists and is full size (>= 1500px wide)", width >= 1500, f"{path} width={width}"),
        ok("described the colors", len(c.field("SCHEME")) > 10, c.field("SCHEME")),
    ]


# ================================================================ 8
def check_8(c: Ctx):
    rows = c.sql("SELECT * FROM overlays")
    clicks = c.tools("click") or c.tools("run_steps")
    nav_notes = [r for r in c.results("navigate", "read_url") if "dismissed 1 remembered pop-up" in r]
    return [
        ok("saw the banner on the first visit", c.field("FIRST_BANNER_PRESENT").lower().startswith("yes"), c.field("FIRST_BANNER_PRESENT")),
        ok("clicked the Accept button", len(clicks) >= 1, [x["tool"] for x in c.calls]),
        ok("memory learned the dismisser (selector #accept-cookies, active)", rows and rows[0]["selector"] == "#accept-cookies" and rows[0]["state"] == "active", rows[:1]),
        ok("second visit dismissed it automatically (ground truth)", len(nav_notes) >= 1),
        ok("agent quoted the memory note", "dismissed 1 remembered pop-up" in c.field("SECOND_NOTE"), c.field("SECOND_NOTE")),
        ok("the Accept selector is not exposed in any tool output", not c.any_result_has("#accept-cookies", "navigate", "read_url")),
    ]


# ================================================================ 9
def check_9(c: Ctx):
    navs = [x["result"] for x in c.tools("navigate") if "/challenge" in x["arguments"].get("url", "")]
    pb = {r["arm"]: r for r in c.sql("SELECT * FROM playbook")}
    return [
        ok("three visits", len(navs) == 3, len(navs)),
        ok("visits 1 and 2: no strategy change", len(navs) == 3 and all("load strategy" not in n for n in navs[:2])),
        ok("visit 3: escalated to load+1500ms (ground truth)", len(navs) == 3 and "learned load strategy load+1500ms" in navs[2], navs[2][-120:] if len(navs) == 3 else ""),
        ok("blocks were detected each time", all("BLOCKED?" in n for n in navs)),
        ok("playbook: arm 0 failed twice and is benched", 0 in pb and pb[0]["streak_fail"] >= 2 and pb[0]["demoted_until"] > 0, pb.get(0)),
        ok("agent reported the third note", "load+1500ms" in c.field("V3"), c.field("V3")),
    ]


# ================================================================ 10
def check_10(c: Ctx):
    def first_result(fragment):
        return next((x["result"] for x in c.calls if fragment in x["arguments"].get("url", "")), "")

    dump = json.dumps(c.mem.store().dump(), default=str).lower() if c.mem is not None and c.mem.db_path.exists() else ""
    paypal = first_result("paypal.com")
    status = re.search(r"status=(\d+)", paypal)
    return [
        ok("file:// blocked by the tool's policy (ground truth)", first_result("file:").startswith("ERROR"), first_result("file:")[:80]),
        ok("localhost blocked by the tool's policy (ground truth)", first_result("127.0.0.1").startswith("ERROR"), first_result("127.0.0.1")[:80]),
        ok("cloud metadata address blocked by the tool's policy (ground truth)", first_result("169.254").startswith("ERROR"), first_result("169.254")[:80]),
        ok("a public site is NOT blocked by the policy: the navigation went through (ground truth)", bool(paypal) and not paypal.startswith("ERROR"), paypal[:80]),
        ok("agent answers A, B, C = blocked", all(c.field(k).lower().startswith("blocked") for k in "ABC"), [c.field(k) for k in "ABC"]),
        ok("agent answers D = allowed", c.field("D").lower().startswith("allowed"), c.field("D")),
        ok("agent reported the website's own HTTP status correctly", status and status.group(1) in c.field("D_STATUS"), f"{c.field('D_STATUS')!r} vs {status.group(1) if status else None}"),
        ok("memory kept nothing about paypal (sensitive site)", "paypal" not in dump, "found" if "paypal" in dump else "clean"),
    ]


FORMAT_NOTE = "Use the cloakbrowser MCP tools. Do not use any other browser or web tool. "

PROMPTS = [
    Prompt(
        1, "Open and read in one call", "read_url, clean main-content markdown, call economy",
        FORMAT_NOTE + "Open https://books.toscrape.com/ and read it using as few tool calls as possible (ideally one). "
        "Then tell me the titles and prices of the first three books on the page. Reply with exactly three lines in the form "
        "`TITLE | PRICE`, nothing else.",
        check_1, needs_net=("books.toscrape.com",),
        success="three correct title|price lines (A Light in the Attic 51.77, Tipping the Velvet 53.74, Soumission 50.10); read_url used; <= 3 tool calls",
    ),
    Prompt(
        2, "Search a page instead of reading it", "find_in_page / read_url(query), small outputs",
        FORMAT_NOTE + "On https://quotes.toscrape.com/ find the quote by Albert Einstein that begins \"The world as we have created it\". "
        "Do NOT read the whole page into your context: use the tool's search-in-page capability. "
        "Reply with two lines: `AUTHOR=<name>` and `TAGS=<comma separated tags>`.",
        check_2, needs_net=("quotes.toscrape.com",),
        success="AUTHOR=Albert Einstein, TAGS=change, deep-thoughts, thinking, world; search used; no tool result over 3000 chars",
    ),
    Prompt(
        3, "Log in with one batched call", "run_steps (type, type, click, wait), then verify",
        FORMAT_NOTE + "Log in at https://quotes.toscrape.com/login with username `admin` and password `admin`. Do the typing and the click "
        "as a single batched tool call if the tools allow it, and make the same call (or one more) show the page afterwards so you can "
        "confirm that a Logout link is present. Reply with `LOGGED_IN=yes` or `LOGGED_IN=no`.",
        check_3, needs_net=("quotes.toscrape.com",),
        success="LOGGED_IN=yes; run_steps used with no step errors; a result shows Logout; <= 4 tool calls",
    ),
    Prompt(
        4, "Learn an extractor, then reuse it", "extract_structured recording, learned recipe in read_url, memory impact",
        FORMAT_NOTE + "Step 1: open https://books.toscrape.com/catalogue/page-1.html and use the extract_structured tool with item_selector "
        "`article.product_pod` and fields `{\"title\": \"h3 a@title\", \"price\": \".price_color\"}`. Step 2: run exactly the same extraction a "
        "second time to confirm it. Step 3: call read_url on the same page URL and look at what came back. Reply with three lines: "
        "`ITEMS=<number of books extracted>`, `FIRST=<title of the first book>`, and `LEARNED=yes` if the read_url result starts with a "
        "section called \"learned extractor\" (otherwise `LEARNED=no`).",
        check_4, needs_net=("books.toscrape.com",),
        success="ITEMS=20, FIRST=A Light in the Attic, LEARNED=yes and a real 'learned extractor, 20 items' result; memory stored the recipe and counted the application",
    ),
    Prompt(
        5, "Infinite scroll", "scroll_to_load / scroll, JS-loaded content, correctness under load",
        FORMAT_NOTE + "Open https://quotes.toscrape.com/scroll . It loads more quotes as you scroll. Keep scrolling until at least 30 quotes are on "
        "the page. Reply with `QUOTES=<how many quotes are loaded>` and `AUTHOR25=<author of the 25th quote on the page>`.",
        check_5, needs_net=("quotes.toscrape.com",),
        success="QUOTES >= 30 and AUTHOR25 = Jim Henson; a scroll tool was used",
    ),
    Prompt(
        6, "Paging and truncation notes", "offset paging on a long page, snapshot 'list cut' note",
        FORMAT_NOTE + "Read https://en.wikipedia.org/wiki/Vinci_SA as markdown in chunks of 3000 characters: first chunk (offset 0), then "
        "continue with the paging offset the tool gives you (do not re-read from the start) for a second and a third chunk. "
        "Then take a snapshot of the page limited to 10 elements and copy the note the tool adds when the list is cut off. "
        "Reply with three lines: `OFFSET1=<the offset the tool told you to use after chunk 1>`, `CHUNK3=<the first 5 words of chunk 3, exactly as shown>`, "
        "`CUTNOTE=<the note text>`.",
        check_6, needs_net=("en.wikipedia.org",),
        success="OFFSET1=3000; calls with offset=3000 and offset=6000; CHUNK3 matches the real text at offset 6000; CUTNOTE contains 'list cut at 10'",
    ),
    Prompt(
        7, "Cheap screenshots", "scaled JPEG screenshots vs full-size file, token estimate in the usage log",
        FORMAT_NOTE + "Open https://books.toscrape.com/ . Take a normal screenshot with the screenshot tool and describe the page's color scheme "
        "in one sentence. Then save a full-size screenshot to a file called `bench.png` with the file-saving screenshot tool. "
        "Reply with `SCHEME=<your sentence>` and `FILE=<the file path the tool returned>`.",
        check_7, needs_net=("books.toscrape.com",),
        success="inline screenshot estimated <= 800 tokens (scaled to 1024px); saved file exists and is >= 1500px wide; color scheme described",
    ),
    Prompt(
        8, "Learn a cookie banner, then skip it", "click learning, auto-dismiss on the next visit, no selector leakage",
        FORMAT_NOTE + "Open {base}/banner.html . A cookie banner covers the bottom of the page. Dismiss it by clicking its \"Accept all\" button "
        "(use a click tool, not JavaScript). Then open {base}/banner.html again with the navigate tool and read the whole tool result "
        "carefully. Reply with `FIRST_BANNER_PRESENT=yes|no` (was the banner there on the first visit?) and `SECOND_NOTE=<the part of the second "
        "navigate result that starts with \"memory:\" or says anything about pop-ups, quoted exactly>`.",
        check_8, fixtures=True,
        success="memory stores #accept-cookies as active; the second visit's result says 'dismissed 1 remembered pop-up(s)'; the selector never appears in tool output",
    ),
    Prompt(
        9, "Blocked site: the load strategy escalates", "load playbook, benching after two failures, escalation to load+1500ms",
        FORMAT_NOTE + "Using the navigate tool (not read_url), open {base}/challenge three times in a row. It is a bot-check test page that answers "
        "with an error; that is expected. After each visit copy anything the result says that starts with \"memory:\". "
        "Reply with `V1=<text or none>`, `V2=<text or none>`, `V3=<text or none>`.",
        check_9, fixtures=True,
        success="visits 1 and 2 show no strategy note; visit 3 says 'learned load strategy load+1500ms'; memory shows the default strategy benched",
    ),
    Prompt(
        10, "Safety limits and sensitive sites", "scheme and private-address blocking, metadata endpoint, memory skips sensitive sites",
        FORMAT_NOTE + "Try each of these with the navigate tool. For A to D, say whether the cloakbrowser tool's OWN safety policy allowed the "
        "navigation or blocked it (the tool answers with an ERROR); a website refusing you with an HTTP error does not count as blocked by the tool. "
        "Do not try to work around a block. (A) file:///C:/Windows/win.ini  (B) http://127.0.0.1:80/  (C) http://169.254.169.254/latest/meta-data/  "
        "(D) https://www.paypal.com/signin . Reply with five lines: `A=blocked|allowed`, `B=...`, `C=...`, `D=...`, and `D_STATUS=<the HTTP status code "
        "the PayPal site returned>`.",
        check_10, needs_net=("www.paypal.com",),
        success="A, B, C blocked by the tool (ERROR); D allowed by the tool (the site may still answer 403, which is reported as D_STATUS); memory holds nothing about paypal",
    ),
]


def markdown(base: str = "http://127.0.0.1:8765") -> str:
    out = ["# CloakBrowser MCP: 10 test prompts", "",
           "Paste each prompt into an agent that has the cloakbrowser MCP. Prompts 8 and 9 need the local fixture server "
           "(`python prompt_tests/serve_fixtures.py 8765`) and the MCP registered with `CLOAK_ALLOW_PRIVATE=1` and "
           "`CLOAK_MEMORY_ALLOW_IP=1`. Prompt 10 must run **without** `CLOAK_ALLOW_PRIVATE`. Use a fresh `CLOAK_MCP_HOME` per prompt "
           "if you want them independent. `python prompt_tests/run_prompts.py` automates all of this and checks the ground truth.", ""]
    for p in PROMPTS:
        out += [f"## {p.id}. {p.title}", f"*Tests:* {p.tests}" + (" *(needs the local fixture server)*" if p.fixtures else ""), "",
                "```text", p.text.replace("{base}", base), "```", f"**Success:** {p.success}", ""]
    return "\n".join(out)
