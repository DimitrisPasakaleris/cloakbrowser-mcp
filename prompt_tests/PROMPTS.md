# CloakBrowser MCP: 10 test prompts

Paste each prompt into an agent that has the cloakbrowser MCP. Prompts 8 and 9 need the local fixture server (`python prompt_tests/serve_fixtures.py 8765`) and the MCP registered with `CLOAK_ALLOW_PRIVATE=1` and `CLOAK_MEMORY_ALLOW_IP=1`. Prompt 10 must run **without** `CLOAK_ALLOW_PRIVATE`. Use a fresh `CLOAK_MCP_HOME` per prompt if you want them independent. `python prompt_tests/run_prompts.py` automates all of this and checks the ground truth.

## 1. Open and read in one call
*Tests:* read_url, clean main-content markdown, call economy

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Open https://books.toscrape.com/ and read it using as few tool calls as possible (ideally one). Then tell me the titles and prices of the first three books on the page. Reply with exactly three lines in the form `TITLE | PRICE`, nothing else.
```
**Success:** three correct title|price lines (A Light in the Attic 51.77, Tipping the Velvet 53.74, Soumission 50.10); read_url used; <= 3 tool calls

## 2. Search a page instead of reading it
*Tests:* find_in_page / read_url(query), small outputs

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. On https://quotes.toscrape.com/ find the quote by Albert Einstein that begins "The world as we have created it". Do NOT read the whole page into your context: use the tool's search-in-page capability. Reply with two lines: `AUTHOR=<name>` and `TAGS=<comma separated tags>`.
```
**Success:** AUTHOR=Albert Einstein, TAGS=change, deep-thoughts, thinking, world; search used; no tool result over 3000 chars

## 3. Log in with one batched call
*Tests:* run_steps (type, type, click, wait), then verify

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Log in at https://quotes.toscrape.com/login with username `admin` and password `admin`. Do the typing and the click as a single batched tool call if the tools allow it, and make the same call (or one more) show the page afterwards so you can confirm that a Logout link is present. Reply with `LOGGED_IN=yes` or `LOGGED_IN=no`.
```
**Success:** LOGGED_IN=yes; run_steps used with no step errors; a result shows Logout; <= 4 tool calls

## 4. Learn an extractor, then reuse it
*Tests:* extract_structured recording, learned recipe in read_url, memory impact

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Step 1: open https://books.toscrape.com/catalogue/page-1.html and use the extract_structured tool with item_selector `article.product_pod` and fields `{"title": "h3 a@title", "price": ".price_color"}`. Step 2: run exactly the same extraction a second time to confirm it. Step 3: call read_url on the same page URL and look at what came back. Reply with three lines: `ITEMS=<number of books extracted>`, `FIRST=<title of the first book>`, and `LEARNED=yes` if the read_url result starts with a section called "learned extractor" (otherwise `LEARNED=no`).
```
**Success:** ITEMS=20, FIRST=A Light in the Attic, LEARNED=yes and a real 'learned extractor, 20 items' result; memory stored the recipe and counted the application

## 5. Infinite scroll
*Tests:* scroll_to_load / scroll, JS-loaded content, correctness under load

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Open https://quotes.toscrape.com/scroll . It loads more quotes as you scroll. Keep scrolling until at least 30 quotes are on the page. Reply with `QUOTES=<how many quotes are loaded>` and `AUTHOR25=<author of the 25th quote on the page>`.
```
**Success:** QUOTES >= 30 and AUTHOR25 = Jim Henson; a scroll tool was used

## 6. Paging and truncation notes
*Tests:* offset paging on a long page, snapshot 'list cut' note

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Read https://en.wikipedia.org/wiki/Vinci_SA as markdown in chunks of 3000 characters: first chunk (offset 0), then continue with the paging offset the tool gives you (do not re-read from the start) for a second and a third chunk. Then take a snapshot of the page limited to 10 elements and copy the note the tool adds when the list is cut off. Reply with three lines: `OFFSET1=<the offset the tool told you to use after chunk 1>`, `CHUNK3=<the first 5 words of chunk 3, exactly as shown>`, `CUTNOTE=<the note text>`.
```
**Success:** OFFSET1=3000; calls with offset=3000 and offset=6000; CHUNK3 matches the real text at offset 6000; CUTNOTE contains 'list cut at 10'

## 7. Cheap screenshots
*Tests:* scaled JPEG screenshots vs full-size file, token estimate in the usage log

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Open https://books.toscrape.com/ . Take a normal screenshot with the screenshot tool and describe the page's color scheme in one sentence. Then save a full-size screenshot to a file called `bench.png` with the file-saving screenshot tool. Reply with `SCHEME=<your sentence>` and `FILE=<the file path the tool returned>`.
```
**Success:** inline screenshot estimated <= 800 tokens (scaled to 1024px); saved file exists and is >= 1500px wide; color scheme described

## 8. Learn a cookie banner, then skip it
*Tests:* click learning, auto-dismiss on the next visit, no selector leakage *(needs the local fixture server)*

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Open http://127.0.0.1:8765/banner.html . A cookie banner covers the bottom of the page. Dismiss it by clicking its "Accept all" button (use a click tool, not JavaScript). Then open http://127.0.0.1:8765/banner.html again with the navigate tool and read the whole tool result carefully. Reply with `FIRST_BANNER_PRESENT=yes|no` (was the banner there on the first visit?) and `SECOND_NOTE=<the part of the second navigate result that starts with "memory:" or says anything about pop-ups, quoted exactly>`.
```
**Success:** memory stores #accept-cookies as active; the second visit's result says 'dismissed 1 remembered pop-up(s)'; the selector never appears in tool output

## 9. Blocked site: the load strategy escalates
*Tests:* load playbook, benching after two failures, escalation to load+1500ms *(needs the local fixture server)*

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Using the navigate tool (not read_url), open http://127.0.0.1:8765/challenge three times in a row. It is a bot-check test page that answers with an error; that is expected. After each visit copy anything the result says that starts with "memory:". Reply with `V1=<text or none>`, `V2=<text or none>`, `V3=<text or none>`.
```
**Success:** visits 1 and 2 show no strategy note; visit 3 says 'learned load strategy load+1500ms'; memory shows the default strategy benched

## 10. Safety limits and sensitive sites
*Tests:* scheme and private-address blocking, metadata endpoint, memory skips sensitive sites

```text
Use the cloakbrowser MCP tools. Do not use any other browser or web tool. Try each of these with the navigate tool. For A to D, say whether the cloakbrowser tool's OWN safety policy allowed the navigation or blocked it (the tool answers with an ERROR); a website refusing you with an HTTP error does not count as blocked by the tool. Do not try to work around a block. (A) file:///C:/Windows/win.ini  (B) http://127.0.0.1:80/  (C) http://169.254.169.254/latest/meta-data/  (D) https://www.paypal.com/signin . Reply with five lines: `A=blocked|allowed`, `B=...`, `C=...`, `D=...`, and `D_STATUS=<the HTTP status code the PayPal site returned>`.
```
**Success:** A, B, C blocked by the tool (ERROR); D allowed by the tool (the site may still answer 403, which is reported as D_STATUS); memory holds nothing about paypal

