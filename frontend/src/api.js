// Adapter base URL, injected at build time (VITE_ADAPTER_BASE). Empty string =
// same origin (used in local dev via the vite proxy). In production on Railway,
// set VITE_ADAPTER_BASE to the adapter service's public URL.
// Prefer the build-time override (VITE_ADAPTER_BASE). If it isn't injected, fall
// back to same-origin in local dev (so the vite proxy handles /otp) and to the
// adapter's known Railway URL in a production build -- this keeps the deployed
// app working even when the platform doesn't pass the build arg through.
const FALLBACK = import.meta.env.DEV
  ? ''
  : 'https://inbound-carrier-production-aeb4.up.railway.app'
export const API_BASE = (import.meta.env.VITE_ADAPTER_BASE || FALLBACK).replace(/\/+$/, '')

// The carrier "device" reads the currently-active code for an MC. This endpoint
// is intentionally public (a real phone carries no API key); it's the demo
// stand-in for an SMS arriving on the carrier's handset.
export async function peekOtp(mc, signal) {
  const r = await fetch(`${API_BASE}/otp/peek?mc=${encodeURIComponent(mc)}`, { signal })
  if (!r.ok) throw new Error('peek failed: ' + r.status)
  return r.json()
}
