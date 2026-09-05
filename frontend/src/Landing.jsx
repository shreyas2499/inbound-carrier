import { useState } from 'react'
import s from './Landing.module.css'

export default function Landing({ onConnect }) {
  const [val, setVal] = useState('')
  const [shake, setShake] = useState(false)

  const submit = () => {
    const ok = onConnect(val)
    if (!ok) {
      setShake(true)
      setTimeout(() => setShake(false), 400)
    }
  }

  return (
    <main className={s.card} aria-label="Carrier verification">
      <div className={s.top}>
        <span className={s.dot} />
        <div className={s.titles}>
          <h1 className={s.h1}>Carrier Verification</h1>
          <div className={s.sub}>Enter your MC number to receive your code</div>
        </div>
        <span className={s.badge}>Demo Device</span>
      </div>

      <div className={s.body}>
        <label className={s.label} htmlFor="mc">MC / Docket number</label>
        <div className={`${s.row} ${shake ? s.shake : ''}`}>
          <input
            id="mc"
            className={s.input}
            inputMode="numeric"
            autoComplete="off"
            placeholder="e.g. 872144"
            value={val}
            onChange={(e) => setVal(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            aria-label="MC number"
          />
          <button className={s.button} onClick={submit}>Connect</button>
        </div>
        <p className={s.hint}>
          This stands in for the phone where a real carrier would receive a one-time
          code by text. When the agent requests verification on your call, the message
          arrives on the device — read the code back to the agent.
        </p>
      </div>

      <div className={s.foot}>Simulated carrier device — a demo stand-in for SMS delivery.</div>
    </main>
  )
}
