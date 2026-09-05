# Carrier Verification Device (frontend)

A small React app that simulates the phone where a carrier receives its one-time
verification code. It's the demo stand-in for SMS delivery: the carrier enters
their MC number, and when the agent requests verification mid-call, the code
arrives on a lock-screen "text message" to be read back to the agent.

This is a **separate service** from the Flask adapter. It calls the adapter's
public `GET /otp/peek?mc=<mc>` endpoint to display the active code.

## Stack

- React 18 + Vite
- `vite-plugin-singlefile` — build emits one self-contained `dist/index.html`
- CSS Modules for component styles; theme tokens in `src/index.css` (light/dark aware)

## Structure

```
src/
  main.jsx              app entry
  App.jsx               composes Landing + Phone
  useOtp.js             state machine: idle → waiting → code → verified (+ demo driver)
  api.js                adapter base URL + peekOtp()
  Landing.jsx / .module.css   the MC-entry card
  Phone.jsx  / .module.css    the phone popup + lock-screen SMS notification
```

## Develop

```bash
npm install
npm run dev          # http://localhost:5173  (proxies /otp -> localhost:8000)
```

Run the Flask adapter locally on :8000 so `/otp/peek` resolves, or append
`?demo=1` to the URL to self-drive every state without a backend.

## Build

```bash
VITE_ADAPTER_BASE=https://<adapter-url> npm run build
# -> dist/index.html  (single file, JS + CSS inlined)
```

## Deploy (Railway)

Deploy this folder as its own service. Set the service variable
`VITE_ADAPTER_BASE` to the adapter's public URL (baked in at build time), then
the included `Dockerfile` builds and serves `dist/` with `serve` on `$PORT`.

> Note: the adapter's `/otp/peek` must send permissive CORS headers, since this
> app calls it from a different origin.
