import { useCallback, useEffect, useRef, useState } from 'react'
import { peekOtp } from './api.js'

const POLL_MS = 1500
// Deliberately LONGER than POLL_MS. It used to be POLL_MS - 200, which meant a
// backend slower than 1.3s aborted every single poll -- and because a failed poll
// keeps the current screen, the notification froze on-screen forever. /otp/peek
// does a client-side scan through Twin, so 1.3s is well inside its normal range.
const FETCH_TIMEOUT_MS = 5000
const params = new URLSearchParams(typeof location !== 'undefined' ? location.search : '')
const rnd = () => String(Math.floor(100000 + Math.random() * 900000))

// State machine for the carrier device: idle | waiting | code | verified.
//
// Live mode renders whatever the adapter's /otp/peek reports RIGHT NOW -- there is
// no sticky local state. So when a code's ~3-minute life ends the screen clears to
// blank (ready for the next message rather than displaying the stale one), and a
// freshly issued code -- even for a carrier who was already verified -- shows up on
// the next poll.
//
// Expiry is enforced in BOTH layers, on purpose. The backend decides (peek returns
// status "none" once expires_at has passed) and the client holds an independent
// absolute deadline taken from the same expires_in. Either alone is enough; the
// pair means a stalled, slow or failing poll loop can no longer leave a dead code
// sitting on the screen, which is exactly how it got stuck before.
export function useOtp() {
  const [phase, setPhase] = useState('idle')
  const [mc, setMc] = useState(null)
  const [code, setCode] = useState('')
  const [secsLeft, setSecsLeft] = useState(0)
  const [ttl, setTtl] = useState(180)

  const pollRef = useRef(null)
  const tickRef = useRef(null)
  const demoRef = useRef(null)
  const demoTickRef = useRef(null)
  const mcRef = useRef(null)
  const deadlineRef = useRef(0)   // absolute ms timestamp the current code dies at
  const isDemo = useRef(
    params.get('demo') === '1' ||
    (typeof location !== 'undefined' && location.protocol === 'file:')
  )

  const stopTimers = () => {
    for (const r of [pollRef, tickRef, demoRef, demoTickRef]) {
      if (r.current) { clearInterval(r.current); clearTimeout(r.current); r.current = null }
    }
  }

  const clearScreen = useCallback(() => {
    deadlineRef.current = 0
    setCode('')
    setSecsLeft(0)
    setPhase('waiting')
  }, [])

  // Reflect the backend's current status, nothing more.
  const applyStatus = useCallback((d) => {
    const status = d && (d.status || (d.verified ? 'verified' : null))
    if (status === 'verified') {
      // A verified panel is still tied to the challenge's life: once the code it
      // confirmed expires, the screen goes back to blank like any other message.
      if (d.expires_in != null) deadlineRef.current = Date.now() + Number(d.expires_in) * 1000
      setPhase('verified')
      return
    }
    if (status === 'active' && d.code) {
      const secs = Number(d.expires_in ?? d.ttl ?? 0)
      deadlineRef.current = Date.now() + secs * 1000
      setCode(String(d.code))
      setTtl(Number(d.ttl ?? 180))
      setSecsLeft(secs)
      setPhase('code')
      return
    }
    clearScreen() // status none -> blank lock screen, ready for the next code
  }, [clearScreen])

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

  // Local 1s ticker. It runs the countdown between polls (so the meter is smooth
  // rather than stepping whenever a response lands) and, critically, it is what
  // clears the screen at zero. That clear does not depend on the network.
  const startTicker = useCallback(() => {
    if (tickRef.current) return
    tickRef.current = setInterval(() => {
      if (!deadlineRef.current) return
      const left = Math.max(0, Math.round((deadlineRef.current - Date.now()) / 1000))
      setSecsLeft(left)
      if (left <= 0) clearScreen()
    }, 1000)
  }, [clearScreen])

  // Self-scheduling rather than setInterval: a slow response delays the next poll
  // instead of stacking a queue of overlapping requests on top of it.
  const poll = useCallback(async () => {
    try {
      const ctrl = new AbortController()
      const to = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS)
      let d
      try {
        d = await peekOtp(mcRef.current, ctrl.signal)
      } finally {
        clearTimeout(to)
      }
      applyStatus(d)
    } catch {
      // On a deployed page a transient network error should NOT invent codes --
      // keep the current screen and let the next poll retry. The local deadline
      // still runs, so a persistent outage expires the screen on schedule instead
      // of pinning a dead code to it. Only a local file preview (no backend at
      // all) falls back to the demo driver.
      if (typeof location !== 'undefined' && location.protocol === 'file:') {
        isDemo.current = true
        runDemo()
        return
      }
    }
    if (mcRef.current) pollRef.current = setTimeout(poll, POLL_MS)
  }, [applyStatus, runDemo])

  const start = useCallback((value) => {
    const digits = String(value || '').replace(/\D/g, '')
    if (digits.length < 4) return false
    mcRef.current = digits
    setMc(digits)
    deadlineRef.current = 0
    setPhase('waiting')
    if (isDemo.current) {
      runDemo()
    } else {
      startTicker()
      poll()
    }
    return true
  }, [poll, runDemo, startTicker])

  const stop = useCallback(() => {
    stopTimers()
    mcRef.current = null
    deadlineRef.current = 0
    setMc(null)
    setPhase('idle')
  }, [])

  useEffect(() => () => stopTimers(), [])

  return { phase, mc, code, secsLeft, ttl, start, stop }
}
