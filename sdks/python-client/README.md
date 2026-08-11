# codify-client

Python client SDK for the [codify](https://github.com/codify-ai/codify)
server API.

`codify-client` is a typed client for driving codify sessions over the
server's HTTP + SSE API — creating sessions, sending turns, and streaming
responses. It shares the `StreamEvent` / `SessionStreamEventType` types that the
server emits, so streamed envelopes are validated against a single source of
truth.

It is released in lockstep with the core `codify` package at a matching
version:

```bash
pip install codify-client
```

See the [codify repository](https://github.com/codify-ai/codify) for full
documentation.
