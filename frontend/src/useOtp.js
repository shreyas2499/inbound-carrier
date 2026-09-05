import { useCallback, useEffect, useRef, useState } from 'react'
import { peekOtp } from './api.js'

const POLL_MS = 1500
const params = new URLSearchParams(typeof location !== 'undefined' ? location.search : '')

// Central state machine for the device: idle → waiting → code → verified.
// Live mode polls the adapter's /otp/peek. Demo mode (opened as a file, or
// ?demo=1) self-drives so every state is reviewable without a backend.
export function useOtp() {
  const [phase, setPhase] = useState('idle') // idle | waiting | code | verified
  const [mc, setMc] = useState(null)
  const [code, setCode] = useState('')
  const [secsLeft, setSecsLeft] = useState(0)
  const [ttl, setTtl] = useState(180)

  const pollRef = useRef(null)
  const tickRef = useRef(null)
  const mcRef = useRef(null)
  const verifiedRef = useRef(false)
  const demoRef = useRef(
    params.get('demo') === '1' ||
    (typeof location !== 'undefined' && location.protocol === 'file:')
  )

  const clearTimers = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null }
  }

  const markVerified = useCallback(() => {
    verifiedRef.current = true
    if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null }
    setPhase('verified')
  }, [])

  const showCode = useCallback((c, left, t) => {
    if (verifiedRef.current) return
    setCode(String(c)); setSecsLeft(left); setTtl(t || 180); setPhase('code')
  }, [])

  // Self-driving demo: mint a code, count it down, cycle once, then verify.
  const startDemo = useCallback(() => {
    clearTimers()
    const T = 30
    let cycles = 0
    let left = 0
    setPhase('waiting')
    const cycle = () => {
      showCode(String(Math.floor(100000 + Math.random() * 900000)), T, T)
      left = T
      tickRef.current = setInterval(() => {
        left -= 1
        if (left <= 0) {
          clearInterval(tickRef.current); tickRef.current = null
          cycles += 1
          if (cycles >= 2) { markVerified(); return }
          cycle(); return
        }
        setSecsLeft(left)
      }, 1000)
    }
    setTimeout(cycle, 1600)
  }, [markVerified, showCode])

  const poll = useCallback(async () => {
    try {
      const ctrl = new AbortController()
      const to = setTimeout(() => ctrl.abort(), POLL_MS - 200)
      const d = await peekOtp(mcRef.current, ctrl.signal)
      clearTimeout(to)
      if (d.verified) { markVerified(); return }
      if (d.status === 'active' && d.code) {
        showCode(d.code, Number(d.expires_in ?? d.ttl ?? 0), Number(d.ttl ?? 180))
      } else if (!verifiedRef.current) {
        setPhase('waiting')
      }
    } catch {
      // Backend unreachable (e.g. previewing the built file directly): fall into
      // demo so the visual states are still reviewable instead of spinning.
      if (typeof location !== 'undefined' && location.protocol === 'file:') {
        demoRef.current = true
        clearTimers()
        startDemo()
        return
      }
      if (!verifiedRef.current) setPhase('waiting')
    }
  }, [markVerified, showCode, startDemo])

  const start = useCallback((value) => {
    const digits = String(value || '').replace(/\D/g, '')
    if (digits.length < 4) return false
    verifiedRef.current = false
    mcRef.current = digits
    setMc(digits)
    setPhase('waiting')
    if (demoRef.current) {
      startDemo()
    } else {
      poll()
      pollRef.current = setInterval(poll, POLL_MS)
    }
    return true
  }, [poll, startDemo])

  const stop = useCallback(() => {
    clearTimers()
    verifiedRef.current = false
    mcRef.current = null
    setMc(null)
    setPhase('idle')
  }, [])

  useEffect(() => () => clearTimers(), [])

  return { phase, mc, code, secsLeft, ttl, start, stop }
}
