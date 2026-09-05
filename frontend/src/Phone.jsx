import { useEffect, useState } from 'react'
import s from './Phone.module.css'

function useClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 15000)
    return () => clearInterval(t)
  }, [])
  const h = (now.getHours() % 12) || 12
  const m = String(now.getMinutes()).padStart(2, '0')
  return {
    time: `${h}:${m}`,
    date: now.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
  }
}

function fmt(sec) {
  sec = Math.max(0, Math.ceil(sec))
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

export default function Phone({ open, mc, phase, code, secsLeft, ttl, onClose }) {
  const clock = useClock()
  const frac = Math.max(0, Math.min(1, ttl ? secsLeft / ttl : 0))
  const low = secsLeft <= 20

  return (
    <div
      className={`${s.overlay} ${open ? s.open : ''}`}
      aria-hidden={!open}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className={s.phone} role="dialog" aria-label="Carrier phone">
        <button className={s.close} onClick={onClose} aria-label="Close">&times;</button>

        <div className={s.screen}>
          <div className={s.island} />

          <div className={s.statusbar}>
            <span>{clock.time}</span>
            <span className={s.icons}>
              <span className={s.bars}><i /><i /><i /><i /></span>
              <svg width="16" height="12" viewBox="0 0 16 12" fill="#fff" aria-hidden="true">
                <path d="M8 11.2 .6 3.9A10.4 10.4 0 0 1 8 .8a10.4 10.4 0 0 1 7.4 3.1L8 11.2Z" opacity="0.95" />
              </svg>
              <svg width="24" height="12" viewBox="0 0 24 12" fill="none" aria-hidden="true">
                <rect x="0.7" y="0.7" width="19" height="10.6" rx="3" stroke="#fff" opacity="0.6" />
                <rect x="2.2" y="2.2" width="14" height="7.6" rx="1.6" fill="#fff" />
                <rect x="21" y="4" width="1.6" height="4" rx="0.8" fill="#fff" opacity="0.6" />
              </svg>
            </span>
          </div>

          <div className={s.lock}>
            <div className={s.lockIcon} aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="#fff">
                <path d="M12 1.8a4.7 4.7 0 0 0-4.7 4.7V9H6.4A1.4 1.4 0 0 0 5 10.4v9.2A1.4 1.4 0 0 0 6.4 21h11.2a1.4 1.4 0 0 0 1.4-1.4v-9.2A1.4 1.4 0 0 0 17.6 9h-.9V6.5A4.7 4.7 0 0 0 12 1.8Zm2.7 7.2H9.3V6.5a2.7 2.7 0 0 1 5.4 0V9Z" />
              </svg>
            </div>
            <div className={s.lockTime}>{clock.time}</div>
            <div className={s.lockDate}>{clock.date}</div>
          </div>

          <div className={s.notifArea}>
            {phase === 'waiting' && (
              <div className={s.waitPill}>
                <span className={s.miniSpin} /> Waiting for verification code…
              </div>
            )}

            {phase === 'code' && (
              <div className={s.notif}>
                <div className={s.nHead}>
                  <span className={s.nApp}>
                    <svg viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
                      <path d="M12 3C6.5 3 2 6.6 2 11c0 2.4 1.3 4.6 3.5 6-.3 1.1-1 2.3-2 3.2 1.6-.1 3.4-.6 4.8-1.6 1.2.3 2.4.5 3.7.5 5.5 0 10-3.6 10-8s-4.5-8-10-8Z" />
                    </svg>
                  </span>
                  <span className={s.nTitle}>MESSAGES</span>
                  <span className={s.nTime}>now</span>
                </div>
                <div className={s.nSender}>FreightVerify</div>
                <div className={s.nMsg}>Your carrier verification code is</div>
                <div className={s.nCode}>{code.split('').join(' ')}</div>
                <div className={s.nMeter}>
                  <i style={{ transform: `scaleX(${frac})`, background: low ? '#c2620a' : '#2563eb' }} />
                </div>
                <div className={s.nFoot}>
                  <b>Expires in {fmt(secsLeft)}</b> · Never share this code. FreightVerify will never call to ask for it.
                </div>
              </div>
            )}

            {phase === 'verified' && (
              <div className={`${s.notif} ${s.ok}`}>
                <div className={s.okCheck} aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20 6 9 17l-5-5" />
                  </svg>
                </div>
                <div className={s.okTxt}>
                  <b>Identity verified</b>
                  <span>The agent confirmed your code.</span>
                </div>
              </div>
            )}
          </div>

          <div className={s.phoneFoot}><div className={s.homeInd} /></div>
        </div>

        <div className={s.caption}>
          Listening for <b>MC {mc}</b> · <span className={s.link} onClick={onClose}>change</span>
        </div>
      </div>
    </div>
  )
}
