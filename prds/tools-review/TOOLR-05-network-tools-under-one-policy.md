---
title: "TOOLR-05: Every network tool under one destination policy"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "fetch_page, web_search, the Outlook Graph backend and any tool using agenthicc_http_client: allow-list, SSRF protection, redirects, response bounds, content handling and error classification"
related_prds: [TOOLR-00, TOOLR-04, PRD-04, PRD-108]
tags: [tools, network, security]
---

# TOOLR-05: Every network tool under one destination policy

## 1. Problem

TOOLR-00 N1–N3. CLAUDE.md's invariant is that network access must pass the
configured allow-list. The browser tools honour it with care: an origin
policy, DNS revalidation on every navigation, port limits and private-address
blocking (`tools/cloakbrowser/policy.py`). The plain HTTP tools do not.

- **`fetch_page`** (`skills/web_search.py:181`):
  - never consults `security.network_allow_list`;
  - follows redirects anywhere;
  - can reach `127.0.0.1`, private ranges and cloud metadata
    (`169.254.169.254`);
  - reads the whole body into memory;
  - parses it on the event loop;
  - returns all of its visible text, with no bound;
  - treats a PDF or binary body as HTML.

  A prompt-injected page can make the agent fetch an internal service or a
  multi-megabyte page.
- **`agenthicc_http_client()`** (`tools/http.py`) has no policy parameter, so
  every caller would have to remember it, and none do: web search, Outlook,
  the skill installer, ads and welcome.
- **Status errors raise.** `raise_for_status()` raises `HTTPStatusError`, which
  `_is_network_error` does not count. A 404 or 403 ends the tool call with an
  exception, and Outlook's `_safe_call` lets a 401 through the same way.

## 2. Goals

1. **A policy-bound client.** `agenthicc_http_client(policy=…)` takes an
   `HttpDestinationPolicy`, and tool code may not construct a client without
   one. The policy is built once per session from
   `security.network_allow_list` and reuses `BrowserPolicy`'s origin parsing
   and address checks. Its rules:
   - only `http` and `https` schemes;
   - the host must match the allow-list (empty means deny, unless
     `security.network_allow_all = true`);
   - every resolved address is checked, so loopback, private, link-local,
     multicast and metadata ranges are refused unless explicitly allowed;
   - the policy is re-checked on every redirect hop (at most 5) through an
     httpx event hook;
   - and on every connection, to defeat DNS rebinding.
2. **Bounded responses.** Bodies are streamed:
   - stop at `tools.http_max_response_bytes` (default 5 MB) with
     `truncated: true`;
   - decode by the declared charset;
   - extract text off the event loop;
   - bound the returned text to `tools.fetch_max_chars` (default 40,000),
     preferring the main content block;
   - with an `offset` argument for paging through the rest.
3. **Content-aware.** `text/html` is extracted to text, with links listed
   separately (≤ 50). `text/*` and JSON are returned as-is within the bound.
   Other types return the type, size and `error_code="unsupported_content"`
   instead of mojibake.
4. **Classified errors.**
   - HTTP 4xx and 5xx return `ok: false, error_code: "http_status"`, the status,
     and `retryable` (true for 408, 429 and 5xx).
   - A policy refusal returns `error_code: "outside_allow_list"`, naming the
     host and how to allow it.
   - Network failures return `error_code: "network"`.
5. **Every network tool uses it.** `web_search`, `fetch_page` and the Outlook
   Graph backend use the session policy. The Brave API host and
   `graph.microsoft.com` are added to the effective allow-list only when the
   feature is configured, and the docs say so.

## 3. Non-goals

- Changing the browser tools, which already meet this bar. This PRD brings
  the plain HTTP tools up to it.
- A crawler or a cache for fetched pages.

## 4. Acceptance criteria

1. With an empty allow-list, `fetch_page("https://example.com")` returns
   `outside_allow_list` and makes no connection (asserted with a transport
   mock).
2. An allowed page that redirects to `http://169.254.169.254/` is refused at
   the redirect hop.
3. A host whose DNS answer is `127.0.0.1` is refused, unless loopback is
   allowed.
4. A 20 MB body stops at 5 MB without holding more than that in memory, and
   returns `truncated: true`. A 100,000-character page returns at most 40,000
   characters plus an offset for the rest.
5. A 404 returns `http_status`, status 404 and `retryable: false`, and raises
   nothing.
6. A PDF URL returns `unsupported_content` and `content_type`.
7. No module outside `tools/http.py` constructs `httpx.AsyncClient`, enforced
   by a test that greps the source.
