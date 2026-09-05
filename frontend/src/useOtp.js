import { useCallback, useEffect, useRef, useState } from 'react'
import { peekOtp } from './api.js'

const POLL_MS = 1500
const params = new URLSearchParams(typeof location !== 'undefined' ? location.search : '')
const rnd = () => String(Math.floor(100000 + Math.random() * 900000))

// State machine for the carrier device: idle | waiting | code | verified.
// Live mode renders whatever the adapter's /otp/peek reports RIGHT NOW — there is
// no sticky local state. So when a code's ~3-minute life ends the screen clears to
// blank (ready for the next message rather than displaying the stale one), and a
// freshly issued code — even for a carrier who was already verified — shows up on
// the next poll.
export function useOtp() {
  const [phase, setPhase] = useState('idle')
  const [mc, setMc] = useState(null)
  const [code, setCode] = useState('')
  const [secsLeft, setSecsLeft] = useState(0)
  const [ttl, setTtl] = useState(180)

  const pollRef = useRef(null)
  const demoRef = useRef(null)
  const demoTickRef = useRef(null)
  const mcRef = useRef(null)
  const isDemo = useRef(
    params.get('demo') === '1' ||
    (typeof location !== 'undefined' && location.protocol === 'file:')
  )

  const stopTimers = () => {
    for (const r of [pollRef, demoRef, demoTickRef]) {
      if (r.current) { clearInterval(r.current); clearTimeout(r.current); r.current = null }
    }
  }

  // Reflect the backend's current status, nothing more.
  const applyStatus = useCallback((d) => {
    if (d && (d.status === 'verified' || d.verified)) { setPhase('verified'); return }
    if (d && d.status === 'active' && d.code) {
      setCode(String(d.code))
      setTtl(Number(d.ttl ?? 180))
      setSecsLeft(Number(d.expires_in ?? d.ttl ?? 0))
      setPhase('code')
      return
    }
    setPhase('waiting') // status none -> blank lock screen, ready for the next code
  }, [])

  // Preview-only driver (no backend). Loops the real states so the UI is reviewable:
  // code -> expire(blank) -> code -> verified -> blank -> a NEW code (a verified
  // caller re-requesting) -> verified -> ...
  const runDemo = useCallback(() => {
    stopTimers()
    const steps = [
      { p: 'waiting', ms: 1400 },
      { p: 'code', secs: 12, ms: 12500, countdown: true },
      { p: 'waiting', ms: 1600 },
      { p: 'code', secs: 10, ms: 3500 },
      { p: 'verified', ms: 3500 },
      { p: 'waiting', ms: 1800 },
      { p: 'code', secs: 10, ms: 3500 },
      { p: 'verified', ms: 3500 },
    ]
    let i = 0
    const run = () => {
      const s = steps[i % steps.length]; i++
      if (demoTickRef.current) { clearInterval(demoTickRef.current); demoTickRef.current = null }
      if (s.p === 'code') {
        setCode(rnd()); setTtl(s.secs); setSecsLeft(s.secs); setPhase('code')
        if (s.countdown) {
          let left = s.secs
          demoTickRef.current = setInterval(() => {
            left -= 1; setSecsLeft(Math.max(0, left))
            if (left <= 0 && demoTickRef.current) { clearInterval(demoTickRef.current); demoTickRef.current = null }
          }, 1000)
        }
      } else {
        setPhase(s.p)
      }
      demoRef.current = setTimeout(run, s.ms)
    }
    run()
  }, [])

  const poll = useCallback(async () => {
    try {
      const ctrl = new AbortController()
      const to = setTimeout(() => ctrl.abort(), POLL_MS - 200)
      const d = await peekOtp(mcRef.current, ctrl.signal)
      clearTimeout(to)
      applyStatus(d)
    } catch {
      // On a deployed page a transient network error should NOT invent codes —
      // keep the current screen and let the next poll retry. Only a local file
      // preview (no backend at all) falls back to the demo driver.
      if (typeof location !== 'undefined' && location.protocol === 'file:') {
        isDemo.current = true
        runDemo()
      }
    }
  }, [applyStatus, runDemo])

  const start = useCallback((value) => {
    const digits = String(value || '').replace(/\D/g, '')
    if (digits.length < 4) return false
    mcRef.current = digits
    setMc(digits)
    setPhase('waiting')
    if (isDemo.current) {
      runDemo()
    } else {
      poll()
      pollRef.current = setInterval(poll, POLL_MS)
    }
    return true
  }, [poll, runDemo])

  const stop = useCallback(() => {
    stopTimers()
    mcRef.current = null
    setMc(null)
    setPhase('idle')
  }, [])

  useEffect(() => () => stopTimers(), [])

  return { phase, mc, code, secsLeft, ttl, start, stop }
}
