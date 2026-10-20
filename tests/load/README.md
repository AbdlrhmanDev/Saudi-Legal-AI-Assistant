# Load test

`chat_load.js` is a [k6](https://k6.io) script for the public chat path. It is not part of the
pytest suite; run it against a staging deployment that uses production-like providers.

```bash
k6 run -e BASE_URL=https://staging-api.example.com tests/load/chat_load.js
```

Before running:

- Raise `RATE_LIMIT_CHAT_PER_MINUTE` on staging, or keep the per-VU `X-Forwarded-For` addresses the
  script sends and set `TRUSTED_PROXY_COUNT` to the number of proxies in front of the API.
- With `LLM_PROVIDER=openai`, check the OpenAI account's requests/tokens-per-minute limits: at 150
  concurrent users with ~10 s think time, expect ~10 chat requests per second at peak.

Pass criteria (PRD v2 section 15): chat p95 under 8 s, error rate under 1 %, freshness p95 under
500 ms. Read `docs/scaling.md` for how to size replicas and database connections from the result.
